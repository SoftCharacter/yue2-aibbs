"""Streaming chat client for the AI lyric features (prompts and message building live in lyrics_ai).

* OpenAI-compatible Chat Completions (DeepSeek, 通义千问, Kimi, 智谱, 硅基流动, OpenRouter, Ollama, LM Studio …)
  over plain HTTP with server-sent events.
* Anthropic Claude through the official ``anthropic`` Python SDK (only when it is installed).

本模块负责 AI 歌词相关功能的大模型流式对话客户端：统一 OpenAI 兼容接口（SSE 流式）
与 Anthropic 官方 SDK 两条通路，把逐块返回的文本通过 ``on_delta`` 回调上抛，并最终拼成完整结果。
不依赖 Qt，可被 GUI 各页面在后台线程中调用；提示词与请求内容组装见 lyrics_ai 模块。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress

# 预设服务商列表：(显示名, 协议, 接口地址, 默认模型)，用于设置页下拉框与一键填充
PRESETS = (
    ('DeepSeek', 'openai', 'https://api.deepseek.com/v1', 'deepseek-chat'),
    ('通义千问 (阿里云百炼)', 'openai', 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen-plus'),
    ('Kimi (月之暗面)', 'openai', 'https://api.moonshot.cn/v1', 'moonshot-v1-8k'),
    ('智谱 GLM', 'openai', 'https://open.bigmodel.cn/api/paas/v4', 'glm-4-flash'),
    ('硅基流动 SiliconFlow', 'openai', 'https://api.siliconflow.cn/v1', 'deepseek-ai/DeepSeek-V3'),
    ('火山方舟 (豆包)', 'openai', 'https://ark.cn-beijing.volces.com/api/v3', ''),
    ('OpenRouter', 'openai', 'https://openrouter.ai/api/v1', ''),
    ('OpenAI', 'openai', 'https://api.openai.com/v1', ''),
    ('Anthropic Claude（需安装 anthropic 包）', 'anthropic', 'https://api.anthropic.com', 'claude-opus-5'),
    ('Ollama 本地', 'openai', 'http://localhost:11434/v1', 'qwen2.5'),
    ('LM Studio 本地', 'openai', 'http://localhost:1234/v1', ''),
    ('自定义（OpenAI 兼容）', 'openai', '', ''),
)

# 默认配置：与用户设置里的 llm 字典合并，未填写的字段回落此值
DEFAULT_CONFIG = {
    'preset': 'DeepSeek',
    'protocol': 'openai',
    'base_url': 'https://api.deepseek.com/v1',
    'api_key': '',
    'model': 'deepseek-chat',
    'temperature': 0.8,
}


class LLMError(RuntimeError):
    """大模型调用相关的可读错误，统一在界面层展示，不暴露底层异常细节。"""


def current_config(settings):
    """把默认配置与用户设置合并，返回当前生效的大模型配置字典。"""
    return {**DEFAULT_CONFIG, **(settings.get('llm') or {})}


def settings_problem(settings):
    """Like config_problem, but tells a never-configured {} apart from a deliberate configuration.

    current_config() fills the DeepSeek defaults in, so checking only the merged result would let a
    first-time user start work that can only fail at the first API call.
    """
    # 完全没有配置过 llm 时给出整体引导，而不是落到某个缺字段的提示
    if not settings.get('llm'):
        return '还没有配置大模型：请到「设置 → 大模型 API」选择服务，填写接口地址、API Key 和模型后保存'
    return config_problem(current_config(settings))


def config_problem(config):
    """配置不完整时返回提示文字，完整时返回空字符串。"""
    # 模型名是硬性前提；openai 协议还需要一个可用的接口地址
    if not config.get('model'):
        return '还没有配置大模型：请到「设置 → 大模型 API」填写模型名称'
    if config.get('protocol', 'openai') == 'openai' and not (config.get('base_url') or '').strip():
        return '还没有配置大模型：请到「设置 → 大模型 API」填写接口地址'
    return ''


def _proxy_for(url):
    """Proxy for this URL, honouring the Windows system proxy's bypass list (ProxyOverride) and NO_PROXY.

    httpx's trust_env reads the system proxy but ignores its bypass list, so local servers such as
    Ollama / LM Studio on localhost would be sent through the proxy and fail (e.g. HTTP 503).
    """
    import urllib.request
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    # 本机回环地址一律不走代理，避免把 Ollama / LM Studio 等本地服务发到系统代理
    host = parts.hostname or ''
    if not host or host in ('localhost', '127.0.0.1', '::1'):
        return None
    # 去掉 netloc 里的用户信息（user:pass@host:port），只留 host:port 供绕过列表匹配
    host_port = parts.netloc.rsplit('@', 1)[-1]
    try:
        # 命中系统绕过列表（ProxyOverride 或 NO_PROXY）时同样返回 None
        if urllib.request.proxy_bypass(host_port) or (':' in host and urllib.request.proxy_bypass(host)):
            return None
    except OSError:
        pass

    # 按协议取代理，再退化到 all 通配项；缺 scheme 的裸地址补 http:// 前缀
    proxies = urllib.request.getproxies()
    proxy = proxies.get(parts.scheme) or proxies.get('all')
    if proxy and '://' not in proxy:
        proxy = 'http://' + proxy
    return proxy


def _chat_url(base):
    """把用户填写的接口地址规范化为 Chat Completions 的完整端点。"""
    base = base.rstrip('/')
    return base if base.endswith('/chat/completions') else base + '/chat/completions'


def _explain_status(code, body):
    """把非 2xx 响应的状态码和响应体翻译成用户可读的中文提示。"""
    # 常见状态码的中文映射，命中则作为一行摘要，否则为空
    hint = {
        401: 'API Key 无效或未填写',
        403: '没有权限访问该模型',
        404: '接口地址或模型名称不正确',
        429: '请求太频繁或额度不足',
        402: '账户余额不足',
    }.get(code, '')
    # 响应体只保留前 400 字符，避免把整段错误日志塞进界面
    body = body.strip()[:400]
    try:
        # 优先提取 JSON 里的 message 字段，逐层回落到原始文本
        payload = json.loads(body)
        message = (payload.get('error') or {}).get('message') or payload.get('message') or body
    except (ValueError, AttributeError):
        # 响应体不是 JSON 时，直接把截断后的文本当作提示
        message = body
    return f'HTTP {code}{" · " + hint if hint else ""}\n{message}'


def stream_chat(config, system, user, on_delta, cancelled=lambda: False, on_status=None):
    """Stream a reply; calls on_delta(text) per chunk and returns the full text.

    按协议分流：anthropic 走官方 SDK，其余（openai 兼容）走 SSE 流式解析。
    ``cancelled`` 默认为恒 False 的可调用，``on_status`` 用于上报"模型思考中"等状态。
    """
    protocol = config.get('protocol', 'openai')
    if not config.get('model'):
        raise LLMError('请先在「设置 → 大模型 API」中填写模型名称')
    if protocol == 'anthropic':
        return _stream_anthropic(config, system, user, on_delta, cancelled)
    return _stream_openai(config, system, user, on_delta, cancelled, on_status)


def _run_cancellable(operation, cancelled):
    """Run async network I/O in the calling worker, closing sockets on cancellation.

    在工作线程里运行 asyncio 事件循环：轮询取消标志，取消时中断任务并关闭套接字，
    从而让阻塞在异步网络 I/O 上的请求能及时退出。
    """

    async def run():
        # 开始前先检查一次取消状态
        if cancelled():
            raise InterruptedError('已取消')
        task = asyncio.create_task(operation())
        # 以 0.1s 为粒度轮询任务是否完成，期间持续响应取消请求
        while not task.done():
            if cancelled():
                raise InterruptedError('已取消')
            await asyncio.wait({task}, timeout=0.1)
        if cancelled():
            raise InterruptedError('已取消')
        # 等待任务结果；任务异常时会在此处重新抛出
        result = await task
        # 防御性清理：若任务仍未结束则取消，并吞掉取消/常规异常避免噪声
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        return result

    return asyncio.run(run())


def _stream_openai(config, system, user, on_delta, cancelled, on_status):
    """通过 OpenAI 兼容接口（SSE）流式请求，逐块回调 on_delta 并返回完整文本。"""
    import httpx

    # 接口地址缺失直接报错，避免后续拼出无效 URL
    base_url = (config.get('base_url') or '').strip()
    if not base_url:
        raise LLMError('请先在「设置 → 大模型 API」中填写接口地址')

    # 固定请求头：发送 JSON、接收 SSE 事件流
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
    # 有 API Key 时附加 Bearer 鉴权头
    if config.get('api_key'):
        headers['Authorization'] = f'Bearer {config["api_key"].strip()}'

    # 组装 Chat Completions 请求体：system + user 两条消息，开启流式
    payload = {
        'model': config['model'],
        'stream': True,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
    }
    # temperature 显式给出时才写入，避免覆盖服务商默认值
    if config.get('temperature') is not None:
        payload['temperature'] = float(config['temperature'])

    async def request():
        url = _chat_url(base_url)
        # 手动解析代理（httpx 的 trust_env 会忽略系统绕过列表），并信任系统环境补全代理
        transport = httpx.AsyncHTTPTransport(proxy=_proxy_for(url), trust_env=True)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(180, connect=15),
            follow_redirects=True,
            transport=transport,
            trust_env=False,
        ) as client:
            # 最多重试一次：用于处理个别服务商在 temperature 参数上返回 400 的情况
            for attempt in range(2):
                async with client.stream(
                    'POST', _chat_url(base_url), headers=headers, json=payload
                ) as response:
                    if response.status_code != 200:
                        await response.aread()
                        # 首次请求因 temperature 被拒时，去掉该参数重试一次
                        if (
                            attempt == 0
                            and response.status_code == 400
                            and 'temperature' in payload
                            and 'temperature' in response.text.lower()
                        ):
                            payload.pop('temperature')
                            continue
                        raise LLMError(
                            _explain_status(response.status_code, response.text)
                        )

                    # 逐块收集文本；announced 标记是否已上报过"思考中"状态
                    parts = []
                    announced = False
                    done = False
                    async for line in response.aiter_lines():
                        if cancelled():
                            raise InterruptedError('已取消')
                        # 只处理 SSE 的 data: 行
                        if not line or not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        # [DONE] 表示流正常结束
                        if data == '[DONE]':
                            done = True
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            # 某些服务商会夹带非 JSON 的 keep-alive 注释，忽略即可
                            continue

                        # 服务端在流内返回错误对象时，转成可读异常
                        if chunk.get('error'):
                            err = chunk['error']
                            raise LLMError(
                                str(err.get('message', err))
                                if isinstance(err, dict)
                                else str(err)
                            )

                        for choice in chunk.get('choices') or []:
                            delta = choice.get('delta') or {}
                            # 推理型模型先吐 reasoning_content，此时给一次状态提示
                            if delta.get('reasoning_content') and not announced:
                                announced = True
                                if on_status:
                                    on_status('模型思考中…')
                            # 真正的正文增量：累积并即时回调
                            text = delta.get('content')
                            if text:
                                parts.append(text)
                                on_delta(text)
                            # 根据结束原因判断是否完整收尾
                            reason = str(choice.get('finish_reason') or '').strip().lower()
                            if reason in ('stop', 'eos', 'end_turn', 'stop_sequence'):
                                done = True
                                continue
                            if not reason:
                                continue
                            raise LLMError(
                                '模型未完整返回歌词（结束原因：'
                                + reason
                                + '），已保留收到的部分，请重试。'
                            )

                    # 流中途断开而未收到结束标记，保留已收部分并提示
                    if not done:
                        raise LLMError(
                            '响应流提前结束，歌词可能不完整；已保留收到的部分，请重试。'
                        )
                    return ''.join(parts)

    return _run_cancellable(request, cancelled)


def _anthropic_options(config):
    """把 openai 风格的配置字典映射为 Anthropic SDK 的构造参数。"""
    # api_key 缺失时显式传 None，避免 SDK 走环境变量等隐式来源
    options = {'api_key': config.get('api_key') or None}
    # base_url 非空才覆盖，否则让 SDK 用官方默认端点
    base_url = (config.get('base_url') or '').strip()
    if base_url:
        options['base_url'] = base_url
    return options


def _stream_anthropic(config, system, user, on_delta, cancelled):
    """通过 Anthropic 官方 SDK 流式请求，逐块回调 on_delta 并返回完整文本。"""
    # SDK 属于可选依赖，未安装时给出明确的安装指引
    try:
        import anthropic
    except ImportError as err:
        raise LLMError('使用 Claude 需要安装官方 SDK：在调试器里运行  pip install anthropic') from err

    async def request():
        parts = []
        try:
            async with anthropic.AsyncAnthropic(**_anthropic_options(config)) as client:
                async with client.messages.stream(
                    model=config['model'],
                    max_tokens=16000,
                    system=system,
                    messages=[{'role': 'user', 'content': user}],
                ) as stream:
                    async for text in stream.text_stream:
                        if cancelled():
                            raise InterruptedError('已取消')
                        parts.append(text)
                        on_delta(text)
                    # 流结束后取最终消息，用于检查是否因 max_tokens 截断而提前停止
                    final = await stream.get_final_message()
        except anthropic.AuthenticationError as err:
            raise LLMError('API Key 无效') from err
        except anthropic.RateLimitError as err:
            raise LLMError('请求太频繁或额度不足，请稍后重试') from err
        except anthropic.APIStatusError as err:
            raise LLMError(f'HTTP {err.status_code}：{err.message}') from err
        except anthropic.APIConnectionError as err:
            raise LLMError(f'无法连接 Anthropic API：{err}') from err

        # 结束原因不是自然停止时，提示可能被截断
        if final.stop_reason not in ('end_turn', 'stop_sequence'):
            raise LLMError(
                '模型未完整返回歌词（结束原因：'
                + str(final.stop_reason)
                + '），请调整要求后重试。'
            )
        return ''.join(parts)

    return _run_cancellable(request, cancelled)


def list_models(config):
    """按当前配置查询可用模型列表，返回去重排序后的模型 ID 列表。"""
    # Anthropic 走官方 SDK 的 models.list()
    if config.get('protocol') == 'anthropic':
        try:
            import anthropic
        except ImportError as err:
            raise LLMError('使用 Claude 需要安装官方 SDK：在调试器里运行  pip install anthropic') from err
        with anthropic.Anthropic(**_anthropic_options(config)) as client:
            return [model.id for model in client.models.list()]

    # OpenAI 兼容协议走 /models 端点
    import requests

    base = (config.get('base_url') or '').strip().rstrip('/')
    if not base:
        raise LLMError('请先填写接口地址')
    # 用户若填的是完整 chat 端点，则去掉后缀取根地址
    if base.endswith('/chat/completions'):
        base = base[:-len('/chat/completions')]

    headers = {}
    if config.get('api_key'):
        headers['Authorization'] = f'Bearer {config["api_key"].strip()}'

    try:
        resp = requests.get(base + '/models', headers=headers, timeout=20)
    except requests.RequestException as err:
        raise LLMError(f'无法连接到 {base}：{err}') from err

    if resp.status_code != 200:
        raise LLMError(_explain_status(resp.status_code, resp.text))

    # 兼容不同返回结构：dict 取 data 字段，否则直接当列表用
    data = resp.json()
    items = data.get('data') if isinstance(data, dict) else data
    # 只保留带 id 的 dict 项，去重后排序返回
    return sorted(
        {item.get('id') for item in (items or []) if isinstance(item, dict) and item.get('id')}
    )
