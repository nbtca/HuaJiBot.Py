import re
from datetime import datetime

import pytest

from routing import PATTERN, Gate, parse_command
from services import TZ, calendar_text
from state import State


@pytest.mark.parametrize(
    "text,result",
    [
        ("日程", ("schedule", "")),
        ("日程 -1", ("schedule", "-1")),
        ("/schedule 2", ("schedule", "2")),
        ("hj 工单", ("tickets", "")),
        ("滑稽 查询", ("mc", "")),
        ("公告 多词 公告内容", ("announce", "多词 公告内容")),
        ("日程\t2", ("schedule", "2")),
        ("ai hello world", ("ai", "hello world")),
    ],
)
def test_commands(text, result):
    assert re.search(PATTERN, text)
    assert parse_command(text) == result


@pytest.mark.parametrize(
    "text",
    [
        "明天日程是什么",
        "查询一下",
        "工单已经处理",
        "hello",
        "问候",
        "aichat",
        "这是 ai 问题",
    ],
)
def test_chat_does_not_trigger(text):
    assert not re.search(PATTERN, text)
    assert parse_command(text) is None


def test_admission_scoped_dedup_and_busy():
    gate = Gate()
    assert gate.enter("group-a", "u", "id", 10)
    assert not gate.enter("group-a", "v", "id2", 10)
    gate.leave("group-a")
    assert not gate.enter("group-a", "u", "id", 0)
    assert gate.enter("group-b", "u", "id", 10)


ICS = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:weekly\r
DTSTART:20261101T010000Z\r
DTEND:20261101T020000Z\r
RRULE:FREQ=DAILY;COUNT=10\r
EXDATE:20261106T010000Z\r
SUMMARY:Repair meetup\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:cancelled\r
DTSTART:20261107T010000Z\r
DTEND:20261107T020000Z\r
STATUS:CANCELLED\r
SUMMARY:Do not show\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_calendar_recurring_timezone_exclusion_and_past():
    now = datetime(2026, 11, 5, 12, tzinfo=TZ)
    text = calendar_text(ICS, now, 7)
    assert "11-07 09:00" in text
    assert "11-06" not in text and "Do not show" not in text
    assert "11-05 09:00" not in text
    assert "11-04 09:00" in calendar_text(ICS, now, -7)


def test_calendar_queries_skip_placeholders_before_selecting_next():
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:marked
DTSTART:20261127T123000Z
SUMMARY:NWDC
DESCRIPTION:[skip notification]
END:VEVENT
BEGIN:VEVENT
UID:tentative
DTSTART:20261127T123000Z
SUMMARY:NWDC （待定）
END:VEVENT
BEGIN:VEVENT
UID:repair
DTSTART:20261128T013000Z
SUMMARY:理工维修日
END:VEVENT
END:VCALENDAR"""
    now = datetime(2026, 11, 20, 15, tzinfo=TZ)
    for only_next in (False, True):
        result = calendar_text(raw, now, 30, only_next)
        assert "11-28 09:30 · 理工维修日" in result
        assert "NWDC" not in result


def test_notification_persistence_and_idempotency(tmp_path):
    path = tmp_path / "state.db"
    state = State(path)
    with pytest.raises(ValueError):
        state.enqueue("key", "target", "hello")
    state.bind("target")
    assert state.enqueue("key", "target", "hello") == "pending"
    assert state.enqueue("key", "target", "hello") == "pending"
    with pytest.raises(ValueError):
        state.enqueue("key", "target", "changed")
    state.close()
    state = State(path)
    assert state.next()[:3] == ("key", "target", "hello")
    state.unbind("target")
    assert state.next() is None
    state.close()
