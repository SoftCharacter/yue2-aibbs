"""Keep wheel scrolling from changing parameter values anywhere in the UI.

本模块提供一个全局事件过滤器 WheelGuard：拦截落在数值类控件（旋转框、下拉框、
滑杆、旋钮）上的滚轮事件并忽略，避免用户滚动页面时误改参数值。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractSpinBox, QComboBox, QDial, QSlider


class WheelGuard(QObject):
    """全局滚轮守卫：屏蔽落在数值控件上的滚轮事件。"""

    def eventFilter(self, obj, event):
        """过滤滚轮事件：目标是数值控件时忽略该事件并返回 True 表示已处理。

        obj 为事件目标对象，event 为待过滤的事件。返回 True 会阻止事件继续传播，
        从而让数值控件不响应滚轮；其余情况返回 False 交给默认处理。
        """
        if event.type() == QEvent.Wheel and isinstance(obj, (QAbstractSpinBox, QComboBox, QSlider, QDial)):
            event.ignore()
            return True
        return False
