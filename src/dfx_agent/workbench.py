"""Local single-task UI adapter. Existing CLI and run artifacts remain authoritative."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import threading
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .workspace import confined, read_json


def stamp():
    return datetime.now(timezone.utc).isoformat()


def identifier():
    return datetime.now().strftime('%Y%m%d-%H%M%S-%f')


def total_tokens(result):
    """Sum provider usage only when every API call has a usable receipt."""
    if result is None:
        return None
    usage, calls = result.get('usage'), result.get('api_calls')
    if not isinstance(usage, list) or type(calls) is not int or len(usage) != calls:
        return None
    values = [item.get('total_tokens') if isinstance(item, dict) else None for item in usage]
    return sum(values) if all(type(v) is int and v >= 0 for v in values) else None


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class TaskInput(Input):
    assignment_id: str = ''
    prepared_id: str = ''
    repo: str
    task: str = Field(min_length=1, max_length=100000)
    mode: Literal['fixture', 'deepseek'] = 'fixture'
    responses: str = ''
    scope: str = ''
    logs: str = ''
    host: str = ''
    max_steps: int = Field(default=40, ge=2)
    max_output_tokens: int = Field(default=32768, gt=0)
    command_timeout: int = Field(default=60, gt=0)
    script_timeout: int = Field(default=120, gt=0)
    build_timeout: int = Field(default=600, gt=0)
    model_timeout: int = Field(default=180, gt=0)
    allow_device: bool = False
    allow_dependency_install: bool = False
    allow_system_navigation: bool = False
    allow_screen_unlock: bool = False


class PathInput(Input):
    path: str


class ExportInput(Input):
    evaluation: str = ''


class ResumeInput(Input):
    waiting_tool: int = Field(ge=1)


class MemberSettings(Input):
    member: Literal['member1', 'member2']
    dataset: str
    deveco: str
    device: str
    swipe_only: bool = False


class AssignmentInput(Input):
    task_id: str


class PreparedInput(Input):
    prepared_id: str


class DeviceProbe(Input):
    deveco: str


class MemberFeedback(Input):
    task_id: str
    status: Literal['NOT_RUN', 'PASS', 'FAIL', 'BLOCKED', 'UNVERIFIED']
    notes: str = Field(default='', max_length=20000)
    run_id: str = ''


def existing_path(value, *, directory=False):
    path = Path(value).expanduser()
    if not value or not path.is_absolute():
        raise ValueError('需要本机绝对路径')
    path = path.resolve()
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError('路径不存在或类型不符：' + str(path))
    return path


class Workbench:
    def __init__(self, root, *, allow_live=False, allow_device=False):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = self.root / 'index.json'
        self.rows = read_json(self.index) if self.index.exists() else []
        self.lock = threading.RLock()
        self.children = {}
        self.allow_live, self.allow_device = allow_live, allow_device
        from .member_tasks import MemberTasks
        self.assignments = MemberTasks(self.root)

    def save(self):
        temporary = self.index.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.rows, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.index)

    def row(self, key):
        found = next((r for r in self.rows if r['id'] == key), None)
        if found is None:
            raise HTTPException(404, '任务不存在')
        return found

    def status(self, row):
        process = self.children.get(row['id'])
        if process is not None and process.poll() is None:
            return 'Stopping' if row.get('stop_requested') else 'Running'
        result_path = Path(row['run']) / 'result.json'
        if result_path.exists():
            return read_json(result_path)['status']
        if row.get('spawn_error'):
            return 'LaunchError'
        exit_path = Path(row['job']) / 'exit.json' if row.get('job') else None
        if process is not None or 'returncode' in row or (exit_path and exit_path.exists()):
            return 'ExitedWithoutResult'
        return 'Unobserved'

    def list_runs(self):
        with self.lock:
            return [{**r, 'status': self.status(r)} for r in reversed(self.rows)]

    def start(self, data):
        with self.lock:
            if any(self.status(r) in ('Running', 'Stopping', 'Unobserved') for r in self.rows):
                raise HTTPException(409, '已有任务运行或状态待核对；不启动第二个任务')
            if data.assignment_id:
                binding = self.assignments.binding(data.prepared_id)
                if data.model_dump() != TaskInput(**binding).model_dump():
                    raise ValueError('任务输入与已准备的绑定不一致，请重新选题')
                from .member_tasks import environment_only_attempt
                if any(r.get('assignment_id') == data.assignment_id and
                       not environment_only_attempt({**r, 'status': self.status(r)}) for r in self.rows):
                    raise ValueError('本题已有运行记录；先回传该结果，不自动重新派发')
            if data.mode == 'deepseek' and not self.allow_live:
                raise HTTPException(409, '当前服务为零 API 模式，真实模型未启用')
            if data.allow_device and not self.allow_device:
                raise HTTPException(409, '当前服务未启用设备操作')
            if (data.allow_screen_unlock or data.allow_system_navigation) and not data.allow_device:
                raise ValueError('导航/解锁需要本次任务允许设备操作')
            if (data.allow_device or data.allow_dependency_install) and not data.host:
                raise ValueError('设备/依赖操作需要主机配置')
            repo = existing_path(data.repo, directory=True)
            if self.root.is_relative_to(repo):
                raise ValueError('工作台数据目录必须在输入项目外')
            paths = {name: existing_path(getattr(data, name), directory=name == 'logs')
                     for name in ('scope', 'host', 'logs') if getattr(data, name)}
            if data.mode == 'fixture':
                paths['responses'] = existing_path(data.responses)
                if not isinstance(read_json(paths['responses']), list):
                    raise ValueError('夹具文件必须为动作数组')
            else:
                if not data.scope or read_json(paths['scope']).get('outbound_approved') is not True:
                    raise ValueError('真实任务需要已审阅的外发范围；页面不自动批准范围')
                if not os.environ.get('DEEPSEEK_API_KEY'):
                    raise ValueError('服务进程未配置 DEEPSEEK_API_KEY')
            if not data.task.strip():
                raise ValueError('请输入任务描述')
            key = identifier()
            job = self.root / 'jobs' / key
            job.mkdir(parents=True)
            (job / 'task.txt').write_text(data.task, encoding='utf-8')
            argv = [sys.executable, '-u', '-m', 'dfx_agent.workbench_worker', str(job / 'exit.json'),
                    'solve', '--repo', str(repo), '--task-file', str(job / 'task.txt'), '--out', str(job / 'run'),
                    '--model-protocol', 'tool-calls', '--closing-requests', '1',
                    '--thinking', 'enabled', '--reasoning-effort', 'high',
                    '--context-window-tokens', '1000000', '--context-headroom-ratio', '0.10']
            for name, path in paths.items():
                argv += ['--' + name, str(path)]
            if data.mode == 'deepseek':
                argv += ['--provider', 'deepseek', '--model', 'deepseek-flash']
            for name in ('max_steps', 'max_output_tokens', 'command_timeout', 'script_timeout',
                         'build_timeout', 'model_timeout'):
                argv += ['--' + name.replace('_', '-'), str(getattr(data, name))]
            for name in ('allow_device', 'allow_dependency_install', 'allow_system_navigation', 'allow_screen_unlock'):
                if getattr(data, name):
                    argv.append('--' + name.replace('_', '-'))
            row = {'id': key, 'run': str(job / 'run'), 'job': str(job), 'title': data.task.strip()[:70],
                   'repo': str(repo), 'mode': data.mode, 'created': stamp(), 'config': data.model_dump(),
                   'stop_requested': False}
            if data.assignment_id:
                row.update(assignment_id=data.assignment_id, prepared_id=data.prepared_id)
                argv.append('--environment-preflight')
            self.rows.append(row)
            self.save()
            (job / 'command.json').write_text(json.dumps(argv, ensure_ascii=False, indent=2), encoding='utf-8')
            return self.spawn(row, argv)

    def spawn(self, row, argv):
        # Called with the manager lock held; one process per active task.
        key, job = row['id'], Path(row['job'])
        row['worker_receipt'] = argv[4]
        env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        if row['mode'] == 'fixture':
            env.pop('DEEPSEEK_API_KEY', None)
        # The same installed package is used by both UI and CLI.
        env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)
        options = {}
        if os.name == 'nt':
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = subprocess.SW_HIDE
            options.update(creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=info)
        try:
            with (job / 'worker.log').open('ab') as output:
                child = subprocess.Popen(argv, cwd=job, env=env, stdin=subprocess.PIPE,
                                         stdout=output, stderr=subprocess.STDOUT, **options)
        except OSError as exc:
            row['spawn_error'] = str(exc)
            self.save()
            raise
        self.children[key] = child
        row['pid'] = child.pid
        self.save()
        threading.Thread(target=self.observe_exit, args=(key, child), daemon=True).start()
        return {'id': key}

    def resume(self, key, waiting_tool):
        from .continuation import load_continuation
        with self.lock:
            row = self.row(key)
            if any(self.status(r) in ('Running', 'Stopping', 'Unobserved') for r in self.rows):
                raise HTTPException(409, '已有任务运行或状态待核对；不会重复继续')
            if row.get('imported') or not row.get('job'):
                raise ValueError('导入的历史记录仅供审阅，不能从工作台续写')
            data = load_continuation(Path(row['run']))
            if not data['result'].get('waiting_user'):
                raise ValueError('该任务没有等待操作者处理的事项')
            if data['result']['tool_calls'] != waiting_tool:
                raise HTTPException(409, '待处理事项已经变化，请查看当前任务；未重复继续')
            if data['launch']['provider'] and (not self.allow_live or not os.environ.get('DEEPSEEK_API_KEY')):
                raise ValueError('当前服务未启用真实模型或未配置 DEEPSEEK_API_KEY')
            if data['launch']['options']['allow_device'] and not self.allow_device:
                raise ValueError('当前服务未启用设备操作')
            job = Path(row['job'])
            attempt = len(list(job.glob('resume-*-command.json'))) + 1
            stem = f'resume-{attempt:04d}'
            argv = [sys.executable, '-u', '-m', 'dfx_agent.workbench_worker', str(job / (stem + '-exit.json')),
                    'resume', '--run', row['run'], '--user-action-done']
            (job / (stem + '-command.json')).write_text(json.dumps(argv, ensure_ascii=False, indent=2), encoding='utf-8')
            row.update(stop_requested=False)
            for name in ('spawn_error', 'ended', 'returncode', 'export'):
                row.pop(name, None)
            self.save()
            return self.spawn(row, argv)

    def observe_exit(self, key, child):
        code = child.wait()
        with self.lock:
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            if self.children.get(key) is child:
                self.row(key).update(returncode=code, ended=stamp())
                self.save()

    def stop(self, key):
        with self.lock:
            row = self.row(key)
            child = self.children.get(key)
            if child is None or child.poll() is not None:
                raise HTTPException(409, '没有可控制的运行进程；不会按旧PID终止进程')
            if row.get('stop_requested'):
                return {'status': 'Stopping'}
            child.stdin.write(b'STOP\n')
            child.stdin.flush()
            row['stop_requested'] = True
            self.save()
            return {'status': 'Stopping'}

    def close(self):
        # EOF requests the same interrupt if the UI service shuts down. Never
        # claim cleanup succeeded before the child writes its actual result.
        with self.lock:
            for child in self.children.values():
                if child.stdin and not child.stdin.closed:
                    child.stdin.close()

    def detail(self, key):
        with self.lock:
            row = self.row(key).copy()
            status = self.status(row)
        run = Path(row['run'])
        writing = []
        def record(name):
            path = run / name
            if not path.exists():
                return None
            try:
                return read_json(path)
            except json.JSONDecodeError:
                if status not in ('Running', 'Stopping'):
                    raise
                writing.append(name)
                return None
        result, task = record('result.json'), record('task.json')
        preflight = record('tools/environment-preflight/result.json')
        can_resume, resume_error = False, None
        if status == 'Paused' and result and result.get('waiting_user') and not row.get('imported'):
            from .continuation import load_continuation
            try:
                load_continuation(run)
                can_resume = True
            except (ValueError, OSError) as exc:
                resume_error = str(exc)
        startup_error = None
        worker_receipt = Path(row['worker_receipt']) if row.get('worker_receipt') else None
        worker_failed = (status not in ('Running', 'Stopping') and worker_receipt
                         and worker_receipt.exists() and read_json(worker_receipt).get('returncode') not in (0, 3))
        if (status == 'ExitedWithoutResult' or worker_failed) and row.get('job'):
            log = Path(row['job']) / 'worker.log'
            if log.exists():
                startup_error = '启动或续做进程未正常完成，请核对错误和失败路径；旧结果保留，未自动重试。\n' + log.read_text(encoding='utf-8', errors='replace')[-4000:]
        independent = {'status': 'NOT_RUN'}
        if row.get('export'):
            independent = read_json(Path(row['export']) / 'delivery-summary.json')['independent']
        return {**row, 'status': status, 'result': result, 'task': task, 'independent': independent,
                'environment_preflight': preflight, 'can_resume': can_resume,
                'resume_error': resume_error, 'startup_error': startup_error,
                'total_tokens': total_tokens(result),
                'records_being_written': writing,
                'recorded_requests': len(list((run / 'model').glob('*/request.json'))),
                'workspace': str(run / 'workspace')}

    def events(self, key, cursor):
        path = Path(self.row(key)['run']) / 'tools/events.jsonl'
        if not path.exists():
            return {'events': [], 'cursor': 0, 'pending': False, 'has_more': False}
        if cursor > path.stat().st_size:
            raise ValueError('事件文件大小发生变化，请核对原始记录')
        events, pending = [], False
        with path.open('rb') as stream:
            stream.seek(cursor)
            for _ in range(50):
                start = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b'\n'):
                    pending = True
                    stream.seek(start)
                    break
                events.append(json.loads(line))
            cursor = stream.tell()
            has_more = not pending and cursor < path.stat().st_size
        return {'events': events, 'cursor': cursor, 'pending': pending, 'has_more': has_more}

    def import_run(self, path):
        run = existing_path(path, directory=True)
        result, task = read_json(run / 'result.json'), read_json(run / 'task.json')
        if result.get('format') != 'dfx-solve-1':
            raise ValueError('仅支持完整保存的 solve 任务')
        with self.lock:
            found = next((r for r in self.rows if Path(r['run']) == run), None)
            if found:
                return {'id': found['id']}
            row = {'id': identifier(), 'run': str(run), 'title': task['task'][:70],
                   'repo': task['repo'], 'mode': result['evaluation_scope'], 'created': stamp(), 'imported': True}
            self.rows.append(row)
            self.save()
            return {'id': row['id']}

    def export(self, key, evaluation):
        from .run_report import write_agent_delivery
        row = self.row(key)
        if self.status(row) in ('Running', 'Stopping', 'Unobserved'):
            raise ValueError('任务未取得终态，暂不能导出最终摘要')
        output = self.root / 'exports' / identifier()
        summary = write_agent_delivery(Path(row['run']), output,
                        evaluation=existing_path(evaluation, directory=True) if evaluation else None)
        with self.lock:
            row['export'] = str(output)
            self.save()
        return summary

    def demo(self):
        source = self.root / 'demo-inputs' / identifier()
        repo = source / '中文 示例工程'
        repo.mkdir(parents=True)
        (repo / 'calc.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
        (repo / 'check.py').write_text('from calc import add\nassert add(2, 3) == 5\nprint("检查通过")\n', encoding='utf-8')
        command = "& '" + sys.executable.replace("'", "''") + "' check.py; exit $LASTEXITCODE"
        actions = [{'action': 'read', 'path': 'calc.py'}, {'action': 'exec', 'command': command},
                   {'action': 'edit', 'path': 'calc.py', 'search': 'a - b', 'replace': 'a + b'},
                   {'action': 'exec', 'command': command},
                   {'action': 'finish', 'summary': '零 API 夹具完成真实工具往返；不是模型自主修复成绩。',
                    'unverified': ['模型自主能力', 'HarmonyOS', '独立验收']}]
        fixture = source / 'responses.json'
        fixture.write_text(json.dumps([{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': f'demo_{i}', 'type': 'function', 'function': {'name': a['action'],
                           'arguments': json.dumps({k: v for k, v in a.items() if k != 'action'}, ensure_ascii=False)}}]}}
            for i, a in enumerate(actions)], ensure_ascii=False, indent=2), encoding='utf-8')
        return {'repo': str(repo), 'responses': str(fixture), 'mode': 'fixture',
                'task': '修复加法行为并运行工程内的检查。此处执行固定夹具，只验证工作台与真实 CLI 工具链。'}


def create_app(root, *, allow_live=False, allow_device=False):
    manager = Workbench(root, allow_live=allow_live, allow_device=allow_device)

    @asynccontextmanager
    async def lifespan(app):
        yield
        manager.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager

    @app.middleware('http')
    async def local_only(request, call_next):
        if request.url.hostname not in ('127.0.0.1', 'localhost'):
            return JSONResponse({'detail': '仅限本机访问'}, status_code=403)
        if request.method not in ('GET', 'HEAD'):
            if request.headers.get('origin') != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': '写操作需要同源页面'}, status_code=403)
            if request.headers.get('content-type', '').split(';')[0] != 'application/json':
                return JSONResponse({'detail': '需要 JSON 请求'}, status_code=415)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(OSError)
    async def filesystem_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.get('/api/config')
    def config():
        return {'version': __version__, 'python': sys.executable, 'data_dir': str(manager.root),
                'allow_live': allow_live, 'allow_device': allow_device,
                'api_key_configured': bool(os.environ.get('DEEPSEEK_API_KEY')),
                'model': 'deepseek-flash', 'thinking': 'enabled', 'reasoning_effort': 'high'}

    @app.get('/api/runs')
    def runs():
        return manager.list_runs()

    @app.post('/api/runs')
    def start(data: TaskInput):
        return manager.start(data)

    @app.get('/api/assignments')
    def assignments():
        return manager.assignments.list_tasks(manager.list_runs())

    @app.get('/api/environment/discover')
    def discover():
        from .member_tasks import discover_deveco
        return {'deveco': discover_deveco(), 'python': sys.executable,
                'api_key_configured': bool(os.environ.get('DEEPSEEK_API_KEY'))}

    @app.post('/api/environment')
    def environment(data: MemberSettings):
        if any(manager.status(r) in ('Running', 'Stopping', 'Unobserved') for r in manager.rows):
            raise ValueError('任务运行期间不能切换环境')
        return manager.assignments.save_settings(data.model_dump())

    @app.post('/api/environment/devices')
    def devices(data: DeviceProbe):
        if not allow_device:
            raise ValueError('当前零API服务不连接设备；可手动填写，执行版支持识别')
        from .member_tasks import check_deveco
        from .validation import execute, EXECUTION_ENV
        root = check_deveco(data.deveco)
        log = manager.root / 'environment' / (identifier() + '.log')
        log.parent.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k.upper() in EXECUTION_ENV}
        result = execute([str(root / 'sdk/default/openharmony/toolchains/hdc.exe'), 'list', 'targets'],
                         root, env, 30, log)
        if result.get('returncode') != 0:
            raise ValueError('HDC连接检查失败：' + str(result))
        targets = [s.strip() for s in log.read_text(encoding='utf-8', errors='replace').splitlines()
                   if re.fullmatch(r'[A-Za-z0-9_.:-]+', s.strip()) and s.strip() != 'Empty']
        return {'devices': targets, 'message': '未发现设备，请先启动专用模拟器' if not targets else ''}

    @app.post('/api/assignments/prepare')
    def prepare_assignment(data: AssignmentInput):
        with manager.lock:
            return manager.assignments.prepare(data.task_id)

    @app.post('/api/assignments/start')
    def start_assignment(data: PreparedInput):
        return manager.start(TaskInput(**manager.assignments.binding(data.prepared_id)))

    @app.post('/api/assignments/feedback')
    def feedback(data: MemberFeedback):
        if data.run_id:
            row = manager.row(data.run_id)
            if row.get('assignment_id') != data.task_id:
                raise ValueError('反馈与任务运行不一致')
        return manager.assignments.save_feedback(data.task_id, data.model_dump(exclude={'task_id'}))

    @app.post('/api/assignments/export')
    def export_assignments():
        with manager.lock:
            path = manager.assignments.export(manager.list_runs())
        return {'filename': path.name, 'url': '/api/assignments/download/' + path.name}

    @app.get('/api/assignments/download/{name}')
    def download_assignments(name: str):
        path = confined(manager.root / 'exports', name)
        if not name.endswith('.zip') or not path.is_file(): raise HTTPException(404, '反馈包不存在')
        return FileResponse(path, media_type='application/zip', filename=name)

    @app.post('/api/demo')
    def demo():
        return manager.demo()

    @app.post('/api/import')
    def import_run(data: PathInput):
        return manager.import_run(data.path)

    @app.post('/api/scope')
    def scope(data: PathInput):
        value = read_json(existing_path(data.path))
        if not isinstance(value, dict) or set(value) - {'repo', 'outbound_approved', 'exclude', 'read_only'}:
            raise ValueError('不是现有格式的项目范围文件')
        return value

    @app.get('/api/runs/{key}')
    def detail(key: str):
        return manager.detail(key)

    @app.get('/api/runs/{key}/events')
    def events(key: str, cursor: int = Query(0, ge=0)):
        return manager.events(key, cursor)

    @app.post('/api/runs/{key}/stop')
    def stop(key: str):
        return manager.stop(key)

    @app.post('/api/runs/{key}/resume')
    def resume_run(key: str, data: ResumeInput):
        return manager.resume(key, data.waiting_tool)

    @app.post('/api/runs/{key}/export')
    def export(key: str, data: ExportInput):
        return manager.export(key, data.evaluation)

    @app.get('/api/runs/{key}/summary')
    def summary(key: str):
        row = manager.row(key)
        if not row.get('export'):
            raise HTTPException(404, '尚未导出摘要')
        return FileResponse(Path(row['export']) / 'delivery-summary.md',
                            media_type='application/octet-stream', filename='delivery-summary.md')

    @app.get('/api/runs/{key}/tree')
    def tree(key: str, directory: str = 'workspace', offset: int = Query(0, ge=0)):
        root = Path(manager.row(key)['run'])
        path = confined(root, directory)
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        return {'entries': [{'name': p.name, 'path': p.relative_to(root).as_posix(), 'directory': p.is_dir()}
                            for p in entries[offset:offset + 200]],
                'next': offset + 200 if len(entries) > offset + 200 else None}

    @app.get('/api/runs/{key}/file')
    def file(key: str, path: str, download: bool = False):
        row = manager.row(key)
        target = (Path(row['job']) / 'worker.log' if path == '@worker.log' and row.get('job')
                  else confined(Path(row['run']), path))
        if not target.is_file():
            raise HTTPException(404, '文件尚未生成或不存在')
        if download:
            return FileResponse(target, media_type='application/octet-stream', filename=target.name)
        with target.open('rb') as stream:
            data = stream.read(256000)
        return {'text': data.decode('utf-8', errors='replace'), 'truncated': target.stat().st_size > len(data)}

    assets = Path(__file__).with_name('web')
    app.mount('/', StaticFiles(directory=assets, html=True), name='ui')
    return app


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description='DFX local workbench; offline by default')
    parser.add_argument('--data-dir', type=Path, default=Path('reports/workbench'))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--allow-live', action='store_true', help='Enable separately approved live task inputs')
    parser.add_argument('--allow-device', action='store_true', help='Enable separately approved device inputs')
    parser.add_argument('--open-browser', action='store_true', help='Open the local UI when its server is ready')
    args = parser.parse_args()
    if args.open_browser:
        import urllib.request
        import webbrowser
        def open_when_ready():
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/api/config', timeout=2) as response:
                    actual = json.load(response)
                if actual['data_dir'] == str(args.data_dir.resolve()):
                    webbrowser.open(f'http://127.0.0.1:{args.port}')
            except (OSError, ValueError):
                print(f'请在浏览器打开 http://127.0.0.1:{args.port}', flush=True)
        threading.Timer(1.5, open_when_ready).start()
    uvicorn.run(create_app(args.data_dir, allow_live=args.allow_live, allow_device=args.allow_device),
                host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
