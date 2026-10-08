"""coord_utils.py - SEG-Y 坐标处理与 GeoJSON 生成"""
import numpy as np


# ── 各地区地理范围（用于自动匹配 UTM zone）─────────────────────
REGION_BOUNDS = {
    'australia': {'lat': (-45, -10), 'lon': (112, 155), 'zones': range(49, 57), 'south': True},
    'china':     {'lat': (18,  53),  'lon': (73,  135), 'zones': range(43, 52), 'south': False},
    'north_sea': {'lat': (50,  62),  'lon': (-5,  12),  'zones': range(28, 33), 'south': False},
    'north_america': {'lat': (24, 70), 'lon': (-170, -50), 'zones': list(range(1, 23)) + list(range(54, 61)), 'south': False},
}


def apply_scalar_array(values: np.ndarray, scalars: np.ndarray) -> np.ndarray:
    """
    SEG-Y Rev1 坐标标量应用（向量化）：
      scalar < 0 → 除以 abs(scalar)
      scalar > 0 → 乘以 scalar
      scalar == 0 → 保持原值（视为 1）
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


def guess_crs_and_convert(x: np.ndarray, y: np.ndarray,
                           coord_units: np.ndarray,
                           region_hint: str = 'auto',
                           zone_hint: int = None) -> dict:
    """
    识别坐标系并转换为 WGS84 经纬度。

    region_hint 可选值：
      'auto'        - 自动检测
      'australia'   - 澳大利亚 GDA94/MGA（南半球 UTM zone 49-56）
      'china'       - 中国 CGCS2000/CGS2000（zone 43-51）
      'north_sea'   - 北海/欧洲（zone 28-32）
      'north_america' - 北美洲（zone 1-22）
      'geographic'  - 已是十进制经纬度
    """
    valid_mask = (x != 0) | (y != 0)
    x_valid = x[valid_mask]
    y_valid = y[valid_mask]

    if len(x_valid) == 0:
        return {'crs': 'unknown', 'converted': False,
                'lon': x.astype(float), 'lat': y.astype(float)}

    x_med = float(np.median(x_valid))
    y_med = float(np.median(y_valid))

    # 用户明确指定地理坐标
    if region_hint == 'geographic':
        lon = np.clip(x.astype(float), -180, 180)
        lat = np.clip(y.astype(float), -90, 90)
        return {'crs': 'WGS84_geographic', 'converted': False, 'lon': lon, 'lat': lat}

    # 检查 CoordinateUnits（字节 89）众数
    units_valid = coord_units[coord_units > 0]
    dominant_unit = 0
    if len(units_valid) > 0:
        counts = np.bincount(units_valid.astype(int))
        if len(counts) > 0:
            dominant_unit = int(counts.argmax())

    # 已是十进制度（CoordinateUnits=4 或值域在经纬度范围内）
    already_geo = (dominant_unit == 4) or (-180 <= x_med <= 180 and -90 <= y_med <= 90)
    if already_geo and region_hint == 'auto':
        lon = np.clip(x.astype(float), -180, 180)
        lat = np.clip(y.astype(float), -90, 90)
        return {'crs': 'WGS84_geographic', 'converted': False, 'lon': lon, 'lat': lat}

    # 有明确 zone 号（来自 EBCDIC 或用户手填）→ 直接用，不搜索
    if zone_hint and 1 <= zone_hint <= 60:
        south = (region_hint == 'auto' and _is_likely_south(y.astype(float))) or \
                (region_hint in REGION_BOUNDS and REGION_BOUNDS[region_hint]['south'])
        lon_arr, lat_arr = _convert_utm_zone(x.astype(float), y.astype(float), zone_hint, south)
        hemisphere = 'S' if south else 'N'
        return {
            'crs': f'UTM_Zone{zone_hint}{hemisphere}_from_ebcdic',
            'converted': True,
            'lon': lon_arr, 'lat': lat_arr,
            'utm_zone': zone_hint,
        }

    # 用户指定区域 → 直接用对应区域的参数
    if region_hint in REGION_BOUNDS:
        bounds = REGION_BOUNDS[region_hint]
        lon_arr, lat_arr, zone, crs_name = _utm_to_wgs84_with_zone_search(
            x.astype(float), y.astype(float),
            lat_bounds=bounds['lat'],
            lon_bounds=bounds['lon'],
            zones=bounds['zones'],
            south=bounds['south'],
        )
        if lon_arr is not None:
            return {'crs': crs_name, 'converted': True,
                    'lon': lon_arr, 'lat': lat_arr, 'utm_zone': zone}
        # 找不到匹配 zone，仍按该区域参数强制转换第一个 zone
        zone0 = list(bounds['zones'])[0]
        lon_arr, lat_arr = _convert_utm_zone(
            x.astype(float), y.astype(float), zone0, south=bounds['south'])
        return {'crs': f'{region_hint.upper()}_zone{zone0}', 'converted': True,
                'lon': lon_arr, 'lat': lat_arr, 'utm_zone': zone0}

    # 自动检测：依次尝试各区域
    for region, bounds in REGION_BOUNDS.items():
        lon_arr, lat_arr, zone, crs_name = _utm_to_wgs84_with_zone_search(
            x.astype(float), y.astype(float),
            lat_bounds=bounds['lat'],
            lon_bounds=bounds['lon'],
            zones=bounds['zones'],
            south=bounds['south'],
        )
        if lon_arr is not None:
            return {'crs': crs_name, 'converted': True,
                    'lon': lon_arr, 'lat': lat_arr, 'utm_zone': zone,
                    'auto_region': region}

    # 兜底：用旧的简单算法（可能不准）
    lon_arr, lat_arr = _legacy_approx_utm(x.astype(float), y.astype(float))
    return {'crs': 'UTM_approx_unknown_zone', 'converted': True,
            'lon': lon_arr, 'lat': lat_arr}


def _is_likely_south(northing: np.ndarray) -> bool:
    """粗略判断是否南半球（northing 在 1M-9M 且减去 10M 后得到合理南纬）"""
    n_valid = northing[northing != 0]
    if len(n_valid) == 0:
        return False
    n_med = float(np.median(n_valid))
    lat_if_south = (n_med - 10_000_000) / 110574.0
    return 1_000_000 <= n_med <= 9_500_000 and -60 <= lat_if_south <= 0


def _utm_to_wgs84_with_zone_search(easting, northing,
                                    lat_bounds, lon_bounds,
                                    zones, south) -> tuple:
    """
    尝试给定范围内的每个 UTM zone，返回第一个使坐标落在目标区域内的结果。
    返回 (lon_arr, lat_arr, zone, crs_name) 或 (None, None, None, None)
    """
    e_nonzero = easting[easting != 0]
    n_nonzero = northing[northing != 0]
    if len(e_nonzero) == 0 or len(n_nonzero) == 0:
        return None, None, None, None

    for zone in zones:
        lon_arr, lat_arr = _convert_utm_zone(easting, northing, zone, south)
        lat_med = float(np.median(lat_arr[lat_arr != 0])) if np.any(lat_arr != 0) else 9999
        lon_med = float(np.median(lon_arr[lon_arr != 0])) if np.any(lon_arr != 0) else 9999

        if (lat_bounds[0] <= lat_med <= lat_bounds[1] and
                lon_bounds[0] <= lon_med <= lon_bounds[1]):
            hemisphere = 'S' if south else 'N'
            crs_name = f'UTM_Zone{zone}{hemisphere}_approx'
            return lon_arr, lat_arr, zone, crs_name

    return None, None, None, None


def _convert_utm_zone(easting: np.ndarray, northing: np.ndarray,
                       zone: int, south: bool) -> tuple:
    """
    用指定 UTM zone 和半球将投影坐标转为近似经纬度。
    精度约 ±1–5 km（够地图显示用）。
    """
    n = northing.astype(np.float64)

    # 纬度计算
    if south:
        # 南半球 UTM：false northing = 10,000,000 m
        lat_arr = (n - 10_000_000.0) / 110574.0
    else:
        lat_arr = n / 110574.0

    lat_arr = np.clip(lat_arr, -90, 90)

    # 中央子午线
    central_meridian = zone * 6 - 183

    # 每度经度对应米数（按纬度修正）
    lat_med = float(np.median(lat_arr))
    cos_lat = abs(np.cos(np.radians(lat_med)))
    if cos_lat < 1e-6:
        cos_lat = 1e-6
    meters_per_deg_lon = 111320.0 * cos_lat

    lon_arr = central_meridian + (easting.astype(np.float64) - 500_000.0) / meters_per_deg_lon
    lon_arr = np.clip(lon_arr, -180, 180)

    return lon_arr, lat_arr


def _legacy_approx_utm(easting: np.ndarray, northing: np.ndarray) -> tuple:
    """兜底：旧的简单近似算法（不知道区域时使用）"""
    n = northing.astype(np.float64)

    # 尝试判断南半球（值在 5M-9M 且减去 10M 后在合理纬度范围内）
    n_med = float(np.median(n[n != 0])) if np.any(n != 0) else 0
    if 1_000_000 <= n_med <= 9_000_000:
        lat_south = (n_med - 10_000_000) / 110574.0
        lat_north = n_med / 110574.0
        if -60 <= lat_south <= 0:
            lat_arr = (n - 10_000_000) / 110574.0
        else:
            lat_arr = n / 110574.0
    else:
        lat_arr = n / 110574.0

    lat_arr = np.clip(lat_arr, -90, 90)

    # 用纬度中值估算每度米数
    lat_med = float(np.median(lat_arr))
    cos_lat = abs(np.cos(np.radians(lat_med)))
    if cos_lat < 1e-6:
        cos_lat = 1e-6
    meters_per_deg_lon = 111320.0 * cos_lat

    # zone 无法确定，取 zone 31 中央子午线 3°E 作为默认（欧洲附近常见）
    e_med = float(np.median(easting[easting != 0])) if np.any(easting != 0) else 500000.0
    # 粗略：中央子午线未知，只算相对偏移
    lon_arr = (easting.astype(np.float64) - 500_000.0) / meters_per_deg_lon
    lon_arr = np.clip(lon_arr, -180, 180)

    return lon_arr, lat_arr


def build_geojson(lon: np.ndarray, lat: np.ndarray,
                  ilines: np.ndarray, xlines: np.ndarray,
                  max_points: int = 500) -> dict:
    """
    生成 GeoJSON FeatureCollection：
    - 均匀抽样点（最多 max_points 个）
    - 测区包络框 Polygon
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
