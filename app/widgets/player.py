"""Audio player with a clickable waveform.

本模块实现音频播放器 AudioPlayer 与可点击的波形显示控件 WaveformView。AudioPlayer 封装
QMediaPlayer 与 QAudioOutput，提供播放/暂停、进度跳转、音量调节，并在后台线程中用
audio_utils.waveform_peaks 提取波形峰值后异步回填到波形控件；WaveformView 负责把峰值数组
绘制成左右渐变的柱状波形，悬停时显示竖向参考线，点击时按比例发出 seekRequested 信号。
模块级 _players 列表用于保证同一时间只有一个播放器在播放（切换播放前暂停其它实例）。
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..theme import C
from .common import button, label

# 全部播放器实例的注册表，用于 toggle 时互斥：播放前暂停其它正在播放的实例
_players = []


def fmt_time(seconds):
    """把秒数格式化为 mm:ss 文本，负值或空值归零。"""
    seconds = max(0, int(seconds or 0))
    return f'{seconds // 60}:{seconds % 60:02d}'


class _PeakSignal(QObject):
    """跨线程信号载体：后台线程提取完波形后，经 ready 信号回主线程。"""

    # 参数依次为：音频路径、峰值数组、时长（秒）
    ready = Signal(str, object, float)


class WaveformView(QWidget):
    """可点击波形控件：绘制峰值柱状图，支持悬停参考线与点击跳转。"""

    # 点击后按 0~1 的比例发出跳转请求
    seekRequested = Signal(float)

    def __init__(self, parent=None):
        """初始化波形数据与交互状态，开启鼠标追踪并设为手型光标。"""
        super().__init__(parent)
        self.peaks = None
        self.position = 0
        self.hover = None
        self.setMouseTracking(True)
        self.setMinimumHeight(64)
        self.setCursor(Qt.PointingHandCursor)

    def set_peaks(self, peaks):
        """设置峰值数组并触发重绘。"""
        self.peaks = peaks
        self.update()

    def set_position(self, ratio):
        """更新播放进度比例（夹在 0~1 之间）并触发重绘。"""
        self.position = min(1, max(0, ratio))
        self.update()

    def mousePressEvent(self, event):
        """按下鼠标时按横向位置比例发出跳转信号（宽度为 0 时忽略）。"""
        if self.width() > 0:
            self.seekRequested.emit(event.position().x() / self.width())

    def mouseMoveEvent(self, event):
        """记录悬停横坐标，用于绘制竖向参考线。"""
        self.hover = event.position().x()
        self.update()

    def leaveEvent(self, event):
        """鼠标移出时清除悬停参考线。"""
        self.hover = None
        self.update()

    def paintEvent(self, event):
        """绘制波形：无峰值时画虚线中线，有峰值时画渐变色柱状条。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()
        mid = h / 2

        # 峰值缺失或为空时，仅在垂直居中处画一条虚线占位
        if self.peaks is None or not len(self.peaks):
            painter.setPen(QPen(QColor(C['border_hi']), 1, Qt.DashLine))
            painter.drawLine(QPointF(0, mid), QPointF(w, mid))
            return

        # 柱宽与间距，按宽度估算可容纳的柱数
        bar_w, gap = (3, 2)
        n_bars = max(1, w // (bar_w + gap))
        # 把峰值数组按柱数分段，每段取最大值作为该柱的高度
        indices = np.linspace(0, len(self.peaks), n_bars + 1).astype(int)
        values = [float(self.peaks[i:max(i + 1, j)].max()) for i, j in zip(indices[:-1], indices[1:])]
        playhead = self.position * w

        # 已播放区域用 accent→accent2 横向渐变，未播放区域用静态底色
        gradient = QLinearGradient(0, 0, w, 0)
        gradient.setColorAt(0, QColor(C['accent']))
        gradient.setColorAt(1, QColor(C['accent2']))
        played_brush = QBrush(gradient)
        idle_brush = QBrush(QColor(C['wave_idle']))

        painter.setPen(Qt.NoPen)
        for idx, val in enumerate(values):
            x = idx * (bar_w + gap)
            bar_h = max(2, val * (h - 6))
            painter.setBrush(played_brush if x < playhead else idle_brush)
            painter.drawRoundedRect(QRectF(x, mid - bar_h / 2, bar_w, bar_h), 1.5, 1.5)

        # 悬停时画一条半透明竖向参考线
        if self.hover is not None:
            hover_color = QColor(C['wave_hover'])
            hover_color.setAlpha(90)
            painter.setPen(QPen(hover_color, 1))
            painter.drawLine(QPointF(self.hover, 0), QPointF(self.hover, h))


class AudioPlayer(QFrame):
    """音频播放器：组合播放按钮、波形、时间与音量控件，并管理媒体播放生命周期。"""

    def __init__(self, title='', compact=False, parent=None):
        """构建播放器布局；compact 为真时使用更紧凑的波形高度并隐藏标题行。"""
        super().__init__(parent)
        self.path = ''
        self.duration = 0

        # 底层媒体播放器与音频输出，默认音量 0.85
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.output.setVolume(0.85)
        self.player.setAudioOutput(self.output)

        # 波形提取的跨线程信号，结果就绪后由 _peaks_ready 回填
        self._signal = _PeakSignal()
        self._signal.ready.connect(self._peaks_ready)
        _players.append(self)

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        # 标题与元信息（时长）行，紧凑模式下省略
        head = QHBoxLayout()
        self.title = label(title or '未加载音频', 'CardTitle')
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.title.setMinimumWidth(10)
        self.meta = label('', 'Hint')
        head.addWidget(self.title, 1)
        head.addWidget(self.meta)
        if not compact:
            box.addLayout(head)

        # 播放按钮 + 波形 + 时间/音量 的组合行
        row = QHBoxLayout()
        row.setSpacing(12)
        self.play_btn = button('▶', 'RoundPlay', '播放 / 暂停', self.toggle)
        self.wave = WaveformView()
        self.wave.setMinimumHeight(44 if compact else 64)
        self.wave.seekRequested.connect(self.seek_ratio)
        self.time = QLabel('0:00 / 0:00')
        self.time.setObjectName('Hint')
        self.time.setMinimumWidth(80)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(85)
        self.volume.setFixedWidth(70)
        self.volume.setToolTip('音量')
        self.volume.valueChanged.connect(lambda v: self.output.setVolume(v / 100))
        row.addWidget(self.play_btn)
        row.addWidget(self.wave, 1)

        # 右侧时间标签与音量滑块的纵向排列
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(self.time)
        col.addWidget(self.volume)
        row.addLayout(col)
        box.addLayout(row)

        # 连接媒体状态信号到对应的界面更新方法
        self.player.positionChanged.connect(self._position)
        self.player.durationChanged.connect(self._duration)
        self.player.playbackStateChanged.connect(self._state)
        self.player.errorOccurred.connect(lambda _: self.meta.setText('无法播放: ' + self.player.errorString()))
        self.set_enabled(False)

    def set_enabled(self, value):
        """统一启用/禁用播放按钮与波形控件。"""
        self.play_btn.setEnabled(value)
        self.wave.setEnabled(value)

    def set_source(self, path, title=None):
        """切换播放源：释放旧源、设置新源，并启动后台线程提取波形。"""
        self.release()
        self.duration = 0
        self.meta.clear()
        self._position(0)
        self.path = str(path) if path else ''

        # 路径无效时进入未加载状态
        if not self.path or not Path(self.path).exists():
            self.title.setText(title or '未加载音频')
            self.meta.setText('')
            self.wave.set_peaks(None)
            self.set_enabled(False)
            return

        self.title.setText(title or Path(self.path).name)
        self.player.setSource(QUrl.fromLocalFile(self.path))
        self.set_enabled(True)
        self.wave.set_peaks(None)

        # 后台线程提取波形，避免阻塞界面；完成后经信号回填
        source = self.path

        def work():
            try:
                from ..audio_utils import waveform_peaks

                peaks, duration = waveform_peaks(source)
            except Exception:
                peaks, duration = (None, 0)
            self._signal.ready.emit(source, peaks, duration)

        threading.Thread(target=work, daemon=True).start()

    def _peaks_ready(self, path, peaks, duration):
        """波形提取完成回调：仅在结果仍对应当前源时回填波形与时长。"""
        if path == self.path:
            self.wave.set_peaks(peaks)
            if duration:
                self.duration = duration
                self.meta.setText(fmt_time(duration))
                self._position(self.player.position())

    def release(self):
        """停止播放并清空媒体源，用于切换前的清理。"""
        self.player.stop()
        self.player.setSource(QUrl())

    def toggle(self):
        """切换播放/暂停；播放前暂停其它正在播放的实例以保证互斥。"""
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            return
        for other in _players:
            if other is not self and other.player.playbackState() == QMediaPlayer.PlayingState:
                other.player.pause()
        self.player.play()

    def seek_ratio(self, ratio):
        """按比例跳转到对应位置；若当前未播放则先启动播放。"""
        dur = self.player.duration() or self.duration * 1000
        if dur:
            self.player.setPosition(int(dur * min(1, max(0, ratio))))
            if self.player.playbackState() != QMediaPlayer.PlayingState:
                self.toggle()

    def _duration(self, ms):
        """媒体时长变化回调：更新秒数并刷新时间显示。"""
        if ms:
            self.duration = ms / 1000
        self._position(self.player.position())

    def _position(self, ms):
        """播放位置变化回调：更新时间文本与波形进度比例。"""
        dur = self.duration or 0
        self.time.setText(f'{fmt_time(ms / 1000)} / {fmt_time(dur)}')
        self.wave.set_position((ms / 1000) / dur if dur else 0)

    def _state(self, state):
        """播放状态变化回调：切换播放/暂停按钮图标。"""
        self.play_btn.setText('❚❚' if state == QMediaPlayer.PlayingState else '▶')
