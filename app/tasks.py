"""Single background worker for GPU jobs plus stdout/stderr capture.

本模块负责 GUI 后台任务的单工作线程调度：TaskContext 封装单个任务的取消与进度上报，
TaskRunner 通过 Qt 信号把子线程的完成/失败/进度/事件安全地回投到主线程，
LogBridge 与 StreamTee 则把子线程/子进程的 stdout/stderr 转发到界面日志。
"""
from __future__ import annotations

import itertools
import sys
import threading
import traceback

from PySide6.QtCore import QObject, Signal


class TaskContext:
    """单个后台任务的可取消上下文，供工作线程查询状态并回报进度。"""

    def __init__(self, runner, task_id):
        # 持有调度器的引用，用于把进度/事件信号回投到主线程
        self._runner = runner
        # 任务唯一标识，与调度器内部回调表一一对应
        self.task_id = task_id
        # 线程安全的事件标志：置位即表示请求取消
        self._cancel = threading.Event()

    def cancelled(self):
        """返回是否已请求取消该任务。"""
        return self._cancel.is_set()

    def cancel(self):
        """置位取消标志，请求工作线程尽快退出。"""
        self._cancel.set()

    def progress(self, **info):
        """上报进度信息，任意关键字参数都会作为字典原样回投。"""
        self._runner.progress.emit(self.task_id, info)

    def emit(self, kind, payload):
        """上报自定义事件，kind 为事件类型，payload 为附带数据。"""
        self._runner.event.emit(self.task_id, kind, payload)


class TaskRunner(QObject):
    """单任务串行调度器：同一时刻只运行一个后台任务，避免 GPU 资源冲突。"""

    # 任务启动信号：(task_id, title)
    started = Signal(str, str)
    # 进度信号：(task_id, info 字典)
    progress = Signal(str, dict)
    # 事件信号：(task_id, kind, payload)
    event = Signal(str, str, object)
    # 完成信号：(task_id, result)
    finished = Signal(str, object)
    # 失败信号：(task_id, message, trace)
    failed = Signal(str, str, str)
    # 忙闲状态变化信号，界面据此禁用/启用提交入口
    busyChanged = Signal(bool)

    def __init__(self):
        super().__init__()
        # 自增的任务 id 生成器，从 1 开始，保证每次提交的 id 唯一
        self._ids = itertools.count(1)
        # 当前正在运行的任务上下文，None 表示空闲
        self._current = None
        # 当前任务的标题，用于界面展示
        self._title = ''
        # 每个任务注册的回调四元组 (on_done, on_error, on_progress, on_event)
        self._callbacks = {}
        # 把各 Qt 信号连接到对应的分发方法
        self.finished.connect(self._dispatch_done)
        self.failed.connect(self._dispatch_error)
        self.progress.connect(self._dispatch_progress)
        self.event.connect(self._dispatch_event)

    @property
    def busy(self):
        """是否有任务正在运行。"""
        return self._current is not None

    @property
    def title(self):
        """当前任务的标题。"""
        return self._title

    def submit(
        self,
        title,
        fn,
        params=None,
        *,
        on_done=None,
        on_error=None,
        on_progress=None,
        on_event=None,
    ):
        """提交一个后台任务并立即返回其 id；若已有任务在跑则拒绝并返回 None。

        fn 在工作线程中调用，签名为 ``fn(ctx, params)``，ctx 为 TaskContext。
        """
        # 忙时拒绝新任务，保证 GPU 任务串行执行
        if self._current is not None:
            return None

        task_id = str(next(self._ids))
        ctx = TaskContext(self, task_id)
        self._current = ctx
        self._title = title
        self._callbacks[task_id] = (on_done, on_error, on_progress, on_event)
        self.busyChanged.emit(True)
        self.started.emit(task_id, title)

        def work():
            # 实际任务入口：先执行用户函数，再按结果/异常回投对应信号
            try:
                result = fn(ctx, params or {})
                self.finished.emit(task_id, result)
            except InterruptedError:
                # 主动取消：仅上报简洁的"已取消"信息，不打印堆栈
                self.failed.emit(task_id, '已取消', '')
            except BaseException as exc:
                # 组装"异常类型: 消息"的摘要，并对显存不足给出可操作提示
                message = f'{type(exc).__name__}: {exc}'
                if 'out of memory' in str(exc).lower():
                    message = (
                        '显存不足：'
                        + message
                        + '\n可在「设置」中勾选运行前卸载其他模型，或降低生成长度。'
                    )
                # 完整堆栈先打印到 stderr，再随失败信号一并回投
                print(traceback.format_exc(), file=sys.stderr)
                self.failed.emit(task_id, message, traceback.format_exc())

        threading.Thread(target=work, name=f'task-{task_id}', daemon=True).start()
        return task_id

    def cancel(self):
        """请求取消当前正在运行的任务（若无任务则不做任何事）。"""
        if self._current is not None:
            self._current.cancel()

    def _finish(self, task_id):
        """清理指定任务：若是当前任务则复位为空闲，并移除其回调并返回。"""
        if self._current is not None and self._current.task_id == task_id:
            self._current = None
            self._title = ''
            self.busyChanged.emit(False)
        return self._callbacks.pop(task_id, (None, None, None, None))

    def _dispatch_done(self, task_id, result):
        """完成信号分发：清理任务后调用 on_done 回调（若注册）。"""
        cb = self._finish(task_id)[0]
        if cb:
            cb(result)

    def _dispatch_error(self, task_id, message, trace):
        """失败信号分发：清理任务后调用 on_error 回调（若注册）。"""
        cb = self._finish(task_id)[1]
        if cb:
            cb(message, trace)

    def _dispatch_progress(self, task_id, info):
        """进度信号分发：调用 on_progress 回调（若注册）。"""
        cb = self._callbacks.get(task_id, (None, None, None, None))[2]
        if cb:
            cb(info)

    def _dispatch_event(self, task_id, kind, payload):
        """事件信号分发：调用 on_event 回调（若注册）。"""
        cb = self._callbacks.get(task_id, (None, None, None, None))[3]
        if cb:
            cb(kind, payload)


class LogBridge(QObject):
    """在界面上展示日志文本的信号桥，外部通过 text 信号推送一行日志。"""

    text = Signal(str)


class StreamTee:
    """Forward writes to the original stream and to the in-app log.

    包装一个原始流（如 sys.stdout），把每次 write 同时转发到 LogBridge，
    使子线程/子进程的打印内容也能出现在界面日志里。
    """

    def __init__(self, original, bridge):
        self.original = original
        self.bridge = bridge

    def write(self, data):
        """把数据写到原始流，并在数据非空时推送到日志信号；返回写入长度。"""
        if self.original is not None:
            try:
                self.original.write(data)
            except (OSError, ValueError):
                # 原始流已关闭等情况下忽略写入错误，保证日志转发不受影响
                pass
        if data:
            self.bridge.text.emit(data)
        return len(data)

    def flush(self):
        """刷新原始流，忽略其关闭等异常。"""
        if self.original is not None:
            try:
                self.original.flush()
            except (OSError, ValueError):
                pass

    def isatty(self):
        """恒返回 False：转发流不视为交互式终端。"""
        return False

    @property
    def encoding(self):
        """返回原始流的编码，缺失时回落到 utf-8。"""
        return getattr(self.original, 'encoding', None) or 'utf-8'

    def __getattr__(self, name):
        """把未定义的属性访问透传给原始流；原始流为空则抛出 AttributeError。"""
        if self.original is None:
            raise AttributeError(name)
        return getattr(self.original, name)


# 全局单例：应用共享的后台任务调度器与日志桥
runner = TaskRunner()
log_bridge = LogBridge()
