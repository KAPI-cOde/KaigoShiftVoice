"""公開版（Hugging Face Spaces など。環境変数 SHIFTVOICE_PUBLIC=1 の時だけ使う）。

手元の動き（127.0.0.1 だけ・data/runtime の置き場1つ）は変えない。公開版でだけ次のことをする。
- 人ごとに置き場とセッションを分ける。初めて来た人に推測できない長い ID のクッキーを渡し、その人だけの
  ひだまりの郷の写しを、人ごとに別の親フォルダの下に作る（ほかの人の置き場はフォルダの一覧にも出ない）。
  ID はサーバーが発行した物だけを受け、フォルダの名前には ID そのものでなく ID の要約（sha256 の頭）を使う
- 残す口だけを並べ、ほかの口（施設の一覧・切り替え・取り込み・声 など）は 403 で断る
- Claude を呼ぶ全部の口（一言・送り直し・名前を選んだ後・翌月のルールの変え方・組み立て）を、読み手と
  組み立て役の包み（GatedReader・GatedBuilder）で1か所の見張り（Gate）に通す。人ごとと全体の回数・
  呼ぶ前に出力と送り直しまで含めた最大の費用を帳簿から引く・公開の終わりの日
- 名簿に無い名前らしい語・ひらがなの名前に付いた「さん」・電話番号らしい数字を含む一言は送らない
- 配った後の理由の読み分け（aging.py）は、公開版では呼ばない（裏で Claude を呼ぶ口を残さない）
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .reader import BUILD_SYSTEM, MAX_TOKENS, SYSTEM, LimitError

JST = dt.timezone(dt.timedelta(hours=9))
SEED = Path(__file__).resolve().parents[2] / "data" / "hidamari"
COOKIE = "sv_id"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")

# 価格（1M トークンあたりのドル。2026-11 の Anthropic の表。環境変数で変えられる）
PRICE = {"read": (0.10, 0.50), "build": (2.00, 10.00)}
BUILD_MAX_TOKENS = 1500

MSG_USER_LIMIT = "試せる回数を超えました。しばらくしてから試してください（表もルールも変えていません）"
MSG_BUSY = "いま混み合っています。1分ほどしてから試してください（表もルールも変えていません）"
MSG_BUDGET = "公開版で試せる量を使い切りました。手元で動かす手順は README にあります（表もルールも変えていません）"
MSG_LEDGER = "公開版の帳簿が読めないので、Claude へは送っていません。手元で動かす手順は README にあります"
MSG_NAME = "名簿に無い名前のような言葉（{w}）があるので、Claude へは送っていません。架空の施設の名簿の名前で試してください（表もルールも変えていません）"
MSG_PRIVATE = "電話番号やメールのような文字があるので、Claude へは送っていません。架空の施設の言葉で試してください"
MSG_HEALTH = "病気やけがのこと（{w}）が書かれているので、Claude へは送っていません。理由は「通院」「私用」などで試してください（表もルールも変えていません）"


def closed_message(until: dt.date) -> str:
    return f"公開は{until.month}月{until.day}日で終わりました。手元で動かす手順は README にあります"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class PublicConfig:
    root: Path                       # 人ごとの置き場を作る親（一時フォルダ）
    until: dt.date = dt.date(2026, 11, 13)   # この日（日本時間）の終わりまで開く
    ledger_dir: Path | None = None   # 費用の帳簿の置き場
    ledger_persistent: bool = False  # 再起動しても帳簿が残る置き場か（Spaces の永続の置き場 /data など）
    total_budget_usd: float = 80.0   # 帳簿の通算の上限（帳簿が残る時だけ意味がある）
    boot_budget_usd: float = 10.0    # 起動ごとの上限（帳簿が消えても、1回の起動で使える量を小さくする）
    user_reads_per_hour: int = 30
    user_builds_per_hour: int = 5
    global_calls_per_minute: int = 60
    global_builds_per_hour: int = 30
    user_posts_per_hour: int = 400   # Claude を呼ばない操作も含めた、人ごとの送信の回数
    user_tables_per_hour: int = 20   # 翌月の案を作る（表の計算）回数
    new_users_per_hour: int = 600
    new_users_per_minute: int = 20   # 新しい置き場を作る速さ（クッキーを捨てて取り直す人が枠を一気に埋めないように）
    max_users: int = 1000            # 置き場1つはひだまりの郷の写し（数百KB）
    evict_idle_seconds: int = 120    # 満ちた時は、これだけ使われていない置き場から片付けて新しい人を受ける
    idle_seconds: int = 7200
    max_text: int = 200
    max_body: int = 16384
    workers: int = 4                 # 同時に動かす重い処理（Claude・表の計算）の数
    host: str | None = None          # 受ける Host（無ければ Origin と Host が同じ時だけ受ける）
    seed: Path = SEED
    base_date: str = "2026-11-30"    # 引き受けを数える基準の日（見本の11月末。画面と12月の返す案で同じ数にする）

    @classmethod
    def from_env(cls) -> "PublicConfig":
        until = dt.date.fromisoformat(os.environ.get("SHIFTVOICE_PUBLIC_UNTIL", "2026-11-13"))
        root = Path(os.environ.get("SHIFTVOICE_PUBLIC_ROOT") or tempfile.mkdtemp(prefix="shiftvoice-public-"))
        led = os.environ.get("SHIFTVOICE_PUBLIC_LEDGER_DIR")
        if led:
            ledger_dir, persistent = Path(led), True
        elif Path("/data").is_dir() and os.access("/data", os.W_OK):
            ledger_dir, persistent = Path("/data/shiftvoice"), True   # Spaces の永続の置き場
        else:
            ledger_dir, persistent = root / ".ledger", False
        return cls(
            root=root, until=until, ledger_dir=ledger_dir, ledger_persistent=persistent,
            total_budget_usd=_env_float("SHIFTVOICE_PUBLIC_BUDGET_USD", 80.0),
            boot_budget_usd=_env_float("SHIFTVOICE_PUBLIC_BOOT_BUDGET_USD", 10.0),
            user_reads_per_hour=_env_int("SHIFTVOICE_PUBLIC_USER_READS_PER_HOUR", 30),
            user_builds_per_hour=_env_int("SHIFTVOICE_PUBLIC_USER_BUILDS_PER_HOUR", 5),
            global_calls_per_minute=_env_int("SHIFTVOICE_PUBLIC_CALLS_PER_MINUTE", 60),
            max_users=_env_int("SHIFTVOICE_PUBLIC_MAX_USERS", 1000),
            base_date=os.environ.get("SHIFTVOICE_PUBLIC_BASE_DATE", "2026-11-30"),
            host=os.environ.get("SHIFTVOICE_PUBLIC_HOST") or None,
        )


# ---- 名前らしい語の守り（名簿の名前は番号に伏せた後の文で見る）

# 人の名前によく使われる字（名字と名前）。この字を含む2字以上の漢字の並びで、下の言葉に無い物は送らない。
# 名前の一覧ではなく字の一覧（名前そのものは書かない）
NAME_CHARS = set("田藤佐鈴橋渡伊辺吉松井林森池岡崎沢澤島嶋谷浦坂野原宮村川山江河菅柳杉桜梅竹菊堀斎斉齋"
                 "郎美恵奈香彦輔翔樹哉也代枝陽菜葵蓮翼聡浩隆裕博誠智健拓悠優真結愛彩舞莉咲")
# その字を含んでも名前でない言葉（仕事・暮らしの言葉）
NOT_NAME_WORDS = {
    "原因", "原則", "野外", "分野", "介護", "美容院", "美容", "宮参", "山場", "川沿", "田舎", "松葉杖",
    "代休", "代わり", "交代", "代理", "時代", "世代", "年代", "代表", "代行", "代金",
    "優先", "優遇", "真夜中", "真ん中", "結婚", "結婚式", "結果", "愛情", "健康", "健診", "健康診断", "誠実",
    "悠長", "舞台", "坂道", "谷間", "林間", "森林", "沢山", "江戸", "梅雨", "陽性", "陽気", "太陽", "菜園",
    "真冬", "真夏", "結局", "愛想", "健常", "誠意", "香典", "奈良", "伊勢", "郵便", "村役場", "河川",
}
# ひらがなに「さん」が付いても名前でない言葉
KANA_SAN_OK = {"たく", "みな", "おばあ", "おじい", "おかあ", "おとう", "おく", "おにい", "おねえ", "おじ", "おば",
               "ばあ", "じい", "かあ", "とう", "にい", "ねえ", "おこ", "おまご", "おくさ", "あか", "おさ", "おいしゃ"}
_KANJI_RUN = re.compile(r"[一-龥々]+")
_PHONE = re.compile(r"[0-9０-９](?:[\s\-－ー‐()（）.]*[0-9０-９]){8,}")   # 区切り（空白・ハイフン・括弧）を挟んでも数字9字以上
_LATIN = re.compile(r"[A-Za-zＡ-Ｚａ-ｚ]{2,}")
LATIN_OK = {"OK", "ok", "Ok", "AM", "PM", "am", "pm"}
# 文の頭や読点の後で、ひらがなの語に助詞が続く所（「はるかは」）。名前でないよく使う語だけを通す
_KANA_HEAD = re.compile(r"(?:^|[、。,，.\s　「（(])([ぁ-ゖー]{2,6}?)(?:は|が|も|と|の|を|に|って)")
KANA_HEAD_OK = {
    "あした", "あす", "あさって", "きょう", "きのう", "おととい", "みんな", "みなさん", "それ", "これ", "あれ", "どれ",
    "そこ", "ここ", "ぜんぶ", "ほか", "わたし", "わたくし", "ぼく", "おれ", "うち", "いつも", "まいつき", "こんげつ",
    "らいげつ", "せんげつ", "らいしゅう", "こんしゅう", "つぎ", "まえ", "あと", "よる", "あさ", "ひる", "ゆうがた",
    "やすみ", "なるべく", "できれば", "ぜったい", "とりあえず", "やっぱり", "いちおう", "また", "まだ", "もう",
    "ちょっと", "すこし", "いま", "こんど", "さいご", "さいしょ", "ふたり", "ひとり", "だれ", "だれか", "どちら",
    "どっち", "こっち", "そっち", "あっち", "おなじ", "べつ", "ほんと", "ほんとう", "たぶん", "いつ", "なに", "なん",
    "でも", "だから", "あと", "それから", "ただ", "あの", "その", "この", "どの", "えっと", "えーと", "あー", "あ",
    "おねがい", "すみません", "ごめん", "よろしく", "しばらく", "ずっと", "ふだん", "まいにち", "まいしゅう",
    "ひだまり", "どようび", "にちようび", "げつようび", "かようび", "すいようび", "もくようび", "きんようび", "どにち", "へいじつ",
}
# 病気・けが・体のことの言葉（個人の事情として外へ送らない）。「通院」「病院」は理由の言い方として通す
HEALTH_WORDS = ("病気", "糖尿", "うつ", "鬱", "がん", "癌", "腫瘍", "手術", "入院", "骨折", "妊娠", "流産", "出産",
                "障害", "障がい", "認知症", "精神", "心療", "透析", "抗がん", "感染", "コロナ", "インフル", "けが",
                "怪我", "腰痛", "ヘルニア", "持病", "治療", "服薬", "薬", "診断", "症", "病")
HEALTH_OK = ("病院", "通院")
_MAIL = re.compile(r"[A-Za-z0-9._%+-]+[@＠][A-Za-z0-9.-]+")


def _strip_known(run: str) -> list[str]:
    """漢字の並びから名前でない言葉を（長い順に）抜いた残りの切れ端。"""
    parts = [run]
    for w in sorted(NOT_NAME_WORDS, key=len, reverse=True):
        nxt: list[str] = []
        for p in parts:
            nxt.extend(x for x in p.split(w) if x)
        parts = nxt
    return parts


def name_like(masked: str) -> str | None:
    """送る前の文（名簿の名前は 職員NN に伏せてある）に、名簿に無い名前らしい語があればその語を返す。"""
    t = re.sub(r"職員\d{2}", "　", masked)
    t = re.sub(r"ユニット\d+", "　", t)
    for mm in _KANJI_RUN.finditer(t):
        for frag in _strip_known(mm.group(0)):
            if len(frag) >= 2 and any(c in NAME_CHARS for c in frag):
                return frag
    for mm in re.finditer(r"([ぁ-ゖー]{1,8})(?:さん|さま|ちゃん|くん)", t):
        run = mm.group(1)
        if run in KANA_SAN_OK:
            continue
        # 前に付いた助詞（「、はなこさん」の前の「は」など）を外しても名前でない言葉なら通す
        if any(run[i:] in KANA_SAN_OK for i in range(1, len(run))):
            continue
        return run + "さん"
    for mm in re.finditer(r"([ァ-ヶー]{2,})(?:さん|さま|ちゃん|くん)", t):
        return mm.group(0)
    return None


def private_like(masked: str) -> bool:
    return bool(_PHONE.search(masked) or _MAIL.search(masked))


def latin_like(masked: str) -> str | None:
    """英字の名前（敬称なし）。英字2字以上の並びは、決まった言葉のほかは送らない。"""
    for mm in _LATIN.finditer(masked):
        if mm.group(0) not in LATIN_OK:
            return mm.group(0)
    return None


def kana_name_like(masked: str) -> str | None:
    """文の頭・読点の後に、ひらがなの語＋助詞（「はるかは」）がある時、その語がよく使う語でなければ返す。"""
    t = re.sub(r"職員\d{2}(?:さん)?", "、", masked)
    for mm in _KANA_HEAD.finditer(t):
        w = mm.group(1)
        if w not in KANA_HEAD_OK:
            return w
    return None


def health_like(masked: str) -> str | None:
    t = masked
    for ok in HEALTH_OK:
        t = t.replace(ok, "　")
    for w in HEALTH_WORDS:
        if w in t:
            return w
    return None


def check_text(masked: str) -> None:
    """Claude へ送ってよい文かを確かめる。だめなら LimitError（表もルールも変えない）。"""
    w = name_like(masked) or latin_like(masked) or kana_name_like(masked)
    if w:
        raise LimitError(MSG_NAME.format(w=w), retry=False)
    h = health_like(masked)
    if h:
        raise LimitError(MSG_HEALTH.format(w=h), retry=False)
    if private_like(masked):
        raise LimitError(MSG_PRIVATE, retry=False)


# ---- 回数と費用の見張り（1か所）

def worst_cost(kind: str, masked: str, history: list | None = None) -> float:
    """呼ぶ前に見積もる最大の費用（ドル）。入力は字の数をトークンの数とみなし（日本語は多めに出る側）、
    出力は上限まで、SDK の送り直し（max_retries=1）で2回分。実際の使用量は数えず、この見積もりを帳簿から引く。"""
    pin, pout = PRICE[kind]
    pin = _env_float(f"SHIFTVOICE_PRICE_{kind.upper()}_IN", pin)
    pout = _env_float(f"SHIFTVOICE_PRICE_{kind.upper()}_OUT", pout)
    system = SYSTEM if kind == "read" else BUILD_SYSTEM
    hist = sum(len(json.dumps(h, ensure_ascii=False)) for h in (history or []))
    tokens_in = len(system) + len(masked) + hist + 200
    tokens_out = MAX_TOKENS if kind == "read" else BUILD_MAX_TOKENS
    return 2 * (tokens_in * pin + tokens_out * pout) / 1_000_000


class Gate:
    """Claude を呼ぶ前に必ず通す見張り。回数・費用・終わりの日を、1つのロックの中で確かめて引く。"""

    def __init__(self, cfg: PublicConfig, now=None):
        self.cfg = cfg
        self.now = now or (lambda: dt.datetime.now(JST))
        self.lock = threading.Lock()
        self.user_hits: dict[tuple[str, str], deque] = {}
        self.global_minute: deque = deque()
        self.global_builds: deque = deque()
        self.boot_spent = 0.0
        self.broken = False
        self.calls = 0
        self.ledger_path = (cfg.ledger_dir / "spend.json") if cfg.ledger_dir else None
        self.total_spent = 0.0
        if self.ledger_path:
            try:
                self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
                if self.ledger_path.exists():
                    d = json.loads(self.ledger_path.read_text(encoding="utf-8"))
                    self.total_spent = float(d["spent_usd"])
            except (OSError, ValueError, KeyError, TypeError):
                self.broken = True   # 読めない帳簿は、呼ばない側に倒す

    def closed(self) -> bool:
        return self.now().astimezone(JST).date() > self.cfg.until

    def _hits(self, key: tuple[str, str], window: float, t: float) -> deque:
        q = self.user_hits.setdefault(key, deque())
        while q and q[0] <= t - window:
            q.popleft()
        return q

    def count(self, user: str, kind: str, per_hour: int) -> None:
        """Claude を呼ばない操作の回数（表の計算など）。超えたら LimitError。"""
        t = time.monotonic()
        with self.lock:
            q = self._hits((user, kind), 3600, t)
            if len(q) >= per_hour:
                raise LimitError(MSG_USER_LIMIT)
            q.append(t)

    def spend(self, user: str, kind: str, cost: float) -> None:
        """呼ぶ前に、回数と費用を確かめて引く。どれかを超えたら LimitError（呼ばない）。"""
        cfg = self.cfg
        if self.closed():
            raise LimitError(closed_message(cfg.until))
        t = time.monotonic()
        with self.lock:
            if self.broken:
                raise LimitError(MSG_LEDGER)
            per_hour = cfg.user_reads_per_hour if kind == "read" else cfg.user_builds_per_hour
            q = self._hits((user, kind), 3600, t)
            if len(q) >= per_hour:
                raise LimitError(MSG_USER_LIMIT)
            while self.global_minute and self.global_minute[0] <= t - 60:
                self.global_minute.popleft()
            if len(self.global_minute) >= cfg.global_calls_per_minute:
                raise LimitError(MSG_BUSY)
            if kind == "build":
                while self.global_builds and self.global_builds[0] <= t - 3600:
                    self.global_builds.popleft()
                if len(self.global_builds) >= cfg.global_builds_per_hour:
                    raise LimitError(MSG_BUSY)
            if self.boot_spent + cost > cfg.boot_budget_usd or self.total_spent + cost > cfg.total_budget_usd:
                raise LimitError(MSG_BUDGET)
            if self.ledger_path:
                try:
                    tmp = self.ledger_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"spent_usd": round(self.total_spent + cost, 6),
                                               "calls": self.calls + 1}), encoding="utf-8")
                    os.replace(tmp, self.ledger_path)
                except OSError:
                    self.broken = True
                    raise LimitError(MSG_LEDGER)
            self.total_spent += cost
            self.boot_spent += cost
            self.calls += 1
            q.append(t)
            self.global_minute.append(t)
            if kind == "build":
                self.global_builds.append(t)


class GatedReader:
    """読み手の包み。送る文を確かめ、見張りで引いてから中の読み手を呼ぶ。"""

    def __init__(self, inner, gate: Gate, user: str):
        self.inner, self.gate, self.user = inner, gate, user
        self.calls_claude = getattr(inner, "calls_claude", True)

    @property
    def sent(self):
        return self.inner.sent

    def read(self, masked_text: str, history: list | None = None):
        if self.calls_claude:
            check_text(masked_text)
            self.gate.spend(self.user, "read", worst_cost("read", masked_text, history))
        return self.inner.read(masked_text, history)


class GatedBuilder:
    def __init__(self, inner, gate: Gate, user: str):
        self.inner, self.gate, self.user = inner, gate, user

    @property
    def sent(self):
        return self.inner.sent

    def build(self, masked_text: str):
        check_text(masked_text)
        self.gate.spend(self.user, "build", worst_cost("build", masked_text))
        return self.inner.build(masked_text)


# ---- 人ごとの置き場

@dataclass
class Workspace:
    token: str
    root: Path
    session: object
    lock: threading.RLock = field(default_factory=threading.RLock)   # 入口で先に取り、同じ道筋の中でもう一度取る
    last: float = field(default_factory=time.monotonic)
    busy: int = 0


class Full(Exception):
    """新しい人の置き場を作れない（人数の上限・作る回数の上限）。"""


class Workspaces:
    def __init__(self, cfg: PublicConfig, make_session):
        self.cfg = cfg
        self.make_session = make_session   # (store, user) -> Session
        self.lock = threading.Lock()
        self.items: dict[str, Workspace] = {}
        self.minted: deque = deque()
        self.swept = 0.0

    @staticmethod
    def dirname(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()[:24]

    def _drop(self, tok: str) -> None:
        ws = self.items.pop(tok)
        shutil.rmtree(ws.root.parent, ignore_errors=True)

    def _sweep(self, t: float) -> None:
        """最後に使ってから時間のたった置き場を消す。処理中（busy）の置き場は消さない。ID は使い直さない。"""
        for tok, ws in list(self.items.items()):
            if ws.busy == 0 and t - ws.last > self.cfg.idle_seconds:
                self._drop(tok)
        self.swept = t

    def create(self) -> Workspace:
        from .store import Store
        t = time.monotonic()
        with self.lock:
            self._sweep(t)
            while self.minted and self.minted[0] <= t - 3600:
                self.minted.popleft()
            if (len(self.minted) >= self.cfg.new_users_per_hour
                    or sum(1 for x in self.minted if x > t - 60) >= self.cfg.new_users_per_minute):
                raise Full()
            if len(self.items) >= self.cfg.max_users:
                # 満ちた時は、しばらく使われていない置き場の古い順に片付ける（処理中・いま使っている人は残す）。
                # クッキーを捨てて取り直す人が枠を埋めても、新しい人が締め出されないように
                idle = sorted((ws.last, tok) for tok, ws in self.items.items()
                              if ws.busy == 0 and t - ws.last >= self.cfg.evict_idle_seconds)
                if not idle:
                    raise Full()
                self._drop(idle[0][1])
            token = secrets.token_urlsafe(32)
            parent = self.cfg.root / "users" / self.dirname(token)
            store = Store.from_seed(self.cfg.seed, parent / "hidamari")
            ws = Workspace(token, store.root, None)
            ws.session = self.make_session(store, token)
            ws.session.open()
            ws.busy = 1
            self.items[token] = ws
            self.minted.append(t)
            return ws

    def acquire(self, token: str | None) -> Workspace | None:
        """サーバーが発行した ID の置き場だけを返す（busy を1つ上げる。終わったら release）。"""
        if not token or not _TOKEN.fullmatch(token):
            return None
        with self.lock:
            t = time.monotonic()
            if t - self.swept > 60:
                self._sweep(t)   # 新しい人が来なくても、時間のたった置き場を消す
            ws = self.items.get(token)
            if ws is None:
                return None
            if ws.busy == 0 and t - ws.last > self.cfg.idle_seconds:
                self._drop(token)   # 時間のたった置き場は、使いに来ても戻さない
                return None
            ws.busy += 1
            ws.last = time.monotonic()
            return ws

    def release(self, ws: Workspace) -> None:
        with self.lock:
            ws.busy -= 1
            ws.last = time.monotonic()


# ---- 公開版の入れ物

GET_OK = {"/", "/index.html", "/api/state", "/api/table.tsv", "/api/cleanup", "/api/next/review",
          "/api/next/review?split=1"}
GET_PREFIX_OK = ("/api/rule?", "/api/why?")
POST_OK = {"/api/say", "/api/answer", "/api/retry", "/api/choose_name", "/api/accept", "/api/move", "/api/refuse",
           "/api/cancel", "/api/undo", "/api/cleanup", "/api/restore", "/api/wider", "/api/distribute",
           "/api/next/answer", "/api/next/change", "/api/next/undo", "/api/next/build", "/api/next/adopt"}
TEXT_FIELDS = ("text", "wrong", "right", "answer", "uid", "rule_id", "plan")


def allowed(method: str, path: str) -> bool:
    if method == "GET":
        return path in GET_OK or path.startswith(GET_PREFIX_OK)
    return path in POST_OK


@dataclass
class PublicApp:
    cfg: PublicConfig
    gate: Gate
    spaces: Workspaces
    has_key: bool
    slots: threading.BoundedSemaphore

    def info(self) -> dict:
        """画面へ渡す公開版の印。"""
        closed = self.gate.closed()
        return {"until": self.cfg.until.isoformat(), "closed": closed,
                "closed_message": closed_message(self.cfg.until) if closed else "",
                "max_text": self.cfg.max_text, "reads_per_hour": self.cfg.user_reads_per_hour}


def make_app(cfg: PublicConfig, *, reader_factory=None, builder_factory=None, has_key: bool | None = None,
             gate: Gate | None = None) -> PublicApp:
    """reader_factory・builder_factory は、人ごとの中の読み手を作る（試験では数える偽物を渡す）。"""
    from .reader import HaikuReader, SonnetBuilder
    from .server import NoKeyReader
    from .session import Session
    if has_key is None:
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if reader_factory is None:
        reader_factory = (lambda: HaikuReader()) if has_key else (lambda: NoKeyReader())
    if builder_factory is None:
        builder_factory = (lambda: SonnetBuilder()) if has_key else (lambda: None)
    gate = gate or Gate(cfg)

    def make_session(store, user):
        b = builder_factory()
        return Session(store, 2026, 11, GatedReader(reader_factory(), gate, user),
                       builder=GatedBuilder(b, gate, user) if b is not None else None, base_date=cfg.base_date)

    return PublicApp(cfg, gate, Workspaces(cfg, make_session), has_key, threading.BoundedSemaphore(cfg.workers))


def serve_public(port: int | None = None) -> None:
    """公開版で開く（0.0.0.0。番号は PORT か 7860）。"""
    from http.server import ThreadingHTTPServer
    from .server import make_handler
    cfg = PublicConfig.from_env()
    app = make_app(cfg)
    handler = make_handler(None, threading.Lock(), app.has_key, None, public=app)
    port = port or _env_int("PORT", 7860)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    keep = "残る置き場" if cfg.ledger_persistent else "再起動で消える置き場（通算の上限は Console の上限に任せる）"
    print(f"公開版で開きました 番号 {port}。{cfg.until.isoformat()} まで。帳簿は{keep}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
