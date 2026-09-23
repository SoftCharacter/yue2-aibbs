"""Small JSON-backed settings store."""
from __future__ import annotations

import copy
import json
import os

from .paths import SETTINGS_FILE

# 默认设置：写入文件时会与用户实际配置合并
DEFAULTS = {
    'theme': 'light',
    'device': 'auto',
    'memory_budget_gib': 0,
    'backend': 'torch',
    'attention': 'auto',
    'vae': 'YuE2-Vae',
    'verify_hashes': False,
    'exclusive_vram': True,
    'output_dir': '',
    'mp3_bitrate': '320k',
    'drafts': {},
    'llm': {},
    'llm_recent': [],
}


class Settings(dict):
    """基于 JSON 文件的设置存储，继承 dict 以保持访问便利。"""

    def __init__(self):
        # 先用默认值的深拷贝初始化，避免修改默认值对象本身
        super().__init__(copy.deepcopy(DEFAULTS))
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            for key, value in loaded.items():
                if key in DEFAULTS:
                    default = DEFAULTS[key]
                    # 数值类型放宽：默认值与目标值均为非 bool 数值时直接接受
                    numeric_ok = (
                        isinstance(default, (int, float))
                        and not isinstance(default, bool)
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)
                    )
                    # 类型一致（或满足数值放宽条件）才接受该键
                    if numeric_ok or isinstance(value, type(default)):
                        self[key] = value

    def save(self):
        # 先写临时文件再原子替换，避免写一半崩溃损坏设置
        try:
            tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + '.tmp')
            tmp.write_text(
                json.dumps(self, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            os.replace(tmp, SETTINGS_FILE)
        except OSError as e:
            print(f'[YuE2 Studio] 保存设置失败: {e}')


# 模块级单例，供全应用共享
settings = Settings()
