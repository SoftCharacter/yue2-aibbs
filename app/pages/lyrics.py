"""Lyrics recognition: Qwen3-ASR + Qwen3-ForcedAligner → timed lines → YuE2 lyrics / LRC.

本模块实现"歌词识别"页面：用户拖入歌曲音频后，用 Qwen3-ASR-1.7B 从人声中识别
歌词（支持带伴奏歌曲与多种语言方言），再用 Qwen3-ForcedAligner-0.6B 给每个字
打上时间戳，据此自动断句、按原曲段落加 [Verse]/[Chorus] 标签并导出 LRC。
识别结果可编辑，也可一键送到 AI 翻唱页或创作页继续使用。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..audio_utils import AUDIO_FILTER
from ..engine import LYRICS_STEPS, engine
from ..lyrics_utils import LANGUAGES, build_lines, format_yue2, to_lrc
from ..paths import open_in_explorer
from ..tasks import runner
from ..widgets.common import Card, Collapsible, DropZone, Segmented, button, form_row, label
from ..widgets.editors import LyricsEditor
from ..widgets.player import AudioPlayer, fmt_time
from ..widgets.progress import StageProgress
from .base import BasePage


class LyricsPage(BasePage):
    """歌词识别页面：Qwen3-ASR 识别歌词 + 时间戳对齐，可编辑后送翻唱/创作页。"""

    title = '🎤 歌词识别 · Qwen3-ASR'
    subtitle = 'Qwen3-ASR-1.7B 从歌曲人声中识别歌词（支持带伴奏的歌曲、30 种语言与 22 种方言）；Qwen3-ForcedAligner-0.6B 给每个字打上时间戳，用来自动断句、按原曲段落加 [Verse]/[Chorus] 标签、导出 LRC。'

    def __init__(self, main):
        super().__init__(main)
        # 记录最近一次识别结果，以及源音频的版本号（用于判断异步识别期间输入是否变更）
        self.last = None
        self._source_version = 0

        # 步骤 1：歌曲音频卡片，含拖放选择框与紧凑播放器
        audio_card = Card('歌曲音频', step=1)
        self.drop = DropZone('拖入歌曲音频，或点击选择', AUDIO_FILTER)
        self.player = AudioPlayer(compact=True)
        # 更换音频时清空旧识别结果并禁用结果按钮
        self.drop.fileChanged.connect(self._source_changed)
        audio_card.body.addWidget(self.drop)
        audio_card.body.addWidget(self.player)

        # 步骤 2：识别选项卡片
        opt_card = Card('识别选项', step=2)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        # 演唱语言下拉框：遍历 LANGUAGES 填充（显示名 + 语言代码）
        self.language = QComboBox()
        for text, code in LANGUAGES:
            self.language.addItem(text, code)
        self.context = QLineEdit()
        self.context.setPlaceholderText('可选：歌名、歌手、专有名词…')
        grid.addWidget(form_row('演唱语言', self.language, '指定语言可避免误判；自动识别也支持中英混唱'), 0, 0)
        grid.addWidget(form_row('提示信息', self.context, '给模型的上下文，写上歌名 / 人名 / 生僻词能提高准确率'), 0, 1)
        opt_card.body.addLayout(grid)

        # 逐字时间戳对齐开关，默认开启；段落分段控件仅在对齐开启时可用
        self.align = QCheckBox('逐字时间戳对齐（Qwen3-ForcedAligner，用于断句、分段、LRC）')
        self.align.setChecked(True)
        opt_card.body.addWidget(self.align)

        self.sections = Segmented(
            [
                ('按停顿分段', 'none', '长时间停顿处分段，统一标为 [Verse]'),
                ('用 SheetSage2 识别段落（推荐）', 'sheetsage', '先分析原曲的前奏/主歌/副歌结构，再给歌词加对应标签'),
            ],
            'sheetsage',
        )
        opt_card.body.addWidget(form_row('段落标签', self.sections))
        self.align.toggled.connect(self.sections.setEnabled)

        # 断句参数折叠面板：停顿阈值与每行最大宽度
        coll = Collapsible('断句参数（识别后调整会立即重新排版）')
        box = QHBoxLayout()
        self.gap = QDoubleSpinBox()
        self.gap.setRange(0.1, 5)
        self.gap.setSingleStep(0.1)
        self.gap.setValue(0.6)
        self.gap.setSuffix(' 秒')
        self.max_width = QSpinBox()
        self.max_width.setRange(6, 60)
        self.max_width.setValue(22)
        self.max_width.setSuffix(' 字')
        box.addWidget(form_row('停顿超过此值换行', self.gap))
        box.addWidget(form_row('每行最多（英文单词算 2）', self.max_width))
        coll.body.addLayout(box)
        opt_card.body.addWidget(coll)

        # 调整断句参数时触发即时重新排版
        self.gap.valueChanged.connect(self.reflow)
        self.max_width.valueChanged.connect(self.reflow)

        self.go_btn = self.run_button('🎤 识别歌词', self.run)
        bar = self.action_bar(None, self.stop_button(), self.go_btn)

        # 右侧结果区：进度条 + 识别结果卡片（操作按钮组）+ 标签页（歌词/时间轴/LRC/原文）
        right = QWidget()
        box = QVBoxLayout(right)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        self.progress = StageProgress(LYRICS_STEPS)
        box.addWidget(self.progress)
        result_card = Card('识别结果')
        self.summary = label('识别后可直接修改歌词，再发送到 AI 翻唱或创作页。', 'Hint', wrap=True)
        result_card.body.addWidget(self.summary)
        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.to_cover = button('🎙️ 发送到 AI 翻唱', callback=self.send_cover)
        self.to_create = button('🎵 发送到创作页', callback=self.send_create)
        self.copy_btn = button('📋 复制歌词', callback=lambda: QGuiApplication.clipboard().setText(self.editor.text()))
        self.save_btn = button('💾 保存…', callback=self.save)
        self.open_btn = button('📂 输出文件夹', callback=lambda: self.last and open_in_explorer(self.last['dir']))
        # 结果按钮初始禁用，识别完成后才启用
        self.result_buttons = [self.to_cover, self.to_create, self.copy_btn, self.save_btn, self.open_btn]
        for b in self.result_buttons:
            b.setEnabled(False)
            btns.addWidget(b)
        btns.addStretch(1)
        result_card.body.addLayout(btns)
        box.addWidget(result_card)

        # 标签页：可编辑歌词 / 逐行时间轴 / LRC 预览 / 原始识别文本
        self.tabs = QTabWidget()
        self.editor = LyricsEditor(examples=False)
        # 歌词编辑器上下文：无风格，结构按段落标签组织
        self.editor.context_provider = lambda: {'style': '', 'structure': 'units'}
        self.tabs.addTab(self.editor, 'YuE2 歌词（可编辑）')

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['开始', '结束', '歌词行（双击跳转播放）'])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(self._seek)
        self.tabs.addTab(self.table, '逐行时间轴')

        self.lrc = QPlainTextEdit()
        self.lrc.setObjectName('Code')
        self.lrc.setReadOnly(True)
        self.tabs.addTab(self.lrc, 'LRC')

        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.tabs.addTab(self.raw, '原始识别文本')

        box.addWidget(self.tabs, 1)
        # 歌词编辑变化时同步刷新时间轴与 LRC 预览
        self.editor.changed.connect(self._sync_timed_views)

        # 左右两栏布局：左侧表单（音频 + 选项），右侧结果区
        self.two_columns([audio_card, opt_card], right, bar, sizes=(460, 740))

    def _source_changed(self, path):
        """源音频被更换：递增版本号、重置播放器与结果，保留编辑框文字。"""
        self._source_version += 1
        self.player.set_source(path)
        self.last = None
        self.table.setRowCount(0)
        self.lrc.clear()
        self.raw.clear()
        self.summary.setText('音频已更换，请重新识别；编辑框中的文字已保留。')
        for b in self.result_buttons:
            b.setEnabled(False)

    def run(self):
        """开始识别歌词：校验输入后提交后台任务，识别期间若输入变更则只保存结果。"""
        if not self.ensure_idle():
            return
        if not self.drop.path or not Path(self.drop.path).exists():
            self.warn('请先选择歌曲音频。')
            return
        # 汇总识别参数：音频路径、语言、上下文、对齐与断句选项
        params = {
            'audio': self.drop.path,
            'language': self.language.currentData(),
            'context': self.context.text().strip(),
            'align': self.align.isChecked(),
            'sections': self.sections.value() if self.align.isChecked() else 'none',
            'gap': self.gap.value(),
            'max_width': self.max_width.value(),
        }
        self.progress.start('歌词识别中…')
        # 记录提交时的源版本与编辑器修订号，用于识别完成后判断输入是否被改动
        before = (self._source_version, self.editor.edit.document().revision())

        def done(result):
            # 识别期间源音频或歌词被修改：只保存结果，不覆盖当前编辑
            if self.drop.path != params['audio'] or before != (self._source_version, self.editor.edit.document().revision()):
                self.progress.finish(True, f"输入已修改，识别结果未填入；已保存到 {result['dir']}")
                self.toast('源歌曲或歌词已修改，当前修改已保留', 'warn')
                return
            self._done(result)

        runner.submit(
            '识别歌词',
            engine.run_lyrics,
            params,
            on_done=done,
            on_error=self._error,
            on_progress=self.progress.update_info,
        )

    def _done(self, result):
        """识别成功：记录结果、结束进度条并刷新各显示区。"""
        self.last = result
        self.progress.finish(True, f"完成 · 识别用时 {result['seconds']:.1f}s")
        self.show_result(result)
        self.toast('歌词识别完成，请检查并修改', 'ok')

    def show_result(self, result):
        """把识别结果回填到编辑器、原文框、摘要栏，并启用结果按钮。"""
        self._generated_lyrics = result['lyrics']
        self.editor.setText(result['lyrics'])
        self.raw.setPlainText(result['text'])
        # 分段方式：有 transcription 为 SheetSage2 段落，有 items 为停顿分段，否则标点分行
        mode = 'SheetSage2 段落' if result.get('transcription') else ('停顿分段' if result['items'] else '标点分行')
        self.summary.setText(
            f"语言：{result['language'] or '未知'} · 时长 {fmt_time(result['duration'])} · {len(result['lines'])} 行 · {mode} · 输出 {result['dir']}\n提示：ASR 可能有同音字错误，请对照原曲检查；段落标签需与原曲结构一致，翻唱效果最好。"
        )
        for b in self.result_buttons:
            b.setEnabled(True)

    def edited_timed_lines(self):
        """Pair the editor's lyric lines with the recognised timestamps.

        Returns (lines, "") when every edited line can reuse its timestamp, else (None, reason).
        Text corrections are carried over; adding or removing lines makes the timing unsafe.
        """
        if not self.last:
            return (None, '还没有识别结果')
        lines = self.last['lines']
        # 未做逐字对齐或存在无时间戳的行，无法生成 LRC
        if not self.last['items'] or any(line['start'] is None for line in lines):
            return (None, '未开启逐字时间戳对齐，没有时间轴，无法生成 LRC')
        # 过滤掉空行与 [标签] 行，得到用户编辑后的有效歌词行
        edited = [
            ln.strip()
            for ln in self.editor.text().splitlines()
            if ln.strip() and not (ln.strip().startswith('[') and ln.strip().endswith(']'))
        ]
        # 编辑后行数与时间轴行数不一致，说明增删了行，无法安全复用时间戳
        if len(edited) != len(lines):
            return (None, f'编辑后的歌词有 {len(edited)} 行，识别时间轴有 {len(lines)} 行，行数不一致，无法安全复用时间戳。修改文字时请保持行数不变，或重新识别后再导出 LRC')
        # 逐行用原时间戳覆盖用户改过的文字
        return [dict(ts, text=ln) for ts, ln in zip(lines, edited)], ''

    def _sync_timed_views(self):
        """歌词编辑变化时同步刷新 LRC 预览与逐行时间轴表格。"""
        if not self.last:
            return
        lines, reason = self.edited_timed_lines()
        if lines is None:
            # 无法复用时间戳时，显示原因并退回原始时间轴
            self.lrc.setPlainText(f'⚠ {reason}')
            self._fill_table(self.last['lines'])
        else:
            self.lrc.setPlainText(to_lrc(lines))
            self._fill_table(lines)

    def _fill_table(self, lines):
        """把带时间戳的歌词行填充到逐行时间轴表格，首列存 start 供双击跳转。"""
        self.table.setRowCount(len(lines))
        for row, line in enumerate(lines):
            # 起止时间格式化为 mm:ss.t；无时间戳则显示破折号
            start = '—' if line['start'] is None else f"{fmt_time(line['start'])}.{int(line['start'] % 1 * 10)}"
            end = '—' if line['end'] is None else f"{fmt_time(line['end'])}.{int(line['end'] % 1 * 10)}"
            text = line['text']
            cells = (start, end, text)
            for col, value in enumerate(cells):
                item = QTableWidgetItem(value)
                # 用 UserRole 存起始时间，双击行时据此定位播放
                item.setData(Qt.UserRole, line['start'])
                self.table.setItem(row, col, item)

    def reflow(self):
        """断句参数变化时按新参数重新排版；若歌词已被手动修改则放弃重排以免覆盖。"""
        # 无识别结果或未做逐字对齐时无法重新排版
        if not self.last or not self.last['items']:
            return
        # 编辑框内容与识别结果不一致，说明用户已手动改过，避免覆盖
        if self.editor.text() != getattr(self, '_generated_lyrics', None):
            self.summary.setText('⚠ 歌词已手动修改，为避免覆盖你的修改，没有按新的断句参数重新排版。按 Ctrl+Z 撤销到识别结果后再调整参数即可重新排版。')
            return
        # 按新参数重建行、YuE2 歌词与 LRC，再刷新显示
        lines = build_lines(self.last['text'], self.last['items'], gap=self.gap.value(), max_width=self.max_width.value())
        self.last['lines'] = lines
        self.last['lyrics'] = format_yue2(lines, self.last['structure'] or None)
        self.last['lrc'] = to_lrc(lines)
        self.show_result(self.last)

    def _seek(self, row, _col):
        """双击时间轴行时跳转到对应歌词的起始时间并开始播放。"""
        start = self.table.item(row, 0).data(Qt.UserRole)
        if start is not None and self.player.path:
            # 稍提前 0.2 秒定位（毫秒为单位），避免切到前一个词的尾音
            self.player.player.setPosition(int(max(0, start - 0.2) * 1000))
            if self.player.player.playbackState() != self.player.player.PlaybackState.PlayingState:
                self.player.toggle()

    def _error(self, message, trace):
        """识别失败：结束进度条；非取消场景弹出带堆栈的错误对话框。"""
        self.progress.finish(False, message.splitlines()[0][:160])
        if message != '已取消':
            self.main.show_error('歌词识别失败', message, trace)

    def save(self):
        """保存歌词：按文件扩展名导出 YuE2 歌词（.txt）或 LRC（.lrc）。"""
        if not self.last:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            '保存歌词',
            str(Path(self.last['dir']) / 'lyrics.txt'),
            'YuE2 歌词 (*.txt);;LRC 歌词 (*.lrc)',
        )
        if not path:
            return
        if path.lower().endswith('.lrc'):
            # 导出 LRC 需要可复用的时间戳，否则提示原因
            lines, reason = self.edited_timed_lines()
            if lines is None:
                self.warn(f'无法导出 LRC：{reason}')
                return
            content = to_lrc(lines)
        else:
            content = self.editor.text()
        Path(path).write_text(content, encoding='utf-8')
        self.toast(f'已保存 {Path(path).name}', 'ok')

    def send_cover(self):
        """把编辑后的歌词送到 AI 翻唱页，并同步原曲音频或旋律乐谱。"""
        if not self.last:
            return
        page = self.main.pages['cover']
        page.lyrics._replace_undoable(self.editor.text())
        page.lyrics_source = self.last['audio']
        transcription = self.last.get('transcription')
        suffix = ''
        # 有 SheetSage2 旋律乐谱则直接载入；否则切换原曲后提示重新扒取旋律
        if transcription and transcription.get('abc'):
            page.load_transcription(transcription)
            suffix = '（含 SheetSage2 旋律乐谱）'
        elif page.drop.path != self.last['audio']:
            page.drop.setPath(self.last['audio'])
            if page.abc.text().strip():
                suffix = '，原曲已切换，请重新“扒取旋律”'
        self.main.go('cover')
        self.toast('歌词已送到 AI 翻唱' + suffix, 'ok')

    def send_create(self):
        """把编辑后的歌词送到创作页。"""
        if self.last:
            self.main.pages['create'].lyrics._replace_undoable(self.editor.text())
            self.main.go('create')
            self.toast('歌词已送到创作页', 'ok')

    def primary_action(self):
        """Ctrl+Enter 触发的默认操作：开始识别歌词。"""
        self.run()
