"""Self-similarity heatmap for MERT frame features.

本模块实现自相似图控件 HeatmapView 与配套的 colormap 颜色映射：把 MERT 帧特征
之间的自相似矩阵渲染成热力图，绘制按时间自适应分布的时间刻度网格，并在鼠标
悬停时显示对应两个时刻及它们的相似度。
"""
from __future__ import annotations

import numpy as np

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from ..theme import C

# 颜色映射锚点：每行 [归一化值, R, G, B]，整体从深蓝渐变到亮黄
_ANCHORS = np.array(
    [[0, 13, 8, 30], [0.25, 72, 30, 120], [0.5, 184, 55, 121], [0.75, 251, 136, 97], [1, 252, 253, 191]],
    dtype=np.float32,
)


def colormap(values):
    """把 0~1 的数值映射为 RGB 颜色，返回 [..., 3] 的 uint8 数组。

    在 R/G/B 三个通道上分别用锚点做线性插值，实现平滑的深色到亮色渐变。
    """
    anchors = _ANCHORS[:, 0]
    rgb = np.stack([np.interp(values, anchors, _ANCHORS[:, i]) for i in (1, 2, 3)], axis=-1)
    return rgb.astype(np.uint8)


class HeatmapView(QWidget):
    """自相似热力图控件：绘制矩阵与时间刻度网格，悬停显示相似度。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 渲染用图像、自相似矩阵以及每个矩阵格对应的秒数
        self.image = None
        self.matrix = None
        self.cell_seconds = 1
        self.setMouseTracking(True)
        self.setMinimumSize(260, 260)

    def set_matrix(self, matrix, cell_seconds=1):
        """设置要显示的自相似矩阵并触发重绘；matrix 为 None 时清空图像。

        先按 2%~99.5% 分位数归一化到 0~1，再经 colormap 映射成 RGB 图像。
        """
        self.matrix = matrix
        self.cell_seconds = cell_seconds
        if matrix is None:
            self.image = None
        else:
            lo = np.percentile(matrix, 2)
            hi = np.percentile(matrix, 99.5)
            norm = np.clip((matrix - lo) / max(hi - lo, 1e-06), 0, 1)
            rgb = np.ascontiguousarray(colormap(norm))
            h, w = rgb.shape[:2]
            self._buffer = rgb
            self.image = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        self.update()

    def _plot_rect(self):
        """计算热力图的方形绘制区域（左上留白给坐标轴刻度）。"""
        left, top, margin = 48, 30, 10
        size = max(10, min(self.width() - left - margin, self.height() - top - margin))
        return QRectF(left, margin, size, size)

    def paintEvent(self, event):
        """绘制热力图：图像、边框与按自适应步长分布的时间刻度网格。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 尚无结果时居中显示占位提示
        if self.image is None:
            painter.setPen(QColor(C['faint']))
            painter.drawText(self.rect(), Qt.AlignCenter, '提取特征后在此显示歌曲结构自相似图')
            return

        rect = self._plot_rect()
        painter.drawImage(rect, self.image)
        painter.setPen(QPen(QColor(C['border_hi']), 1))
        painter.drawRect(rect)

        # 选择使刻度总数不超过 8 的时间步长（候选 5/10/15/30/60/120 秒，超长歌回退 300）
        n = self.matrix.shape[0]
        total = n * self.cell_seconds
        step = next((t for t in (5, 10, 15, 30, 60, 120) if total / t <= 8), 300)

        painter.setPen(QColor(C['muted']))
        t = 0
        while t <= total + 1e-06:
            # 把时间比例换算成绘图坐标，横向/纵向各画一条刻度线与时间标签
            f = t / total if total else 0
            x = rect.left() + f * rect.width()
            y = rect.top() + f * rect.height()
            label = f'{int(t // 60)}:{int(t % 60):02d}'
            painter.drawLine(QPointF(x, rect.bottom()), QPointF(x, rect.bottom() + 4))
            painter.drawText(QRectF(x - 30, rect.bottom() + 5, 60, 18), Qt.AlignHCenter, label)
            painter.drawLine(QPointF(rect.left() - 4, y), QPointF(rect.left(), y))
            painter.drawText(QRectF(0, y - 9, rect.left() - 6, 18), Qt.AlignRight | Qt.AlignVCenter, label)
            t += step

    def mouseMoveEvent(self, event):
        """悬停时显示对应行列的时刻与相似度 ToolTip。"""
        if self.matrix is None:
            return
        rect = self._plot_rect()
        pos = event.position()
        # 鼠标移出绘图区时隐藏提示
        if not rect.contains(pos):
            QToolTip.hideText()
            return

        n = self.matrix.shape[0]
        # 把鼠标坐标换算成矩阵行列索引，并夹到 [0, n-1] 范围
        col = min(n - 1, int((pos.x() - rect.left()) / rect.width() * n))
        row = min(n - 1, int((pos.y() - rect.top()) / rect.height() * n))
        # 帧索引转时间文本的格式化函数
        fmt = lambda i: f'{int(i * self.cell_seconds // 60)}:{int(i * self.cell_seconds % 60):02d}'
        QToolTip.showText(
            event.globalPosition().toPoint(),
            f'{fmt(row)} ↔ {fmt(col)}\n相似度 {self.matrix[row, col]:.3f}',
            self,
        )
