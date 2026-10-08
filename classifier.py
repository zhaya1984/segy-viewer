"""classifier.py - SEG-Y 文件分类逻辑"""
import re
import os
import json
import numpy as np


# ── 时深域关键词 ──────────────────────────────────────────────
DEPTH_KEYWORDS = [
    'depth migrated', 'depth domain', 'psdm', 'kdmig', 'z-domain',
    'depth section', 'depth conversion', 'pre-sdm', 'pre sdm',
    'kirchhoff depth', 'rtm depth', 'fwi depth',
]
TIME_KEYWORDS = [
    'time migrated', 'time domain', 'pstm', 'ktmig', 'twt',
    'two-way time', 'two way time', 'millisecond', 'twtms',
    'pre-stm', 'pre stm', 'kirchhoff time', 'rtm time',
]

# ── EBCDIC 关键字正则 ──────────────────────────────────────────
# Rules:
#  - Longer compound keywords (e.g. "SURVEY NAME") must come BEFORE shorter
#    ones ("SURVEY") in the alternation so they match first.
#  - Capture stops at two-or-more consecutive spaces (next field starts) or
#    at end-of-token via [^  ]+ style trimming done in extract_ebcdic_keywords.

EBCDIC_PATTERNS = {
    # "SURVEY NAME : BROKEN HILL 2005" / "PROJECT NAME : X" / "PROJECT : X"
    'project':    r'(?:survey\s+name|project\s+name|project\s+no\.?|project|survey\s+no\.?|survey)[:\s]+([A-Z0-9][A-Z0-9 \-_\.]{1,40})',

    # "COMPANY : X" / "CLIENT : X" / "ACQUIRED ... FOR X" (client after FOR)
    'company':    r'(?:company|client|operator)[:\s]+([A-Z][A-Z0-9 &\-\.]{2,30})',
    'client_for': r'\bFOR\s+([A-Z][A-Z0-9&\-\.]{1,25})(?:\s|$)',

    # "AREA : X" / "BASIN : X" etc.
    'area':       r'(?:area|field|basin|block|region|location)[:\s]+([A-Z][A-Z0-9 \-_]{2,30})',

    # Dates: "DATE RECORDED : AUG 2005", "SEGY TAPE PRODUCED : MARCH 2008"
    'date':       r'(?:date\s+recorded|date\s+acquired|tape\s+produced|produced|acquired|date|vintage)[:\s]+([A-Z]{3,9}\s+\d{4}|\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?)',

    'year':       r'\b(19[89]\d|20[0-3]\d)\b',

    # "LINE NO : 05GA-BH1" / "LINE NAME : X" — compound before simple
    'line_name':  r'(?:line\s+no\.?|line\s+name|line\s+number|line)[:\s]+([A-Z0-9][A-Z0-9\-_\.]{1,20})',

    # "ACQUIRED BY TERREX SEISMIC" / "CONTRACTOR : X" / "PROCESSED BY : X"
    'contractor': r'(?:acquired\s+by|contractor|processed\s+by|proc\.?\s*by|processing\s+by)[:\s]+([A-Z][A-Z0-9 &\-\.]{2,35})',
}


def classify(parsed: dict) -> dict:
    """
    综合分类结果，返回分类字典。
    """
    ta = parsed['trace_arrays']
    bin_hdr = parsed['bin_hdr']
    ebcdic = parsed['ebcdic_raw']

    dimension  = detect_dimension(ta['ilines'], ta['xlines'], ta['sx'], ta['sy'])
    stack_type = detect_stack_type(ta['offsets'], ta['tric'])
    domain     = detect_domain(ebcdic, bin_hdr['sample_interval_us'])
    survey_type = detect_survey_type(ebcdic)
    keywords   = extract_ebcdic_keywords_llm(ebcdic)
    utm_zone   = extract_utm_zone(ebcdic)

    return {
        'dimension':   dimension,
        'stack_type':  stack_type,
        'domain':      domain,
        'survey_type': survey_type,
        'data_type':   f"{dimension}_{stack_type}_{domain}",
        'ebcdic_keywords': keywords,
        'utm_zone_from_ebcdic': utm_zone,
    }


def detect_dimension(ilines: np.ndarray, xlines: np.ndarray,
                     sx: np.ndarray, sy: np.ndarray) -> str:
    """
    2D vs 3D detection — three-stage logic:

    Stage 1: Filter junk inline/xline values.
      Values outside [1, 999999] are likely garbage (other fields misread into
      these bytes). Example: INLINE_3D = -655293464 in Australian 2D datasets.

    Stage 2: Grid-fill ratio check.
      A real 3D survey has n_il × n_xl ≈ n_traces.
      If n_il × n_xl >> n_samples the bytes don't form a proper grid — fall
      through to coordinate PCA.

    Stage 3: Coordinate PCA.
      Points along a single 2D line are highly collinear (first principal
      component explains >98% of variance). A 3D areal grid is not collinear.
    """
    n_samples = max(1, len(ilines))

    # Stage 1 — filter plausible inline/xline values (1 to 999 999)
    il_valid = ilines[(ilines > 0) & (ilines < 1_000_000)]
    xl_valid = xlines[(xlines > 0) & (xlines < 1_000_000)]
    n_il = len(np.unique(il_valid))
    n_xl = len(np.unique(xl_valid))

    # Stage 2 — grid-fill ratio
    if n_il > 1 and n_xl > 1:
        grid_ratio = (n_il * n_xl) / n_samples
        if grid_ratio <= 5:
            # Looks like a real 3D grid (cells ≈ samples)
            return "3D"
        # grid_ratio >> 1 → both dimensions span almost every sample →
        # they are not forming a grid (CDP seq. numbers in both fields)

    # Stage 3 — PCA on source coordinates
    coords_x = sx[sx != 0].astype(float)
    coords_y = sy[sy != 0].astype(float)
    n = min(len(coords_x), len(coords_y))
    if n < 3:
        return "2D"

    pairs = np.column_stack([coords_x[:n], coords_y[:n]])
    centered = pairs - pairs.mean(axis=0)
    cov = np.cov(centered.T)
    if cov.ndim < 2:
        return "2D"

    eigenvalues = np.linalg.eigvalsh(cov)
    eigenvalues.sort()
    total_var = eigenvalues.sum()
    if total_var == 0:
        return "unknown"

    linearity = eigenvalues[-1] / total_var
    return "2D" if linearity > 0.95 else "3D"


def detect_stack_type(offsets: np.ndarray, tric: np.ndarray) -> str:
    """
    叠前 vs 叠后：
    - offset 全为 0 → poststack
    - 有效道中零值占比 > 95% → poststack
    - 否则 → prestack
    """
    valid = offsets[tric != 2]  # 排除 dead trace（TRIC=2）
    if len(valid) == 0:
        return "poststack"

    unique_offsets = np.unique(valid)
    if len(unique_offsets) <= 1:
        return "poststack"

    zero_ratio = np.sum(valid == 0) / len(valid)
    return "poststack" if zero_ratio > 0.95 else "prestack"


def detect_domain(ebcdic_text: str, sample_interval_us: int) -> str:
    """
    时间域 vs 深度域：
    1. EBCDIC 关键词匹配
    2. 回退：标准地震采样间隔 → 时间域
    """
    text_lower = ebcdic_text.lower()

    depth_score = sum(1 for kw in DEPTH_KEYWORDS if kw in text_lower)
    time_score  = sum(1 for kw in TIME_KEYWORDS  if kw in text_lower)

    if depth_score > time_score:
        return "depth"
    if time_score > depth_score:
        return "time"

    # 标准地震采样间隔（微秒）
    if sample_interval_us in {250, 500, 1000, 2000, 4000}:
        return "time"

    return "time"


def detect_survey_type(ebcdic_text: str) -> str:
    """海上 vs 陆上 vs 过渡带"""
    text_lower = ebcdic_text.lower()
    if any(kw in text_lower for kw in ['marine', 'offshore', 'ocean bottom', 'obs ', 'streamer', 'obc']):
        return "marine"
    if any(kw in text_lower for kw in ['land', 'onshore', 'desert', 'mountain']):
        return "land"
    if any(kw in text_lower for kw in ['transition', 'shallow water', 'swath']):
        return "transition"
    return "unknown"


def extract_utm_zone(raw_text: str) -> int | None:
    """
    从 EBCDIC 文本中提取 UTM Zone 号。
    常见格式：MGA54、MGA ZONE 54、UTM ZONE 54N、GDA94/MGA54、ZONE 54
    """
    text = raw_text.upper()
    patterns = [
        r'MGA\s*(\d{2})',                          # MGA54 / MGA 54
        r'GDA\s*\d*\s*/\s*MGA\s*(\d{2})',          # GDA94/MGA54
        r'UTM\s+ZONE\s+(\d{1,2})\s*[NS]?',         # UTM ZONE 54N
        r'(?<!\d)ZONE\s+(\d{1,2})\s*[NS]?(?!\d)',  # ZONE 54
        r'MGA\s+ZONE\s+(\d{1,2})',                 # MGA ZONE 54
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            zone = int(m.group(1))
            if 1 <= zone <= 60:
                return zone
    return None


def extract_ebcdic_keywords(raw_text: str) -> dict:
    """
    Extract structured metadata from EBCDIC text.

    EBCDIC format: 40 lines × 80 chars, each prefixed "C  1 " … "C 40 ".
    The prefix is stripped before matching so patterns don't need to
    account for it.

    Trimming: captured values are trimmed of leading/trailing whitespace
    and common trailing punctuation. Values that consist only of generic
    words like "NAME", "NO", "NUMBER" are discarded (they indicate the
    regex matched the label instead of the value).
    """
    # Strip line prefixes "C 1 " / "C10 " / "C 40 " etc.
    text_clean = re.sub(r'\bC\s{0,2}\d{1,2}\s', ' ', raw_text)
    text_upper = text_clean.upper()

    # Words that indicate the regex captured a label word, not a value
    JUNK_VALUES = {'NAME', 'NO', 'NUMBER', 'NONE', 'N/A', 'NA', 'TBD', ''}

    result = {}
    for key, pattern in EBCDIC_PATTERNS.items():
        if key in ('year', 'client_for'):
            continue
        m = re.search(pattern, text_upper, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip('.,;:').strip()
            # Trim trailing noise: stop at 2+ consecutive spaces
            val = re.split(r'\s{2,}', val)[0].strip().rstrip('.,;:').strip()
            if val.upper() not in JUNK_VALUES and len(val) >= 2:
                result[key] = val

    # "FOR [CLIENT]" fallback for company if not already found
    if 'company' not in result:
        m = re.search(EBCDIC_PATTERNS['client_for'], text_upper)
        if m:
            val = m.group(1).strip().rstrip('.,;')
            if val.upper() not in JUNK_VALUES:
                result['company'] = val

    # Year hints when date not matched
    if 'date' not in result:
        years = re.findall(EBCDIC_PATTERNS['year'], text_upper)
        if years:
            result['year_hints'] = ', '.join(sorted(set(years)))

    return result


def extract_ebcdic_keywords_llm(raw_text: str) -> dict:
    """
    Extract EBCDIC metadata using Claude API when available,
    falling back to regex-based extraction.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        return extract_ebcdic_keywords(raw_text)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        prompt = f"""You are extracting metadata from a SEG-Y seismic file EBCDIC text header.
The EBCDIC header is 40 lines of 80 characters each, often with line prefixes like "C  1", "C  2", etc.

Extract these fields from the text (return null if not found):
- project: Survey/project/line name
- company: Company, client, or operator name
- area: Area, field, basin, block, or region name
- date: Acquisition or recording date (e.g. "AUG 2005", "2005-08")
- year_hints: Year(s) mentioned if no specific date (e.g. "2005, 2006")
- line_name: Line number or name
- contractor: Contractor, acquired by, or processed by

Return ONLY a JSON object with these exact keys. Use null for missing fields.
Do not include any explanation or markdown.

EBCDIC TEXT:
{raw_text[:3200]}"""

        response = client.messages.create(
            model='claude-haiku-4-5',
            max_tokens=512,
            messages=[{'role': 'user', 'content': prompt}],
        )

        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)

        parsed = json.loads(text)
        result = {}
        for key in ('project', 'company', 'area', 'date', 'year_hints', 'line_name', 'contractor'):
            val = parsed.get(key)
            if val and isinstance(val, str) and val.strip():
                result[key] = val.strip()
        return result

    except Exception as e:
        print(f"[LLM EBCDIC] Falling back to regex: {e}")
        return extract_ebcdic_keywords(raw_text)
