"""直接解码已保存的潜变量，不构建 YuE2 MOT 生成管线。"""
from __future__ import annotations

from contextlib import contextmanager, suppress

import gc
import math
from pathlib import Path

import numpy as np

from . import compat
from .paths import MODELS
from .settings import settings
from .workflow import decode_options


def _cancel(ctx):
    """在取消状态下抛出中断异常，提示已保存的潜变量可稍后继续。"""
    if ctx.cancelled():
        raise InterruptedError('已取消解码，已保存的潜变量可稍后继续')


@contextmanager
def _decoder_precision(torch):
    """采用官方 FP32 后端配置，并恢复进入前的进程全局状态。

    解码阶段要求确定性的 FP32 数值行为，因此在进入时关闭 cudnn benchmark、
    强制确定性、禁用 TF32 并设置为最高 matmul 精度；退出时逐项还原原值。
    """
    cudnn = torch.backends.cudnn
    matmul = torch.backends.cuda.matmul
    # 快照进入前的六项全局状态，按元组顺序对应后续还原
    saved = (
        cudnn.benchmark,
        cudnn.deterministic,
        matmul.allow_tf32,
        cudnn.allow_tf32,
        matmul.allow_fp16_reduced_precision_reduction,
        torch.get_float32_matmul_precision(),
    )
    cudnn.benchmark = False
    cudnn.deterministic = True
    matmul.allow_tf32 = False
    cudnn.allow_tf32 = False
    matmul.allow_fp16_reduced_precision_reduction = False
    torch.set_float32_matmul_precision('highest')
    try:
        yield
    finally:
        # 还原顺序与 saved 元组一致
        torch.set_float32_matmul_precision(saved[5])
        cudnn.benchmark = saved[0]
        cudnn.deterministic = saved[1]
        matmul.allow_tf32 = saved[2]
        cudnn.allow_tf32 = saved[3]
        matmul.allow_fp16_reduced_precision_reduction = saved[4]


def _budget(torch, device):
    """根据设置与显卡容量计算显存预算，返回 (预算 GiB, 预留比例)。

    预算为 0 表示使用整张显卡；CUDA 设备上还要强制保留官方要求的 2 GiB
    运行空间，并额外给出用于 ``_cuda_budget_scope`` 的显存占用比例。
    """
    budget = float(settings.get('memory_budget_gib') or 0)
    if not math.isfinite(budget) or budget < 0:
        raise ValueError('显存预算必须为非负有限数值，0 表示使用整张显卡')

    ratio = None
    if device.type == 'cuda':
        total = torch.cuda.get_device_properties(device).total_memory
        # 预算为 0 时按整卡容量折算，否则取预算与整卡容量的较小值
        budget = min(budget, total / 1073741824) if budget else total / 1073741824
        # 扣除 2 GiB 官方运行空间后的可用字节数
        usable = min((budget - 2) * 1073741824, total - 0x80000000)
        if usable <= 0:
            raise ValueError('解码显存预算需大于 2 GiB，以保留官方要求的 2 GiB 运行空间')
        ratio = min(usable / total, 1)
    elif not budget:
        # 非 CUDA 设备且预算未设置时，给一个宽松的默认值
        budget = 24
    return (budget, ratio)


def decode_latents(engine, ctx, latent, *, runtime=None, options=None, expected_vae=None) -> np.ndarray:
    """将 [T,64] 或 [1,64,T] 潜变量转为连续的 [N,2] float32 音频。

    runtime 固定 VAE 与设备；显存预算始终读取当前设置，以支持降低预算后恢复。
    本函数只加载本地 decoder_only VAE，调用完成后不缓存解码器。
    """
    _cancel(ctx)

    # engine.torch() 返回注入的 torch 模块，统一通过它访问张量与设备相关 API
    torch_mod = engine.torch()
    z = torch_mod.as_tensor(latent, dtype=torch_mod.float32, device='cpu')

    # [T,64] 转置并升维为 [1,64,T]，与 [1,64,T] 统一
    if z.ndim == 2 and z.shape[1] == 64:
        z = z.T.unsqueeze(0)
    if z.ndim != 3 or z.shape[0] != 1 or z.shape[1] != 64 or z.shape[2] < 1:
        raise ValueError('潜变量必须为非空的 [T,64] 或 [1,64,T] 数组')
    if not torch_mod.isfinite(z).all():
        raise ValueError('潜变量包含 NaN 或无穷值，无法解码')

    runtime = runtime or {}
    # 解码器目录优先取 runtime，其次取全局设置，最终回落默认名
    vae_dir = MODELS / Path(runtime.get('vae', settings.get('vae', 'YuE2-Vae')))
    if not (vae_dir / 'config.json').is_file():
        raise FileNotFoundError(f'没有找到本地 YuE2 解码器：{vae_dir}')

    device = torch_mod.device(engine.device(runtime))
    # cuda 设备未指定序号时，回落到当前设备
    if device.type == 'cuda' and device.index is None:
        device = torch_mod.device('cuda', torch_mod.cuda.current_device())

    budget, ratio = _budget(torch_mod, device)
    # 显存越小分块越保守：预算 <=12 GiB 时默认核心块 512 帧，否则 1024 帧
    opts = decode_options(options, default_core=512 if budget <= 12 else 1024)
    engine.last_decode_options = dict(opts)

    from .engine import YUE2_STEPS

    ctx.progress(step=4, steps=YUE2_STEPS, text='校验本地音频解码器…')
    compat.ensure_yue2()
    from yue2.modeling_vae import YuE2VAE
    from yue2.storage import model_identity

    # 校验当前解码器权重/配置与可恢复任务记录是否一致
    ident = model_identity(vae_dir, bool(runtime.get('verify_hashes', settings.get('verify_hashes'))))
    if expected_vae is not None and ident != expected_vae:
        raise ValueError('当前解码器权重或配置与保存任务不一致，请恢复原解码器后继续')

    _cancel(ctx)
    engine.offload_yue2()
    engine._exclusive('yue2')

    vae = None
    audio = None
    with _decoder_precision(torch_mod):
        with engine._cuda_budget_scope(device, ratio):
            ctx.progress(step=4, steps=YUE2_STEPS, text='加载音频解码器（仅解码部分，FP32）…')
            _cancel(ctx)
            vae = YuE2VAE.from_pretrained(
                vae_dir,
                decoder_only=True,
                device='cpu',
                dtype=torch_mod.float32,
                local_files_only=True,
            )
            _cancel(ctx)

            # 分块模式下校验上下文帧数是否满足解码器要求
            if opts['mode'] == 'tiled':
                required = vae.required_halo(opts['core_frames'])
                if opts['context_frames'] < required:
                    raise ValueError(
                        f'当前解码器的分块上下文至少需要 {required} 帧，'
                        f'当前设置为 {opts["context_frames"]} 帧；请调高上下文长度'
                    )

            vae.to(device)
            # 满量解码只走一次，分块解码则按核心帧数估算块数
            chunks = 1 if opts['mode'] == 'full' else (
                (z.shape[-1] + opts['core_frames'] - 1) // opts['core_frames']
            )

            def report(completed, total):
                """上报解码进度，并在前后各检查一次取消状态。"""
                _cancel(ctx)
                ctx.progress(
                    step=4, steps=YUE2_STEPS, text='解码音频…',
                    value=completed, maximum=total, detail=f'{completed}/{total} 块',
                )
                _cancel(ctx)

            report(0, chunks)
            with torch_mod.inference_mode():
                if opts['mode'] == 'full':
                    audio = vae.decode(z.to(device)).cpu()
                    report(1, 1)
                else:
                    audio = vae.decode_tiled(
                        z,
                        core_frames=opts['core_frames'],
                        halo_frames=opts['context_frames'],
                        output_device='cpu',
                        on_progress=report,
                    )
                _cancel(ctx)

                # 校验解码结果形状与数值，防止写出坏音频
                if audio.ndim != 3 or audio.shape[0] != 1 or audio.shape[1] != 2 or audio.shape[2] < 1:
                    raise ValueError('解码音频必须是非空的双声道数组')
                if not torch_mod.isfinite(audio).all():
                    raise ValueError('解码音频包含 NaN 或无穷值')

                # 去掉批次维、转成 [N,2]、夹紧到 [-1,1] 后转为 numpy
                result = audio[0].float().clamp(-1, 1).T.contiguous().cpu().numpy()

            # 解码完成后立即把 VAE 搬回 CPU 并释放引用，避免占用后续显存
            if vae is not None:
                with suppress(Exception):
                    vae.to('cpu')
            vae = None
            audio = None
            gc.collect()
            if device.type == 'cuda':
                torch_mod.cuda.empty_cache()

    return result
