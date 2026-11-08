"""公開版のブラウザの音声認識（画面の webDone）。node で画面の JS を取り出し、document などを偽物にして動かす。
文字が取れなかった時に空の一言を送らないこと、取れた時に1回だけ送ることを確かめる。"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

HTML = pathlib.Path(__file__).resolve().parents[1] / "src" / "shiftvoice" / "static" / "index.html"


def _run(calls):
    """calls は [{finals, interim, failed}, ...]。同じ r に対して webDone を calls の数だけ呼ぶ。"""
    if not shutil.which("node"):
        pytest.skip("node が無い")
    fn = re.search(r"function webDone\(r\) \{.*?\n\}", HTML.read_text(encoding="utf-8"), re.S)[0]
    js = ("var notes=[],submits=0,rec=null,pttKey=true;"
          "var els={talk:{textContent:'',classList:{add(){},remove(){}}},input:{value:''},form:{requestSubmit(){submits++;}}};"
          "var document={getElementById(id){return els[id];}};"
          "function pttNote(s){notes.push(s);}"
          f"{fn};"
          f"var r0=Object.assign({{finals:[],interim:'',failed:'',done:false}},{json.dumps(calls[0], ensure_ascii=False)});"
          "rec=r0;"
          f"for(var i=0;i<{len(calls)};i++){{webDone(r0);}}"
          "console.log(JSON.stringify({notes:notes,submits:submits,input:els.input.value,rec:rec,pttKey:pttKey,talk:els.talk.textContent}));")
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def test_文字が取れず失敗の理由がある時は_送らず理由を出す():
    out = _run([{"finals": [], "failed": "マイクが使えません。ブラウザのマイクの許可を確かめるか、打って伝えてください"}])
    assert out["submits"] == 0 and out["input"] == ""
    assert out["notes"] == ["マイクが使えません。ブラウザのマイクの許可を確かめるか、打って伝えてください"]
    assert out["rec"] is None and out["pttKey"] is False and out["talk"] == "話す"


def test_文字が取れず理由も無い時は_送らず聞き取れなかったと出す():
    out = _run([{"finals": [], "interim": "  "}])
    assert out["submits"] == 0 and out["notes"] == ["聞き取れませんでした。もう一度話すか、打って伝えてください"]


def test_取れた文字は入力欄に入り_送るのは1回だけ():
    out = _run([{"finals": ["外れているルールは"]}])
    assert out["input"] == "外れているルールは" and out["submits"] == 1


def test_同じ聞き取りで2回呼ばれても_送るのは1回だけ():
    out = _run([{"finals": ["外れているルールは"]}, {}])
    assert out["submits"] == 1 and out["notes"] == [""]
