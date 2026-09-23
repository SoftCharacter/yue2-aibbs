"""Forms shared by the create / cover / edit pages.

本模块实现三个创作页（创作 / 翻唱 / 编辑）共用的表单组件。StyleCard 封装风格描述输入
与可点击的风格标签库；DecodeSettings 提供音频解码参数的设置（自动 / 分块 / 完整三种
模式）；GenerationSettings 组合乐谱规划模式、ODE 步数、随机种子、生成数量、CFG 与作品
名等生成参数；AdvancedSampling 以网格形式暴露两阶段（乐谱规划 / 歌曲生成）的采样超参。
模块顶部还定义了 STYLE_GROUPS 风格标签库、COT_OPTIONS 乐谱规划模式选项与
SAMPLING_DEFAULTS 默认采样参数三份常量数据。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QWidget,
)

from .common import (
    Card,
    ChipPicker,
    Collapsible,
    Segmented,
    SeedEdit,
    SliderSpin,
    button,
    form_row,
    label,
)

# 风格标签库：每个分组为 (分组名, [(中文名, 英文标签), ...])，供 ChipPicker 展示
STYLE_GROUPS = [
    ('流派', [
        ('流行', 'pop'), ('摇滚', 'rock'), ('民谣', 'folk'), ('爵士', 'jazz'),
        ('放克', 'funk'), ('迪斯科', 'nu-disco'), ('R&B', 'R&B'), ('嘻哈', 'hip-hop'),
        ('电子', 'EDM'), ('合成器流行', 'synth-pop'), ('Lo-fi', 'lo-fi'),
        ('金属', 'heavy metal'), ('抒情', 'ballad'), ('国风', 'Chinese traditional'),
        ('古典', 'classical'), ('乡村', 'country'), ('朋克', 'punk'),
        ('灵魂乐', 'soul'), ('City Pop', 'city pop'),
    ]),
    ('人声', [
        ('女声', 'female vocal'), ('男声', 'male vocal'), ('温暖人声', 'warm lead vocal'),
        ('有力人声', 'powerful vocal'), ('气声', 'breathy vocal'), ('沙哑', 'raspy vocal'),
        ('合唱', 'choir'), ('对唱', 'male and female duet'), ('说唱', 'rap vocal'),
    ]),
    ('乐器', [
        ('钢琴', 'piano'), ('木吉他', 'acoustic guitar'), ('电吉他', 'electric guitar'),
        ('贝斯', 'electric bass'), ('鼓', 'drums'), ('弦乐', 'strings'), ('合成器', 'synth'),
        ('电钢琴', 'Rhodes piano'), ('萨克斯', 'saxophone'), ('小号', 'trumpet'),
        ('管风琴', 'organ'), ('二胡', 'erhu'), ('古筝', 'guzheng'), ('笛子', 'dizi'),
        ('808', '808 bass'),
    ]),
    ('情绪', [
        ('欢快', 'upbeat'), ('伤感', 'melancholic'), ('激昂', 'energetic'),
        ('梦幻', 'dreamy'), ('浪漫', 'romantic'), ('史诗', 'epic'), ('温暖', 'warm'),
        ('黑暗', 'dark'), ('放松', 'chill'), ('怀旧', 'nostalgic'),
    ]),
    ('语言', [
        ('中文', 'Mandarin'), ('粤语', 'Cantonese'), ('英语', 'English'),
        ('日语', 'Japanese'), ('韩语', 'Korean'),
    ]),
    ('速度', [
        ('慢速', 'slow tempo'), ('中速', 'mid-tempo'), ('快速', 'fast tempo'),
    ]),
]

# 乐谱规划模式选项：(显示名, 值, 说明)
COT_OPTIONS = [
    ('旋律 + 和弦', 'full', '先写出带和弦的 ABC 乐谱再生成歌曲（默认，结构最好）'),
    ('仅旋律', 'melody', '只规划旋律，不含和弦；翻唱推荐'),
    ('不使用乐谱', 'off', '直接生成，不做乐谱规划'),
]

# 各阶段采样参数的默认值，键为阶段名（abc / semantic）
SAMPLING_DEFAULTS = {
    'abc': {
        'temperature': 0.7, 'top_p': 0.9, 'top_k': 30, 'repetition_penalty': 1.005,
        'min_tokens': 32, 'max_tokens': 4096, 'penalty_window': 100,
    },
    'semantic': {
        'temperature': 1, 'top_p': 0.95, 'top_k': 100, 'repetition_penalty': 1.2,
        'min_tokens': 200, 'max_tokens': 9000, 'penalty_window': 50,
    },
}


class StyleCard(Card):
    """风格描述卡片：可编辑的风格文本框 + 可点击的风格标签库。"""

    def __init__(self, title='风格描述', step=None, hint=None, expanded=False, parent=None):
        """构建卡片：文本框、字数统计与折叠的标签库；点击标签会追加到文本框。"""
        # hint 缺省时给出一段引导文案，说明标签用英文逗号分隔效果最好
        super().__init__(
            title,
            hint or '用英文逗号分隔的标签效果最好：流派、人声、乐器、情绪、语言等。点击下方标签快速添加。',
            step=step,
            parent=parent,
        )

        # 风格描述文本框：占位提示 + 固定高度，文本变化时刷新字数
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText('例如：Mandarin, warm piano, acoustic pop, female vocal')
        self.edit.setFixedHeight(76)
        self.body.addWidget(self.edit)

        # 头部字数统计标签，初始为空，随输入更新
        self.count = label('', 'Hint')
        self.add_header_widget(self.count)

        # 折叠的标签库面板，内部用 ChipPicker 展示 STYLE_GROUPS
        tag_panel = Collapsible('风格标签库（流派 / 人声 / 乐器 / 情绪 / 语言，点击即可添加）', expanded=expanded)
        picker = ChipPicker(STYLE_GROUPS)
        picker.picked.connect(self.append_tag)
        tag_panel.body.addWidget(picker)
        self.body.addWidget(tag_panel)

        # 文本变化时刷新字数，并立即执行一次初始化计数
        self.edit.textChanged.connect(self._count)
        self._count()

    def _count(self):
        """更新字数统计标签；超过 1000 字时用红色高亮提示。"""
        n = len(self.edit.toPlainText())
        self.count.setText(f'{n}/1000')
        # 内联导入主题色，避免模块级循环依赖
        from ..theme import C

        self.count.setStyleSheet(f'color:{C["red"]};' if n > 1000 else '')

    def append_tag(self, tag):
        """把风格标签追加到文本框；标签已存在（忽略大小写）时不重复添加。"""
        # 去掉末尾逗号后拆分，得到已有的标签列表（统一转小写去重比较）
        current = self.edit.toPlainText().strip().rstrip(',')
        tags = [t.strip().lower() for t in current.split(',')]
        if tag.lower() in tags:
            return
        # 当前为空则直接填入标签，否则用 ", " 拼接
        self.edit.setPlainText(f'{current}, {tag}' if current else tag)

    def text(self):
        """返回风格描述的规范文本：把任意空白折叠为单个空格。"""
        return ' '.join(self.edit.toPlainText().split())

    def setText(self, text):
        """设置风格描述文本框内容，None 或空值归一为空串。"""
        self.edit.setPlainText(text or '')


class DecodeSettings(Collapsible):
    """音频解码参数；自动模式交由管线沿用其默认策略。"""

    def __init__(self, parent=None):
        """构建解码设置：模式分段选择器 + 分块长度 / 上下文长度两个数字框。"""
        super().__init__('音频解码设置', expanded=False, parent=parent)

        # 三种解码模式：自动（沿用默认）/ 分块 / 完整
        self.mode = Segmented([
            ('自动（默认）', 'auto', '沿用管线默认的音频解码策略'),
            ('分块解码', 'tiled', '分块处理潜变量，使用下方分块设置'),
            ('完整解码', 'full', '一次解码完整音频，显存占用较高'),
        ], 'auto')
        self.body.addWidget(self.mode)

        # 模式说明文本，随模式切换更新
        self.mode_hint = label('', 'Hint', wrap=True)
        self.body.addWidget(self.mode_hint)

        # 分块参数网格：分块长度与上下文长度并排
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)

        self.core_frames = QSpinBox()
        self.core_frames.setRange(64, 8192)
        self.core_frames.setSuffix(' 帧')

        self.context_frames = QSpinBox()
        self.context_frames.setRange(12, 256)
        self.context_frames.setSuffix(' 帧')

        grid.addWidget(form_row('分块长度', self.core_frames, '每块实际解码的潜变量帧数'), 0, 0)
        grid.addWidget(
            form_row('上下文长度', self.context_frames, '每块前后额外读取的帧数；当前 YuE2-Vae 至少需要 12 帧，默认 16'),
            0,
            1,
        )
        self.body.addLayout(grid)

        # 底部单位说明
        self.body.addWidget(
            label('以上单位均为潜变量帧，25 帧约 1 秒音频；仅分块解码模式使用这些设置。', 'Hint', wrap=True)
        )

        # 模式变化时联动启停分块参数；最后载入默认值并同步一次状态
        self.mode.changed.connect(self._mode_changed)
        self.load(None)

    def _mode_changed(self, mode):
        """按模式切换分块参数可用性，并更新模式说明文本。"""
        tiled = mode == 'tiled'
        self.core_frames.setEnabled(tiled)
        self.context_frames.setEnabled(tiled)
        self.mode_hint.setText({
            'auto': '沿用管线默认策略，无需手动设置分块参数。',
            'tiled': '分块解码可降低峰值显存；较长的分块通常需要更多显存。',
            'full': '完整解码一次处理全部音频，显存占用较高。',
        }[mode])

    def values(self) -> dict:
        """返回当前解码设置字典（mode / core_frames / context_frames）。"""
        return {
            'mode': self.mode.value(),
            'core_frames': self.core_frames.value(),
            'context_frames': self.context_frames.value(),
        }

    def load(self, data: dict | None):
        """从字典载入解码设置；非法模式回退到 auto，数值缺省取默认。"""
        data = data or {}
        mode = data.get('mode', 'auto')
        if mode not in ('auto', 'tiled', 'full'):
            mode = 'auto'
        self.mode.setValue(mode)
        self.core_frames.setValue(int(data.get('core_frames', 1024)))
        self.context_frames.setValue(int(data.get('context_frames', 16)))
        self._mode_changed(mode)


class GenerationSettings(Card):
    """生成设置卡片：规划模式、ODE 步数、种子、数量、CFG、作品名与解码设置。"""

    def __init__(self, title='生成设置', step=None, cot_options=COT_OPTIONS, cot='full', show_count=True, parent=None):
        """构建生成设置表单；show_count 为假时隐藏"生成数量"行。"""
        super().__init__(title, step=step, parent=parent)
        # 记录默认规划模式，供 load 时回退
        self._default_cot = cot

        # 乐谱规划模式分段选择器
        self.cot = Segmented(cot_options, cot)
        self.body.addWidget(form_row('乐谱规划模式', self.cot, 'YuE2 会先写出 ABC 乐谱（旋律/和弦），再按乐谱生成歌曲'))

        # 主参数网格
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)

        # ODE 步数滑条，附带快速 / 标准 / 精细三档快捷按钮
        self.steps = SliderSpin(8, 64, 32, ' 步')

        chips = QHBoxLayout()
        chips.setSpacing(6)
        for name, val in (('快速 16', 16), ('标准 32', 32), ('精细 48', 48)):
            chips.addWidget(button(name, 'Chip', callback=lambda _=False, v=val: self.steps.setValue(v)))
        chips.addStretch(1)

        # 用 QWidget 承载滑条行，便于整体放到网格单元格
        steps_wrap = QWidget()
        steps_layout = QHBoxLayout(steps_wrap)
        steps_layout.setContentsMargins(0, 0, 0, 0)
        steps_layout.addWidget(self.steps, 1)

        grid.addWidget(form_row('渲染质量 (ODE 步数)', steps_wrap, '声学合成的流匹配步数；越高越细腻、越慢。官方默认 32'), 0, 0, 1, 2)
        grid.addLayout(chips, 1, 0, 1, 2)

        # 随机种子编辑
        self.seed = SeedEdit(42)
        grid.addWidget(form_row('随机种子', self.seed, '相同参数 + 相同种子 = 相同结果'), 2, 0, 1, 2)

        # 生成数量（批量生成时种子依次 +1）
        self.count = QSpinBox()
        self.count.setRange(1, 16)
        self.count.setValue(1)
        self.count.setSuffix(' 首')
        self.count.setToolTip('批量生成：种子依次 +1')

        # CFG 自定义开关 + 数值框
        cfg_wrap = QWidget()
        cfg_layout = QHBoxLayout(cfg_wrap)
        cfg_layout.setContentsMargins(0, 0, 0, 0)
        self.cfg_on = QCheckBox('自定义')
        self.cfg = QDoubleSpinBox()
        self.cfg.setRange(0, 20)
        self.cfg.setSingleStep(0.05)
        self.cfg.setDecimals(2)
        self.cfg.setValue(1.2)
        self.cfg.setEnabled(False)
        self.cfg_on.toggled.connect(self.cfg.setEnabled)
        cfg_layout.addWidget(self.cfg_on)
        cfg_layout.addWidget(self.cfg, 1)

        # 有"生成数量"时与 CFG 各占一列，否则 CFG 横跨两列
        if show_count:
            grid.addWidget(form_row('生成数量', self.count), 3, 0)
            grid.addWidget(
                form_row('文本引导 CFG', cfg_wrap, '默认：旋律模式 1.0 / 直接生成 1.01。调高(如 1.2)更贴合风格描述，但可能损失自然度'),
                3,
                1,
            )
        else:
            grid.addWidget(
                form_row('文本引导 CFG', cfg_wrap, '默认：旋律模式 1.0 / 直接生成 1.01。调高(如 1.2)更贴合风格描述'),
                3,
                0,
                1,
                2,
            )
        self.body.addLayout(grid)

        # 作品名（可选）
        self.title = QLineEdit()
        self.title.setPlaceholderText('作品名（可选，用于作品库）')
        self.body.addWidget(self.title)

        # 音频解码设置子面板
        self.decode = DecodeSettings()
        self.body.addWidget(self.decode)

    def values(self, advance_seed=True):
        """汇总生成参数；advance_seed 为真时推进到下一个种子（用于批量生成）。"""
        return {
            'cot': self.cot.value(),
            'ode_steps': self.steps.value(),
            'seed': self.seed.next_seed() if advance_seed else self.seed.value(),
            'seed_auto': self.seed.auto.isChecked(),
            'count': self.count.value(),
            'cfg_scale': self.cfg.value() if self.cfg_on.isChecked() else None,
            'title': self.title.text().strip(),
            'decode_options': self.decode.values(),
        }

    def load(self, data):
        """从字典载入生成参数；各字段缺失时回退到默认值。"""
        self.decode.load(data.get('decode_options'))
        self.cot.setValue(data.get('cot') or self._default_cot)
        self.steps.setValue(data.get('ode_steps') or 32)
        self.seed.setValue(data.get('seed') if data.get('seed') is not None else 42)
        self.seed.auto.setChecked(bool(data.get('seed_auto', False)))
        cfg_scale = data.get('cfg_scale')
        self.cfg_on.setChecked(cfg_scale is not None)
        if cfg_scale is not None:
            self.cfg.setValue(float(cfg_scale))
        self.title.setText(data.get('title') or '')
        self.count.setValue(int(data.get('count') or 1))


class AdvancedSampling(Collapsible):
    """高级采样参数：以网格暴露乐谱规划 / 歌曲生成两阶段的采样超参。"""

    def __init__(self, parent=None):
        """构建高级采样面板：启用开关、恢复默认按钮与两阶段参数网格。"""
        super().__init__('高级采样参数', expanded=False, parent=parent)

        # 顶部行：启用复选框 + 恢复默认按钮
        row = QHBoxLayout()
        self.enabled = QCheckBox('启用自定义采样')
        self.enabled.setToolTip('未勾选时使用官方默认值；勾选后使用下方两阶段参数')
        row.addWidget(self.enabled)
        row.addStretch(1)
        row.addWidget(button('恢复默认', 'Ghost', callback=self.reset))
        self.body.addLayout(row)

        # 两阶段参数字段容器：fields['abc' / 'semantic'] 各自保存参数名 → 控件
        self.fields = {'abc': {}, 'semantic': {}}

        # 参数网格：第一列参数名，后两列分别对应两个阶段
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)

        # 表头三列：参数 / 乐谱规划 / 歌曲生成
        for col, (title, tip) in enumerate((
            ('参数', ''),
            ('乐谱规划', 'ABC 乐谱规划阶段的采样'),
            ('歌曲生成', '歌曲 token 生成阶段；25 tokens ≈ 1 秒音乐，9000 ≈ 6 分钟上限'),
        )):
            header = label(title, 'Hint')
            header.setToolTip(tip)
            grid.addWidget(header, 0, col)

        # 七个采样参数（行）及其显示名
        rows = (
            ('temperature', '温度'),
            ('top_p', 'Top-p'),
            ('top_k', 'Top-k'),
            ('repetition_penalty', '重复惩罚'),
            ('min_tokens', '最小 tokens'),
            ('max_tokens', '最大 tokens'),
            ('penalty_window', '惩罚窗口'),
        )

        # 逐行、逐阶段创建控件并填入网格
        for i, (key, name) in enumerate(rows, 1):
            grid.addWidget(label(name), i, 0)
            for stage, stage_key in enumerate(('abc', 'semantic'), 1):
                # 整数型参数用 QSpinBox，浮点型参数用 QDoubleSpinBox
                if key in ('top_k', 'min_tokens', 'max_tokens', 'penalty_window'):
                    spin = QSpinBox()
                    if key == 'top_k':
                        spin.setRange(1, 1000)
                    elif key == 'penalty_window':
                        spin.setRange(1, 100)
                        spin.setToolTip('重复惩罚回看的 token 数量，范围 1～100')
                    else:
                        # min_tokens 允许从 0 起；max_tokens 上限按阶段区分（abc 4096 / semantic 9000）
                        spin.setRange(0 if key == 'min_tokens' else 1, 4096 if stage_key == 'abc' else 9000)
                        spin.setToolTip('最小 tokens 不得超过最大 tokens；下调最小值后可降低生成上限')
                else:
                    spin = QDoubleSpinBox()
                    spin.setDecimals(3)
                    spin.setSingleStep(0.01 if key != 'temperature' else 0.05)
                    spin.setRange(*{'temperature': (0, 5), 'top_p': (0.01, 1), 'repetition_penalty': (0.5, 3)}[key])
                self.fields[stage_key][key] = spin
                grid.addWidget(spin, i, stage)

        # 让每阶段的最大 tokens 始终不小于最小 tokens
        for stage_fields in self.fields.values():
            stage_fields['min_tokens'].valueChanged.connect(
                lambda value, maximum=stage_fields['max_tokens']: maximum.setMinimum(max(1, value))
            )

        # 底部时长提示，随歌曲生成阶段的 max_tokens 变化刷新
        self.length_hint = label('', 'Hint', wrap=True)
        self.fields['semantic']['max_tokens'].valueChanged.connect(self._length)
        self.body.addLayout(grid)
        self.body.addWidget(self.length_hint)

        # 初始化时载入默认参数
        self.reset()

    def _length(self, value):
        """按最大 token 数估算歌曲最长时长（25 tokens ≈ 1 秒）并显示。"""
        seconds = value / 25
        self.length_hint.setText(f'歌曲最长约 {int(seconds // 60)}:{int(seconds % 60):02d}（歌曲 token 上限 {value}）')

    def reset(self):
        """把两阶段采样参数全部恢复为 SAMPLING_DEFAULTS 默认值。"""
        for key, values in SAMPLING_DEFAULTS.items():
            self._load_stage(key, values)

    def _load_stage(self, key, values):
        """把某阶段参数写入对应控件；min_tokens 单独处理缺省值，避免被 0 覆盖。"""
        stage_fields = self.fields[key]
        stage_fields['min_tokens'].setValue(values.get('min_tokens', SAMPLING_DEFAULTS[key]['min_tokens']))
        for name, value in values.items():
            if name not in stage_fields or name == 'min_tokens':
                continue
            stage_fields[name].setValue(value)

    def values(self):
        """返回两阶段采样参数；未启用自定义采样时返回 (None, None)。"""
        if not self.enabled.isChecked():
            return (None, None)
        return tuple(
            {name: field.value() for name, field in self.fields[stage].items()}
            for stage in ('abc', 'semantic')
        )

    def load(self, abc=None, semantic=None):
        """从两阶段参数字典载入；先恢复默认，再覆盖已提供的阶段参数。"""
        self.reset()
        self.enabled.setChecked(bool(abc or semantic))
        for key, values in (('abc', abc), ('semantic', semantic)):
            self._load_stage(key, values or {})
