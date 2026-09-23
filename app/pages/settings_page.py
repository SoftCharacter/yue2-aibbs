"""Model status, runtime options and environment info.

本模块实现"设置"页面：展示各模型文件的下载状态与显存占用、界面外观主题，
并提供运行参数（计算设备、显存预算、解码后端、注意力内核、VAE、码率）、
LLM 服务配置（预设 / 密钥 / 提示词）以及环境信息检测等。
"""
from __future__ import annotations

import importlib.metadata
import platform
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor
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
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import compat, lyrics_ai
from ..engine import engine, vae_choices
from ..paths import ALIGNER_DIR, ASR_DIR, MERT_DIRS, MODELS, SHEETSAGE_DIR, YUE2_DIR, output_root
from ..settings import settings
from ..tasks import runner
from ..theme import C
from ..widgets.common import Card, button, form_row, label, scroll
from .base import BasePage

# 模型清单：名称、所在目录、用途说明。逐行渲染到"模型文件"表格中。
MODEL_ROWS = [
    ('YuE2-3B', YUE2_DIR, '歌曲生成主模型（乐谱规划 + 歌曲 token + 声学合成）'),
    ('YuE2-Vae', MODELS / 'YuE2-Vae', '音频解码器：把声学潜变量解码为 48kHz 立体声'),
    ('SheetSage2', SHEETSAGE_DIR, '扒谱：旋律 / 和弦 / 节拍 / 调性 / 结构（依赖 MERT-v2-FullSong）'),
    ('MERT-v2-FullSong', MERT_DIRS['FullSong'], '整首歌音乐表征编码器（360 秒上下文）'),
    ('MERT-v2-30s', MERT_DIRS['30s'], '30 秒上下文音乐表征编码器'),
    ('Qwen3-ASR-1.7B', ASR_DIR, '歌词识别：从带伴奏的歌曲中听写歌词（52 种语言/方言）'),
    ('Qwen3-ForcedAligner-0.6B', ALIGNER_DIR, '逐字时间戳对齐：自动断句、分段、生成 LRC'),
]


class _LlmBridge(QObject):
    """跨线程回传 LLM 连接测试结果的信号桥。"""

    result = Signal(object, str, bool, object)


def folder_size(path: Path):
    """计算目录内所有 .safetensors 权重文件的总字节数；非目录返回 0。"""
    return sum(f.stat().st_size for f in path.glob('*.safetensors')) if path.is_dir() else 0


class SettingsPage(BasePage):
    """设置页面：模型文件状态、运行参数、LLM 配置与环境信息。"""

    title = '⚙️ 设置'
    subtitle = '模型文件状态、显卡与运行参数。修改后下次加载模型时生效。'

    def __init__(self, main):
        super().__init__(main)

        # 整体内容用一个可滚动容器承载，逐卡片垂直堆叠
        w = QWidget()
        box = QVBoxLayout(w)
        box.setContentsMargins(0, 0, 8, 0)
        box.setSpacing(12)

        # 卡片 1：模型文件状态表（模型 / 用途 / 文件状态 / 显存中）与卸载按钮
        model_card = Card('模型文件')
        self.table = QTableWidget(len(MODEL_ROWS), 4)
        self.table.setHorizontalHeaderLabels(['模型', '用途', '文件状态', '显存中'])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        header = self.table.horizontalHeader()
        for col, mode in enumerate((QHeaderView.ResizeToContents, QHeaderView.Stretch, QHeaderView.ResizeToContents, QHeaderView.ResizeToContents)):
            header.setSectionResizeMode(col, mode)
        self.table.setMinimumHeight(40 + 34 * len(MODEL_ROWS))
        model_card.body.addWidget(self.table)
        unload_row = QHBoxLayout()
        unload_row.addStretch(1)
        self.unload_btn = button('⏏ 卸载全部模型并释放显存', callback=self.unload_all)
        model_card.body.addLayout(unload_row)
        box.addWidget(model_card)

        # 卡片 2：LLM 服务配置（由独立方法构造，含密钥与提示词）
        box.addWidget(self._build_llm_card())

        # 卡片 4：运行设置（设备 / 显存预算 / 解码后端 / 注意力 / VAE / 码率 / 输出目录）
        run_card = Card('运行设置')
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)

        # 计算设备下拉框：自动（优先 CUDA）/ CUDA / CPU
        self.device = QComboBox()
        for text, code in (('自动（优先 CUDA）', 'auto'), ('CUDA 显卡', 'cuda'), ('CPU（非常慢）', 'cpu')):
            self.device.addItem(text, code)

        # YuE2 显存预算（0 表示整张显卡）
        self.budget = QDoubleSpinBox()
        self.budget.setRange(0, 200)
        self.budget.setDecimals(0)
        self.budget.setSuffix(' GiB')
        self.budget.setSpecialValueText('自动（整张显卡）')

        # 解码后端：torch + CUDA Graph / torch-eager
        self.backend = QComboBox()
        self.backend.addItem('torch + CUDA Graph（快）', 'torch')
        self.backend.addItem('torch-eager（兼容模式）', 'torch-eager')

        # 注意力内核：自动检测 / cuDNN / SDPA 通用
        self.attention = QComboBox()
        for text, code in (('自动检测', 'auto'), ('cuDNN', 'cudnn'), ('SDPA 通用', 'sdpa')):
            self.attention.addItem(text, code)

        # 音频解码器 VAE 变体
        self.vae = QComboBox()
        for item in vae_choices() or ['YuE2-Vae']:
            self.vae.addItem(item, item)

        # MP3 导出码率
        self.bitrate = QComboBox()
        for item in ('320k', '256k', '192k', '128k'):
            self.bitrate.addItem(item, item)

        # 逐项放入网格：第 0 行设备/预算、第 1 行后端/注意力、第 2 行 VAE/码率
        grid.addWidget(form_row('计算设备', self.device), 0, 0)
        grid.addWidget(form_row('YuE2 显存预算', self.budget, '只在 YuE2 生成/规划运行期间生效，结束后恢复整张显卡给其他模型使用。实际可用 = 预算 − 2 GiB 运行保留，至少填 4 GiB；YuE2 模型本身约 7 GiB，声学合成使用 cuDNN 注意力时整首歌峰值约 9~10 GiB（实测 3 分 43 秒的歌在 16 GiB 预算内完成）。预算只是上限，不会减少占用；0 = 使用整张显卡'), 0, 1)
        grid.addWidget(form_row('YuE2 解码后端', self.backend, 'CUDA Graph 大幅提升 token 生成速度；若出现兼容问题可切到 eager'), 1, 0)
        grid.addWidget(form_row('注意力内核', self.attention, 'Windows 版 PyTorch 未内置 FlashAttention，自动检测会选 cuDNN。同时作用于声学合成：自动/cuDNN 显存占用最低；选"SDPA 通用"时声学合成改为分块计算，显存略高、速度较慢'), 1, 1)
        grid.addWidget(form_row('音频解码器 VAE', self.vae, 'YuE2-Vae 听感更好（默认）；YuE2-Vae-legacy 用于复现论文基准'), 2, 0)
        grid.addWidget(form_row('MP3 导出码率', self.bitrate), 2, 1)
        run_card.body.addLayout(grid)

        # 校验与显存独占开关
        self.verify = QCheckBox('加载 YuE2 时校验权重 SHA256（约 5 秒，确认文件完整）')
        self.exclusive = QCheckBox('运行某个模型前自动卸载其他模型（节省显存，推荐）')
        run_card.body.addWidget(self.verify)
        run_card.body.addWidget(self.exclusive)

        # 输出目录：输入框 + 浏览按钮
        out_box = QHBoxLayout()
        self.output = QLineEdit()
        self.output.setPlaceholderText(str(output_root()))
        out_box.addWidget(self.output, 1)
        out_box.addWidget(button('浏览…', callback=self.pick_output))
        run_card.body.addWidget(form_row('输出目录', out_box, '留空则保存到项目下的 outputs 文件夹'))
        box.addWidget(run_card)

        # 卡片 5：环境信息（Python / 依赖版本 / GPU / 推理包等）
        self.env = Card('环境信息')
        self.env_text = label('正在检测…', 'Hint', wrap=True)
        self.env_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.env.body.addWidget(self.env_text)
        box.addWidget(self.env)

        # 卡片 6：关于
        about_card = Card('关于')
        about_card.body.addWidget(label('YuE2 Studio · 基于 m-a-p 开源的 YuE2-3B / YuE2-Vae / SheetSage2 / MERT-v2 模型。\n模型权重采用 CC BY-NC 4.0 许可，仅限非商业用途。项目主页：https://github.com/multimodal-art-projection/YuE', 'Hint', wrap=True))
        box.addWidget(about_card)

        box.addStretch(1)
        self.root.addWidget(scroll(w), 1)

        # 初始载入已保存的设置
        self._load()

        # 各项设置变化即触发保存（信号 → _save）
        for widget, sig in (
            (self.device, 'currentIndexChanged'),
            (self.backend, 'currentIndexChanged'),
            (self.attention, 'currentIndexChanged'),
            (self.vae, 'currentIndexChanged'),
            (self.bitrate, 'currentIndexChanged'),
            (self.budget, 'valueChanged'),
            (self.verify, 'toggled'),
            (self.exclusive, 'toggled'),
            (self.output, 'editingFinished'),
        ):
            getattr(widget, sig).connect(self._save)

        # 定时器：页面可见时每 2 秒刷新一次模型加载状态
        self.timer = QTimer(self, interval=2000, timeout=self.refresh_models)

    def _load(self):
        """把 settings 中已保存的值回填到各控件。"""

        def pick(combo, value):
            # 按下拉框的 itemData 匹配设置值并选中，找不到则保持默认
            idx = combo.findData(value)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        pick(self.device, settings['device'])
        pick(self.backend, settings['backend'])
        pick(self.attention, settings['attention'])
        pick(self.vae, settings['vae'])
        pick(self.bitrate, settings['mp3_bitrate'])
        self.budget.setValue(float(settings['memory_budget_gib'] or 0))
        self.verify.setChecked(bool(settings['verify_hashes']))
        self.exclusive.setChecked(bool(settings['exclusive_vram']))
        self.output.setText(settings.get('output_dir') or '')

    def _save(self, *_):
        """把当前控件值写回 settings 并落盘，同时刷新作品库。"""
        settings.update(
            device=self.device.currentData(),
            backend=self.backend.currentData(),
            attention=self.attention.currentData(),
            vae=self.vae.currentData(),
            mp3_bitrate=self.bitrate.currentData(),
            memory_budget_gib=self.budget.value(),
            verify_hashes=self.verify.isChecked(),
            exclusive_vram=self.exclusive.isChecked(),
            output_dir=self.output.text().strip(),
        )
        settings.save()
        self.main.library_changed()

    def _build_llm_card(self):
        """构造"AI 服务"卡片：LLM 预设、密钥、提示词与连接测试。"""
        from ..llm_client import PRESETS, current_config

        config = current_config(settings)
        card = Card('AI 服务')

        # 预设下拉框：填 PRESETS 全部名称，按当前配置选中，找不到则选最后一项（自定义）
        self.llm_preset = QComboBox()
        for name, *_ in PRESETS:
            self.llm_preset.addItem(name, name)
        idx = self.llm_preset.findData(config.get('preset'))
        self.llm_preset.setCurrentIndex(idx if idx >= 0 else len(PRESETS) - 1)

        # 接口地址
        self.llm_base = QLineEdit(config.get('base_url', ''))
        self.llm_base.setPlaceholderText('https://api.example.com/v1')

        # API Key（密码掩码）+ 显示/隐藏切换
        key_box = QHBoxLayout()
        self.llm_key = QLineEdit(config.get('api_key', ''))
        self.llm_key.setEchoMode(QLineEdit.Password)
        self.llm_key.setPlaceholderText('sk-…（本地 Ollama / LM Studio 可留空）')
        key_box.addWidget(self.llm_key, 1)
        eye = button('👁', 'Ghost', '显示 / 隐藏')
        eye.setCheckable(True)
        eye.toggled.connect(lambda on: self.llm_key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        key_box.addWidget(eye)

        # 模型名称（可编辑下拉框）+ 获取模型列表按钮
        model_box = QHBoxLayout()
        self.llm_model = QComboBox()
        self.llm_model.setEditable(True)
        self.llm_model.setCurrentText(config.get('model', ''))
        model_box.addWidget(self.llm_model, 1)
        self.llm_models_btn = button('获取模型列表', callback=self._fetch_models)
        model_box.addWidget(self.llm_models_btn)

        # 温度
        self.llm_temp = QDoubleSpinBox()
        self.llm_temp.setRange(0, 2)
        self.llm_temp.setSingleStep(0.1)
        self.llm_temp.setValue(float(config.get('temperature', 0.8)))

        # 网格布局：预设/地址、Key/模型、温度
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.addWidget(form_row('服务商预设', self.llm_preset), 0, 0)
        grid.addWidget(form_row('接口地址 Base URL', self.llm_base), 0, 1)
        grid.addWidget(form_row('API Key', key_box), 1, 0)
        grid.addWidget(form_row('模型名称', model_box), 1, 1)
        grid.addWidget(form_row('温度', self.llm_temp), 2, 0)
        card.body.addLayout(grid)

        # 系统提示词：按 PROMPT_KINDS 分标签页编辑，可一键恢复默认
        card.body.addWidget(label('系统提示词', 'CardTitle'))
        self.llm_prompts = {}
        tabs = QTabWidget()
        for key, title, desc in lyrics_ai.PROMPT_KINDS:
            page = QWidget()
            box = QVBoxLayout(page)
            box.setContentsMargins(0, 8, 0, 0)
            top = QHBoxLayout()
            edit = QPlainTextEdit(lyrics_ai.system_prompt(settings, key))
            edit.setMinimumHeight(240)
            top.addWidget(label(f'用于：{desc}', 'Hint', wrap=True), 1)
            top.addWidget(button('恢复默认', 'Ghost', f'恢复默认的{title}提示词', lambda _=False, e=edit, k=key: e.setPlainText(lyrics_ai.DEFAULT_PROMPTS[k])))
            box.addLayout(top)
            box.addWidget(edit, 1)
            tabs.addTab(page, title)
            self.llm_prompts[key] = edit
        card.body.addWidget(tabs)

        # 测试连接按钮 + 状态标签
        test_row = QHBoxLayout()
        self.llm_test_btn = button('🔌 测试连接', callback=self._test_llm)
        self.llm_status = label('', 'Hint', wrap=True)
        test_row.addWidget(self.llm_test_btn)
        test_row.addWidget(self.llm_status, 1)
        card.body.addLayout(test_row)

        # 防抖保存定时器：配置变化 600ms 后统一保存
        self._llm_timer = QTimer(self, singleShot=True, interval=600, timeout=self._save_llm)
        self.llm_preset.activated.connect(self._apply_preset)
        for w in (self.llm_base, self.llm_key):
            w.textChanged.connect(lambda *_: self._llm_timer.start())
        self.llm_model.currentTextChanged.connect(lambda *_: self._llm_timer.start())
        self.llm_temp.valueChanged.connect(lambda *_: self._llm_timer.start())
        for edit in self.llm_prompts.values():
            edit.textChanged.connect(lambda: self._llm_timer.start())

        # 跨线程结果信号桥
        self._llm_bridge = _LlmBridge()
        self._llm_bridge.result.connect(self._llm_result)
        return card

    def _llm_values(self):
        """汇总当前 LLM 配置为字典；提示词仅保存与默认不同的部分（相同则存空串）。"""
        from ..llm_client import PRESETS

        preset = self.llm_preset.currentData()
        protocol = next((item[1] for item in PRESETS if item[0] == preset), 'openai')
        return {
            'preset': preset,
            'protocol': protocol,
            'base_url': self.llm_base.text().strip(),
            'api_key': self.llm_key.text().strip(),
            'model': self.llm_model.currentText().strip(),
            'temperature': self.llm_temp.value(),
            'prompts': {key: ('' if edit.toPlainText().strip() == lyrics_ai.DEFAULT_PROMPTS[key] else edit.toPlainText())
                        for key, edit in self.llm_prompts.items()},
        }

    @staticmethod
    def _llm_connection(config):
        """决定测试结果是否仍然有效的连接参数；改提示词不会让连接测试结果过期。"""
        return tuple(sorted((k, v) for k, v in config.items() if k != 'prompts'))

    def _save_llm(self):
        """把 LLM 配置写入 settings 并落盘。"""
        settings['llm'] = self._llm_values()
        settings.save()

    def _apply_preset(self, index):
        """切换预设时回填该平台的 Base URL 与默认模型，并提示填写 API Key。"""
        from ..llm_client import PRESETS

        name, protocol, base_url, model = PRESETS[index]
        if base_url:
            self.llm_base.setText(base_url)
        self.llm_model.clear()
        self.llm_model.setCurrentText(model)
        self._save_llm()
        self.llm_status.setText(f'已切换到 {name}，请填写该平台的 API Key。' + ('' if model else '模型名称请手动填写或点"获取模型列表"。'))

    def _run_llm_job(self, kind, fn):
        """在后台线程执行 LLM 请求，结果经信号桥回传主线程。

        ``kind`` 区分 'test' 与 'models' 两种用途；``fn`` 为接受 config 的调用。
        """
        self._save_llm()
        config = self._llm_values()
        fingerprint = self._llm_connection(config)
        # 执行期间禁用两个按钮，避免重复请求
        self.llm_test_btn.setEnabled(False)
        self.llm_models_btn.setEnabled(False)
        self.llm_status.setText('连接中…')
        bridge = self._llm_bridge

        def work():
            try:
                ok, payload = True, fn(config)
            except Exception as exc:
                ok, payload = False, str(exc)
            # 窗口可能已销毁，捕获 RuntimeError 防止 emit 抛错
            try:
                bridge.result.emit(fingerprint, kind, ok, payload)
            except RuntimeError:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _test_llm(self):
        """发起一次最小对话测试连接是否可用。"""
        from ..llm_client import stream_chat

        self._run_llm_job('test', lambda config: stream_chat(config, '你是一个连接测试助手。', '请只回复"连接成功"四个字。', on_delta=lambda _t: None))

    def _fetch_models(self):
        """请求远端模型列表并填入下拉框。"""
        from ..llm_client import list_models

        self._run_llm_job('models', list_models)

    def _llm_result(self, fingerprint, kind, ok, payload):
        """接收后台线程回传的连接结果并更新 UI；配置已变更则丢弃过期结果。"""
        self.llm_test_btn.setEnabled(True)
        self.llm_models_btn.setEnabled(True)
        if fingerprint != self._llm_connection(self._llm_values()):
            self.llm_status.setText('配置已更改，已忽略旧连接结果；请重新测试或获取模型列表。')
            return
        if not ok:
            self.llm_status.setText(f'⚠ {payload}')
            return
        if kind == 'test':
            self.llm_status.setText('✓ 连接成功，模型回复：' + str(payload).strip()[:80])
            return
        # kind == 'models'：填入模型列表并保留当前选中
        current = self.llm_model.currentText()
        self.llm_model.blockSignals(True)
        self.llm_model.clear()
        self.llm_model.addItems(payload)
        self.llm_model.setCurrentText(current)
        self.llm_model.blockSignals(False)
        self.llm_status.setText(f'✓ 读取到 {len(payload)} 个模型，可在"模型名称"下拉框中选择。')

    def pick_output(self):
        """弹出目录选择框设置输出目录，选中后保存。"""
        folder = QFileDialog.getExistingDirectory(self, '选择输出目录', self.output.text() or str(output_root()))
        if folder:
            self.output.setText(folder)
            self._save()

    def on_show(self):
        """页面变为可见时刷新模型状态与环境信息。"""
        self.refresh_models()
        self.refresh_env()

    def showEvent(self, event):
        """页面显示时刷新模型并启动定时刷新。"""
        super().showEvent(event)
        self.refresh_models()
        self.timer.start()

    def hideEvent(self, event):
        """页面隐藏时停止定时刷新。"""
        self.timer.stop()
        super().hideEvent(event)

    def save_draft(self):
        """若有待保存的 LLM 配置（防抖定时器活跃），先停止定时器并立即保存。"""
        if self._llm_timer.isActive():
            self._llm_timer.stop()
            self._save_llm()

    def refresh_models(self):
        """刷新模型文件状态表：就绪/缺失、体积与是否已加载到显存。"""
        loaded = engine.loaded()
        for r, (name, path, desc) in enumerate(MODEL_ROWS):
            # YuE2-Vae 目录跟随当前 VAE 设置动态变化
            if name == 'YuE2-Vae':
                path = MODELS / settings.get('vae', 'YuE2-Vae')
                name = path.name
            size = folder_size(path)
            ok = (path / 'config.json').is_file() and size > 0
            status = f'✓ 已就绪 · {size / 1073741824:.2f} GB' if ok else '✕ 未找到'
            cells = [name, desc, status, '●' if loaded.get(name) else '—']
            for c, value in enumerate(cells):
                item = QTableWidgetItem(value)
                # 状态列按就绪/缺失着色
                if c == 2:
                    item.setForeground(QColor(C['green' if ok else 'red']))
                if c == 3:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, c, item)

    def refresh_env(self):
        """检测并展示运行环境信息：Python/依赖版本、GPU、推理包与工具链。"""
        lines = [f'Python {sys.version.split()[0]} · {platform.platform()}']
        # 逐个查询关键依赖版本，未安装的单独标注
        for pkg in ('torch', 'transformers', 'PySide6', 'numpy', 'soundfile', 'pretty_midi', 'mir_eval'):
            try:
                lines.append(f'{pkg} {importlib.metadata.version(pkg)}')
            except importlib.metadata.PackageNotFoundError:
                lines.append(f'{pkg} 未安装')
        # 已导入 torch 时检测 CUDA 与显卡信息
        if 'torch' in sys.modules:
            torch = sys.modules['torch']
            if torch.cuda.is_available():
                prop = torch.cuda.get_device_properties(0)
                lines.append(f'GPU: {prop.name} · {prop.total_memory / 1073741824:.1f} GiB · CUDA {torch.version.cuda} · 算力 {prop.major}.{prop.minor}')
            else:
                lines.append('GPU: 未检测到 CUDA')
        lines.append(f'YuE2 推理包: {compat.yue2_source or "尚未加载（首次生成时从 whl 载入）"}')
        resolved = compat._attention.get('resolved')
        lines.append(f'注意力内核: {resolved or "首次生成时检测"}')
        nar = compat._nar_attention.get('resolved')
        lines.append(f'声学合成注意力: {compat.NAR_ATTENTION_NAMES[nar] if nar else "首次生成时检测"}')
        import shutil

        lines.append(f'ffmpeg: {shutil.which("ffmpeg") or "未找到"}')
        lines.append(f'模型目录: {MODELS}')
        # 首行单独成行，其余用换行拼接
        self.env_text.setText('  ·  '.join(lines[:1]) + '\n' + '\n'.join(lines[1:]))

    def unload_all(self):
        """卸载全部模型并释放显存（后台任务执行）。"""
        if not self.ensure_idle():
            return
        runner.submit('卸载模型', engine.run_unload, {}, on_done=lambda _r: (self.refresh_models(), self.toast('已释放显存', 'ok')))
