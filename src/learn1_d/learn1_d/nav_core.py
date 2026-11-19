"""底盘交互与导航能力：动作等待、地图/点位查询、定位、转向、防撞、播报。

一律是"调底盘 + 等结果"的同步工具函数，不含导览流程；流程写在 map_nav_test.py。
"""

import math
import time

from learn1_d.tour_config import (
    ALIGN_MAX_ATTEMPTS,
    ALIGN_SETTLE_S,
    ALIGN_TOLERANCE_RAD,
    GUARD_CLEAR_S,
    GUARD_CONFIRM,
    GUARD_GRACE_S,
    GUARD_MAX_YIELDS,
    GUARD_POLL_S,
    GUARD_STALE_ABORT_S,
    GUARD_WAIT_S,
    NO_PROGRESS_S,
    RELOC_TOL_M,
    RELOC_TOL_RAD,
    POLL_S,
    MIN_BATTERY,
    MIN_LOCALIZATION_CONF,
    PREFLIGHT_GUARD_S,
    RECOVER_MAX_ATTEMPTS,
    RECOVER_RETRY_S,
    REDISPATCH_MAX_ATTEMPTS,
    REDISPATCH_SETTLE_S,
    RELOCALIZE_TRIGGER,
    TERMINAL_FAIL,
)

# 底盘在终点前被近物挡住时的判定：剩余距离不超过 2 m，
# 且深度相机看到 0.8 m 内有东西，就按“被挡住”处理而不是当作失败。
NEAR_GOAL_MM = 2000.0
NEAR_GOAL_OBSTACLE_M = 0.8
# 剩余距离变化小于 15 cm 视为没在前进，用来识别终点前的原地微调。
NO_PROGRESS_DIST_MM = 150.0
# 与点位偏差小于这个值时不再下发导航：底盘对极近目标会直接回 failed，
# 日志里表现为 distance_remaining=0、distance_time=0，动作根本没起步。
GOAL_ARRIVE_M = 0.4

# ---------------------------------------------------------------- 动作执行与等待


def classify_action(status):
    """失败优先；明确结束且没有失败标记（含未给 success 字段）也算到达。"""
    if not isinstance(status, dict):
        raise RuntimeError(f"无法识别任务状态：{status}")
    states = {str(status.get(key, "")).strip().lower()
              for key in ("status", "state", "action_state")}
    if states & TERMINAL_FAIL or status.get("success") is False:
        return "failure"
    if states & {"successed", "succeeded", "success"}:
        return "success"
    if (status.get("done") is True or status.get("is_finished") is True
            or states & {"finished", "done", "completed"}):
        # 部分固件结束时只回 finished、不带 success，按到达处理；
        # 真正的失败会在上面的失败标记里被拦下。
        return "success"
    return "running"


def action_id_from(response):
    if not isinstance(response, dict) or response.get("action_id") in (None, ""):
        raise RuntimeError(f"底盘没有返回有效任务编号：{response}")
    return str(response["action_id"])


class ActionFailed(RuntimeError):
    """动作失败；status 保留底盘原始状态，供判断是否属于「未起步」的瞬态拒绝。"""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class ActionBlocked(RuntimeError):
    """前方有障碍，主动取消本次导航；distance 为检测到的最近距离（米）。"""

    def __init__(self, message, distance):
        super().__init__(message)
        self.distance = float(distance)


class SafetyStop(RuntimeError):
    """安全功能失效（断流、定位丢失等）：必须停下整场导览，不能降级继续跑。"""


def never_started(status):
    """动作被判 failed 且从未起步时返回 True。

    底盘拒绝任务时常见 distance_time=0，但 distance_remaining 仍是整段距离，
    所以不能要求两者都为 0；只要没有预计耗时，就按“没有起步”处理，允许重发。
    """
    if not isinstance(status, dict):
        return False
    if "distance_time" in status:
        try:
            return float(status["distance_time"]) == 0.0
        except (TypeError, ValueError):
            return False
    seen = False
    for key in ("distance_time", "distance_remaining"):
        if key not in status:
            continue
        seen = True
        try:
            if float(status[key]) != 0.0:
                return False
        except (TypeError, ValueError):
            return False
    return seen


def wait_action(chassis, label, action_id, timeout_s, guard=None):
    """等待明确成功；失败、查询异常、超时、被挡或 Ctrl+C 都尝试取消本次任务。"""
    completed = False
    try:
        deadline = time.monotonic() + timeout_s
        started = time.monotonic()
        last = None
        blocked_hits = 0
        next_status = 0.0
        next_stale_log = 0.0
        stale_since = None
        progress_time = None
        progress_remaining = None
        progress_since = time.monotonic()
        while time.monotonic() < deadline:
            now = time.monotonic()
            # 起步阶段先不判障碍，等底盘自己走起来；连续多帧才算有人。
            if guard is not None and now - started >= GUARD_GRACE_S:
                distance = guard.distance()
                if distance is None:
                    # 断流时防撞等于失效：先告警，持续断流超过阈值就停车，不盲走。
                    stale_since = stale_since or now
                    if now - stale_since >= GUARD_STALE_ABORT_S:
                        raise SafetyStop(
                            f"{label} 深度相机断流 {now - stale_since:.0f}s，"
                            "前向防撞失效，已取消任务停车")
                    if now >= next_stale_log:
                        print(f"  警告：{label} 收不到深度图，前向防撞暂时失效")
                        next_stale_log = now + 5.0
                else:
                    stale_since = None
                    if guard.blocked():
                        blocked_hits += 1
                    else:
                        blocked_hits = 0
                if blocked_hits >= GUARD_CONFIRM:
                    raise ActionBlocked(
                        f"{label} 前方 {distance:.2f} m 有障碍，停车等待让行",
                        distance)
            if now >= next_status:
                status = chassis.get_action_status(action_id)
                verdict = classify_action(status)
                if status != last:
                    print(f"  {label} 状态：{status}")
                    last = status
                if verdict == "success":
                    completed = True
                    return
                if verdict == "failure":
                    raise ActionFailed(f"{label} 失败或结果不明确：{status}", status)
                # 导航任务被受理却完全不动：剩余距离/预计时间一直不变，说明底盘没起步。
                # 终点前被挡住时剩余距离会在几厘米内来回跳，不能只看是否完全相等。
                if status.get("type") == "move_to":
                    cur_time = status.get("distance_time")
                    try:
                        cur_remaining = float(status.get("distance_remaining"))
                    except (TypeError, ValueError):
                        cur_remaining = None
                    improved = progress_time is None or cur_time != progress_time
                    if (not improved and cur_remaining is not None
                            and progress_remaining is not None
                            and abs(cur_remaining - progress_remaining) > NO_PROGRESS_DIST_MM):
                        improved = True
                    if improved:
                        progress_time = cur_time
                        progress_remaining = cur_remaining
                        progress_since = now
                    elif now - progress_since >= NO_PROGRESS_S:
                        raise ActionFailed(
                            f"{label} 底盘 {NO_PROGRESS_S:.0f}s 没有任何进展，已取消任务",
                            status)
                next_status = now + POLL_S
            # 开了防撞就按更快的节奏采样，避免等人贴到车前才刹住。
            time.sleep(GUARD_POLL_S if guard is not None else POLL_S)
        raise TimeoutError(f"{label} 超时（{timeout_s}s）")
    finally:
        if not completed:
            try:
                chassis.cancel_action([action_id])
                print(f"已请求取消任务 {action_id}")
            except Exception as exc:
                print(f"取消任务失败，请检查底盘并使用急停：{exc}")


def wait_until_stopped(chassis, action_id, timeout_s=5.0):
    """取消是异步的：等上一个任务真正结束，避免重发时被底盘拒绝。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if classify_action(chassis.get_action_status(action_id)) != "running":
                return True
        except Exception as exc:  # noqa: BLE001 - 查询抖动继续等
            print(f"  查询任务状态失败：{exc}")
        time.sleep(POLL_S)
    print(f"  警告：任务 {action_id} 取消后 {timeout_s:.0f}s 内未确认结束")
    return False


# ---------------------------------------------------------------- 地图/点位/轨道查询


def unwrap_list(rv, *keys):
    if isinstance(rv, list):
        return rv
    if isinstance(rv, dict):
        for key in keys:
            if isinstance(rv.get(key), list):
                return rv[key]
    return []


def map_short_name(full_path):
    """get_current_map 返回全路径，取末段去扩展名，例如 daolannew。"""
    base = str(full_path or "").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


def current_map(chassis):
    rv = chassis.get_current_map() or {}
    return str(rv.get("map_name") or ""), map_short_name(rv.get("map_name"))


def read_pois(chassis, map_name=None):
    full = map_name or current_map(chassis)[0]
    rv = chassis.get_pois(map_name=full or None, point_name=None)
    pois = {}
    for poi in unwrap_list(rv, "pois", "poi", "data"):
        if isinstance(poi, dict):
            name = poi.get("name") or poi.get("point_name")
            if name:
                pois[str(name)] = poi
    return full, pois


def print_pois(chassis, map_name=None):
    full, short = (map_name, map_short_name(map_name)) if map_name else current_map(chassis)
    _, pois = read_pois(chassis, map_name=full)
    print(f"当前地图：{short}（{full}）")
    print(f"点位（{len(pois)} 个）：")
    for name, poi in sorted(pois.items()):
        print(f"  {name:<10} x={poi.get('x')}  y={poi.get('y')}  yaw={poi.get('yaw')}")
    return pois


def check_map(chassis, map_name):
    """启动前只读校验目标地图：必须存在且有点位，避免切图后才发现配置写错。"""
    cur_full, cur_short = current_map(chassis)
    print(f"底盘当前地图：{cur_short or '未知'}（{cur_full or '无'}）")
    if not map_name:
        print("未指定目标地图，沿用当前地图")
        return
    target = map_short_name(map_name)
    _, pois = read_pois(chassis, map_name=map_name)
    if not pois:
        raise ValueError(
            f"地图 {target} 不存在或没有点位，拒绝启动；"
            "请先在底盘建图并录制点位后重试")
    print(f"目标地图：{target}，点位 {len(pois)} 个，校验通过")
    if cur_short and cur_short != target:
        print(f"当前地图 {cur_short} 与目标 {target} 不一致，启动后将自动切换到 {target}")


def read_tracks(chassis, map_name=None):
    """读取当前地图上的轨道，返回 {轨道名: track_id}。"""
    full, _ = current_map(chassis)
    rv = chassis.get_tracks(map_name=map_name or full or None)
    tracks = {}
    for track in unwrap_list(rv, "tracks", "track", "data"):
        if not isinstance(track, dict):
            continue
        name = track.get("track_name") or track.get("track_id")
        track_id = track.get("track_id")
        if name is not None and track_id is not None:
            tracks[str(name)] = track_id
    return tracks


def print_tracks(chassis, map_name=None):
    tracks = read_tracks(chassis, map_name=map_name)
    print(f"轨道（{len(tracks)} 条）：")
    for name, track_id in sorted(tracks.items()):
        print(f"  {name:<20} track_id={track_id}")
    if not tracks:
        print("  （当前地图上没有轨道；请先在 App/底盘里录制轨道）")
    return tracks


def poi_pose_from_map(chassis, poi_name, map_name=None):
    """取某个 POI 在地图里的 (x, y, yaw)，用于重定位初始位姿。"""
    rv = chassis.get_pois(map_name=map_name or None, point_name=poi_name)
    if isinstance(rv, dict) and (rv.get("name") or rv.get("point_name")):
        # 指定 point_name 查询时，部分固件直接返回单个点位 dict。
        entries = [rv]
    else:
        entries = unwrap_list(rv, "pois", "poi", "data")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("point_name")
        if str(name) != poi_name:
            continue
        try:
            return (float(entry["x"]), float(entry["y"]), float(entry["yaw"]))
        except (KeyError, TypeError, ValueError):
            return None
    return None


def check_route(chassis, args, targets, verbose=True):
    """只读核对目标地图的点位和轨道；正式导航和 --plan 共用。"""
    map_name = args.map or current_map(chassis)[0]
    _, pois = read_pois(chassis, map_name=map_name)
    wanted = targets + [p for p in (args.start, args.return_poi) if p]
    missing = sorted(set(wanted) - set(pois))
    if missing:
        raise ValueError(f"地图 {map_name} 缺少点位：{missing}；可用：{sorted(pois)}")
    for name in set(wanted):
        point = pois[name]
        for key in ("x", "y", "yaw"):
            if not isinstance(point, dict) or key not in point:
                raise ValueError(f"点位 {name} 缺少 {key} 字段：{point!r}")
            try:
                value = float(point[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"点位 {name} 的 {key} 不是有效数值：{point[key]!r}") from exc
            if not math.isfinite(value):
                raise ValueError(f"点位 {name} 的 {key} 坐标无效")
    track_id = None
    if args.track:
        tracks = read_tracks(chassis, map_name=map_name)
        if args.track not in tracks:
            raise ValueError(f"地图 {map_name} 缺少轨道 {args.track}；可用：{sorted(tracks)}")
        track_id = tracks[args.track]
    if verbose:
        order = ([args.start] if args.start else []) + targets
        if args.return_poi:
            order.append(args.return_poi)
        print(f"路线地图：{map_name}")
        print("计划：" + " -> ".join(order))
        print(f"行驶方式：沿轨道 {args.track}（id={track_id}）"
              if args.track else "行驶方式：点对点导航")
    return pois, track_id


# ---------------------------------------------------------------- 地图加载与定位


def _accepted_recovery(exc):
    """SDK 的异常文本只有前 200 字；从 HTTP cause 中读取完整任务编号。"""
    response = getattr(getattr(exc, "cause", None), "response", None)
    if response is None:
        return None
    try:
        result = response.json()
    except (ValueError, TypeError):
        return None
    if isinstance(result, dict) and result.get("accepted") is True:
        return action_id_from(result)
    return None


def wait_localization(chassis, timeout_s, pose, force=False):
    """正常定位连续确认；有已知初值时提交重定位，并跟踪单个任务。

    不根据起点猜当前位置，不重载当前地图；仅明确失败的任务允许有限重试。
    HTTP 异常中已受理的任务沿用完整 action_id，结果不明时绝不补发。
    """
    deadline = time.monotonic() + timeout_s
    attempts = 0
    next_recover = 0.0
    last = None
    stable = 0
    active = None
    recovered = False
    needs_recovery = force
    if pose is not None:
        pose = tuple(float(v) for v in pose)
        if len(pose) != 3 or not all(math.isfinite(v) for v in pose):
            raise ValueError("重定位初始位姿必须是有限数值 x,y,yaw")
    if force and pose is None:
        raise ValueError("强制重定位需要明确的 --pose 或 --reloc-poi")
    try:
        while time.monotonic() < deadline:
            rv = chassis.get_localization_status() or {}
            status = rv.get("slam_status")
            if status != last:
                print(f"  定位状态：slam_status={status} "
                      "（0=正常 5=等待重定位 6=定位中 7=加载地图 8=等待传感器）")
                last = status
            if active is not None:
                result = chassis.get_action_status(active)
                verdict = classify_action(result)
                if verdict == "failure":
                    print(f"  重定位任务 {active} 明确失败：{result}")
                    active = None
                    stable = 0
                    needs_recovery = True
                    if attempts >= RECOVER_MAX_ATTEMPTS:
                        return False
                    next_recover = time.monotonic() + RECOVER_RETRY_S
                elif verdict == "success":
                    print(f"  重定位任务 {active} 已完成，等待定位状态稳定")
                    active = None
                    recovered = True
                    needs_recovery = False
                else:
                    stable = 0
                    time.sleep(POLL_S)
                    continue
            # 即使 SLAM 暂时报 0，也不能绕过仍在执行/已经失败的重定位任务。
            if status == 0 and not needs_recovery:
                stable += 1
                if stable >= 3:
                    return True
            else:
                stable = 0
            now = time.monotonic()
            if (not recovered and pose is not None and now >= next_recover
                    and attempts < RECOVER_MAX_ATTEMPTS
                    and (status in RELOCALIZE_TRIGGER or (force and status == 0))):
                x, y, yaw = pose
                attempts += 1
                needs_recovery = True
                print(f"  下发重定位（第 {attempts} 次）：x={x}, y={y}, yaw={yaw}")
                try:
                    response = chassis.recover_localization(
                        x=x, y=y, yaw=yaw,
                        max_time_ms=max(1, int((deadline - now) * 1000)))
                except Exception as exc:
                    active = _accepted_recovery(exc)
                    if active is None:
                        raise RuntimeError(
                            f"重定位提交失败或受理状态不明，停止自动重试：{exc}") from exc
                    print(f"  接口报错但任务已受理，跟踪 action_id={active}，不重复提交")
                else:
                    active = action_id_from(response)
                print(f"  重定位任务编号：{active}")
            time.sleep(POLL_S)
        print("定位等待超时；未开始导航。请检查底盘定位服务、激光数据和地图匹配。")
        if pose is None:
            print("未提供已知当前位置；请在厂家界面定位，或将车放在已知点位，"
                  "用 reloc_poi:=点位名 指定。SDK 未提供明确的全局重定位接口。")
        return False
    finally:
        if active is not None:
            try:
                chassis.cancel_action([active])
                print(f"已请求取消本次重定位任务 {active}；未取消其他任务")
            except Exception as exc:
                print(f"取消重定位失败，请在底盘界面检查任务：{exc}")


def relocalize(chassis, pose, timeout_s=60.0):
    """按给定初始位姿强制重定位，并校验匹配结果是否跑偏。

    与 wait_localization 的区别：不看当前 slam_status，直接重新下发一次匹配，
    结束后比对最终位姿与预期位姿；偏差过大就报错，避免带着错误位姿导航撞墙。
    """
    if pose is None:
        raise ValueError("强制重定位需要初始位姿：用 --pose x,y,yaw 或 --reloc-poi 点位名")
    x, y, yaw = (float(v) for v in pose)
    print(f"强制重定位：初始位姿 x={x:.2f} y={y:.2f} yaw={yaw:.2f}")
    before = chassis.get_pose() or {}
    try:
        bx, by = float(before["x"]), float(before["y"])
    except (KeyError, TypeError, ValueError):
        bx = by = None
    if not wait_localization(chassis, timeout_s, pose, force=True):
        raise RuntimeError(f"强制重定位失败（{timeout_s:.0f}s 内定位未就绪）")

    time.sleep(2.0)  # 等匹配结果稳定后再读数
    pose_now = chassis.get_pose() or {}
    px = float(pose_now["x"])
    py = float(pose_now["y"])
    pyaw = float(pose_now["yaw"])
    dist = math.hypot(px - x, py - y)
    dyaw = abs(normalize_angle(pyaw - yaw))
    print(f"重定位结果：x={px:.2f} y={py:.2f} yaw={pyaw:.2f} "
          f"conf={pose_now.get('confidence')}，与预期相差 "
          f"{dist:.2f} m / {math.degrees(dyaw):.1f}°")
    moved = math.hypot(px - bx, py - by) if bx is not None else float("inf")
    if bx is not None:
        print(f"  与重定位前的位姿相差 {moved:.2f} m")
    if dist > RELOC_TOL_M or dyaw > RELOC_TOL_RAD:
        raise RuntimeError(
            f"重定位结果与声明初值相差 {dist:.2f} m / {math.degrees(dyaw):.1f}°，"
            "拒绝导航。请核对实际位置、车头方向及地图，不自动放宽搜索范围")
    return px, py, pyaw


def load_map(chassis, name, timeout_s, pose, reloc_poi=""):
    """把 name 设为当前地图；已经是当前地图就不重复加载（避免打断定位）。

    返回 (地图短名, 实际使用的重定位位姿)。pose 为空且给了 reloc_poi 时，
    从该地图的点位坐标里取初始位姿。
    """
    full, short = current_map(chassis)
    target = map_short_name(name)
    already_current = bool(short and target == short)

    # 先解析重定位位姿，再开始加载地图，避免和加载过程抢查询。
    if pose is None and reloc_poi:
        query_map = full if already_current else name
        pose = poi_pose_from_map(chassis, reloc_poi, map_name=query_map)
        if pose is not None:
            print(f"重定位初始位姿取自点位 {reloc_poi}："
                  f"x={pose[0]:.2f}, y={pose[1]:.2f}, yaw={pose[2]:.2f}")
        else:
            print(f"警告：地图上找不到点位 {reloc_poi}，无法按点位重定位；"
                  "可在车旁用 --pose x,y,yaw 指定")

    if already_current:
        print(f"当前地图已经是 {short}，保留现有定位，不重复加载地图")
        return target, pose

    print(f"加载地图：{target}（当前：{full or '无'}）")
    # start_localization 只接受地图名，不带路径和扩展名，统一传短名。
    chassis.start_localization(slam_type=1, map_name=target)
    time.sleep(3.0)

    # 调用方统一等待或执行强制重定位，避免同次启动提交两次恢复任务。
    return target, pose


# ---------------------------------------------------------------- 运动与朝向


def normalize_angle(angle):
    """把角度归一化到 [-pi, pi]，用于计算最短转向方向。"""
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def wait_yaw_settled(chassis, target_yaw, timeout_s=ALIGN_SETTLE_S):
    """底盘停车后定位姿态会延迟刷新：最多等 timeout_s 让 yaw 追上目标再判断。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pose = chassis.get_pose() or {}
        try:
            yaw = float(pose["yaw"])
        except (KeyError, TypeError, ValueError):
            yaw = None
        if (yaw is not None and math.isfinite(yaw)
                and abs(normalize_angle(target_yaw - yaw)) <= ALIGN_TOLERANCE_RAD):
            return
        time.sleep(0.2)


def align_to_yaw(chassis, target_yaw, turn_w, label, guard=None,
                 on_blocked=None, on_resume=None):
    """原地转向，把车头对齐到地图里该点位保存的 yaw。

    转向途中同样启用前向防撞：被挡就停车让行，让开后重新转，最多让行
    GUARD_MAX_YIELDS 次，避免讲解转身时把人扫到。
    """
    target = float(target_yaw)
    yields = 0
    attempts = 0
    if guard is not None:
        # 转向时正前方往往就是刚讲解完的展品或墙，切到更近的阈值
        guard.set_turning(True)
    try:
        while True:
            pose = chassis.get_pose() or {}
            current_yaw = float(pose["yaw"])
            if not math.isfinite(current_yaw) or not math.isfinite(target):
                raise ValueError("底盘或目标朝向无效")
            delta = normalize_angle(target - current_yaw)
            if abs(delta) <= ALIGN_TOLERANCE_RAD:
                print(f"{label} 朝向已对齐（yaw={current_yaw:.3f}，"
                      f"偏差 {math.degrees(delta):+.1f}°）")
                break
            if attempts >= ALIGN_MAX_ATTEMPTS:
                raise RuntimeError(
                    f"{label} 转向 {ALIGN_MAX_ATTEMPTS} 次后仍未对齐"
                    f"（当前 {current_yaw:.3f}，目标 {target:.3f}，"
                    f"偏差 {math.degrees(delta):+.1f}°）")

            print(f"{label} 原地重定向（第 {attempts + 1}/{ALIGN_MAX_ATTEMPTS} 次）："
                  f"当前 {current_yaw:.3f} -> 目标 {target:.3f} rad"
                  f"（{math.degrees(delta):+.1f}°）")
            # move_mode=1 表示原地转向：distance 必须为 0，direction 是相对角度（弧度）。
            rv = chassis.move_by(direction=delta, distance=0, move_mode=1, v=0.0, w=turn_w)
            action_id = action_id_from(rv)
            timeout_s = abs(delta) / max(turn_w, 0.05) + 10.0
            try:
                wait_action(chassis, f"{label} 重定向", action_id, timeout_s, guard=guard)
                attempts += 1
            except ActionBlocked as exc:
                yields += 1
                print(f"{exc}")
                if on_blocked is not None:
                    on_blocked()
                if yields > GUARD_MAX_YIELDS:
                    raise RuntimeError(
                        f"{label} 已让行 {GUARD_MAX_YIELDS} 次仍无法转向，放弃这次转向") from exc
                if not wait_guard_clear(guard, GUARD_WAIT_S):
                    raise RuntimeError(
                        f"{label} 等待让行超时（{GUARD_WAIT_S:.0f}s），放弃这次转向") from exc
                wait_until_stopped(chassis, action_id)
                print(f"前方已让开，继续转向 {label}")
                if on_resume is not None:
                    on_resume()
                time.sleep(REDISPATCH_SETTLE_S)
            # 停车后定位姿态会延迟刷新：先等它追上目标，再决定要不要补转
            wait_yaw_settled(chassis, target)
    finally:
        if guard is not None:
            guard.set_turning(False)

    pose = chassis.get_pose() or {}
    print(f"{label} 重定向完成，当前 yaw={float(pose['yaw']):.3f}")


def turn_relative(chassis, delta_rad, turn_w, label, guard=None,
                  on_blocked=None, on_resume=None):
    """相对当前朝向原地转动（负值右转、正值左转），用于到点后转向展品。"""
    pose = chassis.get_pose() or {}
    current_yaw = float(pose["yaw"])
    if not math.isfinite(current_yaw) or not math.isfinite(float(delta_rad)):
        raise ValueError("底盘朝向或转向角度无效")
    align_to_yaw(chassis, normalize_angle(current_yaw + float(delta_rad)),
                 turn_w, label, guard, on_blocked, on_resume)


def _face_target(chassis, poi_name, poi, turn_w, guard, on_blocked, on_resume):
    """目标在车后方（超过 90°）时先原地掉头，否则底盘不会为身后的目标下发运动。"""
    try:
        pose = chassis.get_pose() or {}
        px = float(pose["x"])
        py = float(pose["y"])
        pyaw = float(pose["yaw"])
        bearing = math.atan2(float(poi["y"]) - py, float(poi["x"]) - px)
    except (KeyError, TypeError, ValueError):
        return
    if abs(normalize_angle(bearing - pyaw)) <= math.pi / 3:
        return
    print(f"{poi_name} 在侧后方，先原地调头")
    align_to_yaw(chassis, bearing, turn_w, f"{poi_name} 掉头",
                 guard, on_blocked, on_resume)


def _blocked_near_goal(guard, status):
    """终点前放弃且前方确实有近物时返回障碍距离（米），否则返回 None。"""
    if guard is None or not isinstance(status, dict):
        return None
    try:
        remaining_mm = float(status.get("distance_remaining"))
    except (TypeError, ValueError):
        return None
    if remaining_mm <= 0 or remaining_mm > NEAR_GOAL_MM:
        return None
    distance = guard.distance()
    if distance is None or distance > NEAR_GOAL_OBSTACLE_M:
        return None
    return distance


def _pose_offset(chassis, poi):
    """当前位姿到点位的平面距离（米）；读不到返回 None。"""
    try:
        pose = chassis.get_pose() or {}
        return math.hypot(float(pose["x"]) - float(poi["x"]),
                          float(pose["y"]) - float(poi["y"]))
    except (KeyError, TypeError, ValueError):
        return None


def _localization_brief(chassis):
    """把定位状态压成一行，写进瞬态失败日志，方便下次直接定位原因。"""
    try:
        rv = chassis.get_localization_status() or {}
        return f"slam_status={rv.get('slam_status')}"
    except Exception as exc:  # noqa: BLE001 - 诊断信息读不到不影响导航
        return f"定位状态读取失败：{exc}"


def _localization_lost(chassis):
    """定位状态不是 0 时返回 True（4=未启动定位，同样需要重新匹配）。"""
    try:
        status = (chassis.get_localization_status() or {}).get("slam_status")
    except Exception:  # noqa: BLE001
        return False
    return status not in (0, None)


def goto_poi(chassis, poi_name, poi, timeout_s, track_id=None,
             guard=None, on_blocked=None, on_resume=None, turn_w=None):
    """导航到单个 POI，轮询到底盘动作结束；track_id 非空时沿轨道行驶。

    底盘在换任务过渡期可能瞬间返回 failed 且从未起步，这种情况重发几次；
    前方有障碍时取消任务，等让行后重新下发同一目标，最多让行 GUARD_MAX_YIELDS 次。
    on_blocked / on_resume 分别在刚被挡住、障碍移开准备续行时调用（用于播报）。
    目标在车后方时先原地掉头：底盘对身后的 move_to_poi 常常不下发运动。
    """
    retries = 0
    yields = 0
    use_coord = False
    recovered = False
    offset = _pose_offset(chassis, poi)
    if offset is not None and offset <= GOAL_ARRIVE_M:
        # 已经在点位旁边还下发导航，底盘会当成零长度任务直接判 failed。
        print(f"{poi_name} 已在点位附近（偏差 {offset:.2f} m），不再下发导航")
        return
    if turn_w is not None:
        _face_target(chassis, poi_name, poi, turn_w, guard, on_blocked, on_resume)
    while True:
        if track_id is None and not use_coord:
            print(f"点对点前往 {poi_name}")
            rv = chassis.move_to_poi(poi_name=poi_name)
        elif track_id is None:
            x, y = float(poi["x"]), float(poi["y"])
            yaw = float(poi.get("yaw") or 0.0)
            print(f"改用坐标前往 {poi_name}（x={x:.2f}, y={y:.2f}）")
            rv = chassis.move_to(x, y, yaw)
        else:
            print(f"沿轨道 {track_id} 前往 {poi_name}")
            rv = chassis.move_to_locations_poi(
                track_id, poi_name, uptrack_enabled=True)
        action_id = action_id_from(rv)
        try:
            wait_action(chassis, poi_name, action_id, timeout_s, guard=guard)
            break
        except ActionBlocked as exc:
            yields += 1
            print(f"{exc}")
            if on_blocked is not None:
                on_blocked()
            if yields > GUARD_MAX_YIELDS:
                raise RuntimeError(
                    f"{poi_name} 已让行 {GUARD_MAX_YIELDS} 次仍被挡住，放弃该点位") from exc
            if not wait_guard_clear(guard, GUARD_WAIT_S):
                raise RuntimeError(
                    f"{poi_name} 等待让行超时（{GUARD_WAIT_S:.0f}s），放弃该点位") from exc
            wait_until_stopped(chassis, action_id)
            print(f"前方已让开，重新前往 {poi_name}")
            if on_resume is not None:
                on_resume()
            time.sleep(REDISPATCH_SETTLE_S)
        except ActionFailed as exc:
            # 点位前一两米有人或墙时，底盘会反复微调后自行取消。
            # 此时按“被挡住”处理，播报让行并等待，不要让整场导览直接中止。
            blocked_distance = _blocked_near_goal(guard, exc.status)
            if blocked_distance is not None:
                raise ActionBlocked(
                    f"{poi_name} 在终点前被挡住（前方 {blocked_distance:.2f} m）",
                    blocked_distance) from exc
            # 底盘有时会把“就在点位上”的目标直接回成 failed，
            # 位姿确实在点位附近就按到达处理，别让回程整段中止。
            offset = _pose_offset(chassis, poi)
            if offset is not None and offset <= GOAL_ARRIVE_M:
                print(f"{poi_name} 动作被底盘取消，但已在点位附近"
                      f"（偏差 {offset:.2f} m），按到达处理")
                return
            if never_started(exc.status):
                print(f"{poi_name} 底盘瞬态拒绝（{_localization_brief(chassis)}）")
                # 点名接口连续被拒时换成坐标接口，绕开 POI 名称校验。
                if not use_coord and track_id is None:
                    use_coord = True
                    print(f"{poi_name} 改用坐标导航重试")
                    time.sleep(REDISPATCH_SETTLE_S)
                    continue
                # 定位异常时 move_to 会瞬间失败（move_by_direction 靠里程计还能转），
                # 用当前位姿重新匹配一次再重试，只做一次，避免原地反复重定位。
                if track_id is None and not recovered and _localization_lost(chassis):
                    recovered = True
                    try:
                        pose_now = chassis.get_pose() or {}
                        relocalize(chassis,
                                   (float(pose_now["x"]), float(pose_now["y"]),
                                    float(pose_now["yaw"])))
                    except Exception as reloc_exc:  # noqa: BLE001 - 失败就走正常重发
                        print(f"{poi_name} 重定位失败：{reloc_exc}")
                    else:
                        print(f"{poi_name} 重定位完成，重新尝试前往")
                    continue
                retries += 1
                if retries < REDISPATCH_MAX_ATTEMPTS:
                    print(f"{poi_name} 被判 failed 且从未起步，"
                          f"{REDISPATCH_SETTLE_S:.1f}s 后重发（第 {retries + 1} 次）")
                    time.sleep(REDISPATCH_SETTLE_S)
                    continue
            raise
    pose = chassis.get_pose() or {}
    dx = float(pose["x"]) - float(poi["x"])
    dy = float(pose["y"]) - float(poi["y"])
    error = math.hypot(dx, dy)
    if not math.isfinite(error) or error > 0.3:
        raise RuntimeError(f"{poi_name} 报告成功但到点偏差 {error:.2f} m，超过 0.3 m")
    print(f"到达 {poi_name}，到点偏差 {error:.2f} m")


# ---------------------------------------------------------------- 出发自检


def preflight(chassis, guard=None, min_battery=MIN_BATTERY,
              min_conf=MIN_LOCALIZATION_CONF, guard_wait_s=PREFLIGHT_GUARD_S):
    """出发前自检电量、定位质量、深度图是否出帧，返回阻止出发的问题列表。

    读不到的项只告警不拦截：固件字段有差异时，宁可少查一项也不要卡死导览。
    """
    problems = []
    try:
        pack = chassis.get_battery_pack() or {}
    except Exception as exc:  # noqa: BLE001 - 查询失败只告警
        print(f"  警告：读不到电量：{exc}")
        pack = {}
    try:
        battery = float(pack.get("batteryPercentage"))
    except (TypeError, ValueError):
        battery = None
    if battery is None:
        print("  警告：电量字段缺失，跳过电量检查")
    elif battery < min_battery:
        problems.append(f"电量 {battery:.0f}%，低于 {min_battery:.0f}%")

    try:
        conf = float((chassis.get_pose() or {}).get("confidence"))
    except (TypeError, ValueError):
        conf = None
    if conf is None:
        print("  警告：位姿没有置信度字段，跳过定位质量检查")
    elif conf < min_conf:
        problems.append(f"定位置信度 {conf:.2f}，低于 {min_conf:.2f}")

    if guard is None:
        return problems
    # 防撞节点刚起来时话题可能还没出帧，给它一小段时间确认相机真的在工作。
    deadline = time.monotonic() + guard_wait_s
    while time.monotonic() < deadline:
        if guard.distance() is not None:
            return problems
        time.sleep(GUARD_POLL_S)
    problems.append(f"{guard_wait_s:.0f}s 内收不到深度图，前向防撞不可用")
    return problems


# ---------------------------------------------------------------- 前向防撞


def wait_guard_clear(guard, timeout_s):
    """等前方连续 GUARD_CLEAR_S 秒没有障碍，再恢复导航。"""
    deadline = time.monotonic() + timeout_s
    clear_since = None
    next_log = 0.0
    while time.monotonic() < deadline:
        if guard.clear():
            clear_since = clear_since or time.monotonic()
            if time.monotonic() - clear_since >= GUARD_CLEAR_S:
                return True
        else:
            clear_since = None
            distance = guard.distance()
            now = time.monotonic()
            if distance is not None and now >= next_log:
                print(f"  等待让行：前方 {distance:.2f} m")
                next_log = now + 2.0
        time.sleep(POLL_S)
    return False


def start_guard(topic, stop_distance, turn_distance=None):
    """启动前向防撞：深度相机订阅在后台线程 spin，主流程只读最近距离。"""
    from learn1_d.front_guard import GuardNode
    return GuardNode(topic, stop_distance, turn_distance)


# ---------------------------------------------------------------- 语音播报


def speak_text(text):
    """播报故障只告警并继续导览；Ctrl+C 仍中断整个流程。"""
    try:
        return _speak_text_checked(text)
    except Exception as exc:
        print(f"警告：播报失败或未确认播完，将继续导览：{exc}")
        return False


_TTS_NODE = None
_TTS_CONTEXT = None


def _tts_speaker():
    """复用一个 TTS 节点。

    之前每次播报都新建 rclpy context，要重新做一遍 DDS 和服务发现，
    实测从“收到指令”到“开口说话”会多等 3~5 秒，听起来就是反应特别慢。
    """
    global _TTS_NODE, _TTS_CONTEXT
    if _TTS_NODE is None:
        import rclpy
        from learn1_d.speak_test import Speaker

        _TTS_CONTEXT = rclpy.Context()
        rclpy.init(context=_TTS_CONTEXT)
        _TTS_NODE = Speaker("", "audio_guide", interrupt=True, context=_TTS_CONTEXT)
    return _TTS_NODE


def warmup_tts(timeout=3.0):
    """启动时先把 TTS 节点和它的服务发现做完，第一次应答才不会慢半拍。"""
    try:
        return _tts_speaker().cli.wait_for_service(timeout_sec=timeout)
    except Exception as exc:  # noqa: BLE001 - 预热失败不影响导览
        print(f"提示：TTS 预热失败，首次播报可能偏慢：{exc}")
        return False


def _speak_text_checked(text):
    """确认播放结果；空文本表示显式跳过。"""
    from learn1_d.speak_test import clean_text

    # 复用的 TTS 节点是直接赋值 text 的，这里也要过一遍清洗，
    # 避免大模型回答里的表情/Markdown 让平台合成失败。
    text = clean_text(text)
    if not text:
        return True
    node = _tts_speaker()
    node.text = text
    if not node.speak():
        raise RuntimeError("播报未确认完成")
    return True
