"""MERT-v2 music representations: feature extraction, structure map, song similarity.

本模块实现"音乐特征"页面：用 MERT-v2 提取 1024 维音乐表征（25 帧/秒，24 层），
可视化歌曲的自相似结构图，比较多首歌的两两相似度，并导出 .npy 特征供下游任务使用。
用户可拖入多个音频文件或整个文件夹，选择模型变体、输出层与计算精度后批量提取。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QListWidget,
    QListWidgetItem,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..audio_utils import AUDIO_EXTS, AUDIO_FILTER
from ..engine import MERT_LAYER_GUIDE, MERT_STEPS, engine
from ..paths import open_in_explorer
from ..tasks import runner
from ..widgets.common import Card, Segmented, button, form_row, label
from ..widgets.heatmap import HeatmapView, colormap
from ..widgets.player import fmt_time
from ..widgets.progress import StageProgress
from .base import BasePage

# 两种 MERT-v2 模型变体的说明文案，随 Segmented 切换显示在模型卡片下方
VARIANT_INFO = {
    '30s': 'MERT-v2-30s：30 秒上下文，MARBLE 通用理解任务整体最强（流派 91.7、标签、情绪）。长音频按 30 秒切片分别编码后拼接。',
    'FullSong': 'MERT-v2-FullSong：在 30s 版本基础上继续训练到 30–360 秒整首歌上下文，适合整首歌的结构 / 调性分析，SheetSage2 的主干。',
}


class FileList(QListWidget):
    """支持拖拽添加音频文件/文件夹的列表控件，条目数据保存文件绝对路径。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 开启拖放并允许多选，方便批量移除
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setMinimumHeight(150)

    def dragEnterEvent(self, event):
        """拖入的 MIME 数据含 URL（文件）时才接受放置。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        """拖拽移动阶段直接接受，交由 dropEvent 处理实际添加。"""
        event.acceptProposedAction()

    def dropEvent(self, event):
        """把拖入的本地文件路径交给 add_paths 处理。"""
        self.add_paths(url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile())

    def add_paths(self, paths):
        """把一批文件或文件夹路径加入列表；文件夹递归收集其中所有音频文件。"""
        # 用已有路径集合去重，避免重复添加同一文件
        seen = set(self.paths())
        for path in paths:
            p = Path(path)
            # 文件夹递归枚举所有音频文件（按路径排序保证顺序稳定），单个文件则直接处理
            files = sorted(f for f in p.rglob('*') if f.suffix.lower() in AUDIO_EXTS) if p.is_dir() else [p]
            for f in files:
                # 跳过已存在或已失效的文件
                if str(f) in seen or not f.is_file():
                    continue
                # 条目显示文件名与其所在父目录名，完整路径存到 UserRole 供后续读取
                item = QListWidgetItem(f'🎵 {f.name}   ·  {f.parent.name}')
                item.setToolTip(str(f))
                item.setData(Qt.UserRole, str(f))
                self.addItem(item)
                seen.add(str(f))

    def paths(self):
        """返回当前列表全部条目存储的文件绝对路径。"""
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]

    def paintEvent(self, event):
        """列表为空时在视口中央绘制拖放提示文字。"""
        super().paintEvent(event)
        if self.count() == 0:
            # 方法内动态导入主题色，避免模块级循环依赖
            from ..theme import C

            painter = QPainter(self.viewport())
            painter.setPen(QColor(C['faint']))
            painter.drawText(self.viewport().rect(), Qt.AlignCenter, '🎵 拖入音频文件或文件夹\n或点击下方“添加文件”')


class MertPage(BasePage):
    """音乐特征页面：MERT-v2 提取特征、可视化结构与相似度、导出 .npy。"""

    title = '🧬 音乐特征 · MERT-v2'
    subtitle = '用 MERT-v2 提取 1024 维音乐表征（25 帧/秒，24 层），可视化歌曲结构、比较多首歌的相似度，并导出 .npy 供下游任务使用。'

    def __init__(self, main):
        super().__init__(main)
        # 记录最近一次提取结果，供自相似图切换与打开输出文件夹使用
        self.last = None

        # 步骤 1：模型卡片，选择 MERT-v2 变体并显示对应说明
        model_card = Card('模型', step=1)
        self.variant = Segmented([('MERT-v2-30s', '30s'), ('MERT-v2-FullSong', 'FullSong')], 'FullSong')
        self.variant_info = label('', 'Hint', wrap=True)
        self.variant.changed.connect(self._variant)
        model_card.body.addWidget(self.variant)
        model_card.body.addWidget(self.variant_info)

        # 步骤 2：音频文件卡片，拖放列表 + 增删按钮
        audio_card = Card('音频文件', '可拖入多个文件或整个文件夹；多首歌会计算两两相似度。', step=2)
        self.files = FileList(None)
        audio_card.body.addWidget(self.files)
        box = QHBoxLayout()
        box.addWidget(button('＋ 添加文件', callback=self.add_files))
        box.addWidget(button('＋ 添加文件夹', callback=self.add_folder))
        box.addStretch(1)
        box.addWidget(button('移除所选', 'Ghost', callback=self.remove_selected))
        box.addWidget(button('清空', 'Ghost', callback=self.files.clear))
        audio_card.body.addLayout(box)

        # 步骤 3：特征设置卡片，选择输出层、处理时长与计算精度
        feat_card = Card('特征设置', step=3)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        # 输出层下拉框：最后一层（索引 0）+ 第 1~24 层（数据即层号，与 L 层号一致）
        self.layer = QComboBox()
        self.layer.addItem('最后一层（默认）', 0)
        for i in range(1, 25):
            self.layer.addItem(f'第 {i} 层 (L{i})', i)
        # 下游任务快速选择下拉框，选中后按官方推荐自动设置层
        self.task = QComboBox()
        self.task.addItem('按下游任务推荐层…', None)
        for name, *_ in MERT_LAYER_GUIDE:
            self.task.addItem(name, name)
        self.task.activated.connect(self._recommend)
        grid.addWidget(form_row('输出帧特征的层', self.layer, '帧级特征与歌曲向量取自该层；各层平均向量总是包含全部 24 层'), 0, 0)
        grid.addWidget(form_row('快速选择', self.task), 0, 1)
        # 只处理前 N 秒（0 表示整首）与计算精度
        self.max_seconds = QDoubleSpinBox()
        self.max_seconds.setRange(0, 3600)
        self.max_seconds.setDecimals(0)
        self.max_seconds.setSuffix(' 秒')
        self.max_seconds.setSpecialValueText('整首')
        self.dtype = Segmented([('BF16 快速', 'bf16'), ('FP32 精确', 'fp32')])
        grid.addWidget(form_row('只处理前', self.max_seconds), 1, 0)
        grid.addWidget(form_row('计算精度', self.dtype), 1, 1)
        feat_card.body.addLayout(grid)
        # 可选保存项：帧级特征 / 各层平均向量；歌曲级向量总会保存
        self.save_frames = QCheckBox('保存帧级特征 frames.npy  [帧数, 1024]')
        self.save_frames.setChecked(True)
        self.save_layers = QCheckBox('保存各层平均向量 layers.npy  [24, 1024]')
        self.save_layers.setChecked(True)
        feat_card.body.addWidget(self.save_frames)
        feat_card.body.addWidget(self.save_layers)
        feat_card.body.addWidget(label('歌曲级向量 embedding.npy [1024]（所选层的帧平均）总会保存。', 'Hint'))

        self.go_btn = self.run_button('🧬 提取特征', self.run)
        bar = self.action_bar(None, self.stop_button(), self.go_btn)

        # 右侧结果区：进度条 + 标签页（结构图 / 相似度 / 输出文件 / 层选择指南）
        right = QWidget()
        box = QVBoxLayout(right)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        self.progress = StageProgress(MERT_STEPS)
        box.addWidget(self.progress)
        self.tabs = QTabWidget()

        # 标签页 1：结构自相似图，顶部歌曲下拉框 + 打开输出文件夹按钮
        ssm_page = QWidget()
        ssm_box = QVBoxLayout(ssm_page)
        ssm_box.setContentsMargins(0, 8, 0, 0)
        top = QHBoxLayout()
        self.ssm_file = QComboBox()
        self.ssm_file.currentIndexChanged.connect(self._show_ssm)
        top.addWidget(label('歌曲'))
        top.addWidget(self.ssm_file, 1)
        self.open_btn = button('📂 打开输出文件夹', callback=lambda: self.last and open_in_explorer(self.last['dir']))
        self.open_btn.setEnabled(False)
        top.addWidget(self.open_btn)
        ssm_box.addLayout(top)
        self.heatmap = HeatmapView()
        ssm_box.addWidget(self.heatmap, 1)
        ssm_box.addWidget(label('自相似矩阵：横纵轴都是时间，亮色表示两个时刻听起来相似。对角线外的亮色斜线/方块通常对应重复的副歌或段落。', 'Hint', wrap=True))
        self.tabs.addTab(ssm_page, '结构自相似图')

        # 标签页 2：歌曲相似度矩阵
        self.sim = QTableWidget()
        self.sim.setEditTriggers(QAbstractItemView.NoEditTriggers)
        sim_page = QWidget()
        sim_box = QVBoxLayout(sim_page)
        sim_box.setContentsMargins(0, 8, 0, 0)
        sim_box.addWidget(self.sim, 1)
        sim_box.addWidget(label('歌曲向量的余弦相似度（1 = 完全相同）。需要至少两首歌。', 'Hint'))
        self.tabs.addTab(sim_page, '歌曲相似度')

        # 标签页 3：输出文件列表，双击打开对应文件
        self.outputs = QListWidget()
        self.outputs.itemDoubleClicked.connect(self._open_output)
        self.tabs.addTab(self.outputs, '输出文件')

        # 标签页 4：官方推荐的冻结编码器探针层指南表
        guide_table = QTableWidget(len(MERT_LAYER_GUIDE), 3)
        guide_table.setHorizontalHeaderLabels(['下游任务', 'MERT-v2-30s', 'MERT-v2-FullSong'])
        guide_table.verticalHeader().setVisible(False)
        guide_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        guide_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for r, row in enumerate(MERT_LAYER_GUIDE):
            for c, value in enumerate(row):
                guide_table.setItem(r, c, QTableWidgetItem(value))
        guide_page = QWidget()
        guide_box = QVBoxLayout(guide_page)
        guide_box.setContentsMargins(0, 8, 0, 0)
        guide_box.addWidget(label('官方推荐的冻结编码器探针层（L1 = hidden_states[0]）。“全部层”表示用 24 层特征训练 MLP。', 'Hint', wrap=True))
        guide_box.addWidget(guide_table, 1)
        self.tabs.addTab(guide_page, '层选择指南')

        box.addWidget(self.tabs, 1)

        # 左右两栏布局：左侧表单（模型 + 音频 + 特征设置），右侧结果区
        self.two_columns([model_card, audio_card, feat_card], right, bar, sizes=(460, 740))
        # 初始按当前变体刷新说明文案
        self._variant(self.variant.value())

    def _variant(self, value):
        """模型变体切换时，把对应说明文案显示到模型卡片下方。"""
        self.variant_info.setText(VARIANT_INFO[value])

    def _recommend(self, index):
        """根据用户选择的下游任务，自动设置官方推荐的输出层。"""
        # 第一个"按下游任务推荐层…"项 itemData 为 None，直接忽略
        value = self.task.itemData(index)
        if not value:
            return
        # 在指南表中找到该任务对应的条目
        entry = next(t for t in MERT_LAYER_GUIDE if t[0] == value)
        # 按当前模型变体取第 1（30s）或第 2（FullSong）列的推荐值
        layer = entry[1] if self.variant.value() == '30s' else entry[2]
        if layer.startswith('L'):
            # 形如 "L4"：把层下拉框切到对应层
            self.layer.setCurrentIndex(int(layer[1:]))
            self.toast(f'{value}：推荐 {layer}', 'info')
        else:
            # "全部层"：回到默认层，并确保保存各层平均向量
            self.layer.setCurrentIndex(0)
            self.save_layers.setChecked(True)
            self.toast(f'{value}：推荐使用全部层（layers.npy / 或导出全部帧）', 'info')
        # 完成后把快速选择下拉框复位到提示项
        self.task.setCurrentIndex(0)

    def add_files(self):
        """通过文件选择对话框批量添加音频文件。"""
        paths, _filter = QFileDialog.getOpenFileNames(self, '添加音频', '', AUDIO_FILTER)
        self.files.add_paths(paths)

    def add_folder(self):
        """通过目录选择对话框添加整个文件夹（递归收集其中的音频）。"""
        folder = QFileDialog.getExistingDirectory(self, '添加文件夹')
        if folder:
            self.files.add_paths([folder])

    def remove_selected(self):
        """移除列表中当前选中的条目。"""
        for item in self.files.selectedItems():
            self.files.takeItem(self.files.row(item))

    def run(self):
        """开始提取特征：校验输入后提交后台任务。"""
        if not self.ensure_idle():
            return
        paths = self.files.paths()
        if not paths:
            self.warn('请先添加音频文件。')
            return
        # 汇总提取参数：模型变体、文件、输出层、时长上限、精度与保存项
        params = {
            'variant': self.variant.value(),
            'files': paths,
            'layer': self.layer.currentData(),
            'max_seconds': self.max_seconds.value() or None,
            'dtype': self.dtype.value(),
            'save_frames': self.save_frames.isChecked(),
            'save_layers': self.save_layers.isChecked(),
        }
        self.progress.start('提取特征中…')
        runner.submit(
            'MERT 特征提取',
            engine.run_mert,
            params,
            on_done=self._done,
            on_error=self._error,
            on_progress=self.progress.update_info,
        )

    def _done(self, result):
        """提取成功：填充歌曲下拉框、相似度矩阵与输出文件列表。"""
        self.last = result
        self.progress.finish(True, f'完成 · {len(result["items"])} 首')

        # 填充歌曲下拉框（阻断信号避免逐项触发 _show_ssm）
        self.ssm_file.blockSignals(True)
        self.ssm_file.clear()
        for item in result['items']:
            self.ssm_file.addItem(f'{item["name"]}  ·  {fmt_time(item["seconds"])}  ·  {item["frames"]} 帧')
        self.ssm_file.blockSignals(False)
        self._show_ssm(0)

        # 相似度矩阵：行/列标题为截断后的歌曲名
        names = [item['name'] for item in result['items']]
        sim = result['similarity']
        self.sim.setRowCount(len(names))
        self.sim.setColumnCount(len(names))
        labels = [n if len(n) <= 18 else n[:16] + '…' for n in names]
        self.sim.setHorizontalHeaderLabels(labels)
        self.sim.setVerticalHeaderLabels(labels)

        # 归一化范围取自对角线之外的相似度值（多首歌时），单首则用整体展平
        if len(names) > 1:
            v = sim[~(abs(sim - 1) < 1e-06)]
        else:
            v = sim.ravel()
        if v.size:
            lo, hi = float(v.min()), float(v.max())
        else:
            lo, hi = 0, 1

        # 逐格填充：数值 + 按归一化相似度着色（对角线恒为 1）
        for i in range(len(names)):
            for j in range(len(names)):
                val = float(sim[i, j])
                item = QTableWidgetItem(f'{val:.3f}')
                item.setTextAlignment(Qt.AlignCenter)
                norm = 1 if i == j else (val - lo) / max(hi - lo, 1e-06)
                rgb = colormap(max(0, min(1, norm)) * 0.85)
                item.setBackground(QColor(int(rgb[0]), int(rgb[1]), int(rgb[2])))
                # 根据背景亮度选择黑/白前景色保证可读性
                lum = 0.299 * int(rgb[0]) + 0.587 * int(rgb[1]) + 0.114 * int(rgb[2])
                item.setForeground(QColor('black' if lum > 140 else 'white'))
                item.setToolTip(f'{names[i]}\n{names[j]}\n余弦相似度 {val:.4f}')
                self.sim.setItem(i, j, item)
        self.sim.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        # 输出文件夹中的文件清单（按名称排序）
        self.outputs.clear()
        self.outputs.addItems(sorted(f.name for f in Path(result['dir']).iterdir()))
        self.open_btn.setEnabled(True)
        self.toast('特征提取完成', 'ok')

    def _show_ssm(self, index):
        """切换歌曲时更新自相似图；索引越界则清空显示。"""
        if not self.last or not (0 <= index < len(self.last['items'])):
            self.heatmap.set_matrix(None)
            return
        item = self.last['items'][index]
        self.heatmap.set_matrix(item['ssm'], item['pool_seconds'])

    def _error(self, message, trace):
        """提取失败：结束进度条；非取消场景弹出错误对话框。"""
        self.progress.finish(False, message.splitlines()[0][:160])
        if message != '已取消':
            self.main.show_error('特征提取失败', message, trace)

    def _open_output(self, item):
        """双击输出文件条目时，用系统默认程序打开该文件。"""
        if self.last:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.last['dir']) / item.text())))

    def primary_action(self):
        """Ctrl+Enter 触发的默认操作：开始提取特征。"""
        self.run()
