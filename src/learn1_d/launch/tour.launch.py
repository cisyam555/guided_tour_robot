"""一键启动导览：加载地图、重定位、按点位讲解并返回起点。

    ros2 launch learn1_d tour.launch.py
    ros2 launch learn1_d tour.launch.py plan:=true            # 只检查路线，不运动
    ros2 launch learn1_d tour.launch.py speed:=0.15
    ros2 launch learn1_d tour.launch.py guard:=false speak:=false
    ros2 launch learn1_d tour.launch.py wait_voice:=false     # 不等语音，直接开始

默认启动后原地待机，观众说“带我去参观”之类的指令才开始导览；
待机时的关键词和引导语在 tour.json 的 voice 段里改。

route 留空时用 --config 指向的 tour.json 里的固定路线（默认包内 tour.json），
此时再改 map/start/pois/return_poi 会报错；要走自定义点位请传 route:=custom。
换一条路线就复制一份 json 改内容，再用 config:=/path/to/other.json 启动。
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(text):
    return str(text).strip().lower() in _TRUTHY


def _arguments(context):
    cfg = context.launch_configurations
    argv = []
    if cfg["config"].strip():
        argv += ["--config", cfg["config"]]
    if cfg["route"].strip():
        argv += ["--route", cfg["route"]]
    # 固定路线由配置决定；这些参数只在 route=custom 或留空时才有意义，
    # 两者冲突时由 map_nav_test 报出具体字段，避免两处各写一套规则。
    for key, flag in (("map", "--map"), ("start", "--start"),
                      ("pois", "--pois"), ("return_poi", "--return-poi")):
        if cfg[key].strip():
            argv += [flag, cfg[key]]

    argv += ["--speed", cfg["speed"], "--turn-w", cfg["turn_w"],
             "--timeout", cfg["timeout"],
             "--guard-distance", cfg["guard_distance"],
             "--turn-distance", cfg["turn_distance"]]
    if cfg["reloc_poi"].strip():
        argv += ["--reloc-poi", cfg["reloc_poi"]]
    if _flag(cfg["reloc"]):
        argv.append("--reloc")
    if _flag(cfg["plan"]):
        argv.append("--plan")
    if not _flag(cfg["guard"]):
        argv.append("--no-guard")
    if not _flag(cfg["speak"]):
        argv.append("--no-speak")
    if not _flag(cfg["align"]):
        argv.append("--no-align")
    if not _flag(cfg["wait_voice"]):
        argv.append("--no-wait-voice")
    if cfg["wake_words"].strip():
        argv += ["--wake-words", cfg["wake_words"]]
    argv += ["--wait-timeout", cfg["wait_timeout"]]
    if cfg["standby_text"].strip():
        argv += ["--standby-text", cfg["standby_text"]]
    argv += ["--greet-distance", cfg["greet_distance"],
             "--listen-timeout", cfg["listen_timeout"],
             "--sway-deg", cfg["sway_deg"]]
    if _flag(cfg["standby_only"]):
        argv.append("--standby-only")
    # 留空则用配置里的话术，避免这里覆盖 tour.json。
    if cfg["return_yield_text"].strip():
        argv += ["--return-yield-text", cfg["return_yield_text"]]
    if cfg["return_resume_text"].strip():
        argv += ["--return-resume-text", cfg["return_resume_text"]]

    print(f"[tour.launch] map_nav_test {' '.join(argv)}")
    return [Node(package="learn1_d", executable="map_nav_test",
                 arguments=argv, output="screen", emulate_tty=True)]


def generate_launch_description():
    args = [
        DeclareLaunchArgument("config", default_value="",
                             description="导览配置 json；留空用包内 tour.json"),
        DeclareLaunchArgument("route", default_value="",
                             description="留空=按配置的固定路线，custom=用下面的点位参数"),
        DeclareLaunchArgument("map", default_value="",
                             description="自定义路线时加载的地图名，例如 daolannew"),
        DeclareLaunchArgument("start", default_value="",
                             description="自定义路线时的起点点位名"),
        DeclareLaunchArgument("pois", default_value="",
                             description="自定义路线时依次访问的点位，逗号分隔"),
        DeclareLaunchArgument("return_poi", default_value="",
                             description="自定义路线时返回的点位，留空不返回"),
        DeclareLaunchArgument("reloc_poi", default_value="POI_01",
                             description="重定位用的点位名；清空则用 --start / 当前地图"),
        DeclareLaunchArgument("reloc", default_value="false",
                             description="true=强制重新匹配定位"),
        DeclareLaunchArgument("plan", default_value="false",
                             description="true=只校验路线，不加载地图、不运动"),
        DeclareLaunchArgument("speed", default_value="0.3", description="限速 m/s"),
        DeclareLaunchArgument("turn_w", default_value="0.3", description="原地转向角速度 rad/s"),
        DeclareLaunchArgument("timeout", default_value="180.0", description="单段导航超时（秒）"),
        DeclareLaunchArgument("guard", default_value="true", description="true=开启前向防撞"),
        DeclareLaunchArgument("guard_distance", default_value="0.5",
                             description="直行停车距离（米）"),
        DeclareLaunchArgument("turn_distance", default_value="0.3",
                             description="原地转向停车距离（米）"),
        DeclareLaunchArgument("speak", default_value="true", description="true=开启语音播报"),
        DeclareLaunchArgument("align", default_value="true",
                             description="true=每个点位到点后按地图朝向摆正车头"),
        DeclareLaunchArgument("wait_voice", default_value="true",
                             description="true=启动后原地待机，等到语音指令再开始导览"),
        DeclareLaunchArgument("wake_words", default_value="",
                             description="启动导览的关键词，逗号分隔；留空用配置"),
        DeclareLaunchArgument("wait_timeout", default_value="0.0",
                             description="待机最长等待秒数，0=一直等"),
        DeclareLaunchArgument("standby_text", default_value="",
                             description="无人靠近时（无深度信息时）的引导语；留空用配置"),
        DeclareLaunchArgument("greet_distance", default_value="0.0",
                             description="有人进入该距离（米）就主动问候；0=用配置"),
        DeclareLaunchArgument("listen_timeout", default_value="0.0",
                             description="问候后等语音指令的秒数；0=用配置"),
        DeclareLaunchArgument("sway_deg", default_value="0.0",
                             description="待机时上半身左右摇摆角度（度）；0=不摇（默认）"),
        DeclareLaunchArgument("standby_only", default_value="false",
                             description="自测待机：收到启动指令就结束，不导航不移动"),
        DeclareLaunchArgument("return_yield_text", default_value="",
                             description="返回途中被挡时的话术；留空用配置"),
        DeclareLaunchArgument("return_resume_text", default_value="",
                             description="返回途中恢复导航的话术；留空用配置"),
    ]
    return LaunchDescription(args + [OpaqueFunction(function=_arguments)])
