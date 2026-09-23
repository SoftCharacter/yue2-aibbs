"""Audio decoding helpers built on the bundled ffmpeg and soundfile."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

# 文件对话框中的音频文件过滤器
AUDIO_FILTER = '音频文件 (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus *.wma *.mp4 *.mkv);;所有文件 (*)'
# 支持的音频/视频扩展名集合
AUDIO_EXTS = {
    '.aac', '.m4a', '.mkv', '.mp3', '.mp4', '.ogg', '.wav', '.wma',
    '.flac', '.opus', '.webm',
}
# Windows 下创建子进程时隐藏控制台窗口（CREATE_NO_WINDOW 标志），其它平台为 0
_NO_WINDOW = 134217728 if sys.platform == 'win32' else 0


def ffmpeg_path():
    """在 PATH 中查找 ffmpeg 可执行文件，找不到返回 None。"""
    return shutil.which('ffmpeg')


def run_hidden(command, **kwargs):
    """以隐藏窗口方式运行子进程，避免 ffmpeg 弹出黑色控制台。"""
    return subprocess.run(command, creationflags=_NO_WINDOW, **kwargs)


def decode_audio(path, sample_rate=24000, mono=True, max_seconds=None) -> np.ndarray:
    """Decode any ffmpeg-readable file to float32 [samples] (mono) or [samples, channels]."""
    if not ffmpeg_path():
        raise RuntimeError('找不到 ffmpeg，请确认 env/ffmpeg/bin 存在')

    # 组装 ffmpeg 解码命令：只取音频、限定时长、指定声道与采样率、输出原始 f32
    cmd = ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(Path(path).resolve()), '-vn']
    if max_seconds:
        cmd += ['-t', str(float(max_seconds))]
    cmd += ['-ac', '1' if mono else '2', '-ar', str(sample_rate), '-f', 'f32le', 'pipe:1']

    proc = run_hidden(cmd, capture_output=True, timeout=900)
    if proc.returncode:
        raise ValueError('无法解码音频: ' + proc.stderr.decode(errors='replace')[-800:])

    # 将原始字节按小端 32 位浮点解析，单声道返回一维数组，立体声返回 [N, 2]
    samples = np.frombuffer(proc.stdout, dtype='<f4').copy()
    return samples if mono else samples.reshape(-1, 2)


def waveform_peaks(path, buckets=1600):
    """Return (peaks[buckets] in 0..1, duration seconds) for drawing."""
    data, sr = None, None
    try:
        import soundfile
        # 始终按二维读取，再对声道取最大得到单声道峰值包络
        data, sr = soundfile.read(str(path), dtype='float32', always_2d=True)
        data = np.abs(data).max(axis=1)
    except Exception:
        # 无法用 soundfile 读取时回退到 ffmpeg 解码
        sr = 8000
        data = np.abs(decode_audio(path, sr))

    duration = len(data) / sr if sr else 0
    if len(data) == 0:
        return (np.zeros(buckets, dtype=np.float32), 0)

    # 样本数足够时按块取最大值，否则用插值重采样到目标桶数
    keep = len(data) - len(data) % buckets if len(data) >= buckets else len(data)
    if len(data) >= buckets:
        peaks = data[:keep].reshape(buckets, -1).max(axis=1)
    else:
        peaks = np.interp(
            np.linspace(0, len(data) - 1, buckets), np.arange(len(data)), data
        )

    # 归一化到 0..1，峰值恒为 0 时退化为 1 避免除零
    peak = float(peaks.max()) or 1
    return ((peaks / peak).astype(np.float32), duration)


def media_duration(path):
    """读取音频时长（秒），失败时返回 None。"""
    try:
        import soundfile
        return soundfile.info(str(path)).duration
    except Exception:
        return None
