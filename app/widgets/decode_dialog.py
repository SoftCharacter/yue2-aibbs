"""Choose how to render saved YuE2 latents into another audio file.

本模块提供"重新解码"对话框：把已经保存的 YuE2 潜变量用本地解码器重新渲染成
新的音频文件（复用已完成的声学合成，仅运行音频解码器），结果保存为新作品。
用户可在此选择本地解码器变体并调整解码设置。
"""
from pathlib import Path

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QVBoxLayout

from ..engine import vae_choices
from ..settings import settings
from .common import form_row, label
from .song_forms import DecodeSettings


class DecodeDialog(QDialog):
    """重新解码对话框：选择解码器与解码选项，把保存的潜变量渲染成新音频。"""

    def __init__(self, latent, parent=None):
        super().__init__(parent)
        # 把潜变量路径统一转成字符串存为实例属性，供提交解码任务时使用
        self.latent = str(Path(latent))
        self.setWindowTitle('重新解码音频')
        self.resize(620, 340)

        # 垂直布局承载：说明标签、解码器下拉框、解码设置与按钮组
        box = QVBoxLayout(self)
        box.addWidget(label(f'潜变量：{self.latent}', wrap=True))
        box.addWidget(label('复用已完成的声学合成，仅运行音频解码器。结果保存为新作品，可在作品库播放。', wrap=True))

        # 本地解码器下拉框：遍历可用变体，文本与数据均为变体名
        self.vae = QComboBox()
        for name in vae_choices():
            self.vae.addItem(name, name)
        # 按上次设置（默认 YuE2-Vae）定位选中项，找不到则保持默认第一项
        idx = self.vae.findData(settings.get('vae', 'YuE2-Vae'))
        if idx >= 0:
            self.vae.setCurrentIndex(idx)
        box.addWidget(form_row('本地解码器', self.vae))

        # 解码设置面板，默认开启"重新解码"开关
        self.decode = DecodeSettings()
        self.decode.toggle.setChecked(True)
        box.addWidget(self.decode)

        # 确定/取消按钮组：确定按钮在无可用解码器时禁用
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('开始重新解码')
        buttons.button(QDialogButtonBox.Ok).setEnabled(self.vae.count() > 0)
        buttons.button(QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

    def values(self):
        """汇总对话框选项：潜变量路径、所选解码器与解码设置。"""
        return {
            'latent': self.latent,
            'vae': self.vae.currentData(),
            'decode_options': self.decode.values(),
        }
