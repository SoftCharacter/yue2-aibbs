"""Repair word boundaries after single-audio ASR without altering vendor alignment."""
from __future__ import annotations

# 这些语言在词与词之间本就不加空格，修复边界时需跳过空格插入
_UNSPACED_LANGUAGES = {'thai', 'chinese', 'japanese', 'cantonese'}


def transcribe_single(model, **kwargs):
    """Return one transcription, preserving original chunk text and timestamp offsets.

    Capture the vendor's raw chunk outputs while it performs its normal alignment.
    Only replace final text when its exact unseparated concatenation is confirmed.
    """
    # 取出模型内部的底层 ASR 推理方法，若不存在则退回普通转写
    infer_asr = getattr(model, '_infer_asr', None)
    if not callable(infer_asr):
        return model.transcribe(**kwargs)[0]

    # 用哨兵记录原 _infer_asr 属性，便于 finally 中精确还原
    sentinel = object()
    original = vars(model).get('_infer_asr', sentinel)
    chunks = []

    def capture(contexts, wavs, languages):
        """调用原推理方法，同时把每段 (上下文, 语言) 捕获到外层列表。"""
        out = infer_asr(contexts, wavs, languages)
        chunks.extend(zip(out, languages))
        return out

    # 临时替换 _infer_asr，让转写过程中顺带记录分块上下文
    model._infer_asr = capture
    try:
        results = model.transcribe(**kwargs)
    finally:
        # 无论成功与否，还原 _infer_asr 属性
        if original is sentinel:
            del model._infer_asr
        else:
            model._infer_asr = original

    if len(results) != 1:
        raise ValueError('transcribe_single 仅支持一个音频输入')

    result = results[0]
    # 少于两段说明没有跨块边界，无需修复
    if len(chunks) < 2:
        return result

    from qwen_asr.inference.utils import parse_asr_output
    # 解析每一段的 (语言, 文本)，解析时显式传入该段的语言
    parsed = [parse_asr_output(ctx, user_language=lang) for ctx, lang in chunks]

    # 仅当各段文本的无分隔拼接与原文本完全一致时，才进行边界修复
    if ''.join(text for _, text in parsed) != result.text:
        return result

    accumulated = ''
    prev_lang = None
    for lang, text in parsed:
        if not text:
            continue
        lang = (lang or '').strip().lower()
        # 需要补空格：已有内容、末尾无空格、当前开头无空格，且两侧语言不全是无空格语言
        if (
            accumulated
            and not accumulated[-1].isspace()
            and not text[0].isspace()
            and (lang not in _UNSPACED_LANGUAGES or prev_lang not in _UNSPACED_LANGUAGES)
        ):
            accumulated += ' '
        accumulated += text
        prev_lang = lang

    result.text = accumulated
    return result
