"""segy_parser.py - SEG-Y 文件解析核心模块"""
import numpy as np
import segyio


DATA_FORMAT_NAMES = {
    1: "IBM_FLOAT_4_BYTE",
    2: "INT_4_BYTE",
    3: "INT_2_BYTE",
    4: "FIXED_POINT_4_BYTE",
    5: "IEEE_FLOAT_4_BYTE",
    6: "IEEE_FLOAT_8_BYTE",
    8: "INT_1_BYTE",
    9: "INT_8_BYTE",
    10: "UINT_4_BYTE",
    11: "UINT_2_BYTE",
    12: "UINT_8_BYTE",
    15: "INT_3_BYTE",
    16: "INT_4_BYTE_ALT",
}


def parse_segy_file(filepath: str, max_traces: int = 1000) -> dict:
    """
    解析 SEG-Y 文件，返回 EBCDIC 头、二进制头、道头数组。
    使用 ignore_geometry=True 安全打开任意非标 SEG-Y 文件。
    对大文件均匀抽样以提升性能。
    """
    with segyio.open(filepath, mode='r', ignore_geometry=True) as f:

        # --- EBCDIC 文本头 ---
        try:
            ebcdic_raw = segyio.tools.wrap(f.text[0])
        except Exception:
            ebcdic_raw = str(f.text[0])

        # --- 二进制头 ---
        bin_hdr = f.bin
        sample_interval  = int(bin_hdr[segyio.BinField.Interval])
        samples_per_trace = int(bin_hdr[segyio.BinField.Samples])
        data_format_code = int(bin_hdr[segyio.BinField.Format])
        try:
            segy_revision = int(bin_hdr[segyio.BinField.SEGYRevision])
        except Exception:
            segy_revision = 0

        total_traces = f.tracecount

        # --- 道头抽样 ---
        if total_traces <= max_traces:
            indices = np.arange(total_traces, dtype=np.int32)
        else:
            indices = np.linspace(0, total_traces - 1, max_traces, dtype=np.int32)

        # 批量读取道头字段（C 层实现，比 Python 循环快 100x）
        def read_attr(field):
            try:
                return f.attributes(field)[indices]
            except Exception:
                return np.zeros(len(indices), dtype=np.int32)

        sx      = read_attr(segyio.TraceField.SourceX)
        sy      = read_attr(segyio.TraceField.SourceY)
        cdpx    = read_attr(segyio.TraceField.CDP_X)
        cdpy    = read_attr(segyio.TraceField.CDP_Y)
        ilines  = read_attr(segyio.TraceField.INLINE_3D)
        xlines  = read_attr(segyio.TraceField.CROSSLINE_3D)
        offsets = read_attr(segyio.TraceField.offset)
        scalars = read_attr(segyio.TraceField.SourceGroupScalar)
        coord_units = read_attr(segyio.TraceField.CoordinateUnits)
        tric    = read_attr(segyio.TraceField.TraceIdentificationCode)

    return {
        'ebcdic_raw': ebcdic_raw,
        'bin_hdr': {
            'sample_interval_us': sample_interval,
            'samples_per_trace': samples_per_trace,
            'data_format_code': data_format_code,
            'data_format': DATA_FORMAT_NAMES.get(data_format_code, f"CODE_{data_format_code}"),
            'segy_revision': segy_revision,
        },
        'trace_arrays': {
            'sx': sx, 'sy': sy,
            'cdpx': cdpx, 'cdpy': cdpy,
            'ilines': ilines, 'xlines': xlines,
            'offsets': offsets, 'scalars': scalars,
            'coord_units': coord_units, 'tric': tric,
        },
        'total_traces': total_traces,
        'sampled_count': len(indices),
    }
