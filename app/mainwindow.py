"""Main window: sidebar navigation, pages, log drawer and status bar.

本模块定义应用主窗口：左侧导航侧栏、右侧页面堆栈、底部日志抽屉与状态栏，
并统一承载后台任务状态、日志转发、GPU 显存监控与关闭前的保存确认逻辑。
"""
from __future__ import annotations

import sys
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .pages.batch import BatchPage
from .pages.cover import CoverPage
from .pages.create import CreatePage
from .pages.edit import EditPage
from .pages.library import LibraryPage
from .pages.lyrics import LyricsPage
from .pages.mert import MertPage
from .pages.settings_page import SettingsPage
from .pages.transcribe import TranscribePage
from .settings import settings
from .tasks import log_bridge, runner
from .theme import C
from .widgets.common import Toast, button, label

# 侧栏导航结构：(分组名, [(页面标识, 按钮文案), ...])，页面标识与 pages 字典键一一对应
NAV = [
    ('创作', [
        ('create', '🎵  创作歌曲'),
        ('cover', '🎙️  AI 翻唱'),
        ('batch', '🔁  批量生成'),
        ('edit', '🎼  乐谱编辑'),
    ]),
    ('分析', [
        ('transcribe', '📝  扒谱分析'),
        ('lyrics', '🎤  歌词识别'),
        ('mert', '🧬  音乐特征'),
    ]),
    ('管理', [
        ('library', '📚  作品库'),
        ('settings', '⚙️  设置'),
    ]),
]


class _Warmup(QObject):
    """在后台线程检测显卡，并把结果通过 ready 信号回投到主线程。"""

    # 检测结果字典：torch 版本、GPU 名称/显存，失败时带 error 字段
    ready = Signal(dict)


class MainWindow(QMainWindow):
    """主窗口：组合侧栏、页面堆栈、日志抽屉与状态栏，并处理关闭流程。"""

    def __init__(self):
        super().__init__()
        # 标题里带上版本号与联系方式
        self.setWindowTitle(
            f'YuE2 Studio {__version__} · AI音乐创作工作站'
        )
        self.resize(1480, 940)
        self.setMinimumSize(1100, 700)

        # 根容器：水平布局（侧栏 + 主区），无外边距与间距
        root = QWidget()
        root.setObjectName('Root')
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 左侧导航侧栏：固定宽度，纵向布局
        sidebar = QFrame()
        sidebar.setObjectName('Sidebar')
        sidebar.setFixedWidth(212)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 18, 12, 14)
        side.setSpacing(2)

        # 品牌区：渐变圆形音符 logo + 品牌名/副标题
        brand = QHBoxLayout()
        logo = QLabel('♪')
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(38, 38)
        logo.setStyleSheet(
            f'background: {C["accent"]};'
            'border: 2px solid #000; color: white; font-size: 22px; font-weight: 800;'
        )
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_text.addWidget(label('YuE2 Studio', 'Brand'))
        brand_text.addWidget(label('AI 音乐创作工作站', 'BrandSub'))
        brand.addWidget(logo)
        brand.addLayout(brand_text)
        side.addLayout(brand)
        side.addSpacing(14)

        # 页面堆栈：一次性创建全部页面，按 key 建立映射
        self.stack = QStackedWidget()
        self.pages = {
            'create': CreatePage(self),
            'cover': CoverPage(self),
            'batch': BatchPage(self),
            'edit': EditPage(self),
            'transcribe': TranscribePage(self),
            'lyrics': LyricsPage(self),
            'mert': MertPage(self),
            'library': LibraryPage(self),
            'settings': SettingsPage(self),
        }

        # 导航按钮：放入同一按钮组实现单选，点击后切页并高亮
        self.nav_group = QButtonGroup(self)
        self.nav_buttons = {}
        for section, items in NAV:
            side.addWidget(label(section.upper(), 'NavSection'))
            for key, text in items:
                btn = QPushButton(text)
                btn.setObjectName('NavButton')
                btn.setCheckable(True)
                btn.setCursor(Qt.PointingHandCursor)
                # 用默认参数固定当前 key，避免闭包晚绑定导致的串页
                btn.clicked.connect(lambda _=False, k=key: self.go(k))
                self.nav_group.addButton(btn)
                self.nav_buttons[key] = btn
                side.addWidget(btn)
                self.stack.addWidget(self.pages[key])
        side.addStretch(1)

        # 显卡信息卡片：名称、显存进度条与用量文本
        card = QFrame()
        card.setObjectName('Card')
        card_box = QVBoxLayout(card)
        card_box.setContentsMargins(10, 8, 10, 10)
        card_box.setSpacing(4)
        self.gpu_name = label('正在检测显卡…', 'Hint', wrap=True)
        self.gpu_bar = QProgressBar()
        self.gpu_bar.setFixedHeight(6)
        self.gpu_bar.setRange(0, 100)
        self.gpu_text = label('', 'Hint')
        card_box.addWidget(self.gpu_name)
        card_box.addWidget(self.gpu_bar)
        card_box.addWidget(self.gpu_text)
        side.addWidget(card)

        layout.addWidget(sidebar)

        # 主区分割器：上方页面堆栈，下方可收起/展开的日志抽屉
        self.vsplit = QSplitter(Qt.Vertical)
        self.vsplit.addWidget(self.stack)

        drawer = QFrame()
        drawer.setObjectName('Drawer')
        drawer_box = QVBoxLayout(drawer)
        drawer_box.setContentsMargins(16, 8, 16, 10)
        header = QHBoxLayout()
        header.addWidget(label('运行日志', 'CardTitle'))
        header.addStretch(1)
        header.addWidget(button('清空', 'Ghost', callback=lambda: self.log_view.clear()))
        header.addWidget(button('收起', 'Ghost', callback=lambda: self.toggle_log(False)))
        drawer_box.addLayout(header)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName('LogView')
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)
        drawer_box.addWidget(self.log_view)

        self.drawer = drawer
        self.vsplit.addWidget(drawer)
        self.vsplit.setSizes([760, 200])
        drawer.hide()
        layout.addWidget(self.vsplit, 1)

        # 状态栏：任务标签、不确定进度条、取消按钮与日志开关
        status = self.statusBar()
        self.task_label = QLabel('就绪')
        self.task_bar = QProgressBar()
        self.task_bar.setFixedSize(120, 6)
        self.task_bar.setRange(0, 0)
        self.task_bar.hide()
        self.cancel_btn = button('取消', 'Ghost', callback=runner.cancel)
        self.cancel_btn.hide()
        status.addWidget(self.task_label)
        status.addWidget(self.task_bar)
        status.addWidget(self.cancel_btn)
        self.log_btn = button(
            '📜 日志',
            'Ghost',
            '显示/隐藏运行日志',
            lambda: self.toggle_log(not self.drawer.isVisible()),
        )
        status.addPermanentWidget(self.log_btn)

        # 弹窗提示器（Toast）与后台任务信号的绑定
        self.toaster = Toast(self)
        runner.started.connect(self._task_started)
        runner.busyChanged.connect(self._busy)

        # 日志缓冲：先攒在列表里，再由定时器批量刷新到文本框，减少界面抖动
        self._log_buffer = []
        log_bridge.text.connect(self._log_text)
        self._log_timer = QTimer(self, interval=120, timeout=self._flush_log)
        self._log_timer.start()

        # 全局快捷键：回车触发当前页主操作，Ctrl+L 切换日志抽屉
        QShortcut(QKeySequence('Ctrl+Return'), self, activated=self._primary)
        QShortcut(QKeySequence('Ctrl+Enter'), self, activated=self._primary)
        QShortcut(
            QKeySequence('Ctrl+L'),
            self,
            activated=lambda: self.toggle_log(not self.drawer.isVisible()),
        )

        # 显卡监控：后台线程做一次性探测，定时器周期刷新显存占用
        self._gpu_timer = QTimer(self, interval=2000, timeout=self._gpu_tick)
        self._warm = _Warmup()
        self._warm.ready.connect(self._warm_ready)
        threading.Thread(target=self._warmup, daemon=True).start()

        self.go('create')

    def go(self, key):
        """切换到指定页面：更新堆栈、高亮导航按钮并触发页面的 on_show 钩子。"""
        page = self.pages[key]
        self.stack.setCurrentWidget(page)
        self.nav_buttons[key].setChecked(True)
        page.on_show()

    def open_in_editor(self, data):
        """把作品数据载入乐谱编辑页并跳转。"""
        self.pages['edit'].load(data)
        self.go('edit')

    def open_in_cover(self, transcription):
        """把扒谱结果送入 AI 翻唱页，并提示补充歌词与风格。"""
        self.pages['cover'].load_transcription(transcription)
        self.go('cover')
        self.toast('乐谱已送到 AI 翻唱，请补充歌词和目标风格', 'ok')

    def open_in_create(self, meta):
        """把作品元数据载入创作页并跳转。"""
        self.pages['create'].load_params(meta)
        self.go('create')
        self.toast('参数已载入创作页', 'ok')

    def open_in_library(self, data):
        """刷新作品库并定位到指定文件夹，然后跳转。"""
        page = self.pages['library']
        page.refresh()
        page.select_folder(data['dir'])
        self.go('library')

    def library_changed(self):
        """标记作品库需要刷新；若当前正显示作品库则立即刷新。"""
        page = self.pages['library']
        page.dirty = True
        if self.stack.currentWidget() is page:
            page.refresh()

    def toast(self, text, kind='info'):
        """在右下角弹出一条轻量提示。"""
        self.toaster.show_message(text, kind)

    def show_error(self, title, message, trace=''):
        """弹出一个带可选详细堆栈的警告对话框。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(message[:1500])
        if trace:
            box.setDetailedText(trace)
        box.exec()

    def toggle_log(self, visible):
        """显示/隐藏日志抽屉；显示时把光标滚到末尾。"""
        self.drawer.setVisible(visible)
        if visible:
            self.log_view.moveCursor(QTextCursor.End)

    def _log_text(self, text):
        """日志桥每推送一段文本，先追加到缓冲区。"""
        self._log_buffer.append(text)

    def _flush_log(self):
        """把缓冲区的日志统一写入文本框，并在用户停在底部时自动跟随滚动。"""
        if not self._log_buffer:
            return
        text = ''.join(self._log_buffer).replace('\r\n', '\n').replace('\r', '\n')
        self._log_buffer.clear()

        cursor = self.log_view.textCursor()
        at_bottom = (
            self.log_view.verticalScrollBar().value()
            >= self.log_view.verticalScrollBar().maximum() - 4
        )
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        if at_bottom:
            self.log_view.verticalScrollBar().setValue(
                self.log_view.verticalScrollBar().maximum()
            )

    def _task_started(self, _task_id, title):
        """后台任务开始时更新状态栏标签并打印一行日志。"""
        self.task_label.setText(f'⏳ {title}')
        print(f'[YuE2 Studio] 开始任务：{title}')

    def _busy(self, busy):
        """按忙闲状态显示/隐藏进度条与取消按钮，空闲时复位标签。"""
        self.task_bar.setVisible(busy)
        self.cancel_btn.setVisible(busy)
        if not busy:
            self.task_label.setText('就绪')

    def _primary(self):
        """触发当前页面绑定的主操作（例如创作页的"开始生成"）。"""
        self.stack.currentWidget().primary_action()

    def _warmup(self):
        """后台线程：探测 PyTorch 与 CUDA 显卡信息，结果经信号回投。"""
        info = {}
        try:
            import torch

            info['torch'] = torch.__version__
            if torch.cuda.is_available():
                props = torch.cuda.get_device_properties(0)
                info['name'] = props.name
                info['total'] = props.total_memory
                torch.cuda.mem_get_info()
        except Exception as exc:
            info['error'] = str(exc)
        self._warm.ready.emit(info)

    def _warm_ready(self, info):
        """接收显卡探测结果：有 GPU 则启动显存监控，否则显示未检测到提示。"""
        if info.get('name'):
            self.gpu_name.setText(f'🖥 {info["name"]}')
            self._gpu_timer.start()
            self._gpu_tick()
        else:
            self.gpu_name.setText(
                '⚠ 未检测到 CUDA 显卡'
                + (f'\n{info["error"][:80]}' if info.get('error') else '')
            )
        print(
            f'[YuE2 Studio] PyTorch {info.get("torch", "?")} · GPU {info.get("name", "无")}'
        )

    def _gpu_tick(self):
        """周期刷新显存占用进度条与用量文本；无 torch 或查询失败时静默跳过。"""
        torch = sys.modules.get('torch')
        if torch is None:
            return
        try:
            free, total = torch.cuda.mem_get_info()
        except Exception:
            return
        used = total - free
        self.gpu_bar.setValue(int(used / total * 100))
        self.gpu_text.setText(
            f'显存 {used / 1073741824:.1f} / {total / 1073741824:.1f} GiB'
        )

    def closeEvent(self, event):
        """关闭前：先停掉乐谱导出、确认后台任务、再保存草稿并提示未保存的编辑。"""
        from .widgets.render_export import _render_runner

        # 乐谱导出尚在运行时：请求取消，稍后自动重试关闭
        if _render_runner.busy:
            _render_runner.cancel()
            if not getattr(self, '_waiting_render_close', False):
                self.toast('正在停止乐谱导出并关闭浏览器进程…', 'info')
            self._waiting_render_close = True
            event.ignore()
            QTimer.singleShot(200, self.close)
            return
        self._waiting_render_close = False

        # 有 GPU 任务在跑时，需用户确认是否强制退出
        if runner.busy and QMessageBox.question(
            self, '退出', f'任务“{runner.title}”仍在运行，确定要退出吗？'
        ) != QMessageBox.Yes:
            event.ignore()
            return

        # 逐页保存草稿；对仍有未保存编辑的页面询问是否放弃
        for page in self.pages.values():
            if page.save_draft() is not False:
                continue
            if not page.has_unsaved_edits():
                continue
            if QMessageBox.question(
                self,
                '退出',
                '批量生成表格里有编辑还没保存。\n\n放弃这些编辑并退出吗？选“否”留在程序里，稍后再保存。',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            ) != QMessageBox.Yes:
                event.ignore()
                return
            page.discard_pending_edits()

        settings.save()
        event.accept()
