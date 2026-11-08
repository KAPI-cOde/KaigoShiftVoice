"""自分の Claude の鍵を貼った人が、鍵の間違い・残高切れ・使える量の上限で止まった時に、次にすることが分かる返事を出す。
前は「読み取りに失敗しました」だけで、鍵が原因だと分からなかった（2026-11-08、間違った鍵で起動して確かめた）。
SDK の本物の例外を作り、Claude は呼ばない。"""
import shutil

import anthropic
import httpx2 as httpx
import pytest

from conftest import FIXED_NOW
from shiftvoice.reader import KEY_BAD, KEY_CREDIT, KEY_RATE, KeyProblem, ReadError, api_error
from shiftvoice.session import READ_FAILED, Session
from shiftvoice.store import Store

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _err(cls, status, message):
    return cls(message, response=httpx.Response(status, request=REQ), body=None)


@pytest.mark.parametrize("e, want", [
    (_err(anthropic.AuthenticationError, 401, "invalid x-api-key"), KEY_BAD),
    (_err(anthropic.PermissionDeniedError, 403, "permission denied"), KEY_BAD),
    (_err(anthropic.BadRequestError, 400, "Your credit balance is too low to access the Anthropic API"), KEY_CREDIT),
    (_err(anthropic.RateLimitError, 429, "rate limit"), KEY_RATE),
])
def test_鍵の間違い_残高_上限は_次にすることを言う(e, want):
    got = api_error(e, "読み取り")
    assert isinstance(got, KeyProblem) and str(got) == want


@pytest.mark.parametrize("e", [
    _err(anthropic.BadRequestError, 400, "messages: field required"),
    _err(anthropic.InternalServerError, 500, "overloaded"),
    anthropic.APIConnectionError(request=REQ),
])
def test_鍵と関係ない失敗は_鍵のせいにしない(e):
    got = api_error(e, "読み取り")
    assert not isinstance(got, KeyProblem) and isinstance(got, ReadError)
    assert "鍵" not in str(got)


class _Raises:
    """読み取りで決まった失敗を起こす読み手（Claude は呼ばない）。"""
    sent: list = []

    def __init__(self, exc):
        self.exc = exc

    def read(self, masked_text, history=None):
        raise self.exc


def _sess(built_dir, tmp_path, exc):
    d = tmp_path / "h"
    shutil.copytree(built_dir, d)
    s = Session(Store(d), 2026, 11, _Raises(exc), now=lambda: FIXED_NOW)
    s.open()
    return s


def test_鍵が違う時の一言は_鍵の直し方を返し_表を変えない(built_dir, tmp_path):
    s = _sess(built_dir, tmp_path, KeyProblem(KEY_BAD))
    before = s.store.month(2026, 11).table
    r = s.say("遅番の次の日に早番はきついから、やめてあげて")
    assert r.kind == "error" and r.message == KEY_BAD and r.extra.get("key_problem")
    assert ".env" in r.message and "Ctrl+C" in r.message
    assert s.store.month(2026, 11).table == before


def test_通信の失敗は_今までどおり読み取りの失敗と返す(built_dir, tmp_path):
    s = _sess(built_dir, tmp_path, ReadError("読み取りを呼べませんでした（APIConnectionError）"))
    r = s.say("遅番の次の日に早番はきついから、やめてあげて")
    assert r.kind == "error" and r.message == READ_FAILED and not (r.extra or {}).get("key_problem")
