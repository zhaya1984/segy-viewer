"""crs_detect.py - Infer the source CRS (EPSG) from SEG-Y headers

SEG-Y files almost never state an EPSG code, so it is inferred from:
  1. A user-supplied EPSG code (highest priority)
  2. Trace header byte 89 CoordinateUnits (2=arc-seconds, 3=degrees, 4=DMS → geographic)
  3. An explicit "EPSG:xxxx" in the EBCDIC header
  4. Datum / projection / zone in the EBCDIC header (e.g. "GDA94, MGA ZONE 54")
  5. Acquisition year (rules out impossible datums, e.g. 2005 data cannot be GDA2020)
  6. Region (Australia / PNG), chosen by the user or inferred from position, to fill in a missing datum
Each candidate is validated with pyproj's area_of_use: transformed coordinates must fall inside the CRS's area of use.

Coverage: Australia and Papua New Guinea (PNG) only, both southern hemisphere.
"""
import re
from functools import lru_cache

import numpy as np
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError


# ── Projected CRS families: name → {zone: EPSG} (all southern hemisphere) ──
PROJECTED_FAMILIES = {
    'GDA2020_MGA': {z: 7800 + z  for z in range(46, 60)},
    'GDA94_MGA':   {z: 28300 + z for z in range(48, 59)},
    'AGD84_AMG':   {z: 20300 + z for z in range(48, 59)},
    'AGD66_AMG':   {z: 20200 + z for z in range(48, 59)},   # zones 54–56 also cover PNG
    'PNG94_PNGMG': {54: 5550, 55: 5551, 56: 5552, 57: 9874},
    'WGS84_UTM':   {z: 32700 + z for z in range(46, 60)},   # UTM south
}

# Datum → geographic CRS EPSG
GEOGRAPHIC_BY_DATUM = {
    'GDA2020': 7844, 'GDA94': 4283, 'AGD84': 4203, 'AGD66': 4202,
    'PNG94': 5546, 'WGS84': 4326,
}

# Datum patterns (all matches are collected)
DATUM_PATTERNS = {
    'GDA2020': r'GDA\s*-?\s*2020',
    'GDA94':   r'GDA\s*-?\s*(?:19)?94\b',
    'AGD84':   r'AGD\s*-?\s*(?:19)?84\b|AMG\s*-?\s*84\b',
    'AGD66':   r'AGD\s*-?\s*(?:19)?66\b|AMG\s*-?\s*66\b',
    'PNG94':   r'PNG\s*-?\s*94\b|PNGMG',
    'WGS84':   r'WGS\s*-?\s*(?:19)?84\b',
}

# Region → CRS families to try when no datum is stated (first is the default)
REGION_PREFS = {
    'australia': ['GDA94_MGA', 'GDA2020_MGA', 'WGS84_UTM'],
    'png':       ['AGD66_AMG', 'WGS84_UTM', 'PNG94_PNGMG'],
}
REGION_LABELS = {'australia': 'Australia', 'png': 'PNG'}

# Auto region: latitudes north of this are treated as PNG, otherwise Australia
PNG_LAT_THRESHOLD = -10.5

GEOGRAPHIC_UNITS = {2: 'seconds of arc', 3: 'decimal degrees', 4: 'DMS'}


@lru_cache(maxsize=None)
def crs_from_epsg(epsg: int):
    """Return a pyproj CRS, or None if the EPSG code does not exist."""
    try:
        return CRS.from_epsg(int(epsg))
    except (CRSError, ValueError, TypeError):
        return None


@lru_cache(maxsize=None)
def _transformer_to_wgs84(epsg: int) -> Transformer:
    return Transformer.from_crs(crs_from_epsg(epsg), CRS.from_epsg(4326), always_xy=True)


def transform_to_wgs84(x: np.ndarray, y: np.ndarray, epsg: int) -> tuple:
    """Rigorous transform to WGS84 lon/lat. Invalid (0, 0) points become NaN."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    lon, lat = _transformer_to_wgs84(int(epsg)).transform(x, y)
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    invalid = (x == 0) & (y == 0)
    lon[invalid] = np.nan
    lat[invalid] = np.nan
    return lon, lat


# ── EBCDIC parsing ──────────────────────────────────────────

def parse_crs_hints(ebcdic_text: str) -> dict:
    """Extract CRS clues from the EBCDIC text."""
    text = re.sub(r'\s+', ' ', (ebcdic_text or '').upper())

    hints = {'epsg': None, 'datums': [], 'projection': None,
             'zone': None, 'hemisphere': None, 'year': None, 'snippets': []}

    m = re.search(r'EPSG\s*[:#]?\s*(\d{4,5})\b', text)
    if m:
        hints['epsg'] = int(m.group(1))
        hints['snippets'].append(m.group(0))

    for datum, pattern in DATUM_PATTERNS.items():
        m = re.search(pattern, text)
        if m:
            hints['datums'].append(datum)
            hints['snippets'].append(m.group(0))

    for proj in ('PNGMG', 'MGA', 'AMG', 'UTM'):
        if re.search(rf'\b{proj}', text):
            hints['projection'] = proj
            break

    zone_patterns = [
        r'\b(?:PNGMG\s*(?:94)?|MGA|AMG|UTM)\s*(?:ZONE)?\s*[-:]?\s*(\d{1,2})\s*(N(?:ORTH)?|S(?:OUTH)?)?\b',
        r'\bZONE\s*[-:]?\s*(\d{1,2})\s*(N(?:ORTH)?|S(?:OUTH)?)?\b',
    ]
    for pattern in zone_patterns:
        m = re.search(pattern, text)
        if m and 1 <= int(m.group(1)) <= 60:
            hints['zone'] = int(m.group(1))
            if m.group(2):
                hints['hemisphere'] = m.group(2)[0]
            hints['snippets'].append(m.group(0).strip())
            break

    years = [int(y) for y in re.findall(r'\b(19[5-9]\d|20[0-4]\d)\b', text)]
    if years:
        hints['year'] = max(years)

    return hints


# ── Entry point ─────────────────────────────────────────────

def detect_crs(ebcdic_text: str, x: np.ndarray, y: np.ndarray,
               coord_unit: int = 0, region_hint: str = 'auto',
               epsg_override: int = None) -> dict:
    """
    Infer the source CRS. x / y are already scaled; geographic values are in decimal degrees.

    Returns:
      epsg, name        chosen EPSG and name (None if undetermined)
      confidence        user / high / medium / low / none
      evidence          reasons for the decision (list of strings)
      alternatives      other candidates [{epsg, name, in_area}]
      in_area           whether the result falls inside the chosen CRS's area of use
    """
    evidence = []
    hints = parse_crs_hints(ebcdic_text)

    # 1. User override
    if epsg_override:
        if crs_from_epsg(epsg_override) is None:
            raise ValueError(f'Unknown EPSG code: {epsg_override}')
        evidence.append(f'User override: EPSG:{epsg_override}')
        return _finalize([epsg_override], 'user', evidence, x, y)

    # 2. Geographic coordinates (trace header byte 89 or region hint)
    is_geo_unit = coord_unit in GEOGRAPHIC_UNITS
    if is_geo_unit or region_hint == 'geographic':
        if is_geo_unit:
            evidence.append(f'Trace header byte 89 = {coord_unit} ({GEOGRAPHIC_UNITS[coord_unit]})')
        else:
            evidence.append('Region hint: geographic')
        return _geographic_result(hints, evidence, x, y)

    # 3. Explicit EPSG in EBCDIC
    if hints['epsg'] and crs_from_epsg(hints['epsg']) is not None:
        evidence.append(f'EBCDIC: "{hints["snippets"][0]}"')
        return _finalize([hints['epsg']], 'high', evidence, x, y)

    # 4. Values look like lon/lat but no unit is declared
    x_med, y_med = _median_nonzero(x), _median_nonzero(y)
    if x_med is not None and abs(x_med) <= 180 and abs(y_med) <= 90 and not hints['zone']:
        evidence.append('Coordinate values fall within lon/lat range (byte 89 does not declare degrees)')
        result = _geographic_result(hints, evidence, x, y)
        result['confidence'] = 'low'
        return result

    # 5. Projected coordinates: candidates from EBCDIC clues + region
    candidates, confidence = _projected_candidates(hints, region_hint, x, y, evidence)
    if not candidates:
        evidence.append('No zone information found in EBCDIC — please specify EPSG manually')
        return _empty_result(evidence)

    return _finalize(candidates, confidence, evidence, x, y)


def _projected_candidates(hints: dict, region_hint: str, x: np.ndarray, y: np.ndarray,
                          evidence: list) -> tuple:
    zone, proj, datums = hints['zone'], hints['projection'], hints['datums']
    if hints['snippets']:
        evidence.append('EBCDIC: ' + ', '.join(f'"{s}"' for s in hints['snippets']))
    if not zone:
        return [], 'none'
    # Choose the CRS family
    confidence = 'high'
    if hints['hemisphere'] == 'N':
        evidence.append('EBCDIC states northern hemisphere, but Australia/PNG use '
                        'southern-hemisphere grids — ignored')
        confidence = 'medium'
    if proj == 'PNGMG' or 'PNG94' in datums:
        families = ['PNG94_PNGMG']
    elif proj == 'MGA' or 'GDA94' in datums or 'GDA2020' in datums:
        if 'GDA2020' in datums:
            families = ['GDA2020_MGA']
        elif 'GDA94' in datums:
            families = ['GDA94_MGA']
        elif hints['year'] and hints['year'] < 2017:
            families = ['GDA94_MGA']
            evidence.append(f'Datum not stated; latest year in EBCDIC is {hints["year"]} '
                            f'(before GDA2020) → GDA94')
        else:
            families = ['GDA2020_MGA', 'GDA94_MGA'] if (hints['year'] or 0) >= 2020 \
                else ['GDA94_MGA', 'GDA2020_MGA']
            confidence = 'medium'
            evidence.append('MGA without datum: GDA94 vs GDA2020 differ by ~1.8 m')
    elif proj == 'AMG' or 'AGD84' in datums or 'AGD66' in datums:
        if 'AGD84' in datums:
            families = ['AGD84_AMG']
        elif 'AGD66' in datums:
            families = ['AGD66_AMG']
        else:
            families = ['AGD66_AMG', 'AGD84_AMG']
            confidence = 'medium'
            evidence.append('AMG without datum: assuming AGD66, AGD84 as alternative')
    elif 'WGS84' in datums:
        families = ['WGS84_UTM']
    else:
        # Zone but no datum → use the common families for the region
        region = region_hint if region_hint in REGION_PREFS else _infer_region(zone, x, y, evidence)
        families = list(REGION_PREFS[region])
        evidence.append(f'Datum not stated; using {REGION_LABELS[region]} defaults')
        confidence = 'medium'

    candidates = []
    for fam in families:
        code = PROJECTED_FAMILIES[fam].get(zone)
        if code is None:
            evidence.append(f'Zone {zone} is not valid for {fam}')
        elif crs_from_epsg(code) is not None and code not in candidates:
            candidates.append(code)

    return candidates, confidence


def _infer_region(zone: int, x: np.ndarray, y: np.ndarray, evidence: list) -> str:
    """Estimate latitude via WGS84 UTM to decide between PNG and Australia."""
    code = PROJECTED_FAMILIES['WGS84_UTM'].get(zone)
    x_med, y_med = _median_nonzero(x), _median_nonzero(y)
    if code is None or x_med is None:
        return 'australia'
    _, lat = transform_to_wgs84(np.array([x_med]), np.array([y_med]), code)
    region = 'png' if float(lat[0]) > PNG_LAT_THRESHOLD else 'australia'
    evidence.append(f'Region inferred from position (~{float(lat[0]):.1f}°): {REGION_LABELS[region]}')
    return region


def _geographic_result(hints: dict, evidence: list, x, y) -> dict:
    datums = [d for d in hints['datums'] if d in GEOGRAPHIC_BY_DATUM]
    if datums:
        evidence.append(f'Datum from EBCDIC: {datums[0]}')
        return _finalize([GEOGRAPHIC_BY_DATUM[d] for d in datums], 'high', evidence, x, y)
    evidence.append('Datum not stated; assuming WGS84')
    return _finalize([4326], 'medium', evidence, x, y)


def _finalize(candidates: list, confidence: str, evidence: list, x, y) -> dict:
    """Validate each candidate against its area of use and pick the final EPSG."""
    checked = [{'epsg': c, 'name': crs_from_epsg(c).name, 'in_area': _in_area_of_use(c, x, y)}
               for c in candidates]

    chosen = checked[0]
    if chosen['in_area'] is False:
        passing = [c for c in checked if c['in_area']]
        if passing and confidence != 'user':
            evidence.append(f'EPSG:{chosen["epsg"]} fails area-of-use check; '
                            f'using EPSG:{passing[0]["epsg"]}')
            chosen = passing[0]
            confidence = 'low' if confidence == 'high' else confidence
        else:
            evidence.append(f'Warning: coordinates fall outside the area of use of EPSG:{chosen["epsg"]}')
            if confidence != 'user':
                confidence = 'low'

    # More than one candidate passes → ambiguous
    others_passing = [c for c in checked if c is not chosen and c['in_area']]
    if others_passing and confidence == 'high':
        confidence = 'medium'

    return {
        'epsg': chosen['epsg'],
        'name': chosen['name'],
        'confidence': confidence,
        'in_area': chosen['in_area'],
        'evidence': evidence,
        'alternatives': [c for c in checked if c is not chosen],
    }


def _empty_result(evidence: list) -> dict:
    return {'epsg': None, 'name': None, 'confidence': 'none', 'in_area': None,
            'evidence': evidence, 'alternatives': []}


def _in_area_of_use(epsg: int, x, y, tolerance_deg: float = 0.5):
    """Transform the median point to WGS84 and check it is inside the area of use (±0.5°)."""
    crs = crs_from_epsg(epsg)
    x_med, y_med = _median_nonzero(x), _median_nonzero(y)
    if crs is None or crs.area_of_use is None or x_med is None:
        return None
    try:
        lon, lat = transform_to_wgs84(np.array([x_med]), np.array([y_med]), epsg)
    except Exception:
        return False
    lon, lat = float(lon[0]), float(lat[0])
    if not (np.isfinite(lon) and np.isfinite(lat)):
        return False
    w, s, e, n = crs.area_of_use.bounds
    return (w - tolerance_deg <= lon <= e + tolerance_deg and
            s - tolerance_deg <= lat <= n + tolerance_deg)


def _median_nonzero(arr, default=None):
    a = np.asarray(arr, dtype=np.float64)
    a = a[(a != 0) & np.isfinite(a)]
    return float(np.median(a)) if len(a) else default
