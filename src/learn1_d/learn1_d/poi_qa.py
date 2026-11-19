"""点位讲解后的游客问答：平台 ASR 收问题，大模型回答，TTS 播报。

讲解播报结束后开一个短聆听窗口（默认 10 秒）。听到问题就调用 vLLM 作答，
答完继续听下一问；没人说话或游客说结束词就结束，继续前往下一个点位。

回声处理：TTS 播报期间麦克风会收回机器人自己的声音，所以每次开窗前先
drain 掉积压的识别结果，播报后再 drain 一次，并按相似度过滤残余回声。
"""

from __future__ import annotations

import difflib
import re
import time

from learn1_d.qwen import QwenClient

DEFAULTS = {
    "enabled": True,
    "listen_seconds": 10.0,
    "settle_seconds": 0.6,
    "base_url": "http://120.48.75.178:7006/v1",
    "model": "Qwen3.5-122B-A10B",
    "api_key": "EMPTY",
    "timeout": 6.0,
    "max_reply_chars": 80,
    "chitchat": True,
    "no_answer": "这个我暂时答不上来，可以问问现场的老师。",
    "end_words": ["不用了", "没有了", "没有问题", "没了", "不问了", "问完了",
                  "谢谢", "继续", "走吧", "下一个", "出发", "讲解完了", "知道了"],
    "ask_prompt": "大家有什么问题可以问我。",
    "followup_prompt": "还有别的问题吗？",
    "abort_words": ["不听了", "不想听了", "不看了", "别讲了", "结束导览", "结束参观",
                    "带我回去", "回去吧", "回起点", "回原点", "回待命点", "回去待命"],
    "faq": [],
}

_PROMPT = """你是展厅导览机器人，正在「{poi}」点位回答游客的问题。
要求：
1. 只依据下面给出的资料回答；资料里没有的内容直接回答“这个我不太清楚，可以问问现场的老师”，绝对不要编造。
2. 用口语，一到两句话，最多 {limit} 个字，适合直接朗读；不要用列表、编号、括号、表情。
3. 不要提“资料”“根据提供的信息”这类词。
{knowledge}"""

_CHAT_PROMPT = """你是展厅导览机器人，正在「{poi}」点位和游客交流。
要求：
1. 与下面资料有关的问题，只依据资料回答，不要编造资料外的具体信息（数字、姓名、薪资、承诺等）；资料没有就说“这个我不太清楚，可以问问现场的老师”。
2. 打招呼、闲聊或一般常识问题，可以自然友好地回应，一两句话即可。
3. 最多 {limit} 个字，用口语，适合直接朗读；不要用列表、编号、括号、表情。
4. 不要提“资料”“根据提供的信息”这类词。
5. 不聊政治、宗教、医疗、法律、投资等敏感或专业建议，遇到就礼貌带过并把话题引回参观。
{knowledge}"""


def _plain(text):
    """去掉标点和空白，只留文字，用于回声比对。"""
    return re.sub(r"[\s，。！？、,.!?；;：:“”\"'（）()《》~…-]+", "", str(text or ""))


class PoiQA:
    def __init__(self, config):
        cfg = dict(DEFAULTS)
        cfg.update(config or {})
        self.enabled = bool(cfg.get("enabled", True))
        self.listen_seconds = max(0.0, float(cfg.get("listen_seconds", 10.0)))
        self.settle_seconds = max(0.0, float(cfg.get("settle_seconds", 0.6)))
        self.base_url = str(cfg.get("base_url") or "").strip()
        self.model = str(cfg.get("model") or "").strip()
        self.api_key = str(cfg.get("api_key") or "EMPTY")
        self.timeout = max(1.0, float(cfg.get("timeout", 6.0)))
        self.max_reply_chars = max(20, int(cfg.get("max_reply_chars", 80)))
        self.chitchat = bool(cfg.get("chitchat", True))
        self.no_answer = str(cfg.get("no_answer") or DEFAULTS["no_answer"])
        self.end_words = [str(w).strip() for w in cfg.get("end_words") or []
                          if str(w).strip()] or list(DEFAULTS["end_words"])
        self.abort_words = [str(w).strip() for w in cfg.get("abort_words") or []
                            if str(w).strip()] or list(DEFAULTS["abort_words"])
        self.ask_prompt = str(cfg.get("ask_prompt") or "").strip()
        self.followup_prompt = str(cfg.get("followup_prompt") or "").strip()
        self.common_knowledge = self._as_knowledge(cfg.get("common_knowledge"))
        self.faq = [item for item in (cfg.get("faq") or [])
                    if isinstance(item, dict) and str(item.get("answer") or "").strip()]
        self._waiter = None

    @staticmethod
    def _as_knowledge(value):
        """把字符串或数组统一成可拼进提示词的文本。"""
        if isinstance(value, (list, tuple)):
            return "\n".join(f"- {item}" for item in value if str(item).strip())
        return str(value or "").strip()

    # ---- 语音输入 -------------------------------------------------------
    def set_waiter(self, waiter):
        """复用待机阶段的语音输入，避免重复打开麦克风。"""
        self._waiter = waiter

    def _get_waiter(self):
        if self._waiter is None:
            from learn1_d.voice_wait import VoiceWaiter

            self._waiter = VoiceWaiter()
            self._waiter.spin_for(1.5)
        return self._waiter

    # ---- 主流程 ---------------------------------------------------------
    def ask(self, poi_name, script, speak):
        """在点位上开放问答；speak 是播报回调（传 announce 包一层）。"""
        if not self.enabled or not self.base_url or not self.model:
            return False
        # 点位资料 + 通用资料（乐知教育介绍），游客在任何点位都能问公司相关的问题。
        local = self._as_knowledge(script.get("knowledge") or script.get("text") or "")
        blocks = []
        if local:
            blocks.append("【当前点位资料】\n" + local)
        if self.common_knowledge:
            blocks.append("【关于乐知教育的通用资料】\n" + self.common_knowledge)
        knowledge = "\n\n".join(blocks)
        if not knowledge:
            return False

        try:
            waiter = self._get_waiter()
        except Exception as exc:  # noqa: BLE001 - 语音不可用就跳过问答，不影响导览
            print(f"提示：问答语音不可用，跳过：{exc}")
            self.enabled = False
            return False

        # 讲完先主动邀请提问，再开聆听窗口。
        spoken = str(script.get("text") or "")
        if self.ask_prompt:
            try:
                speak(self.ask_prompt)
                spoken = f"{spoken} {self.ask_prompt}"
            except Exception:  # noqa: BLE001 - 播报失败不影响问答
                pass
        # 游客可能在邀请语播放期间就回答了“没有问题”，播完先查一次。
        early = self._check_pending(waiter, spoken)
        if early == "abort":
            return True
        if early == "end":
            return False
        waiter.clear()
        waiter.drain(self.settle_seconds)

        system = (_CHAT_PROMPT if self.chitchat else _PROMPT).format(
            poi=poi_name, knowledge=knowledge, limit=self.max_reply_chars)
        client = QwenClient(
            base_url=self.base_url, model=self.model, api_key=self.api_key,
            timeout=self.timeout, system_prompt=system, history_turns=2)

        print(f"{poi_name} 问答开始：{self.listen_seconds:.0f} 秒内可提问")
        while True:
            heard = waiter.hear(self.listen_seconds)
            if heard is None:
                print("  没有听到提问，继续导览")
                break
            if self._is_abort(heard):
                print(f"  游客要求结束导览：{heard}")
                return True
            if self._is_end(heard):
                print(f"  游客结束提问：{heard}")
                break
            if self._is_echo(heard, spoken):
                print(f"  忽略疑似回声：{heard}")
                continue
            print(f"  提问：{heard}")
            reply = self._local_answer(heard)
            if reply:
                print(f"  本地资料回答：{reply}")
            else:
                try:
                    reply = client.chat(heard)
                except Exception as exc:  # noqa: BLE001 - 模型失败不阻断导览
                    print(f"  问答失败：{exc}")
                    speak(self.no_answer)
                    break
                reply = self._shorten(reply)
                if not reply:
                    speak(self.no_answer)
                    break
                print(f"  回答：{reply}")
            speak(reply)
            spoken = f"{spoken} {reply}"
            # 答完主动追问一句，游客说“没有了”或沉默 10 秒就前往下一点位。
            if self.followup_prompt:
                speak(self.followup_prompt)
                spoken = f"{spoken} {self.followup_prompt}"
            # 抢在播报里说的“没有问题/不听了”不能丢，播完统一判定。
            action = self._check_pending(waiter, spoken)
            if action == "abort":
                return True
            if action == "end":
                break
            # 播报刚结束，先把麦克风里的回声清干净，再听下一问。
            waiter.clear()
            waiter.drain(self.settle_seconds)

        return False

    def _check_pending(self, waiter, spoken):
        """处理播报期间积压的识别结果，只认结束/返程这类控制词。"""
        for text in waiter.take_pending():
            if self._is_abort(text):
                print(f"  播报期间听到结束导览：{text}")
                return "abort"
            if self._is_end(text) and not self._is_echo(text, spoken):
                print(f"  播报期间听到结束提问：{text}")
                return "end"
        return None

    def _local_answer(self, text):
        """命中本地 FAQ 就直接回答，不依赖大模型网络。"""
        plain = _plain(text)
        for item in self.faq:
            answer = str(item.get("answer") or "").strip()
            for kw in item.get("keywords") or []:
                kw = str(kw).strip()
                if kw and (kw in text or _plain(kw) in plain):
                    return self._shorten(answer)
        return ""

    def _is_abort(self, text):
        """游客说“不听了/结束导览/带我回去”时结束整场导览并返程。"""
        return any(word in text for word in self.abort_words)

    def shutdown(self):
        if self._waiter is not None:
            try:
                self._waiter.shutdown()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                pass
            self._waiter = None

    # ---- 文本处理 -------------------------------------------------------
    def _shorten(self, text):
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(text) <= self.max_reply_chars:
            return text
        cut = text[:self.max_reply_chars]
        # 优先在句号处截断，读起来更自然。
        index = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"))
        return cut[:index + 1] if index >= self.max_reply_chars // 2 else cut

    def _is_end(self, text):
        plain = _plain(text)
        if not plain:
            return True
        if len(text.strip()) <= 6 and any(word in text for word in self.end_words):
            return True
        return any(word == plain for word in self.end_words)

    def _is_echo(self, text, spoken):
        plain = _plain(text)
        if len(plain) < 2:
            return True
        said = _plain(spoken)
        if not said:
            return False
        if plain in said or said in plain:
            return True
        return difflib.SequenceMatcher(None, plain, said).ratio() >= 0.6
