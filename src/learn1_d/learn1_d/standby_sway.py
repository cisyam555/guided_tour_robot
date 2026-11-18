"""待机时上半身轻微左右摇晃，表示机器人“活着、在等您”。

只动腰部 31 号关节（yaw）：+幅度 -> -幅度 -> 回正。
头部深度相机跟着上身一起转，所以摇晃期间不能做靠近判断；
调用方应在摇晃前后各留出稳定时间，只在回正后读取距离。
"""

import math
import time

import rclpy
from rclpy.executors import SingleThreadedExecutor
from ros2_bridge_msgs.msg import MotorCtrl, WaistCtrl

WAIST_TOPIC = "/waist/cmd"
YAW_MOTOR = 31
DEFAULT_SPEED = 0.5   # 关节速度 rad/s，慢一点更像“待机”
DEFAULT_CURRENT = 10.0  # 最大电流 A；不填 0 会被当成 0 A，电机不动


class WaistSway:
    def __init__(self, speed=DEFAULT_SPEED):
        self._context = rclpy.Context()
        rclpy.init(context=self._context)
        self._node = rclpy.create_node("standby_sway", context=self._context)
        self._executor = SingleThreadedExecutor(context=self._context)
        self._executor.add_node(self._node)
        self._pub = self._node.create_publisher(WaistCtrl, WAIST_TOPIC, 10)
        self._speed = float(speed)
        self._announced = False
        # 等订阅端出现再发，否则第一条指令会在 DDS 匹配完成前被丢掉，
        # 表现就是只往一边摇。匹配后再多留 0.3 s 让桥接端把句柄建好。
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self._pub.get_subscription_count() == 0:
            self._executor.spin_once(timeout_sec=0.05)
        time.sleep(0.3)

    def _send(self, pos):
        try:
            cmd = MotorCtrl()
            cmd.name = YAW_MOTOR
            cmd.pos = float(pos)
            cmd.spd = self._speed
            cmd.cur = DEFAULT_CURRENT
            msg = WaistCtrl()
            msg.mode = 0
            msg.label = 0
            msg.header.stamp = self._node.get_clock().now().to_msg()
            msg.header.frame_id = "waist"
            msg.ctrl = [cmd]
            # 连发几帧：位置指令是绝对目标，重发不会叠加，只提高到达率。
            for _ in range(3):
                msg.header.stamp = self._node.get_clock().now().to_msg()
                self._pub.publish(msg)
                self._executor.spin_once(timeout_sec=0.1)
        except Exception as exc:  # noqa: BLE001 - 摇晃失败不影响待机
            if not self._context.ok():  # Ctrl+C 之后 context 已关，属于正常退出
                return
            print(f"  警告：上身摇晃指令发送失败：{exc}")

    def sweep(self, degrees):
        """摇一个来回并回正，阻塞到动作结束。"""
        amp = math.radians(abs(float(degrees)))
        if amp <= 0.0:
            return
        travel = amp / self._speed + 0.15
        if not self._announced:
            print(f"待机中：上半身持续左右各摇 {abs(float(degrees)):.0f}°（有人靠近会先回正）")
            self._announced = True
        self._send(amp)
        time.sleep(travel)
        self._send(-amp)
        time.sleep(travel * 2)
        self._send(0.0)
        time.sleep(travel + 0.2)

    def center(self):
        """回到正中，用于有人靠近或退出待机前。"""
        self._send(0.0)
        time.sleep(0.4)

    def shutdown(self):
        try:
            self.center()
            self._executor.shutdown(timeout_sec=1.0)
            self._node.destroy_node()
            rclpy.shutdown(context=self._context)
        except Exception:  # noqa: BLE001 - Ctrl+C 时 context 可能已关闭
            pass
