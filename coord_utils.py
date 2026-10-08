"""coord_utils.py - SEG-Y coordinate handling and GeoJSON generation"""
import numpy as np


def apply_scalar_array(values: np.ndarray, scalars: np.ndarray) -> np.ndarray:
    """
    Apply the SEG-Y Rev1 coordinate scalar (vectorised):
      scalar < 0 → divide by abs(scalar)
      scalar > 0 → multiply by scalar
      scalar == 0 → unchanged (treated as 1)
    """
    result = np.zeros(len(values), dtype=np.float64)
    v = values.astype(np.float64)
    s = scalars.astype(np.float64)

    neg  = s < 0
    pos  = s > 0
    zero = s == 0

    result[neg]  = v[neg]  / np.abs(s[neg])
    result[pos]  = v[pos]  * s[pos]
    result[zero] = v[zero]

    return result


def to_decimal_degrees(values: np.ndarray, coord_unit: int) -> np.ndarray:
    """
    Convert per SEG-Y Rev1 byte 89 (CoordinateUnits) to decimal degrees:
      2 = seconds of arc → / 3600
      3 = decimal degrees → unchanged
      4 = DMS             → split ±DDDMMSS.ss
    Other units (1 = metres/feet) are returned unchanged.
    """
    v = values.astype(np.float64)
    if coord_unit == 2:
        return v / 3600.0
    if coord_unit == 4:
        sign = np.sign(v)
        a = np.abs(v)
        deg = np.floor(a / 10000)
        minutes = np.floor((a - deg * 10000) / 100)
        seconds = a - deg * 10000 - minutes * 100
        return sign * (deg + minutes / 60.0 + seconds / 3600.0)
    return v


def dominant_value(arr: np.ndarray, default: int = 0) -> int:
    """Mode of the non-zero values."""
    a = arr[arr != 0]
    if len(a) == 0:
        return default
    vals, counts = np.unique(a, return_counts=True)
    return int(vals[counts.argmax()])


def build_geojson(lon: np.ndarray, lat: np.ndarray,
                  ilines: np.ndarray, xlines: np.ndarray,
                  max_points: int = 500) -> dict:
    """
    Build a GeoJSON FeatureCollection:
    - uniformly sampled trace points (at most max_points)
    - survey bounding-box Polygon
    """
    valid = (
        (lon != 0) & (lat != 0) &
        np.isfinite(lon) & np.isfinite(lat) &
        (lon >= -180) & (lon <= 180) &
        (lat >= -90)  & (lat <= 90)
    )
    lon_v = lon[valid]
    lat_v = lat[valid]
    il_v  = ilines[valid] if len(ilines) == len(lon) else np.zeros(valid.sum(), dtype=np.int32)
    xl_v  = xlines[valid] if len(xlines) == len(lon) else np.zeros(valid.sum(), dtype=np.int32)

    if len(lon_v) == 0:
        return {"type": "FeatureCollection", "features": []}

    features = []

    n = len(lon_v)
    step = max(1, n // max_points)
    for i in range(0, n, step):
        props = {"trace_idx": int(i)}
        if len(il_v) > i and int(il_v[i]) != 0:
            props["inline"] = int(il_v[i])
            props["xline"]  = int(xl_v[i])
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [round(float(lon_v[i]), 6), round(float(lat_v[i]), 6)],
            },
            "properties": props,
        })

    min_lon, max_lon = float(lon_v.min()), float(lon_v.max())
    min_lat, max_lat = float(lat_v.min()), float(lat_v.max())
    pad_lon = max(0.001, (max_lon - min_lon) * 0.02)
    pad_lat = max(0.001, (max_lat - min_lat) * 0.02)

    features.append({
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[
            [min_lon - pad_lon, min_lat - pad_lat],
            [max_lon + pad_lon, min_lat - pad_lat],
            [max_lon + pad_lon, max_lat + pad_lat],
            [min_lon - pad_lon, max_lat + pad_lat],
            [min_lon - pad_lon, min_lat - pad_lat],
        ]]},
        "properties": {
            "type": "survey_bbox", "label": "Survey Area",
            "min_lon": round(min_lon, 4), "max_lon": round(max_lon, 4),
            "min_lat": round(min_lat, 4), "max_lat": round(max_lat, 4),
        },
    })

    return {"type": "FeatureCollection", "features": features}


def compute_bbox(lon: np.ndarray, lat: np.ndarray) -> dict:
    valid = (
        (lon != 0) & (lat != 0) &
        np.isfinite(lon) & np.isfinite(lat) &
        (lon >= -180) & (lon <= 180) &
        (lat >= -90)  & (lat <= 90)
    )
    if not np.any(valid):
        return {"min_lon": 0, "max_lon": 0, "min_lat": 0, "max_lat": 0}
    return {
        "min_lon": round(float(lon[valid].min()), 6),
        "max_lon": round(float(lon[valid].max()), 6),
        "min_lat": round(float(lat[valid].min()), 6),
        "max_lat": round(float(lat[valid].max()), 6),
    }
