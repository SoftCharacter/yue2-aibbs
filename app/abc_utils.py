"""ABC 乐谱文本工具（不依赖 Qt，工作线程可用）。"""
from __future__ import annotations

import re

# 匹配引号内的和弦符号：形如 "Am" 的字母和弦，或 "N.C." / "N.C"（无和弦标记）
CHORD_SYMBOL = re.compile('"(?:[A-G][^"]*|N\\.?C\\.?)"')


def count_chords(abc):
    """统计 ABC 文本中的和弦数量，跳过信息行（X: 等）与注释行（% 开头）。"""
    return sum(
        len(CHORD_SYMBOL.findall(line))
        for line in abc.splitlines()
        # 信息头（如 "X:1"）与注释行不计入和弦
        if not re.match('^[A-Za-z]:', line) and not line.startswith('%')
    )


def strip_chords(abc):
    """去除 ABC 文本中的和弦符号，信息行与注释行原样保留。"""
    out = []
    for line in abc.splitlines():
        if re.match('^[A-Za-z]:', line) or line.startswith('%'):
            out.append(line)
            continue
        out.append(CHORD_SYMBOL.sub('', line))
    # 保持原文末尾换行的有无一致
    return '\n'.join(out) + ('\n' if abc.endswith('\n') else '')
