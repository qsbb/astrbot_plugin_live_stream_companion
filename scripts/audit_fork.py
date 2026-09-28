#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fork 发布前自检：私货关键词 / 环境泄漏 / 溯源与版本一致性。

用法::

    python scripts/audit_fork.py [仓库根目录]

退出码 0 = 干净；1 = 有 findings（发布前必须处理）。

判定口径（与 docs/FORK.md 一致）：

* **私货**：代码里出现本 fork 维护者自有系列/插件的标识符，或硬依赖某个具体插件；
* **环境泄漏**：维护者个人路径、内网主机、口令等；
* **溯源**：上游版权与作者信息必须保留，README 必须声明这是 fork；
* **版本一致**：metadata / README / CHANGELOG 三处版本号一致。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

#: 代码/文档里不允许出现的「维护者私有」标识（通用化改造后应为 0）
PRIVATE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("系列名", r"凝心溯溪"),
    ("私有插件 id", r"astrbot_plugin_(voice_hub|update_manager|orchestration_hub|active_learner|conversation_flow|identity_guardian|environment_awareness|embodiment_bridge|companion_phone|relationship)"),
    ("私有契约名", r"series\.(control|model_router|diagnostics|module|webui)@"),
)

#: 维护者环境痕迹（个人路径、内网主机、口令）
ENV_PATTERNS: tuple[tuple[str, str], ...] = (
    ("个人用户名", r"lingxi"),
    ("内网主机", r"192\.168\.5\."),
    ("口令痕迹", r"Zyb@"),
)

TEXT_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".md", ".html", ".js", ".mjs", ".css", ".txt"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".pytest_cache"}

#: 许可证里必须保留的上游版权署名
LICENSE_CREDITS = ("xfgryujk", "Raven95676")


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        yield path


#: 自检脚本自身与它的测试天然包含这些关键词，扫描时跳过
SELF_EXEMPT = {"scripts/audit_fork.py", "tests/test_fork_audit.py"}


def _scan_patterns(root: Path, patterns) -> list[str]:
    findings: list[str] = []
    for path in iter_files(root):
        if str(path.relative_to(root)) in SELF_EXEMPT:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(root)
        for line_no, line in enumerate(text.splitlines(), start=1):
            for label, pattern in patterns:
                if re.search(pattern, line):
                    findings.append(f"{rel}:{line_no}: [{label}] {line.strip()[:120]}")
    return findings


def check_provenance(root: Path) -> list[str]:
    findings: list[str] = []

    license_file = root / "LICENSE"
    license_text = license_file.read_text(encoding="utf-8", errors="ignore") if license_file.exists() else ""
    missing = [name for name in LICENSE_CREDITS if name not in license_text]
    if missing:
        findings.append(f"LICENSE: 缺少上游版权署名 {', '.join(missing)}")

    metadata_file = root / "metadata.yaml"
    if not metadata_file.exists():
        findings.append("metadata.yaml: 缺失")
    else:
        metadata = metadata_file.read_text(encoding="utf-8", errors="ignore")
        if not re.search(r"^author:\s*menglimi\s*$", metadata, re.M):
            findings.append("metadata.yaml: author 字段不再是上游作者（fork 不得冒充原创）")

    readme = root / "README.md"
    readme_text = readme.read_text(encoding="utf-8", errors="ignore") if readme.exists() else ""
    if "FORK.md" not in readme_text and "fork" not in readme_text.lower():
        findings.append("README.md: 未声明这是 fork（需链接 docs/FORK.md）")
    return findings


def _first_heading_version(text: str) -> str:
    match = re.search(r"^##\s+v?(\d+\.\d+\.\d+)\s*$", text, re.M)
    return match.group(1) if match else ""


def check_versions(root: Path) -> list[str]:
    findings: list[str] = []

    metadata_file = root / "metadata.yaml"
    metadata_version = ""
    if metadata_file.exists():
        match = re.search(
            r"^version:\s*(\S+)\s*$",
            metadata_file.read_text(encoding="utf-8", errors="ignore"),
            re.M,
        )
        metadata_version = match.group(1) if match else ""
    if not metadata_version:
        findings.append("metadata.yaml: 缺少 version")

    readme_file = root / "README.md"
    readme_version = ""
    if readme_file.exists():
        match = re.search(
            r"当前版本[：:]\s*`?v?(\d+\.\d+\.\d+)`?",
            readme_file.read_text(encoding="utf-8", errors="ignore"),
        )
        readme_version = match.group(1) if match else ""

    changelog_file = root / "CHANGELOG.md"
    changelog_version = ""
    if changelog_file.exists():
        changelog_version = _first_heading_version(
            changelog_file.read_text(encoding="utf-8", errors="ignore")
        )

    for label, value in (
        ("README", readme_version),
        ("CHANGELOG", changelog_version),
    ):
        if metadata_version and value and value != metadata_version:
            findings.append(
                f"版本不一致：metadata={metadata_version} vs {label}={value}"
            )
    return findings


def check_config_keys(root: Path) -> list[str]:
    """新增的传输开关必须同时出现在 schema 与页面白名单里（避免页面看不到）。"""
    findings: list[str] = []
    schema_file = root / "_conf_schema.json"
    page_file = root / "page_config.py"
    for key in ("live_tts_emotion_passthrough_enabled", "live_tts_style_hint_enabled"):
        if schema_file.exists():
            try:
                schema = json.loads(schema_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:  # pragma: no cover - 明确报错
                findings.append(f"_conf_schema.json: JSON 解析失败 {exc}")
                schema = {}
            if key not in schema:
                findings.append(f"_conf_schema.json: 缺少 {key}")
        if page_file.exists():
            text = page_file.read_text(encoding="utf-8", errors="ignore")
            if text.count(key) < 2:
                findings.append(f"page_config.py: {key} 未同时进入白名单与分组（应出现 2 次）")
    return findings


def run_audit(root: Path | str) -> list[str]:
    root_path = Path(root).resolve()
    findings: list[str] = []
    findings += _scan_patterns(root_path, PRIVATE_PATTERNS)
    findings += _scan_patterns(root_path, ENV_PATTERNS)
    findings += check_provenance(root_path)
    findings += check_versions(root_path)
    findings += check_config_keys(root_path)
    return findings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path(__file__).resolve().parents[1]
    findings = run_audit(root)
    if not findings:
        print(f"fork 自检通过：{root}")
        return 0
    print(f"fork 自检发现 {len(findings)} 个问题：")
    for item in findings:
        print(f"  - {item}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
