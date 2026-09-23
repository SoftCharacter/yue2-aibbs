"""AI 歌词对话框：“AI 改歌词”（按要求改写现有歌词）与“AI 写歌词”（按主题创作新歌词，可顺便写风格）。

两个对话框都继承自 _StreamingDialog，通过后台线程流式请求大模型，把增量文本实时刷进
结果框；run id 机制保证旧请求的迟到回复被忽略。改写对话框的确认结果从 result_text 取，
写歌词对话框的确认结果从 result_lyrics / result_style 取。请求内容的组装见 lyrics_ai 模块。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import lyrics_ai
from ..llm_client import LLMError, current_config, settings_problem, stream_chat
from ..settings import settings
from .common import FlowLayout, Segmented, button, form_row, label
from .editors import LyricsHighlighter

# “AI 改歌词”的快捷修改要求，点击即填入要求输入框
QUICK_INSTRUCTIONS = [
    '副歌更押韵、更好记',
    '整体更口语化',
    '改成粤语',
    '翻译成英文歌词',
    '改得更伤感',
    '改得更积极向上',
    '加一段 [Bridge] 桥段',
    '精简到两段主歌两段副歌',
    '修正错别字和语病，不改变意思',
    '每行字数更整齐',
]

# “AI 写歌词”的快捷其他要求，点击后追加到要求输入框末尾
QUICK_REQUIREMENTS = [
    '副歌押韵、好记',
    '口语化，有画面感',
    '情绪层层递进',
    '适合男女对唱',
    '结尾留有余味',
    '积极向上',
    '带一点忧伤',
]

# 改歌词时的结构约束选项：(显示名, 标识)，标识与 lyrics_ai.STRUCTURES 对应
STRUCTURE_OPTIONS = (
    ('不限制结构', 'off'),
    ('保持段落和行数', 'lines'),
    ('保持段落、行数和每行字数（翻唱贴合原曲旋律）', 'units'),
)

# 写歌词时可选的语言：(显示名, 标识)；空标识表示由 AI 决定
WRITE_LANGUAGES = (
    ('由 AI 按主题决定', ''),
    ('中文', '中文'),
    ('粤语', '粤语'),
    ('英文', '英文'),
    ('日语', '日语'),
    ('韩语', '韩语'),
)


def remember_instruction(text):
    """把本次修改要求记入最近使用列表（去重后置顶，最多保留 30 条）。"""
    recent = [t for t in settings.get('llm_recent', []) if t != text]
    settings['llm_recent'] = [text] + recent[:29]
    settings.save()


class _Bridge(QObject):
    """跨线程信号载体：后台线程经这四个信号把结果送回主线程。"""

    # 参数依次为：run id、增量文本
    delta = Signal(int, str)
    # 参数依次为：run id、状态文本
    status = Signal(int, str)
    # 参数依次为：run id、最终结果对象
    done = Signal(int, object)
    # 参数依次为：run id、错误信息
    failed = Signal(int, str)


def _safe_emit(signal, *args):
    """对话框可能已关闭并销毁，而请求线程仍在运行，此时忽略信号发送异常。"""
    try:
        signal.emit(*args)
    except RuntimeError:
        pass


class _StreamingDialog(QDialog):
    """一次只跑一个后台请求；旧请求的迟到回复按 run id 忽略。"""

    def __init__(self, parent):
        super().__init__(parent)
        self._run_id = 0
        self._cancel = threading.Event()
        self.bridge = _Bridge()
        self.bridge.delta.connect(self._delta)
        self.bridge.status.connect(self._status)
        self.bridge.done.connect(self._done)
        self.bridge.failed.connect(self._failed)

    def start_request(self, job):
        """启动后台线程执行 job；job(on_delta, on_status, cancelled) 的返回值会送到 _done。"""
        self._run_id += 1
        run_id = self._run_id
        self._cancel = threading.Event()
        cancel = self._cancel
        bridge = self.bridge

        def work():
            try:
                result = job(
                    lambda text: _safe_emit(bridge.delta, run_id, text),
                    lambda text: _safe_emit(bridge.status, run_id, text),
                    cancel.is_set,
                )
                _safe_emit(bridge.done, run_id, result)
            except InterruptedError:
                _safe_emit(bridge.failed, run_id, '已停止')
            except (LLMError, ValueError) as exc:
                _safe_emit(bridge.failed, run_id, str(exc))
            except Exception as exc:
                _safe_emit(bridge.failed, run_id, f'{type(exc).__name__}: {exc}')

        threading.Thread(target=work, daemon=True).start()

    def _status(self, run_id, text):
        """状态文本回调：仅当前 run id 有效时更新状态栏。"""
        if run_id == self._run_id:
            self.status.setText(text)

    def stop(self):
        """置位取消事件，请求线程据此尽快中断。"""
        self._cancel.set()

    def reject(self):
        """取消请求后再关闭对话框。"""
        self._cancel.set()
        super().reject()

    def _no_default_buttons(self):
        """取消所有按钮的默认/自动默认属性，避免回车误触。"""
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
            b.setDefault(False)

    @staticmethod
    def _header(title):
        """对话框顶部标题行：左侧标题，右侧当前模型与预设信息。"""
        cfg = current_config(settings)
        row = QHBoxLayout()
        row.addWidget(label(title, 'CardTitle'))
        row.addStretch(1)
        row.addWidget(label(f'模型：{cfg.get("model") or "未设置"} · {cfg.get("preset", "")}', 'Hint'))
        return row


class AiLyricsDialog(_StreamingDialog):
    """按修改要求改写现有歌词。structure 取值 off / lines / units。"""

    def __init__(self, parent, lyrics, style='', structure='off', instruction=''):
        super().__init__(parent)
        self.setWindowTitle('✨ AI 改歌词')
        self.resize(1080, 720)
        self.base_lyrics = lyrics
        self.style = style
        self.result_text = None
        self._chunks = []

        box = QVBoxLayout(self)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(10)

        # 标题行 + 要求输入行
        box.addLayout(self._header('告诉 AI 怎么改'))
        row = QHBoxLayout()
        self.instruction = QLineEdit(instruction)
        self.instruction.setPlaceholderText('例如：把副歌改得更押韵；第二段主歌换成回忆童年的内容；整首改成英文…')
        # 输入框带历史补全，方便复用之前的修改要求
        completer = QCompleter(settings.get('llm_recent', []), self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.instruction.setCompleter(completer)
        self.instruction.returnPressed.connect(self.generate)
        self.go_btn = button('✨ 生成', 'Primary', callback=self.generate)
        self.stop_btn = button('■ 停止', 'Danger', callback=self.stop)
        self.stop_btn.setEnabled(False)
        row.addWidget(self.instruction, 1)
        row.addWidget(self.go_btn)
        row.addWidget(self.stop_btn)
        box.addLayout(row)

        # 快捷要求标签，点击即填充到输入框
        chips = QWidget()
        flow = FlowLayout(chips, spacing=6)
        for text in QUICK_INSTRUCTIONS:
            b = button(text, 'Chip')
            b.clicked.connect(lambda _=False, t=text: self.instruction.setText(t))
            flow.addWidget(b)
        box.addWidget(chips)

        # 结构约束与附带风格选项
        row2 = QHBoxLayout()
        self.structure = QComboBox()
        for name, key in STRUCTURE_OPTIONS:
            self.structure.addItem(name, key)
        self.structure.setCurrentIndex(self.structure.findData(structure))
        self.structure.setToolTip('“保持段落和行数”适合翻译成其他语言；同语言翻唱时选每行字数也保持，最贴合原旋律')
        self.use_style = QCheckBox('附带歌曲风格描述')
        self.use_style.setChecked(bool(style))
        self.use_style.setEnabled(bool(style))
        row2.addWidget(label('结构'))
        row2.addWidget(self.structure)
        row2.addWidget(self.use_style)
        row2.addStretch(1)
        box.addLayout(row2)

        # 左右分栏：左侧当前歌词（只读），右侧 AI 修改结果（可编辑）
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        self.base_title = label('当前歌词', 'CardTitle')
        left_box.addWidget(self.base_title)
        self.base_view = QPlainTextEdit(lyrics)
        self.base_view.setReadOnly(True)
        self._hl1 = LyricsHighlighter(self.base_view.document())
        left_box.addWidget(self.base_view)
        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.addWidget(label('AI 修改结果（可以直接再手动修改）', 'CardTitle'))
        self.result = QPlainTextEdit()
        self.result.setPlaceholderText('输入修改要求后点击“生成”，结果会实时显示在这里')
        self._hl2 = LyricsHighlighter(self.result.document())
        right_box.addWidget(self.result)
        split.addWidget(left)
        split.addWidget(right)
        box.addWidget(split, 1)

        # 底部状态栏与操作按钮
        bar = QHBoxLayout()
        self.status = QLabel('')
        self.status.setObjectName('Hint')
        self.status.setWordWrap(True)
        bar.addWidget(self.status, 1)
        self.continue_btn = button(
            '↪ 在结果上继续改',
            tooltip='把右侧结果作为新的“当前歌词”，再输入新的修改要求',
            callback=self.continue_from_result,
        )
        self.apply_btn = button('✅ 应用到歌词', 'Primary', callback=self.apply)
        cancel_btn = button('取消', callback=self.reject)
        for b in (self.continue_btn, self.apply_btn):
            b.setEnabled(False)
            bar.addWidget(b)
        bar.addWidget(cancel_btn)
        box.addLayout(bar)

        self._no_default_buttons()
        # 配置缺失时在状态栏提示，不自动发起请求
        problem = settings_problem(settings)
        if problem:
            self.status.setText('⚠ ' + problem)
        # 外部带入了修改要求则立即生成
        if instruction:
            self.generate()

    def generate(self):
        """校验修改要求与配置，组装改写请求并启动流式生成。"""
        instruction = self.instruction.text().strip()
        if not instruction:
            self.instruction.setFocus()
            self.status.setText('请先输入修改要求。')
            return
        problem = settings_problem(settings)
        if problem:
            self.status.setText('⚠ ' + problem)
            return
        # 改写请求组装失败（如结构非法）时直接提示，不发请求
        try:
            request = lyrics_ai.rewrite_request(
                self.base_lyrics,
                instruction,
                structure=self.structure.currentData(),
                style=self.style if self.use_style.isChecked() else '',
            )
        except ValueError as e:
            self.status.setText(f'⚠ {e}')
            return
        remember_instruction(instruction)
        cfg = current_config(settings)
        system = lyrics_ai.system_prompt(settings, 'rewrite')
        self._chunks = []
        self.result.clear()
        self._busy(True)
        self.status.setText(f'正在请求 {cfg.get("model")} …')
        self.start_request(
            lambda on_delta, on_status, cancelled: stream_chat(
                cfg, system, request, on_delta=on_delta, cancelled=cancelled, on_status=on_status,
            )
        )

    def _busy(self, busy):
        """按运行状态启用/禁用相关控件，运行中锁定输入与结果框。"""
        self.go_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        self.instruction.setEnabled(not busy)
        self.result.setReadOnly(busy)
        if busy:
            self.continue_btn.setEnabled(False)
            self.apply_btn.setEnabled(False)

    def _delta(self, run_id, text):
        """增量文本回调：仅当前 run id 有效时追加到结果框并更新字数。"""
        if run_id != self._run_id:
            return
        self._chunks.append(text)
        cursor = self.result.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.result.setTextCursor(cursor)
        self.status.setText(f'生成中… {sum(len(c) for c in self._chunks)} 字')

    def _done(self, run_id, full):
        """生成完成回调：清理文本、校验非空、刷新结果并启用后续按钮。"""
        if run_id != self._run_id:
            return
        self._busy(False)
        text, notes = lyrics_ai.tidy_lyrics(lyrics_ai.clean_lyrics(full))
        if not lyrics_ai.lyric_lines(text):
            self.result.clear()
            self.status.setText('⚠ 模型返回了空内容，请换个说法重试。')
            return
        self.result.setPlainText(text)
        old_lines = lyrics_ai.lyric_lines(self.base_lyrics)
        new_lines = lyrics_ai.lyric_lines(text)
        self.status.setText(
            f'✓ 完成（原 {len(old_lines)} 行 → 新 {len(new_lines)} 行）。'
            '满意就点“应用到歌词”，也可以在右侧手动微调或继续改。'
        )
        self.continue_btn.setEnabled(True)
        self.apply_btn.setEnabled(True)

    def _failed(self, run_id, message):
        """失败回调：尽量保留已流式输出且有效的部分，供用户继续。"""
        if run_id != self._run_id:
            return
        self._busy(False)
        self.status.setText(f'⚠ {message}')
        text, notes = lyrics_ai.tidy_lyrics(lyrics_ai.clean_lyrics(self.result.toPlainText()))
        ok = bool(lyrics_ai.lyric_lines(text))
        self.result.setPlainText(text if ok else '')
        self.apply_btn.setEnabled(ok)
        self.continue_btn.setEnabled(ok)

    def continue_from_result(self):
        """把右侧结果当作新的当前歌词，清空输入以进行下一轮改写。"""
        text = self.result.toPlainText().strip()
        if not text:
            return
        self.base_lyrics = text + '\n'
        self.base_view.setPlainText(self.base_lyrics)
        self.base_title.setText('当前歌词（上一轮 AI 结果）')
        self.result.clear()
        self.instruction.clear()
        self.instruction.setFocus()
        self.continue_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.status.setText('输入新的修改要求，继续在这个版本上改。')

    def apply(self):
        """校验结果非空后记录 result_text 并接受对话框。"""
        text = self.result.toPlainText().strip()
        if not lyrics_ai.lyric_lines(text):
            self.status.setText('⚠ 歌词是空的。')
            return
        self.result_text = text + '\n'
        self.accept()


class AiWriteDialog(_StreamingDialog):
    """按主题创作新歌词；可以顺便生成风格描述。确认后通过 result_lyrics / result_style 取结果。"""

    def __init__(self, parent, *, theme='', style=''):
        super().__init__(parent)
        self.setWindowTitle('📝 AI 写歌词')
        self.resize(1080, 760)
        # 折叠空白后的当前风格描述，作为可选参考
        self.page_style = ' '.join(str(style or '').split())
        self.result_lyrics = None
        self.result_style = None
        self._chars = 0

        box = QVBoxLayout(self)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(10)

        # 标题行 + 主题输入
        box.addLayout(self._header('告诉 AI 写一首什么样的歌'))
        self.theme = QPlainTextEdit(theme)
        self.theme.setPlaceholderText('主题或故事，例如：毕业那天在操场告别，约定十年后再见；或者：写给远方外婆的一封信')
        self.theme.setFixedHeight(76)
        box.addWidget(form_row('主题 / 故事', self.theme))

        # 语言与篇幅
        row = QHBoxLayout()
        self.language = QComboBox()
        self.language.setEditable(True)
        for name, key in WRITE_LANGUAGES:
            self.language.addItem(name, key)
        # 篇幅选项：把 LENGTHS 的 (标识, 中文名, 说明) 重排成 Segmented 的 (显示名, 值, 提示)
        self.length = Segmented(
            [(label, key, desc) for key, label, desc in lyrics_ai.LENGTHS],
            lyrics_ai.DEFAULT_LENGTH,
        )
        row.addWidget(form_row('歌词语言', self.language, '可以直接输入其他语言或方言'), 1)
        row.addWidget(
            form_row('篇幅', self.length, '按目标篇幅安排段落和行数；实际时长受旋律、间奏和歌曲生成上限影响'),
            3,
        )
        box.addLayout(row)

        # 其他要求输入 + 快捷要求标签
        self.requirements = QLineEdit()
        self.requirements.setPlaceholderText('其他要求（可选），例如：副歌押韵好记；用第一人称；不要出现“梦想”这个词')
        box.addWidget(form_row('其他要求', self.requirements))
        chips = QWidget()
        flow = FlowLayout(chips, spacing=6)
        for text in QUICK_REQUIREMENTS:
            b = button(text, 'Chip')
            b.clicked.connect(lambda _=False, t=text: self._add_requirement(t))
            flow.addWidget(b)
        box.addWidget(chips)

        # 风格相关选项与生成/停止按钮
        row2 = QHBoxLayout()
        self.use_style = QCheckBox('参考当前风格描述写词')
        self.use_style.setChecked(bool(self.page_style))
        self.use_style.setEnabled(bool(self.page_style))
        self.use_style.setToolTip(self.page_style or '页面上还没有填写风格描述')
        self.make_style = QCheckBox('同时生成风格描述（应用时填入风格框）')
        self.make_style.setChecked(not self.page_style)
        self.go_btn = button('📝 开始写', 'Primary', callback=self.generate)
        self.stop_btn = button('■ 停止', 'Danger', callback=self.stop)
        self.stop_btn.setEnabled(False)
        row2.addWidget(self.use_style)
        row2.addWidget(self.make_style)
        row2.addStretch(1)
        row2.addWidget(self.go_btn)
        row2.addWidget(self.stop_btn)
        box.addLayout(row2)

        # 歌词结果框
        box.addWidget(label('生成的歌词（生成结束后可手动修改）', 'CardTitle'))
        self.result = QPlainTextEdit()
        self.result.setPlaceholderText('填写主题后点击“开始写”，歌词会实时显示在这里')
        self._hl = LyricsHighlighter(self.result.document())
        box.addWidget(self.result, 1)

        # 风格描述输入行，随“同时生成风格描述”开关显隐
        self.style_edit = QLineEdit()
        self.style_edit.setPlaceholderText('勾选“同时生成风格描述”后，这里显示 AI 写的风格，也可以手动修改')
        self.style_row = form_row('风格描述', self.style_edit)
        box.addWidget(self.style_row)
        self.make_style.toggled.connect(self.style_row.setVisible)
        self.style_row.setVisible(self.make_style.isChecked())

        # 底部状态栏与操作按钮
        bar = QHBoxLayout()
        self.status = QLabel('')
        self.status.setObjectName('Hint')
        self.status.setWordWrap(True)
        bar.addWidget(self.status, 1)
        self.apply_btn = button('✅ 应用', 'Primary', callback=self.apply)
        self.apply_btn.setEnabled(False)
        bar.addWidget(self.apply_btn)
        bar.addWidget(button('取消', callback=self.reject))
        box.addLayout(bar)

        self._no_default_buttons()
        problem = settings_problem(settings)
        if problem:
            self.status.setText('⚠ ' + problem)

    def _add_requirement(self, text):
        """把快捷要求追加到要求输入框末尾（去重，用分号分隔）。"""
        cur = self.requirements.text().strip().rstrip('；;')
        if text not in cur:
            self.requirements.setText(f'{cur}；{text}' if cur else text)

    def request(self):
        """返回 (系统提示词, 请求内容)，供生成与测试使用。"""
        return (
            lyrics_ai.system_prompt(settings, 'write'),
            lyrics_ai.write_request(
                self.theme.toPlainText(),
                length=self.length.value(),
                language=self._language(),
                style=self.page_style if self.use_style.isChecked() else '',
                requirements=self.requirements.text(),
            ),
        )

    def _language(self):
        """取当前语言选择：下拉项未编辑时用其标识，编辑过则用输入的文本。"""
        idx = self.language.currentIndex()
        text = self.language.currentText().strip()
        if idx >= 0 and text == self.language.itemText(idx):
            return self.language.itemData(idx)
        return text

    def generate(self):
        """校验配置并启动写词任务；勾选生成风格时在同一任务里续写风格。"""
        problem = settings_problem(settings)
        if problem:
            self.status.setText('⚠ ' + problem)
            return
        try:
            system, request = self.request()
        except ValueError as e:
            self.status.setText(f'⚠ {e}')
            self.theme.setFocus()
            return
        cfg = current_config(settings)
        make_style = self.make_style.isChecked()
        style_system = lyrics_ai.system_prompt(settings, 'style')
        theme, language, requirements = self.theme.toPlainText().strip(), self._language(), self.requirements.text()
        reference = self.page_style if self.use_style.isChecked() else ''

        def job(on_delta, on_status, cancelled):
            # 先流式写歌词，整理后校验非空；再按需生成风格描述
            full = stream_chat(cfg, system, request, on_delta=on_delta, cancelled=cancelled, on_status=on_status)
            lyrics, notes = lyrics_ai.tidy_lyrics(lyrics_ai.clean_lyrics(full))
            if not lyrics_ai.lyric_lines(lyrics):
                raise ValueError('模型返回了空内容，请换个说法重试')
            result = {'lyrics': lyrics, 'notes': notes, 'style': None, 'style_error': ''}
            if make_style:
                on_status('歌词已写好，正在生成风格描述…')
                facts = [f'主题 {theme}'] + ([f'演唱语言 {language}'] if language else [])
                result['style'] = lyrics_ai.clean_style(
                    stream_chat(
                        cfg,
                        style_system,
                        lyrics_ai.style_request(lyrics, requirements=requirements, facts=facts, reference=reference),
                        on_delta=(lambda _text: None),
                        cancelled=cancelled,
                    )
                )
            return result

        self._chars = 0
        self.result.clear()
        self.style_edit.clear()
        self._busy(True)
        self.status.setText(f'正在请求 {cfg.get("model")} …')
        self.start_request(job)

    def _busy(self, busy):
        """按运行状态启用/禁用控件，运行中锁定结果与风格输入。"""
        self.result.setReadOnly(busy)
        self.style_edit.setReadOnly(busy)
        for w in (
            self.go_btn,
            self.theme,
            self.language,
            self.length,
            self.requirements,
            self.use_style,
            self.make_style,
        ):
            w.setEnabled(not busy)
        if not busy:
            self.use_style.setEnabled(bool(self.page_style))
        self.stop_btn.setEnabled(busy)
        if busy:
            self.apply_btn.setEnabled(False)

    def _delta(self, run_id, text):
        """增量文本回调：仅当前 run id 有效时追加到结果框并更新字数。"""
        if run_id != self._run_id:
            return
        self._chars += len(text)
        cursor = self.result.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.result.setTextCursor(cursor)
        self.status.setText(f'写词中… {self._chars} 字')

    def _done(self, run_id, result):
        """写词完成回调：刷新结果与风格、统计行数并提示。"""
        if run_id != self._run_id:
            return
        self._busy(False)
        self.result.setPlainText(result['lyrics'])
        lines = lyrics_ai.lyric_lines(result['lyrics'])
        msg = f'✓ 写好了（{len(lines)} 行歌词，篇幅目标 {lyrics_ai.length_label(self.length.value())}）'
        if result['notes']:
            msg += '；' + '；'.join(result['notes'])
        if result['style']:
            self.style_edit.setText(result['style'])
        elif result['style_error']:
            msg += f'。⚠ 风格描述没有生成：{result["style_error"]}（可以手动填写）'
        self.status.setText(msg + '。满意就点“应用”，也可以先手动修改。')
        self.apply_btn.setEnabled(True)

    def _failed(self, run_id, message):
        """失败回调：尽量保留已流式输出且有效的歌词部分。"""
        if run_id != self._run_id:
            return
        self._busy(False)
        self.status.setText(f'⚠ {message}')
        text, notes = lyrics_ai.tidy_lyrics(lyrics_ai.clean_lyrics(self.result.toPlainText()))
        ok = bool(lyrics_ai.lyric_lines(text))
        self.result.setPlainText(text if ok else '')
        self.apply_btn.setEnabled(ok)

    def apply(self):
        """校验歌词非空后记录 result_lyrics / result_style 并接受对话框。"""
        text = self.result.toPlainText().strip()
        if not lyrics_ai.lyric_lines(text):
            self.status.setText('⚠ 歌词是空的。')
            return
        self.result_lyrics = lyrics_ai.with_default_tag(text + '\n')
        style_text = ' '.join(self.style_edit.text().split())
        self.result_style = style_text if self.make_style.isChecked() and style_text else None
        self.accept()
