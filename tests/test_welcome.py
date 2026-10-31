import pytest

from hearback.util import HearbackError
from hearback.welcome import Welcome, parse_welcome


def make(tmp_path, text="Hello and welcome"):
    if text is not None:
        (tmp_path / "welcome.txt").write_text(text, encoding="utf-8")
    return Welcome({"enabled": True, "file": "welcome.txt"}, tmp_path, tmp_path / ".hearback")


def test_parse_welcome():
    assert parse_welcome(None) == {"enabled": False, "file": ""}
    assert parse_welcome({"enabled": True, "file": "w.txt"}) == {"enabled": True, "file": "w.txt"}
    with pytest.raises(HearbackError):
        parse_welcome({"enabled": True})
    with pytest.raises(HearbackError):
        parse_welcome({"enabled": "yes", "file": "w.txt"})


def test_each_person_gets_it_once_bots_and_the_owner_never(tmp_path):
    w, sent = make(tmp_path), []
    send = lambda uid, text: sent.append((uid, text))
    assert w.greet(send, "1") and not w.greet(send, "1")
    assert not w.greet(send, "2", is_bot=True) and not w.greet(send, "3", owner_id="3")
    assert sent == [("1", "Hello and welcome")]
    assert make(tmp_path).greeted() == {"1"}          # kept across restarts


def test_closed_dms_are_tried_again_and_no_file_sends_nothing(tmp_path):
    w = make(tmp_path)

    def closed(uid, text):
        raise HearbackError("HTTP 403: Cannot send messages to this user")
    assert not w.greet(closed, "1") and w.greeted() == set()
    sent = []
    assert w.greet(lambda u, t: sent.append(u), "1") and sent == ["1"]
    (tmp_path / "welcome.txt").unlink()
    assert not w.greet(lambda u, t: sent.append(u), "2") and sent == ["1"]


def test_off_sends_nothing(tmp_path):
    (tmp_path / "welcome.txt").write_text("hi", encoding="utf-8")
    w = Welcome({"enabled": False, "file": "welcome.txt"}, tmp_path, tmp_path / ".hearback")
    assert not w.greet(lambda u, t: pytest.fail("off"), "1")


def test_mark_counts_people_greeted_elsewhere(tmp_path):
    w = make(tmp_path)
    w.mark(["5", "6"])
    assert not w.greet(lambda u, t: pytest.fail("already greeted"), "5")
