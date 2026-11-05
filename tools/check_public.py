"""Check tracked files for key-like strings and unapproved name forms.

Usage: python tools/check_public.py
       python tools/check_public.py --dir PATH   （送る物を写した置き場を、Git の除外にかかわらず全部見る）
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
        # 追跡中のファイルに加え、まだ add していない新しいファイルも見る（.gitignore の物は除く）。
        # add の前に点検を流して、新しい試験のファイルの名前を見落としたことがあるため（2026-11-03）
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def _all_files(d: Path) -> list[str]:
    """置き場の中の全部のファイル（.git の中は除く）。.gitignore で外れる .env なども見る。"""
    return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file() and ".git" not in p.relative_to(d).parts)


def main_dir(d: Path | str, repo: Path | str) -> int:
    """Space へ送る物を写した置き場 d を、そのまま全部見る（名前の許可はリポジトリ repo の一覧）。"""
    return main(repo, files_root=Path(d))


def main(root: Path | str, files_root: Path | None = None) -> int:
    root = Path(root)
    allowed = _allowed_names(root)
    findings: list[str] = []
    base = files_root or root
    files = _all_files(base) if files_root else _tracked_files(root)

    for rel in files:
        if _should_skip(rel):
            continue
        try:
            text = (base / rel).read_bytes().decode("utf-8")
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
    if len(sys.argv) == 3 and sys.argv[1] == "--dir":
        sys.exit(main_dir(Path(sys.argv[2]), repo))
    sys.exit(main(repo))
