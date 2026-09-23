"""AI 写歌词 / 改歌词 / 风格描述：默认提示词、请求内容组装和模型输出整理。

不依赖 Qt 和网络，创作页、改词对话框与批量生成共用。发送请求见 llm_client.stream_chat。
"""
from __future__ import annotations

import re

# 提示词种类：(标识, 中文名, 用途说明)，用于界面下拉框与设置项
PROMPT_KINDS = (
    ('write', '写歌词', '创作页"AI 写歌词"、批量创作按主题写词时使用'),
    ('rewrite', '改歌词', '各页面"AI 改歌词"、批量翻唱改词、批量创作润色导入歌词时使用'),
    ('style', '风格描述', '"AI 写歌词"顺便生成风格、批量生成"AI 生成风格"时使用'),
)

# 三种提示词的默认系统提示词，用户可在设置中覆盖
DEFAULT_PROMPTS = {
    'write': '你是一名专业的流行歌曲作词人，负责按用户给出的主题和要求创作一首完整的新歌词。歌词会交给 AI 音乐模型 YuE2 演唱。\n\n请严格遵守：\n1. 只输出歌词本身，不要歌名、解释、前言、Markdown 或代码块。\n2. 用段落标签组织歌词，每个标签独占一行：[Intro] [Verse] [Pre-Chorus] [Chorus] [Bridge] [Instrumental] [Outro]；段落之间空一行。前奏、间奏、尾奏等纯器乐段落只写标签，下面不写歌词。\n3. 每行一句，行尾不加标点。中文每行 6～14 个字为宜，英文每行 5～10 个单词为宜；同一段落内各行长度大致整齐，方便配旋律。\n4. 结构完整：主歌叙事铺垫，副歌提炼主题、朗朗上口，几次副歌可以重复或只做小改动；需要起伏时加导歌或桥段。\n5. 按【篇幅】给出的段落安排和行数来写，不要明显超出；篇幅是目标，实际歌曲时长还受旋律、间奏和生成上限影响。\n6. 意象具体、情绪连贯、押韵自然，避免空洞的口号和陈词滥调；除非用户要求，不要夹杂其他语言。',
    'rewrite': '你是一名专业的歌词编辑，负责按用户的修改要求改写现有歌词。改写后的歌词会交给 AI 音乐模型 YuE2 演唱。\n\n请严格遵守：\n1. 只输出修改后的完整歌词本身，不要解释、标题、前言、Markdown 或代码块。\n2. 使用段落标签组织歌词，每个标签独占一行，例如 [Intro] [Verse] [Pre-Chorus] [Chorus] [Bridge] [Instrumental] [Outro]；段落之间空一行。纯器乐段落只写标签。\n3. 每行一句，行尾不加标点，句子长度适合演唱。\n4. 用户没有要求修改的部分尽量保持原样；注意押韵、节奏、意象和情绪的连贯。给出【歌词语言】时，改写后的歌词使用该语言。\n5. 出现【约束】时必须遵守：要求保持段落和行数时，段落标签顺序、每段行数不变；要求每行字数也保持时，每行字数（英文按音节数）与原歌词对应行尽量一致，以便贴合原曲旋律。',
    'style': '你是一名音乐制作人，负责为 AI 音乐模型 YuE2 写歌曲风格描述。\n\n只输出一行英文标签，用英文逗号分隔，依次包含：演唱语言、流派、人声（性别与音色）、2～4 种主要乐器、情绪、速度。\n不超过 30 个英文单词；不要解释，不要编号，不要 Markdown。给出【风格参考】或【要求】时优先遵从。\n示例：Mandarin, uplifting pop, bright female vocal, piano, acoustic guitar, light drums, hopeful, mid-tempo',
}

# 篇幅选项：(标识, 中文名, 段落与行数说明)
LENGTHS = (
    ('1min', '约 1 分钟', '[Verse] 4 行 + [Chorus] 4 行，共约 8～10 行歌词'),
    ('2min', '约 2 分钟', '[Intro] + [Verse] 4 行 + [Chorus] 4 行 + [Verse] 4 行 + [Chorus] 4 行 + [Outro]，共约 16～20 行歌词'),
    ('3min', '约 3 分钟', '[Intro] + [Verse] + [Pre-Chorus] + [Chorus] + [Verse] + [Pre-Chorus] + [Chorus] + [Bridge] + [Chorus] + [Outro]；主歌各 6 行、副歌各 4 行，导歌、桥段各 2 行，共约 28～34 行歌词'),
    ('4min', '约 4 分钟', '[Intro] + [Verse] + [Pre-Chorus] + [Chorus] + [Verse] + [Pre-Chorus] + [Chorus] + [Instrumental] + [Bridge] + [Chorus] + [Chorus] + [Outro]；主歌、副歌各 6 行、导歌和桥段各 2～4 行，共约 40～50 行歌词'),
)

# 所有篇幅标识，供界面下拉框使用
LENGTH_KEYS = tuple(k for k, _, _ in LENGTHS)

# 默认篇幅
DEFAULT_LENGTH = '3min'

# 结构约束选项：off 不限制 / lines 保持段落与行数 / units 每行字数也保持
STRUCTURES = ('off', 'lines', 'units')

# 各结构约束在改写请求中的附加说明
STRUCTURE_CONSTRAINTS = {
    'lines': '【约束】保持段落和行数：段落标签顺序、每段行数与当前歌词一致；每行字数可以按内容和语言自然调整。',
    'units': '【约束】保持结构：段落标签顺序、每段行数、每行字数与当前歌词一致。',
}

# 匹配段落标签行，形如 [Verse] / [Chorus] 等
TAG_LINE = re.compile(r'^\[[^\]]+\]$')

# 匹配开头的歌名/标题行（书名号形式或"歌名: xxx"形式）
_TITLE_LINE = re.compile(
    r'^(?:《[^》]{1,40}》|(?:歌名|标题|曲名|title)\s*[:：].{0,60})$', re.I
)


def system_prompt(settings, kind):
    """设置里保存的提示词；没填（或只有空白）时使用默认提示词。"""
    if kind not in DEFAULT_PROMPTS:
        raise ValueError(f'未知的提示词类型：{kind}')
    # 逐层取 settings.llm.prompts.<kind>，任一环节缺失都回落到空串
    value = ((settings.get('llm') or {}).get('prompts') or {}).get(kind) or ''
    return value.strip() or DEFAULT_PROMPTS[kind]


def length_label(key):
    """返回篇幅标识对应的中文名。"""
    return next(label for k, label, _ in LENGTHS if k == key)


def write_request(
    theme,
    *,
    length=DEFAULT_LENGTH,
    language='',
    style='',
    requirements='',
    variant=None,
    avoid=(),
):
    """写新歌词的请求内容。variant=(第几首, 共几首)；avoid=已写好的其他版本开头。"""
    theme = str(theme or '').strip()
    if not theme:
        raise ValueError('请先填写歌曲主题或故事')
    # 由篇幅标识找到"中文名：段落说明"，找不到说明标识非法
    label = next(f'{l}：{d}' for k, l, d in LENGTHS if k == length)
    if label is None:
        raise ValueError(f'未知的篇幅：{length}')

    parts = [f'【主题】\n{theme}', f'【篇幅】\n{label}']
    if str(language or '').strip():
        parts.append(f'【歌词语言】\n{language.strip()}')
    if str(style or '').strip():
        parts.append(f'【歌曲风格】\n{style.strip()}')
    if str(requirements or '').strip():
        parts.append(f'【其他要求】\n{requirements.strip()}')

    # 批量生成同一主题多首时，提示模型写出明显不同的版本
    if variant and variant[1] > 1:
        diff = f'【差异化】这是同一主题的第 {variant[0]}/{variant[1]} 首，请在视角、意象、叙事和段落安排上与其他版本明显不同。'
        if avoid:
            diff += '\n已经写好的其他版本开头（请避免雷同）：\n' + '\n'.join(list(avoid)[:20])
        parts.append(diff)

    return '\n\n'.join(parts)


def rewrite_request(lyrics, instruction, *, language='', style='', structure='off'):
    """改写现有歌词的请求内容。structure：off 不限制 / lines 段落和行数 / units 每行字数也保持。"""
    if structure not in STRUCTURES:
        raise ValueError(f'未知的结构约束：{structure}')
    lyrics = str(lyrics or '').strip()
    instruction = str(instruction or '').strip()
    if not lyrics:
        raise ValueError('当前没有歌词，无法改写；请先填写歌词，或使用"AI 写歌词"')
    if not instruction:
        raise ValueError('请先填写修改要求')

    parts = [f'【当前歌词】\n{lyrics}']
    if str(language or '').strip():
        parts.append(f'【歌词语言】\n{language.strip()}')
    if str(style or '').strip():
        parts.append(f'【歌曲风格】\n{style.strip()}')
    parts.append(f'【修改要求】\n{instruction}')
    if structure != 'off':
        parts.append(STRUCTURE_CONSTRAINTS[structure])
    return '\n\n'.join(parts)


def style_request(lyrics, *, requirements='', facts=(), reference=''):
    """为歌词写风格描述的请求内容。facts：主题、语言、原曲速度调性等已知信息。"""
    parts = [f'【歌词】\n{str(lyrics or "").strip()[:1500]}']
    if str(requirements or '').strip():
        parts.append(f'【要求】\n{requirements.strip()}')

    # 已知信息先逐项去空白、过滤空项，再用分号拼接
    facts = [str(item).strip() for item in facts if str(item).strip()]
    if facts:
        parts.append('【已知信息】\n' + '；'.join(facts))

    if str(reference or '').strip():
        parts.append(f'【风格参考】\n{reference.strip()}')
    return '\n\n'.join(parts)


def clean_lyrics(text):
    """去掉思考过程和代码块标记，保留全部歌词文字（空行、"希望你"这类句子也可能是真歌词）。"""
    # 去掉 <think>...</think> 思考块（非贪婪，跨行）
    text = re.sub(r'<think>.*?(?:</think>|$)', '', text, flags=re.S)
    # 去掉独占一行的 ``` 代码块围栏，再去掉首尾空白
    text = re.sub(r'^\s*```[a-zA-Z]*\s*$', '', text, flags=re.M).strip()
    # 逐行去掉行尾空白后重新拼接，末尾补一个换行
    return '\n'.join(line.rstrip() for line in text.splitlines()).strip() + '\n'


def with_default_tag(text):
    """歌词没有任何段落标签时，在开头补一个 [Verse] 标签。"""
    if any(TAG_LINE.match(line.strip()) for line in text.splitlines()):
        return text
    return '[Verse]\n' + text.lstrip()


def tidy_lyrics(text):
    """整理生成的歌词：去掉开头的歌名行；没有段落标签时补 [Verse]。返回 (歌词, 提示列表)。"""
    notes = []
    lines = text.strip().splitlines()
    # 首行是歌名/标题时去掉，并记录提示
    if lines and _TITLE_LINE.match(lines[0].strip()):
        lines = lines[1:]
        notes.append('已去掉开头的歌名行')

    text = '\n'.join(lines).strip() + '\n'
    result = with_default_tag(text)
    # 结果与整理前不同，说明确实补了 [Verse] 标签
    if result != text:
        notes.append('歌词没有段落标签，已整体标为 [Verse]')
    return result, notes


def clean_style(text):
    """取模型回复的第一行作为风格描述。"""
    text = clean_lyrics(text)
    # 去掉空行与代码块围栏，得到有效的描述行
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith('```')
    ]
    # 去掉形如 "style:" / "风格:" 的前缀
    first = re.sub(
        r'^(?:style|风格(?:描述)?)\s*[:：]',
        '',
        lines[0] if lines else '',
        flags=re.I,
    )
    # 折叠空白并去掉引号/句点等装饰字符
    first = ' '.join(first.split()).strip('`"\'"”。. ')
    if not first:
        raise ValueError('模型没有返回风格描述')
    return first[:1000]


def lyric_lines(text):
    """歌词里真正要唱的行（去掉段落标签和空行）。"""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not TAG_LINE.match(line.strip())
    ]
