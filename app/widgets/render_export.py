"""Shared async score/piano export dialog.

本模块实现共享的乐谱/钢琴异步导出对话框 RenderExportDialog：收集输入来源（结果
文件夹 / ABC / MIDI）、乐谱格式与钢琴声部，通过 TaskRunner 提交渲染任务并实时反馈
进度与结果；模块级 show_render_export 维护单例对话框，运行中的导出不会重复启动。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ..paths import open_in_explorer, output_root
from ..rendering import resolve_sources, run_render
from ..settings import settings
from ..tasks import TaskRunner
from .common import button, label

# 全局共享的渲染任务运行器与当前导出对话框（保证同一时间只有一个导出对话框）
_render_runner = TaskRunner()
_active_dialog = None


class RenderExportDialog(QDialog):
    """乐谱/钢琴导出对话框：收集输入与格式，提交渲染任务并展示进度结果。"""

    def __init__(self, parent=None, *, source_dir=None, abc=None):
        """构建对话框布局；source_dir/abc 为可选的初始输入。"""
        super().__init__(parent)
        self.setWindowTitle('导出乐谱与钢琴预览')
        self.resize(720, 540)

        # 内部状态：当前 ABC 文本、任务 ID、输出目录与关闭标记
        self._abc = abc
        self._task_id = None
        self._output_dir = None
        self._close_after_cancel = False
        self._runner = _render_runner
        self._audio_source = None

        # 主布局
        box = QVBoxLayout(self)
        box.setSpacing(12)
        box.addWidget(label('从已有结果文件夹或 ABC / MIDI 导出，无需重新扒谱。', wrap=True))

        # 输入行：结果文件夹 / ABC / MIDI / 保存位置，各带一个"选择…"按钮
        self.source_input = QLineEdit(str(source_dir or ''))
        self.source_input.setPlaceholderText('选择已有结果文件夹（自动查找 score.abc 与 MIDI）')
        self.abc_input = QLineEdit()
        self.abc_input.setPlaceholderText('可单独指定 ABC 文件')
        self.midi_input = QLineEdit()
        self.midi_input.setPlaceholderText('可单独指定 MIDI 文件')
        self.output_input = QLineEdit(str(output_root(settings) / 'rendered'))
        self._editing_widgets = []
        for row_title, widget, chooser in (
            ('结果文件夹', self.source_input, self.choose_source),
            ('ABC 文件', self.abc_input, self.choose_abc),
            ('MIDI 文件', self.midi_input, self.choose_midi),
            ('保存位置', self.output_input, self.choose_output),
        ):
            row = QHBoxLayout()
            caption = QLabel(row_title)
            caption.setMinimumWidth(75)
            row.addWidget(caption)
            row.addWidget(widget, 1)
            btn = button('选择…', callback=chooser)
            row.addWidget(btn)
            box.addLayout(row)
            self._editing_widgets.extend([widget, btn])

        # 内联提示：仅在初始提供了 ABC 文本时显示
        self.inline_label = label(
            '当前编辑器 ABC 将用于乐谱；WAV 使用选择的 MIDI，编辑后的音符不会自动同步到 MIDI。',
            'Hint',
            wrap=True,
        )
        self.inline_label.setVisible(abc is not None)
        box.addWidget(self.inline_label)

        # 乐谱格式复选框（PDF / PNG / SVG），默认选中 PDF
        self.format_checks = {}
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(label('乐谱格式'))
        for fmt in ('pdf', 'png', 'svg'):
            cb = QCheckBox(fmt.upper())
            cb.setChecked(fmt == 'pdf')
            fmt_row.addWidget(cb)
            self.format_checks[fmt] = cb
            self._editing_widgets.append(cb)
        fmt_row.addStretch(1)
        box.addLayout(fmt_row)

        # 钢琴 WAV 总开关
        self.audio_check = QCheckBox('钢琴 WAV')
        box.addWidget(self.audio_check)
        self._editing_widgets.append(self.audio_check)

        # 分声部复选框，默认选中"混合"
        self.part_checks = {}
        part_row = QHBoxLayout()
        for key, name in (
            ('mix', '混合'),
            ('melody', '全部旋律'),
            ('vocal', '人声旋律'),
            ('instrumental', '器乐旋律'),
            ('chords', '和弦'),
        ):
            cb = QCheckBox(name)
            cb.setChecked(key == 'mix')
            self.part_checks[key] = cb
            part_row.addWidget(cb)
            self._editing_widgets.append(cb)
        box.addLayout(part_row)

        # 说明文字
        box.addWidget(
            label(
                '分声部导出均为钢琴预览；按 MIDI 轨道名称区分人声、器乐与和弦。乐谱保留 ABC 中的谱表。',
                'Hint',
                wrap=True,
            )
        )
        box.addWidget(
            label('每次导出都会在保存位置创建新文件夹。PNG / SVG 按页保存，PDF 包含全部页面。', 'Hint', wrap=True)
        )

        # 状态标签（显示进度/结果信息），支持选中复制
        self.status_label = label('请选择输入及导出格式。', wrap=True)
        self.status_label.setTextFormat(Qt.PlainText)
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(self.status_label)
        box.addStretch(1)

        # 底部按钮行：打开文件夹 / 取消导出 / 开始导出
        btn_row = QHBoxLayout()
        self.open_button = button(
            '打开导出文件夹',
            callback=lambda: self._output_dir and open_in_explorer(self._output_dir),
        )
        self.open_button.setEnabled(False)
        self.cancel_button = button('取消导出', callback=self.cancel_export)
        self.cancel_button.setEnabled(False)
        self.start_button = button('开始导出', callback=self.start_export)
        btn_row.addWidget(self.open_button)
        btn_row.addStretch(1)
        btn_row.addWidget(self.cancel_button)
        btn_row.addWidget(self.start_button)
        box.addLayout(btn_row)

        # 信号连接：运行器忙状态、复选框切换与输入编辑
        self._runner.busyChanged.connect(self._set_busy)
        self.audio_check.toggled.connect(self._update_parts)
        self.source_input.editingFinished.connect(self._refresh_audio_default)
        self.midi_input.editingFinished.connect(self._refresh_audio_default)
        self.abc_input.editingFinished.connect(self._commit_abc_source)
        self._refresh_audio_default()
        self._set_busy(self._runner.busy)

        # 应用退出时取消运行中的渲染任务
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._runner.cancel)

    def _update_parts(self):
        """根据钢琴 WAV 开关与运行状态，启用/禁用分声部复选框。"""
        for cb in self.part_checks.values():
            cb.setEnabled(self.audio_check.isChecked() and not self._runner.busy)

    def _refresh_audio_default(self, force=False):
        """仅在来源确认改变时更新 WAV 默认，不打断输入或其他格式偏好。"""
        # 用（源文件夹, MIDI）组合作为判重键，避免无谓刷新
        key = (self.source_input.text().strip(), self.midi_input.text().strip())
        if not force and key == self._audio_source:
            return
        self._audio_source = key
        # 尝试解析来源，判断能否生成钢琴 WAV（失败则取消勾选）
        try:
            resolve_sources({'source_dir': key[0], 'midi_path': key[1], 'score': [], 'audio': True})
            ok = True
        except (ValueError, OSError):
            ok = False
        self.audio_check.setChecked(ok)

    def _set_busy(self, busy):
        """按运行状态启用/禁用编辑控件与按钮。"""
        for w in self._editing_widgets:
            w.setEnabled(not busy)
        self.start_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy and self._task_id is not None)
        self._update_parts()

    def choose_source(self):
        """选择已有结果文件夹，并清空单独的 ABC / MIDI 输入。"""
        path = QFileDialog.getExistingDirectory(self, '选择已有结果文件夹', self.source_input.text())
        if path:
            self.source_input.setText(path)
            self.abc_input.clear()
            self.midi_input.clear()
            self._abc = None
            self.inline_label.hide()
            self._refresh_audio_default(force=True)

    def _commit_abc_source(self):
        """ABC 输入框有内容时，清空内联 ABC 文本并隐藏提示。"""
        if self.abc_input.text().strip():
            self._abc = None
            self.inline_label.hide()

    def choose_abc(self):
        """选择单独的 ABC 乐谱文件。"""
        path, _ = QFileDialog.getOpenFileName(
            self, '选择 ABC 乐谱', self.abc_input.text(), 'ABC 乐谱 (*.abc);;所有文件 (*)'
        )
        if path:
            self.abc_input.setText(path)
            self._abc = None
            self.inline_label.hide()

    def choose_midi(self):
        """选择单独的 MIDI 文件，并强制刷新 WAV 默认。"""
        path, _ = QFileDialog.getOpenFileName(
            self, '选择 MIDI', self.midi_input.text(), 'MIDI 文件 (*.mid *.midi);;所有文件 (*)'
        )
        if path:
            self.midi_input.setText(path)
            self._refresh_audio_default(force=True)

    def choose_output(self):
        """选择导出保存位置。"""
        path = QFileDialog.getExistingDirectory(self, '选择导出保存位置', self.output_input.text())
        if path:
            self.output_input.setText(path)

    def start_export(self):
        """收集输入并提交渲染任务；来源非法时在状态栏提示。"""
        if self._runner.busy:
            self.status_label.setText('已有导出任务正在运行，请等待完成或取消。')
            return
        # 汇总所有输入与选项
        params = {
            'source_dir': self.source_input.text().strip(),
            'abc': self._abc,
            'abc_path': self.abc_input.text().strip(),
            'midi_path': self.midi_input.text().strip(),
            'output_parent': self.output_input.text().strip(),
            'audio': self.audio_check.isChecked(),
            'score': [k for k, v in self.format_checks.items() if v.isChecked()],
            'parts': [k for k, v in self.part_checks.items() if v.isChecked()],
        }
        try:
            resolve_sources(params)
        except (ValueError, OSError, UnicodeError) as e:
            self.status_label.setText(str(e))
            return
        self._output_dir = None
        self.open_button.setEnabled(False)
        self.status_label.setText('正在启动导出…')
        self._task_id = self._runner.submit(
            '乐谱与钢琴导出',
            run_render,
            params,
            on_done=self._done,
            on_error=self._error,
            on_progress=self._progress,
        )
        self.cancel_button.setEnabled(self._task_id is not None)

    def _progress(self, info):
        """转发进度：更新输出目录与状态文本。"""
        self._output_dir = info.get('output_dir') or self._output_dir
        self.status_label.setText(info.get('message', '正在导出…'))

    def _done(self, result):
        """导出完成：记录目录、恢复按钮并显示结果。"""
        self._task_id = None
        self._output_dir = result['dir']
        self.open_button.setEnabled(True)
        # 有告警时拼到完成提示之后
        warnings = '\n' + '\n'.join(result.get('warnings', [])) if result.get('warnings') else ''
        self.status_label.setText(f'导出完成：{self._output_dir}{warnings}')
        if self._close_after_cancel:
            self.close()

    def _error(self, message, trace):
        """导出失败：清空任务并显示错误，输出目录若有效仍可打开。"""
        self._task_id = None
        detail = f'\n本次目录：{self._output_dir}' if self._output_dir else ''
        self.status_label.setText(message + detail)
        self.open_button.setEnabled(bool(self._output_dir) and Path(self._output_dir).is_dir())
        if self._close_after_cancel:
            self.close()

    def cancel_export(self):
        """取消运行中的导出任务。"""
        if self._task_id is not None:
            self._runner.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText('正在取消导出…')

    def reject(self):
        """重写拒绝：任务运行中先标记关闭，取消完成后自动关闭；否则直接拒绝。"""
        if self._task_id is not None and self._runner.busy:
            self._close_after_cancel = True
            self.cancel_export()
        else:
            super().reject()

    def closeEvent(self, event):
        """关闭窗口时若任务仍在运行，则延迟到取消完成后再关闭。"""
        if self._task_id is not None and self._runner.busy:
            self._close_after_cancel = True
            self.cancel_export()
            event.ignore()
        else:
            self._close_after_cancel = False
            super().closeEvent(event)


def show_render_export(parent, *, source_dir=None, abc=None):
    """显示并返回共享对话框；abc 为当前 ABC 文本，运行中的导出不会重复启动。"""
    if _active_dialog is not None:
        try:
            # 已有对话框且可见或在导出中：复用；空闲时同步输入后再显示
            if _active_dialog.isVisible() or _render_runner.busy:
                if not _render_runner.busy:
                    # 输入是否与现有对话框不同，据此决定是否强制刷新 WAV 默认
                    changed = (
                        _active_dialog._abc != abc
                        or _active_dialog.source_input.text().strip() != str(source_dir or '').strip()
                        or bool(_active_dialog.midi_input.text().strip())
                    )
                    _active_dialog._abc = abc
                    _active_dialog.source_input.setText(str(source_dir or ''))
                    _active_dialog.abc_input.clear()
                    _active_dialog.midi_input.clear()
                    _active_dialog.inline_label.setVisible(abc is not None)
                    _active_dialog._refresh_audio_default(force=changed)
                _active_dialog.show()
                _active_dialog.raise_()
                _active_dialog.activateWindow()
                return _active_dialog
            _active_dialog.deleteLater()
        except RuntimeError:
            # 底层 Qt 对话框已被销毁时重建
            pass
    _active_dialog = RenderExportDialog(parent, source_dir=source_dir, abc=abc)
    _active_dialog.show()
    return _active_dialog
