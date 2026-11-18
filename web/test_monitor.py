"""Independent read-only backend checks; starts no ROS node or robot service."""
import ast
import json
import threading
import time
import unittest
from unittest import mock
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import monitor


class ReadOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = monitor.State()
        cls.server = monitor.Server(('127.0.0.1', 0), monitor.Handler)
        cls.server.state = cls.state
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def test_status_is_explicitly_read_only(self):
        value = json.load(urllib.request.urlopen(self.base+'/api/status'))
        self.assertIs(value['read_only'], True)
        self.assertFalse(value['ros']['available'])
        self.assertEqual(value['host'], {})

    def test_tour_endpoint_reads_project_configuration(self):
        value = json.load(urllib.request.urlopen(self.base+'/api/tour'))
        self.assertEqual(value, json.loads(monitor.TOUR_PATH.read_text()))
        self.assertIn('POI_02', value['pois'])

    def test_invalid_tour_is_reported_as_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tour.json'
            path.write_text('{"pois": []}')
            with mock.patch.object(monitor, 'TOUR_PATH', path):
                with self.assertRaises(urllib.error.HTTPError) as result:
                    urllib.request.urlopen(self.base+'/api/tour')
                self.assertEqual(result.exception.code, 503)

    def test_no_control_routes_or_write_methods(self):
        for path in ['/api/start', '/api/stop', '/api/navigate', '/api/status']:
            for method in ['POST', 'PUT', 'PATCH', 'DELETE']:
                with self.subTest(path=path, method=method):
                    request = urllib.request.Request(self.base+path, data=b'{}', method=method)
                    with self.assertRaises(urllib.error.HTTPError) as result:
                        urllib.request.urlopen(request)
                    self.assertEqual(result.exception.code, 405)

    def test_no_directory_or_source_access(self):
        for path in ['/monitor.py', '/start_monitor.sh', '/demo.html', '/api/start', '/../README.md']:
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as result:
                    urllib.request.urlopen(self.base+path)
                self.assertEqual(result.exception.code, 404)

    def test_stale_camera_is_not_served(self):
        with self.state.lock:
            self.state.frames['color'] = {'at': time.time()-10, 'jpeg': b'old', 'width': 1, 'height': 1}
        with self.assertRaises(urllib.error.HTTPError) as result:
            urllib.request.urlopen(self.base+'/api/frame?kind=color')
        self.assertEqual(result.exception.code, 503)

    def test_snapshot_excludes_image_bytes_and_marks_age(self):
        self.state.sample('test', {'value': 5})
        with self.state.lock:
            self.state.frames['color'] = {'at': time.time(), 'jpeg': b'bytes', 'width': 1, 'height': 1}
        value = self.state.snapshot()
        self.assertGreaterEqual(value['samples']['test']['age_s'], 0)
        self.assertNotIn('jpeg', value['cameras']['color'])
        json.dumps(value, allow_nan=False)

    def test_nonfinite_and_credential_redaction(self):
        self.assertIsNone(monitor.finite(float('nan')))
        self.assertIsNone(monitor.finite(float('inf')))
        redacted = monitor.redact('api_key=private_value access_token: private_token Bearer private_bearer')
        for secret in ['private_value', 'private_token', 'private_bearer']:
            self.assertNotIn(secret, redacted)

    def test_chassis_battery_uses_explicit_charging_flag(self):
        parsed = monitor.parse_chassis_battery({'success': True, 'batteryPercentage': 62, 'current': -31003, 'isCharging': True})
        self.assertEqual(parsed['percent'], 62)
        self.assertIs(parsed['is_charging'], True)
        self.assertIsNone(monitor.parse_chassis_battery({'success': True, 'batteryPercentage': 0})['is_charging'])
        for data in [{'success': False, 'batteryPercentage': 62}, {'success': True, 'batteryPercentage': -1}, {'success': True, 'batteryPercentage': float('nan')}, {'success': True, 'batteryPercentage': True}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                monitor.parse_chassis_battery(data)

    def test_backend_has_no_robot_command_calls(self):
        tree = ast.parse(Path(monitor.__file__).read_text())
        forbidden = {'create_publisher', 'create_client', 'publish', 'call_async', 'send_goal_async', 'set_parameters'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden)


if __name__ == '__main__':
    unittest.main()
