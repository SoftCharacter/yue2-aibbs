"""对现有 ABC/MIDI 做离线渲染；独立子进程不加载模型。"""
from __future__ import annotations

from datetime import datetime
import importlib.util as importlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

# 项目根目录：本文件位于 app/ 下，上一级即为项目根
ROOT = Path(__file__).resolve().parents[1]
# SheetSage2 官方渲染器所在目录
SHEETSAGE_DIR = ROOT / 'models' / 'SheetSage2'
# 便携 Python 环境目录
ENV = ROOT / 'env'
# 支持导出的乐谱格式
FORMATS = ('pdf', 'png', 'svg')
# 支持导出的钢琴声部
PARTS = ('mix', 'melody', 'vocal', 'instrumental', 'chords')


def _choices(value, allowed, caption):
    """把用户传入的选项规范化为去重后的元组，并对非法项抛出可读错误。"""
    # 字符串按逗号拆分，否则视为已拆分好的可迭代对象
    items = value.split(',') if isinstance(value, str) else value
    # 逐项转小写、去空白，再借助 dict.fromkeys 去重并保持顺序
    items = tuple(dict.fromkeys(str(v).strip().lower() for v in items))
    # 出现不允许的值时，报出具体是哪些项不合法
    if set(items) - set(allowed):
        raise ValueError(
            f'不支持的{caption}：{", ".join(sorted(set(items) - set(allowed)))}'
        )
    return items


def resolve_sources(params):
    """解析输入和选项，不创建目录、不导入浏览器或模型。abc 参数是当前 ABC 文本。"""
    # 解析导出格式与钢琴声部，均回落到默认值
    score = _choices(params.get('score', ('pdf',)), FORMATS, '乐谱格式')
    audio = bool(params.get('audio', True))
    parts = _choices(params.get('parts', ('mix',)), PARTS, '钢琴声部')

    # 至少要导出一种东西：要么乐谱，要么钢琴 WAV
    if not audio and not score:
        raise ValueError('请至少选择一种导出格式。')
    if audio and not parts:
        raise ValueError('请至少选择一个钢琴声部。')

    # 结果文件夹：给出则解析为绝对路径，否则为 None
    source_dir = (
        Path(params['source_dir']).expanduser().resolve()
        if params.get('source_dir')
        else None
    )
    if source_dir is not None and not source_dir.is_dir():
        raise ValueError('结果文件夹不存在，请重新选择。')

    # 当前 ABC 文本：若已提供则必须是含内容的字符串
    abc = params.get('abc')
    if abc is not None and (not isinstance(abc, str) or not abc.strip()):
        raise ValueError('当前 ABC 为空，请先填写乐谱。')

    # 单独的 ABC 文件与 MIDI 文件路径（可选）
    abc_path = (
        Path(params['abc_path']).expanduser().resolve()
        if params.get('abc_path')
        else None
    )
    midi_path = (
        Path(params['midi_path']).expanduser().resolve()
        if params.get('midi_path')
        else None
    )

    # 未给出 ABC 时，尝试从结果文件夹内推断：优先 score.abc，其次唯一的一个 .abc
    if abc is None and abc_path is None and source_dir:
        score_abc = source_dir / 'score.abc'
        candidates = sorted(source_dir.glob('*.abc'))
        abc_path = (
            score_abc
            if score_abc.is_file()
            else (candidates[0] if len(candidates) == 1 else None)
        )

    # 未给出 MIDI 时，从结果文件夹内推断：先按固定名，再退化为唯一的 .mid/.midi
    if midi_path is None and source_dir:
        midi_path = next(
            (
                source_dir / name
                for name in ('transcription.mid', 'melody.mid', 'score.mid')
                if (source_dir / name).is_file()
            ),
            None,
        )
        if midi_path is None:
            candidates = sorted(
                set(source_dir.glob('*.mid')) | set(source_dir.glob('*.midi'))
            )
            midi_path = candidates[0] if len(candidates) == 1 else None

    # 需要导出乐谱但还没有 ABC 文本时，从 ABC 文件读取
    if score and abc is None:
        if not (abc_path and abc_path.is_file()):
            raise ValueError(
                '乐谱导出需要 ABC。请选择包含 score.abc 的结果文件夹，或单独选择 ABC 文件。'
            )
        abc = abc_path.read_text(encoding='utf-8-sig')
        if not abc.strip():
            raise ValueError('ABC 文件为空。')

    # 需要导出钢琴 WAV 时，必须有可用的 MIDI 文件
    if audio and (midi_path is None or not midi_path.is_file()):
        raise ValueError(
            '钢琴 WAV 需要已有 MIDI。请选择 MIDI 文件，或取消勾选钢琴 WAV。'
        )

    return {
        'source_dir': source_dir,
        'abc': abc,
        'midi_path': midi_path,
        'audio': audio,
        'score': score,
        'parts': parts,
    }


def check_render_dependencies(assets_dir):
    """给缺失的可选运行时提供可操作提示，资源哈希由官方渲染器核验。"""
    # 优先使用传入的 assets 目录，否则回落到 SheetSage2 下的 render_assets
    assets = Path(assets_dir) if assets_dir else SHEETSAGE_DIR / 'render_assets'
    # 若默认位置没有 manifest，再尝试 env 目录下的 render_assets
    if assets_dir is None and not (assets / 'manifest.json').is_file():
        assets = ENV / 'render_assets'

    if not (assets / 'manifest.json').is_file():
        raise RuntimeError(
            '缺少渲染资源：请从官方 m-a-p/SheetSage2 下载 render_assets，'
            '放入 models/SheetSage2 或 env 目录。'
        )

    # 检查可选渲染依赖是否已安装
    missing = [
        name
        for name in ('playwright', 'pretty_midi')
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        raise RuntimeError(
            '缺少渲染依赖（'
            + ', '.join(missing)
            + '）。请运行 env\\python.exe models\\SheetSage2\\setup_render.py。'
        )

    return assets.resolve()


def _new_destination(params):
    """在输出根目录下新建一个带时间戳前缀的临时目录作为本次渲染目标。"""
    # 输出根目录：优先用户指定，否则使用默认的 outputs/rendered
    out_parent = Path(
        params.get('output_parent') or ROOT / 'outputs' / 'rendered'
    ).expanduser().resolve()
    out_parent.mkdir(parents=True, exist_ok=True)
    # 用 tempfile.mkdtemp 生成唯一的输出目录
    return Path(
        tempfile.mkdtemp(
            prefix=datetime.now().strftime('%Y%m%d-%H%M%S_'), dir=out_parent
        )
    )


def _write_metadata(directory, data):
    """把渲染状态写入目录下的 render.json，供 GUI 轮询进度。"""
    (directory / 'render.json').write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
    )


def render_existing(params, *, destination):
    """同步底层接口；返回 dir/audio/score/warnings。GUI 应通过 run_render 调用。"""
    # 解析输入并核验渲染依赖
    sources = resolve_sources(params)
    assets = check_render_dependencies(params.get('assets_dir'))

    # 便携环境自带 playwright 浏览器时，注入其搜索路径
    if (ENV / 'ms-playwright').is_dir():
        os.environ.setdefault(
            'PLAYWRIGHT_BROWSERS_PATH', str(ENV / 'ms-playwright')
        )

    # 未指定目标目录则新建一个
    destination = Path(destination) if destination else _new_destination(params)

    # 初始元数据：状态为渲染中，格式与声部先落入 metadata
    meta = {
        'type': 'render',
        'status': 'rendering',
        'source_dir': str(sources['source_dir'] or ''),
        'formats': list(sources['score']),
        'audio': sources['audio'],
        'parts': list(sources['parts']),
    }
    _write_metadata(destination, meta)

    # 把 ABC 文本落地为 score.abc，供渲染器读取
    abc_file = None
    if sources['abc'] is not None:
        abc_file = destination / 'score.abc'
        abc_file.write_text(sources['abc'], encoding='utf-8')

    # 需要钢琴 WAV 时，把 MIDI 复制到目标目录
    midi_file = None
    if sources['audio']:
        midi_file = destination / 'transcription.mid'
        shutil.copyfile(sources['midi_path'], midi_file)

    # 动态加载官方渲染脚本并执行渲染
    spec = importlib.util.spec_from_file_location(
        '_studio_score_renderer', SHEETSAGE_DIR / 'rendering_sheetsage2.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = module.render_outputs(
        sources['source_dir'],
        midi=midi_file,
        abc=abc_file,
        output_dir=destination,
        audio=sources['audio'],
        score=sources['score'],
        parts=sources['parts'],
        assets_dir=assets,
    )

    # 请求了 WAV 但结果里没有，且同时请求了乐谱，说明选中的声部缺失
    if sources['audio'] and not result['audio'] and sources['score']:
        raise ValueError(
            '选中的声部在 MIDI 中不存在，没有生成 WAV；请选择混合声部或其他声部。'
        )

    # 补充目标目录，并把缺失声部的警告改写成更友好的中文提示
    result['dir'] = str(destination)
    result['warnings'] = [
        '缺少对应 MIDI 声部，已跳过：' + w[3:].split(' track', 1)[0]
        if (w.startswith('No ') and ' track is available' in w)
        else w
        for w in result.get('warnings', [])
    ]

    # 标记完成并落盘元数据
    meta.update(status='complete', result=result)
    _write_metadata(destination, meta)
    return result


def _stop_process_tree(process):
    """终止渲染子进程及其整棵进程树，Windows 下走 taskkill。"""
    # 已经退出则无需处理
    if process.poll() is not None:
        return

    if os.name == 'nt':
        # Windows：taskkill /T /F 连带子进程一起强杀
        subprocess.run(
            ['taskkill', '/PID', str(process.pid), '/T', '/F'],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=15,
        )
    else:
        # POSIX：向进程组发送 SIGTERM；进程组可能已不存在则忽略
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    # 仍未退出则强杀，最后等待其回收
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def run_render(ctx, params):
    """TaskRunner 工作线程入口；可取消独立渲染进程及其浏览器子进程。"""
    # 开始前与依赖核验后各检查一次取消状态
    if ctx.cancelled():
        raise InterruptedError()
    resolve_sources(params)
    check_render_dependencies(params.get('assets_dir'))
    if ctx.cancelled():
        raise InterruptedError()

    # 新建输出目录并上报进度
    destination = _new_destination(params)
    ctx.progress(message='正在生成乐谱与钢琴预览…', output_dir=str(destination))

    process = None
    try:
        # 临时目录存放请求/响应 JSON，子进程通过文件交换数据
        with tempfile.TemporaryDirectory(prefix='yue2-render-') as tmpdir:
            request_path = Path(tmpdir) / 'request.json'
            response_path = Path(tmpdir) / 'response.json'
            request_path.write_text(
                json.dumps(
                    {'params': params, 'destination': str(destination)},
                    ensure_ascii=False,
                ),
                encoding='utf-8',
            )

            # 优先使用便携环境里的 python.exe，缺失则退回当前解释器
            python = ENV / 'python.exe'
            if not python.is_file():
                python = Path(sys.executable)

            # 强制 UTF-8 环境，避免中文路径/日志乱码
            env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
            # Windows 隐藏控制台窗口，POSIX 新建会话以便整体终止
            popen_kwargs = (
                {'creationflags': subprocess.CREATE_NO_WINDOW}
                if os.name == 'nt'
                else {'start_new_session': True}
            )

            # 以本文件自身为入口启动独立渲染子进程
            process = subprocess.Popen(
                [
                    str(python),
                    str(Path(__file__)),
                    str(request_path),
                    str(response_path),
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **popen_kwargs,
            )

            # 轮询子进程：短超时反复检查，既支持取消又能及时读结果
            while True:
                if ctx.cancelled():
                    _stop_process_tree(process)
                    _write_metadata(
                        destination, {'type': 'render', 'status': 'cancelled'}
                    )
                    raise InterruptedError()

                try:
                    stdout, stderr = process.communicate(timeout=0.15)
                except subprocess.TimeoutExpired:
                    continue

                if ctx.cancelled():
                    _write_metadata(
                        destination, {'type': 'render', 'status': 'cancelled'}
                    )
                    raise InterruptedError()

                # 响应文件未生成说明子进程提前失败，带上尾部输出辅助排查
                if not response_path.is_file():
                    raise RuntimeError(
                        '渲染子进程未能完成：'
                        + (stderr or stdout).decode('utf-8', errors='replace')[
                            -2500:
                        ]
                    )

                response = json.loads(response_path.read_text(encoding='utf-8'))

                # 返回码非零或响应里带 error 都视为失败
                if process.returncode or 'error' in response:
                    error = response.get('error', '渲染进程失败')
                    # 浏览器未就绪时给出可操作的安装指引
                    if 'setup_render.py' in error or (
                        'renderer' in error.lower() and 'start' in error.lower()
                    ):
                        error = (
                            '渲染浏览器未就绪，请运行 '
                            'env\\python.exe models\\SheetSage2\\setup_render.py。\n'
                            + error
                        )
                    raise RuntimeError(
                        error + f'\n本次目录（可能含未完成文件）：{destination}'
                    )

                return response['result']
    finally:
        # 无论成功、失败还是取消，都确保子进程被终止、管道被关闭
        if process is not None:
            _stop_process_tree(process)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def _main():
    """子进程入口：读取请求 JSON，调用同步渲染，并把结果写回响应 JSON。"""
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    response_path = Path(sys.argv[2])
    try:
        result = render_existing(
            request['params'], destination=request['destination']
        )
        response_path.write_text(
            json.dumps({'result': result}, ensure_ascii=False), encoding='utf-8'
        )
        return 0
    except Exception as exc:
        response_path.write_text(
            json.dumps({'error': str(exc)}, ensure_ascii=False), encoding='utf-8'
        )
        return 1


if __name__ == '__main__':
    raise SystemExit(_main())
