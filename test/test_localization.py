"""离线定位回归：使用假底盘和假时钟，不访问机器人接口。"""
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/learn1_d"))
from learn1_d import nav_core as nav


def sequence(*values):
    items = iter(values)
    last = values[-1]
    def read(*args, **kwargs):
        nonlocal last
        last = next(items, last)
        if isinstance(last, BaseException):
            raise last
        return last
    return read


class Clock:
    now = 0.0
    def monotonic(self):
        return self.now
    def sleep(self, delay):
        self.now += delay


class LocalizationTests(unittest.TestCase):
    def setUp(self):
        clock = Clock()
        for name in ("monotonic", "sleep"):
            p = patch.object(nav.time, name, getattr(clock, name))
            p.start()
            self.addCleanup(p.stop)
        p = contextlib.redirect_stdout(io.StringIO())
        p.__enter__()
        self.addCleanup(p.__exit__, None, None, None)
        self.chassis = Mock()
        self.chassis.get_localization_status.return_value = {"slam_status": 0}
        self.chassis.recover_localization.return_value = {"action_id": "recover-1"}
        self.chassis.get_action_status.return_value = {"status": "succeeded"}

    def test_healthy_does_not_submit(self):
        self.assertTrue(nav.wait_localization(self.chassis, 3, None))
        self.chassis.recover_localization.assert_not_called()
        self.assertGreaterEqual(self.chassis.get_localization_status.call_count, 3)

    def test_no_pose_does_not_guess(self):
        self.chassis.get_localization_status.return_value = {"slam_status": 5}
        self.assertFalse(nav.wait_localization(self.chassis, 3, None))
        self.chassis.recover_localization.assert_not_called()

    def test_current_map_never_reloaded(self):
        self.chassis.get_current_map.return_value = {"map_name": "/maps/daolannew.pcd"}
        self.chassis.get_localization_status.return_value = {"slam_status": 5}
        self.assertEqual(nav.load_map(self.chassis, "daolannew", 3, None),
                         ("daolannew", None))
        self.chassis.start_localization.assert_not_called()
        self.chassis.get_pois.assert_not_called()

    def test_accepted_http_error_uses_full_response(self):
        exc = RuntimeError("REST 500: truncated task id")
        exc.cause = Mock()
        exc.cause.response.json.return_value = {
            "accepted": True, "action_id": "complete-action-uuid-not-truncated"}
        self.chassis.recover_localization.side_effect = exc
        self.chassis.get_localization_status.side_effect = sequence(
            {"slam_status": 5}, {"slam_status": 6}, {"slam_status": 0})
        self.chassis.get_action_status.side_effect = sequence(
            {"status": "running"}, {"status": "succeeded"})
        self.assertTrue(nav.wait_localization(self.chassis, 10, (1, 2, 0)))
        self.chassis.recover_localization.assert_called_once()
        self.chassis.get_action_status.assert_called_with("complete-action-uuid-not-truncated")

    def test_active_task_not_duplicated_or_early_success(self):
        self.chassis.get_localization_status.side_effect = sequence(
            {"slam_status": 5}, {"slam_status": 0})
        self.chassis.get_action_status.return_value = {"status": "running"}
        self.assertFalse(nav.wait_localization(self.chassis, 25, (1, 2, 0)))
        self.chassis.recover_localization.assert_called_once()
        self.chassis.cancel_action.assert_called_once_with(["recover-1"])

    def test_unknown_submission_never_retried(self):
        self.chassis.get_localization_status.return_value = {"slam_status": 5}
        self.chassis.recover_localization.side_effect = TimeoutError("network")
        with self.assertRaisesRegex(RuntimeError, "停止自动重试"):
            nav.wait_localization(self.chassis, 30, (1, 2, 0))
        self.chassis.recover_localization.assert_called_once()

    def test_missing_task_id_is_rejected(self):
        self.chassis.get_localization_status.return_value = {"slam_status": 5}
        self.chassis.recover_localization.return_value = {}
        with self.assertRaisesRegex(RuntimeError, "任务编号"):
            nav.wait_localization(self.chassis, 30, (1, 2, 0))
        self.chassis.recover_localization.assert_called_once()

    def test_explicit_failures_have_bounded_retries(self):
        self.chassis.get_localization_status.return_value = {"slam_status": 5}
        self.chassis.get_action_status.return_value = {"status": "failed"}
        self.assertFalse(nav.wait_localization(self.chassis, 60, (1, 2, 0)))
        self.assertEqual(self.chassis.recover_localization.call_count, 3)
        self.chassis.cancel_action.assert_not_called()

    def test_map_loading_and_sensor_wait_do_not_submit(self):
        self.chassis.get_localization_status.side_effect = sequence(
            {"slam_status": 7}, {"slam_status": 8}, {"slam_status": 0})
        self.assertTrue(nav.wait_localization(self.chassis, 5, (1, 2, 0)))
        self.chassis.recover_localization.assert_not_called()

    def test_interrupt_cancels_only_our_task(self):
        self.chassis.get_localization_status.return_value = {"slam_status": 5}
        self.chassis.get_action_status.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            nav.wait_localization(self.chassis, 30, (1, 2, 0))
        self.chassis.cancel_action.assert_called_once_with(["recover-1"])

    def test_forced_recovery_is_tracked(self):
        self.assertTrue(nav.wait_localization(self.chassis, 5, (1, 2, 0), force=True))
        self.chassis.recover_localization.assert_called_once()
        self.chassis.get_action_status.assert_called_with("recover-1")

    def test_forced_recovery_needs_explicit_hint(self):
        with self.assertRaises(ValueError):
            nav.wait_localization(self.chassis, 5, None, force=True)
        self.chassis.recover_localization.assert_not_called()

    def test_wrong_match_rejected_even_if_old_pose_same(self):
        self.chassis.get_pose.return_value = {"x": 20, "y": 20, "yaw": 0}
        with self.assertRaisesRegex(RuntimeError, "拒绝导航"):
            nav.relocalize(self.chassis, (1, 2, 0), 5)


if __name__ == "__main__":
    unittest.main()
