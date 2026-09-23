"""Right-hand panel that shows YuE2 generation progress and results.

本模块实现结果面板 ResultPanel：负责展示 YuE2 生成任务的进度与结果。面板从上到下依次为
多阶段进度卡片、已保存提示行、生成结果卡片（内含音频播放器、历史列表与操作按钮），以及
承载乐谱预览 / ABC 文本 / 生成信息三个标签页。模块顶部另提供 export_mp3，用本地 ffmpeg
把 FLAC/WAV 转码为 MP3。
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..audio_utils import ffmpeg_path
from ..paths import open_in_explorer
from ..settings import settings
from .common import Card, button, label
from .editors import AbcHighlighter
from .player import AudioPlayer, fmt_time
from .progress import StageProgress
from .score import ScoreView


def export_mp3(parent, source, done):
    """Convert a FLAC/WAV to MP3 next to it with the bundled ffmpeg.

    用随附的 ffmpeg 把 source 转码为同名 .mp3（放在源文件旁）。`done(ok, message)` 在
    结束（成功或失败）时回调一次，message 为输出文件路径或错误信息。ffmpeg 缺失时直接
    回调失败并返回。
    """
    # 统一转为 Path，便于后续 with_suffix 取输出路径
    source = Path(source)
    # ffmpeg 路径缺失时立即报告失败，避免 QProcess 启动空程序
    if not ffmpeg_path():
        if done:
            done(False, '找不到 ffmpeg')
        return

    out = source.with_suffix('.mp3')
    proc = QProcess(parent)
    proc.setProgram('ffmpeg')
    # 隐藏横幅、仅输出错误、覆盖旧文件；码率取设置项（默认 320k）
    proc.setArguments([
        '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
        '-codec:a', 'libmp3lame', '-b:a', settings.get('mp3_bitrate', '320k'), str(out),
    ])
    # 用字典标记防止 finished 与 errorOccurred 同时触发时重复回调
    reported = {'reported': False}

    def report(ok, message):
        """统一出口：仅回调一次 done，并清理进程对象。"""
        if not reported['reported']:
            reported['reported'] = True
            if done:
                done(ok, message)
            proc.deleteLater()

    def finished(code, _status):
        """进程正常结束：退出码 0 视为成功，否则读取标准错误作为失败原因。"""
        report(
            code == 0,
            str(out) if code == 0 else bytes(proc.readAllStandardError()).decode(errors='replace'),
        )

    def failed(error):
        """进程未能启动时的失败回调。"""
        if error == QProcess.ProcessError.FailedToStart:
            report(False, '无法启动 ffmpeg')

    proc.finished.connect(finished)
    proc.errorOccurred.connect(failed)
    proc.start()


class ResultPanel(QWidget):
    """结果面板：展示生成进度与结果，并触发编辑 / 打开 / 导出等后续动作。"""

    # 请求跳转到乐谱编辑页（携带当前作品数据）
    editRequested = Signal(dict)
    # 请求在作品库继续处理（携带检查点数据）
    libraryRequested = Signal(dict)
    # 顶部提示条消息：第一参数为文本，第二参数为级别（ok / error）
    toast = Signal(str, str)

    def __init__(self, steps, show_score=True, parent=None):
        """构建面板布局；`steps` 为进度步骤名序列，`show_score` 控制是否显示乐谱预览。"""
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        # 顶部：多阶段进度卡片
        self.progress = StageProgress(steps)
        box.addWidget(self.progress)

        # 已保存提示行：左侧提示文本 + 右侧跳转按钮（初始隐藏）
        row = QHBoxLayout()
        self.saved_hint = label('', 'Hint', wrap=True)
        self.saved_btn = button(
            '在作品库继续',
            callback=lambda: self.checkpoint and self.libraryRequested.emit(self.checkpoint),
        )
        self.saved_btn.hide()
        self.checkpoint = None
        row.addWidget(self.saved_hint, 1)
        row.addWidget(self.saved_btn)
        box.addLayout(row)

        # 生成结果卡片：播放器 + 历史列表 + 操作按钮
        card = Card('生成结果')
        self.player = AudioPlayer()
        card.body.addWidget(self.player)

        # 历史列表：多次生成时按时间倒序展示，仅一项时隐藏
        self.history = QListWidget()
        self.history.setMaximumHeight(110)
        self.history.hide()
        self.history.currentItemChanged.connect(self._select)
        card.body.addWidget(self.history)

        # 操作按钮行：打开文件夹 / 导出 MP3 / 编辑乐谱，再单独一行放导出乐谱
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.open_btn = button('📂 打开文件夹', callback=self._open)
        self.mp3_btn = button('💾 导出 MP3', callback=self._mp3)
        self.edit_btn = button(
            '✏️ 编辑乐谱再生成',
            tooltip='把这首歌的乐谱、风格和歌词送到乐谱编辑页',
            callback=lambda: self.current and self.editRequested.emit(self.current),
        )
        self.render_btn = button('导出乐谱', callback=self._render)
        for w in (self.open_btn, self.mp3_btn, self.edit_btn):
            actions.addWidget(w)
        actions.addStretch(1)
        card.body.addLayout(actions)
        card.body.addWidget(self.render_btn)
        box.addWidget(card)

        # 标签页：乐谱预览（可选）/ ABC 文本 / 生成信息
        self.tabs = QTabWidget()
        self.score = ScoreView() if show_score else None
        if self.score:
            self.tabs.addTab(self.score, '🎼 乐谱预览')

        self.abc_text = QPlainTextEdit()
        self.abc_text.setObjectName('Code')
        self.abc_text.setReadOnly(True)
        self.abc_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._highlighter = AbcHighlighter(self.abc_text.document())
        self.tabs.addTab(self.abc_text, 'ABC 文本')

        self.info = QPlainTextEdit()
        self.info.setObjectName('Code')
        self.info.setReadOnly(True)
        self.tabs.addTab(self.info, '生成信息')
        box.addWidget(self.tabs, 1)

        # 初始无结果，操作按钮全部禁用
        self.current = None
        self._set_actions(False)

    def _set_actions(self, value):
        """统一启用 / 禁用四个操作按钮。"""
        for w in (self.open_btn, self.mp3_btn, self.edit_btn, self.render_btn):
            w.setEnabled(value)

    def start(self, title):
        """开始新任务：清空检查点与提示，并启动进度计时。"""
        self.checkpoint = None
        self.saved_hint.clear()
        self.saved_btn.hide()
        self.progress.start(title)

    def show_checkpoint(self, result):
        """展示已保存的检查点：显示阶段名与对应的跳转按钮。"""
        from ..jobs import STAGE_NAMES

        self.checkpoint = result
        self.saved_hint.setText('已保存：' + STAGE_NAMES[result['job']['stage']])
        self.saved_btn.setText('查看作品' if result['job']['stage'] == 'audio' else '在作品库继续')
        self.saved_btn.show()

    def _render(self):
        """把当前作品的潜变量交给"导出乐谱"对话框重新渲染。"""
        if self.current:
            from .render_export import show_render_export

            show_render_export(self, source_dir=self.current['dir'], abc=self.current.get('abc'))

    def on_progress(self, info):
        """转发进度信息给进度卡片。"""
        self.progress.update_info(info)

    def add_result(self, result):
        """向历史列表插入一条结果，并在存在多项时显示列表、选中最新项。"""
        seconds = result.get('seconds', 0)
        meta = result.get('meta', {})
        # 标题优先取 meta.title，否则回退到种子名
        title = meta.get('title') or f'种子 {meta.get("seed")}'
        item = QListWidgetItem(f'🎵 {title} · {fmt_time(seconds)} · {Path(result["dir"]).name}')
        item.setData(Qt.UserRole, result)
        self.history.insertItem(0, item)
        self.history.setVisible(self.history.count() > 1)
        self.history.setCurrentItem(item)

    def _select(self, item, _previous=None):
        """历史列表选中项变化时展示对应结果。"""
        if item is not None:
            self.show_result(item.data(Qt.UserRole))

    def show_result(self, result):
        """展示单个生成结果：更新播放器、乐谱、信息页与按钮状态。"""
        self.current = result
        meta = result.get('meta', {})
        # 标题回退链：meta.title → 首条非空且非小节标记的歌词行 → 目录名
        title = meta.get('title') or next(
            (
                line.strip()
                for line in meta.get('lyrics', '').splitlines()
                if line.strip() and not line.strip().startswith('[')
            ),
            Path(result['dir']).name,
        )
        self.player.set_source(result['audio'], title)

        abc = result.get('abc') or ''
        self.show_score(abc or '')

        timing = result.get('timing') or {}
        lines = [
            f'输出目录: {result["dir"]}',
            f'音频时长: {fmt_time(result.get("seconds"))}',
            f'种子: {meta.get("seed")}    规划模式: {meta.get("cot")}    ODE 步数: {meta.get("ode_steps")}',
            f'CFG: {meta.get("cfg_scale") if meta.get("cfg_scale") is not None else "默认"}',
        ]
        # 存在计时数据时追加各阶段耗时明细
        if timing:
            lines.append(
                f'总耗时: {timing.get("e2e_seconds", 0):.1f}s   乐谱: {timing.get("abc", {}).get("seconds", 0):.1f}s   '
                f'歌曲 tokens: {timing.get("semantic", {}).get("seconds", 0):.1f}s   合成: {timing.get("nar_seconds", 0):.1f}s   '
                f'解码: {timing.get("vae_seconds", 0):.1f}s'
            )

        # 任一阶段被截断时给出提示
        truncated = result.get('truncated') or {}
        if any(truncated.values()):
            lines.append('⚠ 达到生成长度上限，歌曲可能被截断（可在高级采样参数中调高最大 tokens）')

        # 追加风格、歌词与详细计时（JSON 格式）
        lines += [
            '',
            '风格: ' + meta.get('style', ''),
            '',
            '歌词:',
            meta.get('lyrics', ''),
            '',
            '详细计时: ' + json.dumps(timing, ensure_ascii=False, indent=2),
        ]

        self.info.setPlainText('\n'.join(lines))
        self._set_actions(True)
        # 有乐谱时才允许编辑乐谱 / 导出乐谱
        self.edit_btn.setEnabled(bool(abc))
        self.render_btn.setEnabled(bool(abc))

    def show_score(self, abc, debounce=False):
        """同步更新 ABC 文本页与乐谱预览；无乐谱时显示占位提示。"""
        self.abc_text.setPlainText(abc or '（直接生成模式没有乐谱）')
        if self.score:
            self.score.set_abc(abc or '', debounce=debounce)

    def finish(self, ok, message):
        """结束进度卡片：成功打勾填满，失败标记当前步骤。"""
        self.progress.finish(ok, message)

    def _open(self):
        """在资源管理器中打开当前作品的音频文件。"""
        if self.current:
            open_in_explorer(self.current['audio'])

    def _mp3(self):
        """把当前作品音频导出为 MP3，完成后提示并可打开所在目录。"""
        if not self.current:
            return
        self.mp3_btn.setEnabled(False)
        self.mp3_btn.setText('导出中…')

        def done(ok, message):
            """导出结束回调：恢复按钮、发提示，成功时打开输出目录。"""
            self.mp3_btn.setEnabled(True)
            self.mp3_btn.setText('💾 导出 MP3')
            self.toast.emit(
                f'已导出 {Path(message).name}' if ok else f'导出失败: {message[-200:]}',
                'ok' if ok else 'error',
            )
            if ok:
                open_in_explorer(message)

        export_mp3(self, self.current['audio'], done)
