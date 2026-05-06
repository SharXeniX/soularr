"""
Persistent state store for Soularr, backed by TinyDB.

Tables
------
albums          In-flight grab tracking, per Lidarr album.
failed_imports  Albums whose Lidarr import failed (migrated from failed_imports.json).
orphans         Download folders that have been scanned at least once (Phase 2b).
runtime         Singleton key/value pairs: current_page, schema_version, etc.

Single file at <var_dir>/soularr.db.json. Inter-process safety provided by an
fcntl flock around every write so soularr.py and webui.py don't clobber each
other.
"""

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import RLock
from tinydb import Query, TinyDB

DB_FILENAME = "soularr.db.json"
LOCK_FILENAME = "soularr.db.lock"
SCHEMA_VERSION = 1


# Album-level states
STATE_QUEUED = "queued"
STATE_DOWNLOADING = "downloading"
STATE_PARTIAL = "partial"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_ABANDONED = "abandoned"

IN_FLIGHT_STATES = {STATE_QUEUED, STATE_DOWNLOADING, STATE_PARTIAL}
TERMINAL_STATES = {STATE_SUCCEEDED, STATE_FAILED, STATE_ABANDONED}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, var_dir: str):
        self.var_dir = var_dir
        self._db_path = os.path.join(var_dir, DB_FILENAME)
        self._lock_path = os.path.join(var_dir, LOCK_FILENAME)
        self._db = TinyDB(self._db_path)
        self._albums = self._db.table("albums")
        self._failed = self._db.table("failed_imports")
        self._orphans = self._db.table("orphans")
        self._runtime = self._db.table("runtime")
        self._tlock = RLock()
        self.migrate_legacy()

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------
    @contextmanager
    def _flock(self):
        """Inter-process exclusive lock on the DB. Reentrant within a process."""
        with self._tlock:
            f = open(self._lock_path, "w")
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                f.close()

    # ------------------------------------------------------------------
    # Migration (idempotent — runs once when schema_version is missing)
    # ------------------------------------------------------------------
    def migrate_legacy(self):
        Q = Query()
        with self._flock():
            if self._runtime.get(Q.key == "schema_version"):
                return

            legacy_failed = os.path.join(self.var_dir, "failed_imports.json")
            if os.path.exists(legacy_failed):
                try:
                    with open(legacy_failed) as f:
                        data = json.load(f)
                    for entry in data.values():
                        self._failed.upsert(entry, Q.album_id == entry.get("album_id"))
                    shutil.move(legacy_failed, legacy_failed + ".migrated")
                except Exception:
                    pass

            # Legacy current page file (plain int, not JSON). Soularr historically uses
            # ".current_page.txt"; tolerate the older "current_page.json" too.
            for legacy_page in (
                os.path.join(self.var_dir, ".current_page.txt"),
                os.path.join(self.var_dir, "current_page.json"),
            ):
                if not os.path.exists(legacy_page):
                    continue
                try:
                    with open(legacy_page) as f:
                        raw = f.read().strip()
                    page = int(raw) if raw else 1
                    self._runtime.upsert(
                        {"key": "current_page", "value": page},
                        Q.key == "current_page",
                    )
                    shutil.move(legacy_page, legacy_page + ".migrated")
                    break
                except Exception:
                    pass

            self._runtime.insert({"key": "schema_version", "value": SCHEMA_VERSION})

    # ------------------------------------------------------------------
    # Albums — in-flight grab tracking (Phase 1+)
    # ------------------------------------------------------------------
    def is_in_flight(self, album_id: int) -> bool:
        Q = Query()
        with self._tlock:
            doc = self._albums.get(Q.album_id == album_id)
            return bool(doc) and doc.get("state") in IN_FLIGHT_STATES

    def register_grab(
        self,
        album_id: int,
        artist: str,
        title: str,
        year: str,
        current_user: str,
        transfers: dict,
        candidates: list = None,
    ):
        Q = Query()
        doc = {
            "album_id": album_id,
            "artist": artist,
            "title": title,
            "year": year,
            "state": STATE_DOWNLOADING,
            "first_seen": _now(),
            "last_updated": _now(),
            "current_user": current_user,
            "transfers": transfers,
            "candidates": candidates or [],
            "attempts": [],
        }
        with self._flock():
            self._albums.upsert(doc, Q.album_id == album_id)

    def update_transfers_bulk(self, album_id: int, transfers_by_id: dict):
        """Sync a snapshot {transfer_id: slskd_state_string} into the album doc."""
        Q = Query()
        with self._flock():
            doc = self._albums.get(Q.album_id == album_id)
            if not doc:
                return
            transfers = doc.get("transfers", {})
            for tid, slskd_state in transfers_by_id.items():
                if tid in transfers:
                    transfers[tid]["state"] = slskd_state
            doc["transfers"] = transfers
            doc["last_updated"] = _now()
            doc["state"] = self._compute_album_state(transfers)
            self._albums.upsert(doc, Q.album_id == album_id)

    def cleanup_terminal(self, album_id: int):
        Q = Query()
        with self._flock():
            self._albums.remove(Q.album_id == album_id)

    def get_album(self, album_id: int) -> dict:
        Q = Query()
        with self._tlock:
            return self._albums.get(Q.album_id == album_id)

    def all_in_flight(self) -> list:
        Q = Query()
        with self._tlock:
            return self._albums.search(Q.state.one_of(list(IN_FLIGHT_STATES)))

    @staticmethod
    def _compute_album_state(transfers: dict) -> str:
        if not transfers:
            return STATE_FAILED
        states = [t.get("state", "") for t in transfers.values()]
        succeeded = sum(1 for s in states if s.startswith("Completed, Succeeded"))
        terminal_bad = sum(1 for s in states if s.startswith("Completed,") and "Succeeded" not in s)
        in_flight = len(states) - succeeded - terminal_bad
        if in_flight > 0:
            return STATE_DOWNLOADING
        if succeeded == len(states):
            return STATE_SUCCEEDED
        if succeeded > 0:
            return STATE_PARTIAL
        return STATE_FAILED

    # ------------------------------------------------------------------
    # Failed imports (replaces failed_imports.json)
    # ------------------------------------------------------------------
    def is_in_failed_imports(self, album_id: int) -> bool:
        Q = Query()
        with self._tlock:
            return self._failed.contains(Q.album_id == album_id)

    def add_failed_import(
        self,
        album_id: int,
        artist: str,
        title: str,
        folder_path: str = "",
    ):
        Q = Query()
        entry = {
            "album_id": album_id,
            "artist": artist,
            "title": title,
            "failed_at": _now(),
            "folder_path": folder_path,
        }
        with self._flock():
            self._failed.upsert(entry, Q.album_id == album_id)

    def remove_failed_import(self, album_id: int) -> dict:
        Q = Query()
        with self._flock():
            doc = self._failed.get(Q.album_id == album_id)
            self._failed.remove(Q.album_id == album_id)
            return doc

    def list_failed_imports(self) -> list:
        with self._tlock:
            return self._failed.all()

    # ------------------------------------------------------------------
    # Runtime singletons (current_page etc.)
    # ------------------------------------------------------------------
    def get_current_page(self, default: int = 1) -> int:
        Q = Query()
        with self._tlock:
            doc = self._runtime.get(Q.key == "current_page")
            return doc["value"] if doc else default

    def set_current_page(self, page: int):
        Q = Query()
        with self._flock():
            self._runtime.upsert(
                {"key": "current_page", "value": page},
                Q.key == "current_page",
            )

    # ------------------------------------------------------------------
    # Orphans — Phase 2b stub
    # ------------------------------------------------------------------
    def is_orphan_scanned(self, folder_path: str) -> bool:
        Q = Query()
        with self._tlock:
            return self._orphans.contains(Q.folder_path == folder_path)

    def mark_orphan_scanned(self, folder_path: str, matched_album_id: int = None):
        Q = Query()
        with self._flock():
            self._orphans.upsert(
                {
                    "folder_path": folder_path,
                    "scanned_at": _now(),
                    "matched_album_id": matched_album_id,
                },
                Q.folder_path == folder_path,
            )
