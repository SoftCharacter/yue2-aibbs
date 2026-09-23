"""作品库左侧列表：每个作品绘制成一张卡片（类型标签、标题、时长、参数行、任务状态）。

本模块实现作品库的卡片式列表控件：通过自定义 QStyledItemDelegate 把每个作品条目
绘制成圆角卡片，包含类型徽标、标题、时长、日期/参数详情以及任务状态圆点；并用
SongList 列表控件承载这些卡片。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QStyle, QStyledItemDelegate

from ..theme import C

# 卡片显示数据使用 Qt.UserRole + 1 存储，避免与 Qt.UserRole（作品原始数据）冲突
CARD_ROLE = Qt.UserRole + 1

# 不同创作模式对应的类型徽标颜色
MODE_TONES = {'create': 'accent', 'cover': 'cyan', 'edit': 'amber', 'decode': 'green'}

# 任务状态对应的状态圆点颜色；None 表示不显示圆点（如已完成）
STATUS_TONES = {
    '运行中': 'accent',
    '已中断，可继续': 'amber',
    '待继续': 'amber',
    '失败，可继续': 'red',
    '已取消': 'faint',
    '草稿': 'faint',
    '已完成': None,
}

# 卡片外边距、内边距与行间距（像素）
_MARGIN_X, _MARGIN_Y = (6, 4)
_PAD_X, _PAD_Y = (14, 10)
_LINE_GAP = 4


def card_data(*, title, mode_key, mode_name, detail, date, duration='', status='', status_tone=None):
    """构造一份卡片显示数据字典，字段统一、便于委托读取。"""
    return {
        'title': title,
        'mode_key': mode_key,
        'mode_name': mode_name,
        'detail': detail,
        'date': date,
        'duration': duration,
        'status': status,
        'status_tone': status_tone,
    }


def _mix(color, background, alpha):
    """把 color 按 alpha 叠在 background 上，得到不透明色（深浅主题都清晰）。"""
    c = QColor(color)
    b = QColor(background)
    return QColor(
        round(c.red() * alpha + b.red() * (1 - alpha)),
        round(c.green() * alpha + b.green() * (1 - alpha)),
        round(c.blue() * alpha + b.blue() * (1 - alpha)),
    )


class SongCardDelegate(QStyledItemDelegate):
    """作品卡片委托：自定义绘制圆角卡片及其内部各文本区域。"""

    def __init__(self, parent=None):
        """初始化三种字体：标题（14px 半粗）、正文（12px）、徽标（11px 半粗）。"""
        super().__init__(parent)
        # 以父控件字体为基准，缺失时使用默认字体
        base = QFont(parent.font()) if parent else QFont()
        self.title_font = QFont(base)
        self.title_font.setPixelSize(14)
        self.title_font.setWeight(QFont.DemiBold)
        self.small_font = QFont(base)
        self.small_font.setPixelSize(12)
        self.badge_font = QFont(base)
        self.badge_font.setPixelSize(11)
        self.badge_font.setWeight(QFont.DemiBold)

    def _line_heights(self, data):
        """返回三行内容的高度：标题/徽标行高、正文字行高、是否有状态行。"""
        line1 = max(
            QFontMetrics(self.title_font).height(),
            QFontMetrics(self.badge_font).height() + 6,
        )
        line2 = QFontMetrics(self.small_font).height()
        return line1, line2, bool(data and data.get('status'))

    def sizeHint(self, option, index):
        """依据卡片内容行高计算条目尺寸，宽度取可用宽度。"""
        data = index.data(CARD_ROLE) or {}
        line1, line2, has_status = self._line_heights(data)
        # 上下内边距 + 各行高 + 行间距，有状态行时再多一行
        h = _PAD_Y * 2 + line1 + _LINE_GAP + line2 + (_LINE_GAP + line2 if has_status else 0)
        return QSize(option.rect.width(), h + _MARGIN_Y * 2)

    def paint(self, painter, option, index):
        """绘制单张卡片：背景、边框、选中指示条、徽标、标题、时长、日期、详情与状态。"""
        data = index.data(CARD_ROLE)
        # 无数据时回退到默认绘制
        if not data:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        # 选中与悬停状态
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        # 卡片主体矩形（扣除外边距）
        card_rect = QRectF(option.rect).adjusted(_MARGIN_X, _MARGIN_Y, -_MARGIN_X, -_MARGIN_Y)

        # 圆角路径：向内微调半像素避免描边溢出
        card_path = QPainterPath()
        card_path.addRoundedRect(card_rect.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)

        # 背景填充色：选中 > 悬停 > 普通
        if selected:
            fill = C['accent_soft']
        elif hovered:
            fill = C['card_hi']
        else:
            fill = C['card']

        painter.fillPath(card_path, QColor(fill))

        # 描边颜色随状态变化
        if selected:
            border = C['accent']
        elif hovered:
            border = C['border_hi']
        else:
            border = C['border']
        painter.setPen(QPen(QColor(border), 1))
        painter.drawPath(card_path)

        # 选中时在卡片左侧绘制一条强调竖线
        if selected:
            accent_bar = QPainterPath()
            accent_bar.addRoundedRect(
                QRectF(card_rect.left() + 1, card_rect.top() + 8, 3, card_rect.height() - 16),
                1.5,
                1.5,
            )
            painter.fillPath(accent_bar, QColor(C['accent']))

        # 各行行高与状态行标记
        line1, line2, has_status = self._line_heights(data)
        x = card_rect.left() + _PAD_X
        right_x = card_rect.right() - _PAD_X
        y = card_rect.top() + _PAD_Y

        # 类型徽标：圆角胶囊，颜色按创作模式映射
        badge_fm = QFontMetrics(self.badge_font)
        badge_color = QColor(C[MODE_TONES.get(data['mode_key'], 'muted')])
        badge_w = badge_fm.horizontalAdvance(data['mode_name']) + 14
        badge_h = badge_fm.height() + 4
        badge_rect = QRectF(x, y + (line1 - badge_h) / 2, badge_w, badge_h)
        badge_path = QPainterPath()
        badge_path.addRoundedRect(badge_rect, badge_h / 2, badge_h / 2)
        painter.fillPath(badge_path, _mix(badge_color, fill, 0.16))
        painter.setFont(self.badge_font)
        painter.setPen(badge_color)
        painter.drawText(badge_rect, Qt.AlignCenter, data['mode_name'])

        # 标题起点位于徽标右侧
        title_x = badge_rect.right() + 8
        duration_w = 0
        # 时长靠右显示（存在时才绘制）
        if data.get('duration'):
            painter.setFont(self.small_font)
            duration_w = QFontMetrics(self.small_font).horizontalAdvance(data['duration'])
            painter.setPen(QColor(C['muted']))
            painter.drawText(
                QRectF(right_x - duration_w, y, duration_w, line1),
                Qt.AlignRight | Qt.AlignVCenter,
                data['duration'],
            )

        # 标题：按剩余宽度截断，选中时用强调色
        painter.setFont(self.title_font)
        painter.setPen(QColor(C['accent_text'] if selected else C['text']))
        title_w = max(0, right_x - title_x - (duration_w + 10 if duration_w else 0))
        title_text = QFontMetrics(self.title_font).elidedText(data['title'], Qt.ElideRight, int(title_w))
        painter.drawText(
            QRectF(title_x, y, title_w, line1),
            Qt.AlignLeft | Qt.AlignVCenter,
            title_text,
        )

        # 移到第二行（日期 + 详情）
        y += line1 + _LINE_GAP
        small_fm = QFontMetrics(self.small_font)
        painter.setFont(self.small_font)

        # 日期靠右显示（存在时才绘制）
        date_w = small_fm.horizontalAdvance(data['date']) if data.get('date') else 0
        if date_w:
            painter.setPen(QColor(C['faint']))
            painter.drawText(
                QRectF(right_x - date_w, y, date_w, line2),
                Qt.AlignRight | Qt.AlignVCenter,
                data['date'],
            )

        # 详情靠左显示，按剩余宽度截断
        detail_w = max(0, right_x - x - (date_w + 12 if date_w else 0))
        painter.setPen(QColor(C['muted']))
        painter.drawText(
            QRectF(x, y, detail_w, line2),
            Qt.AlignLeft | Qt.AlignVCenter,
            small_fm.elidedText(data['detail'], Qt.ElideRight, int(detail_w)),
        )

        # 第三行：状态圆点 + 状态文本
        if has_status:
            y += line2 + _LINE_GAP
            status_color = QColor(C[data.get('status_tone') or 'muted'])
            dot = 7
            painter.setPen(Qt.NoPen)
            painter.setBrush(status_color)
            painter.drawEllipse(QRectF(x, y + (line2 - dot) / 2, dot, dot))
            painter.setPen(status_color)
            status_x = x + dot + 7
            painter.drawText(
                QRectF(status_x, y, right_x - status_x, line2),
                Qt.AlignLeft | Qt.AlignVCenter,
                small_fm.elidedText(data['status'], Qt.ElideRight, int(right_x - status_x)),
            )

        painter.restore()


class SongList(QListWidget):
    """作品卡片列表。条目的 CARD_ROLE 放显示数据，Qt.UserRole 仍放作品数据。"""

    def __init__(self, parent=None):
        """初始化列表样式：卡片委托、逐像素滚动、隐藏横向滚动条。"""
        super().__init__(parent)
        self.setObjectName('SongList')
        self.setItemDelegate(SongCardDelegate(self))
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setResizeMode(QListWidget.Adjust)
