"""SheetSage2: music audio → lead sheet, chords, beats, key, structure.

本模块实现"扒谱分析"页面：用 SheetSage2 把歌曲录音转成可编辑的乐谱——旋律
（人声/器乐）、和弦、节拍、调性、段落结构，并导出 ABC / MIDI / 标注文件。
用户可选择论文参数以兼容读取旧结果，也可按推荐预设用重叠窗口拼接处理长歌曲。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QListWidget,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..audio_utils import AUDIO_FILTER
from ..engine import SHEETSAGE_STEPS, engine
from ..paths import open_in_explorer
from ..tasks import runner
from ..theme import C
from ..widgets.common import Card, Collapsible, DropZone, Segmented, button, form_row, label
from ..widgets.editors import AbcHighlighter
from ..widgets.player import AudioPlayer, fmt_time
from ..widgets.progress import StageProgress
from ..widgets.render_export import show_render_export
from ..widgets.score import ScoreView
from .base import BasePage

# 和弦性质缩写到可读名称的映射，用于把内部标签（如 "m"、"maj7"）转成易读的和弦名。
QUALITY = {
    '': '',
    'maj': '',
    'min': 'm',
    'm': 'm',
    'dim': 'dim',
    'aug': 'aug',
    'sus2': 'sus2',
    'sus4': 'sus4',
    'maj7': 'maj7',
    'min7': 'm7',
    'm7': 'm7',
    '7': '7',
    'dom7': '7',
    'dim7': 'dim7',
    'm7b5': 'm7b5',
    '6': '6',
    'min6': 'm6',
}

# 段落标签缩写到中文/易读名称的映射，用于在结构时间轴与表格中展示。
STRUCTURE_NAMES = {
    'intro': '前奏',
    'verse': '主歌',
    'prechorus': '导歌',
    'chorus': '副歌',
    'bridge': '桥段',
    'solo': '独奏',
    'outro': '尾奏',
    'interlude': '间奏',
    'instrumental': '器乐',
    'break': '间奏',
    'drop': '副歌',
    'hook': '副歌',
    'build': '铺垫',
    'ending': '尾奏',
}


def chord_name(label_text):
    """把内部和弦标签（如 ``C:maj/3``）转成可读和弦名（如 ``C``）。

    参数 label_text 形如 ``根音:性质/低音``；空标签或 ``N``/``X``（无和弦）
    统一返回破折号占位，避免在表格里显示原始编码。
    """
    if label_text in ('N', 'X'):
        return '—'
    root, _, quality_bass = label_text.partition(':')
    quality, _, bass = quality_bass.partition('/')
    result = root + QUALITY.get(quality, quality)
    if bass:
        return f'{result}/{bass}'
    return result


def key_name(text):
    """把调性标签（如 ``C:major``）转成可读文本（如 ``C 大调``）。

    大调/小调给出中文名，其他质量原样保留；未知质量直接显示原始文本。
    """
    root, _, quality = text.partition(':')
    return f'{root} {("大调" if quality == "major" else "小调" if quality == "minor" else quality)}'


class StructureTimeline(QWidget):
    """段落结构时间轴：横向色块展示歌曲各段落在时间轴上的分布。

    相邻且标签相同、间隔极短的段落会自动合并，避免同色块碎片化；悬停时
    用 QToolTip 显示段落名称、原始标签与起止时间。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # rows 存 (start, end, label) 三元组；duration 为整首歌时长（秒）
        self.rows = []
        self.duration = 0
        self.setMinimumHeight(46)
        self.setMouseTracking(True)

    def set_rows(self, rows, duration):
        """设置段落行数据并合并相邻同标签段，然后触发重绘。

        rows 为 (start, end, label) 列表；duration 为空时用最后一段的结束时间兜底。
        """
        out = []
        for start, end, label in rows:
            # 与上一段标签相同且时间几乎衔接（间隔 < 0.05 秒）则合并到上一段
            if out and out[-1][2] == label and abs(out[-1][1] - start) < 0.05:
                out[-1] = (out[-1][0], end, label)
                continue
            out.append((start, end, label))
        self.rows = out
        self.duration = duration or (out[-1][1] if out else 0)
        self.update()

    def _color(self, value):
        """根据段落标签生成稳定的 HSL 颜色（对标签哈希取色相）。"""
        hue = int(hashlib.md5(value.encode()).hexdigest()[:6], 16) % 360
        return QColor.fromHsl(hue, 150, 110)

    def paintEvent(self, event):
        """绘制时间轴：按起止时间比例画圆角色块，宽处显示段落名。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width = self.width()
        height = self.height()
        # 无数据时居中显示占位提示
        if not self.rows or not self.duration:
            painter.setPen(QColor(C['faint']))
            painter.drawText(self.rect(), Qt.AlignCenter, '段落结构')
            return
        for start, end, label in self.rows:
            # 起止时间按总时长比例映射到横向像素坐标
            x0 = start / self.duration * width
            x1 = end / self.duration * width
            rect = QRectF(x0 + 1, 4, max(2, x1 - x0 - 2), height - 8)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._color(label))
            painter.drawRect(rect)
            painter.setPen(QColor('white'))
            name = STRUCTURE_NAMES.get(label.lower(), label)
            # 色块足够宽才画文字，避免文字溢出
            if rect.width() > 30:
                painter.drawText(rect, Qt.AlignCenter, name)

    def mouseMoveEvent(self, event):
        """鼠标悬停时定位到对应段落并显示 ToolTip。"""
        if not self.rows or not self.duration:
            return
        # 鼠标横坐标按宽度比例换算成歌曲时间点
        t = event.position().x() / max(1, self.width()) * self.duration
        for start, end, label in self.rows:
            if start <= t <= end:
                QToolTip.showText(
                    event.globalPosition().toPoint(),
                    f'{STRUCTURE_NAMES.get(label.lower(), label)} ({label})\n{fmt_time(start)} – {fmt_time(end)}',
                    self,
                )
                return


class StatTile(QFrame):
    """概览统计小块：大号数值 + 下方标题，用于展示调性/速度等指标。"""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(12, 8, 12, 8)
        box.setSpacing(0)
        # 圆角卡片背景；数值与标题各自透明背景，避免被外层样式覆盖
        self.setStyleSheet(f'background:{C["card"]}; border:2px solid {C["border"]};')
        self.value = label('—')
        self.value.setStyleSheet('font-size:18px; font-weight:700; background:transparent;')
        cap = label(title, 'Hint')
        cap.setStyleSheet('background:transparent;')
        box.addWidget(self.value)
        box.addWidget(cap)


class TranscribePage(BasePage):
    """扒谱分析页面：SheetSage2 转录音频为旋律/和弦/节拍/调性/结构。"""

    title = '📝 扒谱分析 · SheetSage2'
    subtitle = '把歌曲录音转成可编辑的乐谱：旋律（人声/器乐）、和弦、节拍、调性、段落结构，并导出 ABC / MIDI / 标注文件。'

    def __init__(self, main):
        super().__init__(main)
        # 记录最近一次扒谱结果，以及源音频版本号（用于判断异步期间输入是否变更）
        self.last = None
        self._source_version = 0

        # 步骤 1：音频卡片，含拖放选择框与紧凑播放器
        audio_card = Card('音频', step=1)
        self.drop = DropZone('拖入歌曲音频，或点击选择', AUDIO_FILTER)
        self.player = AudioPlayer(compact=True)
        # 更换音频时清空旧结果并禁用结果按钮
        self.drop.fileChanged.connect(self._source_changed)
        audio_card.body.addWidget(self.drop)
        audio_card.body.addWidget(self.player)

        # 步骤 2：识别选项卡片
        opt_card = Card('识别选项', step=2)
        self.melody_only = QCheckBox('仅旋律乐谱（ABC 与伴奏 MIDI 中去掉和弦，用于 YuE2 翻唱）')
        opt_card.body.addWidget(self.melody_only)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        # 预设：默认用重叠窗口拼接长歌曲；论文参数用于兼容读取旧结果
        self.preset = Segmented([
            ('默认', 'default', '长歌曲使用重叠窗口拼接（推荐）'),
            ('论文参数（兼容读取）', 'paper', '固定 overlap=100 / lookahead=0；使用 FFmpeg 读取原采样率音频、声道平均后由 torchaudio 重采样，不保证与论文环境逐位一致'),
        ])
        self.dtype = Segmented([('BF16 快速', 'bf16'), ('FP32 精确', 'fp32')])
        self.max_seconds = QDoubleSpinBox()
        self.max_seconds.setRange(0, 3600)
        self.max_seconds.setDecimals(0)
        self.max_seconds.setSuffix(' 秒')
        self.max_seconds.setSpecialValueText('整首歌曲')
        grid.addWidget(form_row('预设', self.preset), 0, 0)
        grid.addWidget(form_row('计算精度', self.dtype), 0, 1)
        grid.addWidget(form_row('只分析前', self.max_seconds, '0 = 分析整首；调试时可以只分析前 N 秒'), 1, 0)
        opt_card.body.addLayout(grid)

        # 高级折叠面板：窗口参数与各类张量导出选项
        coll = Collapsible('高级：窗口与导出')
        box = QHBoxLayout()
        self.overlap = QDoubleSpinBox()
        self.overlap.setRange(0, 290)
        self.overlap.setValue(200)
        self.overlap.setSuffix(' 秒')
        self.lookahead = QDoubleSpinBox()
        self.lookahead.setRange(0, 290)
        self.lookahead.setValue(100)
        self.lookahead.setSuffix(' 秒')
        box.addWidget(form_row('窗口重叠 overlap', self.overlap, '300 秒窗口之间的重叠长度，需要 lookahead ≤ overlap < 300'))
        box.addWidget(form_row('右侧上下文 lookahead', self.lookahead))
        coll.body.addLayout(box)
        self.export_logits = QCheckBox('导出 logits')
        self.export_scores = QCheckBox('导出 token 分数')
        self.export_embeddings = QCheckBox('导出音频/解码器特征')
        self.all_layers = QCheckBox('导出 MERT 全部 24 层特征')
        grid2 = QGridLayout()
        # 四个复选框按 2×2 网格排布
        for i, cb in enumerate((self.export_logits, self.export_scores, self.export_embeddings, self.all_layers)):
            grid2.addWidget(cb, i // 2, i % 2)
        coll.body.addLayout(grid2)
        coll.body.addWidget(label('导出的张量保存为 safetensors，位于输出目录的 tensors/ 下。', 'Hint'))
        opt_card.body.addWidget(coll)

        self.go_btn = self.run_button('🎼 开始扒谱', self.run)
        bar = self.action_bar(None, self.stop_button(), self.go_btn)

        # 右侧结果区：进度条 + 概览卡片（统计块 + 结构时间轴 + 按钮）+ 标签页
        right = QWidget()
        box = QVBoxLayout(right)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        self.progress = StageProgress(SHEETSAGE_STEPS)
        box.addWidget(self.progress)

        overview = Card('分析概览')
        tiles_row = QHBoxLayout()
        tiles_row.setSpacing(8)
        self.tiles = {}
        # 五个统计小块：调性、速度、拍号、时长、音符数（人声/器乐）
        for key, caption in (('key', '调性'), ('bpm', '速度 BPM'), ('meter', '拍号'), ('duration', '时长'), ('notes', '音符 人声/器乐')):
            tile = StatTile(caption)
            self.tiles[key] = tile
            tiles_row.addWidget(tile)
        overview.body.addLayout(tiles_row)
        self.timeline = StructureTimeline()
        overview.body.addWidget(self.timeline)

        # 结果操作按钮组：发送到翻唱/编辑页、打开输出文件夹
        btns = QHBoxLayout()
        self.to_cover = button('🎙️ 发送到 AI 翻唱', callback=self.send_cover)
        self.to_edit = button('🎼 在乐谱编辑中打开', callback=self.send_edit)
        self.open_dir = button('📂 打开输出文件夹', callback=lambda: self.last and open_in_explorer(self.last['dir']))
        for b in (self.to_cover, self.to_edit, self.open_dir):
            b.setEnabled(False)
            btns.addWidget(b)
        btns.addStretch(1)
        overview.body.addLayout(btns)

        # 导出乐谱/钢琴 WAV 按钮行（无需重新扒谱）
        row2 = QHBoxLayout()
        self.render_export = button('📄 导出乐谱 / 钢琴 WAV', callback=self.export_render)
        row2.addWidget(self.render_export)
        row2.addWidget(label('可选择已有结果，无需重新扒谱', 'Hint'))
        row2.addStretch(1)
        overview.body.addLayout(row2)
        box.addWidget(overview)

        # 标签页：乐谱 / ABC / 和弦进行 / 段落结构 / 输出文件 / 诊断信息
        self.tabs = QTabWidget()
        self.score = ScoreView()
        self.tabs.addTab(self.score, '🎼 乐谱')
        self.abc_text = QPlainTextEdit()
        self.abc_text.setObjectName('Code')
        self.abc_text.setReadOnly(True)
        self.abc_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._hl = AbcHighlighter(self.abc_text.document())
        self.tabs.addTab(self.abc_text, 'ABC')
        self.chords = self._table(['开始', '结束', '和弦', '原始标签'])
        self.tabs.addTab(self.chords, '和弦进行')
        self.structure = self._table(['开始', '结束', '段落'])
        self.tabs.addTab(self.structure, '段落结构')
        self.files = QListWidget()
        self.files.itemDoubleClicked.connect(self._open_file)
        self.tabs.addTab(self.files, '输出文件')
        self.diag = QPlainTextEdit()
        self.diag.setObjectName('Code')
        self.diag.setReadOnly(True)
        self.tabs.addTab(self.diag, '诊断信息')
        box.addWidget(self.tabs, 1)

        # 左右两栏布局：左侧表单（音频 + 选项），右侧结果区
        self.two_columns([audio_card, opt_card], right, bar, sizes=(460, 740))

    def _source_changed(self, path):
        """源音频被更换：递增版本号、重置播放器与所有结果显示。"""
        self._source_version += 1
        self.player.set_source(path)
        self.last = None
        for b in (self.to_cover, self.to_edit, self.open_dir):
            b.setEnabled(False)
        self.score.set_abc('')
        self.abc_text.clear()
        self.chords.setRowCount(0)
        self.structure.setRowCount(0)
        self.files.clear()
        self.diag.clear()
        self.timeline.set_rows([], 0)
        # 各统计小块数值复位为破折号占位
        for tile in self.tiles.values():
            tile.value.setText('—')

    @staticmethod
    def _table(headers):
        """构造统一的只读表格：横向拉伸、整行选中、交替行颜色。"""
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        return table

    def run(self):
        """开始扒谱：校验输入后提交后台任务，识别期间源变更则只保存结果。"""
        if not self.ensure_idle():
            return
        if not self.drop.path or not Path(self.drop.path).exists():
            self.warn('请先选择音频文件。')
            return
        # 默认预设要求窗口参数满足 lookahead ≤ overlap < 300
        if self.preset.value() == 'default' and not (self.lookahead.value() <= self.overlap.value() < 300):
            self.warn('窗口参数需要满足 lookahead ≤ overlap < 300。')
            return
        # 汇总扒谱参数：音频路径、旋律模式、预设、精度、时长与导出选项
        params = {
            'audio': self.drop.path,
            'melody_only': self.melody_only.isChecked(),
            'preset': self.preset.value(),
            'dtype': self.dtype.value(),
            'max_seconds': self.max_seconds.value() or None,
            'overlap': self.overlap.value(),
            'lookahead': self.lookahead.value(),
            'export_logits': self.export_logits.isChecked(),
            'export_scores': self.export_scores.isChecked(),
            'export_embeddings': self.export_embeddings.isChecked(),
            'all_layers': self.all_layers.isChecked(),
        }
        self.progress.start('SheetSage2 扒谱中…')
        # 记录提交时的源版本号，用于完成后判断源是否在识别期间被更换
        before = self._source_version

        def done(result):
            # 识别期间源音频被更换：只保存结果，不覆盖当前显示
            if self.drop.path != params['audio'] or self._source_version != before:
                self.progress.finish(True, f"源歌曲已更换，扒谱结果未填入；已保存到 {result['dir']}")
                self.toast('源歌曲已更换，扒谱结果已保存在输出目录', 'warn')
                return
            self._done(result)

        runner.submit(
            'SheetSage2 扒谱',
            engine.run_transcribe,
            params,
            on_done=done,
            on_error=self._error,
            on_progress=self.progress.update_info,
        )

    def _done(self, result):
        """扒谱成功：记录结果并刷新概览统计块、结构时间轴与各标签页。"""
        self.last = result
        has_abc = bool(result.get('abc'))
        self.progress.finish(True, f"完成 · 用时 {result.get('elapsed') or 0:.1f}s" + ('' if has_abc else ' · 未能生成 ABC'))

        # 调性统计块：去重后取前两个调性，转成可读名用 "/" 连接
        keys = []
        for _, _, key in result['keys']:
            if key not in keys:
                keys.append(key)
        self.tiles['key'].value.setText(' / '.join(key_name(k) for k in keys[:2]) or '—')
        self.tiles['bpm'].value.setText(f"{result['bpm']:.0f}" if result.get('bpm') else '—')
        self.tiles['meter'].value.setText(result.get('meter') or '—')
        self.tiles['duration'].value.setText(fmt_time(result.get('duration')))
        self.tiles['notes'].value.setText(f"{result.get('vocal_notes') or 0} / {result.get('instrumental_notes') or 0}")

        # 结构时间轴与乐谱显示
        self.timeline.set_rows(result['structure'], result.get('duration'))
        self.score.set_abc(result.get('abc') or '')
        self.abc_text.setPlainText(result.get('abc') or f"未生成 ABC：{result.get('abc_error') or result.get('error')}")

        # 和弦进行表：把内部标签转成可读和弦名，最后一列保留原始标签
        self._fill(self.chords, [(fmt_time(s), fmt_time(e), chord_name(c), c) for s, e, c in result['chords']])
        # 段落结构表：显示中文名 + 原始标签
        self._fill(self.structure, [(fmt_time(s), fmt_time(e), f"{STRUCTURE_NAMES.get(l.lower(), l)} ({l})") for s, e, l in result['structure']])

        self.files.clear()
        self.files.addItems(result['files'])

        # 汇总诊断信息：错误、警告、乐谱诊断、峰值显存与输出目录
        lines = []
        if result.get('error'):
            lines.append('错误: ' + result['error'])
        if result.get('abc_error'):
            lines.append('ABC 错误: ' + result['abc_error'])
        lines += ['警告:'] + (result['warnings'] or ['无']) + ['', '乐谱诊断:'] + (result['diagnostics'] or ['无'])
        lines += ['', f"峰值显存: {result.get('peak_gpu_mib') or 0:.0f} MiB", f"输出目录: {result['dir']}"]
        self.diag.setPlainText('\n'.join(lines))

        # 有乐谱才启用发送到翻唱/编辑页；打开输出文件夹始终可用
        for b in (self.to_cover, self.to_edit):
            b.setEnabled(has_abc)
        self.open_dir.setEnabled(True)
        self.toast('扒谱完成' if has_abc else '扒谱完成，但没有生成 ABC 乐谱', 'ok' if has_abc else 'warn')

    @staticmethod
    def _fill(table, rows):
        """把二维行数据填充到表格，数值统一转成文本。"""
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(str(value)))

    def _error(self, message, trace):
        """扒谱失败：结束进度条；非取消场景弹出带堆栈的错误对话框。"""
        self.progress.finish(False, message.splitlines()[0][:160])
        if message != '已取消':
            self.main.show_error('扒谱失败', message, trace)

    def _open_file(self, item):
        """双击输出文件条目时，用系统默认程序打开该文件。"""
        if self.last:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.last['dir']) / item.text())))

    def send_cover(self):
        """把扒谱结果送到 AI 翻唱页继续处理。"""
        if self.last:
            self.main.open_in_cover(self.last)

    def send_edit(self):
        """把扒谱结果（含 ABC 乐谱）送入乐谱编辑页，保留输入不覆盖用户当前内容。"""
        if self.last:
            self.main.open_in_editor({
                'abc': self.last['abc'],
                'meta': {'cot': 'melody' if self.last.get('melody_only') else 'full'},
                'dir': self.last['dir'],
                'keep_inputs': True,
            })

    def export_render(self):
        """打开乐谱渲染导出面板（图片/PDF/钢琴 WAV），已有结果则直接复用其目录。"""
        show_render_export(self, source_dir=self.last.get('dir') if self.last else None)

    def primary_action(self):
        """Ctrl+Enter 触发的默认操作：开始扒谱。"""
        self.run()
