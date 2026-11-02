"""本物の Claude で返事を録り、用意した返事（tests/fixtures）と比べる。鍵が要る（.env か環境変数）。

uv run python tools/record_replies.py [録った返事の置き場.json]

- 送るのは名前を伏せた一言だけ（用意した返事のキーそのもの）
- 型・人・日・勤務・回数・期限の食い違いを1行ずつ出す。食い違いは、用意した返事を直す前にメインが読んで決める
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shiftvoice.reader import HaikuReader, ReadError, SonnetBuilder, parse_build, parse_reply  # noqa: E402
from shiftvoice.server import load_dotenv  # noqa: E402

KEYS = ("type", "staff", "days", "shift", "first", "next", "min", "max", "until_day", "from_day", "to_day",
        "weekdays", "avoid", "from_month", "can_add", "strength")


def main():
    load_dotenv(ROOT / ".env")
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "runtime" / "recorded_replies.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    fixtures = json.loads((ROOT / "tests" / "fixtures" / "replies.json").read_text(encoding="utf-8"))
    reader = HaikuReader(record_to=out)
    bad = 0
    for masked, want_text in fixtures.items():
        want = parse_reply(want_text)
        try:
            got = reader.read(masked)
        except ReadError as e:
            print(f"失敗 {masked} {e}")
            bad += 1
            continue
        diffs = []
        for i, (w, g) in enumerate(zip(want, got)):
            for k in KEYS:
                if w.get(k) != g.get(k):
                    diffs.append(f"{i}.{k} 用意={w.get(k)} 本物={g.get(k)}")
        if len(want) != len(got):
            diffs.append(f"件数 用意={len(want)} 本物={len(got)}")
        print(("合う " if not diffs else "違う ") + masked + ("" if not diffs else "\n    " + "\n    ".join(diffs)))
        bad += bool(diffs)
    builds = json.loads((ROOT / "tests" / "fixtures" / "builds.json").read_text(encoding="utf-8"))
    b = SonnetBuilder()
    for masked, want_text in builds.items():
        got = b.build(masked)
        print("組み立て", masked, "\n    本物=", json.dumps(got, ensure_ascii=False),
              "\n    用意=", json.dumps(parse_build(want_text), ensure_ascii=False))
    print(f"食い違い・失敗 {bad} 件 / {len(fixtures)} 件。録った返事 {out}")


if __name__ == "__main__":
    main()
