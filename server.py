#!/usr/bin/env python3
"""
server.py - SEG-Y Viewer HTTP 服务器
无需安装任何额外依赖，纯 Python 3.13 标准库实现。
启动：python server.py
访问：http://localhost:8000
"""

import email.parser
import email.policy
import json
import os
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 将项目目录加入 Python 路径
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from segy_parser import parse_segy_file
from classifier import classify
from coord_utils import apply_scalar_array, guess_crs_and_convert, build_geojson, compute_bbox

STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MIME_MAP = {
    '.html': 'text/html; charset=utf-8',
    '.js':   'application/javascript; charset=utf-8',
    '.css':  'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.ico':  'image/x-icon',
}

ALLOWED_EXTENSIONS = {'.sgy', '.segy'}
MAX_FILE_SIZE = 5 * 1024 * 1024 * 1024  # 5 GB


class SegyHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    # ── 工具方法 ────────────────────────────────────────────────

    def send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False, default=_json_default).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self._add_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _add_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    # ── 路由 ────────────────────────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(204)
        self._add_cors_headers()
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]  # 去除 query string

        if path in ('/', '/index.html'):
            self._serve_file(STATIC_DIR / 'index.html')
        elif path.startswith('/static/'):
            rel = path[len('/static/'):]
            self._serve_file(STATIC_DIR / rel)
        elif path == '/api/health':
            import segyio, importlib.metadata
            try:
                segyio_ver = importlib.metadata.version('segyio')
            except Exception:
                segyio_ver = 'installed'
            self.send_json(200, {
                'status': 'ok',
                'segyio_version': segyio_ver,
                'python': sys.version,
            })
        else:
            self.send_json(404, {'error': 'Not Found'})

    def do_POST(self):
        if self.path == '/api/upload':
            self._handle_upload()
        else:
            self.send_json(404, {'error': 'Not Found'})

    # ── 文件上传处理 ─────────────────────────────────────────────

    def _handle_upload(self):
        content_type = self.headers.get('Content-Type', '')
        content_length = int(self.headers.get('Content-Length', 0))

        if 'multipart/form-data' not in content_type:
            self.send_json(400, {'error': '需要 multipart/form-data 请求'})
            return

        if content_length > MAX_FILE_SIZE:
            self.send_json(400, {'error': f'文件过大，最大支持 {MAX_FILE_SIZE // 1024**3} GB'})
            return

        # 读取请求体
        raw_body = self.rfile.read(content_length)

        # Python 3.13：用 email 模块解析 multipart（cgi 已移除）
        mime_header = f'Content-Type: {content_type}\r\n\r\n'.encode()
        parser = email.parser.BytesFeedParser(policy=email.policy.compat32)
        parser.feed(mime_header + raw_body)
        msg = parser.close()

        # 找文件部分和坐标系参数
        file_data = None
        filename = 'upload.sgy'
        region_hint = 'auto'
        zone_hint = None
        payload = msg.get_payload()
        if isinstance(payload, list):
            for part in payload:
                fn = part.get_filename()
                cd = part.get('Content-Disposition', '')
                if fn:
                    filename = fn
                    file_data = part.get_payload(decode=True)
                elif 'name="region"' in cd:
                    region_hint = (part.get_payload(decode=True) or b'auto').decode().strip()
                elif 'name="zone"' in cd:
                    try:
                        z = int((part.get_payload(decode=True) or b'0').decode().strip())
                        if 1 <= z <= 60:
                            zone_hint = z
                    except ValueError:
                        pass

        if file_data is None:
            self.send_json(400, {'error': '未找到上传文件'})
            return

        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            self.send_json(400, {'error': f'不支持的文件格式：{ext}，请上传 .sgy 或 .segy 文件'})
            return

        # 写临时文件（segyio 需要磁盘路径）
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext, dir=UPLOAD_DIR)
        try:
            with os.fdopen(tmp_fd, 'wb') as f:
                f.write(file_data)

            t0 = time.monotonic()
            result = _parse_and_classify(tmp_path, filename, region_hint, zone_hint)
            result['parse_time_ms'] = round((time.monotonic() - t0) * 1000)

            self.send_json(200, result)

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[ERROR] {e}\n{tb}")
            self.send_json(500, {
                'error': str(e),
                'type': type(e).__name__,
            })
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ── 静态文件服务 ─────────────────────────────────────────────

    def _serve_file(self, path: Path):
        # 防止路径穿越
        try:
            path.resolve().relative_to(BASE_DIR.resolve())
        except ValueError:
            self.send_json(403, {'error': 'Forbidden'})
            return

        if not path.exists() or not path.is_file():
            self.send_json(404, {'error': f'{path.name} not found'})
            return

        data = path.read_bytes()
        mime = MIME_MAP.get(path.suffix.lower(), 'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ── 核心解析函数 ─────────────────────────────────────────────────

def _parse_and_classify(filepath: str, original_name: str,
                        region_hint: str = 'auto', zone_hint: int = None) -> dict:
    """整合各模块，返回完整解析结果。"""
    raw = parse_segy_file(filepath)
    cls = classify(raw)

    ta = raw['trace_arrays']
    scalars = ta['scalars']

    # 优先用 CDP 坐标（如果有效），否则用炮点坐标
    cdpx_valid = np.any(ta['cdpx'] != 0)
    if cdpx_valid:
        x_raw = apply_scalar_array(ta['cdpx'], scalars)
        y_raw = apply_scalar_array(ta['cdpy'], scalars)
        coord_source = 'CDP'
    else:
        x_raw = apply_scalar_array(ta['sx'], scalars)
        y_raw = apply_scalar_array(ta['sy'], scalars)
        coord_source = 'Source'

    # 优先用 EBCDIC 里读到的 zone，其次用用户手动指定的
    ebcdic_zone = cls.get('utm_zone_from_ebcdic')
    effective_zone = ebcdic_zone or zone_hint
    coord_info = guess_crs_and_convert(x_raw, y_raw, ta['coord_units'],
                                       region_hint, effective_zone)
    geojson    = build_geojson(coord_info['lon'], coord_info['lat'],
                               ta['ilines'], ta['xlines'])
    bbox       = compute_bbox(coord_info['lon'], coord_info['lat'])

    # 坐标标量取众数
    scalars_nonzero = scalars[scalars != 0]
    dominant_scalar = int(scalars_nonzero[0]) if len(scalars_nonzero) > 0 else 0

    # Raw X/Y stats (before CRS conversion)
    x_valid = x_raw[x_raw != 0]
    y_valid = y_raw[y_raw != 0]
    raw_xy = {}
    if len(x_valid) > 0:
        raw_xy = {
            'x_min': round(float(x_valid.min()), 2),
            'x_max': round(float(x_valid.max()), 2),
            'y_min': round(float(y_valid.min()), 2),
            'y_max': round(float(y_valid.max()), 2),
            'x_first': round(float(x_raw[x_raw != 0][0]), 2),
            'y_first': round(float(y_raw[y_raw != 0][0]), 2),
            'x_last':  round(float(x_raw[x_raw != 0][-1]), 2),
            'y_last':  round(float(y_raw[y_raw != 0][-1]), 2),
        }

    return {
        'filename':  original_name,
        'file_size_bytes': os.path.getsize(filepath),
        'classification': {
            'dimension':   cls['dimension'],
            'stack_type':  cls['stack_type'],
            'domain':      cls['domain'],
            'survey_type': cls['survey_type'],
            'data_type':   cls['data_type'],
            'utm_zone_from_ebcdic': cls.get('utm_zone_from_ebcdic'),
        },
        'binary_header': raw['bin_hdr'],
        'ebcdic': {
            'raw_text': raw['ebcdic_raw'],
            'keywords': cls['ebcdic_keywords'],
        },
        'trace_stats': {
            'total_traces':    raw['total_traces'],
            'traces_sampled':  raw['sampled_count'],
        },
        'coordinate_info': {
            'coord_source':    coord_source,
            'scalar':          dominant_scalar,
            'crs_guess':       coord_info['crs'],
            'utm_zone':        coord_info.get('utm_zone'),
            'wgs84_converted': coord_info['converted'],
            'bbox':            bbox,
            'raw_xy':          raw_xy,
        },
        'geojson': geojson,
    }


def _json_default(obj):
    """JSON 序列化 numpy 类型兼容。"""
    import numpy as np
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


# numpy 在 _parse_and_classify 中用到，需在模块级导入
import numpy as np


if __name__ == '__main__':
    host = '0.0.0.0'
    port = 8000
    server = ThreadingHTTPServer((host, port), SegyHandler)
    print(f"SEG-Y Viewer 启动成功")
    print(f"访问地址：http://localhost:{port}")
    print(f"按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
