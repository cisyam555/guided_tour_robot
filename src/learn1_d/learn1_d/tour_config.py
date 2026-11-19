"""导览的可调参数与固定配置：轮询常量（本文件）、路线与文案（tour.json）。

改点位、改讲解、换一条路线都只动 tour.json，不用改代码；
--config 指向另一份 json 就能跑另一条路线。
"""

import json
import os

POLL_S = 0.5
TERMINAL_FAIL = {"failed", "failure", "error", "cancelled", "canceled", "stopped", "aborted", "rejected"}
# 这些定位状态值得主动下发一次重定位
RELOCALIZE_TRIGGER = {1, 2, 3, 5}
# 原地转向：差小于该角度就不再补转（约 6°，底盘单次转身本身的误差就有几度）
ALIGN_TOLERANCE_RAD = 0.10
# 底盘单次转身落点有误差，允许补转几次；每次转完等姿态刷新再判断
ALIGN_MAX_ATTEMPTS = 3
# 停车后定位姿态会延迟刷新，最多等这么久让它追上目标，再决定要不要补转
ALIGN_SETTLE_S = 4.0
# 仅在上次重定位任务明确失败后允许有限重试；已受理或状态不明时不补发
RECOVER_MAX_ATTEMPTS = 3
RECOVER_RETRY_S = 10.0
# 底盘在停稳/换任务的过渡期会瞬间拒绝新任务：failed 且距离、耗时都是 0。
# 这种任务实际没起步，等一拍原样重发即可（与 learn_e2c 的处理一致）。
REDISPATCH_SETTLE_S = 1.0
REDISPATCH_MAX_ATTEMPTS = 3
# 前向防撞：起步 1.5s 内先让底盘稳定；连续 3 帧确认才算有人（按 GUARD_POLL_S 采样）；
# 让行后前方连续 1s 安全才恢复；同一段最多让行 3 次，单次最多等 60s。
GUARD_GRACE_S = 1.5
GUARD_CONFIRM = 3
GUARD_CLEAR_S = 1.0
GUARD_MAX_YIELDS = 3
GUARD_WAIT_S = 60.0
# 原地转向时正前方就是要看的展品/墙，用更近的阈值，否则一转身就判成被挡
TURN_GUARD_DISTANCE = 0.3
# 深度图连续断流超过该时长就认为防撞已失效，取消当前动作停车（宁可停下也不盲走）
GUARD_STALE_ABORT_S = 5.0
# 强制重定位后，最终位姿与预期相差超过该范围就拒绝导航（防止带错位姿乱走）
RELOC_TOL_M = 1.5
RELOC_TOL_RAD = 0.52
# 防撞采样要快：0.5 m 阈值下按 0.15 s 采样，确认 3 帧只前进约 0.13 m
GUARD_POLL_S = 0.15
# 导航任务被受理后，剩余距离/预计时间长时间不变说明底盘根本没起步（常见于目标在身后）
NO_PROGRESS_S = 15.0
# 出发前自检：电量、定位置信度低于阈值就不出车；深度图这么久没有一帧就认为防撞不可用
MIN_BATTERY = 20.0
MIN_LOCALIZATION_CONF = 0.5
PREFLIGHT_GUARD_S = 5.0
# 连续这么多个点位都没到达就中止导览（每个点都要等重发/超时，不设上限会空跑很久）
MAX_CONSECUTIVE_FAILS = 2

# 路线与文案在 tour.json 里：点位增删、讲解内容、让行/回家话术都不用改代码。
DEFAULT_TOUR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tour.json")

_TOUR_FIELDS = ("route", "map", "start", "pois", "return_poi", "texts", "poi_scripts")
_TEXT_FIELDS = ("greet", "arrival", "farewell", "yield", "resume",
                "return_yield", "return_resume")


def load_tour(path=None):
    """读取导览配置；缺字段直接报错，避免静默用了错话术或错路线。"""
    full = os.path.abspath(path or DEFAULT_TOUR_PATH)
    try:
        with open(full, encoding="utf-8") as fp:
            tour = json.load(fp)
    except OSError as exc:
        raise ValueError(f"读不到导览配置 {full}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"导览配置 {full} 不是合法 JSON：{exc}") from exc
    if not isinstance(tour, dict):
        raise ValueError(f"导览配置 {full} 顶层必须是对象")
    missing = [key for key in _TOUR_FIELDS if key not in tour]
    if missing:
        raise ValueError(f"导览配置 {full} 缺少字段：{missing}")
    texts = tour["texts"]
    if not isinstance(texts, dict):
        raise ValueError(f"导览配置 {full} 的 texts 必须是对象")
    missing = [key for key in _TEXT_FIELDS if key not in texts]
    if missing:
        raise ValueError(f"导览配置 {full} 的 texts 缺少：{missing}")
    poi_list = tour["pois"]
    if isinstance(poi_list, str):
        poi_list = [p.strip() for p in poi_list.split(",") if p.strip()]
    if not isinstance(poi_list, list) or not all(isinstance(p, str) for p in poi_list):
        raise ValueError(f"导览配置 {full} 的 pois 必须是点位名数组")
    tour["pois"] = ",".join(poi_list)
    tour["poi_scripts"] = tour["poi_scripts"] or {}
    tour["_path"] = full
    return tour


TOUR = load_tour()


# 语音待机的默认值：配置里没写 voice 段也能用，不强制所有路线都配。
_VOICE_DEFAULTS = {
    "standby_text": "你好，需要参观请对我说：带我去参观",
    "welcome_text": "您好，我是导览机器人，可以带您参观并讲解各个展点。想参观的话，对我说“带我去参观”就可以出发。",
    "ready_text": "好的，请跟我来，我们出发吧",
    "done_text": "已经回到起点了，谢谢大家。我在这里待命，需要参观随时叫我。",
    "wake_words": ["带我去参观", "给我讲解", "讲解", "参观", "导览", "出发"],
    "presence_distance": 0.8,
    "listen_timeout": 30.0,
}


def voice_config(tour):
    """取语音待机的文案和唤醒词；tour.json 里的 voice 段可覆盖默认值。"""
    cfg = dict(_VOICE_DEFAULTS)
    cfg.update(tour.get("voice") or {})
    words = cfg.get("wake_words")
    if isinstance(words, str):
        words = [w.strip() for w in words.split(",") if w.strip()]
    if not isinstance(words, list) or not [w for w in words if str(w).strip()]:
        raise ValueError(f"导览配置 {tour.get('_path')} 的 voice.wake_words 必须是非空数组")
    cfg["wake_words"] = [str(w).strip() for w in words if str(w).strip()]
    for key in ("presence_distance", "listen_timeout"):
        try:
            cfg[key] = float(cfg[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"导览配置 {tour.get('_path')} 的 voice.{key} 必须是数值") from exc
    return cfg


def apply_route(args, tour=None):
    """把配置里的固定路线套用到命令行参数；显式传了不同值就报错。"""
    tour = tour or TOUR
    wanted = args.route or tour["route"]
    if wanted == "custom":
        args.route = wanted
        return
    if wanted != tour["route"]:
        raise ValueError(
            f"未知路线 {wanted}；配置 {tour['_path']} 只定义了 {tour['route']}，"
            "自定义路线请用 --route custom 或改用 --config 指向别的配置")
    args.route = wanted
    for key in ("map", "start", "pois", "return_poi"):
        supplied = getattr(args, key)
        if supplied and supplied != tour[key]:
            raise ValueError(
                f"路线 {wanted} 的 {key} 必须是 {tour[key]}；自定义路线请省略 --route")
        setattr(args, key, tour[key])
