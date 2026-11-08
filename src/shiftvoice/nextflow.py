"""翌月へ進む流れ（台本の3幕）。ルールの見直し（画面8）→ 12月の表を返さない案・返す案で作る（画面6）→ 採る。

画面に出す文はコードが作る（札の表は3行。spec.md 画面8・画面6、計画 B8）。
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import replace

from .build import C3_MESSAGE, TIMEOUT_MESSAGE, month_rules
from .giveback import _months_back, accepted_counts, build_pair
from .giveback import settings as gb_settings
from .intake import WHO_UNKNOWN, Clarify, to_proposal
from .ledger import LedgerError
from .addshift import hide_spoken, read_spoken
from .names import Masker
from .period import other_month, other_month_message, relative_days
from .nextmonth import copy_next_month, next_ym
from .reader import KeyProblem, LimitError, ReadError
from .review import answer, history, review_cards, undo_answer
from .rules import dated_same_as_latest
from .store import Store


def _md(date: str) -> str:
    return f"{int(date[5:7])}月{int(date[8:10])}日"


def card_lines(c, month: int, prev: int) -> dict:
    if c.reason == "expired":
        return {"label": "期限が来た", "title": c.name,
                "lines": [f"{_md(c.until)}で終わりました。{month}月には効きません",
                          f"片付けても、{prev}月の表と引き受けの記録は残ります"],
                "buttons": [["archive", "履歴へ片付ける（おすすめ）"], ["extend", "期間を延ばす"]]}
    if c.reason == "aged":
        return {"label": "事情が続いているか", "title": c.name,
                "lines": [f"{c.months}か月前に「{c.reason_text}」という事情で足したルールです",
                          "事情が終わっていたら片付けてください。続いているなら［今月も使う］で、また何か月か後に聞きます"],
                "buttons": [["keep", "今月も使う"], ["archive", "履歴へ片付ける"]]}
    if c.reason == "overlap":
        return {"label": "同じことを決めている2件", "title": f"{c.name}　と　{c.partner_name}",
                "lines": [f"{c.months}か月続けて、どちらか片方を外して作り直しても、表は両方のルールどおりでした",
                          (f"ゆるい方の「{c.name}」を片付けても、厳しい方の「{c.partner_name}」で守られます。"
                           if c.loose else "片方を片付けると、もう片方だけで守ります。")
                          + "両方を一度に外すと守りが消えるので、片方だけ選んでください"],
                "buttons": [["archive", f"「{c.name}」を片付ける" + ("（おすすめ）" if c.loose else "")],
                            ["archive_partner", f"「{c.partner_name}」を片付ける"], ["keep", "両方使う"]]}
    if c.reason == "quiet":
        what = f"この{1 + len(c.group)}件のルール" if c.group else "このルール"
        first = (f"{c.months}か月続けて、{what}が当てはまる人と日がありませんでした（今の名簿にいない人のルールなど）"
                 if c.no_scene else f"{c.months}か月続けて、このルールを外して作り直しても、表はこのルールどおりでした")
        return {"label": "当てはまる日が無い" if c.no_scene else "無くても同じだった", "title": c.name,
                "lines": [first,
                          f"片付けると、表を作る時に考えるルールが{1 + len(c.group)}件減ります。{prev}月までの表は変わらず、"
                          "また要る時は一言で足し直せます"],
                "buttons": [["archive", f"{1 + len(c.group)}件とも履歴へ片付ける" if c.group else "履歴へ片付ける"],
                            ["keep", "今月も使う"]]}
    if c.reason == "added_last_month" and c.group:
        lines = ["今月も使いますか", "、".join(c.group_names)]
        if c.pairs:   # 札は2行までしか出ないので、2行目に足す
            lines[1] += "。" + "。".join(f"このうち「{a}」と「{b}」は同じ所を決めています（1件ずつ見ると、片方を変えられます）"
                                        for a, b in c.pairs[:2])
        return {"label": "先月増えた", "title": c.name, "lines": lines,
                "buttons": [["keep", "全部 今月も使う"], ["split", "1件ずつ見る"]]}
    # 当番（待機など）の中身を話して直すのは、まだ作っていない。［変える］を出さない
    buttons = [["keep", "今月も使う"]] + ([] if c.kind == "layer" else [["change", "変える（話す）"]])
    return {"label": "先月増えた", "title": c.name, "lines": ["今月も使いますか"], "buttons": buttons}


ANSWER_WORDS = {"archive": "片付けた", "extend": "延ばした", "keep": "今月も使う", "change": "変えた",
                "archive_partner": "もう片方を片付けた"}


# ---- 表を作る前に入れる希望休（作者の作り方の最初の一歩。全員の希望休を入れてから表を作る） ----
WISH_SOURCE = "作る前"          # 帳面の meta.source。翌月の表を作る前に入れた頼みの印
WISH_MARKS = {"希", "希望", "希休", "希望休"}
WISH_ONLY = "表を作る前に入れられるのは、日付の決まった休みや勤務の頼みと、日付を決めた人数（「12日は早番を2人」）です。ほかの頼みは{m}月の表を開いてから言ってください。何も入れていません"


def _wish_span(y: int, m: int) -> tuple[str, str]:
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


class WishOtherMonth(ValueError):
    """貼った希望の表が、入れている月と違う月の物。何も入れない"""

    def __init__(self, said: int):
        super().__init__(said)
        self.said = said


def _label_month(label: str) -> int | None:
    """日の見出しに書いてある月（12/1・12月1日・2026-12-01・Excel の通し番号）。1・1日のように書いていなければ None。"""
    from .onboard import _DAY, _DAY_MD, SERIAL_RANGE
    if re.fullmatch(r"\d{5}", label) and SERIAL_RANGE[0] <= int(label) <= SERIAL_RANGE[1]:
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(label))).month
    m = _DAY_MD.match(label) or _DAY.match(label)
    return int(m.group(1)) if m and m.group(1) else None


def _named_months(cells: list[str]) -> set[int]:
    """題や見出しの隅の「11月」「2026年11月の希望休」の月。「11月30日締切」のような日付は数えない。"""
    found = re.findall(r"(?<![0-9])(\d{1,2})月(?!\s*\d)", " ".join(cells))
    return {int(x) for x in found if 1 <= int(x) <= 12}


def parse_wish_paste(text: str, roster: list[str], days: int,
                     month: int | None = None) -> tuple[dict[str, list[int]], list[str]]:
    """来月の希望を書いた表（名前と日の見出しと「希」）を読む。返すのは 名前 → 日 と、名簿にない名前。
    取り込み（onboard.parse_paste）は空欄を公休にし「希」を公に読み替えるので、希望の紙には使わない
    （空欄＝希望なし、希＝希望休、を分けたいため）。日の見出しは onboard と同じ読み方（1・1日・12/1・12月1日）。
    month を渡すと、見出しや題に書いてある月を見て、別の月のマスに「希」があれば WishOtherMonth を上げる
    （前は月を捨てて日だけ読み、11月の表が12月に入った。Astra 2026-11-06 の指摘4）。
    名前は、全部が同じか、空白で区切った前の語が同じ時だけ本人とする（「岡田 花」は岡田。前は先頭が同じなら
    本人にしていて、名簿にない長い名前が短い名前の別の人に入った。指摘5）"""
    from .onboard import _cell_text, _clean_name, _day_number, _nfkc
    import csv
    import io
    text = _nfkc(text or "")
    delim = "\t" if "\t" in text else ","
    rows = [[_cell_text(c) for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]
    rows = [r for r in rows if any(r)]
    cols: dict[int, int] | None = None
    col_month: dict[int, int | None] = {}
    named: set[int] = set()     # 見出しより上の題に書いてある月
    out: dict[str, list[int]] = {}
    unknown: list[str] = []
    for cells in rows:
        nums = {j: _day_number(c) for j, c in enumerate(cells) if c}
        nums = {j: d for j, d in nums.items() if d is not None}
        if cols is None or (len(nums) >= 3 and len(nums) >= len([c for c in cells if c]) - 2):
            if len(nums) >= 3:   # 見出しの行（日の番号が並ぶ行）
                cols = {j: d for j, d in nums.items() if 1 <= d <= days}
                named |= _named_months([c for j, c in enumerate(cells) if j not in nums])
                title = None if month is None or month in named or not named else min(named)
                col_month = {j: _label_month(cells[j]) or title for j in cols}
                named = set()
                continue
        if cols is None:
            named |= _named_months(cells)
            continue
        marks = [d for j, d in cols.items() if j < len(cells) and re.sub(r"\s", "", cells[j]) in WISH_MARKS]
        if not marks:
            continue
        if month is not None:
            said = next((col_month[j] for j in cols if j < len(cells) and col_month[j] not in (None, month)
                         and re.sub(r"\s", "", cells[j]) in WISH_MARKS), None)
            if said is not None:
                raise WishOtherMonth(said)
        name = next((n for n in roster for c in cells[:min(cols)] if c and _clean_name(c) in (n, n + "さん")), None)
        if name is None:
            name = next((n for n in roster for c in cells[:min(cols)]
                         if c and len(c.split()) > 1 and _clean_name(c.split()[0]) == n), None)
        if name is None:
            raw = next((c for c in cells[:min(cols)] if c and not c.isdigit()), "")
            if raw:
                unknown.append(_clean_name(raw))
            continue
        out.setdefault(name, [])
        out[name] = sorted(set(out[name]) | set(marks))
    return out, unknown



def base_failed_text(message: str, has_choke: bool) -> str:
    """翌月の表（返さない案）そのものが作れなかった時の文。表を作る仕組みの問い（「もう少し考えますか、どれを譲るかを
    決めますか」「このルールを入れると…外しますか」）は、この画面に答えるボタンが無いので、次にすることを言う。
    希望休を外す相談は最後（作者 2026-11-07）なので、先に詰まり所やその日の人数を一言で空けるよう言う"""
    if has_choke:
        return ("全部のルールと希望休を同時に守る表が見つかりませんでした。下のいちばんの詰まり所を、［見直しへ戻る］の"
                "希望休の欄に「12日は早番を2人」「12日、〇〇さんは早番に」のような一言を入れて空けてから、もう一度作ってください")
    if message == TIMEOUT_MESSAGE:
        return ("全部のルールと希望休を同時に守る表が、この時間では見つかりませんでした。行事の日の人数など、その日だけ"
                "ゆるめられる所を、［見直しへ戻る］の希望休の欄に「12日は早番を2人」のような一言で入れてから、もう一度作ってください")
    if message == C3_MESSAGE:
        return ("動かせないルール（毎日の人数・公休の数など）どうしがぶつかって、表が作れませんでした。［見直しへ戻る］で、"
                "今月足したルールと希望休を見直してください")
    return message


def _choke_out(chokes: list[dict], month) -> list[dict]:
    """詰まり所を画面に渡す形（文と、赤い枠を付けるマス「名前|日」）。1件目は詳しい文、2件目からは1行。"""
    from .choke import explain
    return [{"day": c["day"], "text": t, "cells": [f"{n}|{d}" for n, d in c["cells"]]}
            for c, t in zip(chokes, explain(chokes, month))]


class NextFlow:
    def __init__(self, store: Store, year: int, month: int):
        """year, month は今の月（11月）。進む先は翌月（12月）。"""
        self.store, self.year, self.month = store, year, month
        self.ny, self.nm = next_ym(year, month)

    def _ensure(self):
        copy_next_month(self.store, self.year, self.month)

    def review(self, split: bool = False) -> dict:
        self._ensure()
        led = self.store.ledger()
        # 片付けの候補は、専用の画面「ルールの整理」（cleanup.py）で全部まとめて決める。見直しには混ぜない（作者 2026-11-04）
        v = review_cards(led, self.ny, self.nm, split=split)
        cards = []
        for c in v["cards"]:
            d = card_lines(c, self.nm, self.month)
            d.update({"rule_id": c.rule_id, "reason": c.reason, "said": c.said, "until": c.until,
                      "answer": v["answers"].get(c.rule_id, {}).get("answer")})
            cards.append(d)
        n = len(cards)
        def word(c):
            a = v["answers"][c["rule_id"]]
            return ANSWER_WORDS[c["answer"]] + (f"（{a['new_name']}）" if a.get("new_name") else "")
        def line(c):
            if c["reason"] == "overlap":   # どちらを片付けたかを書く（「AとB → 片付けた」では分からない）
                a, b = c["title"].split("\u3000と\u3000")
                return {"keep": f"{c['title']} → 両方使う", "archive": f"{a} → 片付けた（{b}は残す）",
                        "archive_partner": f"{b} → 片付けた（{a}は残す）"}[c["answer"]]
            return f"{c['title']} → {word(c)}"
        summary = [line(c) for c in cards if c["answer"]]
        return {"month": self.nm, "cards": cards, "cursor": v["cursor"], "total": n,
                "head": f"{self.nm}月の表を作る前に　見直すルール {n}件中{min(v['cursor'] + 1, n)}件目"
                        f"（残り{max(n - v['cursor'] - 1, 0)}件）" if v["cursor"] < n else
                        f"{self.nm}月の表を作る前に　見直しは済みました",
                "unchanged": f"変更なし {v['unchanged']}件（今回見直すのは、期限が来たルールと先月増えたルールだけ）",
                "once": f"{self.month}月だけの頼み{v['once']}件は、{self.nm}月へ引き継ぎません",
                "once_names": v["once_names"],

                "done": v["cursor"] >= n, "summary": summary,
                "history": history(led), **self.wishes()}

    def answer(self, rule_id: str, what: str, new_until: str | None = None, split: bool = False) -> dict:
        answer(self.store, self.ny, self.nm, rule_id, what, new_until=new_until, split=split)
        return self.review(split)

    def change(self, rule_id: str, text: str, reader, split: bool = False) -> dict:
        """［変える（話す）］。先月増えた札を、話した（書いた）一言の中身に置き換える（M8）。
        読み取りは1幕の一言と同じ（名前を伏せて送る・型はコードが確かめる）。読めない時は札を変えない。"""
        self._ensure()
        text = (text or "").strip()
        if not text:
            return {"error": "変え方を話すか、文字で入れてください"}
        fac, staff = self.store.facility(), self.store.staff()
        masker = Masker(staff, fac)
        masked, unknown = masker.mask(text)
        if unknown:
            if unknown[0] in masker.leftover:
                return {"error": f"名前が読み取れません（{unknown[0]}）。{masker.leftover[unknown[0]]}さんのように、名前に「さん」を付けて言い直してください。ルールは変えていません"}
            return {"error": f"名簿にない名前です（{'・'.join(unknown)}）。ルールは変えていません"}
        try:
            items = masker.unmask(read_spoken(reader, *hide_spoken(masked, fac)))
            p = to_proposal(items, text, fac, staff, self.store.month(self.ny, self.nm))
        except (LimitError, KeyProblem) as e:   # 公開版で送る前に止めた（public.py）・鍵の間違いなど
            return {"error": str(e)}
        except ReadError:
            return {"error": "読み取りに失敗しました。ルールは変えていません"}
        except Clarify as e:
            return {"error": str(e)}
        if p.kind != "rule" or len(p.rules) != 1:
            return {"error": "このルールの変え方を1つだけ言ってください。ルールは変えていません"}
        try:
            # ［1件ずつ見る］の時は split を渡す（渡さないと札がまとめ直され、2件目以降を変えられなかった。
            # 敵対レビュー 2026-11-04 の A）
            answer(self.store, self.ny, self.nm, rule_id, "change", new_rule=p.rules[0], said=text,
                   new_until=p.until, split=split)
        except LedgerError as e:
            return {"error": str(e)}
        return self.review(split)

    def undo(self, rule_id: str, split: bool = False) -> dict:
        undo_answer(self.store, self.ny, self.nm, rule_id)
        return self.review(split)

    def build(self, base_date: str | None = None) -> dict:
        """返さない案と返す案を同じ条件で作る。数えるのは返していない引き受け（さかのぼるのは施設の設定の
        window_months か月。基準日は今の月の末）。"""
        self._ensure()
        last = calendar.monthrange(self.year, self.month)[1]
        base_date = base_date or f"{self.year:04d}-{self.month:02d}-{last:02d}"
        ym = f"{self.year:04d}-{self.month:02d}"
        fac, staff = self.store.facility(), self.store.staff_on(self.ny, self.nm)
        window = gb_settings(fac)["window_months"]
        target = f"{self.ny:04d}-{self.nm:02d}"
        # 作る月に返した印のある記録は、作り直す時はまだ返していない扱いで数える。採った後に作り直しても、
        # 同じ引き受けを数えて返す案を作れる（印は採る時に付け直す。2026-11-05 敵対レビュー 3×8。前は
        # 「引き受けた人はいません」になり、採り直すと返していない表に置き換わり、印だけが残った）
        items = [replace(x, returned_in=None) if x.returned_in == target else x for x in self.store.acceptances()]
        counts = accepted_counts(items, ym, base_date, window)
        dec = self.store.month(self.ny, self.nm)
        leave = {st.symbol for st in fac.shift_types if st.kind == "leave"}
        this = self.store.month(self.year, self.month).table or {}
        gb = build_pair(fac, staff, month_rules(self.store, self.ny, self.nm, with_premise=False), dec, counts,
                        last_leave={n: sum(v in leave for v in row) for n, row in this.items()})
        # note は返さない案で段を下げた時の知らせ（赤で出す）
        out = {"status": gb.status, "message": gb.message, "counts": counts, "checks": gb.checks, "note": gb.base_note,
               "choke": _choke_out(gb.base_choke, dec)}
        if out["choke"] and out["note"]:
            # 詰まり所の箱は「先に空けられないか試し、どうしても空かない時に相談」と言うので、すぐ上の知らせで先に相談を
            # すすめない（同じ画面で食い違った。作者の方針は対処をしつくしてから相談）
            out["note"] = out["note"].replace("本人と相談してください", "相談の前に、下のいちばんの詰まり所で空けられないか試してください")
        if gb.status == "base_failed":
            out["title"] = f"{self.nm}月の表が作れませんでした"   # 返す・返さないの前に、元の表ができていない
            out["message"] = base_failed_text(gb.message, bool(out["choke"]))
        if gb.status == "cannot" and not any(v > 0 for v in counts.values()):
            # 引き受けた人がいない月（取り込んで作ったばかりの施設など）。返す物が無いので、返さない案だけで採れるようにする
            dec_days = dec.days
            out.update({"status": "ok", "message": "", "main": f"{self.month}月に引き受けた人はいません。{self.nm}月はふだんの作り方で作りました",
                        "who": None, "sub": "" if gb.base_note else "人数とルールはそのまま", "line2": "", "people": [], "others": {},
                        "changed": 0, "leave_avg": round(gb.leave_avg, 1), "stage": [],
                        "seconds": round(gb.base_seconds, 2), "tables": {"base": gb.base.table, "give": gb.base.table},
                        "days": dec_days, "weekdays": [dec.weekday(d) for d in range(1, dec_days + 1)],
                        "holidays": dec.holidays, "no_takers": True})
            m = self.store.month(self.ny, self.nm)
            m.layers["_plans"] = {"base": gb.base.table, "give": gb.base.table, "rev": self.store.ledger().rev,
                                  "tail": m.prev_tail, "acc_ids": []}
            self.store.save_month(m)
            return out
        if gb.status != "ok":
            return out
        who = max(gb.people, key=lambda p: p["accepted"])  # 同じ回数なら people の並び順（名簿の順）
        parts = []
        if who["leave_after"] > who["leave_before"]:
            parts.append(f"有給 {who['leave_before']}→{who['leave_after']}日")
        if who["run_after"] < who["run_before"]:
            parts.append(f"いちばん長い連勤 {who['run_before']}→{who['run_after']}日")
        o = gb.others
        changed = sum(x != y for n in gb.base.table for x, y in zip(gb.base.table[n], gb.give.table[n]))
        out.update({
            "main": f"{who['name']}さん　{self.month}月に{who['accepted']}回引き受け → {self.nm}月は" + "・".join(parts),
            "who": who["name"],
            "sub": "ほかの人の有給と、連勤の合計は増やさない。人数とルールはそのまま",
            "line2": f"引き受けた{sum(p['leave_after'] > p['leave_before'] or p['run_after'] < p['run_before'] for p in gb.people)}人に、"
                     + {("leave", "run"): "有給を1日と短い連勤", ("leave",): "有給を1日",
                                                          ("run",): "短い連勤"}[tuple(gb.stage)]
                     + f"。返さない案から変わるのは{changed}マス",
            "people": gb.people,
            "others": o,
            "changed": changed,
            "leave_avg": round(gb.leave_avg, 1),
            "stage": list(gb.stage),
            "seconds": round(gb.give.seconds + gb.base_seconds, 2),
            "tables": {"base": gb.base.table, "give": gb.give.table},
            "days": dec.days, "weekdays": [dec.weekday(d) for d in range(1, dec.days + 1)],
            "holidays": dec.holidays,
        })
        self._pair = gb
        m = self.store.month(self.ny, self.nm)
        months = set(_months_back(ym, window))
        # 返した印は、実際に有給か連勤が良くなった人にだけ付ける。何も返らなかった人の引き受けは次の月へ持ち越す
        got = {p["name"] for p in gb.people if p["leave_after"] > p["leave_before"] or p["run_after"] < p["run_before"]}
        acc = [x for x in items if x.date[:7] in months and x.date <= base_date
               and not x.returned_in and x.staff in got]
        m.layers["_plans"] = {"base": gb.base.table, "give": gb.give.table, "rev": self.store.ledger().rev,
                              "tail": m.prev_tail,
                              "acc_ids": [x.id for x in acc],
                              # 採った月の表の題の下の1行で、実際に返した物と引き受けの月を言うため
                              "stage": list(gb.stage), "acc_months": sorted({x.date[:7] for x in acc}),
                              "people": [{k: p[k] for k in ("name", "leave_before", "leave_after", "run_before", "run_after")}
                                         for p in gb.people]}
        self.store.save_month(m)
        return out

    def adopt(self, plan: str) -> dict:
        """［12月はこれで作る］。選んだ案を12月の表にする（施設が決める）。"""
        m = self.store.month(self.ny, self.nm)
        plans = m.layers.get("_plans")
        if not plans or plan not in ("base", "give"):
            return {"error": "先に2つの案を作ってください"}
        cur = self.store.month(self.year, self.month).table
        if plans.get("rev") != self.store.ledger().rev or plans.get("tail") != {n: r[-5:] for n, r in cur.items()}:
            # 案を作った後にルールか前の月の末が変わった。古い案は採らない（Astra の指摘7）
            return {"error": f"案を作った後にルールか{self.month}月の表が変わりました。もう一度［{self.nm}月の表を作る］を押してください"}
        m.table = plans[plan]
        m.built_rev = self.store.ledger().rev
        m.layers["_adopted"] = plan
        # 返した引き受けに印を付ける。返さない案を採ったら、その月に前に付けた印も外して次の月へ持ち越す
        # （採り直した時に、前に採った案の印が残らないように）
        target = f"{self.ny:04d}-{self.nm:02d}"
        ids = set(plans.get("acc_ids") or []) if plan == "give" else set()
        items = self.store.acceptances()
        for x in items:
            if x.id in ids:
                x.returned_in = target
            elif x.returned_in == target:
                x.returned_in = None
        self.store.save_acceptances(items)
        self.store.save_month(m)
        return {"ok": True, "plan": plan}

    def wishes(self) -> dict:
        """翌月の表を作る前に入れた頼みの一覧と、まだ1件も入っていない人（画面の一覧）。"""
        start, end = _wish_span(self.ny, self.nm)
        led = self.store.ledger()
        items = [{"rule_id": r.id, "name": r.name, "said": r.meta.get("said"), "who": r.who or []}
                 for r in led.rules_at(between=(start, end)).values()
                 if r.meta.get("source") in (WISH_SOURCE, WISH_SOURCE + "（貼った表）") and r.meta.get("from") == start]
        have = {w for x in items for w in x["who"]}
        try:
            roster = [s.name for s in self.store.staff_on(self.ny, self.nm)]
        except Exception:  # noqa: BLE001 名簿が読めない時は「まだの人」を出さない
            roster = []
        return {"wishes": items, "wish_missing": [n for n in roster if n not in have], "wish_month": self.nm}

    def _add_wish_rules(self, rules: list[dict], said: str, source: str, op_id: str) -> list[str]:
        """頼みを翌月だけのルールとして帳面に足す。同じ人・同じ日・同じ中身が入っていれば足さない。返すのは足した名前"""
        start, end = _wish_span(self.ny, self.nm)
        led = self.store.ledger()
        cur = list(led.rules_at(between=(start, end)).values())
        at = dt.datetime.now().isoformat(timespec="seconds")
        from .session import _next_id
        n = _next_id(led)
        added = []
        for rd in rules:
            dated = dated_same_as_latest(rd, cur)
            if dated or (dated is None and any(
                    r.kind == rd["kind"] and r.params == rd["params"] and (r.who or None) == (rd.get("who") or None)
                    and r.when == rd.get("when") for r in cur)):
                continue
            rd = {**rd, "id": f"r{n}", "scope": "once",
                  "meta": {**rd.get("meta", {}), "said": said, "at": at, "source": source, "until": end}}
            n += 1
            led.append("add", rule=rd, op_id=op_id, frm=start, until=end, meta={"said": said, "at": at})
            added.append(rd["name"])
        if added:
            self.store.save_ledger(led)
        return added

    def wish(self, text: str, reader, split: bool = False) -> dict:
        """表を作る前の一言（「岡田さん、15と16は法事で休み」）。翌月だけの頼みとして帳面に入れ、表は作らない。
        作るのは［〇月の表を作る］の時（その時に、入った頼みを守って作る）"""
        self._ensure()
        text = (text or "").strip()
        out = self.review(split)
        if not text:
            return {**out, "wish_error": "希望休を話すか、文字で入れてください"}
        om = other_month(text, self.nm)
        if om is not None:   # 違う月の頼み。Claude へ送らずに聞く
            return {**out, "wish_error": other_month_message(om, self.nm, what="何も入れていません")}
        fac = self.store.facility()
        staff = self.store.staff_on(self.ny, self.nm)
        masker = Masker(self.store.staff(), fac)
        masked, unknown = masker.mask(text)
        if unknown:
            if unknown[0] in masker.leftover:
                return {**out, "wish_error": f"名前が読み取れません（{unknown[0]}）。{masker.leftover[unknown[0]]}さんのように、名前に「さん」を付けて言い直してください。何も入れていません"}
            return {**out, "wish_error": f"名簿にない名前です（{'・'.join(unknown)}）。何も入れていません"}
        masked, outside = relative_days(masked, dt.date.today(), self.ny, self.nm)
        if outside:
            word, d = outside[0]
            return {**out, "wish_error": f"「{word}」は{d.month}月{d.day}日で、{self.nm}月の外です。{self.nm}月の何日かで言ってください。何も入れていません"}
        dec = self.store.month(self.ny, self.nm)
        try:
            raw = read_spoken(reader, *hide_spoken(masked, fac))
            if not re.search(r"職員\d{2}", masked) and any(
                    isinstance(it, dict) and it.get("staff") not in (None, [], "") for it in raw):
                return {**out, "wish_error": WHO_UNKNOWN.replace("表もルールも変えていません", "何も入れていません")}
            items = masker.unmask(raw)
            p = to_proposal(items, text, fac, staff, dec)
        except (LimitError, KeyProblem) as e:
            return {**out, "wish_error": str(e)}
        except ReadError:
            if getattr(reader, "calls_claude", True) is False:
                return {**out, "wish_error": "Claude の鍵が入っていないので、一言では入れられません（Claude へは送っていません）。希望を書いた表を下に貼ると、鍵なしで入れられます"}
            return {**out, "wish_error": "読み取りに失敗しました。何も入れていません"}
        except Clarify as e:
            return {**out, "wish_error": str(e)}
        ok = (p.kind == "cells" and p.rules and not p.staff_changes and not p.removes and not p.covers
              and all(r["kind"] == "cell" and r.get("when") and r.get("who")
                      and not (r.get("meta") or {}).get("moved_to") and not (r.get("meta") or {}).get("moved_from")
                      for r in p.rules))
        # 日付を決めた人数（「12日は早番を2人」）も、その月だけの頼みとして入れる。全部の希望休を入れると組めない時に
        # 詰まり所の日の人数を減らして空ける一言を、表を作る前に言えるように（作者 2026-11-07「日勤専属の人に早出を
        # 頼む・その日の人数を減らす、などの対処をしつくしてから相談」。前は「表を開いてから」と断り、表の無い時は言えなかった）
        dated = (p.kind == "rule" and p.rules and not p.staff_changes and not p.removes and not p.covers
                 and all(r["kind"] == "coverage" and r.get("when") for r in p.rules))
        if not (ok or dated):
            return {**out, "wish_error": WISH_ONLY.format(m=self.nm)}
        start, _ = _wish_span(self.ny, self.nm)
        led = self.store.ledger()
        added = self._add_wish_rules(p.rules, text, WISH_SOURCE, f"wish-{start[:7]}-{led.rev + 1}")
        out = self.review(split)
        if not added:
            return {**out, "wish_note": f"{self.nm}月はもう入っています"}
        return {**out, "wish_note": "入れました　" + "、".join(added)}

    def wish_paste(self, text: str, split: bool = False) -> dict:
        """来月の希望を書いた表を貼った（Excel の「希」）。Claude は呼ばない"""
        self._ensure()
        staff = self.store.staff_on(self.ny, self.nm)
        dec = self.store.month(self.ny, self.nm)
        try:
            got, unknown = parse_wish_paste(text, [s.name for s in staff], dec.days, self.nm)
        except WishOtherMonth as e:
            return {**self.review(split), "wish_error": f"{e.said}月の希望の表です。いま入れているのは{self.nm}月です。"
                    f"{self.nm}月の表を貼ってください。何も入れていません"}
        if not got:
            return {**self.review(split), "wish_error": "「希」の入ったマスが見つかりませんでした。"
                    "1行目に日の番号（1から）、左の列に名前、希望の日に「希」を書いた表を貼ってください。何も入れていません"}
        rules = [{"name": f"{n}さん {'・'.join(map(str, ds))}日 希望休", "kind": "cell", "flex": "wish",
                  "params": {"in": ["公"]}, "who": [n], "when": ds} for n, ds in got.items()]
        start, _ = _wish_span(self.ny, self.nm)
        led = self.store.ledger()
        added = self._add_wish_rules(rules, "貼った希望の表", WISH_SOURCE + "（貼った表）",
                                     f"wish-{start[:7]}-{led.rev + 1}")
        out = self.review(split)
        note = f"{len(added)}人分を入れました" if added else f"{self.nm}月はもう入っています"
        if unknown:
            note += f"。名簿にない名前は入れていません（{'・'.join(unknown)}）"
        return {**out, "wish_note": note}

    def unwish(self, rule_id: str, split: bool = False) -> dict:
        """一覧の［取り消す］。作る前に入れた頼みだけ外せる"""
        start, _ = _wish_span(self.ny, self.nm)
        led = self.store.ledger()
        r = led.rules_at().get(rule_id)
        if r is None or not str(r.meta.get("source", "")).startswith(WISH_SOURCE) or r.meta.get("from") != start:
            return {**self.review(split), "wish_error": "取り消す頼みが見つかりません"}
        led.append("remove", rule_id=rule_id, op_id=f"unwish-{rule_id}-{led.rev + 1}",
                   meta={"kind": "wish_removed", "at": dt.datetime.now().isoformat(timespec="seconds")})
        self.store.save_ledger(led)
        return {**self.review(split), "wish_note": f"取り消しました　{r.name}"}
