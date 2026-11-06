"""AI のテストプレイ（2026-11-06）で見つけた不具合の試験。失敗した一言をそのまま書く。"""
import json

import pytest

from conftest import REPLIES
from shiftvoice.period import read_answer, read_period
from shiftvoice.reader import RecordedReader
from shiftvoice.session import period_answer


class Fake(RecordedReader):
    """録った返事に、形だけ確かめる返事を足した読み手。"""

    def __init__(self, more):
        super().__init__({**json.loads(REPLIES.read_text(encoding="utf-8")),
                          **{k: json.dumps({"items": v}, ensure_ascii=False) for k, v in more.items()}})


# 1 今月だけじゃなくて毎月 ------------------------------------------------------------

@pytest.mark.parametrize("said", ["遅番の次の日は早番にしない。今月だけじゃなくて毎月",
                                  "今月だけじゃなく、毎月ずっと効くようにお願いします",
                                  "平日は日8を1人。今月だけでなく、これからも",
                                  "今月だけではなくて、これからずっと",
                                  "今回だけじゃなくて今後も",
                                  "今月に限らず、ずっとそうして"])
def test_not_only_this_month_is_every_month(said):
    assert read_period(said) == "rule"


def test_not_only_without_every_month_asks():
    assert read_period("今月だけじゃなくて来月も") is None


@pytest.mark.parametrize("said,want", [("毎月じゃなくて今回だけ", "once"), ("今月だけ", "once"), ("今月は夜勤0で", "once"),
                                       ("毎月、夜勤は2回まで", "rule")])
def test_period_words_stay(said, want):
    assert read_period(said) == want


def test_answer_not_only_this_month():
    assert read_answer("今月だけじゃなくて毎月") == "rule"
    assert period_answer("今月だけじゃなくて、毎月で") == "rule"
    assert period_answer("今月だけ") == "once"
