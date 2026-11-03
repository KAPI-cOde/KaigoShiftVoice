"""事情のルール（docs/rule-cleanup.md の5）。理由が一時的な事情のルールを、何か月か後に「今も要りますか」と聞く。

「腰を痛めているから夜勤なし」のようなルールは、表をきちんと縛り続けるので静か（quiet.py）にはならない。
事情が終わっても誰も外さないまま残る。理由の文が一時的な事情（ケガ・試験・家族の介護・研修期間 など）か、
ずっと続く事情（体の負担・施設の決まり など）かは言葉の読み取りなので、Claude（Haiku）に分けてもらう。

- 送るのは理由の文だけ。名簿の名前は番号に伏せ、名簿にない名前が残る文は送らない（読み取りと同じ決まり）
- 答えは理由の文ごとに置き場の aging.json に残し、同じ文は二度送らない
- 期限（until）があるルールは、期限で自然に終わるので聞かない
- 決めるのは作成者。片付けの候補として見直しに出すだけ
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .names import Masker
from .store import Store, atomic_write_json, read_json

MODEL = "claude-haiku-5-5"
TEMP_MONTHS = 3   # 一時的な事情のルールを、足してから何か月で聞くか（施設の設定 review.temporary_months）

SYSTEM = """あなたは介護施設の勤務表アプリの手伝いです。勤務のルールに添えられた「理由」の文を読み、
その事情が一時的なもの（いつか終わる。ケガ・病気の回復・試験・研修期間・家族の入院や介護・妊娠・引っ越し など）か、
ずっと続くもの（体の負担を減らす配慮・施設の決まり・本人の働き方の希望・資格や役割 など）かを分けます。
人の名前は「職員NN」と伏せてあります。
返事は JSON だけ。形は {"answers": [{"i": 番号, "temporary": true か false}]}。分からない時は false にします。"""


@dataclass
class RecordedAging:
    """試験用。理由の文 → 一時的か。"""
    table: dict[str, bool] = field(default_factory=dict)

    def classify(self, reasons: list[str]) -> list[bool]:
        return [bool(self.table.get(r, False)) for r in reasons]


@dataclass
class HaikuAging:
    model: str = MODEL
    timeout: float = 20.0

    def classify(self, reasons: list[str]) -> list[bool]:
        import anthropic
        body = "\n".join(f"{i}. {r}" for i, r in enumerate(reasons, 1))
        client = anthropic.Anthropic(timeout=self.timeout, max_retries=1)
        resp = client.messages.create(
            model=self.model, max_tokens=2000, system=SYSTEM,
            messages=[{"role": "user", "content": f"理由の一覧\n{body}"}],
            thinking={"type": "disabled"}, output_config={"effort": "low"},
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
        out = [False] * len(reasons)
        for a in data.get("answers", []):
            i = a.get("i")
            if isinstance(i, int) and 1 <= i <= len(reasons):
                out[i - 1] = a.get("temporary") is True
        return out


def _path(store: Store):
    return store.root / "aging.json"


def classify_rules(store: Store, classifier) -> dict:
    """理由つきの毎月のルールの理由を分けて、置き場の aging.json に足す。送った件数と、送らなかった件数を返す。"""
    cache: dict = read_json(_path(store)) or {}
    staff, fac = store.staff(), store.facility()
    masker = Masker(staff, fac)
    todo: dict[str, str] = {}      # 伏せた文 → 元の理由
    skipped = 0
    for r in store.ledger().rules_at().values():
        reason = (r.meta.get("reason") or "").strip()
        if r.scope != "rule" or not reason or r.meta.get("until") or reason in cache:
            continue
        masked, unknown = masker.mask(reason)
        if unknown or masker.leftover:
            skipped += 1           # 名簿にない名前・伏せきれない呼び方が残る文は送らない
            continue
        todo[masked] = reason
    if todo:
        keys = list(todo)
        for k, temp in zip(keys, classifier.classify(keys)):
            cache[todo[k]] = {"temporary": bool(temp), "sent": k}
        atomic_write_json(_path(store), cache)
    return {"sent": len(todo), "skipped": skipped}


def aged(store: Store, year: int, month: int) -> dict[str, int]:
    """対象の月（year, month）の時点で、一時的な事情の理由を持ち、足してから何か月たったか（札ごと）。"""
    cache: dict = read_json(_path(store)) or {}
    out: dict[str, int] = {}
    for rid, r in store.ledger().rules_at().items():
        reason = (r.meta.get("reason") or "").strip()
        frm = r.meta.get("from")
        if r.scope != "rule" or not reason or r.meta.get("until") or not frm:
            continue
        if not (cache.get(reason) or {}).get("temporary"):
            continue
        out[rid] = (year - int(frm[:4])) * 12 + month - int(frm[5:7])
    return out


def temporary_months_setting(store: Store) -> int:
    return int(store.facility().settings.get("review", {}).get("temporary_months", TEMP_MONTHS))
