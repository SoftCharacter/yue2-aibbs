"""Studio theme: a single Neubrutalism palette and stylesheet.

新野兽派（Neubrutalism）主题：奶油米黄底色、纯黑粗边框、亮色扁平色块、无圆角无渐变，
视觉参考 1.html 的 UI 风格。配色集中在 LIGHT 字典，全局 QSS 通过 {C['key']} 插值。
"""
from .paths import ASSETS
from .settings import settings

# 资源目录的 posix 路径，供 QSS 中的图标 URL 使用
ICON = ASSETS.as_posix()

# 新野兽派配色：奶油底 + 纯黑边框 + 亮色强调（红主、青次、黄绿点缀）
LIGHT = {
    'name': 'light',
    'bg': '#FFF8E7',
    'sidebar': '#FFFFFF',
    'surface': '#F6ECD6',
    'card': '#FFFFFF',
    'card_hi': '#FFF3D9',
    'hover': '#FFE66D',
    'pressed': '#FFD93D',
    'input': '#FFFFFF',
    'border': '#000000',
    'border_hi': '#000000',
    'text': '#1A1A1A',
    'muted': '#5A554A',
    'faint': '#9A9280',
    'accent': '#FF6B6B',
    'accent2': '#4ECDC4',
    'accent_hover': '#FF5252',
    'accent2_hover': '#3BB8AF',
    'accent_soft': '#FFE0E0',
    'accent_text': '#C0392B',
    'disabled_bg': '#EFE6CF',
    'disabled_text': '#B8AE93',
    'danger_border': '#000000',
    'danger_hover': '#FFE0E0',
    'cyan': '#2BA8A0',
    'green': '#2F9E44',
    'amber': '#E8A400',
    'red': '#E63946',
    'log_bg': '#FFFDF5',
    'log_text': '#3A362E',
    'wave_idle': '#DDD3BC',
    'wave_hover': '#1A1A1A',
    'shadow': 'rgba(0, 0, 0, 0)',
    'paper_page': '#FFF8E7',
    'paper_bar': '#FFFFFF',
    'chip_text': '#3A362E',
}

# 当前配色：仅保留浅色野兽派主题（深色主题已移除）
C = LIGHT

FONT_STACK = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "PingFang SC", sans-serif'

# 全局样式表：纯黑粗边框、无圆角、纯色扁平色块的新野兽派风格
QSS = f'''
* {{
    font-family: {FONT_STACK};
    font-size: 13px;
    color: {C['text']};
    outline: none;
}}
QMainWindow, QWidget#Root {{ background: {C['bg']}; }}
QWidget#Page, QWidget#PageBody, QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}
QToolTip {{
    background: {C['card']}; color: {C['text']}; border: 2px solid {C['border']};
    padding: 6px 8px;
}}

/* ---------- sidebar ---------- */
QFrame#Sidebar {{ background: {C['sidebar']}; border-right: 2px solid {C['border']}; }}
QLabel#Brand {{ font-size: 20px; font-weight: 700; padding: 2px 0; }}
QLabel#BrandSub {{ color: {C['muted']}; font-size: 11px; }}
QPushButton#NavButton {{
    text-align: left; padding: 10px 14px; border: 2px solid transparent;
    background: transparent; color: {C['muted']}; font-size: 14px;
}}
QPushButton#NavButton:hover {{ background: {C['hover']}; color: {C['text']}; border-color: {C['border']}; }}
QPushButton#NavButton:checked {{
    background: {C['accent']}; color: white; font-weight: 600; border: 2px solid {C['border']};
}}
QLabel#NavSection {{ color: {C['faint']}; font-size: 11px; padding: 10px 14px 2px 14px; letter-spacing: 1px; }}

/* ---------- headers & cards ---------- */
QLabel#PageTitle {{ font-size: 22px; font-weight: 700; }}
QLabel#PageSubtitle {{ color: {C['muted']}; font-size: 13px; }}
QFrame#Card {{ background: {C['card']}; border: 2px solid {C['border']}; }}
QLabel#CardTitle {{ font-size: 14px; font-weight: 600; }}
QLabel#CardHint, QLabel#Hint {{ color: {C['muted']}; font-size: 12px; }}
QLabel#Muted {{ color: {C['muted']}; }}
QLabel#Badge {{
    background: {C['accent_soft']}; color: {C['accent_text']}; border: 1px solid {C['border']};
    padding: 2px 8px; font-size: 11px;
}}
QLabel#StepNumber {{
    background: {C['accent']}; color: white; border: 2px solid {C['border']};
    font-weight: 700; font-size: 12px;
    min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;
}}

/* ---------- inputs ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {C['input']}; border: 2px solid {C['border']};
    padding: 6px 8px; selection-background-color: {C['accent']}; selection-color: white;
}}
QPlainTextEdit, QTextEdit {{ padding: 8px; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 2px solid {C['accent']};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: {C['faint']}; background: {C['surface']};
}}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 16px; border: none; background: transparent;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({ICON}/arrow-up.svg); width: 9px; height: 6px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({ICON}/arrow-down.svg); width: 9px; height: 6px; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url({ICON}/arrow-down.svg); width: 10px; height: 6px; }}
QComboBox QAbstractItemView {{
    background: {C['card']}; border: 2px solid {C['border']}; padding: 4px;
    selection-background-color: {C['accent_soft']}; selection-color: {C['accent_text']};
}}

/* ---------- buttons ---------- */
QPushButton {{
    background: {C['card_hi']}; border: 2px solid {C['border']};
    padding: 7px 14px; color: {C['text']};
}}
QPushButton:hover {{ background: {C['hover']}; }}
QPushButton:pressed {{ background: {C['pressed']}; }}
QPushButton:disabled {{ color: {C['faint']}; background: {C['surface']}; border-color: {C['faint']}; }}
QPushButton#Primary {{
    background: {C['accent']}; border: 2px solid {C['border']};
    color: white; font-weight: 700; font-size: 14px; padding: 10px 22px;
}}
QPushButton#Primary:hover {{ background: {C['accent_hover']}; }}
QPushButton#Primary:disabled {{ background: {C['disabled_bg']}; color: {C['disabled_text']}; border-color: {C['faint']}; }}
QPushButton#Danger {{ background: transparent; border: 2px solid {C['danger_border']}; color: {C['red']}; }}
QPushButton#Danger:hover {{ background: {C['danger_hover']}; }}
QPushButton#Danger:disabled {{ color: {C['faint']}; border-color: {C['faint']}; }}
QPushButton#Ghost {{ background: transparent; border: 2px solid transparent; color: {C['muted']}; padding: 5px 9px; }}
QPushButton#Ghost:hover {{ background: {C['hover']}; color: {C['text']}; border-color: {C['border']}; }}
QPushButton#Chip {{
    background: {C['card']}; border: 2px solid {C['border']};
    padding: 4px 11px; font-size: 12px; color: {C['chip_text']};
}}
QPushButton#Chip:hover, QPushButton#Chip:checked {{
    border-color: {C['border']}; color: {C['accent_text']}; background: {C['accent_soft']};
}}
QPushButton#Segment, QPushButton#SegmentFirst, QPushButton#SegmentLast {{
    background: {C['input']}; border: 2px solid {C['border']}; padding: 7px 12px; color: {C['muted']};
}}
QPushButton#Segment {{ }}
QPushButton#SegmentFirst {{ }}
QPushButton#SegmentLast {{ }}
QPushButton#Segment:hover, QPushButton#SegmentFirst:hover, QPushButton#SegmentLast:hover {{ color: {C['text']}; }}
QPushButton#Segment:checked, QPushButton#SegmentFirst:checked, QPushButton#SegmentLast:checked {{
    background: {C['accent_soft']}; color: {C['accent_text']}; border-color: {C['border']}; font-weight: 600;
}}
QPushButton#Segment:disabled, QPushButton#SegmentFirst:disabled, QPushButton#SegmentLast:disabled {{ color: {C['faint']}; }}
QPushButton#RoundPlay {{
    background: {C['accent']}; border: 2px solid {C['border']};
    color: white; font-size: 16px;
    min-width: 42px; max-width: 42px; min-height: 42px; max-height: 42px; padding: 0;
}}
QPushButton#RoundPlay:disabled {{ background: {C['disabled_bg']}; color: {C['disabled_text']}; border-color: {C['faint']}; }}

/* ---------- misc ---------- */
QCheckBox {{ spacing: 8px; }}
QCheckBox:disabled {{ color: {C['faint']}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 2px solid {C['border']}; background: {C['input']}; }}
QCheckBox::indicator:checked {{ background: {C['accent']}; border-color: {C['border']}; image: url({ICON}/check.svg); }}
QRadioButton::indicator {{ width: 14px; height: 14px; border: 2px solid {C['border']}; background: {C['input']}; }}
QRadioButton::indicator:checked {{ background: {C['accent']}; border: 2px solid {C['border']}; }}
QSlider::groove:horizontal {{ height: 6px; background: {C['border']}; }}
QSlider::sub-page:horizontal {{ background: {C['accent']}; }}
QSlider::handle:horizontal {{ background: {C['accent']}; border: 2px solid {C['border']}; width: 14px; height: 14px; margin: -6px 0; }}
QProgressBar {{
    background: {C['surface']}; border: 2px solid {C['border']}; height: 10px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {C['accent']}; }}
QTabWidget::pane {{ border: none; background: transparent; top: -1px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {C['muted']}; padding: 8px 14px; margin-right: 4px;
    border: 2px solid transparent;
}}
QTabBar::tab:hover {{ color: {C['text']}; }}
QTabBar::tab:selected {{ background: {C['card']}; color: {C['text']}; border: 2px solid {C['border']}; font-weight: 600; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {C['border_hi']}; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {C['faint']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {C['border_hi']}; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QSplitter::handle:vertical {{ height: 8px; }}
QTableWidget, QTreeWidget, QListWidget {{
    background: {C['card']}; border: 2px solid {C['border']};
    gridline-color: {C['border']}; alternate-background-color: {C['input']};
}}
QListWidget::item {{ padding: 4px; margin: 2px; }}
QListWidget::item:selected, QTableWidget::item:selected, QTreeWidget::item:selected {{
    background: {C['accent_soft']}; color: {C['accent_text']};
}}
QListWidget::item:hover {{ background: {C['card_hi']}; }}
/* 作品库卡片列表：条目由 SongCardDelegate 自绘，这里只去掉列表自身的底色和条目边距 */
QListWidget#SongList {{ background: transparent; border: none; padding: 0; }}
QListWidget#SongList::item, QListWidget#SongList::item:hover, QListWidget#SongList::item:selected {{
    background: transparent; border: none; margin: 0; padding: 0;
}}
QHeaderView::section {{
    background: {C['surface']}; color: {C['muted']}; border: none; border-bottom: 2px solid {C['border']};
    padding: 6px 8px; font-weight: 600;
}}
QTableCornerButton::section {{ background: {C['surface']}; border: none; }}
QStatusBar {{ background: {C['sidebar']}; border-top: 2px solid {C['border']}; color: {C['muted']}; }}
QStatusBar QLabel {{ color: {C['muted']}; padding: 0 8px; }}
QMenu {{ background: {C['card']}; border: 2px solid {C['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; }}
QMenu::item:selected {{ background: {C['accent_soft']}; color: {C['accent_text']}; }}
QMessageBox, QDialog {{ background: {C['card']}; }}
QPlainTextEdit#LogView {{
    font-family: "Cascadia Mono", Consolas, "Microsoft YaHei UI", monospace; font-size: 12px;
    background: {C['log_bg']}; border: 2px solid {C['border']}; color: {C['log_text']};
}}
QPlainTextEdit#Code {{
    font-family: "Cascadia Mono", Consolas, "Microsoft YaHei UI", monospace; font-size: 13px;
}}
QFrame#Drawer {{ background: {C['sidebar']}; border-top: 2px solid {C['border']}; }}
QFrame#DropZone {{
    background: {C['input']}; border: 2px dashed {C['border_hi']};
}}
QFrame#DropZone:hover, QFrame#DropZone[hover="true"] {{ border-color: {C['accent']}; background: {C['accent_soft']}; }}
QFrame#Divider {{ background: {C['border']}; max-height: 2px; min-height: 2px; }}
'''
