"""可复用的界面构建块：卡片、流式布局、分段控件、种子输入、拖放区等。

本模块是 YuE2 Studio 各页面共用的控件库。它提供：
- FlowLayout：类似 Qt 官方示例的流式布局，控件放不下时自动换行；
- hbox / label / button / form_row / divider：快捷布局与常用控件的工厂函数；
- Card / PageHeader：带标题的卡片容器与页面头部；
- Segmented：互斥分段选择控件（单选按钮组，扁平胶囊外观）；
- SeedEdit：覆盖 YuE2 完整 63 位范围的随机种子输入（QSpinBox 只支持 32 位）；
- SliderSpin：滑块 + 数值框联动的复合控件；
- Collapsible：可折叠内容块；
- DropZone：支持拖入或点击选择文件的区域；
- ChipPicker：分组标签芯片，点击后追加英文风格标签到文本框；
- Toast：淡入淡出的浮动提示气泡。
"""
from __future__ import annotations

import random
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRegularExpression,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class FlowLayout(QLayout):
    """流式布局：子控件横向排列，超出可用宽度时自动换到下一行。"""

    def __init__(self, parent=None, spacing=6):
        """初始化条目列表与间距，并把外边距清零（由容器自行控制留白）。"""
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        """把布局项追加到内部列表。"""
        self._items.append(item)

    def count(self):
        """返回布局项数量。"""
        return len(self._items)

    def itemAt(self, index):
        """按索引取布局项，越界时返回 None。"""
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        """按索引弹出布局项，越界时返回 None。"""
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        """流式布局不向任何方向扩展，返回空方向。"""
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        """声明高度依赖于宽度，触发 heightForWidth 计算。"""
        return True

    def heightForWidth(self, width):
        """给定宽度时用测试模式计算所需高度。"""
        return self._layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        """设置几何区域：先走父类逻辑，再按实际矩形重排子项。"""
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        """尺寸提示直接取最小尺寸。"""
        return self.minimumSize()

    def minimumSize(self):
        """最小尺寸为所有子项最小尺寸的并集。"""
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _layout(self, rect, test):
        """核心排版：逐项放置子控件，放不下则换行。

        `test` 为 True 时只计算不真正设置几何（用于高度测量），为 False 时实际摆放。
        返回值是内容总高度（相对 rect 顶部的偏移）。
        """
        x = rect.x()
        y = rect.y()
        line_h = 0
        for item in self._items:
            size = item.sizeHint()
            # 预测该子项放完之后的下一个 x 位置
            next_x = x + size.width() + self._spacing
            # 当前行已有内容且即将超出右边界时，先换行
            if next_x - self._spacing > rect.right() and line_h > 0:
                y = y + line_h + self._spacing
                x = rect.x()
                line_h = 0
                next_x = x + size.width() + self._spacing
            if not test:
                item.setGeometry(QRect(QPoint(x, y), size))
            line_h = max(line_h, size.height())
            x = next_x
        return y + line_h - rect.y()


def hbox(*widgets, spacing=8, margins=(0, 0, 0, 0), stretch_last=False):
    """创建水平布局并依次添加控件或子布局。

    `widgets` 中的 None 会被替换为可伸缩的空隙；`stretch_last` 为真时在末尾追加一个伸缩。
    """
    box = QHBoxLayout()
    box.setSpacing(spacing)
    box.setContentsMargins(*margins)
    for w in widgets:
        if w is None:
            box.addStretch(1)
            continue
        if isinstance(w, QLayout):
            box.addLayout(w)
        else:
            box.addWidget(w)
    if stretch_last:
        box.addStretch(1)
    return box


def label(text, name=None, wrap=False):
    """创建文本标签；可选设置对象名（用于 QSS 样式）与自动换行。"""
    w = QLabel(text)
    if name:
        w.setObjectName(name)
    w.setWordWrap(wrap)
    return w


def button(text, name=None, tooltip=None, callback=None):
    """创建按钮；可选设置对象名、悬浮提示与点击回调，并统一使用手型光标。"""
    w = QPushButton(text)
    if name:
        w.setObjectName(name)
    if tooltip:
        w.setToolTip(tooltip)
    if callback:
        w.clicked.connect(callback)
    w.setCursor(Qt.PointingHandCursor)
    return w


class Card(QFrame):
    """圆角卡片容器：可选标题、提示与步骤号，内部 body 供页面填充内容。"""

    def __init__(self, title='', hint='', step=None, parent=None):
        """构建卡片布局：头部（步骤号 + 标题）、可选提示、正文 body。"""
        super().__init__(parent)
        self.setObjectName('Card')
        box = QVBoxLayout(self)
        box.setContentsMargins(16, 14, 16, 16)
        box.setSpacing(10)

        # 头部：横向排列，右侧留伸缩空隙
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        # 存在步骤号时，在最左侧放一个居中的步骤徽标
        if step is not None:
            step_label = QLabel(str(step))
            step_label.setObjectName('StepNumber')
            step_label.setAlignment(Qt.AlignCenter)
            self.header.addWidget(step_label)
        # 有标题时添加标题标签
        if title:
            self.title_label = label(title, 'CardTitle')
            self.header.addWidget(self.title_label)
        self.header.addStretch(1)
        # 只有存在标题或步骤号时才把头部加入卡片，避免空行
        if title or step is not None:
            box.addLayout(self.header)
        # 可选提示行
        if hint:
            self.hint_label = label(hint, 'CardHint', wrap=True)
            box.addWidget(self.hint_label)

        # 正文区域，供子类或调用方继续填充
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        box.addLayout(self.body)

    def add_header_widget(self, widget):
        """向头部追加一个控件（例如放在标题右侧的按钮）。"""
        self.header.addWidget(widget)


class PageHeader(QWidget):
    """页面头部：大标题 + 可选副标题，右侧留出可伸缩区域供额外控件。"""

    def __init__(self, title, subtitle='', parent=None):
        """构建头部布局；`self.right` 为标题行布局，可继续追加右侧控件。"""
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 4)
        box.setSpacing(2)

        head = QHBoxLayout()
        head.addWidget(label(title, 'PageTitle'))
        head.addStretch(1)
        self.right = head
        box.addLayout(head)
        # 副标题存在时才显示
        if subtitle:
            box.addWidget(label(subtitle, 'PageSubtitle', wrap=True))


def scroll(widget):
    """把控件包进一个可滚动区域：无边框、隐藏横向滚动条、随控件缩放。"""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setWidget(widget)
    return area


class Segmented(QWidget):
    """分段选择控件：一组互斥按钮拼成扁平胶囊外观，选择变化时发出 changed。"""

    # 选择变化信号，携带当前选中项对应的值（item[1]）
    changed = Signal(object)

    def __init__(self, options, value=None, parent=None):
        """构建分段控件。

        `options` 为 (按钮文本, 值[, 提示]) 的三元组序列；`value` 为初始选中值，
        缺省时选中第一项。
        """
        super().__init__(parent)
        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        # 互斥按钮组，保证同一时间只有一个被选中
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self._values = []
        for i, item in enumerate(options):
            btn = QPushButton(item[0])
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            # 三元组带提示时设置 tooltip
            if len(item) > 2:
                btn.setToolTip(item[2])
            # 首尾按钮用专门的对象名，便于 QSS 做圆角拼接
            btn.setObjectName(
                'SegmentFirst' if i == 0 else ('SegmentLast' if i == len(options) - 1 else 'Segment')
            )
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.group.addButton(btn, i)
            box.addWidget(btn)
            self._values.append(item[1])

        # 点击时按按钮 id 取出对应值发出信号
        self.group.idClicked.connect(lambda i: self.changed.emit(self._values[i]))
        # 设置初始选中；未显式指定时取第一项
        self.setValue(value if value is not None else self._values[0])

    def value(self):
        """返回当前选中项对应的值，无选中时返回 None。"""
        i = self.group.checkedId()
        return self._values[i] if i >= 0 else None

    def setValue(self, value):
        """按值选中对应按钮；值不存在时保持原状。"""
        if value in self._values:
            self.group.button(self._values.index(value)).setChecked(True)


# YuE2 种子为 63 位有符号整数上限（QSpinBox 只支持 32 位，故用 QLineEdit 自定义）
SEED_MAX = 0x7FFFFFFFFFFFFFFF


class SeedEdit(QWidget):
    """种子输入控件，覆盖 YuE2 完整的 63 位范围（QSpinBox 被限制在 32 位整数）。"""

    def __init__(self, value=42, parent=None):
        """构建输入框 + 随机按钮 + 每次随机复选框的横向布局。"""
        super().__init__(parent)
        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        # 用正则校验器限制只输入 1~19 位数字，防止越界
        self.edit = QLineEdit()
        self.edit.setValidator(QRegularExpressionValidator(QRegularExpression(r'\d{1,19}'), self.edit))
        self.edit.setMinimumWidth(170)
        self.edit.setToolTip(f'0 ~ {SEED_MAX}')
        # 编辑结束后把文本归一化回合法范围
        self.edit.editingFinished.connect(lambda: self.setValue(self.value()))

        self.dice = button('🎲', 'Ghost', '随机一个种子', self.randomize)
        self.auto = QCheckBox('每次随机')
        self.auto.setToolTip('每次生成前自动换一个新的随机种子')

        box.addWidget(self.edit, 1)
        box.addWidget(self.dice)
        box.addWidget(self.auto)
        self.setValue(value)

    def randomize(self):
        """随机生成一个种子（0 ~ 2^31-1 范围）。"""
        self.setValue(random.randint(0, 2147483647))

    def value(self):
        """读取当前种子，非法输入归零，并夹在 [0, SEED_MAX] 内。"""
        try:
            return min(max(int(self.edit.text()), 0), SEED_MAX)
        except ValueError:
            return 0

    def setValue(self, value):
        """设置种子值；超出范围时抛出 ValueError。"""
        value = int(value)
        if not (0 <= value <= SEED_MAX):
            raise ValueError(f'种子超出范围 0 ~ {SEED_MAX}: {value}')
        self.edit.setText(str(value))

    def next_seed(self):
        """返回本次生成应使用的种子；勾选"每次随机"时先随机化。"""
        if self.auto.isChecked():
            self.randomize()
        return self.value()


class SliderSpin(QWidget):
    """滑块与数值框联动的复合控件：拖动滑块或改数值都会同步另一侧。"""

    def __init__(self, minimum, maximum, value, suffix='', parent=None):
        """构建横向布局：左侧滑块可伸缩，右侧固定宽度的数值框。"""
        super().__init__(parent)
        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(minimum, maximum)

        self.spin = QSpinBox()
        self.spin.setRange(minimum, maximum)
        self.spin.setSuffix(suffix)
        self.spin.setFixedWidth(84)

        # 双向同步：滑块值变化写回数值框，数值框变化写回滑块
        self.slider.valueChanged.connect(self.spin.setValue)
        self.spin.valueChanged.connect(self.slider.setValue)
        self.spin.setValue(value)

        box.addWidget(self.slider, 1)
        box.addWidget(self.spin)

    def value(self):
        """返回当前数值（以数值框为准）。"""
        return self.spin.value()

    def setValue(self, value):
        """设置数值（取整后写入数值框，进而同步滑块）。"""
        self.spin.setValue(int(value))


class Collapsible(QWidget):
    """可折叠内容块：点击标题行展开/收起正文，标题前用三角箭头指示状态。"""

    def __init__(self, title, expanded=False, parent=None):
        """构建折叠控件；`self.body` 为正文布局，供调用方继续填充。"""
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        # 标题按钮：可勾选、手型光标、左对齐无内边距，配合 QSS 的 Ghost 样式
        self.toggle = QPushButton()
        self.toggle.setObjectName('Ghost')
        self.toggle.setCheckable(True)
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.setStyleSheet('text-align:left; padding-left:0;')
        self._title = title

        # 正文容器，展开/收起由 _apply 控制可见性
        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)

        box.addWidget(self.toggle)
        box.addWidget(self.content)

        self.toggle.toggled.connect(self._apply)
        self.toggle.setChecked(expanded)
        self._apply(expanded)

    def _apply(self, expanded):
        """按展开状态同步正文可见性与标题箭头。"""
        self.content.setVisible(expanded)
        self.toggle.setText(('▾  ' if expanded else '▸  ') + self._title)


class DropZone(QFrame):
    """拖放文件区：显示文件名与大小，支持拖入或点击选择，路径变化时发出信号。"""

    # 文件路径变化信号，参数为新的路径字符串（清空时为 ''）
    fileChanged = Signal(str)

    def __init__(self, text='拖入音频文件，或点击选择', file_filter='所有文件 (*)', parent=None):
        """构建拖放区：图标 + 标题/副标题 + 清除按钮，并开启拖放接收。"""
        super().__init__(parent)
        self.setObjectName('DropZone')
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.file_filter = file_filter
        self.path = ''

        box = QHBoxLayout(self)
        box.setContentsMargins(14, 12, 14, 12)

        icon = QLabel('🎧')
        icon.setStyleSheet('font-size: 22px;')

        self.title = label(text)
        self.title.setStyleSheet('font-weight: 600;')
        self.sub = label('支持 mp3 / wav / flac / m4a 等格式', 'Hint')
        # 标题与副标题都允许被压缩，避免长文件名撑破布局
        for w in (self.title, self.sub):
            w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            w.setMinimumWidth(40)

        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(self.title)
        col.addWidget(self.sub)

        box.addWidget(icon)
        box.addLayout(col, 1)

        # 清除按钮仅在已选择文件时显示
        self.clear_btn = button('✕', 'Ghost', '清除', self.clear)
        self.clear_btn.hide()
        box.addWidget(self.clear_btn)

        # 记录占位文本，供清空后恢复标题
        self._placeholder = text

    def mousePressEvent(self, event):
        """左键点击时弹出文件选择框；初始目录取当前文件所在目录。"""
        if event.button() == Qt.LeftButton:
            path, _ = QFileDialog.getOpenFileName(
                self,
                '选择文件',
                str(Path(self.path).parent) if self.path else '',
                self.file_filter,
            )
            if path:
                self.setPath(path)

    def dragEnterEvent(self, event):
        """拖入包含 URL 的数据时接受放置并高亮。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._hover(True)

    def dragLeaveEvent(self, event):
        """拖离时取消高亮。"""
        self._hover(False)

    def dropEvent(self, event):
        """放置时取第一个本地文件路径。"""
        self._hover(False)
        files = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if files:
            self.setPath(files[0])

    def _hover(self, value):
        """切换 hover 动态属性并重刷样式，实现悬停高亮。"""
        self.setProperty('hover', 'true' if value else 'false')
        self.style().unpolish(self)
        self.style().polish(self)

    def setPath(self, path):
        """设置当前文件路径并更新界面，最后发出 fileChanged 信号。"""
        self.path = str(path) if path else ''
        if self.path:
            p = Path(self.path)
            self.title.setText(p.name)
            # 读取文件大小（MB），失败时按 0 处理
            try:
                size = p.stat().st_size / 1048576
            except OSError:
                size = 0
            self.sub.setText(f'{size:.1f} MB  ·  {p.parent}')
            self.setToolTip(str(p))
            self.clear_btn.show()
        else:
            self.title.setText(self._placeholder)
            self.sub.setText('支持 mp3 / wav / flac / m4a 等格式')
            self.clear_btn.hide()
        self.fileChanged.emit(self.path)

    def clear(self):
        """清空已选文件。"""
        self.setPath('')


class ChipPicker(QWidget):
    """分组标签芯片，点击后把对应英文风格标签追加到文本框。"""

    # 点击芯片时发出对应的英文标签
    picked = Signal(str)

    def __init__(self, groups, parent=None):
        """构建分组芯片区。

        `groups` 为 (组名, [(按钮文本, 标签), ...]) 的序列，每个组独占一行。
        """
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        for name, chips in groups:
            row = QHBoxLayout()
            row.setSpacing(8)

            # 组名标签靠右对齐，宽度固定
            cap = label(name, 'Hint')
            cap.setFixedWidth(34)
            cap.setAlignment(Qt.AlignTop | Qt.AlignRight)
            cap.setContentsMargins(0, 5, 0, 0)
            row.addWidget(cap)

            # 芯片用流式布局排列，放不下自动换行
            wrap = QWidget()
            flow = FlowLayout(wrap, spacing=6)
            for text, tag in chips:
                btn = button(text, 'Chip', tag)
                # 用默认参数捕获 tag，避免闭包晚绑定导致全部取到最后一个
                btn.clicked.connect(lambda _=False, t=tag: self.picked.emit(t))
                flow.addWidget(btn)
            row.addWidget(wrap, 1)

            box.addLayout(row)


class Toast(QLabel):
    """浮动提示气泡：淡入显示后定时淡出，用于全局的短暂通知。"""

    def __init__(self, parent=None):
        """初始化透明于鼠标的标签、透明度动画与自动淡出定时器。"""
        super().__init__(parent)
        # 让鼠标事件穿透气泡，不阻挡下方控件
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        # 透明度效果驱动淡入淡出
        self.effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.effect)

        self.anim = QPropertyAnimation(self.effect, b'opacity', self)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        # 淡出动画结束后隐藏气泡（仅在目标值为 0 时）
        self.anim.finished.connect(lambda: self.hide() if self.anim.endValue() == 0 else None)

        # 单次触发定时器，到时调用 _fade 开始淡出
        self.timer = QTimer(self, singleShot=True, timeout=self._fade)
        self.hide()

    def show_message(self, text, kind='info', ms=2600):
        """显示一条消息：按级别着色、居中到底部、淡入，`ms` 毫秒后淡出。"""
        from ..theme import C

        # 不同级别对应的左边框强调色
        tones = {'info': C['accent'], 'ok': C['green'], 'warn': C['amber'], 'error': C['red']}
        color = tones.get(kind, C['accent'])
        self.setStyleSheet(
            f'background:{C["card"]}; color:{C["text"]}; border:2px solid #000;'
            f'border-left:6px solid {color}; padding:10px 18px; font-size:13px;'
        )
        self.setText(text)
        self.adjustSize()

        # 水平居中，竖直方向距底部 130 像素
        parent = self.parentWidget()
        self.move((parent.width() - self.width()) // 2, parent.height() - self.height() - 130)
        self.raise_()
        self.show()

        # 重新启动淡入动画与淡出定时器
        self.anim.stop()
        self.anim.setDuration(160)
        self.anim.setStartValue(0)
        self.anim.setEndValue(1)
        self.anim.start()
        self.timer.start(ms)

    def _fade(self):
        """开始淡出动画（400ms 从 1 到 0），结束后由动画回调隐藏。"""
        self.anim.stop()
        self.anim.setDuration(400)
        self.anim.setStartValue(1)
        self.anim.setEndValue(0)
        self.anim.start()


def form_row(title, widget, hint=None):
    """构造一个表单行：标题（可选提示）+ 控件，返回包裹好的 QWidget。"""
    wrap = QWidget()
    box = QVBoxLayout(wrap)
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(4)

    head = QHBoxLayout()
    head.addWidget(label(title))
    # 有提示时在标题右侧放一个 ⓘ 图标，悬浮显示说明
    if hint:
        info = label('ⓘ', 'Hint')
        info.setToolTip(hint)
        head.addWidget(info)
    head.addStretch(1)
    box.addLayout(head)

    # 控件可能是布局，按类型分别处理
    if isinstance(widget, QLayout):
        box.addLayout(widget)
    else:
        box.addWidget(widget)
    return wrap


def divider():
    """返回一条水平分隔线（QSS 中 Divider 样式负责外观）。"""
    line = QFrame()
    line.setObjectName('Divider')
    return line
