"""Turn ASR text + forced-alignment timestamps into singable lyric lines, YuE2 lyrics and LRC."""
from __future__ import annotations

import re

# 视为断句标点的字符集合
BREAK_PUNCT = set('，。！？；、,.!?;…')
# SheetSage2 结构标签到标准段落名的映射
SECTION_TAGS = {
    'intro': 'Intro',
    'verse': 'Verse',
    'pre-chorus': 'Pre-Chorus',
    'prechorus': 'Pre-Chorus',
    'chorus': 'Chorus',
    'bridge': 'Bridge',
    'outro': 'Outro',
    'end': 'Outro',
    'inst': 'Instrumental',
    'instrumental': 'Instrumental',
    'interlude': 'Instrumental',
    'solo': 'Solo',
    'break': 'Instrumental',
}
# 需要忽略的结构段落名
IGNORED_SECTIONS = {'start', 'silence', 'no_function', 'no-function'}
# 语言下拉框选项：(显示名, 内部语言名)，None 表示自动识别
LANGUAGES = [
    ('自动识别', None),
    ('中文', 'Chinese'),
    ('粤语', 'Cantonese'),
    ('英语', 'English'),
    ('日语', 'Japanese'),
    ('韩语', 'Korean'),
    ('法语', 'French'),
    ('德语', 'German'),
    ('西班牙语', 'Spanish'),
    ('俄语', 'Russian'),
    ('葡萄牙语', 'Portuguese'),
    ('意大利语', 'Italian'),
    ('泰语', 'Thai'),
    ('越南语', 'Vietnamese'),
    ('印尼语', 'Indonesian'),
]


def _is_cjk(ch):
    """判断字符是否属于中日韩（含假名、谚文）等无空格书写体系。"""
    cp = ord(ch)
    return (
        12352 <= cp <= 12543      # 平假名 / 片假名
        or 13312 <= cp <= 40959   # CJK 扩展 A + 统一表意文字
        or 44032 <= cp <= 55215   # 谚文音节
        or 63744 <= cp <= 64255   # CJK 兼容表意文字
    )


def _join(tokens):
    """拼接 token，仅在相邻两侧都非 CJK 时插入空格（CJK 之间无需空格）。"""
    out = ''
    for tok in tokens:
        if not tok:
            continue
        if out and not _is_cjk(out[-1]) and not _is_cjk(tok[0]):
            out += ' '
        out += tok
    return out


def _width(tokens):
    """估算 token 序列的显示宽度：CJK 字符计 1，非 CJK token 粗略计 2。"""
    return sum(len(tok) if _is_cjk(tok[0]) else 2 for tok in tokens if tok)


def build_lines(text, items, gap=0.6, max_width=22):
    """items: [(token, start, end)] in order. Break at punctuation, pauses and length."""
    # 无对齐 token 时，直接按标点正则切分原文
    if not items:
        parts = [p.strip() for p in re.split(r'[，。！？；、,.!?;…\n]+', text or '') if p.strip()]
        return [{'start': None, 'end': None, 'text': p} for p in parts]

    # 原文含标点时放宽停顿阈值，避免在句中标点附近误断
    has_punct = any(ch in BREAK_PUNCT for ch in text or '')
    gap_eff = max(gap, 1.5) if has_punct else gap

    lines = []
    current = []
    pos = 0
    prev_end = None
    for token, start, end in items:
        if not token:
            continue
        # 在原文中定位 token，跳过间隔过远的匹配
        idx = text.find(token, pos) if text else -1
        if idx >= 0 and idx - pos > 12:
            idx = -1
        has_break = False
        if idx >= 0:
            has_break = any(ch in BREAK_PUNCT for ch in text[pos:idx])
            pos = idx + len(token)

        cur_width = _width([t[0] for t in current])
        # 上一 token 后有足够停顿且当前行已有内容时触发断行
        is_break = prev_end is not None and start - prev_end >= gap_eff and cur_width >= 4

        if current and (has_break or is_break or cur_width + _width([token]) > max_width):
            lines.append(current)
            current = []
        current.append((token, start, end))
        prev_end = end

    if current:
        lines.append(current)

    # 合并过短且时间上紧邻的相邻行
    merged = []
    for line in lines:
        if (
            merged
            and _width([t[0] for t in line]) <= 2
            and line[0][1] - merged[-1][-1][2] < 2
            and _width([t[0] for t in merged[-1] + line]) <= max_width + 4
        ):
            merged[-1] = merged[-1] + line
            continue
        merged.append(line)

    return [
        {'start': line[0][1], 'end': line[-1][2], 'text': _join([t[0] for t in line])}
        for line in merged
    ]


def merge_structure(structure):
    """规范化结构标签，并合并相邻的同名段落（延长其结束时间）。"""
    merged = []
    for start, end, name in (structure or []):
        if name.lower().strip() in IGNORED_SECTIONS:
            continue
        section = SECTION_TAGS.get(name.lower().strip(), name.strip().title())
        if merged and merged[-1][2] == section:
            merged[-1] = (merged[-1][0], end, section)
            continue
        merged.append((start, end, section))
    return merged


def format_yue2(lines, structure=None, section_gap=4):
    """Group lines under [Section] tags taken from SheetSage2 structure, or from long pauses."""
    # 结构按开始时间排序并规范化，得到 (start, end, name) 列表
    struct = merge_structure(sorted(structure or [], key=lambda part: part[0]))
    grouped = []
    if struct and all(line['start'] is not None for line in lines):
        # 依据结构边界把每行归入对应段落
        i = 0
        for line in lines:
            midpoint = (line['start'] + line['end']) / 2
            # 先把已过去的段落（其开始时间早于行中点）作为空段落推入
            while i < len(struct) and struct[i][0] <= midpoint:
                grouped.append((struct[i][2], []))
                i += 1
            # 找出包含该行中点的段落，否则按是否已到结尾归为 Outro/Verse
            section = next(
                (name for start, end, name in struct if start <= midpoint < end),
                'Outro' if midpoint >= max(end for _, end, _ in struct) else 'Verse',
            )
            if not grouped or grouped[-1][0] != section:
                grouped.append((section, []))
            grouped[-1][1].append(line)
        # 把尚未消费的尾部结构段落补齐为空段落
        grouped.extend((name, []) for _, _, name in struct[i:])
        # 过滤掉没有歌词且非首段的空段落
        filtered = []
        for section, lines_ in grouped:
            if not lines_ and filtered and not filtered[-1][1]:
                continue
            filtered.append((section, lines_))
        # 去掉结尾处的非 Outro 空段落
        while filtered and not filtered[-1][1] and filtered[-1][0] not in ('Outro',):
            filtered.pop()
        grouped = filtered
    else:
        # 无结构时，按行间长停顿切分为 Verse 段落
        current = []
        prev_end = None
        for line in lines:
            if (
                current
                and line['start'] is not None
                and prev_end is not None
                and line['start'] - prev_end >= section_gap
            ):
                grouped.append(('Verse', current))
                current = []
            current.append(line)
            prev_end = line['end']
        if current:
            grouped.append(('Verse', current))

    # 输出 YuE2 歌词文本：[段落名] 标签 + 每行歌词 + 空行
    out = []
    for section, lines_ in grouped:
        out.append(f'[{section}]')
        out.extend(line['text'] for line in lines_)
        out.append('')
    return '\n'.join(out).strip() + '\n'


def to_lrc(lines):
    """把带时间戳的歌词行转成 LRC 格式文本。"""
    out = []
    for line in lines:
        if line['start'] is None:
            out.append(line['text'])
            continue
        # 时间戳：分 / 秒 / 厘秒，格式 [mm:ss.cc]
        minutes, rest = divmod(round(line['start'] * 100), 6000)
        seconds, centis = divmod(rest, 100)
        out.append(f'[{minutes:02d}:{seconds:02d}.{centis:02d}]{line["text"]}')
    return '\n'.join(out) + '\n'
