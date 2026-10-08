import numpy as np
import pytest

from coord_utils import apply_scalar_array, dominant_value, to_decimal_degrees
from crs_detect import transform_to_wgs84


def test_apply_scalar_array():
    values = np.array([12345, 12345, 12345])
    scalars = np.array([-100, 10, 0])
    np.testing.assert_allclose(apply_scalar_array(values, scalars), [123.45, 123450, 12345])


def test_arc_seconds_to_degrees():
    np.testing.assert_allclose(to_decimal_degrees(np.array([509400.0]), 2), [141.5])


def test_dms_to_degrees():
    # 141°32'00.5" E and 31°54'00" S
    out = to_decimal_degrees(np.array([1413200.5, -315400.0]), 4)
    np.testing.assert_allclose(out, [141 + 32 / 60 + 0.5 / 3600, -(31 + 54 / 60)])


def test_metres_unchanged():
    v = np.array([550935.0])
    np.testing.assert_array_equal(to_decimal_degrees(v, 1), v)


def test_dominant_value_ignores_zero_and_handles_negatives():
    assert dominant_value(np.array([0, 0, 0, -100, -100, 10])) == -100
    assert dominant_value(np.array([0, 0])) == 0


def test_transform_matches_published_reference():
    # GDA94 / MGA 54 → lon/lat. The GDA94→WGS84 step is a null/ballpark transform,
    # so this reflects the GDA94 position to well under a metre.
    lon, lat = transform_to_wgs84(np.array([550935.0]), np.array([6465436.0]), 28354)
    assert lon[0] == pytest.approx(141.53893, abs=1e-5)
    assert lat[0] == pytest.approx(-31.94588, abs=1e-5)


def test_transform_marks_zero_points_invalid():
    lon, lat = transform_to_wgs84(np.array([0.0, 550935.0]), np.array([0.0, 6465436.0]), 28354)
    assert np.isnan(lon[0]) and np.isnan(lat[0])
    assert np.isfinite(lon[1])
