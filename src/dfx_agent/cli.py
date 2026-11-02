from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from . import cases, doctor, runner
from .model import TranscriptModel, DeepSeekModel


def output_directory(path: Path, dataset: Path | None = None) -> Path:
    resolved = path.resolve()
    if dataset is not None and resolved.is_relative_to(dataset.resolve()):
        raise ValueError("Report output must be outside the original dataset")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DFX repair agent and legacy evaluation tools")
    sub = parser.add_subparsers(dest="command", required=True)
    solve_parser = sub.add_parser('solve', help='Task-driven mini agent; fixtures are not model results')
    solve_parser.add_argument('--repo', type=Path, required=True)
    description = solve_parser.add_mutually_exclusive_group(required=True)
    description.add_argument('--task')
    description.add_argument('--task-file', type=Path)
    solve_parser.add_argument('--out', type=Path, required=True)
    solve_parser.add_argument('--logs', type=Path, help='Reviewed log-only directory')
    solve_parser.add_argument('--scope', type=Path, help='Approved project scope JSON; required for live calls')
    solve_parser.add_argument('--host', type=Path, help='HarmonyOS host paths; loads shared tools and domain skill')
    solve_parser.add_argument('--allow-device', action='store_true', help='Enable explicitly authorized device operations')
    solve_parser.add_argument('--allow-screen-unlock', action='store_true',
                              help='Enable explicit no-password screen recovery; requires device and swipe_only host profile')
    solve_parser.add_argument('--allow-system-navigation', action='store_true',
                              help='Enable explicit Back/Home outside the target window; requires --allow-device')
    solve_parser.add_argument('--allow-dependency-install', action='store_true', help='Enable authorized ohpm install')
    solve_parser.add_argument('--environment-preflight', action='store_true',
                              help='Check SDK/device/native dependencies and prepare OHPM before model calls')
    solve_parser.add_argument('--build-timeout', type=int, default=600)
    backend = solve_parser.add_mutually_exclusive_group(required=True)
    backend.add_argument('--responses', type=Path, help='Offline action fixtures; real local tools, zero model API')
    backend.add_argument('--provider', choices=['deepseek'])
    solve_parser.add_argument('--model', help='Explicit live model name')
    solve_parser.add_argument('--model-protocol', choices=['json-actions', 'tool-calls'], default='json-actions',
                              help='Explicit action protocol; never automatically falls back')
    solve_parser.add_argument('--max-steps', type=int, default=40)
    solve_parser.add_argument('--closing-requests', type=int, choices=[0, 1], default=0,
                              help='Reserve the last request within max-steps for finish only; native mode')
    solve_parser.add_argument('--pause-after-tools', type=int,
                              help='Explicit pause after this many completed tools in this execution segment')
    solve_parser.add_argument('--command-timeout', type=int, default=60)
    solve_parser.add_argument('--script-timeout', type=int, default=120,
                              help='Shared device script operation deadline; bounded cleanup is separate')
    solve_parser.add_argument('--max-context-chars', type=int, help='Optional additional explicit character limit')
    solve_parser.add_argument('--context-window-tokens', type=int, default=1_000_000)
    solve_parser.add_argument('--context-headroom-ratio', type=float, default=0.10)
    solve_parser.add_argument('--tokenizer-dir', type=Path, help='Offline official tokenizer directory')
    solve_parser.add_argument('--max-output-tokens', type=int, default=4096)
    solve_parser.add_argument('--model-timeout', type=int, default=60,
                              help='HTTP blocking-wait timeout in seconds; not a session deadline')
    solve_parser.add_argument('--thinking', choices=['enabled', 'disabled'], default='enabled')
    solve_parser.add_argument('--reasoning-effort', choices=['low', 'high', 'max'], default='high',
                              help='Thinking effort; used only when thinking is enabled')
    product_resume = sub.add_parser('resume', help='Explicit full-history continuation at a completed boundary')
    product_resume.add_argument('--run', type=Path, required=True)
    product_resume.add_argument('--pause-after-tools', type=int)
    product_resume.add_argument('--user-action-done', action='store_true', help='Confirm the pending operator environment action was handled')
    delivery = sub.add_parser('report-agent', help='Combine saved solve and optional posthoc evidence; no execution')
    delivery.add_argument('--run', type=Path, required=True)
    delivery.add_argument('--evaluation', type=Path)
    delivery.add_argument('--out', type=Path, required=True, help='New report directory outside source records')
    inventory = sub.add_parser("inventory", help="Read data_B and write an inventory; never execute dataset scripts")
    inventory.add_argument("--dataset", type=Path, required=True)
    inventory.add_argument("--out", type=Path, default=Path("reports/inventory"))
    preflight = sub.add_parser("doctor", help="Read-only tool/package discovery")
    preflight.add_argument("--tool-root", type=Path, action="append", default=[])
    preflight.add_argument("--out", type=Path, default=Path("reports/environment"))
    review = sub.add_parser('review-context', help='Export a local source inventory and outbound preview; never call a model')
    review.add_argument('--case', type=Path, required=True)
    review.add_argument('--host', type=Path, help='Explicit local tool/data paths; never model context')
    review.add_argument('--out', type=Path, required=True)
    ledger = sub.add_parser('summarize-runs', help='Summarize local runs; keep fixtures and real cases separate')
    ledger.add_argument('--run-root', type=Path, default=Path('runs/workflow'))
    ledger.add_argument('--out', type=Path, required=True)
    report = sub.add_parser('report-run', help='Export a saved run as JSON and Markdown; no execution or model calls')
    report.add_argument('--run', type=Path, required=True)
    report.add_argument('--out', type=Path, required=True, help='New report directory outside the run')
    log_import = sub.add_parser('import-log', help='Import one selected ZIP jscrash locally, with local provenance')
    log_import.add_argument('--dataset', type=Path, required=True)
    log_import.add_argument('--archive', type=Path, required=True)
    log_import.add_argument('--member', required=True)
    log_import.add_argument('--out', type=Path, required=True)
    evaluation = sub.add_parser('evaluate-localization', help='Local development labels only; no model calls or aggregate score')
    evaluation.add_argument('--run', type=Path, required=True)
    evaluation.add_argument('--labels', type=Path, required=True)
    evaluation.add_argument('--attempt', type=int, default=1)
    evaluation.add_argument('--out', type=Path, required=True)
    for name in ('prepare', 'run', 'baseline'):
        command = sub.add_parser(name, help=('Build, deploy and reproduce original fault locally; no model calls'
                                 if name == 'baseline' else 'Create snapshot/evidence; run drives model/validation'))
        command.add_argument('--case', type=Path, required=True)
        command.add_argument('--host', type=Path, help='Explicit local tool/data paths')
        command.add_argument('--run-root', type=Path, default=Path('runs/workflow'))
        if name == 'run':
            backend = command.add_mutually_exclusive_group()
            backend.add_argument('--responses', type=Path, help='Offline fixture JSON; not live model output')
            backend.add_argument('--provider', choices=['deepseek'])
            command.add_argument('--model', help='Explicit model name for live provider')
            command.add_argument('--max-output-tokens', type=int, default=4096)
            command.add_argument('--execute', action='store_true', help='Execute the trusted case validation commands')
    resume = sub.add_parser('continue', help='Continue a clean model/validation waiting checkpoint')
    resume.add_argument('--run', type=Path, required=True)
    backend = resume.add_mutually_exclusive_group()
    backend.add_argument('--responses', type=Path)
    backend.add_argument('--provider', choices=['deepseek'])
    resume.add_argument('--model')
    resume.add_argument('--max-output-tokens', type=int, default=4096)
    resume.add_argument('--execute', action='store_true')
    recheck = sub.add_parser('revalidate', help='Rebuild and check an unchanged saved model candidate; no API calls')
    recheck.add_argument('--run', type=Path, required=True)
    recheck.add_argument('--attempt', type=int, required=True)
    recheck.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'solve':
            from .agent import solve
            from .workspace import read_json
            task = args.task if args.task is not None else args.task_file.read_text(encoding='utf-8-sig')
            result = solve(args.repo, task, args.out.resolve(),
                           responses=read_json(args.responses) if args.responses else None,
                           provider=DeepSeekModel(args.model, args.max_output_tokens, timeout=args.model_timeout,
                               thinking=args.thinking, reasoning_effort=args.reasoning_effort) if args.provider else None,
                           scope=args.scope, logs=args.logs, max_steps=args.max_steps, host=args.host,
                           closing_requests=args.closing_requests,
                           environment_preflight=args.environment_preflight,
                           allow_device=args.allow_device, allow_dependency_install=args.allow_dependency_install,
                           allow_system_navigation=args.allow_system_navigation,
                           allow_screen_unlock=args.allow_screen_unlock,
                           build_timeout=args.build_timeout, script_timeout=args.script_timeout,
                           model_protocol=args.model_protocol,
                           command_timeout=args.command_timeout, max_context_chars=args.max_context_chars,
                           context_window_tokens=args.context_window_tokens,
                           context_headroom_ratio=args.context_headroom_ratio, tokenizer_dir=args.tokenizer_dir,
                           pause_after_tools=args.pause_after_tools)
            print(json.dumps({'output': str(args.out.resolve()), **result}, ensure_ascii=True))
            return 0 if result['status'] == 'Submitted' else 3
        elif args.command == 'resume':
            from .agent import resume as resume_task
            result = resume_task(args.run, pause_after_tools=args.pause_after_tools,
                                 user_action_done=args.user_action_done)
            print(json.dumps({'output': str(args.run.resolve()), **result}, ensure_ascii=True))
            return 0 if result['status'] == 'Submitted' else 3
        elif args.command == 'report-agent':
            from .run_report import write_agent_delivery
            result = write_agent_delivery(args.run, args.out, evaluation=args.evaluation)
            print(json.dumps({'output': str(args.out.resolve()), 'agent_status': result['agent_status'],
                              'agent_submitted': result['agent_submitted'],
                              'independent_status': result['independent']['status']}, ensure_ascii=True))
        elif args.command == "inventory":
            # Validate output boundary before doing any writes.
            if args.out.resolve().is_relative_to(args.dataset.resolve()):
                raise ValueError("Report output must be outside the original dataset")
            report = cases.discover(args.dataset)
            out = output_directory(args.out, args.dataset)
            (out / "case_registry.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            (out / "summary.md").write_text(cases.summary_markdown(report), encoding="utf-8")
            with (out / "case_registry.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["case_id", "app_root", "asset_status", "mapping_status", "context_review", "issues"])
                for item in report["cases"]:
                    writer.writerow([item[k] for k in ("case_id", "app_root", "asset_status", "mapping_status", "context_review")] + [";".join(item["issues"])])
            print(json.dumps({"output": str(out), **report["summary"]}, ensure_ascii=True))
        elif args.command == 'evaluate-localization':
            from .localization_eval import evaluate
            from .workspace import save_json
            # Evaluation artifacts cannot be written into a run or overwrite labels.
            if (args.out.resolve().is_relative_to(args.run.resolve()) or
                    args.out.resolve() == args.labels.resolve() or args.out.exists()):
                raise ValueError('Evaluation output must be a new file outside the run and labels')
            report = evaluate(args.run, args.labels, args.attempt)
            save_json(args.out, report)
            print(json.dumps({'output': str(args.out.resolve()), 'locations': report['locations']}, ensure_ascii=True))
        elif args.command == 'import-log':
            from .log_import import import_log
            print(json.dumps(import_log(args.archive, args.member, args.out, args.dataset), ensure_ascii=True))
        elif args.command == 'report-run':
            from .run_report import export_run
            print(json.dumps(export_run(args.run, args.out), ensure_ascii=True))
        elif args.command == 'summarize-runs':
            from .run_report import summarize_runs
            from .workspace import save_json
            report = summarize_runs(args.run_root)
            save_json(args.out, report)
            print(json.dumps({'output': str(args.out.resolve()), 'runs': len(report['runs']),
                              'issues': len(report['issues'])}, ensure_ascii=True))
        elif args.command == 'review-context':
            from .context_review import export_review
            print(json.dumps(export_review(args.case, args.out, args.host), ensure_ascii=True))
        elif args.command == 'doctor':
            report = doctor.inspect_environment(args.tool_root)
            out = output_directory(args.out)
            (out / "environment.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            (out / "summary.md").write_text(doctor.summary_markdown(report), encoding="utf-8")
            print(json.dumps({"output": str(out), "runtime_readiness": report["runtime_readiness"],
                              "missing_in_search_scope": report["missing_in_search_scope"]}, ensure_ascii=True))
        else:
            run = args.run if args.command in {'continue', 'revalidate'} else runner.prepare(args.case, args.run_root, args.host)
            if args.command == 'revalidate':
                state = runner.revalidate(run, args.attempt, args.reason)
            elif args.command == 'baseline':
                state = runner.run_baseline(run)
            elif args.command == 'prepare':
                state = json.loads((run / 'state.json').read_text(encoding='utf-8'))
            else:
                model = (DeepSeekModel(args.model, args.max_output_tokens) if args.provider else
                         TranscriptModel(args.responses) if args.responses else None)
                state = runner.drive(run, model, args.execute)
            print(json.dumps({'run': str(run.resolve()), 'status': state['status'],
                              'model_kind': state['model_kind'], 'baseline_status': state['baseline_status']}, ensure_ascii=True))
            return 0 if state['status'] in {'PREPARED', 'VALIDATED', 'SYNTHETIC_VALIDATED', 'FIXTURE_PASSED'} else 3
    except (OSError, ValueError) as error:
        parser.exit(2, f"Error: {error}\n")
    return 0
