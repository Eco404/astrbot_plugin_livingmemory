#!/usr/bin/env python3
"""Scan tracked content and optional Git history for privacy leaks."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 5 * 1024 * 1024
SENSITIVE_BASENAMES = {
    ".env",
    "api_token.md",
    "authoritative_identities.json",
}
SENSITIVE_SUFFIXES = {
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".index",
}
BUILTIN_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "provider token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "bearer credential": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{20,}"),
    "assigned credential": re.compile(
        r"(?i)\b(?:api[_-]?(?:key|token)|auth[_-]?token|password)\b"
        r"\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']"
    ),
    "credential in URL": re.compile(r"https?://[^\s/:@]+:[^\s/@]+@[^\s/]+"),
    "Windows user path": re.compile(r"[A-Za-z]:\\Users\\(?![<{$%])[^\\\s]+"),
    "home directory path": re.compile(r"/" r"home/(?![<{$%])[^/\s]+"),
    "mounted user path": re.compile(r"/" r"mnt/[a-z]/(?![<{$%])[^/\s]+"),
}


@dataclass(frozen=True)
class Finding:
    source: str
    line: int
    rule: str


def _run_git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=root)


def repository_root(start: Path) -> Path:
    output = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], cwd=start, text=True
    )
    return Path(output.strip()).resolve()


def load_local_denylist(root: Path) -> tuple[str, ...]:
    path = root / ".privacy-denylist.local"
    if not path.exists():
        return ()
    values = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        value = raw_line.strip()
        if value and not value.startswith("#"):
            values.append(value)
    return tuple(dict.fromkeys(values))


def tracked_paths(root: Path) -> list[Path]:
    raw = _run_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return [root / item.decode("utf-8") for item in raw.split(b"\0") if item]


def _is_sensitive_path(path: Path) -> bool:
    name = path.name.lower()
    return name in SENSITIVE_BASENAMES or any(
        name.endswith(suffix) for suffix in SENSITIVE_SUFFIXES
    )


def scan_text(
    text: str,
    source: str,
    denylist: Iterable[str] = (),
) -> list[Finding]:
    findings: list[Finding] = []
    literals = tuple(denylist)
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in BUILTIN_PATTERNS.items():
            if pattern.search(line):
                findings.append(Finding(source, line_number, rule))
        for literal in literals:
            if literal in line:
                findings.append(Finding(source, line_number, "local denylist"))
                break
    return findings


def scan_worktree(root: Path, denylist: Iterable[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in tracked_paths(root):
        relative = path.relative_to(root)
        if _is_sensitive_path(relative):
            findings.append(Finding(str(relative), 0, "sensitive file is tracked"))
            continue
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            continue
        if len(payload) > MAX_FILE_BYTES or b"\0" in payload:
            continue
        text = payload.decode("utf-8", errors="replace")
        findings.extend(scan_text(text, str(relative), denylist))
    return findings


def scan_history(root: Path, denylist: Iterable[str]) -> list[Finding]:
    try:
        payload = _run_git(
            root,
            "log",
            "--all",
            "--format=commit:%H",
            "--root",
            "-p",
            "--no-ext-diff",
            "--no-textconv",
        )
    except subprocess.CalledProcessError:
        return [Finding("Git history", 0, "history scan failed")]
    text = payload.decode("utf-8", errors="replace")
    return scan_text(text, "Git history", denylist)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        action="store_true",
        help="also scan patches reachable from all local refs",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repository_root(Path.cwd())
    denylist = load_local_denylist(root)
    findings = scan_worktree(root, denylist)
    if args.history:
        findings.extend(scan_history(root, denylist))
    unique = sorted(set(findings), key=lambda item: (item.source, item.line, item.rule))
    if unique:
        print("Privacy scan failed:", file=sys.stderr)
        for item in unique:
            location = f"{item.source}:{item.line}" if item.line else item.source
            print(f"- {location}: {item.rule}", file=sys.stderr)
        return 1
    scope = "tracked files and Git history" if args.history else "tracked files"
    print(f"Privacy scan passed for {scope}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
