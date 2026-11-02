"""Read-only, bounded discovery of the two supplied DFX dataset layouts.

No application modules or test scripts are imported/executed. Paths are relative
to data_B. Mapping by identifier is only an inventory hypothesis, never an oracle.
"""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

TC = re.compile(r"TC_[A-Za-z]+_\d{3}(?!\d)")
DECLARATION = re.compile(r"^\|\s*(TC_[A-Za-z]+_\d{3})\s*\|\s*([^|]+?)\s*\|", re.M)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace") if path.is_file() else ""


def discover(dataset: Path) -> dict:
    root = dataset.resolve()
    if not root.is_dir() or not any((root / p).is_dir() for p in ("data_B1", "data_B2", "data_B3")):
        raise ValueError("--dataset must point to data_B containing data_B1/data_B2/data_B3")
    records: dict[str, dict] = {}
    apps: list[dict] = []
    issues: list[dict] = []
    excluded: list[dict] = []

    def relative(path: Path) -> str:
        return path.relative_to(root).as_posix()

    def issue(code: str, path: Path, detail: str) -> None:
        issues.append({"code": code, "path": relative(path), "detail": detail})

    def record(split: str, app: Path, tc: str) -> dict:
        key = f"{split}/{app.name}/{tc}"
        if key not in records:
            records[key] = {
                "case_id": key, "split": split, "app_name": app.name,
                "fault_id": tc, "app_root": relative(app), "test_scripts": [],
                "test_configs": [], "test_archives": [], "log_files": [], "log_archives": [],
                "issues": [], "mapping_status": "REVIEW_REQUIRED",
                "context_review": "PENDING", "agent_visible_files": [],
                "test_role": "REVIEW_REQUIRED", "source_snapshot": None,
                "validation": {k: "NOT_RUN" for k in ("original_replay", "build", "fault_replay", "functional", "regression")},
            }
        return records[key]

    def register_app(split: str, app: Path) -> None:
        # Extract only allowlisted metadata: never serialize signing material.
        profile = read_text(app / "build-profile.json5")
        app_config = read_text(app / "AppScope" / "app.json5")
        bundle = re.search(r'["\']bundleName["\']\s*:\s*["\']([^"\']+)', app_config)
        sdk = re.findall(r'["\'](compatibleSdkVersion|targetSdkVersion|compileSdkVersion)["\']\s*:\s*(["\'][^"\']+["\']|\d+)', profile)
        apps.append({
            "app_id": f"{split}/{app.name}", "app_root": relative(app),
            "bundle_name": bundle.group(1) if bundle else None,
            "sdk_declarations": [{"field": k, "value": v.strip('\"\'')} for k, v in sdk],
            "source_presence": "BUILD_PROFILE_FOUND", "source_snapshot": None,
            "runtime_readiness": "NOT_CHECKED",
            "environment_notes": [line.strip() for line in read_text(app / "README.md").splitlines()
                                  if any(key in line for key in ("DevEco Studio版本", "HarmonyOS SDK版本", "模拟器调试", "真机进行调试"))],
        })

    def archive_info(path: Path, *, expected_log: bool = True) -> dict:
        info = {"path": relative(path), "bytes": path.stat().st_size, "log_members": [], "issues": [], "member_count": 0, "member_preview": []}
        try:
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    info["member_count"] += 1
                    if len(info["member_preview"]) < 10:
                        info["member_preview"].append(member.filename)
                    normalized = member.filename.replace("\\", "/")
                    posix = PurePosixPath(normalized)
                    if posix.is_absolute() or ".." in posix.parts or re.match(r"^[A-Za-z]:", normalized):
                        info["issues"].append("UNSAFE_ARCHIVE_MEMBER")
                        continue
                    if not member.is_dir() and posix.name.startswith("jscrash") and posix.suffix in (".log", ".txt"):
                        info["log_members"].append(normalized)
                if expected_log and len(info["log_members"]) != 1:
                    info["issues"].append("ARCHIVE_LOG_COUNT_NOT_ONE")
        except (OSError, zipfile.BadZipFile):
            info["issues"].append("UNREADABLE_ARCHIVE")
        info["issues"] = sorted(set(info["issues"]))
        # Member content is not read here; CRC/content validity remains unchecked.
        info["content_validation"] = "NOT_CHECKED"
        return info

    def add_test(item: dict, path: Path) -> None:
        text = read_text(path)
        source_markers = ("故障点", "故障位置", "崩溃根因", "fault_file", "fault_line", "root_cause")
        item["test_scripts"].append({
            "path": relative(path), "bytes": path.stat().st_size,
            "runner_hint": "xdevice_hypium" if "TestCase" in text or "RunResult" in text else "python_script",
            "answer_marker_detected": any(marker in text for marker in source_markers),
            "content_review": "REQUIRED_EVEN_IF_NO_MARKER", "role": "REVIEW_REQUIRED",
        })

    for split in ("data_B1", "data_B2"):
        split_root = root / split
        if not split_root.is_dir():
            issue("MISSING_SPLIT", split_root, "Partition not present in this snapshot")
            continue
        for app in sorted(split_root.iterdir()):
            if not app.is_dir():
                continue
            if not (app / "build-profile.json5").is_file():
                excluded.append({"path": relative(app), "reason": "NOT_APP_ROOT; retained as supplemental data, not counted"})
                continue
            register_app(split, app)
            test_dir = app / "testcases"
            if test_dir.is_dir():
                for path in sorted(test_dir.iterdir()):
                    match = TC.match(path.stem)
                    if not path.is_file() or not match or path.suffix not in (".py", ".json", ".zip"):
                        continue
                    item = record(split, app, match.group())
                    if path.suffix == ".py":
                        add_test(item, path)
                    elif path.suffix == ".json":
                        item["test_configs"].append(relative(path))
                    else:
                        item["test_archives"].append(archive_info(path, expected_log=False))
            for scene in sorted(app.iterdir()):
                match = TC.match(scene.name)
                if not scene.is_dir() or not match:
                    continue
                item = record(split, app, match.group())
                claimed_app = scene.name[len(match.group()):].lstrip("-")
                if claimed_app and claimed_app != app.name:
                    item["issues"].append("SCENE_APP_NAME_MISMATCH")
                    issue("SCENE_APP_NAME_MISMATCH", scene, f"Parent app={app.name}; scene suffix={claimed_app}; alias not assumed")
                logger = scene / "faultlogger"
                if logger.is_dir():
                    for log in sorted(logger.glob("jscrash*")):
                        if log.is_file() and log.suffix in (".log", ".txt"):
                            item["log_files"].append({"path": relative(log), "bytes": log.stat().st_size})
            matched_ids = [k for k in records if k.startswith(f"{split}/{app.name}/")]
            if not matched_ids:
                issue("APP_WITHOUT_CASE_ARTIFACTS", app, "Source retained; no TC scripts or scene directories discovered")
            if any(not records[k]["test_scripts"] for k in matched_ids) and any(
                not records[k]["log_files"] and not records[k]["log_archives"] for k in matched_ids
            ):
                issue("POSSIBLE_ID_ALIAS", app, "Unpaired identifiers coexist; do not merge without evidence review")

    b3 = root / "data_B3" / "benchmarkfordfx-better"
    app_dir = b3 / "App"
    b3_apps = {p.name: p for p in sorted(app_dir.iterdir()) if p.is_dir() and (p / "build-profile.json5").is_file()} if app_dir.is_dir() else {}
    for app in b3_apps.values():
        register_app("data_B3", app)
    logs_dir = b3 / "Logs" / "故障现场"
    if logs_dir.is_dir():
        for path in sorted(logs_dir.glob("TC_*.zip")):
            match = TC.match(path.stem)
            if not match:
                continue
            tc = match.group()
            name = path.stem[len(tc):].lstrip("-")
            if name not in b3_apps:
                issue("ARCHIVE_APP_NOT_FOUND", path, "No exact matching source app; manual review required")
                continue
            record("data_B3", b3_apps[name], tc)["log_archives"].append(archive_info(path))
    test_root = b3 / "HypiumProject" / "testcases"
    if test_root.is_dir():
        for path in sorted(test_root.glob("TC_*.py")):
            candidates = [item for item in records.values() if item["split"] == "data_B3" and item["fault_id"] == path.stem]
            if len(candidates) != 1:
                issue("UNMAPPED_TEST", path, "No unique archive-to-app mapping; script not assigned")
                continue
            item = candidates[0]
            add_test(item, path)
            if path.with_suffix(".json").is_file():
                item["test_configs"].append(relative(path.with_suffix(".json")))
    readme = b3 / "README.md"
    for tc, name in DECLARATION.findall(read_text(readme)):
        if f"data_B3/{name.strip()}/{tc}" not in records:
            issue("DECLARED_CASE_WITHOUT_ARTIFACT_MAPPING", readme, f"{tc} / {name.strip()} declared only; not an executable case")
    main_path = b3 / "HypiumProject" / "main.py"
    config_path = b3 / "HypiumProject" / "config" / "user_config.xml"
    # Compare declarations only; do not infer xdevice config precedence.
    main_ids = sorted(set(TC.findall(read_text(main_path))))
    config_ids = sorted(set(TC.findall(read_text(config_path))))
    if main_ids and config_ids and main_ids != config_ids:
        issue("TEST_ENTRY_CONFIG_DIFFER", main_path, f"main={main_ids}; config={config_ids}; execution precedence not checked")

    items = sorted(records.values(), key=lambda item: item["case_id"])
    for item in items:
        has_logs = bool(item["log_files"] or any(a["log_members"] and not a["issues"] for a in item["log_archives"]))
        has_tests = bool(item["test_scripts"])
        item["asset_status"] = "PAIRED_ASSETS" if has_logs and has_tests else "LOG_ONLY" if has_logs else "TEST_ONLY" if has_tests else "INCOMPLETE"
        for archive in item["log_archives"] + item["test_archives"]:
            item["issues"].extend(archive["issues"])
            for code in archive["issues"]:
                issue(code, root / archive["path"], f"log_members={len(archive['log_members'])}; member_preview={archive['member_preview']}")
        item["issues"] = sorted(set(item["issues"]))
    counts = Counter(item["asset_status"] for item in items)
    return {
        "schema_version": "0.1", "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_root": str(root), "purpose": "LOCAL_INVENTORY_ONLY_NOT_AGENT_CONTEXT",
        "summary": {"app_count": len(apps), "case_candidate_count": len(items), "asset_status_counts": dict(counts),
                    "issue_count": len(issues), "validated_case_count": 0},
        "apps": apps, "cases": items, "issues": issues, "supplemental_directories": excluded,
        "limitations": [
            "PAIRED_ASSETS means filenames can be associated, not correct mapping or executability.",
            "Only supplied layout locations are scanned; historical reports and arbitrary nested zips are not enumerated.",
            "Inventory records file locations and sizes, not semantic validation; ZIP member content is not checked here.",
            "No source snapshot, root-cause truth, build, runtime verification, or model input approval is produced.",
            "Static marker scanning cannot prove a script free of answer leakage.",
        ],
    }


def summary_markdown(report: dict) -> str:
    s = report["summary"]
    lines = ["# DFX 案例盘点", "", f"应用目录：{s['app_count']}；候选标识：{s['case_candidate_count']}；已运行验证：0。",
             "", "候选标识不是已确认故障数量；PAIRED_ASSETS 仅表示脚本与日志资产可按命名关联。",
             "", "| 案例 | 资产状态 | 脚本数 | 日志文件/包数 | 待核对 |", "|---|---|---:|---:|---|"]
    for item in report["cases"]:
        lines.append(f"| {item['case_id']} | {item['asset_status']} | {len(item['test_scripts'])} | {len(item['log_files']) + len(item['log_archives'])} | {', '.join(item['issues']) or '映射与测试用途尚待审核'} |")
    lines += ["", "## 数据问题", ""]
    lines += [f"- `{i['code']}` — `{i['path']}`：{i['detail']}" for i in report["issues"]]
    lines += ["", "## 边界", "", *[f"- {v}" for v in report["limitations"]], ""]
    return "\n".join(lines)
