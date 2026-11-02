"""読み取り（8章）。伏せた発言を型に当てはめて JSON で返す。

- 本物は Haiku 5.5（思考なし・effort low・出力400トークンまで）。鍵は環境変数 ANTHROPIC_API_KEY から SDK が読む
- 試験は録った返事（RecordedReader）で回す。送る文は名前を伏せた後の文だけ（試験 R9）
- 呼べない・途中で切れた・JSON が読めない時は ReadError。表も帳面も変えず［送り直す］を出す（13章）
- 返事は候補まで。型・番号・数字は手元の intake で確かめてから当てる
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

MODEL = "claude-haiku-5-5"
MAX_TOKENS = 400

TYPES = ("day_off", "set_cell", "avoid_cell", "move", "swap", "forbid_sequence", "pair_apart",
         "count_limit", "weekday_pref", "consecutive_limit", "rest_after_night", "around_off",
         "leave_count", "weigh", "explain", "custom", "staff_info", "cancel", "none", "unclear")


class ReadError(Exception):
    """読み取りに失敗した（通信・途中で切れた・形が違う）。"""


SYSTEM = """あなたは介護施設の勤務表アプリの読み取り係です。作成者の短い一言を、決まった型の JSON にします。
人は「職員NN」、ユニットは「ユニットN」と伏せてあります。番号はそのまま使い、名前を推測しないでください。

勤務の記号: 早=早番 日=日勤 遅=遅番 夜=夜勤 明=明け 公=公休 有=有給 研=研修
返事は JSON だけ。形は {"items": [ ... ]}。一言に頼みが2つあれば2件にします。

型と項目
- day_off 休みの希望。staff, days(日の数の並び), strength(「必ず」なら fixed、「できれば」なら negotiable、ほかは wish), reason。強さの違う日は別の件にする
- set_cell その日をその勤務に。staff, days, shift, strength, reason
- avoid_cell その日はその勤務にしない。staff, days, shift, reason
- move 前に決まったマスを別の日へ移す。staff, shift, from_day, to_day, reason
- forbid_sequence 並びの禁止（AのつぎのひにB）。staff(並び。全員なら null), first, next(記号。Bが2つ以上なら並び), strength, reason
- count_limit 月の回数。staff(並び。全員なら null), shift, min, max(言っていなければ null), until_day(「〇日まで」なら日、なければ null), strength, reason
- weekday_pref 曜日の頼み。staff, weekdays(mon〜sun), shift, avoid(避けるなら true), until_month(「12月いっぱい」なら 12。なければ null), reason
- consecutive_limit 続けて働く日数の上限。staff, max, reason
- staff_info 職員の情報の変更（できる勤務など）。staff, from_month, can_add(記号の並び), can_remove
- leave_count 有給の日数の頼み（日付は任せる）。staff, count(日数。言っていなければ null), reason
- cancel 前の頼みの取り消し（「やっぱりいらない」）。staff, days(取り消す日だけ), shift(取り消す勤務の記号。休みなら 公)。「そのままで」「触らないで」の日と人は書かない
- swap / pair_apart / rest_after_night / around_off / weigh / explain / custom はその名前の type だけ書き、summary に一言
- none 頼みがない。unclear 読めない時は question に聞き返しの1文

strength は fixed(「絶対」「動かせない」と言った時と研修など外の予定) / operational(施設の運用の事情。試験まで・人手など) / wish(本人の希望) / negotiable(配慮の言い方。「きつい」「〜してあげて」)
reason は一言にある理由の言葉を短く抜き出す（言い換えない。「29日に試験だから」なら「29日に試験」）。なければ null。
言い直しは後の方だけ。日の言い直し（「14日、あ、違う、15日」）も、人の言い直し（「職員06さんの…じゃない、職員03さん」）も、前の方は件にしない。
言っていない変更は足さない（「あとはできるだけ今のまま」は件にしない）。「くらい」でも数はそのまま。「毎月」「今月だけ」は期間の言葉なので件にしない。
曜日を言った頼み（「木曜は」「土日は」）は日付の型にせず weekday_pref にする。
表に無い当番や仕組み（待機・呼び出し・オンコールなど、勤務の記号に無い物）を作る一言は、ほかの型に分けず custom 1件だけにする。
move の from_day は、元の日を言っていなければ null（手元で表から補う）。

例
一言「職員03さん、8日は通院で休み」
{"items":[{"type":"day_off","staff":"職員03","days":[8],"reason":"通院"}]}
一言「夜勤明けの次の日に日勤は入れないで」
{"items":[{"type":"forbid_sequence","staff":null,"first":"明","next":"日","strength":"negotiable","reason":null}]}
一言「職員05さん、遅番は月4回まで」
{"items":[{"type":"count_limit","staff":["職員05"],"shift":"遅","min":null,"max":4,"until_day":null,"strength":"operational","reason":null}]}
一言「職員04さん、月曜は遅番にしないで。毎月」
{"items":[{"type":"weekday_pref","staff":"職員04","weekdays":["mon"],"shift":"遅","avoid":true,"until_month":null,"reason":null}]}
一言「職員08さんの研修、20日に変わったって」
{"items":[{"type":"move","staff":"職員08","shift":"研","from_day":null,"to_day":20,"reason":null}]}
一言「職員02さんの9日は……じゃない、職員05さんの9日、休みで」
{"items":[{"type":"day_off","staff":"職員05","days":[9],"reason":null}]}
一言「平日の夜は、休みの人から呼び出し当番を2人決めて。電話はその2人にだけ」
{"items":[{"type":"custom","summary":"平日の夜の呼び出し当番"}]}
"""


def build_messages(masked_text: str, history: list[dict] | None = None) -> list[dict]:
    """送る会話。履歴は JSON の文字列で渡す（10章。Python の書き方で渡すと全部読めなくなった）。"""
    msgs = []
    for h in history or []:
        msgs.append({"role": h["role"], "content": h["content"] if isinstance(h["content"], str)
                     else json.dumps(h["content"], ensure_ascii=False)})
    msgs.append({"role": "user", "content": f"一言「{masked_text}」"})
    return msgs


def parse_reply(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ReadError("返事に JSON がない")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ReadError(f"返事の JSON が読めない {e}") from e
    items = obj.get("items")
    if not isinstance(items, list):
        raise ReadError("items がない")
    for it in items:
        if not isinstance(it, dict) or it.get("type") not in TYPES:
            raise ReadError(f"知らない型 {it}")
    return items


@dataclass
class Sent:
    """外へ送った物の控え（試験 R9 で中身を確かめる）。"""
    system: str
    messages: list[dict]


class RecordedReader:
    """録った返事で答える。キーは伏せた後の一言。無い一言は ReadError（呼べない時と同じ扱い）。"""

    def __init__(self, replies: dict[str, str]):
        self.replies = replies
        self.sent: list[Sent] = []

    @classmethod
    def from_file(cls, path: Path) -> "RecordedReader":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def read(self, masked_text: str, history: list[dict] | None = None) -> list[dict]:
        self.sent.append(Sent(SYSTEM, build_messages(masked_text, history)))
        if masked_text not in self.replies:
            raise ReadError("録った返事がない一言")
        return parse_reply(self.replies[masked_text])


@dataclass
class HaikuReader:
    """本物の Haiku 5.5 を呼ぶ。鍵は SDK が環境変数から読む（コードにも画面にも出さない）。"""
    model: str = MODEL
    timeout: float = 15.0
    sent: list[Sent] = field(default_factory=list)
    record_to: Path | None = None   # 渡すと、返事を録る（伏せた一言 → 返事の文）

    def read(self, masked_text: str, history: list[dict] | None = None) -> list[dict]:
        import anthropic

        msgs = build_messages(masked_text, history)
        self.sent.append(Sent(SYSTEM, msgs))
        try:
            client = anthropic.Anthropic(timeout=self.timeout, max_retries=1)
            resp = client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS, system=SYSTEM, messages=msgs,
                thinking={"type": "disabled"}, output_config={"effort": "low"},
            )
        except anthropic.APIError as e:
            raise ReadError(f"読み取りを呼べませんでした（{type(e).__name__}）") from e
        if resp.stop_reason != "end_turn":
            raise ReadError(f"返事が途中で止まりました（{resp.stop_reason}）")
        text = "".join(b.text for b in resp.content if b.type == "text")
        items = parse_reply(text)
        if self.record_to:
            p = Path(self.record_to)
            rec = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            rec[masked_text] = text
            p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        return items


BUILD_SYSTEM = """あなたは介護施設の勤務表アプリの組み立て係です。作成者の一言から、表に無い当番（待機など）の定義を JSON で作ります。
人は「職員NN」と伏せてあります。返事は JSON だけ。

形
{"layer": {"name": 当番の名前, "days": {"weekdays": [mon〜sun], "holidays": 祝日も入れるなら true},
  "per_day": 1日の人数, "own_shift_on_duty_day": {"in": [当番の日の本人の勤務の記号], "prev_not_in": [前の日に置けない記号]},
  "on_absence": {"call": "duty_only"（急な休みの電話は当番の人にだけ）か "all"},
  "counters": [{"id": "assigned"}, {"id": "activated", "carry_to_next_month": 引き受けた回数を翌月も覚えるなら true}]}}
読めない時は {"question": 聞き返しの1文}

勤務の記号: 早 日 遅 夜 明 公 有 研。「休みの人から」は in に 公、前の日が夜勤・明けの人は外す（prev_not_in に 夜 と 明）。
言っていないことは足さない。

例
一言「毎月、平日の夜は休みの人から呼び出し当番を2人。電話はその2人だけ」
{"layer": {"name": "呼び出し当番", "days": {"weekdays": ["mon","tue","wed","thu","fri"], "holidays": false}, "per_day": 2, "own_shift_on_duty_day": {"in": ["公"], "prev_not_in": ["夜","明"]}, "on_absence": {"call": "duty_only"}, "counters": [{"id": "assigned"}]}}
"""


def parse_build(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ReadError("組み立ての返事に JSON がない")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ReadError(f"組み立ての JSON が読めない {e}") from e
    if not isinstance(obj, dict) or not ("layer" in obj or "question" in obj):
        raise ReadError("組み立ての返事の形が違う")
    return obj


class RecordedBuilder:
    """録った（用意した）組み立ての返事。キーは伏せた後の一言。"""

    def __init__(self, replies: dict[str, str]):
        self.replies = replies
        self.sent: list[Sent] = []

    @classmethod
    def from_file(cls, path: Path) -> "RecordedBuilder":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def build(self, masked_text: str) -> dict:
        self.sent.append(Sent(BUILD_SYSTEM, build_messages(masked_text)))
        if masked_text not in self.replies:
            raise ReadError("録った組み立ての返事がない一言")
        return parse_build(self.replies[masked_text])


@dataclass
class SonnetBuilder:
    """組み立て役（Sonnet 5.5・effort low・1500トークンまで。8章）。"""
    model: str = "claude-sonnet-5-5"
    timeout: float = 30.0
    sent: list[Sent] = field(default_factory=list)

    def build(self, masked_text: str) -> dict:
        import anthropic

        msgs = build_messages(masked_text)
        self.sent.append(Sent(BUILD_SYSTEM, msgs))
        try:
            client = anthropic.Anthropic(timeout=self.timeout, max_retries=1)
            resp = client.messages.create(
                model=self.model, max_tokens=1500, system=BUILD_SYSTEM, messages=msgs,
                thinking={"type": "between_tools"}, output_config={"effort": "low"},
            )
        except anthropic.APIError as e:
            raise ReadError(f"組み立てを呼べませんでした（{type(e).__name__}）") from e
        if resp.stop_reason != "end_turn":
            raise ReadError(f"組み立ての返事が途中で止まりました（{resp.stop_reason}）")
        return parse_build("".join(b.text for b in resp.content if b.type == "text"))
