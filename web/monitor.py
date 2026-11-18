#!/usr/bin/env python3
"""Read-only ROS and host monitor. No command, publish, service or action endpoints."""
import argparse
import collections
import json
import math
import re
import signal
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import urllib.request

import psutil

ROOT = Path(__file__).resolve().parent
TOUR_PATH = ROOT.parent / 'src/learn1_d/learn1_d/tour.json'

def load_tour():
    data = json.loads(TOUR_PATH.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('pois'), list) or not data['pois']:
        raise ValueError('路线配置缺少有效 pois 数组')
    return data


def finite(value):
    value = float(value)
    return value if math.isfinite(value) else None


def redact(text):
    text = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|access[_-]?key(?:[_-]?secret|[_-]?id)?|secret|password|authorization)\s*[:=]\s*)[^\s,;]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [redacted]', text)
    return text[:600]


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.started = time.time()
        self.logs = collections.deque(maxlen=200)
        self.seq = 0
        self.host = {}
        self.ros = {'available': False, 'error': 'ROS 正在连接', 'nodes': [], 'topics': [], 'services': [], 'graph_at': None}
        self.samples = {}
        self.frames = {}
        self.stopping = threading.Event()
        self.add_log('INFO', 'monitor', '只读监测已启动；未开放机器人控制接口')

    def add_log(self, level, source, text):
        with self.lock:
            self.seq += 1
            self.logs.append({'id': self.seq, 'at': time.time(), 'level': level, 'source': source[:100], 'text': redact(text)})

    def sample(self, key, data):
        with self.lock:
            self.samples[key] = {'at': time.time(), **data}

    def snapshot(self):
        with self.lock:
            now = time.time()
            samples = {key: {**data, 'age_s': round(max(0, now-data['at']), 2)} for key, data in self.samples.items()}
            cameras = {key: {'at': data['at'], 'age_s': round(max(0, now-data['at']), 2), 'width': data['width'], 'height': data['height']} for key, data in self.frames.items()}
            graph_age = now-self.ros['graph_at'] if self.ros['graph_at'] else None
            return {'read_only': True, 'preview': getattr(self, 'preview', False), 'server_time': now, 'started_at': self.started, 'host': self.host.copy(), 'ros': {**self.ros, 'graph_age_s': graph_age}, 'samples': samples, 'cameras': cameras}


def host_worker(state):
    psutil.cpu_percent()
    while not state.stopping.is_set():
        try:
            memory = psutil.virtual_memory()
            temps = {}
            for zone in Path('/sys/class/thermal').glob('thermal_zone*'):
                try:
                    value = float((zone/'temp').read_text())/1000
                    if math.isfinite(value):
                        temps[(zone/'type').read_text().strip()] = round(value, 1)
                except (ValueError, OSError, TypeError):
                    pass
            cpu_temp = next((v for k, v in temps.items() if 'cpu' in k.lower()), None)
            with state.lock:
                state.host = {'at': time.time(), 'cpu_percent': psutil.cpu_percent(), 'memory_used_gb': round(memory.used/1024**3, 2), 'memory_total_gb': round(memory.total/1024**3, 2), 'memory_percent': memory.percent, 'cpu_temp_c': cpu_temp, 'uptime_s': round(time.time()-psutil.boot_time()), 'temperatures': temps}
        except Exception as exc:
            traceback.print_exc()
            state.add_log('WARN', 'monitor.host', type(exc).__name__)
        state.stopping.wait(2)



BATTERY_STATUS_URL = 'http://192.168.11.10:9090/api/core/system/v1/battery/pack'


def parse_chassis_battery(data):
    if not isinstance(data, dict) or data.get('success') is not True:
        raise ValueError('Battery response was not successful')
    value = data.get('batteryPercentage')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError('Invalid battery percentage')
    charging = data.get('isCharging')
    current = data.get('current')
    return {'percent': float(value), 'is_charging': charging if isinstance(charging, bool) else None,
            'current_ma': finite(current) if isinstance(current, (int, float)) and not isinstance(current, bool) else None,
            'topic': '底盘 GET /api/core/system/v1/battery/pack'}


def chassis_battery_worker(state):
    # One fixed, known read-only endpoint. No SDK constructor, command or proxy API.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    was_available = None
    previous_charging = None
    while not state.stopping.is_set():
        try:
            with opener.open(BATTERY_STATUS_URL, timeout=3) as response:
                data = json.loads(response.read(65536))
            sample = parse_chassis_battery(data)
            state.sample('chassis_battery', sample)
            if was_available is not True:
                state.add_log('INFO', 'monitor.battery', '底盘电池只读接口已连接')
            if sample['is_charging'] is not previous_charging:
                state.add_log('INFO', 'monitor.battery', '底盘报告：'+('正在充电' if sample['is_charging'] else '未充电' if sample['is_charging'] is False else '充电状态未知'))
            previous_charging = sample['is_charging']
            was_available = True
        except Exception as exc:
            if was_available is not False:
                state.add_log('WARN', 'monitor.battery', '底盘电池状态暂不可用：'+type(exc).__name__)
            was_available = False
        state.stopping.wait(5)


def ros_worker(state):
    node = None
    context = None
    executor = None
    try:
        import cv2
        import numpy as np
        import rclpy
        from rclpy.context import Context
        from rclpy.node import Node
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        from rosidl_runtime_py.utilities import get_message

        context = Context()
        rclpy.init(context=context)
        node = Node('guided_tour_web_monitor', context=context, enable_rosout=False, start_parameter_services=False)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)
        subscriptions = {}
        last_process = {}
        last_graph = 0

        def image_callback(kind, msg):
            now = time.monotonic()
            if now-last_process.get(kind, 0) < (0.33 if kind == 'color' else 0.5):
                return
            last_process[kind] = now
            try:
                if kind == 'color':
                    if hasattr(msg, 'format'):
                        image = cv2.imdecode(np.frombuffer(msg.data, dtype=np.uint8), cv2.IMREAD_COLOR)
                    else:
                        channels = {'rgb8': 3, 'bgr8': 3, 'rgba8': 4, 'bgra8': 4, 'mono8': 1}.get(msg.encoding)
                        if channels is None:
                            return
                        image = np.ndarray((msg.height, msg.width, channels), dtype=np.uint8, buffer=msg.data, strides=(msg.step, channels, 1))
                        conversions = {'rgb8': cv2.COLOR_RGB2BGR, 'rgba8': cv2.COLOR_RGBA2BGR, 'bgra8': cv2.COLOR_BGRA2BGR, 'mono8': cv2.COLOR_GRAY2BGR}
                        if msg.encoding in conversions:
                            image = cv2.cvtColor(image, conversions[msg.encoding])
                else:
                    dtype = {'16UC1': np.dtype('u2'), '32FC1': np.dtype('f4')}.get(msg.encoding)
                    if dtype is None:
                        return
                    dtype = dtype.newbyteorder('>' if msg.is_bigendian else '<')
                    depth = np.ndarray((msg.height, msg.width), dtype=dtype, buffer=msg.data, strides=(msg.step, dtype.itemsize)).astype(np.float32)
                    if msg.encoding == '16UC1':
                        depth /= 1000
                    h, w = depth.shape
                    roi = depth[int(h*.25):int(h*.60), int(w*.35):int(w*.65)]
                    valid = roi[np.isfinite(roi) & (roi > .05)]
                    state.sample('depth', {'distance_m': round(float(np.percentile(valid, 5)), 3) if valid.size else None, 'zero_ratio': round(float(np.count_nonzero(roi <= .05))/roi.size, 3) if roi.size else None, 'topic': '/ob_camera_head/depth/image_raw'})
                    scaled = (np.clip(np.nan_to_num(depth, nan=0, posinf=0, neginf=0), 0, 5)/5*255).astype(np.uint8)
                    image = cv2.applyColorMap(255-scaled, cv2.COLORMAP_TURBO)
                    image[~np.isfinite(depth) | (depth <= .05)] = 0
                if image is None:
                    return
                height, width = image.shape[:2]
                if width > 768:
                    image = cv2.resize(image, (768, round(height*768/width)), interpolation=cv2.INTER_AREA)
                ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 65])
                if ok:
                    with state.lock:
                        state.frames[kind] = {'at': time.time(), 'jpeg': encoded.tobytes(), 'width': width, 'height': height}
            except Exception as exc:
                if now-last_process.get(kind+'_error', 0) > 30:
                    last_process[kind+'_error'] = now
                    state.add_log('WARN', 'monitor.camera', kind+': '+type(exc).__name__)

        def battery_callback(msg):
            state.sample('battery', {'master_percent': finite(msg.master_battery_power), 'little_percent': finite(msg.little_battery_power), 'master_voltage': finite(msg.master_battery_voltage), 'master_current': finite(msg.master_battery_current), 'installed': int(msg.battery_installed), 'working': int(msg.battery_working), 'topic': '/power/battery/status'})

        def board_callback(msg):
            state.sample('power', {'percent': finite(msg.battery_power), 'voltage': finite(msg.battery_voltage), 'current': finite(msg.battery_current), 'topic': '/power/board/status'})

        def ros_log(msg):
            # Infrastructure warnings only; no conversations or arbitrary process output.
            allowed = ('ob_camera', 'knewbots_chassis', 'power_board', 'guide_front_guard', 'tour_voice_wait', 'guide_tts')
            if msg.level >= 30 and any(x in msg.name for x in allowed):
                now = time.monotonic()
                if now-last_process.get('ros_log', 0) >= 1:
                    last_process['ros_log'] = now
                    state.add_log('ERROR' if msg.level >= 40 else 'WARN', msg.name, msg.msg)

        def camera_status(msg):
            state.sample('camera_status', {'online': bool(msg.device_online), 'color_fps': finite(msg.color_frame_rate_cur), 'depth_fps': finite(msg.depth_frame_rate_cur), 'color_delay_ms': finite(msg.color_delay_ms_cur), 'connection': msg.connection_type})

        while not state.stopping.is_set():
            if time.monotonic()-last_graph > 5:
                last_graph = time.monotonic()
                nodes = sorted(set('/'.join((ns.rstrip('/'), name)) for name, ns in node.get_node_names_and_namespaces()))
                topics = dict(node.get_topic_names_and_types())
                services = [name for name, types in node.get_service_names_and_types()]
                with state.lock:
                    was_available = state.ros['available']
                    state.ros = {'available': True, 'error': None, 'nodes': nodes, 'topics': sorted(topics), 'services': sorted(services), 'graph_at': time.time()}
                if not was_available:
                    state.add_log('INFO', 'monitor.ros', 'ROS 图已连接；仅订阅已有状态与图像话题')
                color_topic = '/ob_camera_head/color/image_raw/compressed' if '/ob_camera_head/color/image_raw/compressed' in topics else '/ob_camera_head/color/image_raw'
                candidates = [(color_topic, lambda m: image_callback('color', m)), ('/ob_camera_head/depth/image_raw', lambda m: image_callback('depth', m)), ('/power/battery/status', battery_callback), ('/power/board/status', board_callback), ('/ob_camera_head/device_status', camera_status), ('/rosout', ros_log), ('/lyre/audio_stream', lambda m: state.sample('microphone', {'topic': '/lyre/audio_stream'}))]
                for topic, callback in candidates:
                    if topic in topics and topic not in subscriptions:
                        try:
                            msg_type = get_message(topics[topic][0])
                            subscriptions[topic] = node.create_subscription(msg_type, topic, callback, qos)
                            state.add_log('INFO', 'monitor.ros', '只读订阅 '+topic)
                        except Exception as exc:
                            state.add_log('WARN', 'monitor.ros', '无法订阅 '+topic+' ('+type(exc).__name__+')')
            executor.spin_once(timeout_sec=.1)
    except Exception as exc:
        traceback.print_exc()
        with state.lock:
            state.ros['available'] = False
            state.ros['error'] = 'ROS 监测中断：'+type(exc).__name__
        state.add_log('ERROR', 'monitor.ros', 'ROS 监测不可用：'+type(exc).__name__)
    finally:
        if executor is not None:
            executor.shutdown(timeout_sec=2)
        if node is not None:
            node.destroy_node()
        if context is not None and context.ok():
            context.shutdown()


def ros_supervisor(state):
    while not state.stopping.is_set():
        ros_worker(state)
        if not state.stopping.wait(5):
            state.add_log('INFO', 'monitor.ros', '正在重新连接 ROS 只读监测')


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, content_type='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        state = self.server.state
        if parsed.path == '/api/tour':
            try:
                return self.send(200, json.dumps(load_tour(), ensure_ascii=False).encode())
            except (ValueError, OSError):
                return self.send(503, json.dumps({'error': '导览配置读取失败'}, ensure_ascii=False).encode())
        if parsed.path == '/api/status':
            return self.send(200, json.dumps(state.snapshot(), allow_nan=False).encode())
        if parsed.path == '/api/logs':
            with state.lock:
                logs = list(state.logs)
            return self.send(200, json.dumps({'logs': logs}, ensure_ascii=False).encode())
        if parsed.path == '/api/frame':
            kind = parse_qs(parsed.query).get('kind', ['color'])[0]
            with state.lock:
                frame = state.frames.get(kind)
            if not frame or time.time()-frame['at'] > 3:
                return self.send(503, b'{"error":"no_fresh_frame"}')
            return self.send(200, frame['jpeg'], 'image/jpeg')
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/index.html': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'application/javascript; charset=utf-8'), '/styles.css': ('styles.css', 'text/css; charset=utf-8'), '/favicon.ico': (None, '')}
        if parsed.path not in files:
            return self.send(404, b'{"error":"not_found"}')
        filename, mime = files[parsed.path]
        if filename is None:
            return self.send(204, b'')
        return self.send(200, (ROOT/filename).read_bytes(), mime)

    def do_POST(self):
        self.send(405, b'{"error":"read_only_monitor"}')
    do_PUT = do_DELETE = do_PATCH = do_POST


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--preview', action='store_true', help='仅浏览配置，不连接 ROS、底盘或读取主机状态')
    args = parser.parse_args()
    state = State()
    state.preview = args.preview
    if args.preview:
        state.add_log('INFO', 'web', '配置预览模式；未连接机器人')
    server = Server((args.bind, args.port), Handler)
    server.state = state
    threads = [threading.Thread(target=target, args=(state,), daemon=True) for target in (() if args.preview else (host_worker, ros_supervisor, chassis_battery_worker))]
    for thread in threads:
        thread.start()
    def stop(*unused):
        state.stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f'Read-only monitor listening on {args.bind}:{args.port}', flush=True)
    try:
        server.serve_forever()
    finally:
        state.stopping.set()
        server.server_close()
        for thread in threads:
            thread.join(timeout=5)


if __name__ == '__main__':
    main()
