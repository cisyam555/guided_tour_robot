#!/usr/bin/env python3
"""learn1_d · 加载底盘已保存的地图，并按 POI 点位 / 已录制轨道导航（导航用 REST，播报用 ROS 服务）。

用法：
    # daolannew 导览：点对点，只读检查 / 正式启动（会运动）
    ros2 run learn1_d map_nav_test --route daolannew --plan
    ros2 run learn1_d map_nav_test --route daolannew
    # 该路线：POI_01 起点 -> POI_02~POI_06 -> POI_01，全程 move_to_poi 点对点。
    # 不使用轨道；需要沿轨道走时手动加 --track 即可。
    ros2 run learn1_d map_nav_test --list
    ros2 run learn1_d map_nav_test --map daolannew --list
    ros2 run learn1_d map_nav_test --map daolannew --pois POI_01,POI_02 --pose 0.47,0.49,0.0
    ros2 run learn1_d map_nav_test --map daolannew --start POI_01 \
        --pois POI_01,POI_02,POI_03 --reloc-poi POI_01
    # 沿底盘上已录制的轨道行驶，依次到各点位（轨道名先用 --list 查）
    ros2 run learn1_d map_nav_test --map daolannew --track track_line_1000 \
        --start POI_01 --pois POI_01,POI_02,POI_03 --reloc-poi POI_01
    # 欢迎语 + 依次到点 + 最后一点告别 + 沿轨道返回起点
    ros2 run learn1_d map_nav_test --map daolannew --track track_line_1001 \
        --start POI_01 --reloc-poi POI_01 \
        --pois POI_02,POI_03,POI_04,POI_05,POI_06 --return-poi POI_01

说明：
    1. 地图文件必须已经保存在底盘上（App 建图后保存的地图名，如 daolannew）；
       本脚本只能“加载/切换”地图，不能上传 .bag / .pcd 文件。
    2. --map 会调用 start_localization 把该地图设为当前地图；省略则沿用当前地图。
       已经是当前地图时不会重复加载，避免打断定位。
    3. 定位状态 1/2/3/5（1=丢失 2=降级 3=异常 5=等待重定位）时自动下发一次
       recover_localization。初始位姿优先级：
         --pose x,y,yaw  >  --reloc-poi 点位坐标  >  --start 点位坐标
       所以车停在某已知点位旁边时，直接 --reloc-poi POI_01 即可，不用抄坐标。
    4. 点位坐标和朝向全部来自底盘，本地不维护。
    5. --track 指定轨道名时，每段用 move_to_locations_poi 沿轨道走；
       不指定则用 move_to_poi 点对点直达。点位必须在轨道上，底盘会校验。
    6. 朝向重定向：
       - --start POI_01：出发前先在起点原地转到该点在地图里的 yaw；
       - 每到达一个点位后，也会原地转到该点位在地图里的 yaw（--no-align 关闭）。
    7. 语音：出发前播报 --greet，每到一个点位播报 --arrival（默认“已到达 {poi}”），
       走到 --pois 最后一个点位后播报 --farewell，再用 --return-poi 返回
       （有 --track 时沿轨道，否则点对点）；--no-speak 可关闭全部播报。
       单个点位的专属讲解写在 tour.json 的 poi_scripts：{turn_deg, text}，到点摆正后再原地
       转向 turn_deg（负值右转），然后播报 text（此时不再播 --arrival）。
       播报失败会告警并继续导航；空话术表示显式跳过。
    8. 导航到点后校验坐标偏差（不超过 0.3 米）；转向后校验朝向。
       任务失败、结果不明、查询异常、超时或中断时会尝试取消当前运动。
       --list 只查询指定地图，不加载地图、不重定位。
    9. 路线、点位、讲解、让行话术都在 tour.json 里，改内容不用改代码：
       --route 用配置里的固定路线（出发时不在起点会先自动导航到起点），
       --route custom 完全按命令行，--config 指向另一份 json 就能跑另一条路线。
       固定路线统一用 move_to_poi 点对点导航，不自动绕行；语音失败继续。
   10. 前向防撞（默认开启）：订阅头部深度图，正前方 --guard-distance（默认 0.5 m）
       内有人就取消导航停车、播报 --yield-text，等人离开后重新下发同一目标；
       同一段最多让行 3 次、单次最多等 60 秒，障碍移开后续行前会播报 --resume-text。
       返回起点途中换成 --return-yield-text / --return-resume-text（“我正在回家”），
       避免观众误以为机器人在带路。
       原地转向（含讲解转身）同样受防撞保护，但用更近的 --turn-distance
       （默认 0.3 m），避免正对展品转身时被自己拦停；深度图连续断流 5 秒会取消任务停车。
       加 --reloc 会强制按 --pose/--reloc-poi 重新匹配定位，并校验最终位姿；
       偏差超过 1.5 米或 30° 直接拒绝导航。
       --no-guard 可关闭。
       底盘自带 AEB 在自主导航下不可靠，这一层是真正兜住人的防线。
   11. 出发前自检：电量低于 20%、定位置信度低于 0.5、或 5 秒内收不到深度图，
       一律不出发并播报原因。开防撞时如果防撞节点起不来同样拦下（确认要跑加 --no-guard）。
   12. 单点失败只降级不中止：某个点位到不了就跳过并播报，摆正/讲解转向失败也
       只跳过那一步；连续 2 个点位都到不了、或安全功能失效（防撞断流）才停止导览。
       任何中止都会先停稳、再播报“请联系工作人员”，退出码：
       0=正常完成，1=中止或自检未通过，2=安全功能失效。
   13. 启动后默认原地待机、不运动：订阅头部深度图，检测到有人进入 --greet-distance
       （默认 0.8 m，连续 3 帧确认）就先主动问候 voice.welcome_text，然后等语音指令。
       语音来自底盘自带识别（/audio_asr/keyword 唤醒词、/audio_asr/iat 识别整句），
       命中 voice.wake_words 或 --wake-words 才开始导览；--listen-timeout 控制每次
       问候后等多久（默认 30 s），超时没听到指令就回到待机继续等，下一位来再问候。
       --wait-timeout 是整体上限（0=一直等）。语音栈不可用会拒绝出发（避免自己乱走），
       调试可加 --no-wait-voice 跳过整个待机环节。
       走完一趟导览会播报 voice.done_text，然后回到待机等下一位游客，不退出。
       待机时默认不晃动；需要时可用 --sway-deg 指定左右摇摆角度（0~30 度）。
       有人进入 greet_distance 就主动问候，等指令时不再摇晃。

代码位置：底盘/地图相关的工具函数在 nav_core.py，路线与话术配置在 tour.json
（由 tour_config.py 读取校验），本文件只负责命令行参数和导览流程编排。
"""

import argparse
import math
import os
import sys
import time

# 优先用环境变量指定 chassis SDK 源码位置。
_ROBOT_CONTROLLER_SRC = os.environ.get(
    "ROBOT_CONTROLLER_SRC", "/home/nvidia/robot_controller/src"
)
if _ROBOT_CONTROLLER_SRC not in sys.path:
    sys.path.insert(0, _ROBOT_CONTROLLER_SRC)

from robot_controller.chassis import KnewbotChassisNav  # noqa: E402

from learn1_d.nav_core import (  # noqa: E402
    SafetyStop,
    align_to_yaw,
    check_route,
    current_map,
    goto_poi,
    load_map,
    map_short_name,
    poi_pose_from_map,
    preflight,
    print_pois,
    print_tracks,
    relocalize,
    speak_text,
    start_guard,
    turn_relative,
    wait_localization,
    warmup_tts,
)
from learn1_d.tour_config import (  # noqa: E402
    MAX_CONSECUTIVE_FAILS,
    TURN_GUARD_DISTANCE,
    apply_route,
    load_tour,
    voice_config,
)

# tour.json 的 texts 键名 -> 本文件的命令行参数名；命令行显式传了就以命令行为准。
_TEXT_ARGS = {
    "greet": "greet",
    "arrival": "arrival",
    "farewell": "farewell",
    "yield": "yield_text",
    "resume": "resume_text",
    "return_yield": "return_yield_text",
    "return_resume": "return_resume_text",
}


def parse_pose(text):
    if not text:
        return None
    parts = text.split(",")
    if len(parts) != 3:
        raise ValueError("--pose 格式应为 x,y,yaw，例如 0.47,0.49,0.0")
    return tuple(float(p) for p in parts)


def announce(args, text):
    """按 --no-speak 开关播报；播报失败不影响流程。"""
    if args.speak and text:
        speak_text(text)


def run_step(label, func):
    """单步动作失败只跳过这一步；安全类失败照旧上抛，由 main 统一收尾。"""
    try:
        func()
    except SafetyStop:
        raise
    except Exception as exc:  # noqa: BLE001 - 一次摆正/转向失败不该终止整场导览
        print(f"警告：{label}失败，继续导览：{exc}")


def wait_for_visitor(args, guard, waiter, voice, words, sway=None):
    """待机：有人进入 greet_distance 就主动问候，再等语音指令；超时回到待机。"""
    greet_m = args.greet_distance or voice["presence_distance"]
    listen_s = args.listen_timeout or voice["listen_timeout"]
    deadline = time.monotonic() + args.wait_timeout if args.wait_timeout > 0 else None

    if guard is None:
        announce(args, args.standby_text or voice["standby_text"])
        print(f"没有深度信息，跳过靠近检测，直接等待语音指令：{words}")
        return waiter.wait(words, args.wait_timeout)

    print(f"待机中：有人进入 {greet_m:.1f} m 就主动问候，然后等待语音指令：{words}")
    show_distance = bool(getattr(args, "standby_only", False))
    next_show = 0.0
    # 问候超时回到待机后，同一位观众还站在原地时不重复打招呼，
    # 等他离开（走出问候距离）再来时才会重新欢迎。
    wait_left = False
    told_wait_left = False
    while True:
        # 每一轮都先摇一个来回，摇完回正再量距离：没人就继续摇，形成持续摆动。
        # 摇晃时头部相机跟着转，读出来的距离不能用，所以只在回正后判断。
        if sway is not None:
            sway.sweep(args.sway_deg)
        # 回正后只量一小段窗口（约 0.6 s），没人就回去继续摇；
        # 否则会一直停在这个循环里，看起来就是“摇一下就再也不动了”。
        hits = 0
        distance = None
        window_end = time.monotonic() + 0.6
        while hits < 3 and time.monotonic() < window_end:
            if deadline is not None and time.monotonic() >= deadline:
                return None
            distance = guard.distance()
            hits = hits + 1 if distance is not None and distance <= greet_m else 0
            now = time.monotonic()
            if show_distance and now >= next_show:
                shown = "无数据" if distance is None else f"{distance:.2f} m"
                print(f"  前方距离：{shown}", flush=True)
                next_show = now + 1.0
            if hits < 3:
                time.sleep(0.15)
        if hits < 3:
            if wait_left:  # 已经走出问候距离，下次进来可以重新问候
                wait_left = False
                told_wait_left = False
                print("观众已离开，下一位靠近时会重新问候")
            continue
        if wait_left:
            if not told_wait_left:
                print("同一位观众还在，等他离开后再欢迎下一位")
                told_wait_left = True
            continue
        print(f"检测到有人靠近（{distance:.2f} m），回正后问候")
        if sway is not None:
            sway.center()
        announce(args, voice["welcome_text"])
        time.sleep(0.5)
        waiter.clear()  # 丢掉自己播报被麦克风拾到的回声
        # 问候后固定等 listen_timeout 秒（默认 30 s）：观众走了也等满这段时间，
        # 期间如果重新靠近或换人说话都能接上；超时就回到待机继续摇。
        print(f"等待语音指令（最多 {listen_s:.0f} 秒，没回应就回到待机）")
        heard = waiter.wait(words, listen_s)
        if heard:
            return heard
        if deadline is not None and time.monotonic() >= deadline:
            return None
        print("没有收到指令，回到待机")
        wait_left = True


def run_tour(args, chassis, guard, pois, targets, track_id, poi_scripts,
             on_blocked, on_resume, on_blocked_return, on_resume_return,
             need_move_to_start, qa=None):
    """走一整趟导览。返回 0=走完，1=回程失败；安全类异常继续上抛交给 main。"""
    # 起点较远时先自己走过去，再播欢迎语、摆正车头、开始导览。
    if need_move_to_start:
        goto_poi(chassis, args.start, pois[args.start], args.timeout, None,
                 guard, on_blocked, on_resume, args.turn_w)

    # 出发前播报欢迎语，再摆正车头、依次出发。
    announce(args, args.greet)

    # 起点重定向：按地图里起始 POI 保存的 yaw 摆正车头，再依次出发。
    if args.start:
        run_step(f"起点 {args.start} 摆正",
                 lambda: align_to_yaw(chassis, pois[args.start]["yaw"], args.turn_w,
                                      f"起点 {args.start}", guard,
                                      on_blocked, on_resume))

    # 单点失败只跳过该点：只有连续多个点都到不了，或安全功能失效才中止。
    skipped = []
    consecutive_fail = 0
    aborted = False
    for poi_name in targets:
        poi = pois[poi_name]
        try:
            goto_poi(chassis, poi_name, poi, args.timeout, track_id,
                     guard, on_blocked, on_resume, args.turn_w)
        except SafetyStop:
            raise
        except Exception as exc:  # noqa: BLE001 - 不因一个点位失败终止整场导览
            consecutive_fail += 1
            skipped.append(poi_name)
            print(f"警告：{poi_name} 未能到达，跳过该点位：{exc}")
            announce(args, f"暂时无法前往 {poi_name}，我们继续参观下一个点位")
            if consecutive_fail >= MAX_CONSECUTIVE_FAILS:
                raise
            continue
        consecutive_fail = 0

        if args.align:
            run_step(f"{poi_name} 到点摆正",
                     lambda: align_to_yaw(chassis, poi["yaw"], args.turn_w,
                                          f"{poi_name}", guard,
                                          on_blocked, on_resume))
        # 到点停车后：配置了专属讲解的点位先转向展品再讲解，
        # 其余点位播报到达提示，然后前往下一个点位。
        script = poi_scripts.get(poi_name) or {}
        turn_deg = float(script.get("turn_deg") or 0.0)
        if turn_deg:
            run_step(f"{poi_name} 讲解转向",
                     lambda: turn_relative(chassis, math.radians(turn_deg),
                                           args.turn_w, f"{poi_name} 讲解",
                                           guard, on_blocked, on_resume))
        text = script.get("text") or ""
        if not text and args.arrival:
            text = args.arrival.replace("{poi}", poi_name)
        announce(args, text)
        # 讲解后的游客问答：问完再转回车头继续走下一个点位。
        if qa is not None and text:
            if qa.ask(poi_name, script, lambda reply: announce(args, reply)):
                aborted = True
                if args.return_poi:
                    announce(args, "好的，我们结束参观，现在带您回到起点待命。")
                else:
                    announce(args, "好的，我们结束参观。")
                break
        # 讲解完转回车头，回到该点位在地图里的朝向再前往下一个点位。
        if turn_deg:
            if args.align:
                run_step(f"{poi_name} 讲解结束摆正",
                         lambda: align_to_yaw(chassis, poi["yaw"], args.turn_w,
                                              f"{poi_name} 讲解结束", guard,
                                              on_blocked, on_resume))
            else:
                run_step(f"{poi_name} 讲解结束回转",
                         lambda: turn_relative(chassis, -math.radians(turn_deg),
                                               args.turn_w, f"{poi_name} 讲解结束",
                                               guard, on_blocked, on_resume))

    # 中途听到“不听了/结束导览/带我回去”就直接返程，不再播结束语。
    tail_script = poi_scripts.get(targets[-1]) or {}
    if not aborted and not tail_script.get("text"):
        announce(args, args.farewell)

    if args.return_poi:
        try:
            goto_poi(chassis, args.return_poi, pois[args.return_poi],
                     args.timeout, track_id, guard,
                     on_blocked_return, on_resume_return, args.turn_w)
            if args.align:
                run_step(f"{args.return_poi} 摆正",
                         lambda: align_to_yaw(chassis, pois[args.return_poi]["yaw"],
                                              args.turn_w, f"{args.return_poi}",
                                              guard, on_blocked_return,
                                              on_resume_return))
        except SafetyStop:
            raise
        except Exception as exc:  # noqa: BLE001 - 回不了家也要先说明再退出
            print(f"警告：返回起点失败：{exc}")
            announce(args, "我暂时回不到起点，先停在这里，请联系工作人员")
            return 1

    if skipped:
        print(f"注意：本次有 {len(skipped)} 个点位未到达：{skipped}")
    print("全部点位导航完成")
    return 0


def main():
    parser = argparse.ArgumentParser(description="加载底盘已保存地图，按 POI 点位或已录制轨道导航")
    parser.add_argument("--config", default="",
                        help="导览配置 json；留空用包内 tour.json，换一条路线就指向另一份")
    parser.add_argument("--route", default="",
                        help="用配置里的固定路线（默认取配置）；custom=完全按命令行")
    parser.add_argument("--plan", action="store_true",
                        help="只读验证路线并显示计划，不加载地图、不重定位、不播报、不运动")
    parser.add_argument("--host", default="192.168.11.10")
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--map", default="", help="已保存的地图名，例如 daolannew；留空用当前地图")
    parser.add_argument("--list", action="store_true", help="只列出当前地图、轨道和点位，不移动")
    parser.add_argument("--pois", default="", help="按顺序导航的点位，逗号分隔，例如 POI_01,POI_02")
    parser.add_argument("--track", default="",
                        help="轨道名（先用 --list 查看）；指定后每段沿该轨道行驶")
    parser.add_argument("--start", default="",
                        help="起点点位名；出发前先按地图里该点的 yaw 原地重定向")
    parser.add_argument("--return-poi", default="",
                        help="走完 --pois 后沿同一条轨道返回的点位，例如 POI_01")
    parser.add_argument("--greet", default=None,
                        help="出发前播报的文本；留空则用配置，传空字符串则不播")
    parser.add_argument("--arrival", default=None,
                        help="到达每个点位后的播报模板，{poi} 会替换成点位名；传空字符串关闭")
    parser.add_argument("--farewell", default=None,
                        help="到达 --pois 最后一个点位后播报的文本；传空字符串则不播")
    parser.add_argument("--no-speak", dest="speak", action="store_false",
                        help="关闭全部语音播报")
    parser.add_argument("--no-qa", dest="qa", action="store_false", default=None,
                        help="关闭点位讲解后的大模型问答；默认用配置里的 qa.enabled")
    parser.add_argument("--no-align", dest="align", action="store_false",
                        help="关闭“每到一个点位后按地图 yaw 重定向”")
    parser.add_argument("--no-wait-voice", dest="wait_voice", action="store_false",
                        help="跳过语音待机，自检通过后直接开始导览（调试用）")
    parser.add_argument("--standby-only", action="store_true",
                        help="自测待机：走到“收到启动指令”就结束，不导航、不移动")
    parser.add_argument("--wake-words", default="",
                        help="启动导览的关键词，逗号分隔；留空用配置里的 voice.wake_words")
    parser.add_argument("--wait-timeout", type=float, default=0.0,
                        help="待机最长等待秒数，0 表示一直等")
    parser.add_argument("--standby-text", default="",
                        help="无人靠近时（无深度信息时）播报的引导语；留空用配置")
    parser.add_argument("--greet-distance", type=float, default=0.0,
                        help="有人进入该距离（米）就主动问候；0 表示用配置里的值")
    parser.add_argument("--listen-timeout", type=float, default=0.0,
                        help="问候后等待语音指令的秒数；0 表示用配置里的值")
    parser.add_argument("--sway-deg", type=float, default=0.0,
                        help="待机时上半身左右摇摆的角度（度）；0 表示不摇晃（默认）")
    parser.add_argument("--pose", default="", help="重定位初始位姿 x,y,yaw")
    parser.add_argument("--reloc-poi", default="",
                        help="重定位用哪个点位的坐标（车停在该点位附近时填它）")
    parser.add_argument("--reloc", action="store_true",
                        help="强制重定位：即使地图已是当前地图、定位状态正常，"
                             "也按 --pose/--reloc-poi 重新匹配并校验结果")
    parser.add_argument("--speed", type=float, default=0.3, help="限速 m/s（0.05~1.5）")
    parser.add_argument("--turn-w", type=float, default=0.3, help="原地转向角速度 rad/s")
    parser.add_argument("--no-guard", dest="guard", action="store_false",
                        help="关闭前向深度相机防撞")
    parser.add_argument("--depth-topic", default="/ob_camera_head/depth/image_raw",
                        help="前向防撞使用的深度图话题")
    parser.add_argument("--guard-distance", type=float, default=0.5,
                        help="正前方该距离（米）内有人就停车等待让行")
    parser.add_argument("--turn-distance", type=float, default=TURN_GUARD_DISTANCE,
                        help=f"原地转向时的停车距离，比直行更近（默认 {TURN_GUARD_DISTANCE}）")
    parser.add_argument("--yield-text", default=None,
                        help="被挡住时播报的话术；留空则用配置，传空字符串则不播")
    parser.add_argument("--resume-text", default=None,
                        help="让行结束、恢复导航前播报的话术；传空字符串则不播")
    parser.add_argument("--return-yield-text", default=None,
                        help="返回起点途中被挡住时播报的话术；传空字符串则不播")
    parser.add_argument("--return-resume-text", default=None,
                        help="返回起点途中让行结束、恢复导航前播报的话术；传空字符串则不播")
    parser.add_argument("--timeout", type=float, default=180.0, help="单段导航超时（秒）")
    # ros2 launch 会在命令行尾部追加 --ros-args；本脚本不用 ROS 参数，丢掉它之后的部分。
    argv = sys.argv[1:]
    if "--ros-args" in argv:
        argv = argv[:argv.index("--ros-args")]
    args = parser.parse_args(argv)
    try:
        tour = load_tour(args.config or None)
        apply_route(args, tour)
    except ValueError as exc:
        parser.error(str(exc))
    # 命令行没给的文案用配置里的，保证话术只有一处维护。
    for key, name in _TEXT_ARGS.items():
        if getattr(args, name) is None:
            setattr(args, name, tour["texts"][key])
    poi_scripts = tour["poi_scripts"]
    if args.qa is None:
        args.qa = bool((tour.get("qa") or {}).get("enabled", True))

    if not (math.isfinite(args.speed) and 0.05 <= args.speed <= 1.5):
        parser.error("--speed 须在 [0.05, 1.5] 米/秒")
    if not (math.isfinite(args.turn_w) and 0 < args.turn_w <= 1.0):
        parser.error("--turn-w 须在 (0, 1] 弧度/秒")
    if not (math.isfinite(args.timeout) and args.timeout > 0):
        parser.error("--timeout 必须为有限正数")
    if not (math.isfinite(args.greet_distance) and args.greet_distance >= 0):
        parser.error("--greet-distance 必须是非负有限数值")
    if not (math.isfinite(args.listen_timeout) and args.listen_timeout >= 0):
        parser.error("--listen-timeout 必须是非负有限数值")
    if not (math.isfinite(args.sway_deg) and 0 <= args.sway_deg <= 30):
        parser.error("--sway-deg 须在 [0, 30] 度")
    if not (math.isfinite(args.guard_distance) and args.guard_distance > 0.1):
        parser.error("--guard-distance 须为大于 0.1 的有限数值")
    if not (math.isfinite(args.turn_distance) and args.turn_distance > 0.1):
        parser.error("--turn-distance 须为大于 0.1 的有限数值")
    targets = [p.strip() for p in args.pois.split(",") if p.strip()]
    if args.pois and not targets:
        parser.error("--pois 不能只有空格或逗号")
    pose = parse_pose(args.pose)
    if pose is not None and not all(math.isfinite(v) for v in pose):
        parser.error("--pose 坐标必须是有限数值")
    # 没给 --pose 时，用点位坐标做重定位初始位姿；未指定 --reloc-poi 时用 --start。
    reloc_poi = args.reloc_poi or args.start
    chassis = KnewbotChassisNav(host=args.host, rest_port=args.port)
    guard = None
    qa = None
    try:
        # 列表模式完全只读：不切换地图、不重定位、不播报、不移动。
        if args.list or not targets:
            print_pois(chassis, map_name=args.map or None)
            print_tracks(chassis, map_name=args.map or None)
            if not args.list:
                print("提示：用 --pois 指定按顺序访问的点位才开始导览")
            return 0

        # 所有导航命令先核对地图、点位和轨道，避免参数错时已经切图/重定位。
        pois, track_id = check_route(chassis, args, targets)
        if args.plan:
            print("路线检查通过；未加载地图、重定位、播报或下发运动")
            return 0

        if args.map:
            _, pose = load_map(chassis, args.map, timeout_s=60.0,
                               pose=pose, reloc_poi=reloc_poi)
        elif pose is None and reloc_poi:
            pose = poi_pose_from_map(chassis, reloc_poi)

        if args.reloc:
            pose = relocalize(chassis, pose, timeout_s=60.0)

        if not wait_localization(chassis, timeout_s=60.0, pose=pose):
            raise RuntimeError(
                "定位未就绪，拒绝导航（可用 --reloc-poi 点位名或 --pose x,y,yaw）")

        health = chassis.get_robot_health()
        print(f"底盘健康：{health}")
        if health.hasFatal or health.hasError:
            if args.standby_only:
                # 待机自测不移动，维护模式等底盘故障不影响语音/问候链路。
                print("警告：底盘有故障，但 --standby-only 不移动，继续待机自测")
            else:
                raise RuntimeError("底盘报告故障，拒绝导航；请先处理维护模式、定位或其他故障")

        if args.map and current_map(chassis)[1] != map_short_name(args.map):
            raise RuntimeError("当前地图与计划不符，拒绝导航")

        # 地图确认后重新读取点位/轨道，避免沿用加载前的旧数据。
        pois, track_id = check_route(chassis, args, targets, verbose=False)

        # 不在起点时不再直接退出：先自动点对点导航到起点，再开始导览。
        need_move_to_start = False
        if args.route != "custom":
            pose_now = chassis.get_pose() or {}
            start = pois[args.start]
            try:
                offset = math.hypot(float(pose_now["x"]) - float(start["x"]),
                                    float(pose_now["y"]) - float(start["y"]))
            except (KeyError, TypeError, ValueError):
                raise RuntimeError("无法读取当前位姿，拒绝导航")
            if not math.isfinite(offset):
                raise RuntimeError("当前位姿非有限数值，拒绝导航")
            need_move_to_start = offset > 0.5
            if need_move_to_start:
                print(f"当前距起点 {args.start} 约 {offset:.1f} m，先自动导航到起点")

        chassis.set_aeb_control("On")
        chassis.set_speed_params(max_moving_speed=args.speed)
        print(f"已开启 AEB，限速 {args.speed} m/s")

        # 前向防撞：深度相机在后台线程订阅，导航轮询里读距离。
        if args.guard:
            try:
                guard = start_guard(args.depth_topic, args.guard_distance,
                                    args.turn_distance)
                print(f"前向防撞已开启：直行 {args.guard_distance:.2f} m、"
                      f"转向 {min(args.turn_distance, args.guard_distance):.2f} m "
                      "内有障碍就停车让行")
            except Exception as exc:  # noqa: BLE001 - 没有相机时退化为只靠底盘避障
                print(f"警告：前向防撞启动失败，只依赖底盘避障：{exc}")

        # 每种行程一套让行/续行话术；以后加新场景（比如回充电位）就在配置里加一对文案。
        def replier(yield_text, resume_text):
            def on_blocked():
                announce(args, yield_text)

            def on_resume():
                announce(args, resume_text)

            return on_blocked, on_resume

        # 返回起点途中用另一套话术，让观众知道机器人是在回家而不是在带路。
        on_blocked, on_resume = replier(args.yield_text, args.resume_text)
        on_blocked_return, on_resume_return = replier(
            args.return_yield_text, args.return_resume_text)

        # 出发前自检：电量、定位质量、深度图出帧，避免带病出车。
        # 待机自测不移动，只关心深度图能不能出帧（用来判断有人靠近），跳过电量/定位。
        if args.standby_only:
            problems = preflight(chassis, guard, min_battery=0.0, min_conf=0.0)
        else:
            problems = preflight(chassis, guard)
        if args.guard and guard is None:
            problems.append("前向防撞没能启动（深度相机不可用）")
        if problems:
            for item in problems:
                print(f"自检不通过：{item}")
            announce(args, "出发前检查没有通过，我先不出发，请联系工作人员")
            return 1
        print("出发前自检通过")

        # 提前建好 TTS 节点并完成服务发现：否则第一句应答要多等 3~5 秒。
        if args.speak:
            warmup_tts()

        # 到点讲解后的游客问答：大模型回答 + TTS 播报；失败不影响导览。
        if args.qa and args.speak:
            try:
                from learn1_d.poi_qa import PoiQA

                qa = PoiQA(tour.get("qa") or {})
                print("点位问答已开启：每点讲解后开放提问")
            except Exception as exc:  # noqa: BLE001 - 问答不可用时照常导览
                print(f"提示：点位问答不可用，跳过：{exc}")
                qa = None

        # 默认待机：不动，人靠近主动问候，听到指令才出发。
        if args.wait_voice:
            voice = voice_config(tour)
            words = [w.strip() for w in args.wake_words.split(",") if w.strip()]
            words = words or voice["wake_words"]
            try:
                from learn1_d.voice_wait import VoiceWaiter

                waiter = VoiceWaiter()
            except Exception as exc:  # noqa: BLE001 - 语音不可用就别贸然出发
                print(f"语音待机不可用：{exc}")
                announce(args, "语音功能没有准备好，请联系工作人员")
                return 1
            # 待机时上身轻微摇晃；有人靠近时先回正，再问候。
            sway = None
            if args.sway_deg > 0:
                try:
                    from learn1_d.standby_sway import WaistSway

                    sway = WaistSway()
                except Exception as exc:  # noqa: BLE001 - 摇晃不可用也照常待机
                    print(f"提示：上身摇晃不可用，跳过：{exc}")
            try:
                while True:
                    # 上一场导览期间的语音消息不要带进新一轮待机。
                    waiter.clear()
                    waiter.drain(0.3)
                    heard = wait_for_visitor(args, guard, waiter, voice, words, sway)
                    if heard is None:
                        print("等待语音指令超时，未开始导览")
                        announce(args, "您可以随时叫我，说“带我去参观”，我就出发。")
                        return 0
                    print(f"收到启动指令：{heard}")
                    announce(args, voice["ready_text"])
                    if args.standby_only:
                        print("待机自测完成：已收到启动指令，按 --standby-only 不移动")
                        return 0

                    result = run_tour(args, chassis, guard, pois, targets, track_id,
                                      poi_scripts, on_blocked, on_resume,
                                      on_blocked_return, on_resume_return,
                                      need_move_to_start, qa)
                    if result != 0:
                        return result
                    # 走完不退出：说一句结束语，然后回到待机等下一位游客。
                    announce(args, voice["done_text"])
                    print("导览结束，回到待机，等待下一位游客")
            finally:
                waiter.shutdown()
                if sway is not None:
                    sway.shutdown()
        else:
            return run_tour(args, chassis, guard, pois, targets, track_id,
                            poi_scripts, on_blocked, on_resume,
                            on_blocked_return, on_resume_return,
                            need_move_to_start, qa)

    except KeyboardInterrupt:
        # Ctrl+C：不播报，直接安静退出（入口是 console_scripts，这里必须自己接）。
        print("\n已中断")
        return 130
    except SafetyStop as exc:
        # 安全功能失效（防撞断流等）：停稳后说明情况，不再继续走。
        print(f"安全功能失效，停止导览：{exc}")
        announce(args, "安全系统出了点问题，我先停在这里，请联系工作人员")
        return 2
    except Exception as exc:  # noqa: BLE001 - 未预期的失败也要先说明再退出
        print(f"导览中止：{exc}")
        announce(args, "遇到问题，我先停在这里，请联系工作人员")
        return 1
    finally:
        if qa is not None:
            try:
                qa.shutdown()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                pass
        if guard is not None:
            try:
                guard.shutdown()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                pass
        chassis.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"失败：{exc}")
        sys.exit(1)
