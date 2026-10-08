import sys
from pathlib import Path

import numpy as np
import pytest
import segyio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Straight 2D line near Broken Hill, NSW in GDA94 / MGA zone 54 (EPSG:28354)
LINE_START = (550935.0, 6465436.0)
LINE_END = (545111.0, 6474737.0)
N_TRACES = 200
N_SAMPLES = 50


def write_segy(path: Path, ebcdic: str, x: np.ndarray, y: np.ndarray,
               scalar: int = -100, coord_units: int = 1) -> Path:
    """Write a small post-stack 2D SEG-Y with CDP coordinates and the given EBCDIC text."""
    spec = segyio.spec()
    spec.format = 5  # IEEE float
    spec.samples = range(N_SAMPLES)
    spec.tracecount = len(x)

    with segyio.create(str(path), spec) as f:
        f.text[0] = segyio.tools.create_text_header(
            {i + 1: line for i, line in enumerate(ebcdic.splitlines()[:40])})
        f.bin.update(hdt=2000, hns=N_SAMPLES, format=5)
        for i in range(len(x)):
            f.header[i] = {
                segyio.TraceField.CDP: 2000 + i,
                segyio.TraceField.CDP_X: int(round(x[i] * abs(scalar))) if scalar < 0 else int(x[i]),
                segyio.TraceField.CDP_Y: int(round(y[i] * abs(scalar))) if scalar < 0 else int(y[i]),
                segyio.TraceField.SourceGroupScalar: scalar,
                segyio.TraceField.CoordinateUnits: coord_units,
                segyio.TraceField.offset: 0,
                segyio.TraceField.TraceIdentificationCode: 1,
            }
            f.trace[i] = np.sin(np.linspace(0, 6, N_SAMPLES) + i / 10).astype(np.float32)
    return path


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
