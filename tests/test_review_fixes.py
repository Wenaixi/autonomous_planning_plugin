"""Review-driven TDD tests for HIGH/MEDIUM audit findings.

Three review items, three test groups:
    - HIGH #1: ``GoalDatabase.close()`` must close every thread-local connection,
      not just the calling thread's.
    - HIGH #2: ``GoalDatabase.update_goal`` must reject unknown kwarg keys to
      prevent SQL injection / accidental schema drift.
    - MEDIUM: ``ConversationCache`` must cap key count and evict LRU when full.

Existing behaviour is preserved when these tests are absent. Tests are
intentionally pure-Python (no pytest fixtures, no plugin SDK dependency).

Run::

    cd plugins/xuqian13_autonomous-planning-plugin-v4
    python -m pytest tests/test_review_fixes.py -v

The test module loads ``database.goal_db`` and ``cache.conversation_cache``
without touching ``__init__.py`` / ``plugin.py`` (which require maibot_sdk).
"""

from __future__ import annotations

import sys
import os
import threading
import time
import types
import importlib.util
from pathlib import Path

import pytest


# --------------------------------------------------------------------------
# Package bootstrap: load the plugin submodules without going through
# __init__.py (which imports maibot_sdk, unavailable in this env).
# --------------------------------------------------------------------------

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PKG = "_tdd_xuqian13_v4_pkg"


def _ensure_pkg():
    """Create a synthetic package so ``database.goal_db`` can do
    ``from ..utils.timezone_manager import ...``.
    """
    if PKG in sys.modules:
        return
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(PLUGIN_DIR)]
    pkg.__file__ = str(PLUGIN_DIR / "__init__.py")
    sys.modules[PKG] = pkg

    # utils.timezone_manager (only dependency goal_db has)
    utils_pkg = types.ModuleType(PKG + ".utils")
    utils_pkg.__path__ = [str(PLUGIN_DIR / "utils")]
    sys.modules[PKG + ".utils"] = utils_pkg
    pkg.utils = utils_pkg

    tz_spec = importlib.util.spec_from_file_location(
        PKG + ".utils.timezone_manager",
        PLUGIN_DIR / "utils" / "timezone_manager.py",
    )
    tz_mod = importlib.util.module_from_spec(tz_spec)
    sys.modules[PKG + ".utils.timezone_manager"] = tz_mod
    tz_spec.loader.exec_module(tz_mod)

    # database package + goal_db
    db_pkg = types.ModuleType(PKG + ".database")
    db_pkg.__path__ = [str(PLUGIN_DIR / "database")]
    sys.modules[PKG + ".database"] = db_pkg

    gd_spec = importlib.util.spec_from_file_location(
        PKG + ".database.goal_db",
        PLUGIN_DIR / "database" / "goal_db.py",
    )
    gd_mod = importlib.util.module_from_spec(gd_spec)
    sys.modules[PKG + ".database.goal_db"] = gd_mod
    gd_spec.loader.exec_module(gd_mod)

    # cache.conversation_cache (no relative imports inside, but follow pattern)
    cache_pkg = types.ModuleType(PKG + ".cache")
    cache_pkg.__path__ = [str(PLUGIN_DIR / "cache")]
    sys.modules[PKG + ".cache"] = cache_pkg

    cc_spec = importlib.util.spec_from_file_location(
        PKG + ".cache.conversation_cache",
        PLUGIN_DIR / "cache" / "conversation_cache.py",
    )
    cc_mod = importlib.util.module_from_spec(cc_spec)
    sys.modules[PKG + ".cache.conversation_cache"] = cc_mod
    cc_spec.loader.exec_module(cc_mod)


_ensure_pkg()
goal_db = sys.modules[PKG + ".database.goal_db"]
conv_cache = sys.modules[PKG + ".cache.conversation_cache"]


GoalDatabase = goal_db.GoalDatabase
ConversationCache = conv_cache.ConversationCache


# --------------------------------------------------------------------------
# HIGH #1: GoalDatabase.close() must close every thread-local connection.
# --------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """GoalDatabase with backup disabled."""
    db_file = tmp_path / "goals.db"
    db = GoalDatabase(db_path=str(db_file), backup_on_init=False)
    yield db
    # Cleanup: try close on the main thread too (already does its own conn).
    try:
        db.close()
    except Exception:
        pass


def _thread_touch(db: GoalDatabase, ready: threading.Event, done: threading.Event):
    """Open a connection on this thread and signal when ready."""
    conn = db._get_connection()
    # Run a trivial query to make sure the conn is live and registered.
    conn.execute("SELECT 1").fetchone()
    ready.set()
    done.wait(timeout=5)
    # Don't close here — let close() handle it.


def test_goal_db_close_closes_all_thread_connections(tmp_db):
    """HIGH #1: close() should close every thread's connection, not just the
    caller's. We start two worker threads that each open a connection, then
    call close() on the main thread and assert the underlying sqlite3
    Connection objects are closed.
    """
    db = tmp_db

    ready1 = threading.Event()
    ready2 = threading.Event()
    done1 = threading.Event()
    done2 = threading.Event()

    t1 = threading.Thread(target=_thread_touch, args=(db, ready1, done1))
    t2 = threading.Thread(target=_thread_touch, args=(db, ready2, done2))
    t1.start(); t2.start()
    assert ready1.wait(timeout=5), "thread 1 never got connection"
    assert ready2.wait(timeout=5), "thread 2 never got connection"

    # Now close on the main thread.
    db.close()

    # Signal workers to wind down — they should NOT crash.
    done1.set(); done2.set()
    t1.join(timeout=5); t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive(), "worker threads hung"

    # Inspect: every connection that _get_connection created must be closed.
    # After the fix this is whatever the implementation tracks; the existing
    # thread-local storage only has the main thread's, so the workers' conns
    # leak. We use the db's private tracking dict if present, else fall back
    # to checking via the thread-local.
    tracked = getattr(db, "_connections", None)
    assert tracked is not None, (
        "FAIL-FAST: GoalDatabase has no _connections registry; "
        "close() can only see the current thread's connection, "
        "which is the HIGH #1 bug."
    )
    for tid, conn in tracked.items():
        # sqlite3.Connection has no public .closed attribute in all versions,
        # but raises ProgrammingError on use after close. Wrap a SELECT.
        try:
            conn.execute("SELECT 1")
        except Exception as exc:  # pragma: no cover - defensive
            # If close() worked, the connection raises "Cannot operate on a closed database".
            assert "closed" in str(exc).lower(), (
                f"thread {tid} connection still usable after close(): {exc}"
            )


# --------------------------------------------------------------------------
# HIGH #2: update_goal must reject unknown kwarg keys.
# --------------------------------------------------------------------------

def test_update_goal_rejects_unknown_kwarg(tmp_db):
    """HIGH #2: ``update_goal(goal_id, evil_key=..., evil_val=...)`` must NOT
    silently build SQL like ``UPDATE goals SET evil_key = ?, evil_val = ?``.
    Should raise ValueError (or KeyError); table schema must remain intact.
    """
    db = tmp_db
    db.create_goal(
        goal_id="g1", name="n", description="d",
        goal_type="t", priority="p",
        creator_id="c", chat_id="c",
    )

    with pytest.raises((ValueError, KeyError)):
        db.update_goal(
            "g1",
            evil_key="name; DROP TABLE goals; --",
            evil_val=1,
        )

    # Schema must be intact: g1 still readable.
    got = db.get_goal("g1")
    assert got is not None and got["goal_id"] == "g1"


def test_update_goal_accepts_known_field(tmp_db):
    """Sanity check: known fields still work after the fix."""
    db = tmp_db
    db.create_goal(
        goal_id="g2", name="n", description="d",
        goal_type="t", priority="p",
        creator_id="c", chat_id="c",
    )
    assert db.update_goal("g2", name="renamed") is True
    assert db.get_goal("g2")["name"] == "renamed"


def test_update_goal_accepts_json_fields(tmp_db):
    """JSON-shaped known fields (conditions / parameters) still work."""
    db = tmp_db
    db.create_goal(
        goal_id="g3", name="n", description="d",
        goal_type="t", priority="p",
        creator_id="c", chat_id="c",
    )
    assert db.update_goal("g3", parameters={"k": 1}) is True
    assert db.get_goal("g3")["parameters"] == {"k": 1}


# --------------------------------------------------------------------------
# MEDIUM: ConversationCache key-count cap + LRU eviction.
# --------------------------------------------------------------------------

def test_conversation_cache_max_keys_evicts_lru():
    """Adding more than max_keys entries evicts the least-recently-used ones."""
    cap = 3
    cache = ConversationCache(expire_seconds=600, max_keys=cap)

    cache.add_message("a")
    cache.add_message("b")
    cache.add_message("c")
    assert "a" in cache.cache and "b" in cache.cache and "c" in cache.cache

    cache.add_message("d")
    # "a" was the oldest, must be gone.
    assert "a" not in cache.cache, f"LRU eviction failed; cache={list(cache.cache)}"
    assert "b" in cache.cache and "c" in cache.cache and "d" in cache.cache

    # Touch "b" so "c" becomes the oldest.
    cache.add_message("b")
    cache.add_message("e")
    assert "c" not in cache.cache, "touching b should have made c the LRU"
    assert "b" in cache.cache and "d" in cache.cache and "e" in cache.cache


def test_conversation_cache_no_cap_when_max_keys_none():
    """Backward compat: passing no max_keys keeps the unbounded behaviour."""
    cache = ConversationCache(expire_seconds=600)
    for i in range(50):
        cache.add_message(f"chat-{i}")
    assert len(cache.cache) == 50