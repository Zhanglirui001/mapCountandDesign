"""FIFO admission within a recognition lane, shared by its GUI and workers.

Agent1/2 use independent window lanes. Memory-heavy Agent3 jobs share a FIFO
lane across windows, allowing one Agent3 worker at a time.

SQLite serializes queue edits. A held OS file lock proves each ticket's owner
is alive, so a crashed or killed process cannot block the queue indefinitely.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


def is_queue_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    return code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


@contextmanager
def _queue_database(directory: Path, timeout=0.1):
    db = sqlite3.connect(directory / "queue.sqlite3", timeout=timeout)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS tickets "
                   "(seq INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    finally:
        db.close()


def queue_directory() -> Path:
    configured = os.environ.get("COUNT_DESIGN_QUEUE_DIR")
    if configured:
        return Path(configured).resolve()
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "CountAndDesign" / "recognition-queue"


class _Lease:
    def __init__(self, path: Path, *, create: bool = False):
        self.stream = path.open("a+b" if create else "r+b")
        if create and path.stat().st_size == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise

    def close(self):
        self.stream.close()  # Closing the descriptor releases its OS lock.


def _held(path: Path) -> bool:
    try:
        probe = _Lease(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # Fail closed when another owner or permissions prevent probing.
    probe.close()
    return False


class RecognitionTicket:
    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory or queue_directory())
        self.directory.mkdir(parents=True, exist_ok=True)
        self.token = uuid4().hex
        self.path = self.directory / (self.token + ".lease")
        self.lease = _Lease(self.path, create=True)
        self.closed = False
        try:
            with self._database(timeout=3) as db:
                self._prune(db)
                db.execute("INSERT INTO tickets(token) VALUES (?)", (self.token,))
        except Exception:
            self.lease.close()
            self.path.unlink(missing_ok=True)
            raise

    def _database(self, timeout=0.1):
        return _queue_database(self.directory, timeout)

    def _prune(self, db):
        for (token,) in db.execute("SELECT token FROM tickets").fetchall():
            path = self.directory / (token + ".lease")
            worker_path = self.directory / (token + ".worker.lease")
            if _held(path) or _held(worker_path):
                continue
            db.execute("DELETE FROM tickets WHERE token = ?", (token,))
            for stale_path in (path, worker_path):
                try:
                    stale_path.unlink(missing_ok=True)
                except OSError:
                    pass  # The database row is sufficient to remove a stale job.

    def poll(self) -> tuple[bool, int]:
        """Return (may_run, number_ahead); retain admission until close()."""
        if self.closed:
            raise RuntimeError("Recognition ticket is closed")
        with self._database() as db:
            self._prune(db)
            tokens = [row[0] for row in db.execute("SELECT token FROM tickets ORDER BY seq")]
            position = tokens.index(self.token)
            return position == 0, position

    def close(self):
        if self.closed:
            return
        # No database access here: cancellation/completion must remain possible
        # even while SQLite is busy. The next successful poll prunes this row.
        # A live Agent3 worker lease still protects admission after owner death.
        self.lease.close()
        self.closed = True
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    @property
    def worker_admission(self) -> tuple[str, str]:
        return str(self.directory.resolve()), self.token


class RecognitionWorkerLease:
    """Protect admission for a spawned worker, including after its GUI crashes."""

    def __init__(self, admission: tuple[str, str]):
        directory, token = admission
        self.directory = Path(directory)
        self.path = self.directory / (token + ".worker.lease")
        self.lease = _Lease(self.path, create=True)
        try:
            # Publish the worker lease before checking admission. A concurrent
            # stale sweep either sees this lease or removes the row first; a
            # worker whose original ticket was removed must never start work.
            with _queue_database(self.directory, timeout=3) as db:
                first = db.execute("SELECT token FROM tickets ORDER BY seq LIMIT 1").fetchone()
                if first != (token,) or not _held(self.directory / (token + ".lease")):
                    raise RuntimeError("Recognition owner exited or admission was revoked before worker startup")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.lease.close()
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
