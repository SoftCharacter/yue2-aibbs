"""Score editing: revise the ABC plan (melody / chords), then let YuE2 render it.

本模块实现"乐谱编辑"页面：用户可载入 YuE2 规划出的 ABC 乐谱，直接修改旋律、
重新配和弦、添加独奏，或调整风格与歌词，再按新乐谱重新生成完整歌曲。
同时提供从作品文件夹载入、继续已保存任务、导出乐谱等流程。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QMessageBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import jobs
from ..engine import YUE2_STEPS, engine
from ..paths import new_run_dir, output_root
from ..settings import settings
from ..tasks import runner
from ..widgets.common import Card, button, label, scroll
from ..widgets.editors import AbcEditor, LyricsEditor
from ..widgets.result import ResultPanel
from ..widgets.score import ScoreView
from ..widgets.song_forms import (
    COT_OPTIONS,
    AdvancedSampling,
    GenerationSettings,
    StyleCard,
)
from .base import BasePage
from .create import validate_song_inputs


def load_run(folder: Path, verify: bool = False):
    """Read a generated song folder into {dir, audio, abc, meta}.

    从指定文件夹读取一次生成任务的成果：优先按 job.json 还原（含音频与元数据），
    否则回退到 meta.json / request.json + score.abc 的组合方式。找不到任何可识别
    参数且没有乐谱时返回 None。
    """
    folder = Path(folder)
    # 存在任务清单：直接按任务结果还原，格式异常时转为 ValueError 上报
    if (folder / 'job.json').is_file():
        try:
            return _validate_run(jobs.job_result(folder, verify=verify))
        except (AttributeError, TypeError, KeyError) as exc:
            raise ValueError(f'任务参数格式不正确：{exc}') from exc

    # 无任务清单：从 meta.json / request.json 中拼出元数据（request 优先覆盖旧 meta）
    meta = {}
    for name in ('meta.json', 'request.json'):
        path = folder / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            raise ValueError(f'{name} 必须包含参数对象')
        if name == 'request.json':
            meta = {**data, **meta}
        else:
            meta = data

    # 优先读取 score.abc，其次取元数据中内嵌的乐谱
    abc = ''
    if (folder / 'score.abc').is_file():
        abc = (folder / 'score.abc').read_text(encoding='utf-8', errors='replace')
    elif meta.get('abc'):
        abc = meta['abc']

    # 既无参数也无乐谱，视为无效目录
    if not meta and not abc:
        return None

    audio = folder / 'audio.flac'
    return _validate_run({
        'dir': str(folder),
        'audio': str(audio) if audio.exists() else '',
        'abc': abc,
        'meta': meta,
    })


def _validate_run(data):
    """Validate metadata before it reaches Qt forms or library formatting.

    对作品元数据做严格校验：限定各字段的类型与取值范围，非法值抛出 ValueError，
    确保后续表单回填与作品库格式化不会因脏数据崩溃。
    """
    meta = data.get('meta')
    if not isinstance(meta, dict):
        raise ValueError('作品参数必须是对象')
    meta = dict(meta)

    # 文本类字段：允许空字符串，但存在值时必须为 str
    for key in ('title', 'style', 'lyrics', 'created', 'mode', 'cot', 'source_audio'):
        value = meta.get(key)
        if value is None:
            if key in meta:
                meta[key] = ''
            continue
        if not isinstance(value, str):
            raise ValueError(f'作品参数 {key} 必须是文本')

    # 乐谱必须为文本（若存在）
    if data.get('abc') is not None and not isinstance(data['abc'], str):
        raise ValueError('ABC 乐谱必须是文本')

    # 数值类字段：不能是布尔值，且必须为有限数值
    for key in ('duration', 'generation_seconds', 'cfg_scale', 'seed', 'ode_steps', 'count'):
        value = meta.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'作品参数 {key} 必须是有限数值')

    # 必须是整数的字段
    for key in ('seed', 'ode_steps', 'count'):
        if meta.get(key) is None:
            continue
        if not isinstance(meta[key], int):
            raise ValueError(f'作品参数 {key} 必须是整数')

    # 种子允许范围校验
    if meta.get('seed') is not None and not (0 <= meta['seed'] < 0x8000000000000000):
        raise ValueError('作品种子超出允许范围')

    # 乐谱模式枚举校验
    if meta.get('cot') and meta['cot'] not in ('full', 'melody', 'off'):
        raise ValueError('作品乐谱模式不正确')

    # 采样 / 解码选项必须为对象，且内部数值合法
    for key in ('abc_sampling', 'semantic_sampling', 'decode_options'):
        value = meta.get(key)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f'作品参数 {key} 必须是对象')
        for k, v in (value or {}).items():
            # 解码模式仅允许三种取值，其余子项按有限数值校验
            if key == 'decode_options' and k == 'mode':
                if v not in ('auto', 'tiled', 'full'):
                    raise ValueError('作品解码模式不正确')
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f'作品参数 {key}.{k} 必须是有限数值')

    return dict(data, meta=meta)


class EditPage(BasePage):
    """乐谱编辑页面：修改 ABC 乐谱后按新乐谱重新生成歌曲。"""

    title = '🎼 乐谱编辑'
    subtitle = '修改 YuE2 规划出的 ABC 乐谱（改旋律、重配和弦、加独奏），或调整风格与歌词，然后按新乐谱重新生成。'

    def __init__(self, main):
        super().__init__(main)
        # 记录当前载入的作品目录与快照，用于判断编辑期间输入是否被改动
        self._loaded_dir = None
        self._loaded_snapshot = None

        # 未载入作品时的提示文案
        self.source = label(
            '未载入作品：可从创作页"仅生成乐谱草稿"、生成结果"编辑乐谱再生成"或作品库进入，也可直接打开 .abc 文件。',
            'Hint',
            wrap=True,
        )

        # 顶部页头右侧补充载入 / 保存 / 导出按钮
        self.header.right.addWidget(button('📂 载入作品文件夹', callback=self.load_folder))
        self.header.right.addWidget(button('💾 保存草稿', callback=self.save_project))
        self.header.right.addWidget(button('导出乐谱', callback=self.export_score))

        # 左侧标签页：乐谱 ABC / 风格与歌词 / 生成设置
        tabs = QTabWidget()

        # 标签页 1：乐谱 ABC 编辑器
        widget = QWidget()
        box = QVBoxLayout(widget)
        box.setContentsMargins(0, 8, 0, 0)
        box.addWidget(self.source)
        self.abc = AbcEditor()
        box.addWidget(self.abc, 1)
        tabs.addTab(widget, '🎼 乐谱 ABC')

        # 标签页 2：风格与歌词
        widget = QWidget()
        box = QVBoxLayout(widget)
        box.setContentsMargins(0, 8, 8, 0)
        box.setSpacing(12)
        self.style_card = StyleCard(expanded=False)
        card = Card('歌词', '改动歌词时，请保证段落与乐谱中的段落注释（% verse 等）大致对应。')
        self.lyrics = LyricsEditor(examples=False)
        self.lyrics.context_provider = lambda: {'style': self.style_card.text(), 'structure': 'units'}
        card.body.addWidget(self.lyrics)
        box.addWidget(self.style_card)
        box.addWidget(card)
        box.addStretch(1)
        tabs.addTab(scroll(widget), '风格与歌词')

        # 标签页 3：生成设置（仅保留旋律 + 和弦 / 仅旋律两种乐谱模式）
        widget = QWidget()
        box = QVBoxLayout(widget)
        box.setContentsMargins(0, 8, 8, 0)
        self.gen = GenerationSettings(cot_options=COT_OPTIONS[:2], cot='full')
        self.advanced = AdvancedSampling()
        self.gen.body.addWidget(self.advanced)
        box.addWidget(self.gen)
        box.addStretch(1)
        tabs.addTab(scroll(widget), '生成设置')

        # 右侧标签页：实时预览 / 生成结果
        self.right_tabs = QTabWidget()
        self.preview = ScoreView()
        self.right_tabs.addTab(self.preview, '👁 实时预览')
        self.result = ResultPanel(YUE2_STEPS, show_score=False)
        self.result.editRequested.connect(self.main.open_in_editor)
        self.result.libraryRequested.connect(self.main.open_in_library)
        self.result.toast.connect(self.toast)
        self.right_tabs.addTab(self.result, '🎵 生成结果')

        # 乐谱变更时防抖刷新实时预览
        self.abc.changed.connect(lambda: self.preview.set_abc(self.abc.text(), debounce=True))

        # 底部操作栏：重新规划 / 留白 / 停止 / 按此乐谱生成
        self.replan_btn = self.run_button(
            '🔁 重新规划乐谱',
            self.replan,
            primary=False,
            tooltip='用当前风格/歌词/种子重新让 YuE2 写一份乐谱（会覆盖编辑器内容）',
        )
        self.go_btn = self.run_button('🎵 按此乐谱生成', self.generate)
        bar = self.action_bar(self.replan_btn, None, self.stop_button(), self.go_btn)

        # 继续已保存任务按钮插入到"生成设置"标签页布局中
        self.resume_btn = self.run_button(
            '▶ 继续已保存任务',
            self.resume_saved,
            primary=False,
            tooltip='使用保存时的完整计划和参数，跳过已经完成的阶段',
        )
        box.insertWidget(1, self.resume_btn)
        self.resume_btn.setEnabled(False)

        # 左右两栏：左侧标签页表单，右侧预览 / 结果区
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 8, 0)
        left_box.addWidget(tabs, 1)
        left_box.addWidget(bar)
        split.addWidget(left)
        split.addWidget(self.right_tabs)
        split.setSizes([560, 640])
        self.root.addWidget(split, 1)

    def load(self, data):
        """把作品数据回填到页面各输入控件。"""
        meta = data.get('meta') or {}
        # 先回填乐谱与实时预览
        self.abc.setText(data.get('abc') or '')
        self.preview.set_abc(data.get('abc') or '')

        # 未要求保留输入或元数据缺少风格/歌词时，才覆盖风格与歌词
        keep_inputs = data.get('keep_inputs')
        if not keep_inputs or 'style' not in meta:
            self.style_card.setText(meta.get('style') or '')
        if not keep_inputs or 'lyrics' not in meta:
            self.lyrics.setText(meta.get('lyrics') or '')

        # 生成设置：乐谱模式仅允许 full / melody，否则回退为 full
        gen_meta = dict(meta)
        if gen_meta.get('cot') not in ('full', 'melody'):
            gen_meta['cot'] = 'full'
        self.gen.load(gen_meta)
        self.advanced.load(meta.get('abc_sampling'), meta.get('semantic_sampling'))

        # 记录载入目录与参数快照，供"继续已保存任务"比对
        self._loaded_dir = data.get('dir')
        self._loaded_snapshot = self.params(advance_seed=False)

        self.source.setText(
            (f'来源：{self._loaded_dir}\n' if self._loaded_dir else '来源：未保存的乐谱\n')
            + '修改后按乐谱生成会创建新任务；保留原参数可在"生成设置"继续已保存任务。'
        )
        self.set_busy(runner.busy)
        self.right_tabs.setCurrentIndex(0)

    def load_folder(self):
        """从用户选择的文件夹载入作品（包含 score.abc）。"""
        songs_dir = output_root(settings) / 'songs'
        folder = QFileDialog.getExistingDirectory(
            self,
            '选择作品文件夹（包含 score.abc）',
            str(songs_dir if songs_dir.exists() else output_root(settings)),
        )
        if not folder:
            return
        try:
            data = load_run(Path(folder), verify=True)
        except (OSError, ValueError) as exc:
            self.warn(f'载入失败：{exc}')
            return
        if data is None:
            self.warn('这个文件夹里没有找到 score.abc / request.json。')
            return
        self.load(data)

    def params(self, advance_seed=True):
        """汇总编辑页参数：生成设置、风格/歌词与 ABC 乐谱，供提交任务使用。"""
        values = self.gen.values(advance_seed=advance_seed)
        abc_sampling, semantic_sampling = self.advanced.values()
        return dict(
            values,
            style=self.style_card.text(),
            lyrics=self.lyrics.text().strip(),
            abc=self.abc.text(),
            mode='edit',
            abc_sampling=abc_sampling,
            semantic_sampling=semantic_sampling,
        )

    def input_snapshot(self):
        """Identify the loaded work and edits that an async plan must not replace.

        返回载入目录、完整参数与各编辑器文档修订号组成的快照，用于判断异步规划
        期间用户是否改动了输入，从而避免覆盖未保存的编辑。
        """
        return (
            self._loaded_dir,
            self.params(advance_seed=False),
            self.abc.edit.document().revision(),
            self.lyrics.edit.document().revision(),
            self.style_card.edit.document().revision(),
        )

    def generate(self):
        """按当前 ABC 乐谱生成完整歌曲：校验后提交后台任务。"""
        if not self.ensure_idle():
            return
        params = self.params()
        if not params['abc'].strip():
            self.warn('乐谱为空。请先载入或粘贴 ABC 乐谱。')
            return
        if not validate_song_inputs(self, params['style'], params['lyrics']):
            return
        self.right_tabs.setCurrentIndex(1)
        self.result.start('按乐谱生成中…')
        runner.submit(
            '按乐谱生成',
            engine.run_generate,
            params,
            on_done=self._done,
            on_error=self._error,
            on_progress=self.result.on_progress,
            on_event=self._event,
        )

    def _event(self, kind, payload):
        """接收后台任务进度事件，推送中间成果到结果面板或刷新作品库。"""
        if kind == 'item':
            self.result.add_result(payload)
        if kind in ('item', 'job'):
            self.main.library_changed()
        if kind == 'job':
            self.result.show_checkpoint(payload)

    def _done(self, results):
        """生成成功：结束结果面板并弹出完成提示。"""
        self.result.finish(True, f'完成 · 共 {len(results)} 首')
        self.toast('🎉 按新乐谱生成完成', 'ok')

    def _error(self, message, trace):
        """生成失败：结束结果面板；非取消场景弹出错误对话框。"""
        self.result.finish(False, message.splitlines()[0][:160])
        if message != '已取消':
            self.main.show_error('生成失败', message, trace)

    def replan(self):
        """重新规划乐谱：用当前风格/歌词/种子让 YuE2 重写一份乐谱（覆盖编辑器）。"""
        if not self.ensure_idle():
            return
        params = self.params()
        if not validate_song_inputs(self, params['style'], params['lyrics']):
            return
        # 编辑器已有内容时需用户确认覆盖
        if self.abc.text().strip() and QMessageBox.question(
            self, '重新规划', '重新规划会覆盖当前乐谱编辑器中的内容，继续吗？'
        ) != QMessageBox.Yes:
            return
        self.right_tabs.setCurrentIndex(1)
        self.result.start('正在规划乐谱…')

        # 记录提交时快照，规划期间输入有变则只保存乐谱、不覆盖当前内容
        snapshot = self.input_snapshot()

        def done(result):
            if self.input_snapshot() != snapshot:
                self.result.finish(True, '乐谱已保存；当前输入已变更，未覆盖编辑内容')
                self.toast('新乐谱已保存，当前编辑已保留', 'warn')
                return
            self.load(dict(result, meta=result.get('meta') or params))
            self.result.finish(True, '乐谱已更新')
            self.right_tabs.setCurrentIndex(0)

        runner.submit(
            '规划乐谱',
            engine.run_plan,
            params,
            on_done=done,
            on_error=self._error,
            on_progress=self.result.on_progress,
            on_event=self._event,
        )

    def set_busy(self, busy):
        """按后台任务忙闲切换各按钮状态，并联动"继续已保存任务"按钮。"""
        super().set_busy(busy)
        if hasattr(self, 'resume_btn'):
            self.resume_btn.setEnabled(not busy and self._can_resume())

    def _can_resume(self):
        """判断当前载入的作品是否存在可继续的已保存任务。"""
        if not self._loaded_dir:
            return False
        if not (Path(self._loaded_dir) / 'job.json').is_file():
            return False
        try:
            job = jobs.read_job(self._loaded_dir, verify=False)
            return (
                job['kind'] == 'song'
                and jobs.can_resume(self._loaded_dir, job)
                and job['params'].get('cot') != 'off'
            )
        except (OSError, ValueError):
            return False

    def resume_saved(self):
        """继续已保存任务：跳过已完成阶段，仅在输入未被修改时允许。"""
        if not self.ensure_idle():
            return
        if not self._can_resume():
            return
        if self.params(advance_seed=False) != self._loaded_snapshot:
            self.warn('当前输入已修改。请按此乐谱生成新任务，或重新载入原任务后继续；不会把改过的参数用于旧阶段。')
            return
        self.right_tabs.setCurrentIndex(1)
        self.result.start('继续已保存的阶段…')
        runner.submit(
            '继续生成',
            engine.run_resume,
            {'dir': self._loaded_dir},
            on_done=self._done,
            on_error=self._error,
            on_progress=self.result.on_progress,
            on_event=self._event,
        )

    def save_project(self):
        """把当前编辑保存为完整草稿：未改动时复制原计划，否则按新参数保存。"""
        try:
            p = self.params(advance_seed=False)
            # 输入未变且原目录完整：直接复制计划，保持可复现
            if (
                self._loaded_dir
                and p == self._loaded_snapshot
                and (Path(self._loaded_dir) / 'job.json').is_file()
                and (Path(self._loaded_dir) / 'plan.json').is_file()
            ):
                dest = new_run_dir('songs', 'plan_draft', settings)
                out = jobs.copy_plan(self._loaded_dir, dest)
            else:
                out = engine.save_project(p)
        except (OSError, ValueError) as exc:
            self.warn(f'保存失败：{exc}')
            return
        self.load(out)
        self.main.library_changed()
        self.toast('完整草稿已保存，原作品保持不变', 'ok')
        return out

    def export_score(self):
        """导出当前乐谱为图片 / PDF（通过乐谱渲染导出面板）。"""
        from ..widgets.render_export import show_render_export

        show_render_export(self, source_dir=self._loaded_dir, abc=self.abc.text())

    def primary_action(self):
        """Ctrl+Enter 触发的默认操作：按此乐谱生成。"""
        self.generate()
