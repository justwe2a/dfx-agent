"""Local, read-only environment discovery. Never installs or runs tools."""

from __future__ import annotations

import importlib.metadata
import os
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def registered_deveco(issues: list) -> list[dict]:
    if os.name != "nt":
        return []
    import winreg

    found = []
    keys = (
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, key_path in keys:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, index)) as child:
                            name = winreg.QueryValueEx(child, "DisplayName")[0]
                            if "deveco" not in name.lower():
                                continue
                            try:
                                location = winreg.QueryValueEx(child, "InstallLocation")[0]
                            except OSError as exc:
                                issues.append({"registry": key_path, "entry": name, "field": "InstallLocation", "error": str(exc)})
                                location = ""
                            found.append({"name": name, "location": location})
                    except OSError as exc:
                        issues.append({"registry": key_path, "entry_index": index, "error": str(exc)})
        except OSError as exc:
            issues.append({"registry": key_path, "error": str(exc)})
    return found


def inspect_environment(extra_roots: list[Path] | None = None) -> dict:
    issues = []
    installed = registered_deveco(issues)
    roots = [Path(p["location"]) for p in installed if p["location"]]
    for name in ("DEVECO_HOME", "HARMONY_HOME", "HARMONYOS_SDK_HOME", "OHOS_SDK_HOME"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            roots.extend(Path(base) / p for p in ("Huawei/DevEco Studio", "Huawei/Sdk", "OpenHarmony/Sdk"))
    roots.extend(extra_roots or [])
    roots = sorted(set(p.expanduser().resolve() for p in roots), key=str)
    # Bounded discovery: explicit roots only, no full-drive crawl. Do not follow
    # links/junctions or walk projects/dependencies while looking for tool files.
    executable_names = {"hdc": {"hdc.exe", "hdc"}, "hvigor": {"hvigorw.bat", "hvigor.bat", "hvigor.js"},
                        "ohpm": {"ohpm.bat", "ohpm.exe", "ohpm"}, "deveco": {"devecostudio64.exe", "devecostudio.exe"}}
    tools = {name: {"path": shutil.which(name), "evidence": "PATH" if shutil.which(name) else "NOT_FOUND_IN_SEARCH_SCOPE"}
             for name in ("python", "git", "node", "java", "hdc", "hvigor", "ohpm")}
    tools["deveco"] = {"path": None, "evidence": "NOT_FOUND_IN_SEARCH_SCOPE"}
    visited = 0
    for root in roots:
        if not root.is_dir():
            continue
        for directory, dirs, filenames in os.walk(root, followlinks=False, onerror=lambda exc: issues.append({"path": exc.filename, "error": str(exc)})):
            current = Path(directory)
            depth = len(current.relative_to(root).parts)
            visited += 1
            dirs[:] = sorted(d for d in dirs if d not in {"node_modules", "oh_modules", ".git", "cache", "caches"}
                             and not (current / d).is_symlink()
                             and not getattr(current / d, "is_junction", lambda: False)()) if depth < 6 and visited < 10000 else []
            for name, candidates in executable_names.items():
                for filename in sorted(filenames):
                    if filename.lower() in candidates and tools[name]["path"] is None:
                        tools[name] = {"path": str(current / filename), "evidence": "DISCOVERED_FILE_NOT_EXECUTED"}
            if visited >= 10000:
                break
        if visited >= 10000:
            break
    packages = {}
    for name in ("hypium", "xdevice"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    missing = [name for name in ("deveco", "hdc", "hvigor", "ohpm") if not tools[name]["path"]]
    return {
        "schema_version": "0.1", "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {"platform": platform.system(), "python_version": platform.python_version(), "python_executable": sys.executable},
        "discovery_issues": issues, "tools": tools, "registered_deveco": installed,
        "searched_roots": [{"path": str(p), "exists": p.is_dir()} for p in roots],
        "search_truncated": visited >= 10000, "current_python_packages": packages,
        "missing_in_search_scope": missing,
        "runtime_readiness": "NOT_VERIFIED", "device_status": "NOT_PROBED", "signing_status": "NOT_CHECKED",
        "notes": [
            "No executable, test, build, installer, package import, or paid API was invoked.",
            "Not found does not prove not installed; pass --tool-root for custom installations.",
            "Package results apply only to the current Python environment; compatibility is not verified.",
            "Tool presence does not prove SDK completeness, device availability, signing, or build success.",
        ],
    }


def summary_markdown(report: dict) -> str:
    lines = ["# 本机环境核验", "", f"状态：{report['runtime_readiness']}。本报告只做发现，不证明工程可构建或可运行。",
             "", f"Python：{report['host']['python_version']}", "", "| 工具 | 发现路径 | 证据 |", "|---|---|---|"]
    for name, tool in report["tools"].items():
        lines.append(f"| {name} | {tool['path'] or '搜索范围内未发现'} | {tool['evidence']} |")
    lines += ["", "当前解释器的测试依赖：", ""]
    lines += [f"- {name}: {version or '未发现发行包元数据'}" for name, version in report["current_python_packages"].items()]
    lines += ["", "设备：未探测。签名：未检查。", "", "## 搜索范围", ""]
    lines += [f"- {item['path']}（存在：{item['exists']}）" for item in report["searched_roots"]]
    lines += ["", "## 限制", "", *[f"- {note}" for note in report["notes"]], ""]
    return "\n".join(lines)

