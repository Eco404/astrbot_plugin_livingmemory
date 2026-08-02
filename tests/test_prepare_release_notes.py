import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "prepare_release_notes.py"
WORKFLOW_PATH = (
    Path(__file__).parent.parent / ".github" / "workflows" / "auto-release.yml"
)
SPEC = importlib.util.spec_from_file_location("prepare_release_notes", SCRIPT_PATH)
assert SPEC and SPEC.loader
release_notes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_notes)


def _write_version_files(root: Path, versions: dict[str, str]) -> None:
    (root / "core" / "managers").mkdir(parents=True)
    (root / "metadata.yaml").write_text(
        f"name: plugin\nversion: {versions['metadata']}\n", encoding="utf-8"
    )
    (root / "package.json").write_text(
        json.dumps({"version": versions["package"]}), encoding="utf-8"
    )
    (root / "main.py").write_text(
        '@register(\n    "LivingMemory",\n    "author",\n    "description",\n'
        f'    "{versions["main"]}",\n    "repo",\n)\n',
        encoding="utf-8",
    )
    (root / "core" / "managers" / "backup_manager.py").write_text(
        f'PLUGIN_VERSION = "{versions["backup"]}"\n', encoding="utf-8"
    )


def test_validate_versions_accepts_matching_release(tmp_path: Path) -> None:
    _write_version_files(
        tmp_path,
        {"metadata": "3.5.0", "package": "3.5.0", "main": "3.5.0", "backup": "3.5.0"},
    )

    assert release_notes.validate_versions(tmp_path) == "3.5.0"


def test_validate_versions_rejects_mismatch(tmp_path: Path) -> None:
    _write_version_files(
        tmp_path,
        {"metadata": "3.5.0", "package": "3.4.1", "main": "3.5.0", "backup": "3.5.0"},
    )

    with pytest.raises(ValueError, match="版本号不一致"):
        release_notes.validate_versions(tmp_path)


def test_extract_changelog_section_is_exact() -> None:
    changelog = """# Changelog

## [3.5.0] - 2026-08-02

- 新增索引恢复。
- 修复仪表盘刷新。

## [3.4.1] - 2026-08-01

- 旧版本内容。
"""

    assert release_notes.extract_changelog_section(changelog, "3.5.0") == (
        "- 新增索引恢复。\n- 修复仪表盘刷新。"
    )
    with pytest.raises(ValueError, match="缺少版本"):
        release_notes.extract_changelog_section(changelog, "9.9.9")


def test_render_release_notes_uses_v_prefixed_compare_tags() -> None:
    notes = release_notes.render_release_notes(
        version="3.5.0",
        body="- 修复索引。",
        repository="example/livingmemory",
        previous_version="v3.4.1",
    )

    assert notes.startswith("## LivingMemory 3.5.0")
    assert "- 修复索引。" in notes
    assert "/compare/v3.4.1...v3.5.0" in notes


def test_release_workflow_uses_validated_changelog_notes() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "scripts/prepare_release_notes.py" in workflow
    assert "body_path: release-notes.md" in workflow
    assert "generate_release_notes: false" in workflow
    assert "fetch-depth: 0" in workflow
