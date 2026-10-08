#!/usr/bin/env python3
"""
server.py - SEG-Y Viewer HTTP server
Built on the Python standard library HTTP server (no web framework).
Run:   python server.py
Open:  http://localhost:8000
"""

import json
import os
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

# Make the project modules importable
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from segy_parser import parse_segy_file
from classifier import classify
from coord_utils import (apply_scalar_array, to_decimal_degrees, dominant_value,
                         build_geojson, compute_bbox)
from crs_detect import detect_crs, transform_to_wgs84

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
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024     # 8 MB


class SegyHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    # ── Helpers ─────────────────────────────────────────────────

    def send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False, default=_json_default).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── Routes ──────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split('?')[0]  # strip query string

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
        if urlsplit(self.path).path == '/api/upload':
            self._handle_upload()
        else:
            self.send_json(404, {'error': 'Not Found'})

    # ── Upload handling ─────────────────────────────────────────

    def _handle_upload(self):
        """
        The file is sent as the raw request body and streamed to disk in chunks,
        so memory use does not grow with file size. Parameters are in the query
        string: ?filename=...&region=...&epsg=...
        """
        params = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        filename = Path(params.get('filename', '')).name
        region_hint = params.get('region', 'auto')

        try:
            content_length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self.send_json(400, {'error': 'Empty upload'})
            return
        if content_length > MAX_FILE_SIZE:
            self.send_json(413, {'error': f'File too large (max {MAX_FILE_SIZE // 1024**3} GB)'})
            return

        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            self.send_json(400, {'error': f'Unsupported file type: {ext or "none"} (expected .sgy or .segy)'})
            return

        epsg_override = None
        raw_epsg = params.get('epsg', '').upper().removeprefix('EPSG:').strip()
        if raw_epsg:
            if not raw_epsg.isdigit():
                self.send_json(400, {'error': f'Invalid EPSG code format: {raw_epsg}'})
                return
            epsg_override = int(raw_epsg)

        # Stream to a temp file (segyio needs a path on disk)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext, dir=UPLOAD_DIR)
        try:
            with os.fdopen(tmp_fd, 'wb') as f:
                remaining = content_length
                while remaining > 0:
                    chunk = self.rfile.read(min(UPLOAD_CHUNK_SIZE, remaining))
                    if not chunk:
                        raise ConnectionError('Upload interrupted')
                    f.write(chunk)
                    remaining -= len(chunk)

            t0 = time.monotonic()
            result = _parse_and_classify(tmp_path, filename, region_hint, epsg_override)
            result['parse_time_ms'] = round((time.monotonic() - t0) * 1000)

            self.send_json(200, result)

        except ValueError as e:
            self.send_json(400, {'error': str(e)})
        except Exception as e:
            import traceback
            print(f"[ERROR] {e}\n{traceback.format_exc()}")
            self.send_json(500, {'error': str(e), 'type': type(e).__name__})
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ── Static files ────────────────────────────────────────────

    def _serve_file(self, path: Path):
        # Prevent path traversal
        try:
            path.resolve().relative_to(STATIC_DIR.resolve())
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


# ── Core pipeline ───────────────────────────────────────────

def _parse_and_classify(filepath: str, original_name: str,
                        region_hint: str = 'auto', epsg_override: int = None) -> dict:
    """Run the full pipeline and return the complete result."""
    raw = parse_segy_file(filepath)
    cls = classify(raw)

    ta = raw['trace_arrays']
    scalars = ta['scalars']

    # Prefer CDP coordinates when present, otherwise source coordinates
    cdpx_valid = np.any(ta['cdpx'] != 0)
    if cdpx_valid:
        x_raw = apply_scalar_array(ta['cdpx'], scalars)
        y_raw = apply_scalar_array(ta['cdpy'], scalars)
        coord_source = 'CDP'
    else:
        x_raw = apply_scalar_array(ta['sx'], scalars)
        y_raw = apply_scalar_array(ta['sy'], scalars)
        coord_source = 'Source'

    # Convert geographic units (arc-seconds / DMS) to decimal degrees first
    coord_unit = dominant_value(ta['coord_units'])
    x_src = to_decimal_degrees(x_raw, coord_unit)
    y_src = to_decimal_degrees(y_raw, coord_unit)

    crs = detect_crs(raw['ebcdic_raw'], x_src, y_src, coord_unit,
                     region_hint, epsg_override)
    if crs['epsg']:
        lon, lat = transform_to_wgs84(x_src, y_src, crs['epsg'])
    else:
        lon = np.full(len(x_src), np.nan)
        lat = np.full(len(y_src), np.nan)

    geojson = build_geojson(lon, lat, ta['ilines'], ta['xlines'])
    bbox    = compute_bbox(lon, lat)
    dominant_scalar = dominant_value(scalars)

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
            'coord_unit':      coord_unit,
            'source_crs':      crs,
            'target_crs':      {'epsg': 4326, 'name': 'WGS 84'},
            'bbox':            bbox,
            'raw_xy':          raw_xy,
        },
        'geojson': geojson,
    }


def _json_default(obj):
    """JSON serialisation for numpy types."""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


if __name__ == '__main__':
    # Local-only by default; set SEGY_VIEWER_HOST=0.0.0.0 to expose on the network
    host = os.environ.get('SEGY_VIEWER_HOST', '127.0.0.1')
    port = int(os.environ.get('SEGY_VIEWER_PORT', '8000'))
    server = ThreadingHTTPServer((host, port), SegyHandler)
    print("SEG-Y Viewer running")
    print(f"Open http://localhost:{port}")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
