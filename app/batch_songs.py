"""批量生成：批量翻唱改词（音频 → 扒谱 → 识别 → AI 改词）与批量创作（主题/歌词 → AI 写词）共用的批次引擎。

整个批次的状态保存在批次文件夹的 batch.json 中，每完成一步立即落盘：
程序被关闭、崩溃或用户点击停止后，“继续批次”只会执行尚未完成的步骤。
run_batch 在 TaskRunner 的工作线程中执行；审核/重试等修改只能在批次没有运行时进行。
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import random
import re
import shutil
import sys
import time
import traceback
import unicodedata

from . import jobs, lyrics_ai
from .abc_utils import strip_chords
from .llm_client import LLMError, current_config, settings_problem, stream_chat
from .paths import new_run_dir, output_root
from .settings import settings

VERSION = 3              # 歌词与风格分别保存步骤状态，风格失败时只重试风格
MAX_SONGS = 200
MAX_THEME_VARIANTS = 50
LYRIC_EXTS = {".txt", ".lrc", ".md"}
STEPS = ("score", "lyrics", "rewrite", "style", "review", "generate")
STEP_NAMES = {
    "cover": {"score": "扒谱", "lyrics": "识别歌词", "rewrite": "AI 改词", "style": "AI 生成风格",
              "review": "审核", "generate": "生成"},
    "create": {"score": "扒谱", "lyrics": "导入歌词", "rewrite": "AI 写词", "style": "AI 生成风格",
               "review": "审核", "generate": "生成"},
}
PENDING, RUNNING, DONE, FAILED, SKIPPED, WAITING = "pending", "running", "done", "failed", "skipped", "waiting"
FINISHED = (DONE, SKIPPED)

CHOICES = {
    "source": ("cover", "create"),
    "create_input": ("themes", "files", "theme_n"),
    "lyrics_action": ("polish", "use"),
    "melody": ("melody", "full", "none"),
    "cot": ("full", "melody", "off"),
    "language_mode": ("auto", "fixed", "per_item"),
    "style_mode": ("fixed", "llm", "per_song"),
    "structure": lyrics_ai.STRUCTURES,
    "length": lyrics_ai.LENGTH_KEYS,
    "review_wait": ("block", "continue"),
    "order": ("stage", "song"),
    "on_error": ("skip", "stop"),
}
BATCH_STATUS_NAMES = {"ready": "待继续", "running": "运行中", "paused": "等待审核", "cancelled": "已停止",
                      "failed": "出错停止", "partial": "完成（部分失败）", "complete": "全部完成"}
ITEM_FILES = {"score", "score.abc", "structure.json", "lyrics", "original.txt", "rewritten.txt", "style.txt"}
LLM_RETRIES = 2          # 网络错误、限流等临时失败的重试次数
# 这些大模型错误不会因为换一首歌而消失：继续跑只会让后面每首都失败，所以直接停止整批。
FATAL_LLM_MARKS = ("HTTP 401", "HTTP 402", "HTTP 403", "HTTP 404", "还没有配置大模型", "请先在「设置", "需要安装官方 SDK",
                   "API Key 无效")
MIN_SUNG_UNITS = 8      # 识别结果少于这么多字/词，按“没有人声”处理
POLISH_INSTRUCTION = "润色歌词：修正错别字和语病，补全段落标签，不改变原意"

DEFAULT_OPTIONS = {
    "source": "cover", "create_input": "themes", "lyrics_action": "polish",
    "melody": "melody", "cot": "full", "asr_language": None,
    "instruction": "", "length": lyrics_ai.DEFAULT_LENGTH, "language_mode": "auto", "language": "",
    "structure": "lines", "structure_retries": 1,
    "style_mode": "fixed", "style": "",
    "review": False, "review_wait": "block", "order": "stage", "on_error": "skip",
    "generation": {"count": 1, "seed": 42, "seed_auto": False, "ode_steps": 32, "cfg_scale": None,
                   "decode_options": {"mode": "auto"}, "abc_sampling": None, "semantic_sampling": None},
}


# ---------------------------------------------------------------------------- 选项
def normalize_options(values):
    values = values or {}
    options = copy.deepcopy(DEFAULT_OPTIONS)
    options.update({k: copy.deepcopy(v) for k, v in values.items() if k in options and k != "generation"})
    options["generation"].update({k: copy.deepcopy(v) for k, v in (values.get("generation") or {}).items()
                                  if k in options["generation"]})
    for key, allowed in CHOICES.items():
        if options[key] not in allowed:
            raise ValueError(f"未知选项 {key} = {options[key]!r}")
    options["instruction"] = str(options["instruction"] or "").strip()
    options["style"] = " ".join(str(options["style"] or "").split())
    options["language"] = " ".join(str(options["language"] or "").split())[:40]
    cover = options["source"] == "cover"
    has_original = cover or options["create_input"] == "files"
    if len(options["instruction"]) > 2000:
        raise ValueError("改词/写词要求请控制在 2000 字以内")
    if cover and not options["instruction"]:
        raise ValueError("请填写改词要求")
    if (not cover and options["create_input"] == "files" and options["lyrics_action"] == "polish"
            and not options["instruction"]):
        raise ValueError("“AI 润色”需要填写写词要求，例如：修正错别字，补全段落标签")
    if not has_original:
        options["structure"] = "off"          # 没有原歌词可对照
    if options["style_mode"] == "fixed" and not options["style"]:
        raise ValueError("“统一风格”需要填写风格描述")
    if len(options["style"]) > 1000:
        raise ValueError("风格描述请控制在 1000 字符以内")
    if options["language_mode"] == "fixed" and not options["language"]:
        raise ValueError("“统一指定语言”需要选择或填写语言")
    options["review"] = bool(options["review"])
    options["structure_retries"] = min(max(int(options["structure_retries"]), 0), 3)
    generation = options["generation"]
    for key in ("count", "seed", "ode_steps"):
        generation[key] = int(generation[key])
    generation["seed_auto"] = bool(generation["seed_auto"])
    if generation["cfg_scale"] is not None:
        generation["cfg_scale"] = float(generation["cfg_scale"])
    if not 1 <= generation["count"] <= 16:
        raise ValueError("每首生成数量必须为 1～16")
    if not 8 <= generation["ode_steps"] <= 64:
        raise ValueError("ODE 步数必须为 8～64")
    if not 0 <= generation["seed"] < 2**63:
        raise ValueError("起始种子超出范围 [0, 2^63)")
    return options


def uses_llm_for_lyrics(options, item=None):
    """批量创作“直接使用”导入歌词时不调用大模型（除非审核时标记了让 AI 重改）。"""
    direct = (options["source"] == "create" and options["create_input"] == "files"
              and options["lyrics_action"] == "use")
    return not direct or bool(item and item.get("extra_instruction"))


def estimate_llm_calls(options, songs):
    return (songs if uses_llm_for_lyrics(options) else 0) + (songs if options["style_mode"] == "llm" else 0)


# ---------------------------------------------------------------------------- 输入整理
def parse_themes(text):
    """每行一个主题；可选“主题 | 语言 | 风格”。"""
    entries = []
    for raw in str(text or "").splitlines():
        parts = [part.strip() for part in raw.split("|")]
        if not parts[0]:
            continue
        entries.append({"theme": parts[0], "language": parts[1] if len(parts) > 1 else "",
                        "style": "|".join(parts[2:]).strip() if len(parts) > 2 else ""})
    return entries


def expand_theme(theme, count):
    theme = " ".join(str(theme or "").split())
    if not theme:
        return []
    count = int(count)
    if not 1 <= count <= MAX_THEME_VARIANTS:
        raise ValueError(f"一个主题最多写 {MAX_THEME_VARIANTS} 首")
    return [{"theme": theme, "group": "theme", "variant": index, "variants": count}
            for index in range(1, count + 1)]


_LRC_TIME = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")
_LRC_META = re.compile(r"^\[(?:ar|ti|al|au|by|offset|re|ve|length|tool):.*\]$", re.I)


def import_lyrics(path):
    data = Path(path).read_bytes()
    if len(data) > 1_000_000:
        raise ValueError("歌词文件超过 1 MB")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("无法识别歌词文件编码，请另存为 UTF-8")
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if _LRC_META.match(line):
            continue
        lines.append(_LRC_TIME.sub("", line).strip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not text:
        raise ValueError("歌词文件是空的")
    if len(text) > 12000:
        raise ValueError("歌词超过 12000 字符")
    return text + "\n"


def _safe_name(text):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)[:40].strip("_") or "song"


def _now():
    return datetime.now().isoformat(timespec="seconds")


def create_batch(entries, options, *, require_styles=True):
    """校验输入并创建批次文件夹；只写入状态，不执行任何步骤。

    entries：翻唱 [{"audio"}]；创作·导入 [{"lyrics_file"}]；
             创作·主题 [{"theme","language","style"}]（可来自 parse_themes / expand_theme）。
    require_styles=False 用于“只建批次”：之后在表格里逐首填写风格，开始运行前再检查。
    """
    options = normalize_options(options)
    cover = options["source"] == "cover"
    by_file = cover or options["create_input"] == "files"
    prepared, seen = [], set()
    for entry in entries:
        record = {"audio": "", "lyrics_file": "", "theme": "", "group": str(entry.get("group") or ""),
                  "variant": int(entry.get("variant") or 1), "variants": int(entry.get("variants") or 1),
                  "language": " ".join(str(entry.get("language") or "").split())[:40],
                  "style": " ".join(str(entry.get("style") or "").split())[:1000]}
        if by_file:
            key = "audio" if cover else "lyrics_file"
            if not entry.get(key):
                raise ValueError("输入文件路径为空")
            path = Path(entry[key]).resolve()
            if path in seen:
                continue
            if not path.is_file():
                raise FileNotFoundError(f"文件不存在：{path}")
            if not cover and path.suffix.lower() not in LYRIC_EXTS:
                raise ValueError(f"不支持的歌词文件：{path.name}（支持 txt / lrc / md）")
            seen.add(path)
            record[key], record["name"] = str(path), path.stem
        else:
            theme = " ".join(str(entry.get("theme") or "").split())
            if not theme:
                continue
            if len(theme) > 500:
                raise ValueError("每个主题请控制在 500 字以内")
            record["theme"] = theme
            record["name"] = theme[:20] + (f" #{record['variant']}" if record["variants"] > 1 else "")
        prepared.append(record)
    if not prepared:
        raise ValueError("请至少添加一首歌曲" if cover else "请至少输入一个主题或导入一份歌词")
    if len(prepared) > MAX_SONGS:
        raise ValueError(f"一个批次最多 {MAX_SONGS} 首歌")
    if require_styles and options["style_mode"] == "per_song" and not options["style"]:
        # 批次建好后会立刻开始跑，没有风格的歌要到生成那一步才失败，所以现在就拦下来。
        missing = [record["name"] for record in prepared if not record["style"]]
        if missing:
            raise ValueError("“每首单独填写风格”需要填写默认风格，或为每首都写上风格。没有风格的："
                             + "、".join(missing[:5]) + (" 等" if len(missing) > 5 else ""))

    generation = options["generation"]
    count = generation["count"]
    seeds = []
    for index in range(len(prepared)):
        # 每首歌占用 count 个连续种子，不同歌曲之间不会重复。
        seed = (random.randrange(0, 2**63 - count) if generation["seed_auto"]
                else generation["seed"] + index * count)
        if seed < 0 or seed + count - 1 >= 2**63:
            raise ValueError("批量种子会超出 YuE2 允许的范围 [0, 2^63)，请减小起始种子")
        seeds.append(seed)

    folder = new_run_dir("batch", f"{options['source']}_{len(prepared)}", settings)
    items = []
    for index, (record, seed) in enumerate(zip(prepared, seeds), 1):
        item_id = f"{index:03d}"
        record.update(id=item_id, folder=f"{item_id}_{_safe_name(record['name'])}", seed=seed,
                      detected_language="", extra_instruction="", skipped=False)
        (folder / "items" / record["folder"]).mkdir(parents=True)
        steps = {step: {"status": PENDING, "error": ""} for step in STEPS}
        if not (cover and options["melody"] != "none"):
            steps["score"]["status"] = SKIPPED
        if not by_file:
            steps["lyrics"]["status"] = SKIPPED
        if options["style_mode"] != "llm":
            steps["style"]["status"] = SKIPPED
        if not options["review"]:
            steps["review"]["status"] = SKIPPED
        record["steps"] = steps
        items.append(record)
    state = {"version": VERSION, "kind": "batch_songs", "created": _now(), "status": "ready", "error": "",
             "options": options, "items": items}
    save_batch(folder, state)
    return folder


# ---------------------------------------------------------------------------- 读写
def read_batch(folder):
    state = json.loads((Path(folder) / "batch.json").read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != VERSION or state.get("kind") != "batch_songs":
        raise ValueError("不是有效的批量生成任务")
    if not isinstance(state.get("options"), dict) or not isinstance(state.get("items"), list):
        raise ValueError("批次文件已损坏")
    for item in state["items"]:
        name = item.get("folder", "")
        if not name or Path(name).name != name or name in (".", "..") or set(item.get("steps", {})) != set(STEPS):
            raise ValueError("批次文件已损坏")
    return state


def save_batch(folder, state):
    # Windows 上界面线程刚好在读 batch.json 时，替换文件会短暂失败（共享冲突），稍等重试即可。
    for attempt in range(20):
        try:
            jobs.write_json(Path(folder) / "batch.json", state)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def list_batches():
    root = output_root(settings) / "batch"
    result = []
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True) if root.is_dir() else []:
        try:
            result.append((folder, read_batch(folder)))
        except (OSError, ValueError):
            continue
    return result


def item_path(folder, item, name):
    if name not in ITEM_FILES:
        raise ValueError("批次文件路径不合法")
    path = Path(folder) / "items" / item["folder"] / name
    if path.is_symlink():
        raise ValueError("批次文件路径不合法")
    return path


def read_text(folder, item, name):
    path = item_path(folder, item, name)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def write_text(folder, item, name, text):
    path = item_path(folder, item, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _fresh_dir(path):
    """重做某一步时清掉上次的半成品，避免新旧文件混在一起。"""
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def find_item(state, item_id):
    for item in state["items"]:
        if item["id"] == item_id:
            return item
    raise KeyError(f"批次中没有编号 {item_id} 的歌曲")


def score_info(folder, item):
    text = read_text(folder, item, "structure.json")
    return json.loads(text) if text else {}


def item_language(state, item):
    mode = state["options"]["language_mode"]
    if mode == "fixed":
        return state["options"]["language"]
    if mode == "per_item":
        return item.get("language") or ""
    return ""


def style_hint(state, item):
    mode = state["options"]["style_mode"]
    if mode == "fixed":
        return state["options"]["style"]
    if mode == "per_song":
        return item.get("style") or state["options"]["style"]
    return ""


# ---------------------------------------------------------------------------- 状态汇总
def item_state(item):
    if item.get("skipped"):
        return "skipped"
    statuses = [item["steps"][step]["status"] for step in STEPS]
    if FAILED in statuses:
        return "failed"
    if WAITING in statuses:
        return "waiting"
    if RUNNING in statuses:
        return "running"
    if all(status in FINISHED for status in statuses):
        return "done"
    return "pending"


def batch_status(state):
    states = [item_state(item) for item in state["items"]]
    if "waiting" in states:
        return "paused"
    if "pending" in states or "running" in states:
        return "ready"
    if "failed" in states:
        return "partial"
    return "complete"


def display_status(folder, state):
    if state.get("status") == "running" and not jobs.is_leased(folder):
        return "已中断，可继续"
    return BATCH_STATUS_NAMES.get(state.get("status"), state.get("status", ""))


def counts(state):
    result = {}
    for item in state["items"]:
        key = item_state(item)
        result[key] = result.get(key, 0) + 1
    return result


# ---------------------------------------------------------------------------- 调度
def _ready(item, step):
    steps = item["steps"]
    if steps[step]["status"] != PENDING:
        return False
    return all(steps[previous]["status"] in FINISHED for previous in STEPS[:STEPS.index(step)])


def next_action(state):
    """返回下一步要执行的 (歌曲, 步骤)；没有可执行的步骤时返回 None。"""
    options, items = state["options"], state["items"]
    if options["order"] == "song":
        for item in items:
            for step in STEPS:
                status = item["steps"][step]["status"]
                if status in FINISHED:
                    continue
                if status == PENDING:
                    return item, step
                if status == WAITING and options["review_wait"] == "block":
                    return None          # 等这首审核完再继续
                break                    # FAILED，或选择了“先处理后面的歌”：看下一首
        return None
    for step in STEPS:
        for item in items:
            if _ready(item, step):
                return item, step
    return None


def _recover_running(state):
    for item in state["items"]:
        for record in item["steps"].values():
            if record["status"] == RUNNING:
                record["status"] = PENDING


def can_continue(state):
    """是否还有可执行的步骤（崩溃残留的“进行中”按未完成计算）。

    有歌在等审核时，已经审核通过的歌仍然可以先继续生成。
    """
    probe = copy.deepcopy(state)
    _recover_running(probe)
    return next_action(probe) is not None


class _ItemContext:
    """给内部步骤的进度文字加上“[3/12 七里香 · 生成]”前缀，其余原样转发。"""

    def __init__(self, ctx, prefix):
        self._ctx, self._prefix = ctx, prefix

    def cancelled(self):
        return self._ctx.cancelled()

    def progress(self, **info):
        if info.get("text"):
            info = dict(info, text=self._prefix + info["text"])
        self._ctx.progress(**info)

    def emit(self, kind, payload):
        self._ctx.emit(kind, payload)


def _publish(ctx, folder, state):
    save_batch(folder, state)
    # 工作线程之后还会继续修改 state，发给界面线程的必须是副本。
    ctx.emit("batch", {"dir": str(folder), "state": copy.deepcopy(state)})


def missing_styles(state):
    """“每首单独填写风格”且没有默认风格时，还没填风格、又还没开始生成的歌。"""
    options = state["options"]
    if options["style_mode"] != "per_song" or options["style"]:
        return []
    return [item["name"] for item in state["items"]
            if not item.get("style") and not item.get("skipped")
            and item["steps"]["generate"]["status"] in (PENDING, RUNNING) and not item["steps"]["generate"].get("job_dir")]


def _needs_llm(state):
    options = state["options"]
    return any((item["steps"]["rewrite"]["status"] in (PENDING, RUNNING) and uses_llm_for_lyrics(options, item))
               or item["steps"]["style"]["status"] in (PENDING, RUNNING)
               for item in state["items"])


def run_batch(engine, ctx, folder):
    folder = Path(folder)
    with jobs.job_lease(folder):
        state = read_batch(folder)
        problem = settings_problem(settings)
        if problem and _needs_llm(state):
            raise ValueError(problem)
        missing = missing_styles(state)
        if missing:
            raise ValueError("请先在右侧表格“风格”列为这些歌填写风格：" + "、".join(missing[:5])
                             + (" 等" if len(missing) > 5 else ""))
        _recover_running(state)      # 上次崩溃或强退残留：持有锁时可以安全重做
        state.update(status="running", error="")
        _publish(ctx, folder, state)
        try:
            while True:
                if ctx.cancelled():
                    raise InterruptedError("已停止，批次进度已保存，可点击“继续批次”")
                action = next_action(state)
                if action is None:
                    break
                item, step = action
                if step == "review":
                    item["steps"]["review"]["status"] = WAITING
                    _publish(ctx, folder, state)
                    continue
                _run_one(engine, ctx, folder, state, item, step)
        except InterruptedError:
            state["status"] = "cancelled"
            _publish(ctx, folder, state)
            raise
        except BaseException as exc:
            state.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:2000])
            _publish(ctx, folder, state)
            raise
        state["status"] = batch_status(state)
        _publish(ctx, folder, state)
        return {"dir": str(folder), "status": state["status"], "counts": counts(state)}


def _run_one(engine, ctx, folder, state, item, step):
    record = item["steps"][step]
    record.update(status=RUNNING, error="", started=_now())
    _publish(ctx, folder, state)
    number = state["items"].index(item) + 1
    name = STEP_NAMES[state["options"]["source"]][step]
    sub = _ItemContext(ctx, f"[{number}/{len(state['items'])} {item['name']} · {name}] ")
    try:
        STEP_RUNNERS[step](engine, sub, folder, state, item)
    except InterruptedError:
        record["status"] = PENDING
        raise
    except Exception as exc:  # noqa: BLE001 - 单首失败不影响其他歌曲
        record.update(status=FAILED, error=f"{type(exc).__name__}: {exc}"[:2000])
        print(f"[批量生成] {item['name']} · {name} 失败\n{traceback.format_exc()}", file=sys.stderr)
        if "out of memory" in str(exc).lower():
            # 显存不足后残留的模型和缓存会让后面的歌接连失败，先全部释放。
            try:
                engine.unload()
            except Exception:  # noqa: BLE001
                pass
        _publish(ctx, folder, state)
        if isinstance(exc, LLMError) and any(mark in str(exc) for mark in FATAL_LLM_MARKS):
            raise LLMError("大模型接口设置有误，已停止批次（修改「设置 → 大模型 API」后点“重试失败项”）：\n"
                           + str(exc)) from exc
        if state["options"]["on_error"] == "stop":
            raise
        return
    record.update(status=DONE, finished=_now())
    _publish(ctx, folder, state)


# ---------------------------------------------------------------------------- 各步骤
def _step_score(engine, ctx, folder, state, item):
    melody = state["options"]["melody"]
    out = item_path(folder, item, "score")
    _fresh_dir(out)
    result = engine.run_transcribe(ctx, {"audio": item["audio"], "melody_only": melody == "melody",
                                         "output_dir": str(out)})
    abc = result.get("abc")
    if not abc:
        raise ValueError("SheetSage2 没有得到乐谱：" + str(result.get("error") or result.get("abc_error") or "未知原因"))
    if melody == "melody":
        abc = strip_chords(abc)
    if len(abc) > 100_000:
        raise ValueError("扒出的乐谱超过 100,000 字符，无法用于生成")
    if result.get("error"):
        item["steps"]["score"]["warning"] = "扒谱中途出错，只得到部分乐谱：" + str(result["error"])[:300]
    write_text(folder, item, "score.abc", abc)
    info = {"structure": [list(part) for part in result.get("structure") or []], "bpm": result.get("bpm"),
            "meter": result.get("meter"), "keys": sorted({key[2] for key in result.get("keys") or []}),
            "duration": result.get("duration")}
    write_text(folder, item, "structure.json", json.dumps(info, ensure_ascii=False, indent=2))


def _step_lyrics(engine, ctx, folder, state, item):
    if state["options"]["source"] == "create":
        ctx.progress(step=0, steps=["导入歌词"], text="读取歌词文件…")
        write_text(folder, item, "original.txt", import_lyrics(item["lyrics_file"]))
        return
    structure = score_info(folder, item).get("structure") or []
    out = item_path(folder, item, "lyrics")
    _fresh_dir(out)
    result = engine.run_lyrics(ctx, {"audio": item["audio"], "language": state["options"]["asr_language"],
                                     "align": True, "sections": "given" if structure else "none",
                                     "structure": structure, "output_dir": str(out)})
    sung = lyric_content_units(result["lyrics"])
    if sung < MIN_SUNG_UNITS:
        # 纯伴奏或人声很弱时，识别器常会“听出”一两个语气词；拿去改词只会凭空编一首，不如直接标记失败。
        raise ValueError(f"只识别到 {sung} 个字/词的歌词（{result['lyrics'].strip()[:40]!r}），"
                         "可能是纯音乐或人声太弱；确认有人声可换“识别语言”后重试，否则请跳过")
    write_text(folder, item, "original.txt", result["lyrics"])
    item["detected_language"] = result.get("language") or ""


def rewrite_instruction(state, item):
    """改原歌词的要求：批次的改词/写词要求，加上审核时写的补充要求。"""
    instruction, extra = state["options"]["instruction"], item.get("extra_instruction") or ""
    if instruction and extra:
        return f"{instruction}\n补充要求：{extra}"
    return instruction or extra


def written_openings(folder, state, item):
    """同一主题已经写好的其他版本的前两句，让大模型避免雷同。"""
    openings = []
    for other in state["items"]:
        if other is item or other.get("group") != item.get("group") or other["steps"]["rewrite"]["status"] != DONE:
            continue
        head = lyrics_ai.lyric_lines(read_text(folder, other, "rewritten.txt"))[:2]
        if head:
            openings.append(" / ".join(head))
    return openings


def write_theme_lyrics(folder, state, item, ctx):
    """按主题写词（写歌词提示词）；审核时写了修改意见、且已有歌词时，按意见修改这一版（改歌词提示词）。"""
    options = state["options"]
    extra = item.get("extra_instruction") or ""
    language, style = item_language(state, item), style_hint(state, item)
    previous = read_text(folder, item, "rewritten.txt") if extra else ""
    if previous.strip():
        user = lyrics_ai.rewrite_request(previous, extra, language=language, style=style)
        return _lyrics_reply(_chat(lyrics_ai.system_prompt(settings, "rewrite"), user, ctx, "AI 按审核意见改词中…"))
    requirements = "\n".join(part for part in (options["instruction"], extra) if part)
    user = lyrics_ai.write_request(item["theme"], length=options["length"], language=language, style=style,
                                   requirements=requirements, variant=(item["variant"], item["variants"]),
                                   avoid=written_openings(folder, state, item))
    return _lyrics_reply(_chat(lyrics_ai.system_prompt(settings, "write"), user, ctx, "AI 写词中…"))


def _step_rewrite(engine, ctx, folder, state, item):
    options = state["options"]
    record = item["steps"]["rewrite"]
    if item["steps"]["lyrics"]["status"] != DONE:
        lyrics, problems = write_theme_lyrics(folder, state, item, ctx), []
    else:
        original = read_text(folder, item, "original.txt")
        if not original.strip():
            raise ValueError("原歌词为空，无法改词")
        if uses_llm_for_lyrics(options, item):
            lyrics, problems = rewrite_lyrics(original, rewrite_instruction(state, item), ctx,
                                              language=item_language(state, item), style=style_hint(state, item),
                                              structure=options["structure"], retries=options["structure_retries"])
        else:
            lyrics, problems = original, []
    lyrics, notes = lyrics_ai.tidy_lyrics(lyrics)
    if uses_llm_for_lyrics(options, item) and not lyrics_ai.lyric_lines(lyrics):
        raise ValueError("模型没有返回歌词正文，请调整要求后重试")
    if len(lyrics) > 12000:
        raise ValueError("歌词超过 12000 字符，请调整要求后重试")
    write_text(folder, item, "rewritten.txt", lyrics)
    record["warning"] = "；".join((notes + problems)[:3])


def _step_style(engine, ctx, folder, state, item):
    lyrics = read_text(folder, item, "rewritten.txt")
    if not lyrics.strip():
        raise ValueError("歌词为空，无法生成风格描述")
    write_text(folder, item, "style.txt", suggest_style(state, item, lyrics, score_info(folder, item), ctx))


def _step_generate(engine, ctx, folder, state, item):
    record = item["steps"]["generate"]
    if not record.get("job_dir"):
        record["job_dir"] = str(engine.prepare_generate(build_song_params(folder, state, item)))
        save_batch(folder, state)    # 先记下任务目录：中断后继续同一个任务，不会重复新建
    results = engine.run_resume(ctx, {"dir": record["job_dir"]})
    record["results"] = [result["dir"] for result in results]


STEP_RUNNERS = {"score": _step_score, "lyrics": _step_lyrics, "rewrite": _step_rewrite,
                "style": _step_style, "generate": _step_generate}


def resolve_style(folder, state, item):
    options = state["options"]
    if options["style_mode"] == "llm":
        style = read_text(folder, item, "style.txt")
    else:
        style = style_hint(state, item)
    style = " ".join(style.split())
    if not style:
        raise ValueError("这首歌没有风格描述：请在表格“风格”列填写，或填写默认风格")
    if len(style) > 1000:
        raise ValueError("风格描述超过 1000 字符")
    return style


def build_song_params(folder, state, item):
    options, generation = state["options"], state["options"]["generation"]
    lyrics = read_text(folder, item, "rewritten.txt").strip()
    if not lyrics:
        raise ValueError("最终歌词为空")
    if len(lyrics) > 12000:
        raise ValueError("歌词超过 12000 字符")
    cover = options["source"] == "cover"
    melody = options["melody"] if cover else "none"
    abc = read_text(folder, item, "score.abc") if melody != "none" else None
    if melody != "none" and not (abc or "").strip():
        raise ValueError("缺少扒谱结果")
    params = {"style": resolve_style(folder, state, item), "lyrics": lyrics, "abc": abc,
              "cot": (melody if melody != "none" else "full") if cover else options["cot"],
              "mode": "cover" if melody != "none" else "create",
              "seed": item["seed"], "seed_auto": False, "count": generation["count"],
              "ode_steps": generation["ode_steps"], "cfg_scale": generation["cfg_scale"],
              "decode_options": generation["decode_options"], "abc_sampling": generation["abc_sampling"],
              "semantic_sampling": generation["semantic_sampling"],
              "title": f"{item['name']} · 改词" if cover else item["name"],
              "batch": {"dir": str(folder), "item": item["id"], "source": options["source"],
                        "instruction": options["instruction"], "theme": item.get("theme", "")}}
    if cover:
        params["source_audio"] = item["audio"]
    return params


# ---------------------------------------------------------------------------- 大模型
def _chat(system, user, ctx, caption):
    problem = settings_problem(settings)
    if problem:
        raise LLMError(problem)
    config = current_config(settings)
    received, last = [0], [0.0]

    def delta(text):
        received[0] += len(text)
        now = time.monotonic()
        if now - last[0] >= 0.3:
            last[0] = now
            ctx.progress(step=0, steps=["大模型"], text=caption, detail=f"已接收 {received[0]} 字")

    for attempt in range(LLM_RETRIES + 1):
        received[0] = 0
        ctx.progress(step=0, steps=["大模型"], text=caption, detail=f"请求 {config.get('model')}…")
        try:
            return stream_chat(config, system, user, on_delta=delta, cancelled=ctx.cancelled,
                               on_status=lambda s: ctx.progress(text=caption, detail=s))
        except LLMError as exc:
            message = str(exc)
            temporary = any(mark in message for mark in ("HTTP 429", "HTTP 5", "无法连接", "提前结束"))
            if attempt == LLM_RETRIES or not temporary:
                raise
            for _ in range(30 * (attempt + 1)):      # 3 秒、6 秒后重试，期间可以取消
                if ctx.cancelled():
                    raise InterruptedError("已取消")
                time.sleep(0.1)


def _lyrics_reply(text):
    lyrics = lyrics_ai.clean_lyrics(text)
    if not lyrics.strip():
        raise LLMError("模型返回了空内容")
    return lyrics


def rewrite_lyrics(original, instruction, ctx, *, language="", style="", structure="off", retries=0):
    """改写原歌词（改歌词提示词），返回 (歌词, 结构问题列表)。结构不符时带着问题清单重试，保留问题最少的一版。"""
    system = lyrics_ai.system_prompt(settings, "rewrite")
    base = lyrics_ai.rewrite_request(original, instruction, language=language, style=style, structure=structure)
    keep = structure != "off"
    user, best = base, None
    for attempt in range((retries if keep else 0) + 1):
        caption = "AI 改词中…" if attempt == 0 else f"结构不符，第 {attempt} 次重试…"
        lyrics = _lyrics_reply(_chat(system, user, ctx, caption))
        problems = structure_problems(original, lyrics, structure)
        if best is None or len(problems) < len(best[1]):
            best = (lyrics, problems)
        if not problems:
            break
        user = (base + "\n\n【上一次的结果】\n" + lyrics.strip()
                + "\n\n【上一次结果不符合结构要求，请逐条修正后重新输出完整歌词】\n" + "\n".join(problems[:12]))
    return best


def suggest_style(state, item, lyrics, info, ctx):
    facts = []
    if item.get("theme"):
        facts.append(f"主题 {item['theme']}")
    language = item_language(state, item) or item.get("detected_language")
    if language:
        facts.append(f"演唱语言 {language}")
    if info.get("bpm"):
        facts.append(f"原曲速度约 {info['bpm']:.0f} BPM")
    if info.get("keys"):
        facts.append("调性 " + "、".join(info["keys"]))
    if info.get("meter"):
        facts.append(f"拍号 {info['meter']}")
    user = lyrics_ai.style_request(lyrics, requirements=state["options"]["instruction"], facts=facts)
    return lyrics_ai.clean_style(_chat(lyrics_ai.system_prompt(settings, "style"), user, ctx, "AI 生成风格描述…"))


# ---------------------------------------------------------------------------- 结构校验
_TAG = lyrics_ai.TAG_LINE
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯豈-﫿]")


_NO_SPACE_SCRIPT = re.compile(r"[฀-໿က-႟ក-៿]")   # 泰文、老挝文、缅甸文、高棉文
_INNER_APOSTROPHE = re.compile(r"(?<=\w)['’](?=\w)")


def _word_runs(line):
    """按 Unicode 字母/附加符号切出非中日韩的“词”（任何文字：拉丁、西里尔、希腊、阿拉伯、泰文……）。"""
    run = []
    for char in _INNER_APOSTROPHE.sub("", line):
        if unicodedata.category(char)[0] in "LMN" and not _CJK.match(char):
            run.append(char)
        elif run:
            yield "".join(run)
            run = []
    if run:
        yield "".join(run)


def sung_units(line):
    """每行的近似演唱单位（用于结构比较）：中日韩文字按字计；用空格分词的语言按词计；
    泰文等不用空格分词的文字按约 3 个字母一个音节估算。"""
    units = len(_CJK.findall(line))
    for run in _word_runs(line):
        if _NO_SPACE_SCRIPT.search(run):
            letters = sum(1 for char in run if unicodedata.category(char).startswith("L"))
            units += max(1, round(letters / 3))
        else:
            units += 1
    return units


def lyric_content_units(text):
    """判断“有没有识别到人声歌词”用的歌词量：去掉段落标签和空行后，所有文字的演唱单位之和。"""
    return sum(sung_units(line) for line in text.splitlines() if line.strip() and not _TAG.match(line.strip()))


def lyric_shape(text):
    """[(段落标签, [每行演唱单位])]；只统计有歌词的段落。

    纯器乐段落（只有标签）不参与比较：识别结果里偶尔会有重复的空标签，大模型合并掉它们不影响演唱。
    """
    sections = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _TAG.match(line):
            # [Verse 1] 与 [Verse] 视为同一种段落，避免大模型加编号就被判为结构不同。
            sections.append((re.sub(r"[\s\d]+", "", line.lower()), []))
            continue
        if not sections:
            sections.append(("", []))
        sections[-1][1].append(sung_units(line))
    return [(tag, lines) for tag, lines in sections if lines]


def structure_problems(original, rewritten, mode="lines", tolerance=0.25):
    if mode == "off" or not original.strip():
        return []
    old, new = lyric_shape(original), lyric_shape(rewritten)
    if [tag for tag, _ in old] != [tag for tag, _ in new]:
        return ["段落标签顺序不同：原 " + " ".join(t or "(无标签)" for t, _ in old)
                + "；新 " + " ".join(t or "(无标签)" for t, _ in new)]
    problems = []
    for number, ((tag, a), (_, b)) in enumerate(zip(old, new), 1):
        name = f"{tag or '(无标签)'}（第 {number} 段）"
        if len(a) != len(b):
            problems.append(f"{name} 原 {len(a)} 行，新 {len(b)} 行")
        elif mode == "units":
            for row, (x, y) in enumerate(zip(a, b), 1):
                if abs(x - y) > max(2, round(x * tolerance)):
                    problems.append(f"{name} 第 {row} 行：原 {x} 字，新 {y} 字")
    return problems


# ---------------------------------------------------------------------------- 界面线程调用的修改操作
@contextmanager
def editing(folder):
    """批次运行时锁被工作线程持有，这里会抛出“正在被使用”，保证不会同时写 batch.json。"""
    folder = Path(folder)
    with jobs.job_lease(folder):
        state = read_batch(folder)
        yield state
        state["status"] = batch_status(state)
        save_batch(folder, state)


def approve(folder, item_id, lyrics, style=None):
    with editing(folder) as state:
        item = find_item(state, item_id)
        if item["steps"]["review"]["status"] != WAITING:
            raise ValueError("这首歌当前不需要审核")
        lyrics = lyrics.strip()
        if not lyrics:
            raise ValueError("歌词不能为空")
        if len(lyrics) > 12000:
            raise ValueError("歌词超过 12000 字符")
        if style is not None:
            style = " ".join(style.split())
            if len(style) > 1000:
                raise ValueError("风格描述超过 1000 字符")
            if state["options"]["style_mode"] == "llm":
                if not style:
                    raise ValueError("风格描述不能为空")
                write_text(folder, item, "style.txt", style)
            elif state["options"]["style_mode"] == "per_song":
                item["style"] = style
        write_text(folder, item, "rewritten.txt", lyrics + "\n")
        original = read_text(folder, item, "original.txt")
        item["steps"]["rewrite"]["warning"] = "；".join(
            structure_problems(original, lyrics, state["options"]["structure"])[:3])
        item["steps"]["review"].update(status=DONE, error="", finished=_now())


def skip_item(folder, item_id):
    with editing(folder) as state:
        item = find_item(state, item_id)
        if item_state(item) in ("done", "skipped"):
            raise ValueError("这首歌已经完成或已跳过")
        generate = item["steps"]["generate"]
        if generate.get("job_dir") and generate["status"] != FAILED:
            raise ValueError("这首歌已经开始生成，不能跳过；可以在作品库中删除生成结果")
        item["skipped"] = True
        for step in STEPS:
            if item["steps"][step]["status"] not in FINISHED:
                item["steps"][step].update(status=SKIPPED, error="")


def request_rewrite(folder, item_id, extra=""):
    with editing(folder) as state:
        item = find_item(state, item_id)
        if item.get("skipped"):
            raise ValueError("这首歌已被跳过")
        if item["steps"]["generate"].get("job_dir"):
            raise ValueError("这首歌已经开始生成，不能再改词；可以把歌词复制到创作/翻唱页单独生成")
        if item["steps"]["rewrite"]["status"] not in FINISHED + (FAILED,):
            raise ValueError("这首歌还没有写好词")
        extra = " ".join(str(extra).split())[:500]
        if not extra and not uses_llm_for_lyrics(state["options"]):
            # “直接使用”的歌词点重改却没写要求：交给大模型润色，而不是再原样复制一遍。
            extra = POLISH_INSTRUCTION
        item["extra_instruction"] = extra
        item["steps"]["rewrite"] = {"status": PENDING, "error": ""}
        item["steps"]["style"] = {"status": PENDING if state["options"]["style_mode"] == "llm" else SKIPPED,
                                  "error": ""}
        if state["options"]["review"]:
            item["steps"]["review"] = {"status": PENDING, "error": ""}


def set_item_fields(folder, item_id, *, language=None, style=None):
    """表格里逐首填写语言/风格。"""
    with editing(folder) as state:
        options, item = state["options"], find_item(state, item_id)
        if language is not None:
            if options["language_mode"] != "per_item":
                raise ValueError("只有“每首单独指定语言”可以逐首设置语言")
            if item["steps"]["rewrite"]["status"] in FINISHED:
                raise ValueError("这首歌已经写好词，修改语言不会生效；可以在审核里“标记让 AI 重改”")
            item["language"] = " ".join(str(language).split())[:40]
        if style is not None:
            if options["style_mode"] != "per_song":
                raise ValueError("只有“每首单独填写风格”可以逐首设置风格")
            if item["steps"]["generate"].get("job_dir"):
                raise ValueError("这首歌已经开始生成，修改风格不会生效")
            item["style"] = " ".join(str(style).split())[:1000]


def failed_generate_jobs(state):
    return sum(1 for item in state["items"]
               if item["steps"]["generate"]["status"] == FAILED and item["steps"]["generate"].get("job_dir"))


def retry_failed(folder, item_ids=None, new_generate_job=False):
    """把失败的步骤改回未完成。

    生成步骤默认继续原来的任务（已完成的阶段不重做）；new_generate_job=True 时新建任务重新生成，
    用于原任务被删除、损坏或推理版本已变化而无法继续的情况（原任务仍保留在作品库中）。
    """
    with editing(folder) as state:
        total = 0
        for item in state["items"]:
            if item_ids and item["id"] not in item_ids:
                continue
            for step, record in item["steps"].items():
                if record["status"] == FAILED:
                    record.update(status=PENDING, error="")
                    if step == "generate" and new_generate_job:
                        record.pop("job_dir", None)
                        record.pop("results", None)
                    total += 1
        return total
