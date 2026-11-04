"""撮影用の --base-date。11月末の状態から撮る時に、今日より後の引き受け（14日・22日）も数える（2026-11-04）。"""
import sys

import pytest

import shiftvoice


def test_demo_base_date_is_passed(monkeypatch, tmp_path):
    seen = {}

    def fake_demo(args, lock):
        seen["base_date"] = args.base_date
    monkeypatch.setattr(shiftvoice, "_demo", fake_demo)
    monkeypatch.setattr(sys, "argv", ["shiftvoice", "demo", "--no-open", "--data", str(tmp_path / "d"),
                                      "--base-date", "2026-11-30"])
    try:
        shiftvoice.main()
    except SystemExit:
        pass
    assert seen["base_date"] == "2026-11-30"


def test_bad_base_date_is_refused():
    import datetime as dt
    with pytest.raises(ValueError):
        dt.date.fromisoformat("2026-11-31")
