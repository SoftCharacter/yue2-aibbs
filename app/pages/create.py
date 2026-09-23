"""Text-to-song: style + lyrics → full song.

本模块实现"创作歌曲"页面：用户输入风格描述与歌词，YuE2 先写出旋律与和弦乐谱，
再据此生成带人声和伴奏的完整歌曲（48 kHz 立体声）。页面还提供草稿保存/载入、
乐谱草稿单独规划、结果送入乐谱编辑页或作品库等流程。
"""
from __future__ import annotations

from ..engine import YUE2_STEPS, engine
from ..settings import settings
from ..tasks import runner
from ..widgets.common import Card, button
from ..widgets.editors import LyricsEditor
from ..widgets.result import ResultPanel
from ..widgets.song_forms import AdvancedSampling, GenerationSettings, StyleCard
from .base import BasePage


def validate_song_inputs(page, style, lyrics):
    """校验创作页输入：风格与歌词非空，且长度在限制内，否则弹窗提示并返回 False。"""
    if not style:
        page.warn('请先填写风格描述（流派、人声、乐器、情绪等）。')
        return False
    if not lyrics.strip():
        page.warn('请先填写歌词，并用 [Verse] / [Chorus] 等段落标签组织。')
        return False
    if len(style) > 1000:
        page.warn('风格描述请控制在 1000 字符以内。')
        return False
    if len(lyrics) > 12000:
        page.warn('歌词请控制在 12000 字符以内。')
        return False
    return True


class CreatePage(BasePage):
    """创作歌曲页面：风格 + 歌词 → 完整歌曲，兼作其他页面载入参数的入口。"""

    title = '🎵 创作歌曲'
    subtitle = '输入风格描述和歌词，YuE2 会先写出旋律与和弦乐谱，再生成带人声和伴奏的完整歌曲（48 kHz 立体声）。'

    def __init__(self, main):
        super().__init__(main)
        # 顶部页头右侧补充草稿保存/载入按钮
        self.header.right.addWidget(button('💾 保存草稿', callback=self.save_project))
        self.header.right.addWidget(button('📂 载入草稿', callback=self.load_project))

        # 步骤 1：风格描述卡片（支持流派、人声、乐器、情绪等）
        self.style_card = StyleCard(step=1)

        # 步骤 2：歌词编辑卡片，内含可写歌词编辑器
        self.lyrics_card = Card(
            '歌词',
            '用 [Verse] 主歌、[Chorus] 副歌 等段落标签组织歌词，每行一句。歌词越长歌曲越长。',
            step=2,
        )
        self.lyrics = LyricsEditor(write=True)
        self.lyrics.edit.setMinimumHeight(300)
        # 选中示例时把对应风格回填到风格卡片
        self.lyrics.exampleChosen.connect(lambda style, _l: self.style_card.setText(style))
        # 歌词编辑器所需的上下文（AI 改写/续写时取风格）与风格回填钩子
        self.lyrics.context_provider = lambda: {'style': self.style_card.text(), 'structure': 'off'}
        self.lyrics.style_setter = self.style_card.setText
        self.lyrics_card.body.addWidget(self.lyrics)

        # 步骤 3：生成设置，其中挂载高级采样选项
        self.gen = GenerationSettings(step=3)
        self.advanced = AdvancedSampling()
        self.gen.body.addWidget(self.advanced)

        # 结果面板：展示生成进度与成品，并接通编辑/作品库/提示的跨页跳转
        self.result = ResultPanel(YUE2_STEPS)
        self.result.editRequested.connect(self.main.open_in_editor)
        self.result.libraryRequested.connect(self.main.open_in_library)
        self.result.toast.connect(self.toast)

        # 底部操作栏：乐谱草稿按钮、弹性留白、停止按钮、生成按钮
        self.plan_btn = self.run_button(
            '📝 仅生成乐谱草稿',
            self.plan,
            primary=False,
            tooltip='只做乐谱规划（很快），然后在乐谱编辑页修改旋律/和弦后再生成',
        )
        self.go_btn = self.run_button('✨ 生成歌曲', self.generate)
        bar = self.action_bar(self.plan_btn, None, self.stop_button(), self.go_btn)

        # 左右两栏：左侧表单，右侧结果区
        self.two_columns([self.style_card, self.lyrics_card, self.gen], self.result, bar)
        self.restore_draft()

    def params(self, advance_seed=True):
        """汇总创作参数：生成设置、风格/歌词与采样选项，供提交任务使用。"""
        values = self.gen.values(advance_seed=advance_seed)
        abc_sampling, semantic_sampling = self.advanced.values()
        return dict(
            values,
            style=self.style_card.text(),
            lyrics=self.lyrics.text().strip(),
            mode='create',
            abc_sampling=abc_sampling,
            semantic_sampling=semantic_sampling,
        )

    def generate(self):
        """开始生成完整歌曲：校验输入后提交后台任务。"""
        if not self.ensure_idle():
            return
        params = self.params()
        if not validate_song_inputs(self, params['style'], params['lyrics']):
            return
        self.save_draft()
        count = params['count']
        # 单首与批量显示不同的起始提示
        self.result.start('正在生成…' if count == 1 else f'正在批量生成 {count} 首…')
        runner.submit(
            '生成歌曲',
            engine.run_generate,
            params,
            on_done=lambda results: self._done(results),
            on_error=self._error,
            on_progress=self.result.on_progress,
            on_event=self._event,
        )

    def _event(self, kind, payload):
        """接收后台任务的进度事件，把中间成果推送到结果面板或刷新作品库。"""
        if kind == 'item':
            self.result.add_result(payload)
        if kind in ('item', 'job'):
            self.main.library_changed()
        if kind == 'job':
            self.result.show_checkpoint(payload)

    def _done(self, results):
        """生成成功：统计总时长并结束结果面板，弹出完成提示。"""
        seconds = sum(r['seconds'] for r in results)
        self.result.finish(
            True,
            f'完成 · 共 {len(results)} 首 · {int(seconds // 60)}:{int(seconds % 60):02d}',
        )
        self.toast('🎉 歌曲生成完成', 'ok')

    def _error(self, message, trace):
        """生成失败：结束结果面板；非取消场景再弹出带堆栈的错误对话框。"""
        self.result.finish(False, message.splitlines()[0][:160])
        if message != '已取消':
            self.main.show_error('生成失败', message, trace)

    def plan(self):
        """仅规划乐谱草稿：生成 ABC 乐谱后送入乐谱编辑页，供用户微调后再生成歌曲。"""
        if not self.ensure_idle():
            return
        params = self.params()
        # 不使用乐谱模式没有可编辑的乐谱，直接提示切换
        if params['cot'] == 'off':
            self.warn('“不使用乐谱”模式没有乐谱可编辑，请切换到“旋律 + 和弦”或“仅旋律”。')
            return
        if not validate_song_inputs(self, params['style'], params['lyrics']):
            return
        self.save_draft()
        self.result.start('正在规划乐谱…')

        # 记录提交时的参数与编辑状态，用于完成后判断用户是否在等待期间改了内容
        before = self.params(advance_seed=False)
        revisions = (
            self.lyrics.edit.document().revision(),
            self.style_card.edit.document().revision(),
        )
        edit_page = self.main.pages['edit']
        edit_snapshot = edit_page.input_snapshot()

        def done(result):
            # 规划期间输入或编辑页有变动：只保存乐谱，不覆盖当前内容
            if (
                self.params(advance_seed=False) != before
                or revisions
                != (
                    self.lyrics.edit.document().revision(),
                    self.style_card.edit.document().revision(),
                )
                or edit_page.input_snapshot() != edit_snapshot
            ):
                self.result.finish(True, '乐谱已保存；输入或编辑中的作品已变更，未自动载入')
                self.toast('新乐谱已保存，可从作品库载入；当前修改已保留', 'warn')
                return
            self.result.finish(True, f'乐谱草稿完成 · {len(result["abc"] or "")} 字符')
            self.main.open_in_editor(dict(result, meta=result.get('meta') or params))
            self.toast('乐谱草稿已送到乐谱编辑页', 'ok')

        runner.submit(
            '规划乐谱',
            engine.run_plan,
            params,
            on_done=done,
            on_error=self._error,
            on_progress=self.result.on_progress,
            on_event=self._event,
        )

    def load_params(self, meta):
        """从作品/草稿元数据恢复页面各输入控件。"""
        self.style_card.setText(meta.get('style', ''))
        self.lyrics.setText(meta.get('lyrics', ''))
        self.gen.load(meta)
        self.advanced.load(meta.get('abc_sampling'), meta.get('semantic_sampling'))

    def primary_action(self):
        """Ctrl+Enter 触发的默认操作：开始生成。"""
        self.generate()

    def save_draft(self):
        """把当前创作参数写入草稿存储，供下次启动时恢复。"""
        settings.setdefault('drafts', {})['create'] = self.params(advance_seed=False)

    def save_project(self):
        """把当前输入保存为完整草稿（作品库可载入），并刷新作品库。"""
        try:
            result = engine.save_project(self.params(advance_seed=False))
        except (OSError, ValueError) as exc:
            self.warn(f'保存失败：{exc}')
            return
        self.save_draft()
        self.main.library_changed()
        self.toast('完整草稿已保存，可在作品库载入或继续生成', 'ok')
        return result

    def load_project(self):
        """从用户选择的文件夹载入草稿或作品参数，有乐谱则进编辑页，否则回填本页。"""
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        from ..paths import output_root
        from .edit import load_run

        folder = QFileDialog.getExistingDirectory(
            self, '选择草稿或作品文件夹', str(output_root(settings) / 'songs')
        )
        if not folder:
            return
        try:
            data = load_run(Path(folder), verify=True)
            if data is None:
                raise ValueError('未找到草稿或作品参数')
            if data.get('abc'):
                self.main.open_in_editor(data)
            else:
                self.load_params(data['meta'])
            self.toast('草稿已载入；原任务可在作品库继续', 'ok')
        except (OSError, ValueError) as exc:
            self.warn(f'载入失败：{exc}')

    def restore_draft(self):
        """启动时恢复上次草稿；没有草稿则用首个歌词示例填充空页面。"""
        draft = settings.get('drafts', {}).get('create')
        if draft:
            self.load_params(draft)
        else:
            from ..widgets.editors import LYRIC_EXAMPLES

            style, lyrics = next(iter(LYRIC_EXAMPLES.values()))
            self.style_card.setText(style)
            self.lyrics.setText(lyrics)
