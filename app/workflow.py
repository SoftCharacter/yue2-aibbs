"""Run and resume whole completed YuE2 stages without reusing edited inputs.

本模块负责"整首歌"生成任务的执行与断点续跑：把一首歌拆成 draft(规划)、
plan(曲谱)、semantic(语义 tokens)、latent(潜变量)、audio(音频) 五个可落盘的阶段，
每个阶段完成后把中间产物写入任务目录并提交（commit），下次可从任意已完成的阶段继续，
而不会因为调用方修改了入参就复用被缓存的旧阶段。
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
import uuid
from pathlib import Path

import numpy as np

from . import compat, jobs


def decode_options(values, *, default_core=1024):
    """把解码配置字典规范化，并校验各字段的取值范围。

    未提供的字段回落默认值；mode 为 ``auto`` 时统一采用分块解码（tiled），
    核心帧数使用调用方传入的 ``default_core``（由 VAE 的配置决定）。
    """
    # 传入 None 视为空字典，便于上层直接透传可选配置
    values = values or {}
    mode = values.get('mode', 'auto')
    if mode not in ('auto', 'tiled', 'full'):
        raise ValueError('未知解码模式')

    core_frames = int(values.get('core_frames', 1024))
    context_frames = int(values.get('context_frames', 16))
    # 核心帧数限制在 [64, 8192]，上下文帧数限制在 [0, 256]
    if not (64 <= core_frames <= 8192):
        raise ValueError('解码分块或上下文参数超出范围')
    if not (0 <= context_frames <= 256):
        raise ValueError('解码分块或上下文参数超出范围')

    if mode == 'auto':
        return {'mode': 'tiled', 'core_frames': default_core, 'context_frames': 16}
    return {'mode': mode, 'core_frames': core_frames, 'context_frames': context_frames}


def check_latents(value):
    """校验潜变量为 [T, 64] 的浮点数组，并在忽略溢出的前提下转成 float32。

    返回转换后的 float32 数组；任何形状、类型或数值异常都会抛出 ValueError。
    """
    value = np.asarray(value)
    # 形状必须为二维、第二维为 64、至少一帧、且为浮点类型
    if (
        value.ndim != 2
        or value.shape[1] != 64
        or value.shape[0] < 1
        or value.dtype.kind != 'f'
    ):
        raise ValueError('潜变量必须是浮点数组 [T, 64]，且至少包含一帧')
    if not np.isfinite(value).all():
        raise ValueError('潜变量包含 NaN 或无穷值')

    # 转换 float32 时忽略溢出，避免极值抛出 RuntimeWarning，转换后再统一检查有限性
    with np.errstate(over='ignore'):
        result = value.astype(np.float32, copy=False)
    if not np.isfinite(result).all():
        raise ValueError('潜变量超出 float32 的有限数值范围')
    return result


def _checkpoint(ctx, folder):
    """向 GUI 上报一次任务进度快照，用于在阶段推进后刷新界面。"""
    ctx.emit('job', jobs.job_result(folder))


def _cancel(ctx):
    """在取消状态下抛出中断异常，提示已完成的阶段已保存、可稍后继续。"""
    if ctx.cancelled():
        raise InterruptedError('已取消，完整阶段已保存，可在作品库继续')


def _json(folder, name):
    """读取任务目录下名为 name 的 JSON 文件并解析为 Python 对象。"""
    return json.loads((folder / name).read_text(encoding='utf-8'))


def _finish_audio(folder, audio, params, config, timing, truncated, actual_decode=None):
    """把解码得到的音频落盘为 FLAC，并写出 meta/result 元数据，提交 audio 阶段。

    ``audio`` 为 [N, 2] 的 float32 双声道波形；最终产物 audio.flac 采用 PCM_24 编码，
    同时按任务规范生成 meta.json（含创作信息与耗时）和 result.json（含文件清单与哈希）。
    """
    import soundfile

    audio = np.asarray(audio, dtype=np.float32)
    # 音频必须是双声道、非空且数值有限
    if (
        audio.ndim != 2
        or audio.shape[1] != 2
        or not len(audio)
        or not np.isfinite(audio).all()
    ):
        raise ValueError('解码音频必须为有效的双声道数组')

    # 先写入带随机后缀的临时文件，再原子替换为最终文件名，避免半成品残留
    tmp = folder / ('audio.' + uuid.uuid4().hex + '.tmp')
    try:
        soundfile.write(tmp, audio, 48000, subtype='PCM_24', format='FLAC')
        os.replace(tmp, folder / 'audio.flac')
    finally:
        # 无论成功与否都清理临时文件；成功时它已被 replace 移走，此处为无害的空操作
        tmp.unlink(missing_ok=True)

    job = jobs.read_job(folder)
    meta = {
        **params,
        'created': job['created'],
        'duration': len(audio) / 48000,
        'generation_seconds': timing['e2e_seconds'],
        'timing': timing,
        'truncated': truncated,
        'actual_decode_options': actual_decode or {},
    }
    jobs.write_json(folder / 'meta.json', meta)

    # 从既有产物清单合并新增文件，再逐个统计字节数与哈希
    artifacts = set(job['artifacts']) | {'audio.flac', 'meta.json'}
    artifacts = {
        name: {
            'bytes': (folder / name).stat().st_size,
            'sha256': jobs.sha256(folder / name),
        }
        for name in artifacts
    }

    jobs.write_json(
        folder / 'result.json',
        {
            'status': 'complete',
            'identity': jobs.identity({'params': params, 'config': config}),
            'sample_rate': 48000,
            'audio_seconds': len(audio) / 48000,
            'timing': timing,
            'truncated': truncated,
            'weights': config.get('weights', {}),
            'artifacts': artifacts,
            'actual_decode_options': actual_decode or {},
        },
    )
    jobs.commit_stage(folder, 'audio', ['audio.flac', 'meta.json', 'result.json'])


def execute_job(engine, ctx, folder, *, stop_after='audio'):
    """Continue the saved snapshot; caller-provided changes cannot alter cached stages.

    在任务租约保护下执行整个生成流程：租约确保同一任务不会被并发重复执行，
    内部交由 :func:`_execute_job` 按阶段推进，``stop_after`` 控制提前停在 plan 阶段。
    """
    with jobs.job_lease(folder):
        return _execute_job(engine, ctx, folder, stop_after=stop_after)


def _prepare_decode(ctx, folder, params, runtime, code_hash):
    """Importing external latents is itself a resumable draft-to-latent stage.

    从外部已保存的 .npy 潜变量文件导入：校验来源文件哈希、规范化潜变量、
    写入 latent/config/request 等文件，并直接提交到 latent 阶段。
    """
    source_latent = Path(params['source_latent'])
    if not source_latent.is_file():
        raise FileNotFoundError('重新解码的来源潜变量不存在，请恢复原文件后继续')

    expected_sha256 = params.get('source_latent_sha256')
    actual_sha256 = jobs.sha256(source_latent)
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise ValueError('重新解码的来源潜变量已改变，请还原源文件或新建解码任务')

    latent = check_latents(np.load(source_latent, allow_pickle=False))
    # 加载后再次比对哈希，防止读取过程中文件被并发改写
    if jobs.sha256(source_latent) != actual_sha256:
        raise ValueError('读取期间来源潜变量已改变，请重试')

    _cancel(ctx)

    # 解码器身份缺失时，现场加载 yue2 并计算解码器权重指纹，用于可恢复任务的一致性校验
    decode_vae_identity = runtime.get('decode_vae_identity')
    if decode_vae_identity is None:
        compat.ensure_yue2()
        from .paths import MODELS
        from yue2.storage import model_identity

        decode_vae_identity = model_identity(
            MODELS / runtime['vae'], verify=bool(runtime.get('verify_hashes'))
        )

    config = {
        'studio_decode': decode_options(params.get('decode_options')),
        'weights': {'vae': decode_vae_identity},
        'studio_runtime_sha256': code_hash,
    }

    jobs.write_array(folder / 'latent.npy', latent)
    jobs.write_json(folder / 'latent.json', {'seconds': 0})
    jobs.write_json(folder / 'config.json', config)
    jobs.write_json(folder / 'request.json', params)

    files = ['latent.npy', 'latent.json', 'config.json', 'request.json']
    if params.get('abc'):
        (folder / 'score.abc').write_text(params['abc'], encoding='utf-8')
        files.append('score.abc')

    jobs.commit_stage(folder, 'latent', files)
    _checkpoint(ctx, folder)
    return config


def _execute_job(engine, ctx, folder, *, stop_after='audio'):
    # 把文件夹路径统一转为 Path，便于后续拼接与读取
    folder = Path(folder)
    job = jobs.read_job(folder)
    if stop_after not in ('plan', 'audio'):
        raise ValueError('未知停止阶段')
    # 已经到达音频阶段则直接返回结果，无需重跑
    if job['stage'] == 'audio':
        return jobs.job_result(folder)

    params = job['params']
    stage = job['stage']
    runtime = job['runtime']
    started = time.monotonic()
    elapsed_before = job.get('elapsed_seconds', 0)

    # yue 与 gen_cfg 在进入生成分支后才赋值，用于 finally 中恢复全局状态
    yue = None
    gen_cfg = None
    config = None

    jobs.set_status(folder, 'running')
    try:
        _cancel(ctx)
        code_hash = compat.runtime_identity()

        # 非草稿阶段需要复用已保存的 config，并校验推理版本未变化
        if stage != 'draft':
            config = _json(folder, 'config.json')
            if config.get('studio_runtime_sha256') != code_hash:
                raise ValueError(
                    '保存任务的推理版本与当前版本不同，请另建任务；已有 latent 可单独重新解码'
                )

        # 纯解码任务：从外部潜变量导入并直接进入 latent 阶段
        if job['kind'] == 'decode' and stage == 'draft':
            config = _prepare_decode(ctx, folder, params, runtime, code_hash)
            stage = 'latent'

        # 整首歌任务：依次推进 draft -> plan -> semantic -> latent 阶段
        if job['kind'] == 'song' and stage in ('draft', 'plan', 'semantic'):
            pipeline = compat.ensure_yue2()
            from yue2.protocol import SongRequest, resolve_sampling

            yue = engine.get_yue2(ctx, runtime=runtime)
            gen_cfg = yue.generation_config
            # 用任务参数覆盖 ode_steps，其余生成配置保持原样
            yue.generation_config = dataclasses.replace(
                gen_cfg, ode_steps=int(params.get('ode_steps', 32))
            )

            abc_sampling = resolve_sampling(
                params.get('abc_sampling'), yue.generation_config.abc
            )
            semantic_sampling = resolve_sampling(
                params.get('semantic_sampling'), yue.generation_config.semantic
            )

            request = SongRequest(
                params['style'],
                params['lyrics'],
                params.get('cot', 'full'),
                int(params['seed']),
                # cot 关闭时不提供 ABC 输入，其余情况下把 falsy 值规范化为 None
                (params.get('abc') or None)
                if params.get('cot') != 'off'
                else None,
                params.get('cfg_scale'),
            )

            if stage == 'draft':
                # 从采样参数推导完整生成配置，并记录权重与运行时指纹
                config = yue.effective_config(request, abc_sampling, semantic_sampling)
                config['weights'] = yue.weights
                config['studio_runtime_sha256'] = code_hash
                decode_opts = decode_options(
                    params.get('decode_options'), default_core=yue.vae_core_frames
                )
                config.update(
                    studio_decode=decode_opts,
                    vae_decode='full' if decode_opts['mode'] == 'full' else 'halo_crop',
                    vae_core_frames=decode_opts['core_frames'],
                    vae_halo_frames=decode_opts['context_frames'],
                )
                jobs.write_json(folder / 'config.json', config)
                jobs.write_json(folder / 'request.json', request.to_dict())
            else:
                # 复用旧阶段前必须确认模型权重与推理版本一致
                config = _json(folder, 'config.json')
                if (
                    config.get('weights') != yue.weights
                    or config.get('runtime_sha256') != yue.runtime_sha256
                ):
                    raise ValueError('模型权重或推理版本发生变化，不能复用旧阶段，请另建任务')

            # 把 yue2 内部的终端进度重定向到 GUI 回调，键 1/2 分别对应 abc/semantic 阶段上限
            compat.progress_sink = engine._yue2_sink(
                ctx, '', {1: abc_sampling.max_tokens, 2: semantic_sampling.max_tokens}
            )

            # 在受限显存预算内执行各生成阶段
            with engine._cuda_budget_scope(yue.device, engine._yue2_fraction):
                if stage == 'draft':
                    plan = yue.plan(
                        request=request,
                        abc_sampling=abc_sampling,
                        cancelled=ctx.cancelled,
                    )
                    plan.save(folder)
                    files = set(jobs.PLAN_FILES) | {'config.json', 'request.json'}
                    if plan.abc is not None:
                        files.add('score.abc')
                    jobs.commit_stage(folder, 'plan', files)
                    stage = 'plan'
                    _checkpoint(ctx, folder)
                else:
                    plan = pipeline.SymbolicPlan.load(folder)

                if stop_after == 'plan':
                    jobs.set_status(
                        folder,
                        'ready',
                        elapsed=elapsed_before + time.monotonic() - started,
                    )
                    return jobs.job_result(folder)

                _cancel(ctx)

                if stage == 'plan':
                    semantic = yue.generate_semantic(
                        plan, sampling=semantic_sampling, cancelled=ctx.cancelled
                    )
                    tokens = np.asarray(semantic.tokens, dtype=np.int32)
                    if not len(tokens):
                        raise ValueError('歌曲 tokens 为空，请调整采样参数后新建任务')
                    jobs.write_array(folder / 'semantic.npy', tokens)
                    jobs.write_json(
                        folder / 'semantic.json',
                        {'timing': semantic.timing, 'truncated': semantic.truncated},
                    )
                    jobs.commit_stage(folder, 'semantic', ['semantic.npy', 'semantic.json'])
                    stage = 'semantic'
                    _checkpoint(ctx, folder)
                else:
                    # 从已保存的 semantic.npy 恢复，并校验 tokens 的维度与取值范围
                    tokens = np.load(folder / 'semantic.npy', allow_pickle=False)
                    if (
                        tokens.ndim != 1
                        or tokens.dtype.kind not in 'iu'
                        or not len(tokens)
                        or np.any(tokens < 0)
                        or np.any(tokens >= 32768)
                    ):
                        raise ValueError('保存的歌曲 tokens 格式错误')
                    semantic_info = _json(folder, 'semantic.json')
                    semantic = pipeline.SemanticResult(
                        plan, tokens.tolist(), semantic_info['timing'], semantic_info['truncated']
                    )

                _cancel(ctx)
                nar_started = time.monotonic()
                # 声学合成得到潜变量，并落盘为 latent.npy / latent.json
                latent = check_latents(yue.synthesize(semantic, cancelled=ctx.cancelled))
                jobs.write_array(folder / 'latent.npy', latent)
                jobs.write_json(folder / 'latent.json', {'seconds': time.monotonic() - nar_started})
                jobs.commit_stage(folder, 'latent', ['latent.npy', 'latent.json'])
                stage = 'latent'
                _checkpoint(ctx, folder)

            # 生成阶段结束，清空进度回调
            compat.progress_sink = None

        _cancel(ctx)
        # 走到这里必须是 latent 阶段，否则任务缺少可继续的内容
        if stage != 'latent':
            raise ValueError('任务缺少可继续的阶段')

        # config 可能因纯解码路径尚未赋值，这里兜底读取一次
        config = config or _json(folder, 'config.json')
        latent = check_latents(np.load(folder / 'latent.npy', allow_pickle=False))

        decode_started = time.monotonic()
        audio = engine.decode_latents(
            ctx,
            latent,
            runtime=runtime,
            options=params.get('decode_options') or {'mode': 'auto'},
            expected_vae=config.get('weights', {}).get('vae'),
        )
        _cancel(ctx)

        # 读取各阶段的时间与截断信息，拼装完整 timing / truncated 结构
        plan_info = _json(folder, 'plan.json') if (folder / 'plan.json').exists() else {}
        semantic_info = (
            _json(folder, 'semantic.json') if (folder / 'semantic.json').exists() else {}
        )
        timing = {
            'abc': plan_info.get('timing', {}),
            'semantic': semantic_info.get('timing', {}),
            'nar_seconds': _json(folder, 'latent.json').get('seconds', 0),
            'vae_seconds': time.monotonic() - decode_started,
            'e2e_seconds': elapsed_before + time.monotonic() - started,
        }
        truncated = {
            'abc': bool(plan_info.get('truncated')),
            'semantic': bool(semantic_info.get('truncated')),
        }

        _finish_audio(
            folder,
            audio,
            params,
            config,
            timing,
            truncated,
            getattr(engine, 'last_decode_options', None),
        )
        jobs.set_status(folder, 'complete', elapsed=timing['e2e_seconds'])
        _checkpoint(ctx, folder)
        return jobs.job_result(folder)
    except BaseException as exc:
        # 中断记为 cancelled，其余异常记为 failed，并附上累计耗时
        jobs.set_status(
            folder,
            'cancelled' if isinstance(exc, InterruptedError) else 'failed',
            str(exc),
            elapsed=elapsed_before + time.monotonic() - started,
        )
        _checkpoint(ctx, folder)
        raise
    finally:
        # 无论成功、失败还是取消，都恢复生成配置并清空全局进度回调
        if yue is not None and gen_cfg is not None:
            yue.generation_config = gen_cfg
        compat.progress_sink = None
