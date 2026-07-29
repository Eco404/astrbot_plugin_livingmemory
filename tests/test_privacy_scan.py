import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "privacy_scan.py"
SPEC = importlib.util.spec_from_file_location("privacy_scan", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
privacy_scan = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = privacy_scan
SPEC.loader.exec_module(privacy_scan)


def test_scan_text_detects_constructed_provider_token():
    token = "sk-" + ("a" * 24)
    findings = privacy_scan.scan_text(f'value = "{token}"', "fixture.py")

    assert [item.rule for item in findings] == ["provider token"]


def test_scan_text_detects_local_literal_without_storing_it_in_rules():
    private_value = "private" + "-account-123"
    findings = privacy_scan.scan_text(
        f"account = {private_value}",
        "fixture.py",
        [private_value],
    )

    assert [item.rule for item in findings] == ["local denylist"]


def test_sensitive_database_suffix_is_rejected():
    assert privacy_scan._is_sensitive_path(Path("fixtures/example.db")) is True
    assert privacy_scan._is_sensitive_path(Path("fixtures/example.json")) is False
