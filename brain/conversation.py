"""The conversation: the unit of record above a run.

A run has always been an island. Nothing recorded that two runs were things the
same person said, one after the other, in one sitting — the process that hosted
them was anonymous, and the only cross-run memory (`BrainSession._last_run`) was
one turn deep, in RAM, and died with the process.

A conversation is that missing unit. It is minted per interactive process, every
run it hosts is stamped with it and given a turn ordinal, and the record is
sufficient on its own: a later process can read a conversation back and continue
it. What comes back is the conversation's REFERENTS — what was asked, what was
answered, which files each turn produced — never live state. Containers are not
resurrected and in-flight work is not resumed; a conversation is a record, not a
process.

Liveness is a file lock, not a heuristic: while a process holds its
conversation's lock, that conversation's rows are NOT stale and its containers
are NOT orphans. Before this, a second CLI would force-remove the first's live
sandboxes and mark its running rows aborted.
"""
from __future__ import annotations

import datetime as _dt
import errno
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

try:                                   # POSIX advisory locks
    import fcntl
except ImportError:                    # pragma: no cover — non-POSIX
    fcntl = None                       # type: ignore[assignment]

LOCK_SUFFIX = ".lock"


def new_id() -> str:
    """Readable, sortable conversation id: conv-<timestamp>-<uniq>."""
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"conv-{stamp}-{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------- #
# Liveness
# --------------------------------------------------------------------------- #
class Liveness:
    """An advisory lock marking a conversation as held by a live process.

    Best-effort by design: a platform without ``fcntl`` simply never reports a
    conversation as live, which restores the previous (single-process)
    behavior rather than breaking it.
    """

    def __init__(self, lock_dir: str | Path, conversation_id: str):
        self.path = Path(lock_dir) / f"{conversation_id}{LOCK_SUFFIX}"
        self._fh = None
        self.contended = False      # False + not held => the lock dir is unusable
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 'a+', not 'w': a failed acquisition must not truncate the pid the
            # actual holder recorded.
            fh = open(self.path, "a+")
        except OSError:
            self._fh = None
            return
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            # Held by someone else — the only case that means "contended".
            # An unwritable directory took the branch above, and reporting it
            # as "another live session holds this" was confident and wrong.
            self.contended = e.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)
            fh.close()
            self._fh = None
            return
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
        except OSError:
            pass
        self._fh = fh

    @property
    def held(self) -> bool:
        return self._fh is not None

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
        except OSError:
            pass
        self._fh = None
        try:
            self.path.unlink()
        except OSError:
            pass


def live_ids(lock_dir: str | Path, *, own: Optional[str] = None) -> set[str]:
    """Conversation ids currently held by a live process.

    A lock file whose lock can be TAKEN is a leftover, not a live conversation,
    so it is reported as dead and removed. ``own`` is always reported live —
    this process holds it, and a process must never judge itself stale.
    """
    lock_dir = Path(lock_dir)
    live: set[str] = {own} if own else set()
    if fcntl is None or not lock_dir.is_dir():
        return live
    for p in lock_dir.glob(f"*{LOCK_SUFFIX}"):
        cid = p.name[: -len(LOCK_SUFFIX)]
        if cid in live:
            continue
        try:
            with open(p, "r+") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    live.add(cid)       # held by someone else -> live
                    continue
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            continue
        # The file is NOT removed. Liveness.__init__ opens it and then takes
        # the lock, and those are two steps: a scan landing between them would
        # acquire, release and delete the file out from under a process that is
        # about to hold it. That process's flock then succeeds on an orphaned
        # fd — held, but invisible to every later scan, so the next session
        # treats a live conversation as dead and force-removes its containers.
        # A leftover lock file is a few bytes and answers honestly next time.
    return live


# --------------------------------------------------------------------------- #
# Reading the record back
# --------------------------------------------------------------------------- #
def _connect_ro(db_path: str | Path) -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT conversation_id FROM runs LIMIT 1")
        return conn
    except sqlite3.Error:
        return None


def turns(db_path: str | Path, conversation_id: str, *, limit: int = 200) -> list[dict]:
    """Every run of one conversation, oldest first.

    Self-initiated runs (``origin='pursuit'``) are included and carry the turn
    ordinal of the user turn that authorized them — they are part of the record,
    but they are not things the user said.
    """
    conn = _connect_ro(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT run_id, turn, origin, objective, status, outcome, started_at, "
            "       finished_at, cost_usd, n_containers "
            "FROM runs WHERE conversation_id=? ORDER BY started_at LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def last_user_turn(db_path: str | Path, conversation_id: str) -> Optional[dict]:
    """The most recent thing the user said in this conversation, if any."""
    for row in reversed(turns(db_path, conversation_id)):
        if (row.get("origin") or "user") == "user":
            return row
    return None


def recent_ids(db_path: str | Path, limit: int = 20) -> list[str]:
    """Recorded conversation ids, most recently active first."""
    conn = _connect_ro(db_path)
    if conn is None:
        return []
    try:
        return [r[0] for r in conn.execute(
            "SELECT conversation_id FROM runs WHERE conversation_id IS NOT NULL "
            "GROUP BY conversation_id ORDER BY MAX(started_at) DESC LIMIT ?", (limit,))]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def resolve(db_path: str | Path, ref: str) -> Optional[str]:
    """Resolve a user-typed conversation reference to a recorded id.

    Accepts the full id, a unique prefix, or ``last``. Returns None when the
    reference matches nothing or is ambiguous — a resume that guesses which
    conversation was meant is worse than one that refuses.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    ids = recent_ids(db_path, limit=1000)
    if ref == "last":
        return ids[0] if ids else None
    if ref in ids:
        return ref
    hits = [i for i in ids if i.startswith(ref)]
    return hits[0] if len(hits) == 1 else None
