"""围绕厂商 ASR API 添加取消钩子，但不改变其分块与时间偏移逻辑。"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack


@contextmanager
def _method(owner, name, replacement):
    """临时替换 ``owner.name`` 属性，退出上下文后还原为原值（或删除）。

    使用 ``object`` 作为哨兵值，以区分“原本就没有该属性”与“原值为 None”两种情况：
    前者退出时用 ``delattr`` 删除，后者用 ``setattr`` 还原。
    """
    sentinel = object
    original = vars(owner).get(name, sentinel)
    setattr(owner, name, replacement)
    try:
        yield
    finally:
        if original is sentinel:
            delattr(owner, name)
        else:
            setattr(owner, name, original)


@contextmanager
def cancellable_transcription(model, ctx):
    """在模型推理期间注入可取消的停止条件，退出时还原所有猴子补丁。

    思路：不修改厂商的转写流程本身，而是在调用其 ``generate`` 与 ``align``
    前后各执行一次 ``check``，把取消状态与 ``StoppingCriteria`` 挂钩——
    ``check`` 主动抛异常，``StopRequested`` 则在生成循环内优雅停止。
    """
    from transformers import StoppingCriteria, StoppingCriteriaList

    def check():
        """取消状态下直接抛出中断异常，终止正在进行的识别。"""
        if ctx.cancelled():
            raise InterruptedError('已取消歌词识别')

    class StopRequested(StoppingCriteria):
        """把取消状态暴露为 transformers 的停止条件，供生成循环逐步检查。"""

        def __call__(self, input_ids, scores, **kwargs):
            return ctx.cancelled()

    # ExitStack 统一管理多个临时方法替换，确保异常时也能全部还原
    with ExitStack() as stack:
        check()

        # 仅当后端为 transformers 时才替换 generate，注入停止条件列表
        if getattr(model, 'backend', None) == 'transformers':
            _generate = model.model.generate

            def wrapped_generate(*args, **kwargs):
                """包装 generate：前后各检查一次取消，并追加停止条件。"""
                check()
                # 保留调用方传入的停止条件，额外追加一个 StopRequested 实例
                kwargs['stopping_criteria'] = StoppingCriteriaList(
                    list(kwargs.pop('stopping_criteria', None) or []) + [StopRequested()]
                )
                result = _generate(*args, **kwargs)
                check()
                return result

            stack.enter_context(_method(model.model, 'generate', wrapped_generate))

        # 若存在强制对齐器，同样替换其 align 方法以支持取消
        aligner = getattr(model, 'forced_aligner', None)
        if aligner is not None:
            _align = aligner.align

            def wrapped_align(*args, **kwargs):
                """包装 align：前后各检查一次取消。"""
                check()
                result = _align(*args, **kwargs)
                check()
                return result

            stack.enter_context(_method(aligner, 'align', wrapped_align))

        # 把控制权交还给调用方，期间补丁保持生效
        yield
        # 恢复执行后再检查一次取消状态
        check()
