"""Entry point: python -m app.main

本模块是应用的入口：初始化运行环境、创建 QApplication、加载主题与配色、
绘制应用图标、安装滚轮守卫、重定向标准输出到界面日志，最后启动主窗口进入事件循环。
"""
from __future__ import annotations

import sys


def main():
    """构建并启动 GUI 应用，直到主窗口关闭后退出。"""
    # 先配置运行环境（模型路径、依赖、共享库等），确保后续导入可用
    from .paths import setup_environment

    setup_environment()

    # Qt 各模块按需导入，避免启动时一次性加载全部组件
    from PySide6.QtCore import Qt
    from PySide6.QtGui import (
        QColor,
        QFont,
        QIcon,
        QLinearGradient,
        QPainter,
        QPalette,
        QPixmap,
    )
    from PySide6.QtWidgets import QApplication

    # QtWebEngine 属于可选依赖（部分页面可能用到），缺失时静默跳过，不阻塞主流程
    try:
        from PySide6 import QtWebEngineWidgets
    except Exception:
        pass

    # 共享 OpenGL 上下文，供多个窗口/画布复用 GPU 资源
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

    app = QApplication(sys.argv)
    app.setApplicationName('YuE2 Studio')
    app.setStyle('Fusion')

    # 加载主题模块，取配色表 C 与全局样式表 QSS
    from . import theme

    C = theme.C
    QSS = theme.QSS

    # 用主题配色覆盖 QPalette 各角色，使原生控件与应用整体风格一致
    palette = QPalette()
    for role, color in (
        (QPalette.Window, C['bg']),
        (QPalette.WindowText, C['text']),
        (QPalette.Base, C['input']),
        (QPalette.AlternateBase, C['surface']),
        (QPalette.Text, C['text']),
        (QPalette.Button, C['card_hi']),
        (QPalette.ButtonText, C['text']),
        (QPalette.Highlight, C['accent']),
        (QPalette.HighlightedText, '#ffffff'),
        (QPalette.ToolTipBase, C['card_hi']),
        (QPalette.ToolTipText, C['text']),
        (QPalette.PlaceholderText, C['faint']),
    ):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)

    app.setFont(QFont('Microsoft YaHei UI', 10))
    app.setStyleSheet(QSS)

    # 用 QPainter 现绘一个渐变圆角图标（音符 ♪）作为窗口图标，无需外置图片资源
    icon = QPixmap(128, 128)
    icon.fill(Qt.transparent)
    p = QPainter(icon)
    p.setRenderHint(QPainter.Antialiasing)

    g = QLinearGradient(0, 0, 128, 128)
    g.setColorAt(0, QColor(C['accent']))
    g.setColorAt(1, QColor(C['accent2']))
    p.setBrush(g)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 120, 120, 30, 30)

    p.setPen(QColor('white'))
    p.setFont(QFont('Segoe UI Symbol', 64, QFont.Bold))
    p.drawText(icon.rect(), Qt.AlignCenter, '♪')
    p.end()
    app.setWindowIcon(QIcon(icon))

    # 安装滚轮守卫：拦截 Qt 控件的滚轮事件，避免在非滚动区域误触发数值微调
    from .widgets.wheel_guard import WheelGuard

    app._wheel_guard = WheelGuard(app)
    app.installEventFilter(app._wheel_guard)

    # 把进程的 stdout/stderr 重定向到界面日志，让子线程/子进程的打印也可见
    from . import tasks

    StreamTee = tasks.StreamTee
    log_bridge = tasks.log_bridge
    sys.stdout = StreamTee(sys.stdout, log_bridge)
    sys.stderr = StreamTee(sys.stderr, log_bridge)

    # 创建并显示主窗口，进入事件循环；窗口关闭后以返回值退出
    from .mainwindow import MainWindow

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
