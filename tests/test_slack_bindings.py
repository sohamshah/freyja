"""Persisted thread bindings: survive a restart, stay bounded, keep the
operator's titles."""

from __future__ import annotations

from bridge.gateway.slack_bindings import ThreadBindings


def test_bindings_round_trip_through_the_file(tmp_path):
    path = tmp_path / "b.json"
    a = ThreadBindings(path, flush_delay=0)
    a.add_thread("1.1")
    a.add_message_ts("2.2")
    a.add_code_channel("C9", {"name": "job"})
    a.set_title("D1", "1.1", "Auto name", by_user=False)
    a.save()

    b = ThreadBindings(path, flush_delay=0)
    assert b.has_thread("1.1") and b.has_message_ts("2.2")
    assert b.is_code_channel("C9") and not b.is_code_channel("C1")
    assert b.title_for("D1", "1.1") == "Auto name" and not b.title_is_users("D1", "1.1")


def test_operator_title_wins_over_automatic_ones(tmp_path):
    bd = ThreadBindings(tmp_path / "b.json", flush_delay=0)
    bd.set_title("D1", "1.1", "auto", by_user=False)
    bd.set_title("D1", "1.1", "Mine", by_user=True)
    bd.set_title("D1", "1.1", "auto again", by_user=False)
    assert bd.title_for("D1", "1.1") == "Mine" and bd.title_is_users("D1", "1.1")
    bd.set_title("D1", "1.1", "Mine 2", by_user=True)
    assert bd.title_for("D1", "1.1") == "Mine 2"


def test_sets_are_bounded_oldest_first(tmp_path):
    bd = ThreadBindings(tmp_path / "b.json", max_threads=3, max_message_ts=2, flush_delay=0)
    for i in range(5):
        bd.add_thread(f"t{i}")
        bd.add_message_ts(f"m{i}")
    assert list(bd.mentioned_threads) == ["t2", "t3", "t4"]
    assert list(bd.bot_message_ts) == ["m3", "m4"]


def test_unreadable_file_starts_empty(tmp_path):
    path = tmp_path / "b.json"
    path.write_text("{not json")
    bd = ThreadBindings(path, flush_delay=0)
    assert not bd.mentioned_threads and not bd.code_channels
    bd.add_thread("1.0")
    bd.save()
    assert ThreadBindings(path, flush_delay=0).has_thread("1.0")
