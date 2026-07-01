"""Retention: ActivityStore.prune_before trims old rows (the table is otherwise
append-only and grows without bound)."""

import time

from server.storage.activity_db import ActivityStore


def test_prune_before_deletes_only_old_rows(tmp_path):
    store = ActivityStore(db_path=tmp_path / "library.db")
    now = int(time.time())
    with store._connect() as conn:
        conn.executemany(
            "INSERT INTO activity_log (kind, user_id, at) VALUES (?, NULL, ?)",
            [
                ("search", now - 100 * 86_400),   # old — should go
                ("similar", now - 10 * 86_400),    # recent — should stay
                ("recommend", now),                # now — should stay
            ],
        )

    removed = store.prune_before(now - 30 * 86_400)

    assert removed == 1
    assert store.counts_since(0) == {"similar": 1, "recommend": 1}


def test_prune_before_is_noop_when_nothing_is_old(tmp_path):
    store = ActivityStore(db_path=tmp_path / "library.db")
    store.record("search")

    removed = store.prune_before(int(time.time()) - 365 * 86_400)

    assert removed == 0
    assert store.counts_since(0) == {"search": 1}
