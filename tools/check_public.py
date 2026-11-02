"""Check tracked files for key-like strings and unapproved name forms.

Usage: python tools/check_public.py
Exit code 0 prints "ok"; exit code 1 prints "file:line reason" for each finding.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

KEY_PATTERNS = [
    ("sk-ant-", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("sk-", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("github token", re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}")),
    ("github pat", re.compile(r"github_pat_[A-Za-z0-9_]{16,}")),
    ("aws access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google api key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("slack token", re.compile(r"xox[bp]-[A-Za-z0-9\-]{10,}")),
]

NAME_PATTERN = re.compile(r"([一-鿿]{1,3})さん")

SKIP_NAMES = {"allowed_names.json", "requirements.lock.txt"}
SKIP_GLOBS = ["*.lock"]


def _mask(value: str) -> str:
    return value[:4] + "●"


def _should_skip(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    if name in SKIP_NAMES:
        return True
    return any(fnmatch.fnmatch(name, g) for g in SKIP_GLOBS)


def _allowed_names(root: Path) -> set[str]:
    data = json.loads((root / "tools" / "allowed_names.json").read_text(encoding="utf-8"))
    allowed: set[str] = set()
    for key in ("facility_a", "facility_b", "generic", "test_exceptions"):
        allowed.update(data.get(key, []))
    return allowed


def _tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def main(root: Path | str) -> int:
    root = Path(root)
    allowed = _allowed_names(root)
    findings: list[str] = []

    for rel in _tracked_files(root):
        if _should_skip(rel):
            continue
        try:
            text = (root / rel).read_bytes().decode("utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, pattern in KEY_PATTERNS:
                for m in pattern.finditer(line):
                    findings.append(f"{rel}:{lineno} {label} key-like string ({_mask(m.group(0))})")
            for m in NAME_PATTERN.finditer(line):
                name = m.group(1)
                if name not in allowed:
                    findings.append(f"{rel}:{lineno} name not in allowed list ({name}さん)")

    if findings:
        for f in findings:
            print(f)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    repo = Path(__file__).resolve().parent.parent
    sys.exit(main(repo))
