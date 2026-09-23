"""Make the YuE2 inference package usable from this portable Windows folder.

* Imports ``yue2`` from the bundled wheel when it is not pip-installed.
* Accepts the renamed tokenizer file ``qwen.txt`` in place of ``qwen.tiktoken``.
* Windows PyTorch wheels ship without the built-in variable-length FlashAttention
  kernel that the CUDA-graph decoder picks by default, so the attention kernel
  is probed once and the first one that actually runs is used.
* Routes the package's terminal progress into a callback for the GUI.
"""
from __future__ import annotations

import io
import re
import sys
import threading
from pathlib import Path

from .paths import YUE2_DIR

# 模块级互斥锁，保证 ensure_yue2 只被完整初始化一次
_lock = threading.Lock()
# 是否已经完成 yue2 包的补丁注入
_patched = False
# 记录当前加载的 yue2 推理代码哈希，用于可恢复任务的一致性校验
_loaded_runtime_hash = None
# AR（自回归）解码器的注意力内核配置：requested 为用户/默认选择，resolved 为运行时探测结果
_attention = {'requested': 'auto', 'resolved': None}
# GUI 进度回调，由上层注入；None 表示尚未绑定
progress_sink = None
# yue2 包来源描述（pip 安装路径或 wheel 文件名），用于日志展示
yue2_source = ''


def _package_code_hash():
    """Hash actual Python sources for both wheel ZIP imports and pip installs."""
    import hashlib
    from importlib.resources import files

    # 对 yue2 包内的每个 .py 文件按“文件名 + 内容”计算综合哈希，作为运行时指纹
    digest = hashlib.sha256()
    count = 0
    for item in sorted(files('yue2').iterdir(), key=lambda item: item.name):
        if not item.name.endswith('.py'):
            continue
        if not item.is_file():
            continue
        digest.update(item.name.encode('utf-8') + b'\x00')
        digest.update(hashlib.sha256(item.read_bytes()).digest())
        count += 1
    if not count:
        # 若识别不到任何推理代码，无法可靠保存/恢复任务
        raise RuntimeError('无法识别 YuE2 推理代码，不能可靠保存可恢复任务')
    return digest.hexdigest()


def runtime_identity():
    """返回当前 yue2 运行时代码指纹，并在中途变化时抛错提醒重启。"""
    ensure_yue2()
    digest = _package_code_hash()
    if digest != _loaded_runtime_hash:
        # 推理文件在运行期间被改动，继续使用可能导致不一致
        raise ValueError('YuE2 推理文件在运行期间发生变化，请先重启界面')
    return digest


def _wheel_version(path: Path):
    """从 wheel 文件名中解析版本号，供多个候选 wheel 排序取最新。"""
    m = re.search(r'yue2_infer-([0-9.]+)-', path.name)
    if not m:
        return (0,)
    return tuple(int(part) for part in m.group(1).split('.'))


def ensure_yue2():
    """Import and patch yue2 once; returns the ``yue2.pipeline`` module."""
    global _patched, yue2_source, _loaded_runtime_hash

    # 加锁避免并发初始化；已初始化则直接返回 pipeline 模块
    with _lock:
        if _patched:
            import yue2.pipeline as pipeline
            return pipeline

        # 优先尝试 pip 安装的 yue2，失败则回退到便携目录下的 wheel
        try:
            import yue2
            yue2_source = f'pip: {Path(yue2.__file__).parent}'
        except ImportError:
            wheels = sorted(YUE2_DIR.glob('yue2_infer-*.whl'), key=_wheel_version)
            if not wheels:
                raise ImportError(
                    '未找到 yue2 推理包：请把 yue2_infer-*.whl 放在 models/YuE2-3B 下，或 pip 安装它'
                )
            # 将版本最新的 wheel 路径插入 sys.path，使其可被 import
            sys.path.insert(0, str(wheels[-1]))
            import yue2
            yue2_source = f'wheel: {wheels[-1].name}'

        import yue2.pipeline as pipeline
        import yue2.cuda_graph as cuda_graph
        from yue2.tokenization_yue2 import YuE2TextTokenizer
        from yue2.progress import Progress

        # 便携包内 tokenizer 的 merge 文件被改名，这里按原名缺失时自动回退查找
        class FallbackTokenizer(YuE2TextTokenizer):
            def __init__(self, merge_file):
                merge_file = Path(merge_file)
                if not merge_file.exists():
                    # 依次尝试 .txt 后缀和固定的 qwen.tiktoken.txt 备用名
                    for candidate in (
                        merge_file.with_suffix('.txt'),
                        merge_file.parent / 'qwen.tiktoken.txt',
                    ):
                        if candidate.exists():
                            merge_file = candidate
                            break
                super().__init__(merge_file)

        # 将 yue2 的终端进度重定向为 GUI 回调，而非直接写终端
        class GuiProgress(Progress):
            def __init__(self, enabled=True, stream=None, refresh_interval=0.25):
                # 忽略传入的 stream，统一使用内存缓冲，避免污染终端
                super().__init__(
                    enabled=enabled, stream=io.StringIO(), refresh_interval=refresh_interval
                )
                self._interval = 0.25

            def _write(self, text, final=False):
                # 覆盖基类的终端写出为空操作，进度改由 _render 上报
                pass

            def _render(self, stage, now, status=None, force=False):
                # 未启用，或距上次渲染不足一个节流间隔且非强制，则跳过
                if not self.enabled or (
                    not force and (now - stage._last_render) < self._interval
                ):
                    return
                stage._last_render = now
                sink = progress_sink
                if sink is not None:
                    # 上报当前阶段的标签、进度、单位与耗时
                    elapsed = max(0, now - stage._started)
                    sink(
                        {
                            'label': stage.label,
                            'completed': stage.completed,
                            'total': stage.total,
                            'unit': stage.unit,
                            'elapsed': elapsed,
                            'status': status,
                        }
                    )

            def complete(self, audio_seconds, elapsed, *, truncated=False):
                sink = progress_sink
                if sink is not None:
                    sink(
                        {
                            'label': 'Complete',
                            'audio_seconds': audio_seconds,
                            'elapsed': elapsed,
                            'truncated': truncated,
                            'status': 'completed',
                        }
                    )

        # 保存原始 GraphAR.__init__，用于在自定义初始化中注入注意力内核选择
        _orig_graph_ar_init = cuda_graph.GraphAR.__init__

        def graph_init(self, *args, attention_backend='auto', **kwargs):
            # 内核为 auto 时，先探测一次可用的注意力实现
            if attention_backend == 'auto':
                attention_backend = resolve_attention()
            return _orig_graph_ar_init(
                self, *args, attention_backend=attention_backend, **kwargs
            )

        # 用补丁类替换 pipeline 中的 tokenizer 与进度上报
        pipeline.YuE2TextTokenizer = FallbackTokenizer
        pipeline.Progress = GuiProgress
        cuda_graph.GraphAR.__init__ = graph_init

        # 修复声学合成（NAR）在 Windows 下的显存占用问题
        _patch_nar_attention()
        _loaded_runtime_hash = _package_code_hash()
        _patched = True
        return pipeline


def ensure_qwen_asr():
    """Import qwen_asr (pip or app/vendor) without its heavy optional dependencies.

    The package only uses librosa to load/resample audio and nagisa to split
    Japanese words. We always hand it 16 kHz arrays decoded by ffmpeg, and fall
    back to per-character splitting for Japanese when nagisa is missing.
    """
    import importlib.machinery
    import types
    import transformers

    # 便携目录下 vendor 子目录存放 qwen_asr 的本地实现
    vendor = Path(__file__).resolve().parent / 'vendor'

    # 轻量替代 librosa.resample，避免引入完整音频处理栈
    def resample(y, orig_sr, target_sr, **_):
        import torch
        from torchaudio import functional
        return functional.resample(
            torch.as_tensor(y, dtype=torch.float32), int(orig_sr), int(target_sr)
        ).numpy()

    # 轻量替代 librosa.load，直接走 ffmpeg 解码的 16 kHz 单声道
    def load(path, sr=None, mono=True, **_):
        from .audio_utils import decode_audio
        target_sr = int(sr or 16000)
        return decode_audio(path, target_sr), target_sr

    try:
        import qwen_asr
        return qwen_asr
    except ImportError:
        # 为缺失的可选依赖注入最小化的桩实现
        for name in ('librosa', 'nagisa'):
            try:
                __import__(name)
            except ImportError:
                module = types.ModuleType(name)
                module.__spec__ = importlib.machinery.ModuleSpec(name, None)
                if name == 'librosa':
                    # librosa 桩提供 resample/load 两个接口
                    module.resample = resample
                    module.load = load
                else:
                    # nagisa 桩退化为按字符切分（去空白）
                    module.tagging = lambda text: types.SimpleNamespace(
                        words=[ch for ch in text if not ch.isspace()]
                    )
                sys.modules[name] = module

        # 确保 vendor 目录在 import 路径中，再导入本地的 qwen_asr
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        import qwen_asr
        return qwen_asr


def decode_sheetsage_paper_audio(source):
    """Read native-rate channels and average them for SheetSage2's waveform input.

    Its own loader still performs torchaudio resampling before trimming, as in the paper
    path. No global torchaudio APIs or persistent audio caches are replaced. Different
    FFmpeg builds need not be bit-identical to the original benchmark environment.
    """
    import json
    import numpy
    import torch
    from .audio_utils import run_hidden

    source = str(Path(source).resolve())

    # 先用 ffprobe 读取采样率与声道数
    info = run_hidden(
        [
            'ffprobe', '-v', 'error', '-select_streams', 'a:0',
            '-show_entries', 'stream=sample_rate,channels', '-of', 'json', source,
        ],
        capture_output=True, timeout=120,
    )
    if info.returncode:
        raise RuntimeError('无法读取音频信息: ' + info.stderr.decode(errors='replace')[-500:])

    streams = json.loads(info.stdout or b'{}').get('streams') or []
    if not streams:
        raise ValueError('文件中没有可读取的音轨')

    sample_rate = int(streams[0]['sample_rate'])
    channels = int(streams[0]['channels'])

    # 用 ffmpeg 将音频解码为 32 位浮点原始 PCM
    decoded = run_hidden(
        [
            'ffmpeg', '-v', 'error', '-nostdin', '-i', source, '-vn',
            '-map', '0:a:0', '-f', 'f32le', '-acodec', 'pcm_f32le', 'pipe:1',
        ],
        capture_output=True, timeout=900,
    )
    if decoded.returncode:
        raise RuntimeError('无法解码音频: ' + decoded.stderr.decode(errors='replace')[-500:])

    # 将原始字节按声道重塑为二维数组，再对所有声道取平均得到单声道波形
    audio = numpy.frombuffer(decoded.stdout, dtype='<f4').reshape(-1, channels).copy()
    return torch.from_numpy(audio).mean(dim=1), sample_rate


# NAR 注意力分块计算时的每块帧数（与官方 query tiling 一致）
NAR_QUERY_CHUNK = 1024


def _patch_nar_attention():
    """Keep acoustic synthesis (NAR) attention memory linear on Windows.

    yue2.nar calls plain SDPA with grouped-query heads. Windows PyTorch wheels have no FlashAttention and the
    memory-efficient kernel rejects GQA, so SDPA silently falls back to the MATH kernel and materialises the full
    [16 heads x frames x (prefix + frames)] attention matrix: +7 GiB for a 4-minute song, +14 GiB at the 6-minute
    limit, which is why a 24 GB card could run out of memory in the last stage. cuDNN attention does the same
    computation without that matrix; when it is unavailable, the official query tiling bounds the matrix instead.
    """
    import yue2.nar as nar

    # 已补丁则跳过，避免重复包裹
    if getattr(nar.attention, '_studio_patched', False):
        return

    # 保存原始注意力实现，供补丁函数转发调用
    _orig_attention = nar.attention

    def attention(q, k, v, *, causal=False, backend='sdpa', query_chunk_size=None):
        # 非默认路径（指定后端、非 sdpa 或非 CUDA）直接走原始实现
        if backend != 'sdpa' or query_chunk_size is not None or q.device.type != 'cuda':
            return _orig_attention(
                q, k, v, causal=causal, backend=backend, query_chunk_size=query_chunk_size
            )

        resolved = resolve_nar_attention()
        if resolved == 'cudnn':
            try:
                from torch.nn.attention import SDPBackend, sdpa_kernel
                # 强制使用 cuDNN 内核，避免 MATH 内核物化完整注意力矩阵
                with sdpa_kernel(SDPBackend.CUDNN_ATTENTION):
                    return _orig_attention(q, k, v, causal=causal, backend='sdpa')
            except RuntimeError as e:
                # 仅 OOM 才降级到分块；其他运行时错误照常抛出
                if 'out of memory' not in str(e).lower():
                    raise
                _nar_attention['resolved'] = 'chunked'
                resolved = 'chunked'
                print(
                    '[YuE2 Studio] 声学合成 cuDNN 注意力不可用，改为分块计算：'
                    + str(e).splitlines()[0][:120]
                )

        if resolved == 'chunked':
            # 分块计算，将注意力矩阵峰值限制在单块大小
            return _orig_attention(
                q, k, v, causal=causal, backend='sdpa', query_chunk_size=NAR_QUERY_CHUNK
            )

        return _orig_attention(q, k, v, causal=causal, backend='sdpa')

    attention._studio_patched = True
    _orig_attention._studio_original = attention
    nar.attention = attention


def resolve_nar_attention() -> str:
    """'native' (PyTorch has FlashAttention, as on the official Linux setup), 'cudnn' or 'chunked'."""
    # 已探测过则直接返回缓存结果
    if _nar_attention['resolved']:
        return _nar_attention['resolved']

    import torch
    if torch.backends.cuda.is_flash_attention_available():
        # 官方 Linux 环境具备内置 FlashAttention
        resolved = 'native'
    elif _attention['requested'] == 'sdpa':
        # 用户显式要求 sdpa 时，Windows 上只能退化为分块
        resolved = 'chunked'
    else:
        # 否则尝试 cuDNN 内核，失败则回退到分块
        try:
            _probe_nar_cudnn()
        except Exception:
            resolved = 'chunked'
        else:
            resolved = 'cudnn'

    _nar_attention['resolved'] = resolved
    print('[YuE2 Studio] 声学合成注意力: ' + NAR_ATTENTION_NAMES[resolved])
    return resolved


# 三种 NAR 注意力实现的可读名称，用于日志展示
NAR_ATTENTION_NAMES = {
    'native': 'PyTorch 内置 FlashAttention',
    'cudnn': 'cuDNN（显存占用低）',
    'chunked': f'分块计算（每块 {NAR_QUERY_CHUNK} 帧）',
}

# NAR 注意力的运行时探测结果缓存
_nar_attention = {'resolved': None}


def _probe_nar_cudnn():
    """用小规模 GQA 张量实测 cuDNN 内核是否可用，任何异常都会向上抛出。"""
    import torch
    from torch.nn import functional
    from torch.nn.attention import SDPBackend, sdpa_kernel

    device = torch.device('cuda', torch.cuda.current_device())
    # 构造分组查询注意力所需的 q/k 形状（16 个查询头对 8 个键值头）
    q = torch.randn(1, 16, 12, 128, device=device, dtype=torch.bfloat16)
    k = torch.randn(1, 8, 20, 128, device=device, dtype=torch.bfloat16)

    with sdpa_kernel(SDPBackend.CUDNN_ATTENTION):
        # 两组探测分别覆盖非因果 GQA 与因果 GQA 场景
        functional.scaled_dot_product_attention(q, k, k, enable_gqa=True)
        functional.scaled_dot_product_attention(
            q, q[:, :8], q[:, :8], is_causal=True, enable_gqa=True
        )

    torch.cuda.synchronize(device)


def set_attention(value: str):
    """设置 AR 解码器期望的注意力后端，并清除已缓存的探测结果。"""
    _attention['requested'] = value or 'auto'
    _attention['resolved'] = None
    _nar_attention['resolved'] = None


def resolve_attention() -> str:
    """按优先级探测 AR 解码器可用的注意力内核，返回最终选定的内核名。"""
    if _attention['resolved']:
        return _attention['resolved']

    requested = _attention['requested']
    if requested == 'auto':
        # 自动模式下按 flash -> cudnn -> sdpa 的优先级依次探测
        candidates = ('flash', 'cudnn', 'sdpa')
    else:
        # 用户指定时先尝试其选择，失败再回退到 sdpa
        candidates = (requested, 'sdpa')

    for kind in candidates:
        try:
            _probe(kind)
        except Exception:
            continue
        _attention['resolved'] = kind
        break
    else:
        _attention['resolved'] = 'sdpa'

    print('[YuE2 Studio] 注意力内核: ' + _attention['resolved'])
    return _attention['resolved']


def _probe(kind):
    """在 CUDA 上实测指定注意力内核能否真正运行，失败抛出异常由调用方处理。"""
    import torch
    from torch.nn import functional

    device = torch.device('cuda', torch.cuda.current_device())
    # 与真实解码相近的批量/头/序列长度，用于触发对应内核路径
    q = torch.randn(2, 16, 1, 128, device=device, dtype=torch.bfloat16)
    k = torch.randn(2, 8, 12, 128, device=device, dtype=torch.bfloat16)
    mask = torch.ones(2, 1, 1, 12, dtype=torch.bool, device=device)

    if kind == 'flash':
        # 变长 FlashAttention：将 k 展平为 [总帧数, 头数, 头维度]
        k_reshaped = k.transpose(1, 2).reshape(-1, 8, 128)
        torch.ops.aten._flash_attention_forward(
            q[:, :, 0].contiguous(),
            k_reshaped,
            k_reshaped,
            torch.tensor([0, 1, 2], dtype=torch.int32, device=device),
            torch.tensor([0, 12, 24], dtype=torch.int32, device=device),
            1,
            12,
            0,
            False,
            False,
            seqused_k=torch.tensor([12, 12], dtype=torch.int32, device=device),
        )
    elif kind == 'cudnn':
        from torch.nn.attention import SDPBackend, sdpa_kernel
        with sdpa_kernel(SDPBackend.CUDNN_ATTENTION):
            functional.scaled_dot_product_attention(q, k, k, attn_mask=mask, enable_gqa=True)
    else:
        # sdpa：使用内存高效/数学内核的普通 SDPA
        functional.scaled_dot_product_attention(q, k, k, attn_mask=mask, enable_gqa=True)

    torch.cuda.synchronize(device)
