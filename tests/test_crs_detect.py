import numpy as np
import pytest

from crs_detect import PROJECTED_FAMILIES, crs_from_epsg, detect_crs, parse_crs_hints

# Representative positions (projected metres)
BROKEN_HILL_Z54 = (550935.0, 6465436.0)   # NSW, Australia, ~-31.9°
PNG_KUTUBU_Z54 = (632000.0, 9292000.0)    # Southern Highlands, PNG, ~-6.4°
PNG_Z55 = (410000.0, 9300000.0)           # PNG, ~-6.3°


def detect(text, xy, **kwargs):
    x = np.full(5, xy[0])
    y = np.full(5, xy[1])
    return detect_crs(text, x, y, kwargs.pop('coord_unit', 1), **kwargs)


def test_every_family_epsg_exists_in_proj_database():
    missing = [code for fam in PROJECTED_FAMILIES.values() for code in fam.values()
               if crs_from_epsg(code) is None]
    assert missing == []


@pytest.mark.parametrize('text, zone, datums, projection', [
    ('NOTE: X & Y ARE REAL COORDINATES. GDA94, MGA ZONE 54', 54, ['GDA94'], 'MGA'),
    ('GDA2020 / MGA54', 54, ['GDA2020'], 'MGA'),
    ('AGD66 AMG ZONE 55', 55, ['AGD66'], 'AMG'),
    ('DATUM PNG94 PNGMG94 ZONE 54', 54, ['PNG94'], 'PNGMG'),
    ('WGS84 UTM ZONE 56S', 56, ['WGS84'], 'UTM'),
])
def test_parse_crs_hints(text, zone, datums, projection):
    hints = parse_crs_hints(text)
    assert hints['zone'] == zone
    assert hints['datums'] == datums
    assert hints['projection'] == projection


@pytest.mark.parametrize('text, xy, epsg', [
    ('GDA94, MGA ZONE 54', BROKEN_HILL_Z54, 28354),
    ('GDA2020 MGA ZONE 54', BROKEN_HILL_Z54, 7854),
    ('AGD66 AMG ZONE 54', BROKEN_HILL_Z54, 20254),
    ('AGD84 AMG ZONE 54', BROKEN_HILL_Z54, 20354),
    ('CRS EPSG:28354', BROKEN_HILL_Z54, 28354),
    ('DATUM PNG94 PNGMG94 ZONE 54', PNG_KUTUBU_Z54, 5550),
    ('AGD66 AMG ZONE 55', PNG_Z55, 20255),
    ('WGS84 UTM ZONE 55S', PNG_Z55, 32755),
])
def test_explicit_header_gives_high_confidence(text, xy, epsg):
    r = detect(text, xy)
    assert r['epsg'] == epsg
    assert r['confidence'] == 'high'
    assert r['in_area'] is True


def test_mga_without_datum_uses_year_to_rule_out_gda2020():
    r = detect('DATE RECORDED AUG 2005. MGA ZONE 54', BROKEN_HILL_Z54)
    assert r['epsg'] == 28354
    assert r['confidence'] == 'high'


def test_mga_without_datum_or_year_is_ambiguous():
    r = detect('MGA54', BROKEN_HILL_Z54)
    assert r['epsg'] == 28354
    assert r['confidence'] == 'medium'
    assert 7854 in [a['epsg'] for a in r['alternatives']]


def test_zone_only_infers_australia_from_position():
    r = detect('UTM ZONE 54', BROKEN_HILL_Z54)
    assert r['epsg'] == 28354
    assert r['confidence'] == 'medium'
    assert any('Australia' in e for e in r['evidence'])


def test_zone_only_infers_png_from_position():
    r = detect('UTM ZONE 55', PNG_Z55)
    assert r['epsg'] == 20255
    assert {a['epsg'] for a in r['alternatives']} == {32755, 5551}
    assert any('PNG' in e for e in r['evidence'])


def test_region_hint_overrides_position_inference():
    # Position says Australia, but the user chose PNG → PNG defaults (AGD66 also covers Australia)
    r = detect('ZONE 54', BROKEN_HILL_Z54, region_hint='png')
    assert r['epsg'] == 20254


def test_wrong_region_hint_is_rescued_by_area_of_use():
    # User chose Australia but the data is in PNG: GDA94 MGA fails its area of use,
    # so the next Australian default that fits (WGS84 UTM 55S) is used instead
    r = detect('ZONE 55', PNG_Z55, region_hint='australia')
    assert r['epsg'] == 32755
    assert any('fails area-of-use check' in e for e in r['evidence'])


def test_explicit_header_wins_over_region_hint():
    r = detect('GDA94, MGA ZONE 54', BROKEN_HILL_Z54, region_hint='png')
    assert r['epsg'] == 28354


def test_crs_outside_area_of_use_is_flagged():
    # PNG coordinates but the header claims GDA94 / MGA (Australia only)
    r = detect('GDA94 MGA ZONE 55', PNG_Z55)
    assert r['in_area'] is False
    assert r['confidence'] == 'low'


def test_northern_hemisphere_claim_is_ignored_and_downgraded():
    r = detect('WGS84 UTM ZONE 54N', PNG_KUTUBU_Z54)
    assert r['epsg'] == 32754
    assert r['confidence'] == 'medium'


def test_no_zone_information_returns_none():
    r = detect('NOTHING USEFUL HERE', BROKEN_HILL_Z54)
    assert r['epsg'] is None
    assert r['confidence'] == 'none'


def test_geographic_units_with_datum():
    r = detect('DATUM PNG94', (143.2, -6.4), coord_unit=3)
    assert r['epsg'] == 5546
    assert r['confidence'] == 'high'


def test_geographic_units_without_datum_assume_wgs84():
    r = detect('', (141.5, -31.9), coord_unit=3)
    assert r['epsg'] == 4326
    assert r['confidence'] == 'medium'


def test_user_override():
    r = detect('GDA94, MGA ZONE 54', BROKEN_HILL_Z54, epsg_override=7854)
    assert r['epsg'] == 7854
    assert r['confidence'] == 'user'


def test_invalid_user_override_raises():
    with pytest.raises(ValueError):
        detect('', BROKEN_HILL_Z54, epsg_override=99999)
