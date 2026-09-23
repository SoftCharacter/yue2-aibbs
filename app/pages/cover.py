"""AI cover: SheetSage2 melody transcription → YuE2 melody-conditioned generation.

本模块实现「AI 翻唱」页面：上传原曲 → SheetSage2 扒出旋律乐谱（ABC）→ 整理歌词 →
选择新风格 → YuE2 按原旋律重新编曲演唱。同时承载源歌曲与乐谱/歌词一致性校验、
仅旋律模式的和弦处理、以及扒谱/识别歌词/生成翻唱三条后台任务的提交与回调。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QMessageBox,
)

from ..audio_utils import AUDIO_FILTER
from ..engine import LYRICS_STEPS, YUE2_STEPS, engine
from ..lyrics_utils import LANGUAGES
from ..tasks import runner
from ..theme import C
from ..widgets.common import Card, DropZone, label
from ..widgets.editors import AbcEditor, LyricsEditor, count_chords
from ..widgets.player import AudioPlayer
from ..widgets.result import ResultPanel
from ..widgets.song_forms import AdvancedSampling, GenerationSettings, StyleCard

from .base import BasePage
from .create import validate_song_inputs

# 翻唱「仅旋律 / 旋律+和弦」两个选项：(显示名, 值, 说明)，供生成设置的 cot 下拉框使用
COVER_COT = [
    ('仅旋律（推荐）', 'melody', '只沿用原曲旋律，由 YuE2 重新配和声，官方推荐的翻唱方式'),
    ('旋律 + 和弦', 'full', '同时沿用乐谱中的和弦符号（需要乐谱带和弦）'),
]


class CoverPage(BasePage):
    """AI 翻唱页面：五步流程（源歌曲 → 乐谱 → 歌词 → 风格 → 生成）。"""

    # 页面标题与副标题，显示在顶部 PageHeader
    title = '🎙️ AI 翻唱'
    subtitle = '上传一首已有歌曲 → SheetSage2 扒出旋律乐谱 → 整理歌词 → 选择新风格，YuE2 按原旋律重新编曲演唱。'

    def __init__(self, main):
        # 先走基类初始化（构建两栏骨架、绑定忙闲状态）
        super().__init__(main)

        # ── 第 1 步：源歌曲卡片 ──
        card_source = Card(
            '源歌曲',
            '上传要翻唱的歌曲，用 SheetSage2 自动扒取旋律。也可以跳过这一步，直接粘贴/打开已有的 ABC 乐谱。',
            step=1,
        )
        # 拖拽上传区 + 紧凑音频播放器
        self.drop = DropZone('拖入原曲音频，或点击选择', AUDIO_FILTER)
        self.source_player = AudioPlayer(compact=True)
        # 扒谱得到的段落结构（list）及其来源音频路径；初始为空
        self.structure = ''
        self.structure_audio = ''
        # 源歌曲版本号：每次更换源文件自增，用于判断异步任务结果是否过期
        self._source_version = 0
        self.drop.fileChanged.connect(self._source_changed)
        card_source.body.addWidget(self.drop)
        card_source.body.addWidget(self.source_player)

        # 仅旋律复选框 + 扒谱按钮，横向排布
        row = QHBoxLayout()
        self.melody_only = QCheckBox('仅旋律（去掉和弦符号，翻唱推荐）')
        self.melody_only.setChecked(True)
        # 勾选状态切换时，同步生成设置的 cot 值（melody/full）
        self.melody_only.toggled.connect(
            lambda on: self.gen.cot.setValue('melody' if on else 'full')
        )
        self.transcribe_btn = self.run_button('🎼 扒取旋律', self.transcribe, primary=False)
        row.addWidget(self.melody_only)
        row.addStretch(1)
        row.addWidget(self.transcribe_btn)
        card_source.body.addLayout(row)

        # 源歌曲状态提示行（扒谱结果/警告信息）
        self.source_status = label('', 'Hint', wrap=True)
        card_source.body.addWidget(self.source_status)

        # ── 第 2 步：旋律乐谱卡片 ──
        card_abc = Card(
            '旋律乐谱 (ABC)',
            '检查或修改扒出来的乐谱；右侧“乐谱预览”可以看五线谱并试听旋律。',
            step=2,
        )
        self.abc = AbcEditor()
        self.abc.edit.setMinimumHeight(240)
        # 乐谱文本变化时刷新右侧预览；已有结果在预览时则跳过（避免覆盖）
        self.abc.changed.connect(
            lambda: None
            if self.result.current
            else self.result.show_score(self.abc.text, debounce=True)
        )
        # 手动导入 ABC 文件后更新来源提示
        self.abc.fileLoaded.connect(self._abc_file_loaded)
        self.abc_origin = label('', 'Hint', wrap=True)
        card_abc.body.addWidget(self.abc_origin)
        card_abc.body.addWidget(self.abc)

        # 乐谱与歌词的来源音频路径，用于一致性校验
        self.abc_source, self.lyrics_source = '', ''

        # ── 第 3 步：歌词卡片 ──
        card_lyrics = Card(
            '歌词',
            '按原曲的演唱顺序整理歌词，并用 [Verse] [Chorus] 等标签标出与原曲对应的段落。'
            '可以一键用 Qwen3-ASR 听写原曲歌词（扒过谱会自动按原曲段落加标签），再手动修正错字。',
            step=3,
        )
        asr_row = QHBoxLayout()
        self.asr_language = QComboBox()
        # 演唱语言下拉框：来自 lyrics_utils 的语言清单
        for name, code in LANGUAGES:
            self.asr_language.addItem(name, code)
        self.asr_language.setToolTip('演唱语言')
        self.asr_btn = self.run_button(
            '🎤 识别原曲歌词',
            self.recognize_lyrics,
            primary=False,
            tooltip='Qwen3-ASR 识别 + ForcedAligner 断句，结果会替换下方歌词',
        )
        asr_row.addWidget(label('语言'))
        asr_row.addWidget(self.asr_language)
        asr_row.addStretch(1)
        asr_row.addWidget(self.asr_btn)
        card_lyrics.body.addLayout(asr_row)

        # 歌词编辑器；上下文提供器为 AI 改词补充风格与结构约束
        self.lyrics = LyricsEditor(examples=False)
        self.context_provider = lambda: {'style': self.style_card.text(), 'structure': 'units'}
        card_lyrics.body.addWidget(self.lyrics)

        # ── 第 4 步：目标风格 ──
        self.style_card = StyleCard(
            '目标风格',
            step=4,
            expanded=False,
            hint='描述翻唱后的新风格，例如：Jazz-funk, warm lead vocal, Rhodes piano, electric bass, tight drums',
        )
        self.style_card.setText('Jazz-funk, warm lead vocal, Rhodes piano, electric bass, tight drums')

        # ── 第 5 步：生成设置 ──
        self.gen = GenerationSettings(step=5, cot_options=COVER_COT, cot='melody', show_count=True)
        self.gen.seed.setValue(831001)
        # 高级采样参数折叠进生成设置卡片
        self.advanced = AdvancedSampling()
        self.gen.body.addWidget(self.advanced)

        # ── 结果面板与操作栏 ──
        self.result = ResultPanel(YUE2_STEPS)
        self.result.editRequested.connect(self.main.open_in_editor)
        self.result.libraryRequested.connect(self.main.open_in_library)
        self.result.toast.connect(self.toast)

        self.go_btn = self.run_button('🎙️ 生成翻唱', self.generate)
        bar = self.action_bar(None, self.stop_button(), self.go_btn)
        # 左侧五个步骤卡片，右侧结果面板
        self.two_columns(
            [card_source, card_abc, card_lyrics, self.style_card, self.gen], self.result, bar
        )

    def _source_changed(self, path):
        """源歌曲更换时：刷新播放器、失效旧的扒谱结构并更新来源提示。"""
        self._source_version += 1
        self.source_player.set_source(path)
        # 更换了源歌曲后，之前扒出的结构/结构音频即失效
        if path != self.structure_audio:
            self.structure = []
            self.structure_audio = ''
        self._update_origin()

    def _set_structure(self, structure, audio):
        """记录扒谱得到的段落结构及其来源音频路径。"""
        self.structure = list(structure) if structure else []
        self.structure_audio = audio if audio else ''

    def _abc_file_loaded(self, path):
        """手动导入 ABC 文件后：清空来源并提示「手动导入」。"""
        self.abc_source = ''
        self.abc_origin.setStyleSheet('')
        self.abc_origin.setText(f'乐谱来源：手动导入 {Path(path).name}')

    def _update_origin(self):
        """根据乐谱来源刷新「乐谱来源」提示行，来源与源歌曲不一致时给出琥珀色警告。"""
        # 没有来源（手动粘贴/尚未扒谱）时，仅在原本显示手动导入时保留
        if not self.abc_source:
            if not self.abc_origin.text().startswith('乐谱来源：手动导入'):
                self.abc_origin.setText('')
            return
        name = Path(self.abc_source).name
        # 乐谱来自之前的源歌曲，而当前源歌曲已更换 → 警告
        if self.drop.path and self.drop.path != self.abc_source:
            self.abc_origin.setText(
                f'⚠ 当前乐谱扒自 {name}，与第 1 步的源歌曲不一致，请重新“扒取旋律”'
            )
            self.abc_origin.setStyleSheet(f'color:{C["amber"]};')
        else:
            self.abc_origin.setText(f'乐谱来源：SheetSage2 扒自 {name}')
            self.abc_origin.setStyleSheet('')

    def _stale_parts(self):
        """列出与当前源歌曲不一致的乐谱/歌词来源，用于生成前的确认提示。"""
        path = self.drop.path
        parts = []
        # 乐谱来自其它音频文件时提示
        if path and self.abc_source and self.abc_source != path:
            parts.append(f'乐谱（扒自 {Path(self.abc_source).name}）')
        # 歌词来自其它音频文件时提示
        if path and self.lyrics_source and self.lyrics_source != path:
            parts.append(f'歌词（识别自 {Path(self.lyrics_source).name}）')
        return parts

    def transcribe(self):
        """提交 SheetSage2 扒谱任务；完成后把 ABC 乐谱填入编辑器并刷新预览。"""
        # 有任务在跑时禁止并发
        if not self.ensure_idle():
            return
        # 源音频必须已选择且文件存在
        if not (self.drop.path and Path(self.drop.path).exists()):
            self.warn('请先选择源歌曲音频文件。')
            return

        # 局部导入扒谱步骤标签，避免模块级引入不必要依赖
        from ..engine import SHEETSAGE_STEPS

        self.result.progress.start('SheetSage2 扒谱中…', SHEETSAGE_STEPS)
        self.source_status.setText('')
        # 记录提交时的输入快照，用于结果返回后判断输入是否已被改动
        path = self.drop.path
        source_version = self._source_version
        abc_revision = self.abc.edit.document().revision()

        def done(result):
            """扒谱完成回调：输入未变时填入乐谱，否则仅保存结果不覆盖当前编辑。"""
            # 源歌曲或乐谱在任务期间被修改 → 结果过期，只保存不填入
            if self.drop.path != path or (
                source_version,
                abc_revision,
            ) != (self._source_version, self.abc.edit.document().revision()):
                self.result.finish(True, '源歌曲或乐谱已修改，本次扒谱结果已保存，未填入')
                self.source_status.setText(
                    f'⚠ 扒谱期间输入已修改，{Path(path).name} 的结果未填入（已保存在 {result["dir"]}）'
                )
                self.toast('输入已修改，扒谱结果已保存，当前修改已保留', 'warn')
                return

            self._set_structure(result.get('structure'), path)
            if result.get('abc'):
                # 填入乐谱、更新来源、刷新五线谱预览与状态摘要
                self.abc.setText(result['abc'])
                self.abc_source = path
                self._update_origin()
                self.result.current = None
                self.result.show_score(result['abc'])
                key = ', '.join(sorted({k[2] for k in result['keys']})) or '未知'
                bpm = f'{result["bpm"]:.0f} BPM' if result.get('bpm') else '未知速度'
                self.source_status.setText(
                    f'✓ 扒谱完成：调性 {key} · {bpm} · '
                    f'{result["melody_notes"]} 个音符 · 输出 {result["dir"]}'
                )
                self.result.finish(True, '旋律乐谱已就绪，请整理歌词后生成翻唱')
                self.toast('旋律扒取完成', 'ok')
            else:
                # 未得到 ABC：提取 error / abc_error 给出可读提示
                err = result.get('error') or result.get('abc_error') or '未生成 ABC'
                self.source_status.setText(f'⚠ 没有得到 ABC 乐谱：{err}')
                self.result.finish(False, '扒谱未得到乐谱')

        def error(message, trace):
            """扒谱失败回调：结束进度条，非取消时弹出错误详情。"""
            self.result.progress.finish(False, message.splitlines()[0:160])
            if message != '已取消':
                self.main.show_error('扒谱失败', message, trace)

        runner.submit(
            'SheetSage2 扒谱',
            engine.run_transcribe,
            {'audio': self.drop.path, 'melody_only': self.melody_only.isChecked()},
            on_done=done,
            on_error=error,
            on_progress=self.result.on_progress,
        )

    def generate(self):
        """提交 YuE2 翻唱生成任务；生成前做一致性校验、和弦处理与参数组装。"""
        if not self.ensure_idle():
            return
        # 必须有旋律乐谱
        if not self.abc.text().strip():
            self.warn('请先扒取旋律或粘贴 ABC 乐谱。翻唱需要原曲的旋律乐谱。')
            return

        # 乐谱/歌词来源与源歌曲不一致时，弹窗让用户选择重新扒谱或继续
        stale = self._stale_parts()
        if stale:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle('源歌曲与乐谱/歌词不一致')
            box.setText(
                '第 1 步的源歌曲已更换为 '
                + Path(self.drop.path).name
                + '，但当前的'
                + '、'.join(stale)
                + ' 来自之前的歌曲。\n\n要为新歌曲重新扒取旋律吗？'
            )
            btn_retranscribe = box.addButton('重新扒取旋律', QMessageBox.AcceptRole)
            btn_keep = box.addButton('仍使用现有乐谱和歌词', QMessageBox.DestructiveRole)
            box.addButton('取消', QMessageBox.RejectRole)
            box.exec()
            # 用户选择重新扒谱：直接触发扒谱并返回
            if box.clickedButton() is btn_retranscribe:
                self.transcribe()
                return
            # 既不是「重新扒谱」也不是「仍使用现有」→ 视为取消
            if box.clickedButton() is not btn_keep:
                return

        # 汇总生成参数：基础设置 + 风格 + 歌词 + 乐谱 + 采样方式 + 源音频
        values = self.gen.values()
        abc_sampling, semantic_sampling = self.advanced.values()
        source_audio = self.abc_source or self.drop.path or None
        params = dict(
            values,
            style=self.style_card.text(),
            lyrics=self.lyrics.text().strip(),
            abc=self.abc.text(),
            mode='cover',
            abc_sampling=abc_sampling,
            semantic_sampling=semantic_sampling,
            source_audio=source_audio,
        )

        # 乐谱过长时拒绝生成，避免模型侧异常
        if len(params['abc']) > 100000:
            self.warn('ABC 乐谱过长（超过 100,000 字符）。')
            return

        # 校验风格与歌词（弹窗提示缺失项）
        if not validate_song_inputs(self, params['style'], params['lyrics']):
            return

        # 仅旋律模式遇到带和弦的乐谱时，询问是否去掉和弦符号
        if params['cot'] == 'melody' and count_chords(params['abc']):
            choice = QMessageBox.question(
                self,
                '乐谱含有和弦符号',
                '“仅旋律”模式不会自动去掉和弦符号。\n要先去掉乐谱中的和弦符号吗？',
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if choice == QMessageBox.Cancel:
                return
            if choice == QMessageBox.Yes:
                self.abc.remove_chords()
                params['abc'] = self.abc.text()

        # 启动结果面板并提交生成任务
        self.result.progress.set_steps(YUE2_STEPS)
        self.result.start('正在生成翻唱…')
        runner.submit(
            '生成翻唱',
            engine.run_generate,
            params,
            on_done=self._done,
            on_error=self._error,
            on_progress=self.result.on_progress,
            on_event=self._event,
        )

    def _event(self, kind, payload):
        """生成过程中的事件回调：新增结果、刷新作品库、展示检查点。"""
        # 每产出一首歌就追加到结果面板
        if kind == 'item':
            self.result.add_result(payload)
        # 产出或任务推进时刷新作品库
        if kind in ('item', 'job'):
            self.main.library_changed()
        # 任务检查点（阶段性产物）
        if kind == 'job':
            self.result.show_checkpoint(payload)

    def _done(self, results):
        """生成完成回调：结束结果面板并提示产出数量。"""
        self.result.finish(True, f'翻唱完成 · 共 {len(results)} 首')
        self.toast('🎉 翻唱生成完成', 'ok')

    def _error(self, message, trace):
        """生成失败回调：结束结果面板，非取消时弹出错误详情。"""
        self.result.finish(False, message.splitlines()[0:160])
        if message != '已取消':
            self.main.show_error('翻唱失败', message, trace)

    def recognize_lyrics(self):
        """提交 Qwen3-ASR 歌词识别任务；完成后替换歌词编辑器内容。"""
        if not self.ensure_idle():
            return
        # 原曲音频必须已选择
        if not (self.drop.path and Path(self.drop.path).exists()):
            self.warn('请先在第 1 步选择原曲音频。')
            return
        # 已有歌词时，确认是否覆盖
        if self.lyrics.text().strip() and QMessageBox.question(
            self, '识别歌词', '识别结果会替换当前歌词，继续吗？'
        ) != QMessageBox.Yes:
            return

        # 有扒谱结构且结构来源就是当前源音频 → 按原曲段落对齐；否则按停顿分段
        has_structure = bool(self.structure) and self.structure_audio == self.drop.path
        params = {
            'audio': self.drop.path,
            'language': self.asr_language.currentData(),
            'align': True,
            'sections': 'given' if has_structure else 'none',
            'structure': self.structure if has_structure else [],
        }

        self.result.progress.start('识别原曲歌词…', LYRICS_STEPS)
        # 记录提交时的输入快照
        path = self.drop.path
        source_version = self._source_version
        lyrics_revision = self.lyrics.edit.document().revision()

        def done(result):
            """识别完成回调：输入未变时替换歌词，否则仅保存结果。"""
            if self.drop.path != path or (
                source_version,
                lyrics_revision,
            ) != (self._source_version, self.lyrics.edit.document().revision()):
                self.result.progress.finish(
                    True, f'输入已修改，本次识别结果未填入；已保存到 {result.get("dir", "")}'
                )
                self.toast('源歌曲或歌词已修改，当前修改已保留', 'warn')
                return
            # 可撤销地替换歌词，并记录来源
            self.lyrics._replace_undoable(result['lyrics'])
            self.lyrics_source = path
            self.result.progress.finish(
                True,
                f'歌词识别完成 · {len(result["lines"])} 行 · '
                + ('已按原曲段落加标签' if has_structure else '未扒谱，按停顿分段'),
            )
            self.toast('歌词已填入，请对照原曲修正错字（Ctrl+Z 可撤销）', 'ok')

        def error(message, trace):
            """识别失败回调：结束进度条，非取消时弹出错误详情。"""
            self.result.progress.finish(False, message.splitlines()[0:160])
            if message != '已取消':
                self.main.show_error('歌词识别失败', message, trace)

        runner.submit(
            '识别歌词',
            engine.run_lyrics,
            params,
            on_done=done,
            on_error=error,
            on_progress=self.result.on_progress,
        )

    def load_transcription(self, result):
        """把扒谱结果载入本页（由扒谱分析页跳转过来）。"""
        # 有源音频时回填拖拽区
        if result.get('audio'):
            self.drop.setPath(result['audio'])
        # 恢复结构信息；audio 缺失时回落到当前源音频
        self._set_structure(result.get('structure'), result.get('audio') or self.drop.path)
        # 有 ABC 时填入乐谱、更新来源并刷新预览
        if result.get('abc'):
            self.abc.setText(result['abc'])
            self.abc_source = result.get('audio') or ''
            self.result.current = None
            self.result.show_score(result['abc'])
        self._update_origin()
        # 恢复「仅旋律」勾选状态，缺省为 True
        self.melody_only.setChecked(bool(result.get('melody_only', True)))

    def primary_action(self):
        """Ctrl+Enter 主操作：直接触发翻唱生成。"""
        self.generate()
