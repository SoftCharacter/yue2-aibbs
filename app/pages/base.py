"""Common page scaffolding.

本模块提供各功能页面的公共基类 BasePage：统一左右两栏布局、顶部页头、
底部操作栏、运行/停止按钮的忙闲状态联动，以及草稿保存、未保存编辑检查等
通用钩子，供创作、翻唱、批量生成等页面继承复用。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..tasks import runner
from ..widgets.common import PageHeader, button, scroll


class BasePage(QWidget):
    """页面基类：提供布局脚手架与任务状态联动。

    子类只需设置 title/subtitle，实现 primary_action 等钩子即可接入主窗口。
    """

    # 页面标题与副标题，由子类覆盖，用于构造顶部 PageHeader
    title = ''
    subtitle = ''

    def __init__(self, main):
        # 记录主窗口引用，供 toast / open_in_* 等跨页跳转调用
        super().__init__()
        self.main = main
        self.setObjectName('Page')

        # 运行/停止按钮列表，忙闲状态变化时统一开关
        self.run_buttons = []
        self.stop_buttons = []

        # 根布局：顶部页头 + 内容区（由子类通过 two_columns 等填充）
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 14)
        root.setSpacing(12)
        self.header = PageHeader(self.title, self.subtitle)
        root.addWidget(self.header)
        self.root = root

        # 监听全局后台任务忙闲状态，联动页面内各按钮
        runner.busyChanged.connect(self.set_busy)

    def two_columns(self, left_widgets, right_widget, action_bar=None, sizes=(560, 640)):
        """构建左右两栏布局：左侧表单、右侧结果区，返回分割器。

        ``left_widgets`` 为左侧控件列表；``right_widget`` 为右侧主控件；
        ``action_bar`` 可选，作为右侧底部操作栏；``sizes`` 控制左右初始宽度。
        """
        # 水平分割器，禁止拖拽到 0 宽度导致一侧完全折叠
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)

        # 左侧：垂直排列传入控件，底部弹性占位顶到上方
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 8, 0)
        left_box.setSpacing(12)
        for w in left_widgets:
            left_box.addWidget(w)
        left_box.addStretch(1)

        # 右侧：垂直布局，可滚动容器占满剩余空间，可选底部操作栏
        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.setSpacing(10)
        right_box.addWidget(scroll(left), 1)
        if action_bar is not None:
            right_box.addWidget(action_bar)

        # 组装分割器并加入根布局，左右弹性权重约为 5:6
        split.addWidget(right)
        split.addWidget(right_widget)
        split.setSizes(list(sizes))
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 6)
        self.root.addWidget(split, 1)
        return split

    def action_bar(self, *buttons):
        """构造底部操作栏；传入 None 的位置用弹性占位填充。"""
        frame = QFrame()
        box = QHBoxLayout(frame)
        box.setContentsMargins(0, 4, 8, 0)
        box.setSpacing(8)
        for b in buttons:
            # None 表示在此处留白，把后续按钮推到右侧
            if b is None:
                box.addStretch(1)
            else:
                box.addWidget(b)
        return frame

    def run_button(self, text, callback, primary=True, tooltip=None):
        """创建运行类按钮，纳入 run_buttons 统一管理忙闲状态。"""
        # primary 时用强调样式，否则默认样式
        btn = button(text, 'Primary' if primary else None, tooltip, callback)
        # 记录空闲时的提示文案，忙时提示会覆盖为"正在运行：xxx"
        btn.setProperty('idleTip', tooltip or '')
        self.run_buttons.append(btn)
        return btn

    def stop_button(self):
        """创建停止按钮，绑定全局任务取消，初始禁用。"""
        btn = button('■ 停止', 'Danger', '取消正在运行的任务', runner.cancel)
        btn.setEnabled(False)
        self.stop_buttons.append(btn)
        return btn

    def set_busy(self, busy):
        """按后台任务忙闲切换各按钮可用状态与提示文案。"""
        # 运行按钮：忙时禁用，提示覆盖为当前任务名；闲时恢复空闲提示
        for b in self.run_buttons:
            b.setEnabled(not busy)
            b.setToolTip(f'正在运行：{runner.title}' if busy else (b.property('idleTip') or ''))
        # 停止按钮：仅忙时可用
        for b in self.stop_buttons:
            b.setEnabled(busy)

    def toast(self, text, kind='info'):
        """在右下角弹出一条轻量提示，转发到主窗口的 toaster。"""
        self.main.toast(text, kind)

    def warn(self, text):
        """弹出一个标题为"提示"的警告对话框。"""
        QMessageBox.warning(self, '提示', text)

    def ensure_idle(self):
        """有后台任务在跑时提示并返回 False，否则返回 True。"""
        if runner.busy:
            self.toast(f'请等待当前任务完成：{runner.title}', 'warn')
            return False
        return True

    def primary_action(self):
        """Triggered by Ctrl+Enter."""
        pass

    def on_show(self):
        """Called when the page becomes visible."""
        pass

    def has_unsaved_edits(self):
        """True while edits are kept for a later save (only pages whose save_draft can return False)."""
        return False

    def discard_pending_edits(self):
        """Drop edits that could not be saved (called after the user confirms abandoning them)."""
        pass

    def save_draft(self):
        """Persist unsaved inputs into settings['drafts']; return False if some edits could not be saved."""
        pass
