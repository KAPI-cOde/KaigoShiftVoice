import importlib.util
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("check_public", REPO / "tools" / "check_public.py")
check_public = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_public)


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    shutil.copy(REPO / "tools" / "allowed_names.json", root / "tools" / "allowed_names.json")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def test_allowed_names_only_is_ok(tmp_path, capsys):
    root = _make_repo(tmp_path, {"doc.md": "青木さんと皆さんに確認した。\n"})
    assert check_public.main(root) == 0
    assert "ok" in capsys.readouterr().out


def test_unlisted_name_is_reported(tmp_path, capsys):
    name = "山田" + "さ" + "ん"
    root = _make_repo(tmp_path, {"doc.md": f"{name}に連絡\n"})
    assert check_public.main(root) == 1
    out = capsys.readouterr().out
    assert "doc.md:1" in out


def test_key_like_string_is_reported_and_masked(tmp_path, capsys):
    key = "sk-ant-" + "x" * 20
    root = _make_repo(tmp_path, {"config.txt": f"key = {key}\n"})
    assert check_public.main(root) == 1
    out = capsys.readouterr().out
    assert "config.txt:1" in out
    assert key not in out
