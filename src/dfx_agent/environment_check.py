"""Pre-model environment preparation using existing vendor tools; never builds or repairs."""
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
import zipfile
import shutil

from .task import files
from .validation import execute, EXECUTION_ENV
from .workspace import save_json, read_json, is_link
from .harmony_build import build_environment


def api_number(value):
    if isinstance(value, int):
        return value
    text = str(value or '')
    match = re.search(r'\((\d+)\)', text)
    return int(match[1]) if match else int(text) if text.isdigit() else None


def project_requirements(root, deveco):
    # Extract selected fields in the vendor's JSON5 parser. Never return signing material.
    names = [n for n in files(root) if Path(n).name in
             ('build-profile.json5', 'oh-package.json5', 'module.json5', 'hvigor-config.json5')]
    script = r"""
const fs=require('fs'), path=require('path'), j=require(process.argv[1]);
const root=process.argv[2], names=JSON.parse(fs.readFileSync(0,'utf8')), result=[];
for(const name of names) {
 try {
  const o=j.parse(fs.readFileSync(path.join(root,name),'utf8').replace(/^\uFEFF/,''));
  const row={path:name};
  if(name==='build-profile.json5') row.products=(o.app?.products||[]).map(p=>({
    name:p.name, target:p.targetSdkVersion, compatible:p.compatibleSdkVersion,
    compile:p.compileSdkVersion, runtime:p.runtimeOS}));
  if(name.endsWith('oh-package.json5')) row.dependencies=['dependencies','devDependencies','dynamicDependencies'].flatMap(kind=>
    Object.entries(o[kind]||{}).map(([name,version])=>({kind,
    name,version:typeof version==='string' && !version.includes('://') ? version : '[URL/object omitted]'})));
  if(o.module) row.module={type:o.module.type,deviceTypes:o.module.deviceTypes||[]};
  if(name.endsWith('hvigor-config.json5')) row.modelVersion=o.modelVersion;
  result.push(row);
 } catch(e) {result.push({path:name,error:'JSON5_PARSE_ERROR'});}
}
console.log(JSON.stringify(result));
"""
    response = subprocess.run([str(deveco / 'tools/node/node.exe'), '-e', script,
        str(deveco / 'tools/hvigor/hvigor-ohos-plugin/node_modules/json5'), str(root)],
        input=json.dumps(names), capture_output=True, encoding='utf-8', timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if response.returncode:
        raise ValueError('DevEco JSON5读取器不可用；未调用模型')
    rows = json.loads(response.stdout)
    products = next((r.get('products', []) for r in rows if r['path'] == 'build-profile.json5'), [])
    kits = sorted({kit for name in files(root) if Path(name).suffix in {'.ets', '.ts'}
                   for kit in re.findall(r'[\'\"](@kit\.[A-Za-z0-9_]+)[\'\"]',
                                        (root / name).read_text(encoding='utf-8', errors='replace'))})
    return {'products': products, 'system_kits': kits,
            'dependencies': [dict(d, manifest=r['path']) for r in rows for d in r.get('dependencies', [])],
            'modules': [dict(r['module'], path=r['path']) for r in rows if 'module' in r],
            'configuration_errors': [r['path'] for r in rows if r.get('error')],
            'hvigor': [r for r in rows if 'modelVersion' in r]}


def installed_sdks(deveco):
    result = []
    for path in sorted((deveco / 'sdk').glob('*/sdk-pkg.json')):
        data = read_json(path).get('data', {})
        result.append({'path': str(path.parent), 'api': api_number(data.get('apiVersion')),
                       'name': data.get('displayName'), 'version': data.get('version')})
    return result


def elf_arch(header):
    if len(header) < 20 or header[:4] != b'\x7fELF' or header[5] not in (1, 2):
        return 'UNKNOWN'
    machine = int.from_bytes(header[18:20], 'little' if header[5] == 1 else 'big')
    return {183: 'arm64-v8a', 62: 'x86_64', 40: 'armeabi-v7a', 3: 'x86'}.get(machine, 'UNKNOWN')


def normalize_arch(value):
    return {'aarch64': 'arm64-v8a', 'arm64': 'arm64-v8a', 'amd64': 'x86_64',
            'armv7l': 'armeabi-v7a'}.get(value.lower().strip(), value.lower().strip())


def package_libraries(path):
    """Only libraries actually shipped in this HAP/HSP can block installation."""
    groups = {}
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.endswith('.so'):
                continue
            key = re.sub(r'/(arm64-v8a|aarch64|x86_64|armeabi-v7a|x86)/', '/{abi}/', name)
            row = groups.setdefault(key, {'library': key, 'architectures': []})
            with archive.open(name) as stream:
                arch = elf_arch(stream.read(20))
            if arch not in row['architectures']:
                row['architectures'].append(arch)
    return list(groups.values())


def check_java(deveco):
    env = build_environment(deveco)
    resolved = shutil.which('java', path=env['PATH'])
    expected = deveco / 'jbr/bin' / ('java.exe' if os.name == 'nt' else 'java')
    if not resolved or Path(resolved).resolve() != expected.resolve():
        raise ValueError('构建子进程无法解析所选DevEco的Java；请检查jbr/bin安装。')
    return str(expected)


def native_libraries(root):
    """Read ELF headers, including resolved OHPM HARs. No extraction or execution."""
    groups = {}
    def add(location, header):
        # Keep the package/module path: a same-named library in another package
        # must not hide an incompatible dependency.
        key = re.sub(r'/(arm64-v8a|aarch64|x86_64|armeabi-v7a|x86)/', '/{abi}/', location)
        row = groups.setdefault(key, {'library': key, 'architectures': [], 'paths': []})
        arch = elf_arch(header)
        if arch not in row['architectures']: row['architectures'].append(arch)
        row['paths'].append(location)
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in {'.git', '.hvigor', '.cxx', 'build', 'node_modules'}
                         and not is_link(base / d))
        for name in sorted(names):
            path = base / name
            if path.is_symlink(): continue
            relative = path.relative_to(root).as_posix()
            if path.suffix == '.so':
                with path.open('rb') as stream: add(relative, stream.read(20))
            elif path.suffix == '.har':
                if zipfile.is_zipfile(path):
                    with zipfile.ZipFile(path) as archive:
                        for member in archive.infolist():
                            if member.filename.endswith('.so'):
                                with archive.open(member) as stream: add(relative + '!' + member.filename, stream.read(20))
                elif tarfile.is_tarfile(path):
                    with tarfile.open(path) as archive:
                        for member in archive:
                            if member.isfile() and member.name.endswith('.so'):
                                with archive.extractfile(member) as stream: add(relative + '!' + member.name, stream.read(20))
                else:
                    raise ValueError('无法检查HAR原生库：' + relative)
    return list(groups.values())


def run_preflight(harmony, tools):
    """Called once before the first model request, in its persistent workspace."""
    out = harmony.output / 'environment-preflight'
    out.mkdir()
    started = time.monotonic()
    report = {'status': 'CHECKING', 'stage': '工具链与SDK', 'model_dispatched': False,
              'issues': [], 'warnings': [], 'commands': [], 'dependency_changes': []}
    def save():
        report['seconds'] = time.monotonic() - started
        save_json(out / 'result.json', report)
    def block(message):
        report.update(status='BLOCKED', error=message)
        report['issues'].append(message)
        save()
        return report
    def command(args, name):
        log = out / (name + '.log')
        env = {k: v for k, v in os.environ.items() if k.upper() in EXECUTION_ENV}
        receipt = execute([str(harmony.hdc), '-t', harmony.host['device'], 'shell', *args],
                          harmony.root, env, 30, log)
        report['commands'].append({'name': name, **receipt})
        if receipt.get('returncode') != 0:
            raise ValueError('设备只读检查失败：' + name)
        return log.read_text(encoding='utf-8', errors='replace').strip()
    save()
    try:
        if os.name == 'nt' and not re.fullmatch(r'[A-Za-z]:[\\/][A-Za-z0-9_.() @/\\-]*', str(harmony.root.resolve())):
            return block('Hvigor不支持当前工作副本路径：' + str(harmony.root.resolve()) +
                         '。请使用如D:\\dfx-r\\local-0201的英文运行目录；原目录和记录未移动，未调用模型。')
        from .member_tasks import check_deveco
        check_deveco(str(harmony.deveco))
        report['java'] = check_java(harmony.deveco)
        requirements = project_requirements(harmony.root, harmony.deveco)
        report['requirements'] = requirements
        report['sdks'] = installed_sdks(harmony.deveco)
        available = [s['api'] for s in report['sdks'] if s['api'] is not None]
        requested = [api_number(p.get('compile') or p.get('target')) for p in requirements['products']]
        requested = [n for n in requested if n is not None]
        if not available:
            return block('未识别到DevEco中的HarmonyOS SDK元数据；请在SDK Manager完成安装。')
        if requested and max(requested) > max(available):
            return block(f'本机SDK不足：工程编译/目标API {max(requested)}，已识别API {available}；请在DevEco补齐SDK，不降低工程版本。')
        if not requested or requirements['configuration_errors']:
            report['warnings'].append('工程SDK声明缺失或JSON5配置错误：保留原配置，交给Agent调查；未宣称SDK兼容。')
        if requested and available and min(requested) < max(available):
            report['warnings'].append('工程目标低于本机SDK：未证明全部API兼容，后续构建结果由Agent处理。')
        report['stage'] = '设备架构与系统API'
        save()
        arch = normalize_arch(command(['uname', '-m'], 'device-architecture'))
        api = api_number(command(['param', 'get', 'const.ohos.apiversion'], 'device-api'))
        report['device'] = {'id': harmony.host['device'], 'architecture': arch, 'api': api}
        if arch not in {'arm64-v8a', 'x86_64', 'armeabi-v7a', 'x86'} or api is None:
            return block('无法识别设备架构或系统API；请检查设备连接，原始只读输出已保存。')
        minimums = [api_number(p.get('compatible')) for p in requirements['products']]
        minimums = [n for n in minimums if n is not None]
        if minimums and api < max(minimums):
            return block(f'设备API {api}低于工程最低API {max(minimums)}；请选择兼容镜像或真机。')
        if '@kit.Penkit' in requirements.get('system_kits', []):
            report['stage'] = 'Pen Kit系统包可用性'
            save()
            penkit = command(['bm', 'dump', '-n', 'com.huawei.hmos.hwstylusfeature'], 'penkit-package')
            # Package presence is a necessary check, not proof its ABC can load.
            if 'com.huawei.hmos.hwstylusfeature' not in penkit or 'Penkit' not in penkit:
                return block('工程使用Pen Kit，但未查到其系统包/模块；需支持该Kit的镜像或设备，不删除手写功能。')
            report['warnings'].append('已发现Pen Kit系统包；是否能加载仍需实际启动验证，包存在不代表旧崩溃已解决。')
        report.update(stage='自动准备项目依赖')
        save()
        # Reuse the existing executor and its timeout/cleanup, not another installer.
        dependencies = harmony.build({'operation': 'dependencies'}, out)
        dependencies = tools.check_read_only(dependencies)
        report['dependency_result'] = {k: v for k, v in dependencies.items() if k != 'output'}
        from .task import export_diff
        report['dependency_changes'] = export_diff(harmony.root.parent)['changed_files']
        if dependencies.get('status') != 'EXECUTED':
            return block('依赖准备未完成，未调用模型；请查看 environment-preflight/command.log。' +
                         (' ' + dependencies['error'] if dependencies.get('error') else ''))
        report.update(stage='原生库兼容检查')
        save()
        libraries = native_libraries(harmony.root)
        report['native_libraries'] = libraries
        incompatible = [r for r in libraries if arch not in r['architectures'] and 'UNKNOWN' not in r['architectures']]
        if incompatible:
            report['incompatible_libraries'] = incompatible
            report['warnings'].append('依赖候选包含不支持设备架构 ' + arch + ' 的库：' +
                         ', '.join(r['library'] for r in incompatible[:4]) +
                         '。尚未证明这些库会被打包；安装前检查实际HAP/HSP，不兼容则停止，不删库。')
        if any('UNKNOWN' in r['architectures'] for r in libraries):
            return block('存在无法识别的原生库，未宣称兼容；请核对预检中的文件。')
        report['warnings'].append('预检通过仅代表未发现上述环境障碍；未构建、安装或验证应用业务。系统Kit、签名及实际打包依赖仍以运行结果为准。')
        report.update(status='READY', stage='环境准备完成，交给Agent自主执行')
        save()
        return report
    except KeyboardInterrupt:
        report.update(status='INTERRUPTED', error='环境准备已中断；未调用模型，不自动重试。')
        save()
        raise
    except (OSError, ValueError, subprocess.TimeoutExpired, zipfile.BadZipFile, tarfile.TarError) as exc:
        return block('环境检查无法完成：' + str(exc))
