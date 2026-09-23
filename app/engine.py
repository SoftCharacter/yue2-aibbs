"""Model loading and the long-running jobs behind every page.

All public ``run_*`` methods execute on the single worker thread owned by
``TaskRunner``; they report through ``ctx.progress`` and poll ``ctx.cancelled``.
"""
from __future__ import annotations

import gc
import json
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from . import compat
from .paths import ALIGNER_DIR, ASR_DIR, MERT_DIRS, MODELS, SHEETSAGE_DIR, YUE2_DIR, new_run_dir
from .settings import settings

SEMANTIC_TOKENS_PER_SECOND = 25.0

YUE2_STEPS = ["加载模型", "规划乐谱", "生成歌曲", "声学合成", "解码音频"]
YUE2_STAGE = {
    "Verifying model files": (0, "校验模型文件"),
    "Loading model": (0, "加载 YuE2 模型"),
    "Using provided score": (1, "使用提供的乐谱"),
    "Planning score": (1, "规划乐谱"),
    "Generating song": (2, "生成歌曲"),
    "Synthesizing audio": (3, "声学合成"),
    "Loading audio decoder": (4, "加载音频解码器"),
    "Decoding audio": (4, "解码音频"),
}
SHEETSAGE_STEPS = ["加载模型", "读取音频", "识别音乐", "生成乐谱", "完成"]
MERT_STEPS = ["加载模型", "读取音频", "提取特征", "保存结果"]
LYRICS_STEPS = ["加载模型", "分析段落", "识别歌词", "对齐时间", "整理歌词"]

MERT_LAYER_GUIDE = [
    # task, 30s layer, FullSong layer
    ("流派 · GTZAN", "L23", "L24"),
    ("节拍 · GTZAN", "L21", "L23"),
    ("调性 · GiantSteps", "L4", "L23"),
    ("情绪 · EmoMusic", "全部层", "L24"),
    ("和弦 · Chords1217", "全部层", "全部层"),
    ("标签 · MagnaTagATune", "L22", "L23"),
    ("乐器 · MTG-Jamendo", "L14", "L12"),
    ("氛围/主题 · MTG-Jamendo", "L16", "L13"),
    ("流派 · MTG-Jamendo", "L19", "L16"),
    ("Top-50 标签 · MTG-Jamendo", "L13", "L22"),
]


def vae_choices():
    result = []
    for path in sorted(MODELS.iterdir()) if MODELS.is_dir() else []:
        config = path / "config.json"
        if path.is_dir() and config.is_file():
            try:
                if json.loads(config.read_text(encoding="utf-8")).get("model_type") == "yue2_vae":
                    result.append(path.name)
            except (OSError, ValueError):
                pass
    return result


class Throttle:
    def __init__(self, interval=0.2):
        self.interval, self.last = interval, 0.0

    def ready(self, force=False):
        now = time.monotonic()
        if force or now - self.last >= self.interval:
            self.last = now
            return True
        return False


class Engine:
    def __init__(self):
        self.yue2 = None
        self.yue2_key = None
        self.sheetsage = None
        self.sheetsage_device = None
        self.mert = {}
        self.mert_devices = {}
        self.asr_device = None
        self.asr = None
        self.asr_align = False
        self._yue2_fraction = None
        self.last_decode_options = None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def torch():
        import torch
        return torch

    def device(self, runtime=None):
        torch = self.torch()
        choice = (runtime or {}).get("device", settings.get("device", "auto"))
        if choice == "auto":
            return "cuda" if torch.cuda.is_available() else "cpu"
        return choice

    def loaded(self):
        return {"YuE2-3B": self.yue2 is not None, "SheetSage2": self.sheetsage is not None,
                "MERT-v2-30s": "30s" in self.mert, "MERT-v2-FullSong": "FullSong" in self.mert,
                "Qwen3-ASR-1.7B": self.asr is not None,
                "Qwen3-ForcedAligner-0.6B": self.asr is not None and self.asr_align}

    def unload(self, name=None, keep=None):
        names = {"yue2", "sheetsage", "mert-30s", "mert-FullSong", "asr"} if name is None else {name}
        if keep:
            names.discard(keep)
        freed = False
        if "yue2" in names and self.yue2 is not None:
            self.yue2.close()
            self.yue2, self.yue2_key, freed = None, None, True
            self._yue2_fraction = None
        if "sheetsage" in names and self.sheetsage is not None:
            self.sheetsage, freed = None, True
            self.sheetsage_device = None
        if "asr" in names and self.asr is not None:
            self.asr, self.asr_align, freed = None, False, True
            self.asr_device = None
        for variant in list(self.mert):
            if f"mert-{variant}" in names:
                del self.mert[variant]
                self.mert_devices.pop(variant, None)
                freed = True
        if freed:
            gc.collect()
            torch = self.torch()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def offload_yue2(self):
        """Move the cached YuE2 MOT to host RAM (as the official pipeline does before VAE decoding).

        The pipeline stays cached, so the next song of a batch does not reload 7 GB from disk.
        """
        pipe = self.yue2
        model = getattr(pipe, "_model", None) if pipe is not None else None
        if model is None:
            return
        model.to("cpu")
        torch = self.torch()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _exclusive(self, keep):
        if settings.get("exclusive_vram", True):
            self.unload(keep=keep)

    # ------------------------------------------------------------------ YuE2
    def runtime_snapshot(self):
        """Freeze model choices, keeping the current memory budget adjustable on resume."""
        return {key: settings.get(key) for key in
                ('device', 'backend', 'attention', 'vae', 'verify_hashes', 'memory_budget_gib')}

    def get_yue2(self, ctx, runtime=None):
        pipeline = compat.ensure_yue2()
        torch = self.torch()
        choices_config = dict(settings)
        choices_config.update(runtime or {})
        vae_dir = MODELS / choices_config.get("vae", "YuE2-Vae")
        if not (vae_dir / "config.json").is_file():
            if runtime:
                raise FileNotFoundError(f"已保存任务的 VAE 不存在：{vae_dir}")
            choices = vae_choices()
            if not choices:
                raise FileNotFoundError("models 目录下没有找到 YuE2-Vae 解码器")
            vae_dir = MODELS / choices[0]
        device = self.device(runtime)
        budget = float(settings.get("memory_budget_gib") or 0)
        if 0 < budget < 4:
            raise ValueError("YuE2 显存预算至少需要 4 GiB（其中 2 GiB 会作为运行保留），或设为 0 使用整张显卡")
        if budget <= 0:
            budget = (torch.cuda.get_device_properties(torch.device(device)).total_memory / 2**30) if device.startswith("cuda") else 24.0
        key = (device, round(budget, 2), choices_config.get("backend"), str(vae_dir), choices_config.get("attention"),
               bool(choices_config.get("verify_hashes")))
        self._exclusive("yue2")
        if self.yue2 is not None and self.yue2_key == key:
            return self.yue2
        self.unload("yue2")
        compat.set_attention(choices_config.get("attention", "auto"))
        ctx.progress(step=0, steps=YUE2_STEPS, text="初始化 YuE2 管线…")
        # Construction also changes the allocator limit. Restore it even if construction fails,
        # and publish the cached pipeline only after initialization is complete.
        with self._cuda_budget_scope(device):
            pipe = pipeline.YuE2Pipeline(
                YUE2_DIR, vae_dir, device=device, memory_budget_gib=budget,
                backend=choices_config.get("backend", "torch"), verify_hashes=bool(choices_config.get("verify_hashes")),
                progress=True)
            fraction = (torch.cuda.get_per_process_memory_fraction(pipe.device)
                        if pipe.device.type == "cuda" else None)
        self.yue2, self.yue2_key, self._yue2_fraction = pipe, key, fraction
        return self.yue2

    @contextmanager
    def _cuda_budget_scope(self, device, fraction=None):
        """Limit only the task's CUDA device and restore its exact previous limit."""
        torch = self.torch()
        device = torch.device(device)
        if device.type != "cuda":
            yield
            return
        index = device.index if device.index is not None else torch.cuda.current_device()
        previous = torch.cuda.get_per_process_memory_fraction(index)
        try:
            if fraction is not None:
                torch.cuda.set_per_process_memory_fraction(fraction, index)
            yield
        finally:
            torch.cuda.set_per_process_memory_fraction(previous, index)

    def _yue2_sink(self, ctx, prefix, sampling_limits):
        throttle = Throttle(0.15)

        def sink(info):
            if info.get("label") == "Complete":
                return
            step, text = YUE2_STAGE.get(info["label"], (None, info["label"]))
            done = info.get("status") is not None
            if not throttle.ready(force=done or info.get("completed", 0) == 0):
                return
            completed, total, unit = info.get("completed", 0), info.get("total"), info.get("unit")
            elapsed = info.get("elapsed", 0.0)
            detail = f"{elapsed:.1f}s"
            value = maximum = None
            if unit == "tokens":
                rate = completed / elapsed if elapsed > 0 else 0.0
                detail = f"{completed} tokens · {rate:.0f} tok/s · {elapsed:.1f}s"
                maximum = sampling_limits.get(step)
                value = completed
                if step == 2:
                    detail = (f"约 {completed / SEMANTIC_TOKENS_PER_SECOND:.0f} 秒音乐 · " + detail)
            elif total:
                value, maximum = completed, total
                detail = f"{completed}/{total} {'步' if unit == 'steps' else '块'} · {elapsed:.1f}s"
            ctx.progress(step=step, steps=YUE2_STEPS, text=prefix + text, detail=detail,
                         value=value, maximum=maximum)
        return sink

    @staticmethod
    def _sampling(values):
        if not values:
            return None
        integers = {'top_k', 'max_tokens', 'min_tokens', 'penalty_window'}
        return {key: (int(value) if key in integers else float(value)) for key, value in values.items()}

    def save_project(self, params):
        from . import jobs
        out = new_run_dir('songs', 'draft', settings)
        jobs.create_job(out, params, self.runtime_snapshot())
        return jobs.job_result(out)

    @staticmethod
    def _check_batch_seed(params):
        count = max(1, int(params.get('count', 1)))
        base_seed = int(params['seed'])
        if not 0 <= base_seed < 2**63 or base_seed + count - 1 >= 2**63:
            raise ValueError('批量生成的种子会超出 YuE2 允许的范围 [0, 2^63)，请减小种子或生成数量')
        return base_seed, count

    def prepare_generate(self, params):
        """Create a (batch) generation job and return its folder without running it."""
        from . import jobs
        base_seed, count = self._check_batch_seed(params)
        snapshot = dict(params, seed=base_seed, count=1, seed_auto=False)
        out = new_run_dir('songs', f"{params.get('mode', 'create')}_{base_seed}", settings)
        state = jobs.create_job(out, snapshot, self.runtime_snapshot())
        state['batch_count'] = count
        jobs.write_json(out/'job.json', state)
        return out

    def run_generate(self, ctx, params):
        self._check_batch_seed(params)
        if ctx.cancelled():
            raise InterruptedError('已取消')
        return self.run_resume(ctx, {'dir': str(self.prepare_generate(params))})

    def run_plan(self, ctx, params):
        from . import jobs
        from .workflow import execute_job
        # A request to re-plan must not turn an existing ABC into an external plan.
        snapshot = dict(params, abc=None, count=1, seed_auto=False)
        out = new_run_dir('songs', f"plan_{snapshot['seed']}", settings)
        jobs.create_job(out, snapshot, self.runtime_snapshot())
        ctx.emit('job', jobs.job_result(out))
        try:
            return execute_job(self, ctx, out, stop_after='plan')
        finally:
            if self.yue2 is not None and self.yue2._model is not None:
                self.yue2._model.to('cpu')
                if self.torch().cuda.is_available():
                    self.torch().cuda.empty_cache()

    def run_resume(self, ctx, params):
        from . import jobs
        from .workflow import execute_job
        with jobs.job_lease(params['dir']):
            folders = jobs.prepare_batch(params['dir'])
            for folder in folders:
                ctx.emit('job', jobs.job_result(folder))
            results = []
            for folder in folders:
                if ctx.cancelled():
                    raise InterruptedError('已取消，未完成的批量任务已保存')
                result = execute_job(self, ctx, folder)
                results.append(result)
                ctx.emit('item', result)
            return results

    def decode_latents(self, ctx, latent, *, runtime=None, options=None, expected_vae=None):
        from .decoder import decode_latents
        self.last_decode_options = None
        return decode_latents(self, ctx, latent, runtime=runtime, options=options, expected_vae=expected_vae)

    def run_decode(self, ctx, params):
        from . import jobs
        from .workflow import check_latents, decode_options, execute_job
        path = Path(params['latent'])
        if path.is_dir():
            path = path/'latent.npy'
        if not path.is_file():
            raise FileNotFoundError('没有找到 latent.npy')
        source_meta = {}
        manifest_hash = None
        if path.name == 'latent.npy' and (path.parent/'job.json').exists():
            source = jobs.read_job(path.parent, verify=False)
            if 'latent.npy' not in source['artifacts']:
                raise ValueError('这个任务尚未完成声学合成，潜变量不能作为有效阶段复用')
            jobs.verify_artifact(path.parent, 'latent.npy', source['artifacts']['latent.npy'])
            manifest_hash = source['artifacts']['latent.npy']['sha256']
            source_meta = dict(source['params'])
        elif path.name == 'latent.npy' and (path.parent/'result.json').exists():
            manifest = json.loads((path.parent/'result.json').read_text(encoding='utf-8'))
            expected = manifest.get('artifacts', {}).get('latent.npy')
            if expected and (path.stat().st_size != expected['bytes'] or jobs.sha256(path) != expected['sha256']):
                raise ValueError('原作品的潜变量损坏或已改变')
            manifest_hash = expected['sha256'] if expected else None
            if (path.parent/'meta.json').exists():
                source_meta = json.loads((path.parent/'meta.json').read_text(encoding='utf-8'))
        if path.name == 'latent.npy' and (path.parent/'score.abc').is_file():
            source_meta['abc'] = (path.parent/'score.abc').read_text(encoding='utf-8')
        source_hash = jobs.sha256(path)
        if manifest_hash and source_hash != manifest_hash:
            raise ValueError('来源潜变量在校验后已改变，请恢复原文件或重新选择来源')
        check_latents(np.load(path, allow_pickle=False))
        if jobs.sha256(path) != source_hash:
            raise ValueError('读取期间来源潜变量已改变，请重试')
        runtime = self.runtime_snapshot()
        runtime['vae'] = params.get('vae') or runtime['vae']
        budget = float(settings.get('memory_budget_gib') or 0)
        decode_options(params.get('decode_options'), default_core=512 if 0 < budget <= 12 else 1024)
        if ctx.cancelled():
            raise InterruptedError('已取消')
        compat.ensure_yue2()
        from yue2.storage import model_identity
        ctx.progress(step=0, steps=YUE2_STEPS, text='校验本地音频解码器…')
        weights = model_identity(MODELS/runtime['vae'], verify=bool(runtime.get('verify_hashes')))
        runtime['decode_vae_identity'] = weights
        snapshot = dict(source_meta, source_latent_sha256=source_hash, mode='decode', title=params.get('title') or source_meta.get('title') or path.parent.name,
                        source_latent=str(path.resolve()), decode_options=params.get('decode_options') or {'mode': 'auto'}, count=1)
        out = new_run_dir('songs', 'decode', settings)
        jobs.create_job(out, snapshot, runtime, kind='decode')
        ctx.emit('job', jobs.job_result(out))
        result = execute_job(self, ctx, out)
        ctx.emit('item', result)
        return [result]

    # ------------------------------------------------------------------ SheetSage2
    def get_sheetsage(self, ctx):
        device = self.device()
        self._exclusive("sheetsage")
        if self.sheetsage is not None and self.sheetsage_device == device:
            return self.sheetsage
        self.unload("sheetsage")
        ctx.progress(step=0, steps=SHEETSAGE_STEPS, text="加载 SheetSage2（含 MERT-v2-FullSong 主干）…")
        from transformers import AutoModel
        model = AutoModel.from_pretrained(
            str(SHEETSAGE_DIR), trust_remote_code=True, local_files_only=True,
            base_model_path=str(MERT_DIRS["FullSong"]))
        self.sheetsage = model.eval().to(device)
        self.sheetsage_device = device
        return self.sheetsage

    def run_transcribe(self, ctx, params):
        model = self.get_sheetsage(ctx)
        audio = Path(params["audio"])
        out = Path(params["output_dir"]) if params.get("output_dir") else new_run_dir("transcribe", audio.stem, settings)
        throttle = Throttle(0.2)

        def progress(info):
            if ctx.cancelled():
                raise InterruptedError("已取消")
            stage = info.get("stage")
            if stage == "audio":
                ctx.progress(step=1, steps=SHEETSAGE_STEPS, text="读取并重采样音频…")
            elif stage == "encoding":
                ctx.progress(step=2, steps=SHEETSAGE_STEPS, text=f"编码音频窗口 {info['window']}/{info['windows']}",
                             detail=f"窗口起点 {info['start']:.0f}s")
            elif stage == "decoding" and throttle.ready():
                ctx.progress(step=2, steps=SHEETSAGE_STEPS, text=f"识别旋律/和弦/节拍 · 窗口 {info['window']}/{info['windows']}",
                             detail=f"{info['tokens']} tokens", value=info["tokens"], maximum=model.max_output_seq_len)
            elif stage == "notation":
                ctx.progress(step=3, steps=SHEETSAGE_STEPS, text="生成 ABC 乐谱与 MIDI…")
            elif stage == "complete":
                ctx.progress(step=4, steps=SHEETSAGE_STEPS, text="完成")

        options = dict(melody_only=bool(params.get("melody_only")), preset=params.get("preset", "default"),
                       dtype=params.get("dtype", "bf16"), progress=progress,
                       export_logits=bool(params.get("export_logits")),
                       export_scores=bool(params.get("export_scores")),
                       export_embeddings=bool(params.get("export_embeddings")),
                       output_hidden_states=bool(params.get("all_layers")))
        if params.get("max_seconds"):
            options["max_seconds"] = float(params["max_seconds"])
        if params.get("preset", "default") == "default":
            if params.get("overlap") is not None:
                options["overlap_seconds"] = float(params["overlap"])
            if params.get("lookahead") is not None:
                options["lookahead_seconds"] = float(params["lookahead"])
        source = str(audio)
        if options["preset"] == "paper":
            progress({"stage": "audio"})
            source, options["sampling_rate"] = compat.decode_sheetsage_paper_audio(audio)
            if ctx.cancelled():
                raise InterruptedError("已取消")
        error = None
        try:
            result = model.transcribe(source, output_dir=str(out), **options)
        except RuntimeError as exc:
            if getattr(exc, "result", None) is None:
                raise
            result, error = exc.result, str(exc)
        return summarize_transcription(result, out, audio, error)

    # ------------------------------------------------------------------ MERT-v2
    def get_mert(self, ctx, variant):
        device = self.device()
        self._exclusive(f"mert-{variant}")
        if variant in self.mert and self.mert_devices.get(variant) == device:
            return self.mert[variant]
        self.unload(f"mert-{variant}")
        ctx.progress(step=0, steps=MERT_STEPS, text=f"加载 MERT-v2-{variant}…")
        from transformers import AutoModel
        model = AutoModel.from_pretrained(str(MERT_DIRS[variant]), trust_remote_code=True, local_files_only=True)
        self.mert[variant] = model.eval().to(device)
        self.mert_devices[variant] = device
        return self.mert[variant]

    def run_mert(self, ctx, params):
        from .audio_utils import decode_audio
        torch = self.torch()
        variant = params["variant"]
        model = self.get_mert(ctx, variant)
        device = next(model.parameters()).device
        rate = model.config.sampling_rate
        window = int(model.config.context_seconds * rate)
        minimum = model.config.minimum_input_samples
        layer = params.get("layer", 0)  # 0 = last hidden state, 1..24 = block
        use_bf16 = params.get("dtype", "bf16") == "bf16" and device.type == "cuda"
        out = new_run_dir("mert", variant, settings)
        files = [Path(f) for f in params["files"]]
        names = [p.name for p in files]
        display = [p.name if names.count(p.name) == 1 else f"{p.parent.name}/{p.name}" for p in files]
        items, embeddings = [], []
        for number, path in enumerate(files, 1):
            if ctx.cancelled():
                raise InterruptedError("已取消")
            prefix = f"{number}/{len(files)} · {path.name}"
            ctx.progress(step=1, steps=MERT_STEPS, text=f"读取音频 {prefix}")
            audio = decode_audio(path, rate, max_seconds=params.get("max_seconds") or None)
            chunks = [audio[i:i + window] for i in range(0, len(audio), window)]
            chunks = [c for c in chunks if len(c) >= minimum]
            if not chunks:
                raise ValueError(f"{path.name} 太短，无法提取特征")
            frames, layer_sums, total = [], None, 0
            for index, chunk in enumerate(chunks, 1):
                if ctx.cancelled():
                    raise InterruptedError("已取消")
                ctx.progress(step=2, steps=MERT_STEPS, text=f"提取特征 {prefix}",
                             detail=f"片段 {index}/{len(chunks)}", value=index, maximum=len(chunks))
                x = torch.from_numpy(chunk)[None].to(device)
                with torch.inference_mode(), torch.autocast(device.type, dtype=torch.bfloat16, enabled=use_bf16):
                    output = model(input_values=x, output_hidden_states=True)
                states = output.hidden_states
                selected = output.last_hidden_state if not layer else states[layer - 1]
                frames.append(selected[0].float().cpu())
                means = torch.stack([h[0].float().sum(0) for h in states]).cpu()
                layer_sums = means if layer_sums is None else layer_sums + means
                total += states[0].shape[1]
                del output, states, selected, x
                # A stop pressed during the forward pass (often the only chunk) must not end as a success.
                if ctx.cancelled():
                    raise InterruptedError("已取消")
            frame_array = torch.cat(frames).numpy().astype(np.float32)
            layer_means = (layer_sums / total).numpy().astype(np.float32)
            embedding = frame_array.mean(0)
            stem = f"{number:02d}_{safe_stem(path.stem)}"
            if ctx.cancelled():
                raise InterruptedError("已取消")
            ctx.progress(step=3, steps=MERT_STEPS, text=f"保存 {prefix}")
            if params.get("save_frames", True):
                np.save(out / f"{stem}.frames.npy", frame_array)
            np.save(out / f"{stem}.embedding.npy", embedding)
            if params.get("save_layers", True):
                np.save(out / f"{stem}.layers.npy", layer_means)
            ssm, pool = self_similarity(frame_array, model.config.frame_rate)
            seconds = len(audio) / rate
            items.append({"name": display[number - 1], "path": str(path), "seconds": seconds, "frames": int(frame_array.shape[0]),
                          "ssm": ssm, "pool_seconds": pool, "stem": stem})
            embeddings.append(embedding)
        if ctx.cancelled():
            raise InterruptedError("已取消")
        matrix = cosine_matrix(np.stack(embeddings))
        summary = {"variant": variant, "layer": layer or "last", "dtype": "bf16" if use_bf16 else "fp32",
                   "frame_rate": model.config.frame_rate,
                   "files": [{k: v for k, v in item.items() if k != "ssm"} for item in items],
                   "similarity": matrix.tolist()}
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"dir": str(out), "items": items, "similarity": matrix, "variant": variant, "layer": layer}

    # ------------------------------------------------------------------ Qwen3-ASR lyrics
    def get_asr(self, ctx, align):
        device = self.device()
        self._exclusive("asr")
        if self.asr is not None and self.asr_device == device and (self.asr_align or not align):
            return self.asr
        self.unload("asr")
        if not (ASR_DIR / "config.json").is_file():
            raise FileNotFoundError(f"没有找到 {ASR_DIR}")
        if align and not (ALIGNER_DIR / "config.json").is_file():
            raise FileNotFoundError(f"没有找到 {ALIGNER_DIR}（关闭“时间戳对齐”可只识别文字）")
        ctx.progress(step=0, steps=LYRICS_STEPS, text="加载 Qwen3-ASR-1.7B" + (" + ForcedAligner" if align else "") + "…")
        qwen_asr = compat.ensure_qwen_asr()
        torch = self.torch()
        device = self.device()
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
        device_map = "cuda:0" if device == "cuda" else device
        kwargs = dict(dtype=dtype, device_map=device_map, max_inference_batch_size=1, max_new_tokens=4096)
        if align:
            kwargs.update(forced_aligner=str(ALIGNER_DIR), forced_aligner_kwargs=dict(dtype=dtype, device_map=device_map))
        self.asr = qwen_asr.Qwen3ASRModel.from_pretrained(str(ASR_DIR), **kwargs)
        self.asr_align = bool(align)
        self.asr_device = device
        return self.asr

    def run_lyrics(self, ctx, params):
        """params: audio, language, context, align, sections ('none'|'sheetsage'|'given'),
        structure (for 'given'), gap, max_width."""
        from .audio_utils import decode_audio
        from .lyrics_utils import build_lines, format_yue2, to_lrc
        audio_path = Path(params["audio"])
        align = bool(params.get("align", True))
        structure, transcription = params.get("structure") or [], None
        if params.get("sections") == "sheetsage":
            sub = type("Sub", (), {})()
            sub.cancelled = ctx.cancelled
            sub.progress = lambda **info: ctx.progress(step=1, steps=LYRICS_STEPS,
                                                        text="SheetSage2 分析段落：" + (info.get("text") or ""),
                                                        detail=info.get("detail"), value=info.get("value"),
                                                        maximum=info.get("maximum"))
            sub.emit = ctx.emit
            transcription = self.run_transcribe(sub, {"audio": str(audio_path), "melody_only": True})
            structure = transcription["structure"]
        model = self.get_asr(ctx, align)
        if ctx.cancelled():
            raise InterruptedError("已取消")
        ctx.progress(step=2, steps=LYRICS_STEPS, text="读取音频并识别歌词（Qwen3-ASR）…")
        wav = decode_audio(audio_path, 16000)
        started = time.perf_counter()
        ctx.progress(step=3 if align else 2, steps=LYRICS_STEPS,
                     text="识别歌词并对齐逐字时间戳…" if align else "识别歌词…",
                     detail=f"音频 {len(wav) / 16000:.0f} 秒")
        from .asr_control import cancellable_transcription
        from .asr_text import transcribe_single
        with cancellable_transcription(model, ctx):
            result = transcribe_single(model, audio=(wav, 16000), context=params.get("context") or "",
                                       language=params.get("language"), return_time_stamps=align)
        seconds = time.perf_counter() - started
        if ctx.cancelled():
            raise InterruptedError("已取消")
        if not (result.text or "").strip():
            raise ValueError("没有识别到歌词：可能是纯音乐、人声太弱，或选错了演唱语言")
        ctx.progress(step=4, steps=LYRICS_STEPS, text="整理歌词…")
        items = [(i.text.strip(), float(i.start_time), float(i.end_time))
                 for i in (result.time_stamps or []) if (i.text or "").strip()] if align else []
        lines = build_lines(result.text, items, gap=float(params.get("gap", 0.6)), max_width=int(params.get("max_width", 22)))
        lyrics = format_yue2(lines, structure if structure else None)
        lrc = to_lrc(lines)
        out = Path(params["output_dir"]) if params.get("output_dir") else new_run_dir("lyrics", audio_path.stem, settings)
        out.mkdir(parents=True, exist_ok=True)
        (out / "lyrics.txt").write_text(lyrics, encoding="utf-8")
        (out / "raw.txt").write_text(result.text, encoding="utf-8")
        if align:
            (out / "lyrics.lrc").write_text(lrc, encoding="utf-8")
        (out / "asr.json").write_text(json.dumps({
            "audio": str(audio_path), "language": result.language, "text": result.text,
            "items": items, "lines": lines, "structure": structure, "seconds": seconds,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"dir": str(out), "audio": str(audio_path), "language": result.language, "text": result.text,
                "items": items, "lines": lines, "lyrics": lyrics, "lrc": lrc if align else "",
                "structure": structure, "transcription": transcription, "seconds": seconds,
                "duration": len(wav) / 16000}

    # ------------------------------------------------------------------ batch songs
    def run_batch_songs(self, ctx, params):
        from .batch_songs import run_batch
        return run_batch(self, ctx, params["dir"])

    # ------------------------------------------------------------------ misc
    def run_unload(self, ctx, params):
        self.unload(params.get("name"))
        return self.loaded()


def safe_stem(text):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)[:48] or "audio"


def cosine_matrix(vectors):
    normed = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-8)
    return (normed @ normed.T).astype(np.float32)


def self_similarity(frames, frame_rate, max_size=480):
    pool = max(1, int(round(frame_rate)))  # ~1 second per cell
    n = len(frames) // pool
    if n < 2:
        pool, n = 1, len(frames)
    if n > max_size:
        pool = int(np.ceil(len(frames) / max_size))
        n = len(frames) // pool
    pooled = frames[: n * pool].reshape(n, pool, -1).mean(1)
    return cosine_matrix(pooled), pool / frame_rate


def parse_lab(text, kind="interval"):
    rows = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if not line.strip():
            continue
        try:
            if kind == "interval" and len(parts) >= 3:
                rows.append((float(parts[0]), float(parts[1]), parts[2]))
            elif kind == "beat" and len(parts) >= 4:
                rows.append((float(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])))
        except ValueError:
            continue
    return rows


def summarize_transcription(result, out, audio, error=None):
    labs = result.get("labs") or {}
    beats = parse_lab(labs.get("beat"), "beat")
    bpm = meter = None
    if len(beats) > 2:
        diffs = np.diff([b[0] for b in beats])
        diffs = diffs[diffs > 0]
        if len(diffs):
            bpm = 60.0 / float(np.median(diffs))
        meter = f"{beats[0][2]}/{beats[0][3]}"
    files = sorted(str(p.relative_to(out)) for p in Path(out).rglob("*") if p.is_file())
    return {
        "dir": str(out), "audio": str(audio), "abc": result.get("abc"),
        "abc_error": result.get("abc_error"), "error": error,
        "duration": result.get("duration_seconds"), "bpm": bpm, "meter": meter,
        "keys": parse_lab(labs.get("key")), "structure": parse_lab(labs.get("structure")),
        "chords": parse_lab(labs.get("chord")),
        "melody_notes": result.get("melody_notes"), "vocal_notes": result.get("vocal_notes"),
        "instrumental_notes": result.get("instrumental_notes"), "events": result.get("events") or [],
        "warnings": result.get("warnings") or [], "diagnostics": result.get("diagnostics") or [],
        "elapsed": result.get("elapsed_seconds"), "melody_only": result.get("melody_only"),
        "peak_gpu_mib": result.get("peak_gpu_mib"), "files": files,
    }


engine = Engine()
