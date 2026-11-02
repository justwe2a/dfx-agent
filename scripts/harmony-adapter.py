"""Trusted build/deploy adapters invoked by the workflow, never by the model.

Uses the already prepared emulator build configuration. No signing changes,
privacy prompt handling, runtime success claims or source repairs are performed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dfx_agent.harmony_device import Device, now
from dfx_agent.harmony_build import build_environment, dependency_command, build_command
from dfx_agent.workspace import save_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['build', 'deploy'])
    parser.add_argument('--deveco', type=Path, default=Path(r'D:\HarmonyOS\DevEco Studio'))
    parser.add_argument('--device', default='127.0.0.1:5555')
    parser.add_argument('--module', default='entry')
    parser.add_argument('--product', default='default')
    parser.add_argument('--target', default='default')
    parser.add_argument('--bundle', default='rlyy.1.xxxxxx')
    parser.add_argument('--artifact', default='product/entry/build/default/outputs/default/entry-default-unsigned.hap')
    args = parser.parse_args()
    workspace = Path(os.environ['DFX_WORKSPACE']).resolve()
    artifact = (workspace / args.artifact).resolve()
    if not artifact.is_relative_to(workspace):
        raise ValueError('Artifact leaves candidate workspace')
    env = build_environment(args.deveco)
    if args.stage == 'build':
        for argv in [dependency_command(args.deveco),
                     build_command(args.deveco, args.module, args.product, args.target)]:
            result = subprocess.run(argv, cwd=workspace, env=env)
            if result.returncode:
                return result.returncode
        return 0
    binding={k:os.environ['DFX_'+k.upper()] for k in ['run_id','attempt_id','snapshot_id','patch_id','stage','artifact_id']}
    result={**binding,'status':'BLOCKED', 'details': {'device': args.device, 'started_at': now(),
            'adapter': 'harmony-adapter-v2'}}
    report=Path(os.environ['DFX_REPORT_PATH'])
    try:
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise ValueError('Artifact is missing or empty')
        hdc=args.deveco/'sdk/default/openharmony/toolchains/hdc.exe'
        device=Device(hdc,args.device,report.parent/'deploy-evidence')
        response=device.command('install', str(artifact), timeout=90)
        print(response)
        result['status']='PASS' if 'install bundle successfully' in response else 'BLOCKED'
        result['details'].update(installed_artifact_id=binding['artifact_id'],returncode=0)
        if result['status']=='PASS':
            # On this emulator PACKAGE_CHANGED arrives after install returns and
            # can close an app launched immediately. Wait once, without retrying it.
            result['details']['install_settle_seconds'] = 5
            time.sleep(5)
            # Record the installed application identity for this run.
            result['details']['installed_package']=device.package(args.bundle)
    except (OSError, ValueError, subprocess.TimeoutExpired, RuntimeError) as exc:
        result['status']='BLOCKED'
        result['details']['reason']=f'{type(exc).__name__}: {exc}'
    result['details']['finished_at']=now()
    save_json(report,result)
    return 0 if result['status']=='PASS' else 2


if __name__=='__main__':
    raise SystemExit(main())
