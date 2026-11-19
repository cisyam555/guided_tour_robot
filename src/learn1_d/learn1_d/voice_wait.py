"""语音待机：导览启动后不运动，等到唤醒词或识别文本命中才开始。

直接复用机器人自带的语音识别（讯飞 AIUI 那套）结果，不自己接麦克风：
    /audio_asr/keyword  lyre_msgs/AsrKeyword  {keyword, angle}  唤醒词
    /audio_asr/iat      lyre_msgs/AsrIat      {id, text}        识别文本
两路都看：唤醒词由设备端配置决定，命中整句识别文本更可靠。

实测：语音节点启动后 ASR 是关着的（asr open status:0），
必须持续发布 /intelligent_interaction/lyre/enable=true 才会打开识别，
否则一直收不到任何文本；退出时再发一次 false 还原现场。
"""

import time

import rclpy
from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
from std_msgs.msg import Bool

KEYWORD_TOPIC = "/audio_asr/keyword"
IAT_TOPIC = "/audio_asr/iat"
ENABLE_TOPIC = "/intelligent_interaction/lyre/enable"


class VoiceWaiter:
    def __init__(self):
        from lyre_msgs.msg import AsrIat, AsrKeyword

        self._context = rclpy.Context()
        rclpy.init(context=self._context)
        self._node = rclpy.create_node("tour_voice_wait", context=self._context)
        self._node.create_subscription(AsrKeyword, KEYWORD_TOPIC, self._on_keyword, 10)
        self._node.create_subscription(AsrIat, IAT_TOPIC, self._on_iat, 10)
        self._executor = SingleThreadedExecutor(context=self._context)
        self._executor.add_node(self._node)
        self._enable_pub = self._node.create_publisher(Bool, ENABLE_TOPIC, 10)
        self._enable_msg = Bool()
        self._enable_msg.data = True
        self._heard = ""

    def _on_keyword(self, msg):
        text = str(getattr(msg, "keyword", "") or "").strip()
        if text:
            print(f"  语音唤醒词：{text}（声源角度 {getattr(msg, 'angle', 0)}°）")
            self._heard = text

    def _on_iat(self, msg):
        text = str(getattr(msg, "text", "") or "").strip()
        if text:
            print(f"  语音识别：{text}")
            self._heard = text

    def clear(self):
        """丢掉已经收到但还没判定的文本，避免播报回声被当成指令。"""
        self._heard = ""

    def drain(self, quiet_s=0.6, max_s=3.0):
        """把播报期间堆积的识别结果丢掉，直到连续 quiet_s 秒没有新消息。"""
        self._heard = ""
        started = time.monotonic()
        quiet_until = started + quiet_s
        while time.monotonic() < quiet_until and time.monotonic() - started < max_s:
            try:
                self._executor.spin_once(timeout_sec=0.1)
            except ExternalShutdownException:
                raise KeyboardInterrupt from None
            if self._heard:
                print(f"  忽略回声：{self._heard}")
                self._heard = ""
                quiet_until = time.monotonic() + quiet_s

    def hear(self, timeout_s=0.0):
        """听一句普通说话，不筛关键词；超时返回 None（0 表示一直等）。"""
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        next_enable = 0.0
        while True:
            try:
                self._executor.spin_once(timeout_sec=0.2)
            except ExternalShutdownException:
                raise KeyboardInterrupt from None
            now = time.monotonic()
            if now >= next_enable:
                self._enable_pub.publish(self._enable_msg)
                next_enable = now + 0.5
            if self._heard:
                text, self._heard = self._heard, ""
                return text
            if deadline is not None and now >= deadline:
                return None

    def wait(self, words, timeout_s=0.0):
        """阻塞等待；命中 words 中任一关键词返回该句，超时返回 None（0 表示一直等）。"""
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        next_warn = time.monotonic() + 10.0
        next_enable = 0.0
        while True:
            try:
                self._executor.spin_once(timeout_sec=0.2)
            except ExternalShutdownException:
                # Ctrl+C 时 ROS 先关了 context，当成用户中断，走正常收尾流程。
                raise KeyboardInterrupt from None
            now = time.monotonic()
            # 语音识别默认是关的，要持续使能；停发就会被关回去。
            if now >= next_enable:
                self._enable_pub.publish(self._enable_msg)
                next_enable = now + 0.5
            if self._heard:
                text, self._heard = self._heard, ""
                if any(word in text for word in words):
                    return text
                print(f"  “{text}”不是启动指令，继续待机")
            if now >= next_warn:
                next_warn = now + 30.0
                if self._node.count_publishers(KEYWORD_TOPIC) == 0:
                    print("  警告：没有节点发布语音识别结果，会一直待机")
            if deadline is not None and now >= deadline:
                return None

    def shutdown(self):
        try:
            off = Bool()
            off.data = False
            self._enable_pub.publish(off)
            self._executor.spin_once(timeout_sec=0.1)
        except Exception:  # noqa: BLE001 - 关闭失败不影响退出
            pass
        try:
            self._executor.shutdown(timeout_sec=2.0)
            self._node.destroy_node()
            rclpy.shutdown(context=self._context)
        except Exception:  # noqa: BLE001 - context 已被 SIGINT 关掉时不再重复关闭
            pass
