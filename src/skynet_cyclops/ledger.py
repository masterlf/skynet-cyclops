"""Private crash-consistent SQLite metadata for the observer."""

# SQL schemas and statements are kept legible rather than split into concatenated fragments.
# ruff: noqa: E501

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import LedgerError

_SCHEMA_VERSION = 3
_SCHEMA = """
CREATE TABLE meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK (mode = 'observe'),
    tick_seq INTEGER NOT NULL DEFAULT 0,
    last_heartbeat REAL,
    manager_last_wallclock REAL
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
    incident_id TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1),
    identity_version INTEGER NOT NULL DEFAULT 1 CHECK (identity_version = 1),
    mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
    phase_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_task_id TEXT,
    subject_run_id TEXT,
    severity TEXT NOT NULL,
    observation_sha256 TEXT NOT NULL DEFAULT '',
    expected_state TEXT NOT NULL DEFAULT 'unknown',
    observed_state TEXT NOT NULL DEFAULT 'unknown',
    first_tick INTEGER NOT NULL,
    last_tick INTEGER NOT NULL,
    observed_ticks INTEGER NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('observing', 'active', 'resolved')),
    lifecycle TEXT NOT NULL DEFAULT 'detected'
        CHECK (lifecycle IN ('detected', 'wake_sent', 'claimed', 'resolved', 'human_required', 'dead_letter')),
    terminal_reason TEXT,
    reason_code TEXT,
    human_question_code TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 2),
    next_attempt_at REAL NOT NULL DEFAULT 0,
    acknowledged_at REAL,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    terminal_at REAL,
    clean_tick INTEGER NOT NULL DEFAULT 0 CHECK (clean_tick IN (0, 1)),
    PRIMARY KEY (incident_id, generation)
);
CREATE TABLE wake_attempts (
    attempt_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no IN (1, 2)),
    result_nonce_sha256 TEXT NOT NULL,
    lease_token_sha256 TEXT NOT NULL,
    lease_owner TEXT NOT NULL,
    lease_acquired_at REAL NOT NULL,
    lease_expires_at REAL NOT NULL,
    observation_sha256 TEXT NOT NULL,
    cron_execution_id TEXT UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('leased', 'output_seen', 'ack_valid', 'ack_invalid', 'expired', 'superseded')),
    error_code TEXT,
    UNIQUE (incident_id, generation, attempt_no),
    FOREIGN KEY (incident_id, generation) REFERENCES incidents(incident_id, generation) ON DELETE CASCADE
);
CREATE TABLE wake_budgets (
    mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    used INTEGER NOT NULL CHECK (used >= 0),
    PRIMARY KEY (mission_id, day)
);
CREATE TABLE notification_intents (
    incident_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    terminal_kind TEXT NOT NULL CHECK (terminal_kind IN ('human_required', 'dead_letter')),
    decision_packet_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('pending', 'leased', 'sent', 'failed', 'acknowledged')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 2),
    lease_acquired_at REAL,
    lease_expires_at REAL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_outcome TEXT CHECK (last_outcome IN ('delivered', 'failed', 'not_configured', 'suppressed', 'malformed')),
    courier_execution_id TEXT UNIQUE,
    created_at REAL NOT NULL,
    PRIMARY KEY (incident_id, generation, terminal_kind),
    FOREIGN KEY (incident_id, generation) REFERENCES incidents(incident_id, generation) ON DELETE CASCADE
);
CREATE INDEX incidents_eligibility ON incidents(lifecycle, next_attempt_at, severity, first_tick);
CREATE INDEX wake_attempts_lease ON wake_attempts(state, lease_expires_at);
CREATE INDEX notifications_delivery ON notification_intents(state, created_at);
INSERT INTO meta(singleton, schema_version, mode) VALUES (1, 3, 'observe');
"""

_MIGRATION_V2 = (
    "ALTER TABLE incidents RENAME TO incidents_v1",
    "ALTER TABLE meta ADD COLUMN manager_last_wallclock REAL",
    """CREATE TABLE incidents (
        incident_id TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1),
        identity_version INTEGER NOT NULL DEFAULT 1 CHECK (identity_version = 1),
        mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
        phase_key TEXT NOT NULL, kind TEXT NOT NULL, subject_task_id TEXT, subject_run_id TEXT,
        severity TEXT NOT NULL, observation_sha256 TEXT NOT NULL DEFAULT '',
        expected_state TEXT NOT NULL DEFAULT 'unknown', observed_state TEXT NOT NULL DEFAULT 'unknown',
        first_tick INTEGER NOT NULL, last_tick INTEGER NOT NULL, observed_ticks INTEGER NOT NULL,
        disposition TEXT NOT NULL CHECK (disposition IN ('observing', 'active', 'resolved')),
        lifecycle TEXT NOT NULL DEFAULT 'detected'
            CHECK (lifecycle IN ('detected', 'wake_sent', 'claimed', 'resolved', 'human_required', 'dead_letter')),
        terminal_reason TEXT, reason_code TEXT, human_question_code TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 2),
        next_attempt_at REAL NOT NULL DEFAULT 0, acknowledged_at REAL,
        created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0,
        terminal_at REAL, clean_tick INTEGER NOT NULL DEFAULT 0 CHECK (clean_tick IN (0, 1)),
        PRIMARY KEY (incident_id, generation))""",
    """INSERT INTO incidents(
           incident_id, generation, identity_version, mission_id, phase_key, kind, severity,
           first_tick, last_tick, observed_ticks, disposition, lifecycle, clean_tick)
       SELECT incident_id, 1, 1, mission_id, phase_key, kind, severity, first_tick, last_tick,
              observed_ticks, disposition,
              CASE WHEN disposition='resolved' THEN 'resolved' ELSE 'detected' END,
              CASE WHEN disposition='resolved' THEN 1 ELSE 0 END
       FROM incidents_v1""",
    "DROP TABLE incidents_v1",
    """CREATE TABLE wake_attempts (
        attempt_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, generation INTEGER NOT NULL,
        attempt_no INTEGER NOT NULL CHECK (attempt_no IN (1, 2)), result_nonce_sha256 TEXT NOT NULL,
        lease_token_sha256 TEXT NOT NULL,
        lease_owner TEXT NOT NULL, lease_acquired_at REAL NOT NULL, lease_expires_at REAL NOT NULL,
        observation_sha256 TEXT NOT NULL, cron_execution_id TEXT UNIQUE,
        state TEXT NOT NULL CHECK (state IN ('leased', 'output_seen', 'ack_valid', 'ack_invalid', 'expired', 'superseded')),
        error_code TEXT, UNIQUE (incident_id, generation, attempt_no),
        FOREIGN KEY (incident_id, generation) REFERENCES incidents(incident_id, generation) ON DELETE CASCADE)""",
    """CREATE TABLE wake_budgets (
        mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
        day TEXT NOT NULL, used INTEGER NOT NULL CHECK (used >= 0), PRIMARY KEY (mission_id, day))""",
    """CREATE TABLE notification_intents (
        incident_id TEXT NOT NULL, generation INTEGER NOT NULL,
        terminal_kind TEXT NOT NULL CHECK (terminal_kind IN ('human_required', 'dead_letter')),
        decision_packet_id TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN ('pending', 'leased', 'sent', 'failed', 'acknowledged')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 2),
        lease_expires_at REAL, courier_execution_id TEXT, created_at REAL NOT NULL,
        PRIMARY KEY (incident_id, generation, terminal_kind),
        FOREIGN KEY (incident_id, generation) REFERENCES incidents(incident_id, generation) ON DELETE CASCADE)""",
    "CREATE INDEX incidents_eligibility ON incidents(lifecycle, next_attempt_at, severity, first_tick)",
    "CREATE INDEX wake_attempts_lease ON wake_attempts(state, lease_expires_at)",
    "CREATE INDEX notifications_delivery ON notification_intents(state, created_at)",
    "UPDATE meta SET schema_version=2 WHERE singleton=1 AND schema_version=1",
)

_MIGRATION_V3 = (
    "ALTER TABLE notification_intents ADD COLUMN lease_acquired_at REAL",
    "ALTER TABLE notification_intents ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0",
    "ALTER TABLE notification_intents ADD COLUMN last_outcome TEXT",
    "CREATE UNIQUE INDEX notification_execution_unique ON notification_intents(courier_execution_id)",
    "UPDATE meta SET schema_version=3 WHERE singleton=1 AND schema_version=2",
)


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
    def migrate_v1(cls, path: str | os.PathLike[str], backup_path: str | os.PathLike[str]) -> None:
        """Transactionally migrate a safe v1 ledger, retaining a private backup."""
        candidate = Path(path)
        backup = Path(backup_path)
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        backup_ready = False
        source_identity: tuple[int, int] | None = None
        try:
            info = candidate.lstat()
            source_identity = (info.st_dev, info.st_ino)
            if candidate.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise LedgerError("ledger must be a regular file")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise LedgerError("ledger ownership is unsafe")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise LedgerError("ledger permissions are unsafe")
            source = sqlite3.connect(candidate, timeout=5.0)
            version = source.execute("SELECT schema_version FROM meta WHERE singleton=1").fetchone()
            if version != (1,):
                raise LedgerError("ledger schema is not migration source v1")
            if source.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise LedgerError("ledger is unavailable")
            _secure_parent(backup.parent)
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(backup, flags, 0o600)
            os.close(descriptor)
            destination = sqlite3.connect(backup)
            source.backup(destination)
            destination.commit()
            destination.close()
            destination = None
            backup_ready = True
            os.chmod(backup, 0o600)
            backup_fd = os.open(backup, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(backup_fd)
            finally:
                os.close(backup_fd)
            source.execute("PRAGMA foreign_keys=ON")
            source.execute("BEGIN IMMEDIATE")
            for statement in _MIGRATION_V2:
                source.execute(statement)
            for statement in _MIGRATION_V3:
                source.execute(statement)
            if source.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise LedgerError("ledger migration violated referential integrity")
            source.commit()
            source.close()
            source = None
            with cls.open(candidate):
                pass
        except (OSError, sqlite3.Error, LedgerError) as exc:
            if source is not None:
                with suppress(sqlite3.Error):
                    source.rollback()
                source.close()
            if destination is not None:
                destination.close()
            if backup_ready and backup.exists():
                try:
                    restore_info = candidate.lstat()
                    if (
                        candidate.is_symlink()
                        or not stat.S_ISREG(restore_info.st_mode)
                        or source_identity is None
                        or (restore_info.st_dev, restore_info.st_ino) != source_identity
                    ):
                        raise LedgerError("ledger changed during migration rollback")
                    original = sqlite3.connect(backup)
                    restored = sqlite3.connect(candidate)
                    original.backup(restored)
                    restored.close()
                    original.close()
                    os.chmod(candidate, 0o600)
                except (OSError, sqlite3.Error):
                    pass
            elif backup.exists():
                with suppress(OSError):
                    backup.unlink()
            if isinstance(exc, LedgerError):
                raise
            raise LedgerError("ledger migration failed and was rolled back") from exc

    @classmethod
    def migrate_v2(cls, path: str | os.PathLike[str], backup_path: str | os.PathLike[str]) -> None:
        """Upgrade an exact private v2 ledger to v3 with a durable pre-migration backup."""
        candidate = Path(path)
        backup = Path(backup_path)
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            info = candidate.lstat()
            if (
                candidate.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise LedgerError("ledger migration source is unsafe")
            source = sqlite3.connect(candidate, timeout=5.0)
            if source.execute("SELECT schema_version FROM meta WHERE singleton=1").fetchone() != (
                2,
            ):
                raise LedgerError("ledger schema is not migration source v2")
            columns = {
                str(row[1]) for row in source.execute("PRAGMA table_info(notification_intents)")
            }
            if columns & {"lease_acquired_at", "next_attempt_at", "last_outcome"}:
                raise LedgerError("ledger v2 notification schema is not exact")
            _secure_parent(backup.parent)
            descriptor = os.open(
                backup,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.close(descriptor)
            destination = sqlite3.connect(backup)
            source.backup(destination)
            destination.commit()
            destination.close()
            destination = None
            source.execute("BEGIN IMMEDIATE")
            for statement in _MIGRATION_V3:
                source.execute(statement)
            source.commit()
            source.close()
            source = None
            os.chmod(backup, 0o600)
            with cls.open(candidate):
                pass
        except (OSError, sqlite3.Error, LedgerError) as exc:
            if source is not None:
                with suppress(sqlite3.Error):
                    source.rollback()
                source.close()
            if destination is not None:
                destination.close()
            if isinstance(exc, LedgerError):
                raise
            raise LedgerError("ledger v2 migration failed") from exc

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

    @contextmanager
    def bootstrap_lock(self) -> Iterator[None]:
        """Hold a private cross-process exclusive lock for graph authoring."""
        lock_path = self.path.with_name(f".{self.path.name}.bootstrap.lock")
        _secure_parent(lock_path.parent)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise LedgerError("bootstrap lock must be a regular file")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise LedgerError("bootstrap lock ownership is unsafe")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise LedgerError("bootstrap lock permissions are unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LedgerError("bootstrap apply is already running") from exc
            yield
        except LedgerError:
            raise
        except OSError as exc:
            raise LedgerError("bootstrap lock is unavailable") from exc
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                with suppress(OSError):
                    os.close(descriptor)

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

    def tick_state(self) -> tuple[int, float | None]:
        row = self._connection.execute(
            "SELECT tick_seq, last_heartbeat FROM meta WHERE singleton=1"
        ).fetchone()
        return int(row[0]), None if row[1] is None else float(row[1])

    def commit_tick(self, sequence: int, now: float) -> None:
        cursor = self._connection.execute(
            """UPDATE meta SET tick_seq=?, last_heartbeat=?
               WHERE singleton=1 AND tick_seq=?""",
            (sequence, now, sequence - 1),
        )
        if cursor.rowcount != 1:
            self._connection.rollback()
            raise LedgerError("tick sequence changed concurrently")
        self._connection.commit()

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
                   ON CONFLICT(incident_id, generation) DO UPDATE SET
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

    def observe_manager_incidents(
        self,
        observations: list[Any],
        *,
        mission_id: str | None = None,
        tick_seq: int,
        now: float,
        persistence_ticks: int = 2,
        observe_only: bool = False,
        commit: bool = True,
    ) -> None:
        """Persist one typed observation set and close conditions absent from this tick."""
        from .manager import IncidentObservation, stable_incident_id

        if tick_seq < 1 or not 1 <= persistence_ticks <= 10:
            raise LedgerError("manager observation bounds are invalid")
        typed: list[IncidentObservation] = []
        seen: set[str] = set()
        for item in observations:
            if not isinstance(item, IncidentObservation):
                raise LedgerError("manager observation is not typed")
            incident_id = stable_incident_id(item)
            if incident_id in seen:
                raise LedgerError("manager observations contain a duplicate identity")
            seen.add(incident_id)
            typed.append(item)
        observed_missions = {item.mission_id for item in typed}
        if mission_id is None:
            if len(observed_missions) == 1:
                target_mission = next(iter(observed_missions))
            else:
                mission_rows = self._connection.execute(
                    "SELECT mission_id FROM missions ORDER BY mission_id LIMIT 2"
                ).fetchall()
                if len(mission_rows) != 1:
                    raise LedgerError("manager observation mission is ambiguous")
                target_mission = str(mission_rows[0][0])
        else:
            target_mission = mission_id
        if observed_missions - {target_mission}:
            raise LedgerError("manager observations cross mission boundary")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for item in typed:
                incident_id = stable_incident_id(item)
                row = self._connection.execute(
                    """SELECT generation, last_tick, observed_ticks, lifecycle, clean_tick
                       FROM incidents WHERE incident_id=? ORDER BY generation DESC LIMIT 1""",
                    (incident_id,),
                ).fetchone()
                if row is None:
                    generation, first_tick, observed_ticks = 1, tick_seq, 1
                elif str(row[3]) in {"resolved", "human_required", "dead_letter"} and int(row[4]):
                    generation, first_tick, observed_ticks = int(row[0]) + 1, tick_seq, 1
                else:
                    generation = int(row[0])
                    first = self._connection.execute(
                        "SELECT first_tick FROM incidents WHERE incident_id=? AND generation=?",
                        (incident_id, generation),
                    ).fetchone()
                    first_tick = int(first[0])
                    observed_ticks = int(row[2]) + 1 if int(row[1]) == tick_seq - 1 else 1
                disposition = (
                    "active"
                    if observed_ticks >= persistence_ticks and not observe_only
                    else "observing"
                )
                self._connection.execute(
                    """INSERT INTO incidents(
                           incident_id, generation, identity_version, mission_id, phase_key, kind,
                           subject_task_id, subject_run_id, severity, observation_sha256,
                           expected_state, observed_state, first_tick, last_tick, observed_ticks,
                           disposition, lifecycle, created_at, updated_at, clean_tick
                       ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'detected', ?, ?, 0)
                       ON CONFLICT(incident_id, generation) DO UPDATE SET
                           severity=excluded.severity,
                           observation_sha256=excluded.observation_sha256,
                           expected_state=excluded.expected_state,
                           observed_state=excluded.observed_state,
                           last_tick=excluded.last_tick,
                           observed_ticks=excluded.observed_ticks,
                           disposition=CASE WHEN incidents.lifecycle='detected'
                               THEN excluded.disposition ELSE incidents.disposition END,
                           updated_at=excluded.updated_at,
                           clean_tick=0""",
                    (
                        incident_id,
                        generation,
                        item.mission_id,
                        item.phase_key,
                        item.kind,
                        item.subject_task_id,
                        item.subject_run_id,
                        item.severity,
                        item.observation_sha256,
                        item.expected_state,
                        item.observed_state,
                        first_tick,
                        tick_seq,
                        observed_ticks,
                        disposition,
                        now,
                        now,
                    ),
                )
            current = self._connection.execute(
                """SELECT incident_id, generation, lifecycle FROM incidents
                   WHERE clean_tick=0 AND mission_id=?""",
                (target_mission,),
            ).fetchall()
            for incident_id, generation, lifecycle in current:
                if str(incident_id) in seen:
                    continue
                if str(lifecycle) in {"resolved", "human_required", "dead_letter"}:
                    self._connection.execute(
                        """UPDATE incidents SET clean_tick=1, updated_at=?
                           WHERE incident_id=? AND generation=?""",
                        (now, incident_id, generation),
                    )
                    continue
                self._connection.execute(
                    """UPDATE incidents SET lifecycle='resolved', disposition='resolved',
                           terminal_reason='condition_cleared', terminal_at=?, updated_at=?, clean_tick=1
                       WHERE incident_id=? AND generation=?""",
                    (now, now, incident_id, generation),
                )
                self._connection.execute(
                    """UPDATE wake_attempts SET state='superseded'
                       WHERE incident_id=? AND generation=? AND state='leased'""",
                    (incident_id, generation),
                )
            if commit:
                self._connection.commit()
        except (sqlite3.Error, AttributeError) as exc:
            self._connection.rollback()
            raise LedgerError("manager observations could not be committed") from exc

    def lease_manager_incident(
        self, *, now: float, policy: Any, router_job_id: str
    ) -> dict[str, object]:
        """Reconcile lease expiry and atomically consume one wake budget."""
        from .manager import decision_packet_id

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            previous_clock = self._connection.execute(
                "SELECT manager_last_wallclock FROM meta WHERE singleton=1"
            ).fetchone()[0]
            if previous_clock is not None and now < float(previous_clock):
                self._connection.rollback()
                return {"wakeAgent": False}
            self._connection.execute(
                "UPDATE meta SET manager_last_wallclock=? WHERE singleton=1", (now,)
            )
            expired = self._connection.execute(
                """SELECT attempt_id, incident_id, generation, attempt_no
                   FROM wake_attempts WHERE state='leased' AND lease_expires_at<=?""",
                (now,),
            ).fetchall()
            for attempt_id, incident_id, generation, attempt_no in expired:
                self._connection.execute(
                    "UPDATE wake_attempts SET state='expired', error_code='ack_missing' WHERE attempt_id=?",
                    (attempt_id,),
                )
                if int(attempt_no) >= int(policy.max_attempts):
                    cursor = self._connection.execute(
                        """UPDATE incidents SET lifecycle='dead_letter', disposition='resolved',
                               terminal_reason='ack_missing', terminal_at=?, updated_at=?
                           WHERE incident_id=? AND generation=? AND lifecycle='wake_sent'""",
                        (now, now, incident_id, generation),
                    )
                    if cursor.rowcount == 1:
                        self._insert_notification_intent(
                            incident_id=str(incident_id),
                            generation=int(generation),
                            terminal="dead_letter",
                            decision_packet_id=decision_packet_id(
                                str(incident_id), int(generation), "dead_letter"
                            ),
                            now=now,
                        )
                else:
                    self._connection.execute(
                        """UPDATE incidents SET lifecycle='detected',
                               next_attempt_at=?, updated_at=?
                           WHERE incident_id=? AND generation=? AND lifecycle='wake_sent'""",
                        (now + int(policy.retry_backoff_seconds), now, incident_id, generation),
                    )
            self._connection.commit()

            self._connection.execute("BEGIN IMMEDIATE")
            if self._connection.execute(
                "SELECT 1 FROM wake_attempts WHERE state='leased' AND lease_expires_at>? LIMIT 1",
                (now,),
            ).fetchone():
                self._connection.commit()
                return {"wakeAgent": False}
            row = self._connection.execute(
                """SELECT incident_id, generation, mission_id, phase_key, kind,
                          subject_task_id, subject_run_id, observation_sha256,
                          expected_state, observed_state, attempt_count
                   FROM incidents
                   WHERE lifecycle='detected' AND disposition='active' AND next_attempt_at<=?
                         AND attempt_count < ?
                   ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,
                            first_tick, incident_id LIMIT 1""",
                (now, int(policy.max_attempts)),
            ).fetchone()
            if row is None:
                self._connection.commit()
                return {"wakeAgent": False}
            day = datetime.fromtimestamp(now, UTC).date().isoformat()
            used_row = self._connection.execute(
                "SELECT used FROM wake_budgets WHERE mission_id=? AND day=?", (row[2], day)
            ).fetchone()
            used = 0 if used_row is None else int(used_row[0])
            if used >= int(policy.daily_mission_limit):
                self._connection.commit()
                return {"wakeAgent": False}
            attempt_no = int(row[10]) + 1
            attempt_id = secrets.token_hex(16)
            result_nonce = secrets.token_hex(32)
            lease_token = secrets.token_hex(32)
            expires = now + int(policy.lease_seconds)
            self._connection.execute(
                """INSERT INTO wake_attempts(
                       attempt_id, incident_id, generation, attempt_no, result_nonce_sha256,
                       lease_token_sha256, lease_owner, lease_acquired_at, lease_expires_at,
                       observation_sha256, state
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'leased')""",
                (
                    attempt_id,
                    row[0],
                    row[1],
                    attempt_no,
                    hashlib.sha256(result_nonce.encode()).hexdigest(),
                    hashlib.sha256(lease_token.encode()).hexdigest(),
                    router_job_id,
                    now,
                    expires,
                    row[7],
                ),
            )
            self._connection.execute(
                """INSERT INTO wake_budgets(mission_id, day, used) VALUES (?, ?, 1)
                   ON CONFLICT(mission_id, day) DO UPDATE SET used=used+1""",
                (row[2], day),
            )
            self._connection.execute(
                """UPDATE incidents SET lifecycle='wake_sent', attempt_count=?, updated_at=?
                   WHERE incident_id=? AND generation=? AND lifecycle='detected'""",
                (attempt_no, now, row[0], row[1]),
            )
            self._connection.commit()
            expires_text = datetime.fromtimestamp(expires, UTC).isoformat().replace("+00:00", "Z")
            return {
                "wakeAgent": True,
                "context": {
                    "protocol": "cyclops-manager-ack/v1",
                    "incident_id": str(row[0]),
                    "generation": int(row[1]),
                    "attempt_id": attempt_id,
                    "attempt_no": attempt_no,
                    "result_nonce": result_nonce,
                    "lease_token": lease_token,
                    "lease_expires_at": expires_text,
                    "observation_sha256": str(row[7]),
                    "kind": str(row[4]),
                    "mission_id": str(row[2]),
                    "phase_key": str(row[3]),
                    "subject_task_id": None if row[5] is None else str(row[5]),
                    "subject_run_id": None if row[6] is None else str(row[6]),
                    "expected_state": str(row[8]),
                    "observed_state": str(row[9]),
                    "allowed_recommendations": ["NOOP", "ESCALATE"],
                },
            }
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise LedgerError("manager lease could not be committed") from exc

    def manager_budget(self, mission_id: str, day: str) -> int:
        row = self._connection.execute(
            "SELECT used FROM wake_budgets WHERE mission_id=? AND day=?", (mission_id, day)
        ).fetchone()
        return 0 if row is None else int(row[0])

    def manager_attempt(self, attempt_id: str) -> dict[str, object] | None:
        row = self._connection.execute(
            """SELECT attempt_id, incident_id, generation, attempt_no, lease_token_sha256,
                      result_nonce_sha256, lease_owner, lease_acquired_at, lease_expires_at,
                      observation_sha256,
                      cron_execution_id, state, error_code
               FROM wake_attempts WHERE attempt_id=?""",
            (attempt_id,),
        ).fetchone()
        keys = (
            "attempt_id",
            "incident_id",
            "generation",
            "attempt_no",
            "lease_token_sha256",
            "result_nonce_sha256",
            "lease_owner",
            "lease_acquired_at",
            "lease_expires_at",
            "observation_sha256",
            "cron_execution_id",
            "state",
            "error_code",
        )
        return None if row is None else dict(zip(keys, row, strict=True))

    def current_manager_attempt(self) -> dict[str, object] | None:
        rows = self._connection.execute(
            "SELECT attempt_id FROM wake_attempts WHERE state='leased' ORDER BY lease_acquired_at LIMIT 2"
        ).fetchall()
        if len(rows) != 1:
            return None
        return self.manager_attempt(str(rows[0][0]))

    def manager_incident(self, incident_id: str, generation: int) -> dict[str, object] | None:
        rows = self.manager_incidents(incident_id=incident_id, generation=generation)
        return None if not rows else rows[0]

    def manager_incidents(
        self,
        *,
        incident_id: str | None = None,
        generation: int | None = None,
        mission_id: str | None = None,
    ) -> list[dict[str, object]]:
        query = """SELECT incident_id, generation, mission_id, phase_key, kind, severity,
                          subject_task_id, subject_run_id, observation_sha256, first_tick,
                          last_tick, observed_ticks, disposition, lifecycle, terminal_reason, reason_code,
                          human_question_code, attempt_count, next_attempt_at, acknowledged_at,
                          terminal_at
                   FROM incidents"""
        clauses: list[str] = []
        parameter_values: list[object] = []
        if incident_id is not None and generation is not None:
            clauses.append("incident_id=? AND generation=?")
            parameter_values.extend((incident_id, generation))
        if mission_id is not None:
            clauses.append("mission_id=?")
            parameter_values.append(mission_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY incident_id, generation"
        keys = (
            "incident_id",
            "generation",
            "mission_id",
            "phase_key",
            "kind",
            "severity",
            "subject_task_id",
            "subject_run_id",
            "observation_sha256",
            "first_tick",
            "last_tick",
            "observed_ticks",
            "disposition",
            "lifecycle",
            "terminal_reason",
            "reason_code",
            "human_question_code",
            "attempt_count",
            "next_attempt_at",
            "acknowledged_at",
            "terminal_at",
        )
        result: list[dict[str, object]] = []
        for row in self._connection.execute(query, tuple(parameter_values)).fetchall():
            item = dict(zip(keys, row, strict=True))
            attempt = self._connection.execute(
                """SELECT state FROM wake_attempts WHERE incident_id=? AND generation=?
                   ORDER BY attempt_no DESC LIMIT 1""",
                (item["incident_id"], item["generation"]),
            ).fetchone()
            state = None if attempt is None else str(attempt[0])
            item["manager_state"] = (
                "ack_valid"
                if state == "ack_valid"
                else "leased"
                if state == "leased"
                else "failed"
                if item["lifecycle"] == "dead_letter"
                else "retry_wait"
                if state == "expired" and item["lifecycle"] == "detected"
                else "idle"
            )
            notification = self._connection.execute(
                """SELECT state FROM notification_intents
                   WHERE incident_id=? AND generation=? ORDER BY created_at DESC LIMIT 1""",
                (item["incident_id"], item["generation"]),
            ).fetchone()
            item["notification_state"] = "none" if notification is None else str(notification[0])
            result.append(item)
        return result

    def supersede_manager_attempt(self, attempt_id: str) -> None:
        self._connection.execute(
            "UPDATE wake_attempts SET state='superseded' WHERE attempt_id=? AND state='leased'",
            (attempt_id,),
        )
        self._connection.commit()

    def resolve_manager_result(
        self, *, attempt_id: str, cron_execution_id: str, now: float
    ) -> None:
        """Atomically record a late result after the authoritative condition cleared."""
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            attempt = self._connection.execute(
                "SELECT incident_id, generation FROM wake_attempts WHERE attempt_id=? AND state='leased'",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise LedgerError("manager result attempt changed concurrently")
            cursor = self._connection.execute(
                """UPDATE wake_attempts SET state='superseded', cron_execution_id=?
                   WHERE attempt_id=? AND state='leased'""",
                (cron_execution_id, attempt_id),
            )
            if cursor.rowcount != 1:
                raise LedgerError("manager result attempt changed concurrently")
            self._connection.execute(
                """UPDATE incidents SET lifecycle='resolved', disposition='resolved',
                       terminal_reason='condition_cleared', terminal_at=?, updated_at=?, clean_tick=1
                   WHERE incident_id=? AND generation=? AND lifecycle='wake_sent'""",
                (now, now, attempt[0], attempt[1]),
            )
            self._connection.commit()
        except LedgerError:
            self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise LedgerError("manager result could not be committed") from exc

    def accept_manager_ack(
        self,
        *,
        attempt_id: str,
        cron_execution_id: str,
        terminal: str,
        reason_code: str,
        human_question_code: str,
        now: float,
    ) -> None:
        from .manager import decision_packet_id

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            attempt = self._connection.execute(
                "SELECT incident_id, generation FROM wake_attempts WHERE attempt_id=? AND state='leased'",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise LedgerError("manager ACK attempt changed concurrently")
            self._connection.execute(
                """UPDATE wake_attempts SET state='ack_valid', cron_execution_id=?
                   WHERE attempt_id=? AND state='leased'""",
                (cron_execution_id, attempt_id),
            )
            cursor = self._connection.execute(
                """UPDATE incidents SET lifecycle=?, disposition='resolved', reason_code=?,
                       human_question_code=?, terminal_reason='manager_ack', terminal_at=?, updated_at=?
                   WHERE incident_id=? AND generation=? AND lifecycle='wake_sent'""",
                (terminal, reason_code, human_question_code, now, now, attempt[0], attempt[1]),
            )
            if cursor.rowcount != 1:
                raise LedgerError("manager incident changed concurrently")
            if terminal == "human_required":
                self._insert_notification_intent(
                    incident_id=str(attempt[0]),
                    generation=int(attempt[1]),
                    terminal=terminal,
                    decision_packet_id=decision_packet_id(
                        str(attempt[0]), int(attempt[1]), terminal
                    ),
                    now=now,
                )
            self._connection.commit()
        except LedgerError:
            self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise LedgerError("manager ACK could not be committed") from exc

    def create_notification_intent(
        self,
        *,
        incident_id: str,
        generation: int,
        terminal: str,
        decision_packet_id: str,
        now: float,
    ) -> None:
        self._insert_notification_intent(
            incident_id=incident_id,
            generation=generation,
            terminal=terminal,
            decision_packet_id=decision_packet_id,
            now=now,
        )
        self._connection.commit()

    def _insert_notification_intent(
        self,
        *,
        incident_id: str,
        generation: int,
        terminal: str,
        decision_packet_id: str,
        now: float,
    ) -> None:
        self._connection.execute(
            """INSERT OR IGNORE INTO notification_intents(
                   incident_id, generation, terminal_kind, decision_packet_id, state, created_at
               ) VALUES (?, ?, ?, ?, 'pending', ?)""",
            (incident_id, generation, terminal, decision_packet_id, now),
        )

    def lease_notification(self, *, now: float) -> dict[str, object] | None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """UPDATE notification_intents SET state='pending', lease_acquired_at=NULL,
                       lease_expires_at=NULL, next_attempt_at=?
                   WHERE state='leased' AND lease_expires_at<=? AND attempt_count<2""",
                (now + 300, now),
            )
            self._connection.execute(
                """UPDATE notification_intents SET state='failed'
                   WHERE state='leased' AND lease_expires_at<=? AND attempt_count>=2""",
                (now,),
            )
            row = self._connection.execute(
                """SELECT n.incident_id, n.generation, n.terminal_kind,
                          n.decision_packet_id, i.kind, i.severity, i.mission_id, i.phase_key,
                          i.reason_code, i.human_question_code, i.observed_ticks, i.attempt_count
                   FROM notification_intents n JOIN incidents i
                     ON i.incident_id=n.incident_id AND i.generation=n.generation
                   WHERE n.state='pending' AND n.attempt_count<2 AND n.next_attempt_at<=?
                   ORDER BY n.created_at, n.decision_packet_id LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                self._connection.commit()
                return None
            self._connection.execute(
                """UPDATE notification_intents SET state='leased', attempt_count=attempt_count+1,
                       lease_acquired_at=?, lease_expires_at=?
                   WHERE incident_id=? AND generation=? AND terminal_kind=? AND state='pending'""",
                (now, now + 300, row[0], row[1], row[2]),
            )
            self._connection.commit()
            return self._notification_packet(row)
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise LedgerError("notification lease could not be committed") from exc

    @staticmethod
    def _notification_packet(row: tuple[Any, ...]) -> dict[str, object]:
        return {
            "packet_version": 1,
            "decision_packet_id": str(row[3]),
            "incident_id": str(row[0]),
            "generation": int(row[1]),
            "kind": str(row[4]),
            "severity": str(row[5]),
            "mission_id": str(row[6]),
            "phase_key": str(row[7]),
            "terminal": str(row[2]),
            "reason_code": str(row[8] or "AMBIGUOUS_STATE"),
            "human_question_code": str(row[9] or "REVIEW_INCIDENT"),
            "observed_ticks": int(row[10]),
            "attempt_count": int(row[11]),
        }

    def current_notification(self) -> dict[str, object] | None:
        rows = self._connection.execute(
            """SELECT n.incident_id, n.generation, n.terminal_kind, n.decision_packet_id,
                      i.kind, i.severity, i.mission_id, i.phase_key, i.reason_code,
                      i.human_question_code, i.observed_ticks, i.attempt_count,
                      n.lease_acquired_at, n.lease_expires_at, n.attempt_count
               FROM notification_intents n JOIN incidents i
                 ON i.incident_id=n.incident_id AND i.generation=n.generation
               WHERE n.state='leased' ORDER BY n.lease_acquired_at LIMIT 2"""
        ).fetchall()
        if len(rows) != 1:
            return None
        return {
            "packet": self._notification_packet(rows[0]),
            "lease_acquired_at": float(rows[0][12]),
            "lease_expires_at": float(rows[0][13]),
            "delivery_attempt_count": int(rows[0][14]),
        }

    def record_notification_result(
        self,
        decision_packet_id: str,
        *,
        courier_execution_id: str | None,
        outcome: str | None = None,
        now: float = 0.0,
        delivered: bool | None = None,
    ) -> None:
        if outcome is None and delivered is not None:
            outcome = "delivered" if delivered else "failed"
        if outcome not in {"delivered", "failed", "not_configured", "suppressed", "malformed"}:
            raise LedgerError("notification result outcome is invalid")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT attempt_count FROM notification_intents WHERE decision_packet_id=? AND state='leased'",
                (decision_packet_id,),
            ).fetchone()
            if row is None:
                replay = self._connection.execute(
                    "SELECT state, courier_execution_id FROM notification_intents WHERE decision_packet_id=?",
                    (decision_packet_id,),
                ).fetchone()
                if replay == ("sent", courier_execution_id):
                    self._connection.commit()
                    return
                raise LedgerError("notification result fence mismatch")
            if outcome == "delivered":
                state, next_attempt = "sent", 0.0
            elif int(row[0]) >= 2:
                state, next_attempt = "failed", 0.0
            else:
                state, next_attempt = "pending", now + 300
            self._connection.execute(
                """UPDATE notification_intents SET state=?, courier_execution_id=?,
                       last_outcome=?, next_attempt_at=?, lease_acquired_at=NULL,
                       lease_expires_at=NULL WHERE decision_packet_id=? AND state='leased'""",
                (state, courier_execution_id, outcome, next_attempt, decision_packet_id),
            )
            self._connection.commit()
        except LedgerError:
            self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise LedgerError("notification result could not be committed") from exc

    def acknowledge_incident(self, incident_id: str, generation: int, *, now: float) -> None:
        cursor = self._connection.execute(
            """UPDATE incidents SET acknowledged_at=?, updated_at=?
               WHERE incident_id=? AND generation=?
                 AND lifecycle IN ('resolved', 'human_required', 'dead_letter')""",
            (now, now, incident_id, generation),
        )
        if cursor.rowcount != 1:
            self._connection.rollback()
            raise LedgerError("incident acknowledgement fence mismatch")
        self._connection.execute(
            """UPDATE notification_intents SET state='acknowledged'
               WHERE incident_id=? AND generation=? AND state='sent'""",
            (incident_id, generation),
        )
        self._connection.commit()


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
