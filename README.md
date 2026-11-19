# 导览讲解机器人 · Guided Tour Robot

按预设路线带领游客参观的机器人项目：待机迎宾、语音确认出发、逐站导航和讲解、点位问答，最后返回起点继续待命。

本项目负责“按线路参观并讲解”；访客自主选择目的地的带路服务由 [visitor_wayfinding_robot](https://github.com/cisyam555/visitor_wayfinding_robot) 实现。

## 网页预览

机器人连接状态下的实际网页截图，展示头部相机画面、电池与主机数据。截图是当时状态的记录，不代表当前导航状态。

![循展导览讲解工作台](docs/images/web-dashboard.jpg)

「循展」工作台提供路线与讲解稿浏览、实时相机及设备监测，详见 [Web 工作台说明](web/README.md)。

## 完整流程

```text
读取配置 → 核对地图/点位 → 定位与出发自检 → 原地待机
                                                ↓
                                   有人靠近问候、等待参观指令
                                                ↓
起点 → 欢迎语 → 依次导航到展点 → 摆正/转向 → 讲解 → 点位问答
 ↑                                                   ↓
 └────── 返回起点、摆正、继续待命 ← 最后展点/提前结束导览
```

默认地图及路线为 `daolannew`：`POI_01 → POI_02 → POI_03 → POI_04 → POI_05 → POI_06 → POI_01`。默认采用点对点导航，路线顺序不等同于底盘实际行驶轨迹。

| 阶段 | 行为 |
| --- | --- |
| 待机 | 原地静止，有人靠近后问候；默认不摇摆 |
| 启动 | 听到“带我去参观”等关键词后开始 |
| 到点 | 按点位朝向摆正，再按配置转向展品 |
| 讲解 | 播放该点位的讲解词 |
| 问答 | 讲解后默认开放 10 秒窗口，可连续提问，空闲后继续 |
| 提前结束 | 问答窗口内说“不听了 / 结束导览 / 带我回去”，返回起点 |
| 正常结束 | 完成末站与结束话术，返回起点后继续待机 |
| 播报或模型异常 | 告警或降级，按程序继续导览流程 |

**当前语音边界：行走及讲解播报期间不收音。** 结束导览指令只在问答窗口内生效，不能把它当成全程可用的语音急停。

## 技术实现

- ROS 2 Jazzy / `ament_python`，包名 `learn1_d`。
- `map_nav_test.py` 负责流程，`nav_core.py` 通过底盘 REST SDK 管理异步任务、定位、转向和前向防撞。
- 平台 ASR 提供语音识别，平台 TTS 提供讲解播报；本项目不依赖 faster-whisper。
- OpenAI 兼容的 Qwen 接口用于点位问答；点位知识来自 `tour.json`，资料外的问题使用兜底回复。
- 网页独立监测；点击展点仅浏览配置，不下发导航。

## 环境与启动

依赖机器人上的 ROS 2 Jazzy、`/home/nvidia/xos` 平台环境、`/home/nvidia/robot_controller/src` 底盘 SDK、NumPy、已有头部深度相机和语音服务。网页另需 `psutil` 和 OpenCV。地图及 POI 由底盘保存。

```bash
cd /home/nvidia/guide_robot/learn/guided_tour_robot
source /opt/ros/jazzy/setup.bash
source /home/nvidia/xos/setup.bash
colcon build --packages-select learn1_d --symlink-install
source install/setup.bash
ros2 launch learn1_d tour.launch.py
```

默认启动后加载地图、自检并待机，听到参观指令后开始。不要同时运行其他控制底盘的导航项目。

先检查路线：

```bash
# 读取底盘地图与点位校验配置，不加载地图、不运动、不播报
ros2 launch learn1_d tour.launch.py plan:=true
```

## 配置路线、讲解与问答

主要配置文件：`src/learn1_d/learn1_d/tour.json`。

| 配置段 | 用途 |
| --- | --- |
| `route` / `map` | 固定路线标识与底盘地图 |
| `start` / `pois` / `return_poi` | 起点、依次访问的展点和回程终点 |
| `texts` | 欢迎、到达、结束及让行话术 |
| `voice` | 待机问候、启动关键词、靠近距离、等待时间 |
| `poi_scripts` | 各点位的转向角度、讲解词和问答资料 |
| `qa` | 模型接口、问答窗口、兜底回复和通用知识 |

示例：

```json
{
  "POI_02": {
    "turn_deg": -90.0,
    "text": "这里是本次参观的第一个展点。",
    "knowledge": ["该展点的真实介绍资料"]
  }
}
```

上面内容对应 `poi_scripts` 内的一个点位。`turn_deg` 正值左转、负值右转；讲解和问答资料应与现场一致。修改配置后重新构建，或通过 `config:=/绝对路径/tour.json` 指定配置文件。

## 常用启动参数

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `config` | 包内配置 | 导览 JSON 路径 |
| `route` | 配置值 | 自定义路线时传 `custom` |
| `speed` | `0.3` | 导航限速，米/秒 |
| `timeout` | `180.0` | 单段导航超时，秒 |
| `reloc_poi` | `POI_01` | 重定位参考点 |
| `plan` | `false` | 只读路线校验 |
| `speak` | `true` | 播报开关 |
| `wait_voice` | `true` | 等语音指令后出发 |
| `guard` | `true` | 前向防撞 |
| `align` | `true` | 到点摆正 |
| `standby_only` | `false` | 只测试待机，收到启动指令后退出 |
| `sway_deg` | `0.0` | 待机腰部摇摆角度，0 关闭 |

```bash
# 指定另一份完整配置
ros2 launch learn1_d tour.launch.py config:=/path/to/tour.json

# 调低速度
ros2 launch learn1_d tour.launch.py speed:=0.15

# 只测试待机，不执行路线
ros2 launch learn1_d tour.launch.py standby_only:=true
```

固定路线与命令行点位冲突时会拒绝运行。改用命令行点位须传 `route:=custom`，再指定 `map`、`start`、`pois`、`return_poi`。

## 网页启动

```bash
cd /home/nvidia/guide_robot/learn/guided_tour_robot
bash web/start_monitor.sh --port 8767
```

浏览器访问 `http://<机器人IP>:8767/`。页面只读，不启动导览或播报。CPU、温度、电量及相机来自监测服务；章节选择来自配置，**不是实机导览进度**。定位、当前运行地图及任务进度尚未接入。

## 代码入口

| 文件 | 职责 |
| --- | --- |
| `src/learn1_d/learn1_d/map_nav_test.py` | 导览流程与命令行入口 |
| `src/learn1_d/learn1_d/nav_core.py` | 导航、定位、转向、防撞、自检 |
| `src/learn1_d/learn1_d/tour_config.py` | 参数与配置校验 |
| `src/learn1_d/learn1_d/tour.json` | 路线、话术、知识资料 |
| `src/learn1_d/learn1_d/voice_wait.py` | 待机和问答收音 |
| `src/learn1_d/learn1_d/poi_qa.py` | 点位问答、回声过滤、结束意图 |
| `src/learn1_d/launch/tour.launch.py` | 一键启动 |
| `web/` | 循展工作台及测试 |

## 异常处理与边界

- 出发前检查电量、定位置信度、深度图；默认阈值为电量不少于 20%、定位置信度不少于 0.5、5 秒内有深度图。
- 前方被挡时取消任务并等待让行，道路清空后续行；深度断流会停车中止。
- 单点失败可跳过，连续两点导航失败或安全功能失效会中止；转向或讲解失败按步骤降级。
- TTS 失败只告警，模型不可用使用兜底回复；问答依赖网络。
- 中止后先停车并尝试提示工作人员；返回起点也可能因定位、障碍或底盘异常失败，不能保证所有异常都能自动回程。
- 前向相机不覆盖所有方向，软件保护不替代硬件急停与现场人员看护。

网页离线校验：

```bash
cd web
python3 -m unittest test_monitor -v
node --check app.js
```

测试不会启动机器人；实机运动、定位与语音效果需另行验证。
