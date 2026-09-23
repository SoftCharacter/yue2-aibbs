"""Lyrics and ABC editors with syntax highlighting and helper toolbars.

本模块实现歌词编辑器 LyricsEditor 与 ABC 乐谱编辑器 AbcEditor，并配套两个语法高亮器
LyricsHighlighter / AbcHighlighter。歌词编辑器支持按段落标签（[Verse] / [Chorus] 等）快速
插入结构、载入示例歌词、导入 .txt/.lrc 文本（自动剥离元信息与时间轴）、以及接入 AI 写歌词 /
改歌词对话框；ABC 编辑器提供打开、另存为、一键去掉和弦等操作，并在状态栏实时统计行数、小节
数与和弦数。模块顶部的 SECTIONS 与 LYRIC_EXAMPLES 分别定义段落标签映射与内置示例歌词。
"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..abc_utils import count_chords, strip_chords
from ..theme import C
from .common import FlowLayout, button, label

# 歌词段落标签到中文名的映射：歌词编辑器顶部据此生成插入按钮，ABC 乐谱同步使用相同标记
SECTIONS = [
    ('[Intro]', '前奏'),
    ('[Verse]', '主歌'),
    ('[Pre-Chorus]', '导歌'),
    ('[Chorus]', '副歌'),
    ('[Bridge]', '桥段'),
    ('[Instrumental]', '间奏'),
    ('[Solo]', '独奏'),
    ('[Outro]', '尾奏'),
]

# 内置示例歌词：键为下拉框展示名，值为 (风格描述, 歌词文本) 二元组
LYRIC_EXAMPLES = {
    '中文 · 温暖流行': (
        'Mandarin, warm piano, acoustic pop, female vocal',
        '[Verse]\n晚风轻轻吹过窗前\n你留下的笑还在昨天\n街灯把影子拉得很远\n我在原地数着时间\n\n'
        '[Chorus]\n让这首歌陪你走远\n把所有想念唱成明天\n如果风能带去我的心愿\n就让它落在你身边\n\n'
        '[Verse]\n旧照片里的蓝天\n还是那年夏天的脸\n我们说好的永远\n藏在每一句歌词里面\n\n'
        '[Chorus]\n让这首歌陪你走远\n把所有想念唱成明天\n如果风能带去我的心愿\n就让它落在你身边\n\n'
        '[Outro]\n就让它落在你身边',
    ),
    'English · Dreamy synth-pop': (
        'Dreamy synth-pop, warm female lead vocal, pulsing bass, shimmering synths, uplifting',
        '[Verse]\nCity windows turn to gold\nEvery streetlight has a story\nWe are brave and we are bold\n'
        'Running toward the morning glory\n\n[Chorus]\nStay awake, the night is ours\n'
        'We can dance beneath the stars\nHold this moment, hold it tight\nWe are sparks inside the night\n\n'
        '[Outro]\nInside the night',
    ),
    'English · Indie folk': (
        'Acoustic indie folk, intimate male vocal, fingerpicked guitar, gentle strings',
        '[Verse]\nDust is dancing in the doorway\nSummer settles on the road\nI can hear the old trees whisper\n'
        'All the secrets that they know\n\n[Chorus]\nTake me home across the river\nWhere the evening moves so slow\n'
        'If the wind can find its way there\nThen I know that I can go',
    ),
    '中文 · 放克迪斯科': (
        'Mandarin funk, nu-disco, groovy bass, clean electric guitar, bright synth, energetic male vocal',
        '[Intro]\n\n[Verse]\n灯光一闪 心跳在加速\n城市的夜 从来不认输\n脚步跟着节奏 越跳越投入\n'
        '今晚的我们 谁也别先离去\n\n[Pre-Chorus]\n把烦恼都丢进风里\n让音乐替我们说话\n\n'
        '[Chorus]\n今晚不眠 灯光为我们闪耀\n今晚不眠 所有人一起舞蹈\n别管明天 会不会太早\n'
        '此刻的快乐 就是最好\n\n[Outro]\n今晚不眠',
    ),
}


class LyricsHighlighter(QSyntaxHighlighter):
    """歌词语法高亮器：段落标签整行高亮，圆括号/全角括号内的旁白用弱色标记。"""

    def __init__(self, document):
        """初始化段落（section）与旁白（aside）两种字符格式。"""
        super().__init__(document)
        # 段落标签：强调色加粗，覆盖 [Verse] 这类整行
        self.section = QTextCharFormat()
        self.section.setForeground(QColor(C['accent']))
        self.section.setFontWeight(QFont.Bold)
        # 旁白：弱色，覆盖 (旁白) 或 （旁白）这类括注
        self.aside = QTextCharFormat()
        self.aside.setForeground(QColor(C['muted']))

    def highlightBlock(self, text):
        """按规则高亮一行：整行段落标签优先，其余括号括注用 aside 格式。"""
        # 去除首尾空白后判断是否为完整段落标签（如 [Verse]）
        stripped = text.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            self.setFormat(0, len(text), self.section)
            return
        # 匹配半角/全角圆括号包裹的旁白内容并逐段标记
        for m in re.finditer(r'\([^)]*\)|（[^）]*）', text):
            self.setFormat(m.start(), m.end() - m.start(), self.aside)


class AbcHighlighter(QSyntaxHighlighter):
    """ABC 乐谱语法高亮器：区分注释、声部、头部字段、和弦符号与小节线。"""

    def __init__(self, document):
        """构造一个着色辅助函数，并为各类语法元素准备对应字符格式。"""
        super().__init__(document)

        def fmt(color, bold=False):
            """按颜色（与可选加粗）创建一份字符格式。"""
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Bold)
            return f

        # 头部字段（如 T: 标题）、注释、和弦、小节线、声部各自的配色
        self.header = fmt(C['cyan'], True)
        self.comment = fmt(C['faint'])
        self.chord = fmt(C['accent2'])
        self.bar = fmt(C['amber'])
        self.voice = fmt(C['green'], True)

    def highlightBlock(self, text):
        """按优先级高亮一行：注释 → 声部 → 头部字段 → 和弦符号 → 小节线。"""
        # % 开头的整行视为注释
        if text.startswith('%'):
            self.setFormat(0, len(text), self.comment)
            return
        # V: 或 [V: 开头的声部声明整行高亮
        if re.match('^V:', text) or re.match(r'^\s*\[V:', text):
            self.setFormat(0, len(text), self.voice)
            return
        # 形如 X: 的头部字段仅高亮前两个字符
        if re.match('^[A-Za-z]:', text):
            self.setFormat(0, 2, self.header)
            return
        # 双引号包裹的和弦符号逐个高亮
        for m in re.finditer('"[^"]*"', text):
            self.setFormat(m.start(), m.end() - m.start(), self.chord)
        # 小节线（|、|:、:| 等）逐个高亮
        for m in re.finditer(r'\|+|:\||\|:', text):
            self.setFormat(m.start(), m.end() - m.start(), self.bar)


class LyricsEditor(QWidget):
    """歌词编辑器：段落标签工具条 + AI 输入行 + 正文编辑区 + 示例/导入/清空操作。"""

    # 歌词内容变化信号（无参）与载入示例信号（风格、歌词）
    changed = Signal()
    exampleChosen = Signal(str, str)

    def __init__(self, examples=True, write=False, parent=None):
        """构建布局；`examples` 为真时显示示例下拉框，`write` 为真时显示"AI 写歌词"按钮。"""
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        # 顶部流动布局：按 SECTIONS 生成"中文名 + 标签"的插入按钮
        wrap = QWidget()
        flow = FlowLayout(wrap, spacing=6)
        for tag, name in SECTIONS:
            btn = button(f'{name} {tag}', 'Chip', f'在光标处插入段落标签 {tag}')
            btn.clicked.connect(lambda _=False, t=tag: self.insert_section(t))
            flow.addWidget(btn)
        box.addWidget(wrap)

        # AI 输入行：输入框 + 可选的"AI 写歌词"按钮 + 固定的"AI 改歌词"按钮
        row = QHBoxLayout()
        row.setSpacing(6)
        self.write_enabled = write
        self.ai_input = QLineEdit()
        self.ai_input.setPlaceholderText(
            '✨ 有歌词时输入怎么改，回车交给“AI 改歌词”；歌词为空时输入主题，回车交给“AI 写歌词”'
            if write
            else '✨ 输入怎么改，回车交给大模型，例如：副歌更押韵 / 第二段主歌写童年回忆 / 整首改成英文'
        )
        self.ai_input.returnPressed.connect(self._ai_enter)
        row.addWidget(self.ai_input, 1)
        if write:
            self.write_btn = button(
                '📝 AI 写歌词',
                tooltip='按主题从零创作一首新歌词，可以顺便生成风格描述',
                callback=self.ai_write,
            )
            row.addWidget(self.write_btn)
        self.ai_btn = button(
            '✨ AI 改歌词',
            tooltip='按「设置 → 大模型 API」里的改歌词提示词和你的修改要求，改写当前歌词',
            callback=self.ai_rewrite,
        )
        row.addWidget(self.ai_btn)
        box.addLayout(row)

        # 上下文提供者与风格回填回调默认留空，由宿主页面注入
        self.context_provider = None
        self.style_setter = None

        # 正文编辑区：带占位提示、最小高度与语法高亮
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText(
            '[Verse]\n在这里写主歌歌词…\n\n[Chorus]\n在这里写副歌歌词…\n\n'
            '提示：用 [Verse] [Chorus] 等段落标签组织歌词，每行一句。'
        )
        self.edit.setMinimumHeight(220)
        self.highlighter = LyricsHighlighter(self.edit.document())
        box.addWidget(self.edit, 1)

        # 字数统计标签
        self.count = label('', 'Hint', wrap=True)
        box.addWidget(self.count)

        # 底部操作栏：示例下拉框（可选）+ 导入 + 清空
        bar = QHBoxLayout()
        if examples:
            self.examples = QComboBox()
            self.examples.addItem('载入示例…')
            for name in LYRIC_EXAMPLES:
                self.examples.addItem(name)
            self.examples.activated.connect(self._example)
            bar.addWidget(self.examples, 1)
        else:
            bar.addStretch(1)
        bar.addWidget(button('导入', 'Ghost', '从 .txt 文件导入歌词', self.import_file))
        bar.addWidget(button('清空', 'Ghost', '清空歌词（Ctrl+Z 可撤销）', self.clear_undoable))
        box.addLayout(bar)

        self.edit.textChanged.connect(self._changed)
        self._changed()

    def _changed(self):
        """内容变化时更新字数统计；无段落标签时给出提示并发出 changed 信号。"""
        text = self.edit.toPlainText()
        # 非空且不以 [ 开头的行视为有效歌词行
        lines = [line for line in text.splitlines() if line.strip() and not line.strip().startswith('[')]
        # 以 [ 开头的行视为段落标签，统计其数量
        sections = sum(1 for line in text.splitlines() if line.strip().startswith('['))
        warn = '  ·  ⚠ 建议添加 [Verse]/[Chorus] 段落标签' if text.strip() and not sections else ''
        self.count.setText(f'{len(text)} 字符 · {len(lines)} 行歌词 · {sections} 个段落{warn}')
        self.changed.emit()

    def _example(self, index):
        """选中示例（index>0）时载入对应歌词，并通过 exampleChosen 回传风格与歌词。"""
        if index <= 0:
            return
        name = self.examples.itemText(index)
        style, lyrics = LYRIC_EXAMPLES[name]
        self.edit.setPlainText(lyrics)
        self.exampleChosen.emit(style, lyrics)
        self.examples.setCurrentIndex(0)

    def insert_section(self, tag):
        """在光标处插入段落标签：自动换行定位，并保持与前后内容的空行分隔。"""
        cursor = self.edit.textCursor()
        # 当前块有内容则跳到块尾换行，否则回到块首
        if cursor.block().text().strip():
            cursor.movePosition(QTextCursor.EndOfBlock)
            cursor.insertText('\n')
        else:
            cursor.movePosition(QTextCursor.StartOfBlock)
        # 若光标前已有内容且未以空行结尾，补一个换行以分隔段落
        prefix = self.edit.toPlainText()[:cursor.position()]
        if prefix.strip() and not prefix.endswith('\n\n'):
            cursor.insertText('\n')
        cursor.insertText(f'{tag}\n')
        self.edit.setTextCursor(cursor)
        self.edit.setFocus()

    def _replace_undoable(self, text):
        """把全文替换为 text，并合并为单次可撤销操作。"""
        cursor = self.edit.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.Document)
        cursor.insertText(text)
        cursor.endEditBlock()

    def clear_undoable(self):
        """清空歌词（可撤销）。"""
        self._replace_undoable('')

    def _context(self):
        """取当前上下文（由宿主页面注入的 context_provider 提供），缺失时返回空字典。"""
        return self.context_provider() if self.context_provider else {}

    def _toast(self, text, kind):
        """向主窗口（若提供 toast 方法）发送一条提示消息。"""
        win = self.window()
        if hasattr(win, 'toast'):
            win.toast(text, kind)

    def _ai_enter(self):
        """回车触发：无歌词且支持写歌词时走"AI 写歌词"，否则走"AI 改歌词"。"""
        if self.write_enabled and not self.text().strip():
            self.ai_write()
        else:
            self.ai_rewrite()

    def ai_rewrite(self):
        """打开 AI 改歌词对话框，成功后回填歌词并清空输入。"""
        # 无歌词且不支持写歌词时仅提示，否则先尝试写歌词
        if not self.text().strip():
            if self.write_enabled:
                self.ai_write()
            else:
                self._toast('当前没有歌词：请先填写、导入或识别歌词，再让 AI 改写', 'warn')
            return
        from .ai_lyrics import AiLyricsDialog

        ctx = self._context()
        dlg = AiLyricsDialog(
            self.window(),
            self.text(),
            style=ctx.get('style', ''),
            structure=ctx.get('structure', 'off'),
            instruction=self.ai_input.text().strip(),
        )
        if dlg.exec() and dlg.result_text is not None:
            self._replace_undoable(dlg.result_text)
            self.ai_input.clear()
            self._toast('已应用 AI 修改（Ctrl+Z 可撤销）', 'ok')
        dlg.deleteLater()

    def ai_write(self):
        """打开 AI 写歌词对话框，成功后回填歌词并（可选）回传风格描述。"""
        from .ai_lyrics import AiWriteDialog

        # 已有歌词时不再传主题，否则用输入框文本作为创作主题
        theme = '' if self.text().strip() else self.ai_input.text().strip()
        dlg = AiWriteDialog(self.window(), theme=theme, style=self._context().get('style', ''))
        if dlg.exec() and dlg.result_lyrics:
            self._replace_undoable(dlg.result_lyrics)
            # 有风格结果且宿主提供了回填函数时才回传风格
            has_style = bool(dlg.result_style and self.style_setter)
            if has_style:
                self.style_setter(dlg.result_style)
            self.ai_input.clear()
            self._toast(
                '已填入 AI 写的歌词' + ('和风格描述' if has_style else '') + '（歌词可 Ctrl+Z 撤销）',
                'ok',
            )
        dlg.deleteLater()

    def import_file(self):
        """从 .txt/.lrc 文件导入歌词，剥离 LRC 元信息与时间轴标签。"""
        path, _ = QFileDialog.getOpenFileName(self, '导入歌词', '', '文本文件 (*.txt *.lrc);;所有文件 (*)')
        if path:
            data = Path(path).read_bytes()
            # 优先按 UTF-8（含 BOM）解码，失败则回退到 GB18030
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = data.decode('gb18030', errors='replace')
            # 剥离 [ti:...]/[ar:...] 等元信息行
            text = re.sub(
                r'^\s*\[(?:ti|ar|al|by|au|offset|length|re|ve|tool|#)[^\]]*\]\s*$\n?',
                '',
                text,
                flags=re.M | re.I,
            )
            # 剥离 [00:12.34] 这类时间轴标记
            text = re.sub(r'^\s*(?:\[\d+:\d+(?:[.:]\d+)?\])+', '', text, flags=re.M)
            self._replace_undoable(text)

    def text(self):
        """返回当前歌词全文。"""
        return self.edit.toPlainText()

    def setText(self, text):
        """设置歌词全文，空值按空串处理。"""
        self.edit.setPlainText(text or '')


class AbcEditor(QWidget):
    """ABC 乐谱编辑器：打开/另存为/去掉和弦工具条 + 状态统计 + 语法高亮的正文区。"""

    # 内容变化信号（无参）与文件载入信号（文件路径）
    changed = Signal()
    fileLoaded = Signal(str)

    def __init__(self, parent=None):
        """构建布局：顶部工具条与状态栏，下方为只读式语法高亮的编辑区。"""
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        # 工具条：打开、另存为、去掉和弦 + 右侧统计标签
        row = QHBoxLayout()
        row.addWidget(button('📂 打开', 'Ghost', '打开 .abc 乐谱文件', self.open_file))
        row.addWidget(button('💾 另存为', 'Ghost', '保存为 .abc 文件', self.save_file))
        row.addWidget(
            button('去掉和弦', 'Ghost', '移除 "C" "Am7" 这类和弦符号（仅旋律模式/翻唱推荐）', self.remove_chords)
        )
        row.addStretch(1)
        self.info = label('', 'Hint')
        row.addWidget(self.info)
        box.addLayout(row)

        # 正文编辑区：等宽无换行样式 + ABC 语法高亮
        self.edit = QPlainTextEdit()
        self.edit.setObjectName('Code')
        self.edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.edit.setPlaceholderText('在此粘贴、打开或编辑 ABC 乐谱…')
        self.highlighter = AbcHighlighter(self.edit.document())
        box.addWidget(self.edit, 1)

        self.edit.textChanged.connect(self._changed)
        self._changed()

    def _changed(self):
        """内容变化时统计行数、小节数（| 计数）与和弦数并更新状态栏。"""
        text = self.edit.toPlainText()
        # 忽略头部字段与注释行，其余行累加 | 数量作为小节数估算
        bars = sum(line.count('|') for line in text.splitlines() if not re.match('^[A-Za-z]:|^%', line))
        chords = count_chords(text)
        self.info.setText(
            f'{len(text.splitlines())} 行 · 约 {bars} 小节 · {chords} 个和弦符号' if text else ''
        )
        self.changed.emit()

    def text(self):
        """返回当前 ABC 乐谱全文。"""
        return self.edit.toPlainText()

    def setText(self, text):
        """设置 ABC 乐谱全文，空值按空串处理。"""
        self.edit.setPlainText(text or '')

    def remove_chords(self):
        """一键去掉和弦符号（仅保留旋律与结构）。"""
        self.edit.setPlainText(strip_chords(self.edit.toPlainText()))

    def open_file(self):
        """打开 .abc/.txt 乐谱文件并回传 fileLoaded 信号。"""
        path, _ = QFileDialog.getOpenFileName(self, '打开乐谱', '', 'ABC 乐谱 (*.abc *.txt);;所有文件 (*)')
        if path:
            self.setText(Path(path).read_text(encoding='utf-8', errors='replace'))
            self.fileLoaded.emit(path)

    def save_file(self):
        """把当前乐谱另存为 .abc 文件。"""
        path, _ = QFileDialog.getSaveFileName(self, '保存乐谱', 'score.abc', 'ABC 乐谱 (*.abc)')
        if path:
            Path(path).write_text(self.text(), encoding='utf-8')
