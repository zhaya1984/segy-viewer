import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samples.make_samples import write_segy as _write_segy  # noqa: E402

# Straight 2D line near Broken Hill, NSW in GDA94 / MGA zone 54 (EPSG:28354)
LINE_START = (550935.0, 6465436.0)
LINE_END = (545111.0, 6474737.0)
N_TRACES = 200


def write_segy(path: Path, ebcdic: str, x, y, **kwargs) -> Path:
    return _write_segy(path, ebcdic, x, y, n_samples=50, **kwargs)


@pytest.fixture
def line_xy():
    x = np.linspace(LINE_START[0], LINE_END[0], N_TRACES)
    y = np.linspace(LINE_START[1], LINE_END[1], N_TRACES)
    return x, y


@pytest.fixture
def mga54_segy(tmp_path, line_xy):
    ebcdic = (
        "SURVEY NAME : BROKEN HILL TEST    LINE NO : TEST-01\n"
        "ORIGINAL FIELD TAPE NOS: 1-22\n"
        "DATE RECORDED : AUG 2005\n"
        "TIME MIGRATED STACK\n"
        "NOTE: X & Y ARE REAL COORDINATES. GDA94, MGA ZONE 54"
    )
    return write_segy(tmp_path / "mga54.sgy", ebcdic, *line_xy)


@pytest.fixture
def no_crs_segy(tmp_path, line_xy):
    return write_segy(tmp_path / "nocrs.sgy", "SURVEY NAME : UNKNOWN\nNO COORDINATE INFO", *line_xy)
