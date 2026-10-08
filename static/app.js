'use strict';

// ── Map init ──────────────────────────────────────────────────
const map = L.map('map', { zoomControl: true }).setView([20, 110], 3);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 18,
}).addTo(map);

let traceLayer = null;
let bboxLayer  = null;

// ── DOM refs ──────────────────────────────────────────────────
const uploadZone    = document.getElementById('upload-zone');
const fileInput     = document.getElementById('file-input');
const progressWrap  = document.getElementById('progress-wrap');
const progressBar   = document.getElementById('progress-bar');
const progressLabel = document.getElementById('progress-label');
const errorBox      = document.getElementById('error-box');
const resultPanel   = document.getElementById('result-panel');
const mapEmpty      = document.getElementById('map-empty');
const mapLegend     = document.getElementById('map-legend');

// ── Drag & drop ───────────────────────────────────────────────
uploadZone.addEventListener('dragover', e => {
  e.preventDefault();
  uploadZone.classList.add('drag-over');
});
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('drag-over'));
uploadZone.addEventListener('drop', e => {
  e.preventDefault();
  uploadZone.classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) uploadFile(file);
});
fileInput.addEventListener('change', e => {
  if (e.target.files[0]) uploadFile(e.target.files[0]);
  fileInput.value = '';
});

// ── Upload ────────────────────────────────────────────────────
function uploadFile(file) {
  const ext = file.name.split('.').pop().toLowerCase();
  if (!['sgy', 'segy'].includes(ext)) {
    showError(`Unsupported format: .${ext} — please upload a .sgy or .segy file`);
    return;
  }

  hideError();
  hideResult();
  showProgress(5, `Uploading: ${file.name}`);

  const region  = document.getElementById('region-select').value;
  const zoneVal = document.getElementById('zone-input').value.trim();
  const formData = new FormData();
  formData.append('file', file);
  formData.append('region', region);
  if (zoneVal) formData.append('zone', zoneVal);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');

  xhr.upload.onprogress = e => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 60);
      showProgress(pct, `Uploading... ${Math.round(e.loaded / e.total * 100)}%`);
    }
  };

  xhr.onload = () => {
    try {
      const data = JSON.parse(xhr.responseText);
      if (xhr.status === 200) {
        showProgress(100, 'Done');
        setTimeout(() => { hideProgress(); renderResult(data); }, 400);
      } else {
        hideProgress();
        showError(data.error || `Server error (${xhr.status})`);
      }
    } catch (e) {
      hideProgress();
      showError(`Failed to parse response: ${e.message}`);
    }
  };

  xhr.onerror   = () => { hideProgress(); showError('Network error — is the server running?'); };
  xhr.ontimeout = () => { hideProgress(); showError('Request timed out'); };
  xhr.timeout   = 300_000;
  xhr.send(formData);
}

// ── Render results ────────────────────────────────────────────
function renderResult(data) {
  showProgress(70, 'Rendering map...');

  const cls = data.classification;

  // Classification badges
  const badgesEl = document.getElementById('badges');
  badgesEl.innerHTML = '';
  appendBadge(badgesEl, cls.dimension,
    cls.dimension === '3D' ? 'badge-blue' : 'badge-green');
  appendBadge(badgesEl, cls.stack_type === 'prestack' ? 'Pre-stack' : 'Post-stack',
    cls.stack_type === 'prestack' ? 'badge-orange' : 'badge-teal');
  appendBadge(badgesEl, cls.domain === 'depth' ? 'Depth domain' : 'Time domain',
    cls.domain === 'depth' ? 'badge-purple' : 'badge-cyan');
  if (cls.survey_type !== 'unknown') {
    const surveyMap = { marine: 'Marine', land: 'Land', transition: 'Transition' };
    appendBadge(badgesEl, surveyMap[cls.survey_type] || cls.survey_type, 'badge-gray');
  }

  // File info
  const bh = data.binary_header;
  const ts = data.trace_stats;
  renderTable('file-table', [
    ['Filename',      data.filename],
    ['File size',     formatBytes(data.file_size_bytes)],
    ['Total traces',  ts.total_traces.toLocaleString()],
    ['Traces read',   `${ts.traces_sampled.toLocaleString()} (uniform sample)`],
    ['Sample interval', `${bh.sample_interval_us} μs`],
    ['Samples/trace', bh.samples_per_trace.toLocaleString()],
    ['Data format',   bh.data_format],
    ['SEG-Y revision', bh.segy_revision || 'Rev.0'],
    ['Parse time',    `${data.parse_time_ms} ms`],
  ]);

  // EBCDIC keywords
  const kw = data.ebcdic.keywords;
  const kwRows = Object.entries(kw).map(([k, v]) => [KW_LABELS[k] || k, v]);
  renderTable('ebcdic-table', kwRows.length > 0 ? kwRows : [['(No structured keywords found)', '']]);
  document.getElementById('ebcdic-raw').textContent = data.ebcdic.raw_text || '';

  // Show EBCDIC-detected zone
  const ebcdicZone = cls.utm_zone_from_ebcdic;
  const zoneHintEl = document.getElementById('zone-from-ebcdic');
  if (ebcdicZone) {
    document.getElementById('zone-ebcdic-val').textContent = `Zone ${ebcdicZone}`;
    zoneHintEl.style.display = 'inline';
    if (!document.getElementById('zone-input').value)
      document.getElementById('zone-input').value = ebcdicZone;
  } else {
    zoneHintEl.style.display = 'none';
  }

  // Coordinates
  const ci = data.coordinate_info;
  const bb = ci.bbox;
  const xy = ci.raw_xy || {};
  const coordRows = [
    ['Source field', ci.coord_source],
    ['Scalar',       ci.scalar],
    ['CRS',          ci.crs_guess + (ci.utm_zone ? ` (Zone ${ci.utm_zone})` : '')],
    ['WGS84',        ci.wgs84_converted ? 'Converted (approx.)' : 'Already geographic'],
    ['─── Raw X / Y ───', ''],
    ['X range',  xy.x_min != null ? `${fmtCoord(xy.x_min)} → ${fmtCoord(xy.x_max)}` : '—'],
    ['Y range',  xy.y_min != null ? `${fmtCoord(xy.y_min)} → ${fmtCoord(xy.y_max)}` : '—'],
    ['X start',  xy.x_first != null ? fmtCoord(xy.x_first) : '—'],
    ['Y start',  xy.y_first != null ? fmtCoord(xy.y_first) : '—'],
    ['X end',    xy.x_last  != null ? fmtCoord(xy.x_last)  : '—'],
    ['Y end',    xy.y_last  != null ? fmtCoord(xy.y_last)  : '—'],
    ['─── Geographic ───', ''],
    ['Longitude', bb.min_lon !== 0 ? `${bb.min_lon.toFixed(4)}° to ${bb.max_lon.toFixed(4)}°` : 'Invalid'],
    ['Latitude',  bb.min_lat !== 0 ? `${bb.min_lat.toFixed(4)}° to ${bb.max_lat.toFixed(4)}°` : 'Invalid'],
  ];
  renderTable('coord-table', coordRows);

  // Acquisition
  renderTable('acq-table', [
    ['Data type',   cls.data_type],
    ['Dimension',   cls.dimension],
    ['Stack',       cls.stack_type === 'prestack' ? 'Pre-stack' : 'Post-stack'],
    ['Domain',      cls.domain === 'depth' ? 'Depth' : 'Time'],
  ]);

  showResult();
  renderMap(data.geojson, ci.bbox);
  showProgress(100, 'Complete');
  setTimeout(hideProgress, 300);
}

// ── Map rendering ─────────────────────────────────────────────
function renderMap(geojson, bbox) {
  if (traceLayer) { map.removeLayer(traceLayer); traceLayer = null; }
  if (bboxLayer)  { map.removeLayer(bboxLayer);  bboxLayer  = null; }

  const points   = geojson.features.filter(f => f.geometry.type === 'Point');
  const polygons = geojson.features.filter(f => f.geometry.type === 'Polygon');

  if (points.length === 0 && polygons.length === 0) {
    mapEmpty.style.display = 'flex';
    mapEmpty.querySelector('.empty-text').textContent =
      'Coordinates are invalid or all zero — cannot display location';
    return;
  }

  if (points.length > 0) {
    traceLayer = L.geoJSON(
      { type: 'FeatureCollection', features: points },
      {
        pointToLayer: (feature, latlng) => {
          const m = L.circleMarker(latlng, {
            radius: 3, color: '#2563eb', fillColor: '#60a5fa',
            fillOpacity: 0.75, weight: 1,
          });
          const p = feature.properties;
          const [lon, lat] = feature.geometry.coordinates;
          let tip = p.inline
            ? `IL: ${p.inline} / XL: ${p.xline}<br>`
            : `Trace #${p.trace_idx}<br>`;
          tip += `Lat: ${lat.toFixed(5)}°  Lon: ${lon.toFixed(5)}°`;
          m.bindTooltip(tip, { permanent: false, direction: 'top' });
          return m;
        },
      }
    ).addTo(map);
  }

  if (polygons.length > 0) {
    bboxLayer = L.geoJSON(
      { type: 'FeatureCollection', features: polygons },
      { style: { color: '#dc2626', fillColor: '#fca5a5',
                 fillOpacity: 0.08, weight: 2, dashArray: '6 4' } }
    ).addTo(map);
  }

  if (bbox.min_lon !== 0 || bbox.max_lon !== 0) {
    const pad    = Math.max(0.5, (bbox.max_lon - bbox.min_lon) * 0.15);
    const padLat = Math.max(0.5, (bbox.max_lat - bbox.min_lat) * 0.15);
    map.fitBounds([
      [bbox.min_lat - padLat, bbox.min_lon - pad],
      [bbox.max_lat + padLat, bbox.max_lon + pad],
    ]);
  }

  mapEmpty.style.display = 'none';
  mapLegend.style.display = 'flex';
}

// ── Helpers ───────────────────────────────────────────────────
function showProgress(pct, label) {
  progressWrap.classList.add('visible');
  progressBar.style.width = `${pct}%`;
  progressLabel.textContent = label;
}
function hideProgress() {
  progressWrap.classList.remove('visible');
  progressBar.style.width = '0%';
}
function showError(msg) {
  errorBox.textContent = 'Error: ' + msg;
  errorBox.classList.add('visible');
}
function hideError()  { errorBox.classList.remove('visible'); }
function showResult() { resultPanel.classList.add('visible'); }
function hideResult() { resultPanel.classList.remove('visible'); }

function appendBadge(container, text, cls) {
  const span = document.createElement('span');
  span.className = `badge ${cls}`;
  span.textContent = text;
  container.appendChild(span);
}

function renderTable(id, rows) {
  document.getElementById(id).innerHTML = rows
    .filter(([, v]) => v !== undefined && v !== null)
    .map(([k, v]) => {
      if (k.startsWith('─')) {
        return `<tr><td colspan="2" class="key key-divider">${k}</td></tr>`;
      }
      if (v === '') return '';
      return `<tr><td class="key">${k}</td><td class="val">${v}</td></tr>`;
    })
    .join('');
}

function fmtCoord(v) {
  // Show up to 2 decimal places, with thousand separators
  return Number(v).toLocaleString('en-AU', { maximumFractionDigits: 2 });
}

function formatBytes(bytes) {
  if (!bytes) return 'Unknown';
  if (bytes < 1024)       return `${bytes} B`;
  if (bytes < 1024 ** 2)  return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3)  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

const KW_LABELS = {
  project:    'Survey name',
  company:    'Company / Client',
  area:       'Area / Block',
  date:       'Date',
  year_hints: 'Year (inferred)',
  line_name:  'Line no.',
  contractor: 'Acquired by',
};
