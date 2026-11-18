#!/usr/bin/env python3
"""调用平台 TTS 播报一段文本。

走 ROS 服务 /intelligent_interaction/tts/play（interaction_msgs/srv/TtsService）：
    1. cmd='append', type='text', audio_id=... 提交播报；
    2. cmd='query' 轮询 status，直到不再是 playing；
    3. Ctrl-C 或失败时 cmd='stop' 打断音频。
"""

import argparse
import time

import rclpy
from interaction_msgs.srv import InstructionService, TtsService
from rclpy.node import Node

TTS_SERVICE = "/intelligent_interaction/tts/play"
VOICE_SERVICE = "/intelligent_interaction/tts/update_voice_id"
PLAYING_STATUS = "playing"
SETTLE_S = 0.4
# 本项目的专属音色：播报前切女声，播完恢复平台默认男声，避免影响别的项目
VOICE_ID = "tiangong_v4"
VOICE_DEFAULT = "tiangong_v2"

# 不传命令行文本时，默认播这句；改这里就能改默认话术。
DEFAULT_TEXT = "你好，我是导览机器人，现在开始参观"


class Speaker(Node):
    def __init__(self, text: str, audio_id: str, interrupt: bool,
                 service: str = TTS_SERVICE, context=None) -> None:
        super().__init__("speak_test", context=context)
        self.text = text.strip()
        self.audio_id = audio_id
        self.interrupt = interrupt
        self.service = service
        self.cli = self.create_client(TtsService, service)
        self.voice_id = VOICE_ID
        self.voice_default = VOICE_DEFAULT
        self._voice_cli = None

    def set_voice(self, voice: str) -> None:
        """切换全局音色；失败只告警，不打断播报。"""
        if not voice:
            return
        if self._voice_cli is None:
            self._voice_cli = self.create_client(InstructionService, VOICE_SERVICE)
        if not self._voice_cli.wait_for_service(timeout_sec=2.0):
            self.get_logger().warn("音色服务未就绪，沿用当前音色")
            return
        req = InstructionService.Request()
        req.cmd = voice
        future = self._voice_cli.call_async(req)
        rclpy.spin_until_future_complete(self, future, timeout_sec=5.0)
        if not future.done():
            self.get_logger().warn(f"切换音色 {voice} 超时")
            future.cancel()
            return
        try:
            if not future.result().success:
                self.get_logger().warn(f"切换音色 {voice} 被拒绝")
        except Exception as exc:  # noqa: BLE001
            self.get_logger().warn(f"切换音色 {voice} 异常：{exc}")

    def call(self, cmd: str, text: str = "", msg_type: str = "",
             audio_id: str = ""):
        """同步调用一次 TTS 服务，5 秒没响应就放弃。"""
        req = TtsService.Request()
        req.text = text
        req.type = msg_type or ("text" if text else "")
        req.cmd = cmd
        req.audio_id = audio_id
        req.extra = ""

        future = self.cli.call_async(req)
        rclpy.spin_until_future_complete(self, future, timeout_sec=5.0)
        if not future.done():
            self.get_logger().error(f"TTS {cmd} 调用超时")
            future.cancel()
            return None
        return future.result()

    def stop(self) -> None:
        """打断当前播报；stop 是 best-effort，失败不影响退出。"""
        try:
            self.call("stop", audio_id=self.audio_id)
        except Exception as exc:  # noqa: BLE001
            self.get_logger().warn(f"TTS stop 异常（忽略）：{exc}")

    def speak(self) -> bool:
        if not self.text:
            self.get_logger().error("播报文本为空")
            return False
        if not self.cli.wait_for_service(timeout_sec=5.0):
            self.get_logger().error(f"TTS 服务未就绪：{self.service}")
            return False

        self.set_voice(self.voice_id)
        try:
            return self._speak_once()
        finally:
            self.set_voice(self.voice_default)

    def _speak_once(self) -> bool:
        if self.interrupt:
            self.get_logger().info("先打断当前播报")
            self.stop()

        resp = self.call("append", text=self.text, msg_type="text",
                         audio_id=self.audio_id)
        if resp is None or not resp.success:
            self.get_logger().error("TTS append 失败")
            return False

        self.get_logger().info(f"开始播报：{self.text}")

        # 中文按每字 0.22 秒估算，给 2 倍余量 + 5 秒，最多等 120 秒。
        est = max(1.5, len(self.text) * 0.22)
        started_at = time.monotonic()
        deadline = started_at + min(est * 2.0 + 5.0, 120.0)
        seen_playing = False
        idle_since = None

        while time.monotonic() < deadline:
            resp = self.call("query")
            if resp is None or not resp.success:
                self.get_logger().error("查询播报状态失败，无法确认已播完")
                return False
            status = str(resp.status).strip().lower()
            if status in {"failed", "error", "cancelled", "canceled", "stopped"}:
                self.get_logger().error(f"播报未正常完成：{status}")
                return False
            if status not in {PLAYING_STATUS, "idle", "finished", "done", "completed", "ready"}:
                self.get_logger().error(f"无法识别播报状态：{status!r}")
                return False
            playing = status == PLAYING_STATUS
            if playing:
                seen_playing = True
                idle_since = None
            elif idle_since is None:
                idle_since = time.monotonic()
            elif seen_playing and time.monotonic() - idle_since >= SETTLE_S:
                self.get_logger().info("播报结束")
                return True
            # 有些情况下 query 一直返回 idle，观测不到 playing；
            # 超过估算时长 + 3 秒仍未见 playing，就按已开始播报处理，避免干等。
            if not seen_playing and time.monotonic() - started_at >= est + 3.0:
                self.get_logger().warn("未观测到播放状态，按已开始播报处理")
                return True
            time.sleep(0.2)

        self.get_logger().warn("等待播报结束超时，请检查音箱/服务状态")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="调用平台 TTS 播报文本")
    parser.add_argument("text", nargs="?", default=DEFAULT_TEXT,
                        help="要播报的文本；不填则播 DEFAULT_TEXT")
    parser.add_argument("--audio-id", default="audio_guide",
                        help="audio_config.json 中允许 stop 的实体（默认 audio_guide）")
    parser.add_argument("--interrupt", action="store_true",
                        help="先打断当前正在播放的音频")
    args = parser.parse_args()

    rclpy.init()
    node = None
    ok = False
    try:
        node = Speaker(args.text, args.audio_id, args.interrupt)
        ok = node.speak()
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            if not ok:
                node.stop()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
