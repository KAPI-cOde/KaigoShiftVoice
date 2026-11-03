"""事情のルール（aging.py・docs/rule-cleanup.md の5）。理由が一時的な事情のルールを、何か月か後に見直しで聞く。"""
import shutil

import pytest

from shiftvoice.aging import RecordedAging, aged, classify_rules
from shiftvoice.review import answer, review_cards
from shiftvoice.store import Store

from conftest import SEED


@pytest.fixture
def st(tmp_path):
    shutil.copytree(SEED, tmp_path / "f")
    return Store(tmp_path / "f")


def _add(store, rid, name, reason, frm="2026-08-01", until=None, who=("千葉",)):
    led = store.ledger()
    led.append("add", rule={"id": rid, "name": name, "kind": "count", "flex": "operational",
                            "params": {"shifts": ["夜"], "max": 0}, "who": list(who), "when": None, "scope": "rule",
                            "meta": {"said": name, "reason": reason}}, op_id=f"t-{rid}", frm=frm, until=until)
    store.save_ledger(led)


class Spy(RecordedAging):
    def __init__(self, table):
        super().__init__(table)
        self.calls = []

    def classify(self, reasons):
        self.calls.append(list(reasons))
        return super().classify(reasons)


def test_a1_classify_sends_masked_reason_once(st):
    _add(st, "g1", "千葉さん 夜勤なし", "千葉さんが腰を痛めている")
    _add(st, "g2", "千葉さん 夜勤なし2", "体の負担")
    spy = Spy({"職員16さんが腰を痛めている": True, "体の負担": False})
    out = classify_rules(st, spy)
    assert out == {"sent": 2, "skipped": 0}
    assert sorted(spy.calls[0]) == ["体の負担", "職員16さんが腰を痛めている"]   # 名前は番号に伏せて送る
    classify_rules(st, spy)
    assert len(spy.calls) == 1                                                # 同じ理由は二度送らない
    assert aged(st, 2026, 12) == {"g1": 4}


def test_a2_unknown_name_not_sent_and_until_skipped(st):
    _add(st, "g1", "千葉さん 夜勤なし", "森田さんの代わり")                    # 名簿にない名前
    _add(st, "g2", "千葉さん 夜勤なし2", "試験", until="2026-12-31")         # 期限で終わる
    spy = Spy({})
    out = classify_rules(st, spy)
    assert out == {"sent": 0, "skipped": 1} and spy.calls == []


def test_a3_review_card_and_keep(st):
    _add(st, "g1", "千葉さん 夜勤なし", "腰を痛めている")
    classify_rules(st, RecordedAging({"腰を痛めている": True}))
    v = review_cards(st.ledger(), 2026, 12, aged=aged(st, 2026, 12), temp_months=3)
    c = next(c for c in v["cards"] if c.reason == "aged")
    assert c.rule_id == "g1" and c.months == 4 and c.reason_text == "腰を痛めている"
    from shiftvoice.nextflow import card_lines
    assert card_lines(c, 12, 11)["lines"][0] == "4か月前に「腰を痛めている」という事情で足したルールです"
    answer(st, 2026, 12, "g1", "keep", aged=aged(st, 2026, 12), temp_months=3)
    v = review_cards(st.ledger(), 2027, 2, aged=aged(st, 2027, 2), temp_months=3)
    assert [c for c in v["cards"] if c.reason == "aged"] == []               # 答えてから3か月は聞かない
    v = review_cards(st.ledger(), 2027, 3, aged=aged(st, 2027, 3), temp_months=3)
    assert [c.rule_id for c in v["cards"] if c.reason == "aged"] == ["g1"]
