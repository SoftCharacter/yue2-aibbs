"""Multi-stage progress card.

本模块实现多阶段进度卡片 StageProgress：用"步骤标签行 + 进度条 + 详情文本"的组合
展示任务执行进度，支持设置步骤列表、更新当前步骤与进度值、显示累计耗时，并在
完成或失败时切换到对应的视觉状态（勾选/圆点/叉号）。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout

from ..theme import C
from .common import label


class StageProgress(QFrame):
    """多阶段进度卡片：顶部步骤标签行、中部进度条、底部详情与标题。"""

    def __init__(self, steps=(), parent=None):
        """初始化卡片布局并建立内部状态；`steps` 为步骤名称序列。"""
        super().__init__(parent)
        # 用对象名标记为卡片，方便全局样式表定位
        self.setObjectName('Card')
        # 主布局：承载步骤行、标题/耗时行、进度条与详情
        box = QVBoxLayout(self)
        box.setContentsMargins(16, 12, 16, 14)
        box.setSpacing(8)
        # 步骤标签行（水平），步骤之间由分隔线隔开
        self.steps_row = QHBoxLayout()
        self.steps_row.setSpacing(4)
        box.addLayout(self.steps_row)

        # 标题与耗时并排显示，标题靠左、耗时靠右
        head = QHBoxLayout()
        self.text = label('就绪', 'CardTitle')
        self.elapsed = label('', 'Hint')
        head.addWidget(self.text, 1)
        head.addWidget(self.elapsed)
        box.addLayout(head)

        # 进度条：固定高度，取值范围 0~1
        self.bar = QProgressBar()
        self.bar.setFixedHeight(8)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        box.addWidget(self.bar)

        # 详情文本（次要提示）
        self.detail = label('', 'Hint')
        box.addWidget(self.detail)

        # 内部状态：标签列表、步骤列表、当前步骤索引、开始时间
        self._labels = []
        self._steps = []
        self._current = -1
        self._start = None
        # 500ms 定时器驱动耗时刷新
        self._timer = QTimer(self, interval=500, timeout=self._tick)
        self.set_steps(steps)

    def set_steps(self, steps):
        """重建步骤标签行；`steps` 为空时仅清空显示。"""
        # 统一转成列表并缓存，若与当前一致则跳过重建
        steps = list(steps)
        if steps == self._steps:
            return
        self._steps = steps
        # 逐个移除旧的布局项与控件，避免残留
        while self.steps_row.count():
            item = self.steps_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._labels = []
        for i, name in enumerate(steps):
            # 步骤之间插入一条 1px 分隔线
            if i:
                sep = QFrame()
                sep.setFixedHeight(1)
                sep.setStyleSheet(f'background:{C["border"]};')
                self.steps_row.addWidget(sep, 1)
            # 每个步骤一个标签，初始显示为未完成的圆点
            lbl = QLabel(f'○ {name}')
            lbl.setStyleSheet(f'color:{C["faint"]}; font-size:12px;')
            self.steps_row.addWidget(lbl)
            self._labels.append(lbl)
        self._paint(self._current)

    def _paint(self, current, failed=False):
        """按当前步骤重绘每个标签：已完成打勾、进行中高亮、未完成圆点。"""
        for i, lbl in enumerate(self._labels):
            name = self._steps[i]
            # 已完成（或在失败场景下越过末尾）的步骤显示勾号
            if i < current or current >= len(self._labels):
                lbl.setText(f'✓ {name}')
                lbl.setStyleSheet(f'color:{C["green"]}; font-size:12px;')
            # 当前步骤：成功显示实心圆点，失败显示叉号，字体加粗
            elif i == current:
                mark = '✕' if failed else '●'
                color = C['red'] if failed else C['accent']
                lbl.setText(f'{mark} {name}')
                lbl.setStyleSheet(f'color:{color}; font-size:12px; font-weight:700;')
            # 尚未开始的步骤显示空心圆点
            else:
                lbl.setText(f'○ {name}')
                lbl.setStyleSheet(f'color:{C["faint"]}; font-size:12px;')

    def start(self, title='准备中…', steps=None):
        """开始计时：重置当前步骤为 0、记录起始时间并启动刷新定时器。"""
        if steps is not None:
            self.set_steps(steps)
        self._current = 0
        self._paint(0)
        self.text.setText(title)
        self.detail.setText('')
        self.bar.setRange(0, 0)
        self._start = time.monotonic()
        self._timer.start()
        self._tick()

    def update_info(self, info):
        """依据字典更新卡片各字段，键为 steps/step/text/detail/maximum/value。"""
        if info.get('steps'):
            self.set_steps(info['steps'])
        # 更新当前步骤（非负时才生效）
        step = info.get('step')
        if step is not None and step >= 0:
            self._current = step
            self._paint(step)
        if info.get('text'):
            self.text.setText(info['text'])
        self.detail.setText(info.get('detail') or '')
        # 有最大值时按比例更新进度条，否则归零
        maximum = info.get('maximum')
        value = info.get('value')
        if maximum:
            self.bar.setRange(0, int(maximum))
            self.bar.setValue(min(int(value or 0), int(maximum)))
        else:
            self.bar.setRange(0, 0)

    def finish(self, ok=True, message=''):
        """结束进度：成功时全部打勾并填满进度条，失败时标记当前步骤。"""
        self._timer.stop()
        self._tick()
        if ok:
            self._current = len(self._labels)
            self._paint(self._current)
            self.bar.setRange(0, 1)
            self.bar.setValue(1)
        else:
            self._paint(self._current, failed=True)
            self.bar.setRange(0, 1)
            self.bar.setValue(0)
        # 标题显示自定义消息或默认的"完成/失败"
        self.text.setText(message or ('完成' if ok else '失败'))
        if not ok:
            self.detail.setText('')

    def reset(self, text='就绪'):
        """重置为初始就绪状态，清空进度与耗时。"""
        self._timer.stop()
        self._current = -1
        self._paint(-1)
        self.text.setText(text)
        self.detail.setText('')
        self.elapsed.setText('')
        self.bar.setRange(0, 1)
        self.bar.setValue(0)

    def _tick(self):
        """定时刷新右侧耗时标签，格式为 mm:ss。"""
        if self._start is not None:
            secs = time.monotonic() - self._start
            self.elapsed.setText(f'⏱ {int(secs // 60)}:{int(secs % 60):02d}')
