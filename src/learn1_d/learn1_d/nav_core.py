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
    """动作被判 failed 且从未起步（距离与耗时都为 0）时返回 True。"""
    if not isinstance(status, dict):
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
        progress_sig = None
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
                # 导航任务被受理却完全不动：剩余距离/预计时间一直不变，说明底盘没起步
                if status.get("type") == "move_to":
                    sig = (status.get("distance_time"), status.get("distance_remaining"))
                    if sig != progress_sig:
                        progress_sig = sig
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


def wait_localization(chassis, timeout_s, pose):
    """等定位就绪；卡在等待重定位时用 pose 主动重定位，失败会重试。

    底盘偶发返回 5001 Internal error，但响应里 accepted=true 且带 action_id，
    说明任务可能已受理；这里不直接退出，继续轮询状态，隔一段时间再补发。
    """
    deadline = time.monotonic() + timeout_s
    attempts = 0
    next_recover = 0.0
    last = None
    while time.monotonic() < deadline:
        rv = chassis.get_localization_status() or {}
        status = rv.get("slam_status")
        if status != last:
            print(f"  定位状态：slam_status={status}（0=正常 5=等待重定位）")
            last = status
        if status == 0:
            return True
        now = time.monotonic()
        if (status in RELOCALIZE_TRIGGER and pose is not None
                and attempts < RECOVER_MAX_ATTEMPTS and now >= next_recover):
            x, y, yaw = pose
            print(f"  下发重定位（第 {attempts + 1} 次）：x={x}, y={y}, yaw={yaw}")
            try:
                chassis.recover_localization(x=x, y=y, yaw=yaw)
            except Exception as exc:  # noqa: BLE001 - 底盘 5001 不代表没提交
                print(f"  重定位接口返回异常：{exc}")
                print("  先继续等待定位状态，必要时再补发")
            attempts += 1
            next_recover = now + RECOVER_RETRY_S
        time.sleep(POLL_S)
    return False


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
    chassis.recover_localization(x=x, y=y, yaw=yaw)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rv = chassis.get_localization_status() or {}
        if rv.get("slam_status") == 0:
            break
        time.sleep(POLL_S)
    else:
        raise RuntimeError(f"强制重定位超时（{timeout_s:.0f}s 内定位未就绪）")

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
    # 只有“偏离声明点位”且“相对原姿态也大幅跳变”才认定匹配失败；
    # 车本就不在声明点位附近时，匹配停在原位属于正常收敛。
    if dist > RELOC_TOL_M and moved > RELOC_TOL_M:
        raise RuntimeError(
            f"重定位结果跳变异常：与给定点位相差 {dist:.2f} m，"
            f"与重定位前位姿相差 {moved:.2f} m"
            f"（给定 x={x:.2f} y={y:.2f}，实际 x={px:.2f} y={py:.2f}）；"
            "已拒绝导航。可能这张地图与现场不符，"
            "请确认后重试，或用 --pose 给出准确位姿")
    if dist > RELOC_TOL_M:
        print(f"提示：与给定点位相差 {dist:.2f} m，但与重定位前位姿只差 {moved:.2f} m，"
              "按当前实际位置继续")
    if dyaw > RELOC_TOL_RAD:
        # 朝向差只说明车摆的方向不同，后面 align_to_yaw 会转正
        print(f"提示：车头朝向与预期相差 {math.degrees(dyaw):.1f}°，"
              "位置可用，继续（后续会按点位朝向转向）")
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
        # 当前地图不用重载；定位状态交给调用方（导航前会再检查一次），
        # 这样 --list 只读操作不会被 60s 的等待卡住。
        print(f"当前地图已经是 {short}，无需重复加载"
              "（如要按指定位姿重新匹配，加 --reloc）")
        return target, pose

    print(f"加载地图：{target}（当前：{full or '无'}）")
    # start_localization 只接受地图名，不带路径和扩展名，统一传短名。
    chassis.start_localization(slam_type=1, map_name=target)

    if not wait_localization(chassis, timeout_s, pose):
        raise RuntimeError(
            "地图加载后定位未就绪；请把车摆到已知点，用 --reloc-poi 点位名 "
            "或 --pose x,y,yaw 指定初始位姿")
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
    if abs(normalize_angle(bearing - pyaw)) <= math.pi / 2:
        return
    print(f"{poi_name} 在车后方，先原地掉头")
    align_to_yaw(chassis, bearing, turn_w, f"{poi_name} 掉头",
                 guard, on_blocked, on_resume)


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
    if turn_w is not None:
        _face_target(chassis, poi_name, poi, turn_w, guard, on_blocked, on_resume)
    while True:
        if track_id is None:
            print(f"点对点前往 {poi_name}")
            rv = chassis.move_to_poi(poi_name=poi_name)
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
            retries += 1
            if retries >= REDISPATCH_MAX_ATTEMPTS or not never_started(exc.status):
                raise
            print(f"{poi_name} 被判 failed 且从未起步，"
                  f"{REDISPATCH_SETTLE_S:.1f}s 后重发（第 {retries + 1} 次）")
            time.sleep(REDISPATCH_SETTLE_S)
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
    text = (text or "").strip()
    if not text:
        return True
    node = _tts_speaker()
    node.text = text
    if not node.speak():
        raise RuntimeError("播报未确认完成")
    return True
