#!/usr/bin/env python3
"""Validate release versions and render release notes from CHANGELOG.md."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
CHANGELOG_HEADING_RE = re.compile(
    r"^## \[(?P<version>[^]]+)](?:\s+-\s+[^\n]+)?\s*$", re.MULTILINE
)


def _metadata_version(path: Path) -> str:
    match = re.search(
        r"^version:\s*['\"]?(?P<version>[^'\"\s#]+)",
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        raise ValueError(f"无法从 {path} 读取 version")
    return match.group("version")


def _python_constant_version(path: Path, pattern: str, label: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise ValueError(f"无法从 {path} 读取 {label}")
    return match.group("version")


def collect_versions(root: Path) -> dict[str, str]:
    """Read every independently maintained plugin version."""
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    return {
        "metadata.yaml": _metadata_version(root / "metadata.yaml"),
        "package.json": str(package.get("version") or ""),
        "main.py": _python_constant_version(
            root / "main.py",
            r"@register\(\s*\n\s*['\"]LivingMemory['\"],[\s\S]*?\n\s*['\"](?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)['\"],",
            "@register 版本",
        ),
        "backup_manager.py": _python_constant_version(
            root / "core" / "managers" / "backup_manager.py",
            r"^PLUGIN_VERSION\s*=\s*['\"](?P<version>[^'\"]+)['\"]",
            "PLUGIN_VERSION",
        ),
    }


def validate_versions(root: Path) -> str:
    versions = collect_versions(root)
    distinct = set(versions.values())
    if len(distinct) != 1:
        details = ", ".join(f"{name}={version}" for name, version in versions.items())
        raise ValueError(f"版本号不一致: {details}")
    version = next(iter(distinct))
    if not SEMVER_RE.fullmatch(version):
        raise ValueError(f"版本号不符合语义化版本格式: {version}")
    return version


def extract_changelog_section(changelog: str, version: str) -> str:
    """Return the body of the exact version heading, excluding the heading."""
    matches = list(CHANGELOG_HEADING_RE.finditer(changelog))
    for index, match in enumerate(matches):
        if match.group("version") != version:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(changelog)
        body = changelog[match.end() : end].strip()
        if not body:
            raise ValueError(f"CHANGELOG.md 中 {version} 的发布说明为空")
        return body
    raise ValueError(f"CHANGELOG.md 缺少版本 {version} 的发布说明")


def render_release_notes(
    *, version: str, body: str, repository: str, previous_version: str
) -> str:
    previous = previous_version.strip().removeprefix("v")
    current_tag = f"v{version}"
    lines = [f"## LivingMemory {version}", "", body.strip(), ""]
    if previous and previous.lower() != "none" and previous != version:
        lines.extend(
            [
                "---",
                "",
                f"**完整变更对比**：https://github.com/{repository}/compare/v{previous}...{current_tag}",
            ]
        )
    else:
        lines.extend(
            [
                "---",
                "",
                f"**版本标签**：https://github.com/{repository}/releases/tag/{current_tag}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--previous-version", default="none")
    args = parser.parse_args()

    root = args.root.resolve()
    version = validate_versions(root)
    body = extract_changelog_section(
        (root / "CHANGELOG.md").read_text(encoding="utf-8"), version
    )
    notes = render_release_notes(
        version=version,
        body=body,
        repository=args.repository,
        previous_version=args.previous_version,
    )
    args.output.write_text(notes, encoding="utf-8")
    print(f"已生成 v{version} 发布说明: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
