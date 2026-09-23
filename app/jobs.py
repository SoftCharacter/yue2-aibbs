"""Atomic, verified checkpoints for user projects and completed generation stages.

本模块是作品库与生成流程的持久化核心：把每首歌/解码任务拆成 draft(草稿)、plan(乐谱)、
semantic(语义 tokens)、latent(潜变量)、audio(音频) 五个可落盘阶段，每个阶段提交时对产物
做哈希校验并原子写入 job.json，保证中途取消、崩溃或跨进程并发都不会留下半成品。

除持久化外，还通过 OS 文件锁（.job.lock）+ 线程本地 held 集合实现"同一任务同一时刻只有一个
写入者"的租约机制，并把保存的检查点与实时租约合并成界面可展示的状态；批量生成则在此处
把父任务拆成若干带独立种子与目录的子任务。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


# 线程本地存储：记录当前线程已持有的任务租约（Path 集合），避免同线程重入时重复加锁导致死锁
_leases = threading.local()


@contextmanager
def job_lease(folder):
    """One writer per job across threads/processes; an OS lock survives stale status.

    以上下文管理器的方式为某个任务目录加"租约"：同一任务目录在同一时刻只允许一个持有者。
    租约同时用两层保障——线程内的 held 集合（快速判定同线程是否已持有）与跨进程的 OS 文件锁
    （.job.lock，即使任务状态因崩溃而残留，锁仍由操作系统正确维护）。
    """
    folder = Path(folder).resolve()
    # 每个线程第一次使用时才初始化 held 集合，避免模块加载时就创建无用的集合
    held = getattr(_leases, 'held', None)
    if held is None:
        held = set()
        _leases.held = held
    # 同一线程已经持有该任务租约（重入），直接放行，避免对同一文件重复加锁
    if folder in held:
        yield
        return
    # 锁文件本身不允许是符号链接，防止把锁指向任务目录之外的任意路径
    lock_path = folder / '.job.lock'
    if lock_path.is_symlink():
        raise ValueError('任务锁文件路径不合法')
    with lock_path.open('a+b') as f:
        f.seek(0)
        try:
            # Windows 走 msvcrt 的字节区间锁，POSIX 走 fcntl 的 flock，均为非阻塞
            if os.name == 'nt':
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            # 加锁失败说明别的窗口/进程正在使用该任务，翻译成用户可读的提示
            raise RuntimeError(
                '这个任务正在被另一个窗口或进程使用，请等待它结束后再继续'
            ) from e
        # 加锁成功后登记到 held，标记本线程已持有
        held.add(folder)
        try:
            yield
        finally:
            # 无论正常结束还是异常，都解除登记并释放 OS 锁
            held.remove(folder)
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def is_leased(folder):
    """True while some thread/process holds the job lease (i.e. the job is really running).

    只读地判断任务是否正被持有：先查本进程的 held 集合，再尝试对 .job.lock 做非阻塞加锁，
    若立即失败说明有别的持有者。用于把保存的静态状态与"此刻是否真的在运行"区分开。
    """
    folder = Path(folder).resolve()
    # 本进程某线程已持有则直接判定为已租用
    if folder in (getattr(_leases, 'held', None) or set()):
        return True
    lock_path = folder / '.job.lock'
    # 锁文件不存在或为符号链接都视为未加锁
    if not lock_path.is_file() or lock_path.is_symlink():
        return False
    # 尝试加锁成功后立即解锁，据此判断加锁动作本身能否成功
    with lock_path.open('a+b') as f:
        f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            # 加锁失败 = 有别的持有者，任务确实在运行
            return True
    return False


def display_status(folder, state):
    """Combine saved checkpoints with the live lease without rewriting job.json.

    把 job.json 里保存的状态与实时租约合并成界面展示文案：保存为"运行中/待继续"的任务，
    需要结合租约判断——已加锁才是真的在跑，加锁失败则说明是崩溃残留、可继续。
    """
    status = state['status']
    if status in ('running', 'ready'):
        leased = is_leased(folder)
        # 持有租约且还没走到音频阶段，说明此刻正在运行
        if leased and state.get('stage') != 'audio':
            return '运行中'
        # 标记为 running 但租约已释放，说明上次运行中断、可从断点继续
        if status == 'running' and not leased:
            return '已中断，可继续'
    return STATUS_NAMES[status]


# 五个生成阶段的有序标识，供进度推进与断点续跑使用
STAGES = ('draft', 'plan', 'semantic', 'latent', 'audio')
# 各阶段对应的界面中文名
STAGE_NAMES = {
    'draft': '输入草稿',
    'plan': '乐谱已保存',
    'semantic': '歌曲 tokens 已保存',
    'latent': '声学结果已保存',
    'audio': '音频已完成',
}
# 任务状态标识到界面中文名的映射
STATUS_NAMES = {
    'draft': '草稿',
    'ready': '待继续',
    'running': '运行中',
    'cancelled': '已取消',
    'failed': '失败，可继续',
    'complete': '已完成',
}
# plan 阶段固定产出的文件集合
PLAN_FILES = frozenset(
    {'plan.json', 'prefix.npy', 'abc_tokens.npy', 'plan_manifest.json'}
)
# 任务目录内允许出现的全部合法文件名（= plan 产物 + 其余各阶段的中间/结果文件）
ALLOWED_FILES = PLAN_FILES | frozenset(
    {
        'meta.json',
        'score.abc',
        'audio.flac',
        'latent.npy',
        'config.json',
        'latent.json',
        'result.json',
        'request.json',
        'semantic.npy',
        'semantic.json',
    }
)
# 每个阶段提交时必备的文件集合，用于校验阶段是否完整
REQUIRED = {
    'draft': set(),
    'plan': PLAN_FILES | {'config.json', 'request.json'},
    'semantic': {'semantic.npy', 'semantic.json'},
    'latent': frozenset({'latent.npy', 'config.json', 'latent.json', 'request.json'}),
    'audio': frozenset({'meta.json', 'audio.flac', 'result.json'}),
}


def identity(value):
    """对任意可 JSON 化的值做确定性序列化后求 SHA-256，作为内容指纹。"""
    # sort_keys + 固定分隔符保证同一逻辑内容在不同机器/运行次数下得到相同哈希
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


def sha256(path):
    """分块计算文件的 SHA-256，避免把大文件一次性读入内存。"""
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        # 每次读 8 MiB 的块，直到读到空串为止
        for chunk in iter(lambda: f.read(8388608), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    """把值以 JSON 原子写入 path：先写临时文件再 replace，避免留下半成品。"""
    path = Path(path)
    # 临时文件名 = 原文件名 + 随机后缀，写完后 os.replace 原子替换
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
            encoding='utf-8',
        )
        os.replace(tmp, path)
    finally:
        # 成功时临时文件已被 replace 移走，此处清理是无害的空操作；失败时用于兜底清理
        tmp.unlink(missing_ok=True)


def write_array(path, value):
    """把 NumPy 数组以 .npy 格式原子写入 path（禁止 pickle，仅存原始数组）。"""
    import numpy

    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('wb') as f:
            numpy.save(f, value, allow_pickle=False)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _artifact(folder, name):
    """校验文件名合法并返回 folder/name 的路径，防止目录穿越或符号链接逃逸。"""
    # 文件名必须是字符串且在允许清单内，防止构造任意路径
    if not isinstance(name, str) or name not in ALLOWED_FILES:
        raise ValueError('任务文件路径不合法')
    path = folder / name
    # 路径不能是符号链接，且 resolve 后必须仍在任务目录内
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError('任务文件路径不合法')
    return path


def create_job(folder, params, runtime=None, *, kind='song'):
    """在空目录里创建一个新任务草稿，写入 job.json 并返回任务字典。"""
    folder = Path(folder)
    # 只允许在全新（或不存在）的目录里创建，避免覆盖已有作品
    if folder.is_symlink() or (folder.exists() and any(folder.iterdir())):
        raise FileExistsError('保存任务需要新的空文件夹，原有作品不会被覆盖')
    if kind not in ('song', 'decode'):
        raise ValueError('未知任务类型')
    folder.mkdir(parents=True, exist_ok=True)
    # 把 params/runtime 经 JSON 序列化往返一次，得到纯 dict 的快照，隔离外部可变对象
    payload = json.loads(
        json.dumps({'params': params, 'runtime': runtime or {}}, ensure_ascii=False, allow_nan=False)
    )
    job = {
        'version': 1,
        'kind': kind,
        **payload,
        # 参数内容指纹：后续据此判断任务参数是否被篡改
        'params_sha256': identity(payload),
        'created': datetime.now().isoformat(timespec='seconds'),
        'stage': 'draft',
        'status': 'draft',
        'artifacts': {},
        'error': '',
        'elapsed_seconds': 0,
    }
    write_json(folder / 'job.json', job)
    return job


def read_job(folder, verify=True):
    """读取并校验任务目录的 job.json；verify=True 时额外核验各阶段产物文件。"""
    folder = Path(folder)
    job = json.loads((folder / 'job.json').read_text(encoding='utf-8'))
    # 校验基础字段的格式与取值范围
    if (
        not isinstance(job, dict)
        or job.get('version') != 1
        or job.get('stage') not in STAGES
        or job.get('kind') not in ('song', 'decode')
    ):
        raise ValueError('不支持的任务格式')
    if (
        job.get('status') not in STATUS_NAMES
        or not isinstance(job.get('params'), dict)
        or not isinstance(job.get('runtime'), dict)
    ):
        raise ValueError('任务状态或参数格式不正确')
    # 参数内容指纹不一致说明任务被手工改动过，拒绝复用
    if identity({'params': job['params'], 'runtime': job['runtime']}) != job.get('params_sha256'):
        raise ValueError('已保存的任务参数发生改变，请另存为新任务')
    if verify:
        # 汇总从 draft 到当前阶段所有必备文件，逐一核对是否都已落盘
        needed = set()
        for stage in STAGES[: STAGES.index(job['stage']) + 1]:
            # 纯解码任务不存在 plan/semantic 阶段，跳过这两个阶段的必备文件
            if job['kind'] == 'decode' and stage in ('plan', 'semantic'):
                continue
            needed.update(REQUIRED[stage])
        if not (needed <= set(job['artifacts'])):
            raise ValueError('任务阶段文件不完整')
        # 对每个已登记的产物文件做大小与哈希双重校验
        for name, expected in job['artifacts'].items():
            verify_artifact(folder, name, expected)
    return job


def verify_artifact(folder, name, expected):
    """校验单个产物文件存在、大小与哈希都与登记值一致，返回其路径。"""
    path = _artifact(Path(folder), name)
    if (
        not path.is_file()
        or path.stat().st_size != expected['bytes']
        or sha256(path) != expected['sha256']
    ):
        raise ValueError(f'任务文件缺失、损坏或已改变：{name}；请重新生成或载入完整备份')
    return path


def commit_stage(folder, stage, files):
    """把一批产物文件登记为某个已完成阶段，更新 job.json 并返回任务字典。"""
    folder = Path(folder)
    job = read_job(folder)
    # 纯解码任务只能走 draft/latent/audio 三个阶段，整首歌任务走完整 STAGES
    allowed = ('draft', 'latent', 'audio') if job['kind'] == 'decode' else STAGES
    # 阶段必须紧接当前阶段的下一步，禁止跳过未完成阶段
    if stage not in allowed or allowed.index(stage) != allowed.index(job['stage']) + 1:
        raise ValueError('不能跳过未完成的任务阶段')
    artifacts = dict(job['artifacts'])
    # 逐个登记文件：先确认已保存，再记录字节数与哈希
    for name in files:
        path = _artifact(folder, name)
        if not path.is_file():
            raise ValueError(f'任务文件尚未保存：{name}')
        artifacts[name] = {'bytes': path.stat().st_size, 'sha256': sha256(path)}
    # 该阶段的必备文件必须齐全，否则阶段不完整
    if not (REQUIRED[stage] <= set(artifacts)):
        raise ValueError('任务阶段文件不完整')
    # audio 阶段标记为完成，其余阶段标记为"待继续"
    job.update(
        stage=stage,
        status='complete' if stage == 'audio' else 'ready',
        artifacts=artifacts,
        error='',
    )
    write_json(folder / 'job.json', job)
    return job


def set_status(folder, status, error='', *, elapsed=None):
    """更新任务状态与错误信息（可选累计耗时），写回 job.json 并返回任务字典。"""
    if status not in STATUS_NAMES:
        raise ValueError('未知任务状态')
    folder = Path(folder)
    job = read_job(folder, verify=False)
    job.update(status=status, error=str(error))
    # elapsed 显式给出时才写入，避免覆盖已保存的累计耗时
    if elapsed is not None:
        job['elapsed_seconds'] = float(elapsed)
    write_json(folder / 'job.json', job)
    return job


def job_result(folder, verify=False):
    """组装任务结果字典，供界面与后续流程消费；verify 为 True 时先校验产物。"""
    folder = Path(folder)
    job = read_job(folder, verify=verify)
    # 以保存的参数为基底，再叠加 meta.json 里的创作信息
    result = dict(job['params'])
    if (folder / 'meta.json').is_file():
        result.update(json.loads((folder / 'meta.json').read_text(encoding='utf-8')))
    # 没有 meta 时回落到 job 的创建时间
    result.setdefault('created', job['created'])
    # ABC 优先取落盘的 score.abc，否则退回参数里保存的 abc 文本
    if (folder / 'score.abc').is_file():
        abc = (folder / 'score.abc').read_text(encoding='utf-8')
    else:
        abc = result.get('abc', '') or ''
    # 只有真正完成到 audio 阶段才有音频文件路径
    audio = str(folder / 'audio.flac') if job['stage'] == 'audio' else ''
    return {
        'dir': str(folder),
        'audio': audio,
        'abc': abc,
        'meta': result,
        'job': job,
        'seconds': result.get('duration', 0),
        'timing': result.get('timing', {}),
        'truncated': result.get('truncated', {}),
    }


def copy_plan(folder, destination):
    """Save another editable draft retaining the exact native prefix and ABC tokens.

    把一个已有 plan 阶段的任务复制成新的可编辑草稿，精确保留 prefix/ABC tokens 等二进制
    中间产物，便于在原乐谱基础上继续编辑而不影响源任务。
    """
    import shutil

    source = Path(folder)
    destination = Path(destination)
    job = read_job(source)
    # 只支持整首歌任务，且必须已推进到 plan 阶段
    if job['kind'] != 'song' or STAGES.index(job['stage']) < STAGES.index('plan'):
        raise ValueError('来源没有完整的乐谱计划')
    create_job(destination, job['params'], job['runtime'])
    # 复制 plan 阶段必备文件；若源任务还额外保存了 score.abc 也一并带上
    files = REQUIRED['plan'] | (
        {'score.abc'} if 'score.abc' in job['artifacts'] else set()
    )
    for name in files:
        shutil.copyfile(_artifact(source, name), _artifact(destination, name))
    commit_stage(destination, 'plan', files)
    return job_result(destination)


def batch_folders(folder, state=None):
    """返回批量任务的成员目录列表：父目录排最前，其后是按登记顺序的子任务目录。"""
    folder = Path(folder)
    state = state or read_job(folder, verify=False)
    result = [folder]
    for child in state.get('batch_children', []):
        # 子任务名必须是相对名称（无路径分隔符），且以 <父目录名>_batch_ 开头
        if not (
            isinstance(child, str)
            and Path(child).name == child
            and child.startswith(folder.name + '_batch_')
        ):
            raise ValueError('批量任务路径不合法')
        # 拼出完整路径，并确保它位于父任务目录的同一父目录下、且不是符号链接
        path = folder.parent / child
        if path.is_symlink() or path.resolve().parent != folder.resolve().parent:
            raise ValueError('批量任务路径不合法')
        result.append(path)
    return result


def prepare_batch(folder):
    """Persist every member before starting so cancellation cannot lose queued songs.

    为批量生成做持久化准备：根据 batch_count 补齐子任务目录名并逐个建好草稿（带独立种子），
    返回全部成员目录列表，保证开始生成前所有排队歌曲都已落盘。
    """
    folder = Path(folder)
    job = read_job(folder)
    # 批量数量优先取顶层 batch_count，否则回落 params 里的 count
    count = int(job.get('batch_count', job['params'].get('count', 1)))
    if not (1 <= count <= 16):
        raise ValueError('批量生成数量必须为 1～16 首')
    if count == 1:
        return [folder]
    seed = int(job['params']['seed'])
    # 校验种子与"种子 + 数量"都落在合法的 63 位非负整数范围内
    if not (0 <= seed < 0x8000000000000000) or seed + count - 1 >= 0x8000000000000000:
        raise ValueError('批量种子超出允许范围，请减小种子或另存为新草稿')
    children = list(job.get('batch_children', []))
    # 不足时用"父目录名_batch_序号_随机串"补齐子任务目录名
    while len(children) < count - 1:
        children.append(
            f'{folder.name}_batch_{len(children) + 2:02d}_{uuid.uuid4().hex[:8]}'
        )
    job['batch_children'] = children
    # 先落盘成员清单并校验，再正式写回 job.json
    batch_folders(folder, job)
    write_json(folder / 'job.json', job)
    folders = batch_folders(folder, job)
    # 遍历除父任务外的每个成员：不存在则建草稿（独立种子），已存在则核对种子是否一致
    for i, child in enumerate(folders[1:], 1):
        if (child / 'job.json').exists():
            member = read_job(child, verify=False)
            if member['params'].get('seed') != seed + i:
                raise ValueError('批量任务成员的种子不一致')
        else:
            create_job(
                child,
                dict(job['params'], seed=seed + i, count=1, seed_auto=False),
                job['runtime'],
            )
    return folders


def can_resume(folder, state=None):
    """判断批量任务是否仍有可继续的成员（自身或某个子任务还没到 audio 阶段）。"""
    state = state or read_job(folder, verify=False)
    # 父任务自身未完成即可继续
    if state['stage'] != 'audio':
        return True
    # 任一子任务未完成（或还没建草稿）也可继续
    for child in batch_folders(folder, state)[1:]:
        if not (child / 'job.json').is_file() or read_job(child, verify=False)['stage'] != 'audio':
            return True
    return False
