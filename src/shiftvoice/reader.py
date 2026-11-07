"""読み取り（8章）。伏せた発言を型に当てはめて JSON で返す。

- 本物は Haiku 5.5（思考なし・effort low・出力800トークンまで）。鍵は環境変数 ANTHROPIC_API_KEY から SDK が読む
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
MAX_TOKENS = 800   # 400 では、8人の勤務を1回で言った一言の返事が途中で切れ、読み取りに失敗した（2026-11-07 の通しの試し）

TYPES = ("day_off", "set_cell", "avoid_cell", "move", "swap", "forbid_sequence", "pair_apart",
         "count_limit", "coverage", "weekday_pref", "consecutive_limit", "rest_after_night", "around_off",
         "leave_count", "weigh", "explain", "custom", "staff_info", "cancel", "cover", "none", "unclear")


class ReadError(Exception):
    """読み取りに失敗した（通信・途中で切れた・形が違う）。"""


class LimitError(ReadError):
    """公開版で、送る前に止めた（回数・費用・終わりの日・名前らしい語）。文はそのまま画面に出す。手元では起きない。
    retry が False の時は、送り直しても同じ（名前らしい語など）なので［送り直す］を出さない。"""

    def __init__(self, message: str, retry: bool = True):
        super().__init__(message)
        self.retry = retry


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
- count_limit 月の回数。staff(並び。全員なら null), shift, min, max(言っていなければ null), until_day(「〇日まで」なら日、なければ null), strength, reason。「夜勤なし」「夜勤0」は max 0。「15日まで夜勤なし」は max 0 と until_day 15（日付の型にしない）。「現場勤務は8日まで」「現場に入るのは月8日」「出勤は8日まで」は、勤務の全部を数える月の日数なので shift は「現場」、max 8、until_day は null（期限ではない）
- weekday_pref 人の曜日の頼み。staff, weekdays(mon〜sun), shift, avoid(避けるなら true), until_month(「12月いっぱい」なら 12。なければ null), reason。その曜日を休みにする頼みは shift 公 と avoid false
- coverage 1日に入る人数（勤務の記号の人数だけ）。shift, min(「2人に」「2人ほしい」「2人以上」なら 2), max(「3人まで」なら 3。ちょうどなら min と同じ。言っていなければ null), weekdays(曜日を言ったら mon〜sun の並び。毎日なら null), holidays(「祝日も」「土日祝」と言った時だけ true), strength, reason
- consecutive_limit 続けて働く日数の上限。staff, shift(「夜勤は続けて2日まで」のように勤務を言った時だけ記号。言っていなければ null), max, except_staff(「ただし職員07さんは当たらない」「職員07さんは除く」の人の並び。なければ null), strength, reason。「夜勤は単発で」「夜勤を続けないで」は shift 夜・max 1。表に無い当番の話ではない。「なるべく2回まで、どうしようもない時は3回まで」は、max 2 の negotiable と max 3 の operational の2件にする。除く人は別の件にせず、両方の件の except_staff に入れる
- staff_info 職員の情報の変更（できる勤務など）。staff, from_month, can_add(記号の並び), can_remove
- leave_count 有給の日数の頼み（日付は任せる）。staff, count(日数。言っていなければ null), reason
- cancel 前の頼みの取り消し（「やっぱりいらない」）。staff, days(取り消す日だけ。日を言っていなければ null), shift(取り消す勤務の記号。休みなら 公)。「そのままで」「触らないで」の日と人は書かない
- cover すでに起きた穴埋めの報告（急な休みなどで足りない所に、人が残業した・残ってくれた・早く来てくれた。「埋めた」と言っていなくてもよい。表は変えずに記録だけ）。staff, day(その日。今日・昨日なら null), how(埋め方の短い言葉。「残業」「早出」など), reason
- swap / pair_apart / rest_after_night / around_off / weigh / explain / custom はその名前の type だけ書き、summary に一言
- none 頼みがない。unclear 読めない時は question に聞き返しの1文

strength は fixed(「絶対」「動かせない」「ハードルール」と言った時と研修など外の予定) / operational(施設の運用の事情。試験まで・人手など。「強めのソフト」) / wish(本人の希望) / negotiable(配慮の言い方。「きつい」「〜してあげて」「ソフトルール」)。強さを言う言葉（ハードルール・ソフトルールなど）は reason にしない
reason は一言にある理由の言葉を短く抜き出す（言い換えない。「29日に試験だから」なら「29日に試験」）。なければ null。
言い直しは後の方だけ。日の言い直し（「14日、あ、違う、15日」）も、人の言い直し（「職員06さんの…じゃない、職員03さん」）も、前の方は件にしない。
言っていない変更は足さない（「あとはできるだけ今のまま」は件にしない）。「くらい」でも数はそのまま。「毎月」「今月だけ」は期間の言葉なので件にしない。
曜日を言った頼み（「木曜は」「土日は」）は日付の型にしない。人の頼みは weekday_pref、勤務の人数の頼みは coverage にする。
表に無い当番や仕組み（待機・呼び出し・オンコールなど、勤務の記号に無い物）を作る一言は、人数を言っていても coverage にせず、ほかの型に分けず custom 1件だけにする。
move の from_day は、元の日を言っていなければ null（手元で表から補う）。
cover は、人が残った・早く来たなど、もう済んだ穴埋めを言った時だけにする。これからの頼み（「〜だと助かる」「〜にして」）は日付の型、お礼だけで人を言っていない一言は none。

例
一言「職員03さん、8日は通院で休み」
{"items":[{"type":"day_off","staff":"職員03","days":[8],"reason":"通院"}]}
一言「夜勤明けの次の日に日勤は入れないで」
{"items":[{"type":"forbid_sequence","staff":null,"first":"明","next":"日","strength":"negotiable","reason":null}]}
一言「職員05さん、遅番は月4回まで」
{"items":[{"type":"count_limit","staff":["職員05"],"shift":"遅","min":null,"max":4,"until_day":null,"strength":"operational","reason":null}]}
一言「職員12の現場勤務は8日まで」
{"items":[{"type":"count_limit","staff":["職員12"],"shift":"現場","min":null,"max":8,"until_day":null,"strength":"operational","reason":null}]}
一言「職員04さん、月曜は遅番にしないで。毎月」
{"items":[{"type":"weekday_pref","staff":"職員04","weekdays":["mon"],"shift":"遅","avoid":true,"until_month":null,"reason":null}]}
一言「職員04さんは日曜日は休みで固定してほしい」
{"items":[{"type":"weekday_pref","staff":"職員04","weekdays":["sun"],"shift":"公","avoid":false,"until_month":null,"reason":null}]}
一言「土日は遅番を2人にしてほしい」
{"items":[{"type":"coverage","shift":"遅","min":2,"max":null,"weekdays":["sat","sun"],"holidays":false,"strength":"operational","reason":null}]}
一言「職員08さんの研修、20日に変わったって」
{"items":[{"type":"move","staff":"職員08","shift":"研","from_day":null,"to_day":20,"reason":null}]}
一言「職員02さんの9日は……じゃない、職員05さんの9日、休みで」
{"items":[{"type":"day_off","staff":"職員05","days":[9],"reason":null}]}
一言「職員07さんの夜勤はできるだけ単発で」
{"items":[{"type":"consecutive_limit","staff":"職員07","shift":"夜","max":1,"strength":"operational","reason":null}]}
一言「早番はなるべく続けて2回まで、どうしようもない時は3回まで。ただし職員09さんは当たらない」
{"items":[{"type":"consecutive_limit","staff":null,"shift":"早","max":2,"except_staff":["職員09"],"strength":"negotiable","reason":null},{"type":"consecutive_limit","staff":null,"shift":"早","max":3,"except_staff":["職員09"],"strength":"operational","reason":null}]}
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

    def __init__(self, replies: dict[str, str], rereads: dict[str, str] | None = None):
        self.replies = replies
        self.rereads = rereads or {}   # 読み直しの録った返事（キーは伏せた後の一言）
        self.sent: list[Sent] = []
        self.reread_sent: list[Sent] = []

    @classmethod
    def from_file(cls, path: Path) -> "RecordedReader":
        """同じ所に rereads.json があれば、読み直しの録った返事も読む。"""
        rr = Path(path).with_name("rereads.json")
        return cls(json.loads(Path(path).read_text(encoding="utf-8")),
                   json.loads(rr.read_text(encoding="utf-8")) if rr.exists() else None)

    def read(self, masked_text: str, history: list[dict] | None = None) -> list[dict]:
        self.sent.append(Sent(SYSTEM, build_messages(masked_text, history)))
        if masked_text not in self.replies:
            raise ReadError("録った返事がない一言")
        return parse_reply(self.replies[masked_text])

    def reread(self, masked_text: str, context: dict, first: list | None = None, why: str | None = None) -> dict:
        self.reread_sent.append(Sent(REREAD_SYSTEM, build_reread_messages(masked_text, context, first, why)))
        if masked_text not in self.rereads:
            raise ReadError("録った読み直しの返事がない一言")
        return parse_reread(self.rereads[masked_text])


@dataclass
class HaikuReader:
    """本物の Haiku 5.5 を呼ぶ。鍵は SDK が環境変数から読む（コードにも画面にも出さない）。"""
    model: str = MODEL
    timeout: float = 15.0
    sent: list[Sent] = field(default_factory=list)
    record_to: Path | None = None   # 渡すと、返事を録る（伏せた一言 → 返事の文）
    reread_record_to: Path | None = None   # 渡すと、読み直しの返事を録る
    reread_timeout: float = 60.0
    reread_sent: list[Sent] = field(default_factory=list)
    last_reread_usage: dict | None = None   # 直前の読み直しのトークン数（費用を測る時に見る）

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

    def reread(self, masked_text: str, context: dict, first: list | None = None, why: str | None = None) -> dict:
        """読み直し（Sonnet 5.5・考えるのは少なめ）。最初の読みが型に当てはまらなかった時だけ session が呼ぶ。"""
        import anthropic

        msgs = build_reread_messages(masked_text, context, first, why)
        self.reread_sent.append(Sent(REREAD_SYSTEM, msgs))
        try:
            client = anthropic.Anthropic(timeout=self.reread_timeout, max_retries=1)
            resp = client.messages.create(
                model=REREAD_MODEL, max_tokens=REREAD_MAX_TOKENS, system=REREAD_SYSTEM, messages=msgs,
                thinking={"type": "adaptive"}, output_config={"effort": "low"},
            )
        except anthropic.APIError as e:
            raise ReadError(f"読み直しを呼べませんでした（{type(e).__name__}）") from e
        self.last_reread_usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
        if resp.stop_reason != "end_turn":
            raise ReadError(f"読み直しの返事が途中で止まりました（{resp.stop_reason}）")
        text = "".join(b.text for b in resp.content if b.type == "text")
        got = parse_reread(text)
        if self.reread_record_to:
            p = Path(self.reread_record_to)
            rec = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            rec[masked_text] = text
            p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        return got


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


# ---- 読み直し（最初の読みが型に当てはまらずに止まった時だけ）
# 最初の読みは伏せた一言だけを見る1回きりの当てはめなので、「この形の頼みは、まだ扱えません」や聞き返しで止まることがある。
# 読み直しは、今のルールの一覧・名簿の番号とユニットと入れる勤務・勤務の記号・表で数えられるルールの種類を添えて、
# Sonnet に同じ形の答え（items）をもう一度作らせる。最初の読みで通る一言には呼ばない（呼ぶかは session が決める）

REREAD_MODEL = "claude-sonnet-5-5"
REREAD_MAX_TOKENS = 6000   # 考える分も入る。足りずに途中で止まった時は ReadError（最初の読みの返事に戻す）
NOT_REREAD = ("custom", "pair_apart", "rest_after_night", "around_off", "weigh", "explain", "none")

RULE_KINDS = """表で数えられるルールの種類（手元の計算が扱える物。上の型を手元でこの形に直す）
- cell 人と日を決めたマス。in（その勤務にする）か not_in（その勤務にしない）。型は day_off・set_cell・avoid_cell・move・swap
- count 人ごとの月の回数。shifts（数える記号の並び。「現場」は勤務の全部）、min・max、日の範囲（1日〜until_day）。型は count_limit・leave_count
- coverage 1日の人数。shifts、min・max、曜日・祝日、施設全体かユニットごと。型は coverage
- max_run 続けて働く日数の上限。shifts（なければ勤務の全部）、max、除く人。型は consecutive_limit
- sequence 並び。ある日の勤務の次の日に置ける・置けない勤務。型は forbid_sequence
- weekday 人の曜日の頼み。その曜日にその勤務にする・しない。型は weekday_pref
- 職員の情報（入れる勤務を足す・外す）は staff_info
- ここに無い物（2人を同じ日に組ませない、休みの前後、表に無い当番など）は作れない"""

REREAD_EXTRA = """

ここからは読み直しの時だけの指示です。上の型と項目の決めはそのまま守ります。
最初の読みでは、この一言を型に当てはめられず、手元で止まりました。最初の読みの答えと、止まった理由を添えます。
今度は、添えた施設の文脈（今のルールの一覧・名簿の番号とユニットと入れる勤務・勤務の記号と名前・表の月）と、下の「表で数えられるルールの種類」を見て、上の型の組み合わせで表せるかを考えてください。

- 返事は JSON だけ。形は {"items": [...]}。使える型は day_off set_cell avoid_cell move swap forbid_sequence count_limit coverage weekday_pref consecutive_limit leave_count staff_info cancel cover unclear。custom pair_apart rest_after_night around_off weigh explain は使わない
- 1回の答えの items は、日付の頼み（day_off set_cell avoid_cell move swap）だけか、それ以外（ルール）だけにする。混ぜると手元で止まる。一言に両方があれば、ルールの方を items にし、日付の頼みは "next" に、作成者が続けて言えばよい一言を1つだけ書く（例 "next": "職員03さん、24日と25日は夜勤にしない"）。日付の頼みだけなら日付の頼みを items にする
- 一言が今のルールの言い直し・置き換えなら、"replace": [置き換える今のルールの id の並び] を書く。新しく足すだけなら書かない
- 勤務の名前の言い換え（「午前の勤務」「現場」など）は、勤務の記号と名前の一覧で当てる。一覧に無い記号は使わない
- 表の月の外の日（11月の表で「年末」など）は件にしない。名簿に無い番号を作らない。一言に無い人・日・数を足さない
- 型で表せない時や、人・日・数が足りない時は、items を [{"type":"unclear","question": 何が足りないかを聞く1文}] にする（例「委員の3人は誰で、委員会は何日ですか」）。聞くのは足りない物だけで、1文にする。名簿の人を指す時は職員03のような番号を書き、「職員NN」のような伏せ字は書かない。作成者には番号が見えないので、「職員の番号を教えて」とは聞かず「誰ですか」と聞く
- 最初の読みが none（頼みが無い）でも、勤務の名前と数・「続けて」「連続」「やめましょう」「〜しないで」がある一言は、たいていルールの頼み。上の型で読む。お礼・相づちだけで本当に頼みが無い時は [{"type":"none"}] のままでよい（ルールを作らない）
- 読みが2通りある時は、作成者に言い直しを求めず、施設のルールの一覧と勤務の並びから一番ありそうな読みを1つ選んで items にし、"how" にどう読んだかを1文で書く（理由は書かない）（例 "how": "遅番を4日続けない（続けて3日まで）と読みました。月の回数のことなら、そう言ってください"）
- 「遅番4回はやめましょう」「早番は4回もいらない」のように、「月」と言わずに勤務と回数を言って「やめる」「いらない」と言う一言は、続けて入る日数（その勤務の連勤）の話と読む。同じ勤務に月4回入るのはふつうで、続けて4回が負担になるため。その勤務が続けて入れない勤務（夜勤の次の日は明け など）の時と、その勤務の月の回数のルールが一覧にある時は、月の回数と読む
- どちらとも本当に決められない時だけ、unclear で2択で聞く。選んだ方をそのまま言えばよい一言を「」で2つ添える（例「遅番を4日続けないようにしますか（「全員、遅番は続けて3日まで」）、月に4回入れないようにしますか（「全員、遅番は月3回まで」）」）
- 人を言っていない回数・連勤などのルールは、施設の全員のルールと読み、その件に "everyone": true を付ける（止まった理由が「誰の頼みか読み取れませんでした（全員のルールなら…）」の時も同じ。作成者に「全員」と言い直させない）。how には、全員と読んだことを書く（例 "how": "全員の遅番を4日続けない（続けて3日まで）と読みました"）。「私」「この人」のように誰かの話なのに誰か分からない時と、一言に人の名前らしい語がある時（敬称が無くても）は、everyone を付けず「誰の話ですか」と聞く

""" + RULE_KINDS


REREAD_SYSTEM = SYSTEM + REREAD_EXTRA


def build_reread_messages(masked_text: str, context: dict, first: list | None, why: str | None) -> list[dict]:
    """読み直しで送る会話（1往復）。context の中身は伏せた後の物だけ（session が作り、名前が無いことを確かめてから渡す）。"""
    parts = ["施設の文脈 " + json.dumps(context, ensure_ascii=False)]
    if first is not None:
        parts.append("最初の読みの答え " + json.dumps({"items": first}, ensure_ascii=False))
    if why:
        parts.append(f"手元で止まった理由「{why}」")
    parts.append(f"一言「{masked_text}」")
    return [{"role": "user", "content": "\n".join(parts)}]


def parse_reread(text: str) -> dict:
    """読み直しの返事。{"items": [...], "replace": [id], "next": 一言, "how": どう読んだかの1文}。型は読み取りと同じ確かめ方。
    考えた後の Sonnet は、JSON の前に説明を書き、JSON を ```json の囲みに入れることがある。囲みがあれば最後の囲みを読む。"""
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fences:
        text = fences[-1]
    items = parse_reply(text)
    if any(it.get("type") in NOT_REREAD for it in items) and not all(it.get("type") == "unclear" for it in items):
        items = [it for it in items if it.get("type") not in NOT_REREAD]
        if not items:
            raise ReadError("読み直しでも型に当てはまらない")
    obj = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    rep = obj.get("replace")
    nxt = obj.get("next")
    how = obj.get("how")
    return {"items": items,
            "replace": [str(x) for x in rep] if isinstance(rep, list) else [],
            "next": nxt if isinstance(nxt, str) and nxt.strip() else None,
            "how": how.strip() if isinstance(how, str) and how.strip() else None}

