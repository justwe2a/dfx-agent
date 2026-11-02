"""Portable official-task inputs for the existing workbench; never solves a case."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

from .task import files, matches, PRIVATE
from .workspace import confined, read_json, save_json

DATA = Path(__file__).with_name('data') / 'member-tasks'
INPUT_EXCLUDE = PRIVATE + ['test', 'tests', 'testcases', 'ohosTest', 'DFXBenchMark_TC',
    'TC_*', 'jscrash*', 'cppcrash*', 'faultlogger', 'Logs', 'logs', 'reports',
    '*.py', '*.ps1', '*.bat', '*.cmd', '*.log', '*.zip', '*.hap', 'test_*.abc',
    '*.p7b', '*.cer', '.home', 'local.properties', '.npmrc', '.ohpmrc',
    'AGENTS.md', 'mark-files.json5', 'sessions/*.json', 'sessions/*.md']
# Official evaluator scripts are removed before the workspace exists. New scripts
# written by the Agent in that clean workspace must remain usable for self-checks.
EXCLUDE = [name for name in INPUT_EXCLUDE if name not in
           ('test', 'tests', '*.py', '*.ps1', '*.bat', '*.cmd')]
# App business fixtures remain readable; evaluator fixtures and old diagnoses do not.
READ_ONLY_SOURCE = ['*MockData.ets', '*MockData.ts', '*mockData.ets', '*mockData.ts', '*Privacy*', '*Agreement*', '*Splash*', '*splash*',
                    '*EntryAbility*', '*MainEntry*', '*compat*', '*ohosTest*']
POLICY = ('本批任务业务源码按需可读改，工程配置、内置业务数据、启动/协议/广告源码只读；'
          '排除官方测试/根因资料、旧会话、凭据和原始日志。发送精简错误栈及本任务开发输出、'
          '目标应用UI、新故障、必要路径/设备标识。只在专用测试设备使用合成数据。')


def load_catalog():
    data = read_json(DATA / 'catalog.json')
    identity = DATA / 'member.json'
    if identity.exists():
        member = read_json(identity)['member']
        data = [t for t in data if t['member'] == member]
    return data


def environment_only_attempt(row):
    """Explicit recheck is allowed only when the saved preflight never dispatched a model."""
    path = Path(row['run']) / 'result.json'
    if row.get('status') in ('Running', 'Stopping', 'Unobserved'):
        return False
    if not path.is_file():
        # A confirmed exited worker can fail during copy/Git before model dispatch.
        # A missing result alone never proves that the process stopped.
        return (row.get('status') in ('ExitedWithoutResult', 'LaunchError') and
                not (path.parent / 'trajectory.json').exists() and
                not any((path.parent / 'model').glob('*/request.json')))
    result = read_json(path)
    return (result.get('status') == 'BLOCKED' and result.get('model_calls') == 0
            and result.get('api_calls') == 0
            and (result.get('environment_preflight') or {}).get('status') == 'BLOCKED')


def dataset_root(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('请选择已下载官方数据的本机目录')
    path = path.resolve()
    candidates = [path, path / 'data_B']
    # Official ZIPs commonly add one or two data-master directories. No drive scan.
    for depth in (1, 2, 3):
        candidates.extend(path.glob('/'.join(['*'] * depth + ['data_B'])))
    found = {p.resolve() for p in candidates if all((p / x).is_dir() for x in ('data_B1', 'data_B2', 'data_B3'))}
    if len(found) != 1:
        raise ValueError('未找到唯一data_B目录，请直接选择含data_B1、data_B2、data_B3的目录')
    return found.pop()


def discover_deveco():
    roots = [Path('D:/HarmonyOS/DevEco Studio'), Path('C:/Program Files/Huawei/DevEco Studio')]
    for env in ('DEVECO_HOME',):
        if os.environ.get(env): roots.append(Path(os.environ[env]))
    executable = shutil.which('hvigorw.bat')
    if executable: roots.append(Path(executable).resolve().parents[3])
    if os.name == 'nt':
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for branch in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                           r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
                try:
                    with winreg.OpenKey(hive, branch) as key:
                        for i in range(winreg.QueryInfoKey(key)[0]):
                            try:
                                with winreg.OpenKey(key, winreg.EnumKey(key, i)) as app:
                                    if 'deveco' in str(winreg.QueryValueEx(app, 'DisplayName')[0]).lower():
                                        roots.append(Path(winreg.QueryValueEx(app, 'InstallLocation')[0]))
                            except OSError:
                                continue
                except OSError:
                    continue
    return sorted({str(p.resolve()) for p in roots if (p / 'tools/hvigor/bin/hvigorw.bat').is_file()})


def check_deveco(value):
    root = Path(value).expanduser()
    if not value or not root.is_absolute():
        raise ValueError('请选择DevEco Studio安装目录')
    required = ['tools/hvigor/bin/hvigorw.bat', 'tools/ohpm/bin/ohpm.bat', 'tools/node/node.exe',
        'sdk/default/openharmony/toolchains/hdc.exe', 'jbr/bin/java.exe']
    missing = [n for n in required if not (root / n).is_file()]
    if missing: raise ValueError('DevEco工具不完整：' + ', '.join(missing))
    return root.resolve()


def json5_operation(deveco, source, target=None):
    # Use the vendor parser already installed with the required build tools.
    parser = deveco / 'tools/hvigor/hvigor-ohos-plugin/node_modules/json5'
    script = r"""
const fs=require('fs'), j=require(process.argv[1]);
try {
 const o=j.parse(fs.readFileSync(process.argv[2],'utf8').replace(/^\uFEFF/,''));
 if(process.argv[3]) {
   const changed=[];
   if(o.app?.signingConfigs?.length) {o.app.signingConfigs=[]; changed.push('app.signingConfigs');}
   for(const p of o.app?.products||[]) if('signingConfig' in p) {delete p.signingConfig;changed.push('product.signingConfig');}
   if(changed.length) fs.writeFileSync(process.argv[3],JSON.stringify(o,null,2)+'\n','utf8');
   console.log(JSON.stringify({removed:changed}));
 } else console.log(JSON.stringify({bundle:o.app?.bundleName}));
} catch(e) {console.error('JSON5读取失败，未输出原始配置');process.exit(1);}
"""
    command = [str(deveco / 'tools/node/node.exe'), '-e', script, str(parser), str(source)]
    if target: command.append(str(target))
    try:
        result = subprocess.run(command, capture_output=True, encoding='utf-8', timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('JSON5解析超过30秒，未继续准备或调用模型') from exc
    if result.returncode:
        raise ValueError('无法解析工程JSON5；请检查DevEco自带json5模块，原配置未外发')
    return json.loads(result.stdout)


def log_excerpt(text, bundle):
    module = re.search(r'^Module name:\s*(\S+)', text, re.M)
    if not module or module.group(1) != bundle:
        return None
    lines = text.splitlines()
    selected = [s[:500] for s in lines if s.startswith(('Reason:', 'Error name:', 'Error message:'))]
    stack = False
    for line in lines:
        if line.strip() == 'Stacktrace:':
            stack = True
            continue
        if stack:
            if not line.strip() or not line.lstrip().startswith('at '): break
            # Only project ArkTS frames; no device/system/native metadata.
            if '.ets:' in line or '.ts:' in line:
                selected.append(line[:600])
            if len(selected) >= 23: break
    return '\n'.join(selected) if selected else None


class MemberTasks:
    def __init__(self, root):
        self.root = Path(root)
        self.catalog = load_catalog()

    def task(self, task_id):
        for task in self.catalog:
            if task['task_id'] == task_id: return task
        raise ValueError('任务不属于当前组员包；请使用完整split/app/TC标识')

    def settings(self):
        path = self.root / 'member-settings.json'
        return read_json(path) if path.exists() else {}

    def save_settings(self, data):
        members = {t['member'] for t in self.catalog}
        if data['member'] not in members: raise ValueError('组员与当前执行包不符')
        dataset = dataset_root(data['dataset'])
        deveco = check_deveco(data['deveco'])
        if not data['device'].strip(): raise ValueError('请选择或填写本机模拟器标识')
        if self.root.resolve().is_relative_to(dataset): raise ValueError('结果目录必须在官方数据外')
        value = dict(data, dataset=str(dataset), deveco=str(deveco), device=data['device'].strip())
        save_json(self.root / 'member-settings.json', value)
        return value

    def list_tasks(self, runs):
        settings = self.settings()
        members = sorted({t['member'] for t in self.catalog})
        if not members:
            return {'settings': settings, 'members': [], 'tasks': [], 'policy': POLICY}
        member = settings.get('member', members[0])
        feedback = self.feedback()
        rows = []
        for task in self.catalog:
            if task['member'] != member: continue
            attempts = sorted((r for r in runs if r.get('assignment_id') == task['task_id']), key=lambda r: r['id'])
            rows.append({**task, 'attempts': [{'id': r['id'], 'status': r['status']} for r in attempts],
                         'environment_recheck_allowed': bool(attempts) and all(environment_only_attempt(r) for r in attempts),
                         'feedback': feedback.get(task['task_id'], {})})
        return {'settings': settings, 'members': members, 'tasks': rows, 'policy': POLICY}

    def prepare(self, task_id):
        task, settings = self.task(task_id), self.settings()
        if not settings: raise ValueError('请先完成一次环境设置')
        if task['member'] != settings['member']: raise ValueError('任务与当前组员不符')
        dataset = dataset_root(settings['dataset'])
        source = confined(dataset, task['source_relative'])
        if not (source / 'AppScope/app.json5').is_file():
            raise ValueError('未找到任务工程：' + str(source))
        deveco = check_deveco(settings['deveco'])
        bundle = json5_operation(deveco, source / 'AppScope/app.json5').get('bundle')
        if bundle != task['bundle_name']:
            raise ValueError('工程包名与任务索引不一致，请回传数据版本差异：' + str(bundle))
        token = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        folder = self.root / 'prepared' / token
        project, logs = folder / 'project', folder / 'logs'
        names = files(source, INPUT_EXCLUDE)
        if not names: raise ValueError('任务工程没有可用开发文件')
        project.mkdir(parents=True)
        logs.mkdir()
        readonly, changes = [], []
        for name in names:
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
            business_entry = (task_id == 'data_B3/ImageProcessing1/TC_JSError_018' and
                              name == 'product/phone/src/main/ets/pages/MainEntry.ets')
            if not business_entry and Path(name).name != 'oh-package-lock.json5' and (Path(name).suffix.lower() not in ('.ets', '.ts') or 'src/main/' not in name or matches(name, READ_ONLY_SOURCE)):
                readonly.append(name)
        # Existing machine signing configuration is not portable or model-visible.
        profile = project / 'build-profile.json5'
        if profile.exists():
            removed = json5_operation(deveco, profile, profile)['removed']
            if removed: changes.append({'path': 'build-profile.json5', 'removed': removed, 'reason': '本机签名配置不随任务外发'})
        notes = []
        for index, location in enumerate(task['official_logs']):
            path, sep, entry = location.partition('!')
            file = confined(dataset, path)
            if not file.is_file():
                notes.append({'source': location, 'status': 'MISSING'})
                continue
            if sep:
                with zipfile.ZipFile(file) as archive:
                    if entry not in archive.namelist():
                        notes.append({'source': location, 'status': 'MISSING_ENTRY'})
                        continue
                    text = archive.read(entry).decode('utf-8-sig', errors='replace')
            else:
                text = file.read_text(encoding='utf-8-sig', errors='replace')
            excerpt = log_excerpt(text, bundle)
            notes.append({'source': location, 'status': 'EXCERPT' if excerpt else 'IDENTITY_OR_FORMAT_MISMATCH'})
            if excerpt: (logs / f'fault-{index + 1}.txt').write_text(excerpt + '\n', encoding='utf-8')
        # Hvigor creates module-root BuildProfile.ets. Only exclude paths absent
        # from the original input; legitimate existing source remains visible.
        module_roots = {Path(n).parent for n in names if Path(n).name == 'build-profile.json5'}
        module_roots.update(Path(n).parents[2] for n in names if n.endswith('/src/main/module.json5'))
        generated = sorted((p / 'BuildProfile.ets').as_posix() for p in module_roots
                           if p != Path('.') and (p / 'BuildProfile.ets').as_posix() not in names)
        scope = {'repo': str(project.resolve()), 'outbound_approved': True,
                 'exclude': EXCLUDE + generated, 'read_only': readonly}
        save_json(folder / 'scope.json', scope)
        host = {'deveco': settings['deveco'], 'device': settings['device']}
        if settings['swipe_only']: host['screen_lock_profile'] = 'swipe_only'
        save_json(folder / 'host.json', host)
        manifest = {'task_id': task_id, 'member': settings['member'], 'source': str(source),
                    'files': names, 'read_only': readonly, 'input_changes': changes,
                    'logs': notes, 'generated_paths_excluded': generated,
                    'policy': POLICY, 'version': 'member-batch-20261029',
                    'scope_notes': ['ImageProcessing MainEntry business page is editable; do not bypass privacy, membership or payment behavior.'] if 'ImageProcessing1' in task_id else []}
        save_json(folder / 'manifest.json', manifest)
        data = {'repo': str(project.resolve()), 'task': task['task'], 'mode': 'deepseek',
                'scope': str((folder / 'scope.json').resolve()), 'host': str((folder / 'host.json').resolve()),
                'logs': str(logs.resolve()), 'max_steps': 120, 'max_output_tokens': 32768,
                'command_timeout': 60, 'script_timeout': 120, 'build_timeout': 1800, 'model_timeout': 180,
                'allow_device': True, 'allow_dependency_install': True, 'allow_system_navigation': True,
                'allow_screen_unlock': settings['swipe_only'], 'assignment_id': task_id,
                'prepared_id': token}
        save_json(folder / 'launch-input.json', data)
        return {'prepared_id': token, 'task_id': task_id, 'task': task['task'], 'title': task['title'],
                'files': len(names), 'editable': len(names) - len(readonly), 'logs': notes,
                'input_changes': changes, 'policy': POLICY, 'project': str(project),
                'check': task['check'], 'known_issue': task.get('known_issue', '')}

    def binding(self, token):
        if not re.fullmatch(r'\d{8}-\d{6}-\d{6}', token): raise ValueError('无效任务准备标识')
        data = read_json(self.root / 'prepared' / token / 'launch-input.json')
        self.task(data['assignment_id'])
        return data

    def feedback(self):
        path = self.root / 'member-feedback.json'
        return read_json(path) if path.exists() else {}

    def save_feedback(self, task_id, value):
        self.task(task_id)
        data = self.feedback()
        data[task_id] = {**value, 'kind': 'MEMBER_OBSERVATION', 'updated': datetime.now().isoformat()}
        save_json(self.root / 'member-feedback.json', data)
        return data[task_id]

    def export(self, runs):
        listing = self.list_tasks(runs)
        if any(r['status'] in ('Running', 'Stopping', 'Unobserved') for r in runs):
            raise ValueError('请等待当前任务结束或确认进程状态后导出反馈包')
        if not listing['members']:
            raise ValueError('No bundled task catalog; export the individual run instead')
        member = listing['settings'].get('member', listing['members'][0])
        output = self.root / 'exports' / f'{member}-feedback-{datetime.now().strftime("%Y%m%d-%H%M%S-%f")}.zip'
        output.parent.mkdir(exist_ok=True)
        summary = []
        with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
            for task in listing['tasks']:
                attempts = sorted((r for r in runs if r.get('assignment_id') == task['task_id']), key=lambda r: r['id'])
                summary.append({'task_id': task['task_id'], 'title': task['title'],
                                'attempts': task['attempts'], 'status': attempts[-1]['status'] if attempts else 'NOT_RUN',
                                'feedback': task['feedback']})
                for row in attempts:
                    run, job = Path(row['run']), Path(row['job'])
                    prefix = f'tasks/{task["order"]:02d}/{row["id"]}/'
                    job_records = ['command.json', 'exit.json', 'worker.log', 'task.txt']
                    job_records += sorted(p.name for p in job.glob('resume-*.json')
                                          if re.fullmatch(r'resume-\d+-(command|exit)\.json', p.name))
                    for name in job_records:
                        path = job / name
                        if path.exists(): archive.write(path, prefix + 'job/' + name)
                    if run.exists():
                        for name in files(run, ['original', 'workspace']):
                            archive.write(run / name, prefix + 'run/' + name)
                        result = read_json(run / 'result.json') if (run / 'result.json').exists() else {}
                        for name in result.get('changed_files', []):
                            path = confined(run / 'workspace', name)
                            if path.is_file(): archive.write(path, prefix + 'candidate-files/' + name)
                    prepared = self.root / 'prepared' / row['prepared_id']
                    for name in ('manifest.json', 'launch-input.json', 'scope.json', 'host.json'):
                        if (prepared / name).exists(): archive.write(prepared / name, prefix + 'input/' + name)
            archive.writestr('SUMMARY.json', json.dumps(summary, ensure_ascii=False, indent=2))
            lines = ['# 组员任务反馈', '', f'成员：{member}', '', '| 任务 | 执行状态 | 独立观察 |', '|---|---|---|']
            for t in summary:
                lines.append(f'| {t["task_id"]} | {t["status"]} | {t["feedback"].get("status", "NOT_RUN")} |')
            lines += ['', 'Submitted不是业务PASS；独立观察为组员填写，不覆盖模型终态。',
                      '保留本机原始workspace与官方输入；包内包含变更文件和原始过程，不重复打包完整数据集。',
                      '发送前检查是否含个人资料；如需脱敏只修改副本，注明处理项，保留原始记录。',
                      '额外AI辅助开发请另附sessions对话摘要；姓名、个人贡献与主观评价由本人补充。']
            archive.writestr('START.md', '\n'.join(lines))
            archive.writestr('ENVIRONMENT.json', json.dumps(listing['settings'], ensure_ascii=False, indent=2))
        return output
