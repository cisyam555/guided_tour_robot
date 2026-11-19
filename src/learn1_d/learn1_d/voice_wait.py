"""语音输入：本地麦克风 + faster-whisper 连续识别。

平台自带 ASR 需要唤醒态/会话窗口，实测导览待机时收不到 /audio_asr/iat，
表现为“喊了没反应”。这里改为订阅 /lyre/audio_stream，本地 VAD 断句后
交给 whisper 识别；TTS 播报期间静音，避免机器人听见自己的声音。
"""

from __future__ import annotations

import time

import rclpy
from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor

from learn1_d.mic_asr import MicAsr

# 当前活动的语音输入实例；TTS 播报前通过 set_muted() 静音，播完再恢复。
_ACTIVE = None

DEFAULT_HOTWORDS = (
    "带我去参观,给我讲解,学思屋,教师简介墙,薪资英雄榜,创想谷,乐知教育,"
    "张朋娟,王奇,范腾飞,桑金超,李诗莹"
)
DEFAULT_PROMPT = "以下是普通话导览对话，可能涉及参观、讲解、老师和乐知教育。"


def set_muted(muted: bool) -> None:
    """静音/恢复当前语音输入；没有语音实例时为空操作。"""
    waiter = _ACTIVE
    if waiter is not None:
        waiter.mute(muted)


class VoiceWaiter:
    def __init__(self, hotwords: str = "", initial_prompt: str = ""):
        global _ACTIVE

        self._context = rclpy.Context()
        rclpy.init(context=self._context)
        self._node = rclpy.create_node("tour_voice_wait", context=self._context)
        self._executor = SingleThreadedExecutor(context=self._context)
        self._executor.add_node(self._node)
        self._heard = ""
        self._pending = []
        self._closed = False

        self._asr = MicAsr(
            self._node,
            self._on_text,
            hotwords=hotwords or DEFAULT_HOTWORDS,
            initial_prompt=initial_prompt or DEFAULT_PROMPT,
        )
        self._asr.start()
        _ACTIVE = self

    def _on_text(self, text: str) -> None:
        self._heard = text
        # 播报期间收到的识别结果不会立刻处理，但可能是游客抢话说的
        # “没有问题/不听了”，先留最近几条，播完后再判定。
        self._pending.append(text)
        del self._pending[:-5]

    def spin_for(self, seconds: float) -> None:
        """空转一小段时间，让音频开关服务等回调有机会执行。"""
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            self._spin_once()

    def _spin_once(self, timeout_sec: float = 0.1) -> None:
        try:
            self._executor.spin_once(timeout_sec=timeout_sec)
        except ExternalShutdownException:
            raise KeyboardInterrupt from None

    def mute(self, muted: bool) -> None:
        self._asr.hold(muted)

    def clear(self) -> None:
        """丢掉已经收到但还没判定的文本。"""
        self._heard = ""
        self._pending = []

    def take_pending(self):
        """取走播报期间积压的识别结果，用于检查游客是否在抢话。"""
        texts = list(self._pending)
        self._pending = []
        return texts

    def drain(self, quiet_s=0.6, max_s=3.0) -> None:
        """播报结束后清掉残留音频，再恢复识别。"""
        self.mute(False)
        self.spin_for(min(max(quiet_s, 0.2), max_s))
        self._heard = ""

    def hear(self, timeout_s=0.0):
        """听一句普通说话，不筛关键词；超时返回 None（0 表示一直等）。"""
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        while True:
            self._spin_once()
            if self._heard:
                text, self._heard = self._heard, ""
                return text
            if deadline is not None and time.monotonic() >= deadline:
                return None

    def wait(self, words, timeout_s=0.0):
        """阻塞等待；命中 words 中任一关键词返回该句，超时返回 None（0 表示一直等）。"""
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        next_warn = time.monotonic() + 15.0
        while True:
            self._spin_once()
            if self._heard:
                text, self._heard = self._heard, ""
                if any(word in text for word in words):
                    return text
                print(f"  “{text}”不是启动指令，继续待机")
            now = time.monotonic()
            if now >= next_warn:
                next_warn = now + 30.0
                if not self._asr.ready():
                    print("  警告：语音识别模型还没加载好，请稍等")
            if deadline is not None and now >= deadline:
                return None

    def shutdown(self) -> None:
        global _ACTIVE
        if self._closed:
            return
        self._closed = True
        if _ACTIVE is self:
            _ACTIVE = None
        try:
            self._asr.stop()
        except Exception:  # noqa: BLE001 - 关闭失败不影响退出
            pass
        try:
            self._executor.shutdown(timeout_sec=2.0)
            self._node.destroy_node()
            rclpy.shutdown(context=self._context)
        except Exception:  # noqa: BLE001 - context 已被 SIGINT 关掉时不再重复关闭
            pass
