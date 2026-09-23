"""Project locations. Everything is resolved relative to the project root."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 项目根目录（相对本文件向上两级）
ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / 'app'
ASSETS = APP_DIR / 'assets'
MODELS = ROOT / 'models'
ENV = ROOT / 'env'
SETTINGS_FILE = ROOT / 'ui_settings.json'
YUE2_DIR = MODELS / 'YuE2-3B'
SHEETSAGE_DIR = MODELS / 'SheetSage2'
MERT_DIRS = {
    '30s': MODELS / 'MERT-v2-30s',
    'FullSong': MODELS / 'MERT-v2-FullSong',
}
ASR_DIR = MODELS / 'Qwen3-ASR-1.7B'
ALIGNER_DIR = MODELS / 'Qwen3-ForcedAligner-0.6B'


def _configure_webengine_network():
    """Use the newer Windows network query instead of failing legacy NS_NLA queries.

    Chromium retains this as an opt-in because some systems misreport connectivity.
    Honour an explicit --disable-features=EnableGetNetworkConnectivityHintAPI override.
    This changes only this process's WebEngine configuration, not Windows services.
    """
    # 仅针对较新的 Windows（>= 19041）启用网络连通性提示 API
    if sys.platform != 'win32' or sys.getwindowsversion().build < 19041:
        return
    feature = 'EnableGetNetworkConnectivityHintAPI'
    flag_key = 'QTWEBENGINE_CHROMIUM_FLAGS'
    flags = os.environ.get(flag_key, '')
    pattern = r'(?<!\S)--(enable|disable)-features=("[^"]*"|\'[^\']*\'|\S*)'
    matches = list(re.finditer(pattern, flags))
    # 若用户已显式配置该 feature（无论 enable/disable），则不干预
    for m in matches:
        toggles = m[2].strip('"\'').split(',')
        if any(re.split(r'[<:]', t.strip(), maxsplit=1)[0] == feature for t in toggles):
            return
    enabled = [m for m in matches if m[1] == 'enable']
    if enabled:
        # 在最后一个 --enable-features 条目中追加 feature
        m = enabled[-1]
        raw = m[2]
        quote = raw[0] if raw.startswith(('"', "'")) else ''
        body = raw[1:-1] if quote else raw
        new_val = quote + body + (',' if body else '') + feature + quote
        flags = flags[:m.start(2)] + new_val + flags[m.end(2):]
    else:
        # 没有 enable 条目则新增一条
        flags = (flags.rstrip() + ' --enable-features=' + feature).lstrip()
    os.environ[flag_key] = flags


def setup_environment():
    """Mirror the portable launcher so ffmpeg and the bundled CUDA runtime resolve."""
    _configure_webengine_network()
    # 将便携目录下的可执行目录加入 PATH，使 ffmpeg / CUDA 运行时可被解析
    dirs = [
        ENV / 'ffmpeg' / 'bin',
        ENV,
        ENV / 'Scripts',
        ENV / 'Doc',
        ENV / 'Doc' / 'bin',
    ]
    path = os.environ.get('PATH', '')
    extra = [str(d) for d in dirs if d.is_dir() and str(d) not in path]
    if extra:
        os.environ['PATH'] = os.pathsep.join(extra + [path])
    # 若存在 Doc 目录，则作为 CUDA_PATH（便携 CUDA 运行时）
    if (ENV / 'Doc').is_dir():
        os.environ.setdefault('CUDA_PATH', str(ENV / 'Doc'))
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')


def output_root(settings=None) -> Path:
    # 优先使用用户配置的输出目录，否则用项目 outputs 目录
    output_dir = (settings or {}).get('output_dir')
    return Path(output_dir) if output_dir else ROOT / 'outputs'


def new_run_dir(kind: str, label: str = '', settings=None) -> Path:
    # 生成形如 <时间戳>_<标签> 的运行目录，重名则追加序号
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    safe_label = ''.join(
        ch if ch.isalnum() or ch in '-_' else '_' for ch in label
    )[:40].strip('_')
    base = output_root(settings) / kind
    name = f'{ts}_{safe_label}' if safe_label else ts
    n = 1
    run_dir = base / name
    while run_dir.exists():
        n += 1
        run_dir = base / f'{name}-{n}'
    run_dir.mkdir(parents=True)
    return run_dir


def open_in_explorer(path):
    # 若路径不存在则逐级向上，直到找到存在的目录
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        if sys.platform == 'win32':
            if path.is_file():
                subprocess.Popen(f'explorer /select,"{path}"')
            else:
                os.startfile(str(path))
        else:
            subprocess.Popen(['xdg-open', str(path if path.is_dir() else path.parent)])
    except OSError as e:
        print(f'[YuE2 Studio] 无法打开 {path}: {e}')
