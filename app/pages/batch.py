"""批量生成页面：批量翻唱改词 / 批量创作。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemDelegate, QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .. import batch_songs as bs, lyrics_ai
from ..audio_utils import AUDIO_EXTS, AUDIO_FILTER
from ..engine import engine
from ..llm_client import settings_problem
from ..lyrics_utils import LANGUAGES
from ..paths import open_in_explorer
from ..settings import settings
from ..tasks import runner
from ..theme import C
from ..widgets.ai_lyrics import QUICK_INSTRUCTIONS, QUICK_REQUIREMENTS, AiLyricsDialog
from ..widgets.common import Card, FlowLayout, Segmented, button, form_row, label
from ..widgets.editors import LyricsHighlighter
from ..widgets.progress import StageProgress
from ..widgets.song_forms import COT_OPTIONS, AdvancedSampling, GenerationSettings, StyleCard
from .base import BasePage

SOURCE_OPTIONS = [
    ("批量翻唱改词", "cover", "输入多首音频，按要求改写原歌词后翻唱"),
    ("批量创作", "create", "输入主题或歌词，AI 写词后创作新歌"),
]
CREATE_INPUT_OPTIONS = [
    ("每行一个主题", "themes", "每行生成一首；可用 | 追加语言和风格"),
    ("导入歌词文件", "files", "每个 txt / lrc / md 文件一首"),
    ("一个主题写多首", "theme_n", "同一主题写出多份不同的歌词"),
]
LENGTH_OPTIONS = [(label_text, key, plan) for key, label_text, plan in lyrics_ai.LENGTHS]
LYRICS_ACTION_OPTIONS = [
    ("AI 润色", "polish", "按写词要求修改，并补全段落标签"),
    ("直接使用", "use", "不调用大模型，原样生成（没有段落标签时整体标为 [Verse]）"),
]
STYLE_OPTIONS = [
    ("统一风格", "fixed", "所有歌曲使用下面填写的风格"),
    ("AI 生成风格", "llm", "大模型根据要求、主题、歌词和原曲速度/调性为每首歌写风格"),
    ("每首单独填写", "per_song", "在表格“风格”列或主题行 | 后填写；留空的使用下面的风格"),
]
LANGUAGE_OPTIONS = [
    ("不指定", "auto", "翻唱：按改词要求；创作：由 AI 按主题决定"),
    ("统一指定", "fixed", "所有歌曲使用右侧选择的语言"),
    ("每首单独指定", "per_item", "在表格“语言”列或主题行 | 后填写"),
]
MELODY_OPTIONS = [
    ("按原旋律（仅旋律）", "melody", "官方推荐的翻唱方式：沿用原曲旋律，由 YuE2 重新配和声"),
    ("原旋律 + 和弦", "full", "同时沿用扒出的和弦"),
    ("重新作曲", "none", "不扒谱、不用原旋律，只按新歌词和风格创作"),
]
REVIEW_OPTIONS = [("全自动", "auto", "写好歌词直接生成"), ("写词后暂停审核", "review", "确认歌词后才生成")]
REVIEW_WAIT_OPTIONS = [
    ("等这首审核完再继续", "block", "严格一首做完再做下一首"),
    ("先处理后面的歌", "continue", "等待审核期间，先完成后面歌曲的扒谱、识别和写词"),
]
ORDER_OPTIONS = [
    ("按步骤批量", "stage", "所有歌做完一步再做下一步；模型切换少，总耗时短"),
    ("逐首完成", "song", "一首歌做完全部步骤再做下一首；能更早听到第一首成品"),
]
ERROR_OPTIONS = [
    ("跳过出错的歌", "skip", "出错的歌停在出错的步骤，其他歌照常进行，之后可重试"),
    ("停止整批", "stop", "任何一首出错就停止，已完成的步骤保留"),
]
STRUCTURE_OPTIONS = [
    ("不限制", "off", "大模型可以自由调整段落和行数"),
    ("保持段落和行数", "lines", "推荐；翻译成其他语言时也适用"),
    ("严格：每行字数也一致", "units", "同语言改写时最贴合原旋律"),
]
LYRIC_LANGUAGES = ["中文", "粤语", "英文", "日语", "韩语"]
COL_SCORE, COL_LYRICS, COL_REWRITE, COL_AI_STYLE, COL_REVIEW, COL_GENERATE, COL_LANGUAGE, COL_STYLE = range(2, 10)
MARKS = {bs.PENDING: "·", bs.RUNNING: "● 进行中", bs.DONE: "✓", bs.FAILED: "✕ 失败", bs.SKIPPED: "—",
         bs.WAITING: "⏸ 待审核"}
MAX_FOLDER_FILES = 5000


def _name(options, value):
    return {v: t for t, v, _ in options}.get(value, str(value))


def _colors():
    return {bs.PENDING: C["faint"], bs.RUNNING: C["accent"], bs.DONE: C["green"], bs.FAILED: C["red"],
            bs.SKIPPED: C["faint"], bs.WAITING: C["amber"]}


def _box(*widgets):
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for widget in widgets:
        if isinstance(widget, QHBoxLayout):
            layout.addLayout(widget)
        else:
            layout.addWidget(widget)
    return holder


class FileList(QListWidget):
    """可拖入多个文件或整个文件夹的列表（按扩展名过滤、按路径去重）。"""
    changed = Signal()

    def __init__(self, extensions):
        super().__init__()
        self.extensions = {e.lower() for e in extensions}
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setMinimumHeight(150)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            self.add_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
            event.acceptProposedAction()

    def add_paths(self, paths):
        existing, added = set(self.paths()), 0
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                candidates = []
                for candidate in path.rglob("*"):
                    if candidate.suffix.lower() in self.extensions:
                        candidates.append(candidate)
                        if len(candidates) >= MAX_FOLDER_FILES:
                            break
                candidates.sort()
            else:
                candidates = [path]
            for candidate in candidates:
                try:
                    resolved = str(candidate.resolve())
                    ok = candidate.is_file() and candidate.suffix.lower() in self.extensions
                except OSError:
                    continue
                if ok and resolved not in existing:
                    item = QListWidgetItem(candidate.name)
                    item.setData(Qt.UserRole, resolved)
                    item.setToolTip(resolved)
                    self.addItem(item)
                    existing.add(resolved)
                    added += 1
        self._renumber()
        return added

    def paths(self):
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]

    def remove_selected(self):
        for item in self.selectedItems():
            self.takeItem(self.row(item))
        self._renumber()

    def clear_all(self):
        self.clear()
        self._renumber()

    def _renumber(self):
        for index in range(self.count()):
            item = self.item(index)
            item.setText(f"{index + 1:03d}  {Path(item.data(Qt.UserRole)).name}")
        self.changed.emit()


def file_buttons(page, files, caption, file_filter):
    row = QHBoxLayout()
    count = label("0 个", "Hint")

    def add_files():
        paths, _ = QFileDialog.getOpenFileNames(page, f"选择{caption}", "", file_filter)
        files.add_paths(paths)

    def add_folder():
        path = QFileDialog.getExistingDirectory(page, f"选择包含{caption}的文件夹")
        if path and not files.add_paths([path]):
            page.toast(f"这个文件夹里没有找到支持的{caption}", "warn")

    row.addWidget(button("添加文件", callback=add_files))
    row.addWidget(button("添加文件夹", callback=add_folder))
    row.addWidget(button("移除所选", "Ghost", callback=files.remove_selected))
    row.addWidget(button("清空", "Ghost", callback=files.clear_all))
    row.addStretch(1)
    row.addWidget(count)
    files.changed.connect(lambda: count.setText(f"{files.count()} 个"))
    return row


class BatchPage(BasePage):
    title = "🔁 批量生成"
    subtitle = ("批量翻唱改词：多首音频 → 扒谱 → 识别 → AI 改词 → 翻唱；批量创作：主题或歌词 → AI 写词 → 创作。"
                "进度自动保存，可随时停止和继续。")

    def __init__(self, main):
        super().__init__(main)
        self.state, self.current_dir, self.running_dir, self._filling = None, None, None, False
        self._pending_cells = []       # [(批次目录, 歌曲编号, {字段: 值})]，表格编辑提交后等待保存
        self._flushing_cells = False
        self._edit_version = self._flushed_version = 0   # 表格编辑的序号 / 已尝试保存到的序号
        self.source = Segmented(SOURCE_OPTIONS, "cover")
        self.header.right.addWidget(self.source)

        # ① 翻唱：歌曲列表 -------------------------------------------------------------
        self.songs_card = Card("歌曲列表", "拖入多个音频文件或整个文件夹。同一首歌只会添加一次。", step=1)
        self.audio_files = FileList(AUDIO_EXTS)
        self.songs_card.body.addWidget(self.audio_files)
        self.songs_card.body.addLayout(file_buttons(self, self.audio_files, "音频", AUDIO_FILTER))
        self.asr_language = QComboBox()
        for text, value in LANGUAGES:
            self.asr_language.addItem(text, value)
        self.songs_card.body.addWidget(form_row("识别语言", self.asr_language, "识别原歌词用；所有歌曲语言相同时指定会更准"))

        # ① 创作：来源 -----------------------------------------------------------------
        self.create_card = Card("创作来源", step=1)
        self.create_input = Segmented(CREATE_INPUT_OPTIONS, "themes")
        self.create_card.body.addWidget(self.create_input)
        self.themes = QPlainTextEdit()
        self.themes.setPlaceholderText("每行一个主题，例如：\n毕业季的离别\n夏天夜晚的海边 | 英文\n"
                                       "写给妈妈的歌 | 中文 | warm folk, female vocal")
        self.themes.setMinimumHeight(150)
        self.themes_box = _box(self.themes, label("可选：用 | 追加语言和风格，分别在“每首单独指定语言”"
                                                  "“每首单独填写风格”时生效。", "Hint", wrap=True))
        self.lyric_files = FileList(bs.LYRIC_EXTS)
        self.lyrics_action = Segmented(LYRICS_ACTION_OPTIONS, "polish")
        self.files_box = _box(self.lyric_files, file_buttons(self, self.lyric_files, "歌词文件",
                                                            "歌词 (*.txt *.lrc *.md);;所有文件 (*)"),
                              form_row("导入后", self.lyrics_action, "lrc 的时间标签会自动去掉"))
        self.theme = QLineEdit()
        self.theme.setPlaceholderText("例如：毕业季的离别")
        self.theme_count = QSpinBox()
        self.theme_count.setRange(1, bs.MAX_THEME_VARIANTS)
        self.theme_count.setValue(5)
        self.theme_count.setSuffix(" 首")
        self.theme_n_box = _box(form_row("主题", self.theme),
                                form_row("写几首", self.theme_count,
                                         "每首歌词都不同；每份歌词还可按“每首生成数量”生成多个版本"))
        self.length = Segmented(LENGTH_OPTIONS, lyrics_ai.DEFAULT_LENGTH)
        self.length_row = form_row("歌词篇幅", self.length,
                                   "按篇幅安排段落和行数；歌词越长歌曲越长，实际时长还会随旋律和间奏浮动")
        for widget in (self.themes_box, self.files_box, self.theme_n_box, self.length_row):
            self.create_card.body.addWidget(widget)

        # ② 改词 / 写词要求 --------------------------------------------------------------
        self.rewrite_card = Card("改词要求", step=2)
        self.instruction = QPlainTextEdit()
        self.instruction.setFixedHeight(80)
        self.rewrite_card.body.addWidget(self.instruction)
        # 改词（翻唱、润色导入歌词）点选直接替换要求；按主题写词时点选追加一条要求。
        self.instruction_chips = self._chips(QUICK_INSTRUCTIONS, self.instruction.setPlainText)
        self.requirement_chips = self._chips(QUICK_REQUIREMENTS, self._add_requirement)
        self.rewrite_card.body.addWidget(self.instruction_chips)
        self.rewrite_card.body.addWidget(self.requirement_chips)
        language_row = QHBoxLayout()
        self.language_mode = Segmented(LANGUAGE_OPTIONS, "auto")
        self.language = QComboBox()
        self.language.setEditable(True)
        self.language.addItems(LYRIC_LANGUAGES)
        language_row.addWidget(self.language_mode, 1)
        language_row.addWidget(self.language)
        self.rewrite_card.body.addWidget(form_row("歌词语言", language_row))
        self.structure = Segmented(STRUCTURE_OPTIONS, "lines")
        self.retries = QSpinBox()
        self.retries.setRange(0, 3)
        self.retries.setValue(1)
        self.retries.setSuffix(" 次")
        self.structure_box = _box(form_row("结构约束", self.structure, "按原旋律翻唱时，段落和行数一致才能贴合原曲"),
                                  form_row("结构不符时自动重试", self.retries,
                                           "带着问题清单让大模型重改，保留问题最少的一版"))
        self.rewrite_card.body.addWidget(self.structure_box)

        # ③ 风格 ------------------------------------------------------------------------
        self.style_card = StyleCard("风格", step=3, expanded=False,
                                    hint="“统一风格”用于所有歌曲；“每首单独填写”时作为留空歌曲的默认值；"
                                         "“AI 生成风格”时可以留空。")
        self.style_mode = Segmented(STYLE_OPTIONS, "fixed")
        self.style_card.body.insertWidget(0, self.style_mode)

        # ④ 流程 ------------------------------------------------------------------------
        flow_card = Card("流程选项", step=4)
        self.melody = Segmented(MELODY_OPTIONS, "melody")
        self.cot = Segmented(COT_OPTIONS, "full")
        self.review = Segmented(REVIEW_OPTIONS, "auto")
        self.order = Segmented(ORDER_OPTIONS, "stage")
        self.review_wait = Segmented(REVIEW_WAIT_OPTIONS, "block")
        self.on_error = Segmented(ERROR_OPTIONS, "skip")
        self.melody_row = form_row("旋律", self.melody)
        self.cot_row = form_row("乐谱规划模式", self.cot)
        self.review_wait_row = form_row("审核期间", self.review_wait)
        for widget in (self.melody_row, self.cot_row, form_row("审核", self.review), form_row("执行顺序", self.order),
                       self.review_wait_row, form_row("出错时", self.on_error)):
            flow_card.body.addWidget(widget)

        # ⑤ 生成 ------------------------------------------------------------------------
        self.gen = GenerationSettings("生成设置", step=5, show_count=True)
        self.gen.cot.parentWidget().setVisible(False)      # 由“旋律 / 乐谱规划模式”决定
        self.gen.title.setVisible(False)                   # 作品名自动使用歌名或主题
        self.gen.count.setSuffix(" 个/首")
        self.gen.seed.auto.setText("每首随机")
        self.gen.seed.auto.setToolTip("每首歌使用不同的随机种子（记录在批次中，继续批次时不变）")
        self.gen.seed.setValue(831001)
        self.advanced = AdvancedSampling()
        self.gen.body.addWidget(self.advanced)

        for segmented in (self.source, self.create_input, self.lyrics_action, self.review, self.order,
                          self.language_mode):
            segmented.changed.connect(lambda _=None: self._update_visibility())

        # 右栏 --------------------------------------------------------------------------
        right = QWidget()
        column = QVBoxLayout(right)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)
        pick = QHBoxLayout()
        self.batches = QComboBox()
        self.batches.currentIndexChanged.connect(self._batch_selected)
        pick.addWidget(label("批次"))
        pick.addWidget(self.batches, 1)
        pick.addWidget(button("🔄", "Ghost", "刷新批次列表", self.refresh_batches))
        pick.addWidget(button("📂", "Ghost", "打开批次文件夹", self._open_batch_folder))
        column.addLayout(pick)
        self.summary = label("还没有批次。在左侧填写后点击“开始新批次”。", "Hint", wrap=True)
        column.addWidget(self.summary)
        self.progress = StageProgress()
        column.addWidget(self.progress)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(["#", "名称", "扒谱", "识别/导入", "改词/写词",
                                              "AI 风格", "审核", "生成", "语言", "风格"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_STYLE, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._cell_changed)
        self.table.doubleClicked.connect(lambda _: self.open_selected())
        column.addWidget(self.table, 1)
        tools = QHBoxLayout()
        self.review_btn = button("📝 审核歌词", "Primary", callback=self.open_review)
        self.retry_btn = button("↻ 重试失败项", callback=self.retry)
        self.skip_btn = button("⏭ 跳过所选", "Ghost", "所选歌曲不再继续（例如一直失败的歌）", self.skip_selected)
        self.open_btn = button("🎵 打开所选作品", "Ghost", callback=self.open_selected)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        for widget in (self.review_btn, self.retry_btn, self.skip_btn, self.open_btn):
            tools.addWidget(widget)
        tools.addStretch(1)
        column.addLayout(tools)

        self.start_btn = self.run_button("▶ 开始新批次", self.start)
        self.create_btn = self.run_button("📋 只建批次", self.create_only, primary=False,
                                          tooltip="先建立批次不运行：可以在右侧表格逐首填写语言、风格，再点“继续所选批次”")
        self.resume_btn = self.run_button("⏯ 继续所选批次", self.resume, primary=False)
        bar = self.action_bar(self.stop_button(), None, self.create_btn, self.resume_btn, self.start_btn)
        self.two_columns([self.songs_card, self.create_card, self.rewrite_card, self.style_card, flow_card, self.gen],
                         right, bar)
        self._load_draft()
        self._update_visibility()
        self.refresh_batches()

    @staticmethod
    def _chips(texts, callback):
        holder = QWidget()
        flow = FlowLayout(holder, spacing=6)
        for text in texts:
            chip = button(text, "Chip")
            chip.clicked.connect(lambda _=False, t=text: callback(t))
            flow.addWidget(chip)
        return holder

    def _add_requirement(self, text):
        current = self.instruction.toPlainText().strip()
        if text not in current:
            self.instruction.setPlainText(f"{current}，{text}" if current else text)

    # 显示切换 ------------------------------------------------------------------------
    def _update_visibility(self):
        cover = self.source.value() == "cover"
        mode = self.create_input.value()
        writing = not cover and mode != "files"        # 按主题写新歌词
        polishing = not cover and mode == "files"
        self.songs_card.setVisible(cover)
        self.create_card.setVisible(not cover)
        self.themes_box.setVisible(mode == "themes")
        self.files_box.setVisible(polishing)
        self.theme_n_box.setVisible(mode == "theme_n")
        self.length_row.setVisible(writing)
        self.structure_box.setVisible(cover or (polishing and self.lyrics_action.value() == "polish"))
        self.instruction_chips.setVisible(not writing)
        self.requirement_chips.setVisible(writing)
        if cover:
            title, hint = "改词要求（必填）", "例如：改成积极向上的励志歌词，保留原歌的主要意象"
        elif polishing:
            title, hint = "润色要求（选择“AI 润色”时必填）", "例如：修正错别字和语病，补全段落标签，不改变原意"
        else:
            title, hint = "写词要求（可选，对每首生效）", "例如：副歌押韵好记，口语化，有画面感"
        self.rewrite_card.title_label.setText(title)
        self.instruction.setPlaceholderText(hint)
        self.melody_row.setVisible(cover)
        self.cot_row.setVisible(not cover)
        self.review_wait_row.setVisible(self.review.value() == "review" and self.order.value() == "song")
        self.language.setEnabled(self.language_mode.value() == "fixed")

    # 输入 ------------------------------------------------------------------------
    def entries(self):
        if self.source.value() == "cover":
            return [{"audio": path} for path in self.audio_files.paths()]
        mode = self.create_input.value()
        if mode == "themes":
            return bs.parse_themes(self.themes.toPlainText())
        if mode == "files":
            return [{"lyrics_file": path} for path in self.lyric_files.paths()]
        return bs.expand_theme(self.theme.text(), self.theme_count.value())

    def options(self):
        abc_sampling, semantic_sampling = self.advanced.values()
        # 随机种子在建批次时逐首生成，这里不推进界面上的种子。
        values = self.gen.values(advance_seed=False)
        return {"source": self.source.value(), "create_input": self.create_input.value(),
                "lyrics_action": self.lyrics_action.value(), "melody": self.melody.value(), "cot": self.cot.value(),
                "asr_language": self.asr_language.currentData(),
                "instruction": self.instruction.toPlainText().strip(), "length": self.length.value(),
                "language_mode": self.language_mode.value(), "language": self.language.currentText(),
                "structure": self.structure.value(), "structure_retries": self.retries.value(),
                "style_mode": self.style_mode.value(), "style": self.style_card.text(),
                "review": self.review.value() == "review", "review_wait": self.review_wait.value(),
                "order": self.order.value(), "on_error": self.on_error.value(),
                "generation": {"count": values["count"], "seed": values["seed"], "seed_auto": values["seed_auto"],
                               "ode_steps": values["ode_steps"], "cfg_scale": values["cfg_scale"],
                               "decode_options": values["decode_options"], "abc_sampling": abc_sampling,
                               "semantic_sampling": semantic_sampling}}

    # 批次运行 ------------------------------------------------------------------------
    def start(self):
        self._create(run=True)

    def create_only(self):
        self._create(run=False)

    def _create(self, run):
        if not self.ensure_idle():
            return
        if not self._commit_table_edits():
            return
        try:
            options = bs.normalize_options(self.options())
            entries = self.entries()
        except ValueError as exc:
            self.warn(str(exc))
            return
        if not entries:
            self.warn("请先添加歌曲。" if options["source"] == "cover" else "请先输入主题或导入歌词。")
            return
        calls = bs.estimate_llm_calls(options, len(entries))
        problem = settings_problem(settings)
        if run and calls and problem:
            self.warn(problem)
            return
        if not run:
            try:
                folder = bs.create_batch(entries, options, require_styles=False)
            except (OSError, ValueError) as exc:
                self.warn(str(exc))
                return
            self.save_draft()
            self.refresh_batches(select=folder)
            self.toast("批次已建立：可在右侧表格逐首填写，然后点“继续所选批次”", "ok")
            return
        count = options["generation"]["count"]
        ignored = []
        if any(entry.get("language") for entry in entries) and options["language_mode"] != "per_item":
            ignored.append("语言")
        if any(entry.get("style") for entry in entries) and options["style_mode"] != "per_song":
            ignored.append("风格")
        note = (f"注意：主题行 | 后面写的{'和'.join(ignored)}不会生效（需要选“每首单独指定语言 / 每首单独填写风格”）。\n\n"
                if ignored else "")
        text = (note + f"{_name(SOURCE_OPTIONS, options['source'])}：共 {len(entries)} 首，每首生成 {count} 个版本，"
                f"合计 {len(entries) * count} 首新歌。\n大模型约调用 {calls} 次（结构不符或网络出错重试时会更多）。\n\n"
                f"顺序：{_name(ORDER_OPTIONS, options['order'])} · "
                f"{'写词后暂停审核' if options['review'] else '全自动'} · "
                f"出错时：{_name(ERROR_OPTIONS, options['on_error'])}\n\n"
                "批次运行期间其他页面不能同时运行任务。开始吗？")
        if QMessageBox.question(self, "开始批量生成", text) != QMessageBox.Yes:
            return
        try:
            folder = bs.create_batch(entries, options)
        except (OSError, ValueError) as exc:
            self.warn(str(exc))
            return
        self.save_draft()
        self.refresh_batches(select=folder)
        self._submit(folder)

    def resume(self):
        if not self.current_dir or not self.ensure_idle():
            return
        if not self._commit_table_edits():
            return          # 刚填写的语言/风格没保存成功，不能按旧值开跑
        self._submit(self.current_dir)

    def _submit(self, folder):
        folder = Path(folder)
        self.progress.start("批量任务启动…")
        task = runner.submit("批量生成", engine.run_batch_songs, {"dir": str(folder)},
                             on_done=self._done, on_error=self._error, on_progress=self.progress.update_info,
                             on_event=self._event)
        if task is None:
            self.progress.reset()
            self.toast(f"请等待当前任务完成：{runner.title}", "warn")
            return
        self.running_dir = folder
        self._update_buttons()

    def _event(self, kind, payload):
        if kind == "batch":
            folder = Path(payload["dir"])
            if self.current_dir and folder == self.current_dir:
                self.show_state(folder, payload["state"])
        elif kind in ("job", "item"):
            self.main.library_changed()

    def _done(self, summary):
        self.running_dir = None
        self.refresh_batches()
        numbers = summary["counts"]
        if summary["status"] == "paused":
            self.progress.finish(True, f"已暂停：{numbers.get('waiting', 0)} 首歌词等待审核")
            self.toast("歌词已写好，请审核后继续", "ok")
            if self.current_dir and Path(summary["dir"]) == self.current_dir:
                self.open_review()
            return
        complete = summary["status"] == "complete"
        self.progress.finish(complete, f"{bs.BATCH_STATUS_NAMES.get(summary['status'], summary['status'])} · "
                                       f"完成 {numbers.get('done', 0)} 首"
                                       + (f" · 失败 {numbers['failed']} 首" if numbers.get("failed") else ""))
        self.toast("🎉 批量生成完成" if complete else "批次结束，部分歌曲没有完成", "ok" if complete else "warn")

    def _error(self, message, trace):
        self.running_dir = None
        self.refresh_batches()
        self.progress.finish(False, message.splitlines()[0][:160])
        if message != "已取消" and not message.startswith("已停止"):
            self.main.show_error("批量生成出错", message, trace)

    # 批次显示 ------------------------------------------------------------------------
    def on_show(self):
        if not runner.busy:
            self.refresh_batches()

    def _open_batch_folder(self):
        if self.current_dir:
            open_in_explorer(self.current_dir)

    def refresh_batches(self, select=None):
        if not self._commit_table_edits():
            return
        selected = str(select or self.current_dir or "")
        self.batches.blockSignals(True)
        self.batches.clear()
        for folder, state in bs.list_batches():
            self.batches.addItem(self._batch_text(folder, state), str(folder))
        index = self.batches.findData(selected)
        self.batches.setCurrentIndex(index if index >= 0 else 0)
        self.batches.blockSignals(False)
        self._batch_selected()

    @staticmethod
    def _batch_text(folder, state):
        return f"{Path(folder).name} · {bs.display_status(folder, state)} · {len(state['items'])} 首"

    def _batch_selected(self):
        if not self._commit_table_edits():
            # 选择信号发出时下拉框已切换；保存失败则回到原批次，保留表格中的输入。
            self.batches.blockSignals(True)
            self.batches.setCurrentIndex(self.batches.findData(str(self.current_dir)))
            self.batches.blockSignals(False)
            return
        folder = self.batches.currentData()
        if not folder:
            self.state, self.current_dir = None, None
            self.table.setRowCount(0)
            self.summary.setText("还没有批次。在左侧填写后点击“开始新批次”。")
            self._update_buttons()
            return
        self.current_dir = Path(folder)
        self.reload()

    def reload(self):
        if not self.current_dir:
            return
        if self._pending_cells or self.table.state() == QAbstractItemView.EditingState:
            return          # 旧字段的延迟刷新不能覆盖下一格正在输入的文字，失败编辑也要保留供重试。
        if runner.busy and self.running_dir == self.current_dir and self.state is not None:
            return          # 运行中的批次以工作线程发来的状态为准，不去读正在写入的文件
        try:
            self.show_state(self.current_dir, bs.read_batch(self.current_dir))
        except (OSError, ValueError) as exc:
            self.state = None
            self.table.setRowCount(0)
            self.summary.setText(f"⚠ 无法读取批次：{exc}")
            self._update_buttons()

    def show_state(self, folder, state):
        self.current_dir, self.state = Path(folder), state
        options = state["options"]
        cover = options["source"] == "cover"
        self.table.setHorizontalHeaderLabels(["#", "名称", "扒谱", "识别" if cover else "导入",
                                              "改词" if cover else "写词", "AI 风格", "审核", "生成", "语言", "风格"])
        colors = _colors()
        busy = runner.busy
        self._filling = True
        self.table.setRowCount(len(state["items"]))
        for row, item in enumerate(state["items"]):
            steps = item["steps"]
            texts = [item["id"], item["name"]]
            for step in bs.STEPS:
                mark = MARKS.get(steps[step]["status"], steps[step]["status"])
                if steps[step].get("warning"):
                    mark += " ⚠"
                texts.append(mark)
            texts += [item.get("language") or "", item.get("style") or ""]
            for col, text in enumerate(texts):
                cell = QTableWidgetItem(text)
                flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
                if not busy and not item.get("skipped") and (
                        (col == COL_LANGUAGE and steps["rewrite"]["status"] not in bs.FINISHED)
                        or (col == COL_STYLE and not steps["generate"].get("job_dir"))):
                    flags |= Qt.ItemIsEditable
                cell.setFlags(flags)
                if COL_SCORE <= col <= COL_GENERATE:
                    record = steps[bs.STEPS[col - COL_SCORE]]
                    cell.setForeground(QColor(colors.get(record["status"], C["text"])))
                    tip = record.get("error") or record.get("warning") or ""
                    if tip:
                        cell.setToolTip(tip)
                if col == 1:
                    cell.setToolTip(item.get("theme") or item.get("audio") or item.get("lyrics_file") or "")
                self.table.setItem(row, col, cell)
        self.table.setColumnHidden(COL_SCORE, not (cover and options["melody"] != "none"))
        self.table.setColumnHidden(COL_LYRICS, not (cover or options["create_input"] == "files"))
        self.table.setColumnHidden(COL_AI_STYLE, options["style_mode"] != "llm")
        self.table.setColumnHidden(COL_REVIEW, not options["review"])
        self.table.setColumnHidden(COL_LANGUAGE, options["language_mode"] != "per_item")
        self.table.setColumnHidden(COL_STYLE, options["style_mode"] != "per_song")
        self._filling = False
        numbers = bs.counts(state)
        source = _name(SOURCE_OPTIONS, options["source"]) + (
            "" if cover else " · " + _name(CREATE_INPUT_OPTIONS, options["create_input"]))
        if not cover and options["create_input"] != "files":
            source += " · " + lyrics_ai.length_label(options["length"])
        mode = _name(MELODY_OPTIONS, options["melody"]) if cover else _name(COT_OPTIONS, options["cot"])
        self.summary.setText(
            f"状态：{bs.display_status(folder, state)} · {source} · 共 {len(state['items'])} 首 · "
            f"完成 {numbers.get('done', 0)} · 待审核 {numbers.get('waiting', 0)} · 失败 {numbers.get('failed', 0)}"
            + (f" · 跳过 {numbers['skipped']}" if numbers.get("skipped") else "") + "\n"
            f"要求：{options['instruction'][:80] or '（无）'}\n"
            f"设置：{_name(ORDER_OPTIONS, options['order'])} · {mode} · "
            f"{'审核' if options['review'] else '全自动'} · 每首 {options['generation']['count']} 个 · "
            f"{_name(STYLE_OPTIONS, options['style_mode'])} · 语言{_name(LANGUAGE_OPTIONS, options['language_mode'])}"
            + (f"\n上次错误：{state['error'][:200]}" if state.get("error") else ""))
        index = self.batches.findData(str(folder))
        if index >= 0:
            self.batches.setItemText(index, self._batch_text(folder, state))
        self._update_buttons()

    def _update_buttons(self):
        busy, state = runner.busy, self.state
        numbers = bs.counts(state) if state else {}
        self.review_btn.setText(f"📝 审核歌词（{numbers.get('waiting', 0)}）")
        self.review_btn.setEnabled(bool(numbers.get("waiting")) and not busy)
        self.retry_btn.setEnabled(bool(numbers.get("failed")) and not busy)
        self.resume_btn.setEnabled(bool(state) and not busy and bs.can_continue(state))
        selected = self._selected_item()
        self.skip_btn.setEnabled(not busy and selected is not None and bs.item_state(selected) not in ("done", "skipped"))
        self.open_btn.setEnabled(bool(state))
        self.batches.setEnabled(not busy)

    def set_busy(self, busy):
        super().set_busy(busy)
        if not busy and self.state is not None:
            QTimer.singleShot(0, self.reload)      # 重新计算表格“语言/风格”列是否可编辑
        self._update_buttons()

    def _cell_changed(self, cell):
        if self._filling or not self.state or cell.column() not in (COL_LANGUAGE, COL_STYLE):
            return
        row, column, text = cell.row(), cell.column(), cell.text()
        item_id = self.state["items"][row]["id"]
        field = {"language": text} if column == COL_LANGUAGE else {"style": text}
        # 保存目标在提交这一刻就绑定批次目录：之后即使切换到别的批次（歌曲编号同样从 001 开始），也不会写错。
        self._pending_cells = [(folder, saved_id, values) for folder, saved_id, values in self._pending_cells
                               if (folder, saved_id, set(values)) != (self.current_dir, item_id, set(field))]
        self._pending_cells.append((self.current_dir, item_id, field))
        self._edit_version += 1
        version = self._edit_version
        # 不在 itemChanged 里重建表格（会删除正在发信号的单元格），放到事件循环里处理。
        QTimer.singleShot(0, lambda: self._flush_queued(version))

    def _flush_queued(self, version):
        # 继续、切换、关闭等操作可能已经同步保存（或尝试保存并提示）过这次编辑，不再重复保存和弹窗。
        if version > self._flushed_version:
            self._flush_cells()

    def _commit_editor(self):
        """把表格里正在编辑、还没提交的文字写回单元格（会触发 itemChanged 进入待保存列表）。"""
        if self.table.state() != QAbstractItemView.EditingState:
            return
        for editor in self.table.viewport().findChildren(QLineEdit):
            if editor.isVisible():
                self.table.commitData(editor)
                self.table.closeEditor(editor, QAbstractItemDelegate.NoHint)

    def _flush_cells(self):
        """立刻保存所有待保存的单元格；返回是否全部成功。"""
        if self._flushing_cells:
            return False    # 错误提示框会运行嵌套事件循环，不能重复进入保存并清空待重试的数据。
        self._flushed_version = self._edit_version
        pending, self._pending_cells = self._pending_cells, []
        retryable, errors, dropped = [], [], []
        self._flushing_cells = True
        try:
            for folder, item_id, field in pending:
                try:
                    bs.set_item_fields(folder, item_id, **field)
                except (FileNotFoundError, ValueError, KeyError) as exc:
                    # 目标不存在、批次格式无效或记录不再接受编辑：提示后放弃，并中止本次操作。
                    dropped.append(f"{Path(folder).name} · {item_id}：{exc}")
                except (RuntimeError, OSError) as exc:
                    # Windows 文件共享冲突、权限或磁盘空间问题也可能恢复，不能按 OSError 直接丢弃输入。
                    retryable.append((folder, item_id, field))
                    errors.append(str(exc))
            self._pending_cells = retryable + self._pending_cells
            if errors:
                self.warn(errors[0] + "\n\n未保存的编辑已保留。解决占用或文件读写问题后，"
                          "点击“继续”、刷新或再次关闭窗口即可重试。")
            if dropped:
                self.warn("以下表格编辑无法保存，已放弃：\n" + "\n".join(dropped[:5]))
        finally:
            self._flushing_cells = False
        # 有编辑被放弃时，这一次操作也不继续（否则会悄悄按旧值开跑）；待保存列表已清空，用户看过提示后再点一次即可。
        ok = not self._pending_cells and not dropped
        if pending and not self._pending_cells and not runner.busy:
            QTimer.singleShot(0, self.reload)
        return ok

    def has_unsaved_edits(self):
        return bool(self._pending_cells)

    def discard_pending_edits(self):
        """关闭程序时用户选择放弃无法保存的表格编辑。"""
        self._pending_cells = []

    def _commit_table_edits(self):
        """启动、继续、切换批次之前调用：结束编辑并同步保存，保存失败时不应继续。"""
        self._commit_editor()
        return self._flush_cells()

    # 审核 / 重试 / 打开 ------------------------------------------------------------------
    def open_review(self):
        if not self.current_dir or not self.ensure_idle():
            return
        if not self._commit_table_edits():
            return
        try:
            dialog = BatchReviewDialog(self, self.current_dir)
        except (OSError, ValueError) as exc:
            self.warn(str(exc))
            return
        dialog.exec()
        self.reload()
        if (dialog.changed and self.state and bs.can_continue(self.state)
                and QMessageBox.question(self, "继续批次", "现在继续批次吗？") == QMessageBox.Yes):
            self.resume()

    def retry(self):
        if not self.current_dir or not self.ensure_idle() or not self.state:
            return
        if not self._commit_table_edits():
            return
        new_job = False
        if bs.failed_generate_jobs(self.state):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("重试失败项")
            box.setText("有生成失败的歌曲。要怎样重试？\n\n"
                        "• 继续原任务：已完成的乐谱、tokens 等阶段不重做（推荐）\n"
                        "• 新建任务：原任务被删除、损坏或提示“推理版本不同”时使用，原任务仍保留在作品库")
            resume_button = box.addButton("继续原任务", QMessageBox.AcceptRole)
            new_button = box.addButton("新建任务重新生成", QMessageBox.DestructiveRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() not in (resume_button, new_button):
                return
            new_job = box.clickedButton() is new_button
        try:
            total = bs.retry_failed(self.current_dir, new_generate_job=new_job)
        except (RuntimeError, ValueError, OSError) as exc:
            self.warn(str(exc))
            return
        self.reload()
        if total and QMessageBox.question(self, "重试失败项",
                                          f"已重置 {total} 个失败步骤，现在继续批次吗？") == QMessageBox.Yes:
            self.resume()

    def _selected_item(self):
        rows = {index.row() for index in self.table.selectionModel().selectedRows()} if self.state else set()
        if len(rows) != 1:
            return None
        row = rows.pop()
        return self.state["items"][row] if 0 <= row < len(self.state["items"]) else None

    def skip_selected(self):
        item = self._selected_item()
        if item is None or not self.ensure_idle():
            return
        if not self._commit_table_edits():
            return
        if QMessageBox.question(self, "跳过", f"“{item['name']}”不再继续，确定跳过吗？") != QMessageBox.Yes:
            return
        try:
            bs.skip_item(self.current_dir, item["id"])
        except (RuntimeError, ValueError, KeyError, OSError) as exc:
            self.warn(str(exc))
        self.reload()

    def open_selected(self):
        item = self._selected_item()
        if item is None:
            return
        record = item["steps"]["generate"]
        target = (record.get("results") or [record.get("job_dir")])[0]
        if target and Path(target).is_dir():
            self.main.open_in_library({"dir": target})
        else:
            open_in_explorer(Path(self.current_dir) / "items" / item["folder"])

    # 草稿 ------------------------------------------------------------------------
    def save_draft(self):
        saved = self._commit_table_edits()     # 关闭程序前，刚在表格里填的语言/风格也要落盘
        settings.setdefault("drafts", {})["batch"] = dict(
            self.options(), audio_files=self.audio_files.paths(), lyric_files=self.lyric_files.paths(),
            themes=self.themes.toPlainText(), theme=self.theme.text(), theme_count=self.theme_count.value())
        return saved

    def _load_draft(self):
        draft = (settings.get("drafts") or {}).get("batch") or {}
        if not isinstance(draft, dict) or not draft:
            return
        try:
            self.source.setValue(draft.get("source", "cover"))
            self.create_input.setValue(draft.get("create_input", "themes"))
            self.lyrics_action.setValue(draft.get("lyrics_action", "polish"))
            self.audio_files.add_paths([p for p in draft.get("audio_files", []) if Path(p).is_file()])
            self.lyric_files.add_paths([p for p in draft.get("lyric_files", []) if Path(p).is_file()])
            self.themes.setPlainText(draft.get("themes", ""))
            self.theme.setText(draft.get("theme", ""))
            self.theme_count.setValue(int(draft.get("theme_count", 5)))
            self.length.setValue(draft.get("length", lyrics_ai.DEFAULT_LENGTH))
            index = self.asr_language.findData(draft.get("asr_language")) if draft.get("asr_language") else 0
            self.asr_language.setCurrentIndex(max(0, index))
            self.instruction.setPlainText(draft.get("instruction", ""))
            self.language_mode.setValue(draft.get("language_mode", "auto"))
            self.language.setCurrentText(draft.get("language") or LYRIC_LANGUAGES[0])
            self.structure.setValue(draft.get("structure", "lines"))
            self.retries.setValue(int(draft.get("structure_retries", 1)))
            self.style_mode.setValue(draft.get("style_mode", "fixed"))
            self.style_card.setText(draft.get("style", ""))
            self.melody.setValue(draft.get("melody", "melody"))
            self.cot.setValue(draft.get("cot", "full"))
            self.review.setValue("review" if draft.get("review") else "auto")
            self.review_wait.setValue(draft.get("review_wait", "block"))
            self.order.setValue(draft.get("order", "stage"))
            self.on_error.setValue(draft.get("on_error", "skip"))
            generation = draft.get("generation") or {}
            self.gen.load(dict(generation, cot="full"))
            self.advanced.load(generation.get("abc_sampling"), generation.get("semantic_sampling"))
        except (TypeError, ValueError) as exc:  # 手动改坏的草稿不应让页面无法打开
            print(f"[YuE2 Studio] 批量生成草稿无法载入：{exc}")

    def primary_action(self):
        self.start()


class BatchReviewDialog(QDialog):
    def __init__(self, parent, folder):
        super().__init__(parent)
        self.setWindowTitle("📝 审核歌词")
        self.resize(1200, 760)
        self.folder, self.changed = Path(folder), False
        self.drafts, self.current_id, self.state = {}, None, None

        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._selected)
        splitter.addWidget(self.list)
        self.original = QPlainTextEdit()
        self.original.setReadOnly(True)
        self._hl1 = LyricsHighlighter(self.original.document())
        self.rewritten = QPlainTextEdit()
        self._hl2 = LyricsHighlighter(self.rewritten.document())
        self.rewritten.textChanged.connect(self._check)
        for title, widget in (("原歌词 / 主题（只读）", self.original), ("最终歌词（可直接修改）", self.rewritten)):
            splitter.addWidget(_box(label(title, "CardTitle"), widget))
        splitter.setSizes([220, 480, 500])
        layout.addWidget(splitter, 1)
        self.problems = label("", "Hint", wrap=True)
        layout.addWidget(self.problems)
        self.style = QLineEdit()
        self.style.setPlaceholderText("风格描述")
        self.style_row = form_row("风格", self.style)
        layout.addWidget(self.style_row)
        self.extra = QLineEdit()
        layout.addWidget(self.extra)
        buttons = QHBoxLayout()
        buttons.addWidget(button("✓ 全部通过", callback=self.approve_all))
        buttons.addStretch(1)
        buttons.addWidget(button("✨ 交互式 AI 修改", callback=self.ai_edit))
        buttons.addWidget(button("↻ 标记让 AI 重改", callback=self.mark_rewrite))
        buttons.addWidget(button("跳过这首", "Ghost", callback=self.skip_current))
        buttons.addWidget(button("✓ 通过这首", "Primary", callback=self.approve_current))
        buttons.addWidget(button("关闭", callback=self.accept))
        layout.addLayout(buttons)
        for push in self.findChildren(QPushButton):
            push.setAutoDefault(False)
            push.setDefault(False)
        self.reload()

    def _waiting(self):
        return [item for item in self.state["items"] if item["steps"]["review"]["status"] == bs.WAITING]

    def reload(self):
        self._stash()
        self.state = bs.read_batch(self.folder)
        self.style_row.setVisible(self.state["options"]["style_mode"] != "fixed")
        self.list.blockSignals(True)
        self.list.clear()
        for item in self._waiting():
            warning = "  ⚠" if item["steps"]["rewrite"].get("warning") else ""
            entry = QListWidgetItem(f"{item['id']}  {item['name']}{warning}")
            entry.setData(Qt.UserRole, item["id"])
            self.list.addItem(entry)
        self.list.blockSignals(False)
        self.current_id = None
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self.original.clear()
            self.rewritten.clear()
            self.style.clear()
            self.extra.clear()
            self.problems.setText("✓ 没有待审核的歌词了。")

    def _stash(self):
        if self.current_id:
            self.drafts[self.current_id] = (self.rewritten.toPlainText(), self.style.text(), self.extra.text())

    def _selected(self, current, _previous=None):
        self._stash()
        if current is None:
            return
        item = bs.find_item(self.state, current.data(Qt.UserRole))
        self.current_id = None           # 填充期间不触发结构检查
        original = bs.read_text(self.folder, item, "original.txt")
        if not original.strip():
            language = bs.item_language(self.state, item)
            original = f"（没有原歌词）\n主题：{item.get('theme', '')}" + (f"\n语言：{language}" if language else "")
        self.original.setPlainText(original)
        lyrics, style, extra = self.drafts.get(item["id"], (bs.read_text(self.folder, item, "rewritten.txt"), None, ""))
        if style is None:
            style = (bs.read_text(self.folder, item, "style.txt") if self.state["options"]["style_mode"] == "llm"
                     else bs.style_hint(self.state, item))
        self.rewritten.setPlainText(lyrics)
        self.style.setText(style.strip())
        # 只恢复本轮尚未提交的意见；任务中的 extra_instruction 属于上一轮执行记录。
        self.extra.setText(extra)
        self.extra.setPlaceholderText(
            "“标记让 AI 重改”的修改意见：填写时按意见修改 AI 写的这一版，留空则按主题重新写一版"
            if item.get("theme") else
            "“标记让 AI 重改”的补充要求（可留空）：会和批次的要求一起，基于原歌词重新改写，例如：副歌再短一点")
        self.current_id = item["id"]
        self._check()

    def _check(self):
        if not self.current_id:
            return
        item = bs.find_item(self.state, self.current_id)
        problems = bs.structure_problems(bs.read_text(self.folder, item, "original.txt"),
                                         self.rewritten.toPlainText(), self.state["options"]["structure"])
        self.problems.setText("⚠ " + "；".join(problems[:4]) if problems else "")

    def _style_value(self, text):
        return text if self.state["options"]["style_mode"] != "fixed" else None

    def _run(self, action):
        try:
            action()
        except (RuntimeError, ValueError, KeyError, OSError) as exc:
            QMessageBox.warning(self, "提示", str(exc))
            return False
        self.changed = True
        return True

    def _forget_current(self):
        self.drafts.pop(self.current_id, None)
        self.current_id = None

    def approve_current(self):
        if not self.current_id:
            return
        item_id = self.current_id
        if self._run(lambda: bs.approve(self.folder, item_id, self.rewritten.toPlainText(),
                                        self._style_value(self.style.text()))):
            self._forget_current()
            self.reload()

    def approve_all(self):
        self._stash()
        waiting = self._waiting()
        if not waiting or QMessageBox.question(self, "全部通过", f"通过全部 {len(waiting)} 首歌词吗？") != QMessageBox.Yes:
            return
        for item in waiting:
            lyrics, style, _extra = self.drafts.get(item["id"], (bs.read_text(self.folder, item, "rewritten.txt"), None, ""))
            if not self._run(lambda i=item["id"], text=lyrics, s=style:
                             bs.approve(self.folder, i, text, self._style_value(s) if s is not None else None)):
                break
            self.drafts.pop(item["id"], None)
        self.current_id = None
        self.reload()

    def ai_edit(self):
        if not self.current_id:
            return
        dialog = AiLyricsDialog(self, self.rewritten.toPlainText(), self.style.text(),
                                structure=self.state["options"]["structure"])
        if dialog.exec() and dialog.result_text:
            self.rewritten.setPlainText(dialog.result_text)

    def mark_rewrite(self):
        if not self.current_id:
            return
        item_id = self.current_id
        if self._run(lambda: bs.request_rewrite(self.folder, item_id, self.extra.text())):
            self._forget_current()
            self.reload()

    def skip_current(self):
        if not self.current_id:
            return
        item_id = self.current_id
        if (QMessageBox.question(self, "跳过", "这首歌不再生成，确定跳过吗？") == QMessageBox.Yes
                and self._run(lambda: bs.skip_item(self.folder, item_id))):
            self._forget_current()
            self.reload()
