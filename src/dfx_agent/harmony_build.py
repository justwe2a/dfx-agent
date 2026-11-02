"""Vendor build primitives shared by legacy validation and task-driven tools."""
import os
import json
from pathlib import Path
import subprocess
import zipfile

from .validation import EXECUTION_ENV


def build_environment(deveco):
    env = {k: v for k, v in os.environ.items() if k.upper() in EXECUTION_ENV}
    env.update(DEVECO_SDK_HOME=str(deveco / 'sdk'), JAVA_HOME=str(deveco / 'jbr'))
    inherited_path = next((v for k, v in env.items() if k.upper() == 'PATH'), '')
    env = {k: v for k, v in env.items() if k.upper() != 'PATH'}
    env['PATH'] = os.pathsep.join([str(deveco / 'jbr/bin'), str(deveco / 'tools/node'),
                                 str(deveco / 'tools/ohpm/bin'), inherited_path])
    for key, value in os.environ.items():
        if key.upper() in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}:
            env[key] = value  # Child environment only; never log proxy credentials.
    return env


def dependency_command(deveco):
    return [str(deveco / 'tools/ohpm/bin/ohpm.bat'), 'install', '--all']


def build_command(deveco, module, product, target, task='assembleHap'):
    return [str(deveco / 'tools/hvigor/bin/hvigorw.bat'), '--mode', 'module',
            '-p', f'product={product}', '-p', f'module={module}@{target}',
            '-p', 'buildMode=debug', task, '--no-daemon']


def package_metadata(path):
    """Use the actual packaged dependencies, not directory names or dependency counts."""
    try:
        with zipfile.ZipFile(path) as archive:
            data = json.loads(archive.read('module.json'))
        module = data['module']
        if not isinstance(module.get('name'), str) or not isinstance(module.get('dependencies', []), list):
            raise ValueError('Invalid module identity/dependencies')
        return data
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise ValueError(f'Cannot read HAP/HSP module.json: {path.name}: {exc}') from exc


def project_modules(deveco, root):
    """Read the vendor JSON5 module mapping once, only for a package needing HSPs."""
    script = r"""
const fs=require('fs'),p=require('path'),j=require(process.argv[1]),root=process.argv[2];
const profile=j.parse(fs.readFileSync(p.join(root,'build-profile.json5'),'utf8'));
console.log(JSON.stringify((profile.modules||[]).map(m=>{
 const src=m.srcPath, o=j.parse(fs.readFileSync(p.join(root,src,'src/main/module.json5'),'utf8'));
 return {name:m.name,path:src,module:o.module.name,type:o.module.type};
})));
"""
    result = subprocess.run([str(deveco / 'tools/node/node.exe'), '-e', script,
        str(deveco / 'tools/hvigor/hvigor-ohos-plugin/node_modules/json5'), str(root)],
        capture_output=True, encoding='utf-8', timeout=30, env=build_environment(deveco),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise ValueError('Cannot read project module mapping: ' + result.stderr[-2000:])
    return json.loads(result.stdout)
