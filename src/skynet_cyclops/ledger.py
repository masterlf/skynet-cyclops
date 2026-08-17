"""Private crash-consistent SQLite metadata for the observer."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any

from .errors import LedgerError

_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK (mode = 'observe'),
    tick_seq INTEGER NOT NULL DEFAULT 0,
    last_heartbeat REAL
);
CREATE TABLE missions (
    mission_id TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL
);
CREATE TABLE bindings (
    mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
    phase_key TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    PRIMARY KEY (mission_id, phase_key)
);
CREATE TABLE intents (
    idempotency_key TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
    phase_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared', 'completed')),
    created_at REAL NOT NULL
);
CREATE TABLE incidents (
    incident_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
    phase_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    first_tick INTEGER NOT NULL,
    last_tick INTEGER NOT NULL,
    observed_ticks INTEGER NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('observing', 'active', 'resolved'))
);
INSERT INTO meta(singleton, schema_version, mode) VALUES (1, 1, 'observe');
"""


class Ledger:
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection

    @classmethod
    def create(cls, path: str | os.PathLike[str]) -> Ledger:
        candidate = Path(path)
        _secure_parent(candidate.parent)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(candidate, flags, 0o600)
            os.close(descriptor)
            os.chmod(candidate, 0o600)
            connection = _connect(candidate)
            connection.executescript(_SCHEMA)
            connection.commit()
            return cls(candidate, connection)
        except (OSError, sqlite3.Error) as exc:
            with suppress(OSError):
                candidate.unlink(missing_ok=True)
            raise LedgerError("ledger could not be initialized") from exc

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> Ledger:
        candidate = Path(path)
        try:
            info = candidate.lstat()
            if candidate.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise LedgerError("ledger must be a regular file")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise LedgerError("ledger ownership is unsafe")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise LedgerError("ledger permissions are unsafe")
            connection = _connect(candidate)
            result = connection.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise LedgerError("ledger is unavailable")
            version = connection.execute(
                "SELECT schema_version FROM meta WHERE singleton=1"
            ).fetchone()
            if version is None or version[0] != _SCHEMA_VERSION:
                raise LedgerError("ledger schema is unsupported")
            return cls(candidate, connection)
        except FileNotFoundError as exc:
            raise LedgerError("ledger is missing") from exc
        except LedgerError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise LedgerError("ledger is unavailable") from exc

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @property
    def schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT schema_version FROM meta WHERE singleton=1"
        ).fetchone()
        return int(row[0])

    @property
    def mode(self) -> str:
        row = self._connection.execute("SELECT mode FROM meta WHERE singleton=1").fetchone()
        return str(row[0])

    def pragmas(self) -> dict[str, int]:
        names = ("foreign_keys", "synchronous", "busy_timeout")
        return {
            name: int(self._connection.execute(f"PRAGMA {name}").fetchone()[0]) for name in names
        }

    def register_mission(self, mission_id: str, manifest_hash: str) -> None:
        existing = self._connection.execute(
            "SELECT manifest_hash FROM missions WHERE mission_id=?", (mission_id,)
        ).fetchone()
        if existing is not None and existing[0] != manifest_hash:
            raise LedgerError("registered manifest hash does not match")
        self._connection.execute(
            "INSERT OR IGNORE INTO missions(mission_id, manifest_hash) VALUES (?, ?)",
            (mission_id, manifest_hash),
        )
        self._connection.commit()

    def mission_hash(self, mission_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT manifest_hash FROM missions WHERE mission_id=?", (mission_id,)
        ).fetchone()
        return None if row is None else str(row[0])

    def bind(self, mission_id: str, phase_key: str, task_id: str, idempotency_key: str) -> None:
        self._connection.execute(
            """INSERT INTO bindings(mission_id, phase_key, task_id, idempotency_key)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(mission_id, phase_key) DO UPDATE SET
                 task_id=excluded.task_id, idempotency_key=excluded.idempotency_key""",
            (mission_id, phase_key, task_id, idempotency_key),
        )
        self._connection.commit()

    def bindings(self, mission_id: str) -> dict[str, str]:
        rows = self._connection.execute(
            "SELECT phase_key, task_id FROM bindings WHERE mission_id=? ORDER BY phase_key",
            (mission_id,),
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def prepare_intent(self, key: str, mission_id: str, phase_key: str, now: float) -> None:
        self._connection.execute(
            """INSERT INTO intents(idempotency_key, mission_id, phase_key, state, created_at)
               VALUES (?, ?, ?, 'prepared', ?)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (key, mission_id, phase_key, now),
        )
        self._connection.commit()

    def complete_intent(self, key: str) -> None:
        self._connection.execute(
            "UPDATE intents SET state='completed' WHERE idempotency_key=?", (key,)
        )
        self._connection.commit()

    def next_tick(self, now: float) -> tuple[int, float | None]:
        row = self._connection.execute(
            "SELECT tick_seq, last_heartbeat FROM meta WHERE singleton=1"
        ).fetchone()
        sequence = int(row[0]) + 1
        previous = None if row[1] is None else float(row[1])
        self._connection.execute(
            "UPDATE meta SET tick_seq=?, last_heartbeat=? WHERE singleton=1", (sequence, now)
        )
        self._connection.commit()
        return sequence, previous

    def reconcile_incidents(
        self,
        mission_id: str,
        tick_seq: int,
        candidates: list[dict[str, str]],
        debounce_ticks: int,
    ) -> list[dict[str, Any]]:
        current_ids = {item["incident_id"] for item in candidates}
        for item in candidates:
            row = self._connection.execute(
                "SELECT first_tick, last_tick, observed_ticks FROM incidents WHERE incident_id=?",
                (item["incident_id"],),
            ).fetchone()
            observed = 1 if row is None or int(row[1]) != tick_seq - 1 else int(row[2]) + 1
            first_tick = tick_seq if row is None else int(row[0])
            disposition = "active" if observed >= debounce_ticks else "observing"
            self._connection.execute(
                """INSERT INTO incidents(
                       incident_id, mission_id, phase_key, kind, severity,
                       first_tick, last_tick, observed_ticks, disposition
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(incident_id) DO UPDATE SET
                       last_tick=excluded.last_tick,
                       observed_ticks=excluded.observed_ticks,
                       disposition=excluded.disposition,
                       severity=excluded.severity""",
                (
                    item["incident_id"],
                    mission_id,
                    item["phase_key"],
                    item["kind"],
                    item["severity"],
                    first_tick,
                    tick_seq,
                    observed,
                    disposition,
                ),
            )
        open_incidents = self._connection.execute(
            "SELECT incident_id FROM incidents WHERE mission_id=? AND disposition!='resolved'",
            (mission_id,),
        ).fetchall()
        for row in open_incidents:
            incident_id = str(row[0])
            if incident_id in current_ids:
                continue
            self._connection.execute(
                "UPDATE incidents SET disposition='resolved' WHERE incident_id=?",
                (incident_id,),
            )
        self._connection.commit()
        rows = self._connection.execute(
            """SELECT incident_id, phase_key, kind, severity, first_tick,
                      observed_ticks, disposition
               FROM incidents
               WHERE mission_id=? AND disposition!='resolved'
               ORDER BY incident_id""",
            (mission_id,),
        ).fetchall()
        return [
            {
                "id": str(row[0]),
                "phase_key": str(row[1]),
                "kind": str(row[2]),
                "severity": str(row[3]),
                "age_ticks": tick_seq - int(row[4]) + 1,
                "observed_ticks": int(row[5]),
                "disposition": str(row[6]),
            }
            for row in rows
        ]


def _secure_parent(path: Path) -> None:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise LedgerError("ledger directory is unsafe")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise LedgerError("ledger directory ownership is unsafe")
        os.chmod(path, 0o700)
    except OSError as exc:
        raise LedgerError("ledger directory is unavailable") from exc


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5.0)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=DELETE")
    return connection
