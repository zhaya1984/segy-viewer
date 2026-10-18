"""End-to-end tests on synthetic SEG-Y files, through the parser and the HTTP server."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
import pytest

import server
from classifier import extract_ebcdic_keywords


def test_pipeline_on_mga54_line(mga54_segy):
    r = server._parse_and_classify(str(mga54_segy), mga54_segy.name)

    assert r['classification']['dimension'] == '2D'
    assert r['classification']['stack_type'] == 'poststack'
    assert r['classification']['domain'] == 'time'

    crs = r['coordinate_info']['source_crs']
    assert crs['epsg'] == 28354
    assert crs['confidence'] == 'high'

    bbox = r['coordinate_info']['bbox']
    assert 141.47 < bbox['min_lon'] < bbox['max_lon'] < 141.54
    assert -31.95 < bbox['min_lat'] < bbox['max_lat'] < -31.86
    # scalar -100 must be applied: raw values are centimetres
    assert r['coordinate_info']['raw_xy']['x_max'] == pytest.approx(550935.0)


def test_pipeline_without_crs_does_not_guess(no_crs_segy):
    r = server._parse_and_classify(str(no_crs_segy), no_crs_segy.name)
    assert r['coordinate_info']['source_crs']['epsg'] is None
    assert [f for f in r['geojson']['features'] if f['geometry']['type'] == 'Point'] == []


def test_area_keyword_ignores_field_tape_numbers():
    kw = extract_ebcdic_keywords("C17 ORIGINAL FIELD TAPE NOS: L17405001-L17405022")
    assert 'area' not in kw
    assert extract_ebcdic_keywords("C 5 BASIN : COOPER BASIN")['area'] == 'COOPER BASIN'


# ── HTTP layer ──────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def base_url():
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.SegyHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}'
    httpd.shutdown()


def request(url, data=None):
    req = urllib.request.Request(url, data=data, method='POST' if data is not None else 'GET')
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_upload_streams_file_and_returns_result(base_url, mga54_segy):
    status, body = request(f'{base_url}/api/upload?filename=line.sgy&region=auto',
                           mga54_segy.read_bytes())
    assert status == 200
    assert json.loads(body)['coordinate_info']['source_crs']['epsg'] == 28354


def test_upload_with_epsg_override(base_url, mga54_segy):
    status, body = request(f'{base_url}/api/upload?filename=line.sgy&epsg=EPSG:7854',
                           mga54_segy.read_bytes())
    assert status == 200
    assert json.loads(body)['coordinate_info']['source_crs']['confidence'] == 'user'


@pytest.mark.parametrize('query, message', [
    ('filename=line.txt', 'Unsupported file type'),
    ('filename=line.sgy&epsg=abc', 'Invalid EPSG'),
    ('filename=line.sgy&epsg=99999', 'Unknown EPSG'),
])
def test_upload_rejects_bad_input(base_url, mga54_segy, query, message):
    status, body = request(f'{base_url}/api/upload?{query}', mga54_segy.read_bytes())
    assert status in (400, 413)
    assert message in json.loads(body)['error']


def header_subset(path, max_traces=1000):
    """Python mirror of buildHeaderSubset() in static/app.js."""
    data = path.read_bytes()
    samples = int.from_bytes(data[3220:3222], 'big')
    trace_len = 240 + samples * 4  # IEEE float samples
    total = (len(data) - 3600) // trace_len
    picks = [i * (total - 1) // (max_traces - 1) for i in range(max_traces)]
    body = data[:3600] + b''.join(data[3600 + t * trace_len: 3600 + (t + 1) * trace_len]
                                  for t in picks)
    return body, total


def test_header_subset_matches_full_file(base_url, tmp_path, line_xy):
    from conftest import write_segy
    x = np.linspace(line_xy[0][0], line_xy[0][-1], 5000)
    y = np.linspace(line_xy[1][0], line_xy[1][-1], 5000)
    path = write_segy(tmp_path / 'big.sgy', 'GDA94, MGA ZONE 54', x, y)
    full = server._parse_and_classify(str(path), path.name)

    body, total = header_subset(path)
    assert total == 5000 and len(body) < path.stat().st_size / 4
    status, resp = request(f'{base_url}/api/upload?filename=big.sgy'
                           f'&total_traces={total}&file_size={path.stat().st_size}', body)
    assert status == 200
    r = json.loads(resp)

    assert r['trace_stats'] == {'total_traces': 5000, 'traces_sampled': 1000,
                                'header_subset': True}
    assert r['file_size_bytes'] == path.stat().st_size
    assert r['uploaded_bytes'] == len(body)
    assert r['classification'] == full['classification']
    assert r['coordinate_info']['source_crs']['epsg'] == 28354
    assert r['coordinate_info']['bbox'] == pytest.approx(full['coordinate_info']['bbox'])


@pytest.mark.parametrize('query', ['total_traces=abc', 'total_traces=0', 'file_size=-5',
                                   'total_traces=10'])
def test_header_subset_rejects_bad_params(base_url, mga54_segy, query):
    # mga54_segy has 200 traces, so total_traces=10 is inconsistent
    status, body = request(f'{base_url}/api/upload?filename=line.sgy&{query}',
                           mga54_segy.read_bytes())
    assert status == 400
    assert 'total_traces' in json.loads(body)['error'] or 'file_size' in json.loads(body)['error']


def test_static_files_cannot_escape_static_dir(base_url):
    status, _ = request(f'{base_url}/static/../server.py')
    assert status in (403, 404)
    status, _ = request(f'{base_url}/static/%2e%2e/server.py')
    assert status in (403, 404)


def test_index_is_served(base_url):
    status, body = request(f'{base_url}/')
    assert status == 200
    assert b'epsg-input' in body


# ── Bundled sample files ────────────────────────────────────────────────────

@pytest.mark.parametrize('name, epsg, confidence', [
    ('broken_hill_gda94_mga54.sgy', 28354, 'high'),
    ('png_gulf_utm55_no_datum.sgy', 20255, 'medium'),
    ('cooper_3d_gda2020_mga54.sgy', 7854, 'high'),
])
def test_bundled_samples(name, epsg, confidence):
    path = server.BASE_DIR / 'samples' / name
    r = server._parse_and_classify(str(path), name)
    crs = r['coordinate_info']['source_crs']
    assert (crs['epsg'], crs['confidence']) == (epsg, confidence)


def test_3d_sample_is_classified_as_3d():
    path = server.BASE_DIR / 'samples' / 'cooper_3d_gda2020_mga54.sgy'
    r = server._parse_and_classify(str(path), path.name)
    assert r['classification']['dimension'] == '3D'
    props = r['geojson']['features'][0]['properties']
    assert (props['inline'], props['xline']) == (1000, 2000)
