"""Browse, replay and reuse generated songs.

本模块实现"作品库"页面：统一展示草稿、未完成任务与已生成的歌曲，支持搜索与类型
筛选、播放试听、查看歌词/风格参数/乐谱、继续未完成任务、重新解码、导出 MP3 与乐谱，
以及安全删除作品文件夹。
"""
from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import jobs
from ..engine import engine, YUE2_STEPS
from ..paths import open_in_explorer, output_root
from ..settings import settings
from ..tasks import runner
from ..widgets.common import Card, button, label
from ..widgets.player import AudioPlayer, fmt_time
from ..widgets.progress import StageProgress
from ..widgets.result import export_mp3
from ..widgets.score import ScoreView
from ..widgets.song_list import CARD_ROLE, STATUS_TONES, SongList, card_data
from .base import BasePage
from .edit import load_run

# 作品生成方式的中文名称，键对应 meta 里的 mode 字段
MODE_NAMES = {
    'create': '创作',
    'cover': '翻唱',
    'edit': '编辑',
    'decode': '重新解码',
}

# 乐谱规划模式的中文名称，键对应 meta 里的 cot 字段
COT_NAMES = {
    'full': '旋律+和弦',
    'melody': '仅旋律',
    'off': '无乐谱',
}


class LibraryPage(BasePage):
    """作品库页面：统一管理并复用已生成/未完成的音乐作品。"""

    title = '📚 作品库'
    subtitle = '草稿、未完成任务和歌曲统一保存。继续任务会复用完整阶段；重新解码会创建新作品。'

    def __init__(self, main):
        super().__init__(main)
        # 标记作品列表需要刷新；current 记录当前选中作品的数据字典
        self.dirty = True
        self.current = None

        # 顶部页头右侧：载入 latent 重新解码、刷新列表、打开输出目录
        self.header.right.addWidget(
            self.run_button('载入 latent 重新解码', self.import_latent, primary=False)
        )
        self.header.right.addWidget(button('🔄 刷新', callback=self.refresh))
        self.header.right.addWidget(
            button('📂 打开输出目录', callback=lambda: open_in_explorer(self._songs_dir()))
        )

        # 左侧栏：搜索框 + 类型筛选 + 作品列表 + 计数标签
        left_widget = QWidget()
        left_box = QVBoxLayout(left_widget)
        left_box.setContentsMargins(0, 0, 8, 0)

        # 顶部一行：搜索输入框（实时过滤）与类型下拉框
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('🔍 搜索标题 / 风格 / 歌词…')
        self.search.textChanged.connect(self._filter)
        self.mode = QComboBox()
        self.mode.addItem('全部类型', '')
        for key, name in MODE_NAMES.items():
            self.mode.addItem(name, key)
        self.mode.currentIndexChanged.connect(self._filter)
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.mode)
        left_box.addLayout(search_row)

        # 作品列表：自定义绘制卡片，选中项变化时更新右侧详情
        self.list = SongList()
        self.list.currentItemChanged.connect(self._select)
        left_box.addWidget(self.list, 1)
        self.count = label('', 'Hint')
        left_box.addWidget(self.count)

        # 右侧栏：播放器卡片 + 标签页（歌词/风格参数/乐谱）
        right_widget = QWidget()
        right_box = QVBoxLayout(right_widget)
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.setSpacing(10)

        # 播放器卡片：含音频播放、元信息、动作按钮与任务进度
        card = Card('')
        self.player = AudioPlayer()
        card.body.addWidget(self.player)
        self.meta = label('选择左侧的作品查看详情', 'Hint', wrap=True)
        card.body.addWidget(self.meta)

        # 动作按钮行：载入参数/编辑乐谱/导出 MP3/打开文件夹，右侧删除按钮
        actions_row = QHBoxLayout()
        actions_row.setSpacing(6)
        self.actions = [
            button('↩ 载入参数到创作页', callback=self.to_create),
            button('✏️ 编辑乐谱再生成', callback=self.to_editor),
            button('💾 导出 MP3', callback=self.to_mp3),
            button(
                '📂 打开文件夹',
                callback=lambda: (open_in_explorer(self.current['dir']) if self.current else None),
            ),
        ]
        for b in self.actions:
            actions_row.addWidget(b)
        actions_row.addStretch(1)
        self.delete_btn = button('🗑 删除', 'Danger', callback=self.delete)
        actions_row.addWidget(self.delete_btn)
        self.actions.append(self.delete_btn)
        card.body.addLayout(actions_row)

        # 任务操作行：继续任务/重新解码/导出乐谱，右侧停止按钮
        extra_row = QHBoxLayout()
        self.resume_btn = button('▶ 继续任务', callback=self.resume_current)
        self.decode_btn = button('重新解码', callback=self.decode_current)
        self.render_btn = button('导出乐谱 / 钢琴', callback=self.export_score)
        self.extra_actions = [self.resume_btn, self.decode_btn, self.render_btn]
        for b in self.extra_actions:
            extra_row.addWidget(b)
            b.setEnabled(False)
        extra_row.addStretch(1)
        extra_row.addWidget(self.stop_button())
        card.body.addLayout(extra_row)

        # 阶段进度条：默认隐藏，仅任务进行时显示
        self.progress = StageProgress(YUE2_STEPS)
        self.progress.hide()
        card.body.addWidget(self.progress)
        right_box.addWidget(card)

        # 详情标签页：歌词 / 风格与参数 / 乐谱
        self.tabs = QTabWidget()
        self.style_view = QPlainTextEdit(readOnly=True)
        self.lyrics_view = QPlainTextEdit(readOnly=True)
        self.score = ScoreView()
        self.tabs.addTab(self.lyrics_view, '歌词')
        self.tabs.addTab(self.style_view, '风格与参数')
        self.tabs.addTab(self.score, '乐谱')
        right_box.addWidget(self.tabs, 1)

        # 未选中作品时，动作按钮全部禁用
        for b in self.actions:
            b.setEnabled(False)

        # 左右两栏分割布局
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left_widget)
        split.addWidget(right_widget)
        split.setSizes([420, 780])
        self.root.addWidget(split, 1)

    def _songs_dir(self):
        """返回作品输出目录（不存在则创建）；创建失败时弹提示但仍返回路径。"""
        d = output_root(settings) / 'songs'
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.toast(f'输出目录不可用：{exc}', 'error')
        return d

    def on_show(self):
        """页面变为可见时：若作品库被标记为脏，则刷新列表。"""
        if self.dirty:
            self.refresh()

    def refresh(self):
        """扫描输出目录并重建作品列表，保持当前选中项（若仍存在）。"""
        self.dirty = False
        # 记录当前选中目录，刷新后尽量恢复选中
        keep = self.current['dir'] if self.current else None
        self.list.blockSignals(True)
        self.list.clear()
        self.list.blockSignals(False)
        songs = self._songs_dir()

        # 列出所有子目录，按名称倒序（最新在前）；目录不可读时置空
        try:
            dirs = sorted(
                (p for p in songs.iterdir() if p.is_dir()),
                key=lambda p: p.name,
                reverse=True,
            )
        except OSError:
            dirs = []

        selected = False
        for folder in dirs:
            # 逐文件夹还原；单个文件损坏时降级为"文件有误"卡片而非中断整个刷新
            try:
                data = load_run(folder)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
                data = {
                    'dir': str(folder),
                    'audio': '',
                    'abc': '',
                    'meta': {'title': folder.name},
                    'invalid': str(exc),
                }
            if data is None:
                continue

            meta = data['meta']
            # 创建时间：优先取元数据，否则用目录修改时间
            created = meta.get('created') or datetime.fromtimestamp(
                folder.stat().st_mtime
            ).isoformat(timespec='seconds')
            # 标题：优先取元数据，否则用歌词首行非段落标签，最后回退到目录名
            title = meta.get('title') or next(
                (
                    line.strip()
                    for line in meta.get('lyrics', '').splitlines()
                    if line.strip() and not line.strip().startswith('[')
                ),
                folder.name,
            )
            mode_name = MODE_NAMES.get(meta.get('mode'), '生成')
            # 附加信息：乐谱模式 + 种子，过滤掉空项
            extra = [
                COT_NAMES.get(meta.get('cot'), ''),
                f'种子 {meta["seed"]}' if meta.get('seed') is not None else '',
            ]
            extra = [x for x in extra if x]

            # 任务状态：有任务时显示阶段进度与状态，否则显示文件错误
            status, tone = '', None
            if data.get('job'):
                job = data['job']
                disp = jobs.display_status(folder, job)
                tone = STATUS_TONES.get(disp, 'muted')
                batch = job.get('batch_count', job['params'].get('count', 1))
                if batch > 1:
                    extra.append(f'批量 {batch} 首')
                if tone:
                    status = f'{disp} · {jobs.STAGE_NAMES[job["stage"]]}'
            elif data.get('invalid'):
                status, tone = '文件有误，无法继续', 'red'

            # 日期：去掉 T 与时分秒，若为今年则进一步省略年份
            date = created.replace('T', ' ')[:16]
            if date.startswith(f'{datetime.now().year}-'):
                date = date[5:]
            dur = fmt_time(meta['duration']) if meta.get('duration') else ''

            # 组装列表项：标题 · 类型 · 附加信息 · 时长 · 日期 · 状态
            item = QListWidgetItem(
                ' · '.join(x for x in [title, mode_name, *extra, dur, date, status] if x)
            )
            item.setData(Qt.UserRole, data)
            item.setData(
                CARD_ROLE,
                card_data(
                    title=title,
                    mode_key=meta.get('mode'),
                    mode_name=mode_name,
                    detail=' · '.join(extra),
                    date=date,
                    duration=dur,
                    status=status,
                    status_tone=tone,
                ),
            )
            item.setToolTip(meta.get('style', ''))
            self.list.addItem(item)

            # 恢复之前选中的目录
            if keep == data['dir']:
                self.list.setCurrentItem(item)
                selected = True

        # 原选中项已不存在时清空选择
        if not selected:
            self._clear_selection()
        self._filter()

    def _clear_selection(self):
        """清空当前选择并复位播放器、详情视图与所有动作按钮。"""
        self.current = None
        self.player.set_source('')
        self.meta.setText('选择左侧的作品查看详情')
        self.lyrics_view.clear()
        self.style_view.clear()
        self.score.set_abc('')
        for b in self.actions + self.extra_actions:
            b.setEnabled(False)

    def _filter(self):
        """按搜索文本与类型下拉框过滤列表，并更新计数标签。"""
        text = self.search.text().strip().lower()
        mode = self.mode.currentData()
        shown = 0
        for i in range(self.list.count()):
            item = self.list.item(i)
            meta = item.data(Qt.UserRole)['meta']
            # 拼接标题/风格/歌词作为搜索目标
            blob = ' '.join(str(meta.get(k, '')) for k in ('title', 'style', 'lyrics')).lower()
            ok = (not text or text in blob) and (not mode or meta.get('mode') == mode)
            item.setHidden(not ok)
            shown += ok
        self.count.setText(f'共 {self.list.count()} 项，显示 {shown} 项')

    def _select(self, item, _previous=None):
        """选中某项后更新右侧详情：播放器、元信息、歌词、风格参数与乐谱。"""
        if item is None:
            return
        data = item.data(Qt.UserRole)
        # 判断是否仍选中同一作品（目录与音频都未变），避免重复加载
        unchanged = (
            self.current is not None
            and self.current['dir'] == data['dir']
            and self.player.path == data['audio']
        )
        self.current = data
        meta = data['meta']

        # 标题回退逻辑与 refresh 一致：标题 → 歌词首行 → 目录名
        title = meta.get('title') or next(
            (
                line.strip()
                for line in meta.get('lyrics', '').splitlines()
                if line.strip() and not line.strip().startswith('[')
            ),
            Path(data['dir']).name,
        )

        # 仅当切换到不同作品时才重新加载音频
        if not unchanged:
            self.player.set_source(data['audio'], title)

        # 元信息摘要：生成方式 · 规划模式 · 种子 · ODE 步数 · CFG
        cfg = meta.get('cfg_scale')
        self.meta.setText(
            f'{MODE_NAMES.get(meta.get("mode"), "生成")} · 规划 '
            f'{COT_NAMES.get(meta.get("cot"), meta.get("cot"))} · 种子 {meta.get("seed")} · '
            f'ODE {meta.get("ode_steps", "—")} 步 · CFG {cfg if cfg is not None else "默认"}'
            + (f' · 生成用时 {meta["generation_seconds"]:.0f}s' if meta.get('generation_seconds') else '')
        )
        self.lyrics_view.setPlainText(meta.get('lyrics', ''))

        # 风格与参数视图：逐行拼出风格、目录、原曲、采样、解码等关键信息
        lines = [f'风格：{meta.get("style", "")}', '', f'目录：{data["dir"]}']
        if meta.get('source_audio'):
            lines.append(f'原曲：{meta["source_audio"]}')
        if meta.get('abc_sampling') or meta.get('semantic_sampling'):
            lines += [
                '',
                f'乐谱采样：{meta.get("abc_sampling")}',
                f'歌曲采样：{meta.get("semantic_sampling")}',
            ]
        if meta.get('decode_options'):
            lines.append(f'解码设置：{meta["decode_options"]}')
        if meta.get('actual_decode_options'):
            lines.append(f'本次实际解码：{meta["actual_decode_options"]}')

        # 有任务时补充任务状态、模型设置、上次错误与批量子任务提示
        if data.get('job'):
            job = data['job']
            status = f'{jobs.display_status(data["dir"], job)} · {jobs.STAGE_NAMES[job["stage"]]}'
            self.meta.setText(self.meta.text() + '\n' + status)
            lines += [
                f'任务状态：{status}',
                f'模型设置：{job["runtime"]}',
                '继续任务使用保存的参数；显存预算采用设置页当前值。',
            ]
            if job.get('error'):
                lines.append(f'上次错误：{job["error"]}')
            if job.get('batch_children'):
                lines.append('本任务包含批量子任务，继续时会跳过已完成歌曲；子任务也可以单独继续。')
        if data.get('invalid'):
            lines.append(f'文件错误：{data["invalid"]}')

        self.style_view.setPlainText('\n'.join(lines))
        self.score.set_abc(data.get('abc') or '')
        self._update_actions(runner.busy)

    def _update_actions(self, busy):
        """按当前作品内容与任务忙闲状态，更新各动作按钮的可用性。"""
        data = self.current or {}
        # 有数据且非"文件有误"时才视为有效
        valid = bool(data) and not data.get('invalid')

        # 基础动作按钮：只要有数据即可用（后续按具体内容细化）
        for b in self.actions:
            b.setEnabled(bool(data))
        # 载入参数需有歌词、编辑乐谱需有乐谱、导出 MP3 需有音频
        self.actions[0].setEnabled(valid and bool(data.get('meta', {}).get('lyrics')))
        self.actions[1].setEnabled(valid and bool(data.get('abc')))
        self.actions[2].setEnabled(valid and bool(data.get('audio')))
        self.delete_btn.setEnabled(bool(data) and not busy)

        # 继续任务：需存在可继续的 job
        job = data.get('job') or {}
        try:
            can_resume = valid and bool(job) and jobs.can_resume(data['dir'], job)
        except (OSError, ValueError, KeyError):
            can_resume = False
        self.resume_btn.setEnabled(bool(can_resume) and not busy)

        # 重新解码：需目录下存在 latent.npy
        self.decode_btn.setEnabled(
            valid
            and bool(data.get('dir'))
            and (Path(data['dir']) / 'latent.npy').is_file()
            and not busy
        )
        # 导出乐谱：需有乐谱
        self.render_btn.setEnabled(valid and bool(data.get('abc')))

    def set_busy(self, busy):
        """后台任务忙闲变化时，联动父类按钮状态并刷新动作按钮。"""
        super().set_busy(busy)
        if hasattr(self, 'extra_actions'):
            self._update_actions(busy)

    def resume_current(self):
        """继续当前选中作品的已保存任务。"""
        if not self.current or not self.ensure_idle() or not self.resume_btn.isEnabled():
            return
        self.progress.show()
        self.progress.start('继续已保存的任务…')
        runner.submit(
            '继续生成',
            engine.run_resume,
            {'dir': self.current['dir']},
            on_done=self._done,
            on_error=self._error,
            on_progress=self.progress.update_info,
            on_event=self._event,
        )

    def _event(self, kind, payload):
        """接收后台任务事件：有阶段性成果时标记作品库需要刷新。"""
        if kind in ('job', 'item'):
            self.main.library_changed()

    def _done(self, results):
        """任务完成：结束进度条、刷新列表并定位到最新结果。"""
        self.progress.finish(True, '任务已完成')
        self.refresh()
        if results:
            self.select_folder(results[-1]['dir'])
        self.toast('歌曲已保存', 'ok')

    def _error(self, message, trace):
        """任务失败：结束进度条并刷新；非取消场景弹出错误对话框。"""
        self.progress.finish(False, message.splitlines()[0][:160])
        self.refresh()
        if message != '已取消':
            self.main.show_error('任务未完成，已完成阶段可继续', message, trace)

    def select_folder(self, folder):
        """在列表中定位并选中指定目录对应的作品项。"""
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(Qt.UserRole)['dir'] == str(folder):
                self.search.clear()
                self.mode.setCurrentIndex(0)
                self.list.setCurrentItem(item)
                break

    def import_latent(self):
        """从文件选择器载入一个 latent.npy 进入重新解码流程。"""
        if not self.ensure_idle():
            return
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, '选择潜变量 latent.npy', str(self._songs_dir()), 'NumPy (*.npy)'
        )
        if path:
            self._decode(path)

    def decode_current(self):
        """对当前选中作品的 latent.npy 重新解码。"""
        if self.current and self.ensure_idle() and self.decode_btn.isEnabled():
            self._decode(str(Path(self.current['dir']) / 'latent.npy'))

    def _decode(self, path):
        """弹出解码设置对话框，确认后提交重新解码任务。"""
        from ..widgets.decode_dialog import DecodeDialog

        dlg = DecodeDialog(path, self)
        if dlg.exec() != dlg.Accepted or not self.ensure_idle():
            return
        self.progress.show()
        self.progress.start('重新解码音频…')
        runner.submit(
            '重新解码',
            engine.run_decode,
            dlg.values(),
            on_done=self._done,
            on_error=self._error,
            on_progress=self.progress.update_info,
            on_event=self._event,
        )

    def export_score(self):
        """导出当前作品的乐谱（图片 / PDF / 钢琴可视化）。"""
        if self.current:
            from ..widgets.render_export import show_render_export

            show_render_export(self, source_dir=self.current['dir'], abc=self.current.get('abc'))

    def to_create(self):
        """把当前作品参数载入创作页。"""
        if self.current:
            self.main.open_in_create(self.current['meta'])

    def to_editor(self):
        """把当前作品载入乐谱编辑页。"""
        if self.current:
            self.main.open_in_editor(self.current)

    def to_mp3(self):
        """把当前作品音频导出为 MP3，完成后弹出结果提示。"""
        if self.current:
            export_mp3(
                self,
                self.current['audio'],
                lambda ok, msg: self.toast(
                    f'已导出 {Path(msg).name}' if ok else f'导出失败: {msg[-200:]}',
                    'ok' if ok else 'error',
                ),
            )

    def delete(self):
        """删除当前作品文件夹：先安全校验，再停用占用播放器并重试删除。"""
        if not self.current or not self.ensure_idle():
            return

        # 仅允许删除作品目录内的文件夹，防止误删外部路径
        folder = Path(self.current['dir'])
        if folder.is_symlink() or folder.resolve().parent != self._songs_dir().resolve():
            self.warn('只能删除当前作品目录内的作品文件夹。')
            return
        if QMessageBox.question(self, '删除作品', f'确定要永久删除这个作品文件夹吗？\n{folder}') != QMessageBox.Yes:
            return

        # 停用所有正在播放该目录下文件的播放器，避免文件被占用
        from ..widgets import player

        for p in player._players:
            if not p.path:
                continue
            if Path(p.path).parent != folder:
                continue
            p.set_source('')

        # 重试删除（最多 10 次）：文件被占用时等待 0.2 秒后重试
        err = None
        for _ in range(10):
            try:
                shutil.rmtree(folder)
                err = None
                break
            except FileNotFoundError:
                err = None
                break
            except OSError as exc:
                err = exc
                QApplication.processEvents()
                time.sleep(0.2)

        # 删除失败则提示并保留原选择
        if err is not None:
            self.warn(f'删除失败（文件可能被其他程序占用）：{err}')
            return

        # 删除成功后清空选择、停用播放器并刷新列表
        self.current = None
        self.player.set_source('')
        self.refresh()
        self.toast('已删除', 'ok')
