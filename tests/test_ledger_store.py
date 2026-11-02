"""変更の帳面と保存の骨組み。"""
import json

import pytest

from shiftvoice.ledger import Ledger, LedgerError
from shiftvoice.model import Acceptance
from shiftvoice.store import Store, atomic_write_json


def test_seed_ledger_has_r001_to_r012(store):
    rules = store.ledger().rules_at()
    assert sorted(rules) == [f"r{i:03d}" for i in range(1, 13)]


def test_ledger_until_and_undo():
    lg = Ledger()
    lg.append("add", rule={"id": "r103", "name": "小島さんの夜勤の回数", "kind": "count", "flex": "operational",
                           "who": ["小島"], "params": {"shifts": ["夜"], "max": 3}}, until="2026-11-29")
    assert "r103" in lg.rules_at(on="2026-11-01")
    assert "r103" not in lg.rules_at(on="2026-12-01")  # 期限が過ぎた札は効かない
    rm = lg.append("remove", rule_id="r103")
    assert "r103" not in lg.rules_at()
    u = lg.append("undo", target_rev=rm["rev"])
    assert "r103" in lg.rules_at()
    lg.append("undo", target_rev=u["rev"])  # 取り消しの取り消し
    assert "r103" not in lg.rules_at()
    assert "r103" in lg.rules_at(at_rev=1)  # 確定した月はその時の rev で読む


def test_ledger_rejects_bad_tx():
    lg = Ledger()
    with pytest.raises(LedgerError):
        lg.append("change", rule_id="nai", rule={"name": "x"})
    assert lg.rev == 0  # 当たらない tx は足さない


def test_atomic_write_and_pending(tmp_path, store):
    s = Store.from_seed(store.root, tmp_path / "rt")
    u = s.add_pending(2026, 11, "岡田さん、15と16は法事で休み")
    assert s.pending(2026, 11)[0]["id"] == u["id"]
    s2 = Store(tmp_path / "rt")  # 開き直しても残る
    assert s2.pending(2026, 11)[0]["text"].startswith("岡田さん")
    s2.resolve_pending(2026, 11, u["id"])
    assert s2.pending(2026, 11) == []
    assert not list((tmp_path / "rt" / "months").glob("*.tmp"))


def test_atomic_write_keeps_old_on_failure(tmp_path):
    p = tmp_path / "a.json"
    atomic_write_json(p, {"v": 1})
    with pytest.raises(TypeError):
        atomic_write_json(p, {"v": object()})
    assert json.loads(p.read_text()) == {"v": 1}
    assert [x.name for x in tmp_path.iterdir()] == ["a.json"]


def test_acceptance_record_links_op(tmp_path, store):
    s = Store.from_seed(store.root, tmp_path / "rt")
    seed = s.acceptances()
    assert {a.staff for a in seed} == {"田村", "菊池"}   # B0 用意した記録（田村さん2回・菊池さん1回）
    s.save_acceptances([Acceptance(id="a1", staff="小島", date="2026-11-01", layer="standby", op_id="op-1", tx_rev=13)])
    a = s.acceptances()[0]
    assert (a.staff, a.op_id, a.tx_rev) == ("小島", "op-1", 13)


def test_month_has_layers_and_prev_tail(store):
    m = store.month(2026, 11)
    assert m.days == 30 and m.holidays == [3, 23] and m.weekday(1) == "sun"
    assert all(len(v) == 5 for v in m.prev_tail.values()) and len(m.prev_tail) == 16
    assert m.layers == {}
