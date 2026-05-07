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
import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import RLock
from tinydb import Query, TinyDB

DB_FILENAME = "soularr.db.json"
LOCK_FILENAME = "soularr.db.lock"
SCHEMA_VERSION = 2


# Album-level states
STATE_QUEUED = "queued"
STATE_DOWNLOADING = "downloading"
STATE_PARTIAL = "partial"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_ABANDONED = "abandoned"

# Album-level states still seeing slskd transfer activity. Used by the orphan
# scan to mark a folder as `downloading` (skip auto-import; let
# monitor_downloads finish the grab). PARTIAL is intentionally excluded —
# it means "some succeeded, some bad, nothing active" so the slskd grab is
# done evolving and the orphan flow should import what arrived.
IN_FLIGHT_STATES = {STATE_QUEUED, STATE_DOWNLOADING}
TERMINAL_STATES = {STATE_SUCCEEDED, STATE_FAILED, STATE_ABANDONED}

# Albums that should still be considered "owned" by soularr/slskd: anything
# that hasn't been cleaned up via cleanup_terminal yet. Used by filter_list
# (don't re-grab) and adopt (don't re-adopt) to avoid spawning duplicate
# downloads while a tracker exists. PARTIAL and SUCCEEDED both mean files
# are on disk awaiting an import that will eventually clear the tracker.
TRACKED_STATES = IN_FLIGHT_STATES | {STATE_SUCCEEDED, STATE_PARTIAL}


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
    @staticmethod
    def legacy_orphan_id(doc: dict) -> str:
        """Compute a stable composite id for a legacy orphan record so the v2
        migration can give it the same shape as freshly-scanned entries.

        Uses (artist, album, format) when present (post-Phase 2c records) and
        falls back to the folder_path otherwise (pre-Phase 2c audit entries).
        Mirrors orphans._orphan_id but lives here so state.py stays free of
        the orphans module dependency.
        """
        artist = (doc.get("artist") or "").lower().strip()
        album = (doc.get("album") or "").lower().strip()
        fmt = doc.get("format") or ""
        if artist and album:
            raw = f"{artist}|{album}|{fmt}"
        else:
            raw = "__legacy__|" + (doc.get("folder_path") or "")
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def migrate_legacy(self):
        Q = Query()
        with self._flock():
            v_doc = self._runtime.get(Q.key == "schema_version")
            current_v = v_doc.get("value") if v_doc else 0

            if current_v < 1:
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

            if current_v < 2:
                # v2: orphan records gain a composite `id` derived from
                # (artist, album, format) so a release is uniquely keyed
                # regardless of where its files sit on disk.
                #
                # Legacy records (pre-v2) don't carry artist/album/format —
                # they were keyed by folder_path and only stored matched_album_id
                # plus rejections. Fresh scans will re-detect these folders and
                # create proper composite-keyed entries, so we drop the legacy
                # records here EXCEPT when their status reflects an explicit
                # user decision (ignored / deleted) — those are audit records
                # we preserve under a synthetic `__legacy__` id.
                user_statuses = {"ignored", "deleted"}
                for doc in list(self._orphans.all()):
                    if doc.get("id"):
                        continue
                    has_metadata = bool(doc.get("artist") and doc.get("album"))
                    is_user_choice = doc.get("status") in user_statuses
                    if has_metadata or is_user_choice:
                        self._orphans.update(
                            {"id": self.legacy_orphan_id(doc)},
                            doc_ids=[doc.doc_id],
                        )
                    else:
                        self._orphans.remove(doc_ids=[doc.doc_id])

            self._runtime.upsert(
                {"key": "schema_version", "value": SCHEMA_VERSION},
                Q.key == "schema_version",
            )

    # ------------------------------------------------------------------
    # Albums — in-flight grab tracking (Phase 1+)
    # ------------------------------------------------------------------
    def is_in_flight(self, album_id: int) -> bool:
        """True when soularr/slskd is already managing this album: actively
        downloading OR succeeded but not yet imported. filter_list uses this
        to avoid re-grabbing files that are on disk awaiting an orphan import
        — a Lidarr rejection that fails the import stays SUCCEEDED until the
        user clears it via the UI, and we don't want to start a new download
        cycle in the meantime."""
        Q = Query()
        with self._tlock:
            doc = self._albums.get(Q.album_id == album_id)
            return bool(doc) and doc.get("state") in TRACKED_STATES

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

    def list_albums(self) -> list:
        with self._tlock:
            return self._albums.all()

    def in_flight_album_ids(self) -> set:
        """Albums whose transfers are still active in slskd. Used by orphan
        scan to mark on-disk files as `downloading` (don't auto-import yet —
        monitor_downloads will handle it once everything finishes)."""
        Q = Query()
        with self._tlock:
            return {
                d["album_id"]
                for d in self._albums.search(Q.state.one_of(list(IN_FLIGHT_STATES)))
            }

    def tracked_album_ids(self) -> set:
        """Superset of in_flight_album_ids that also includes SUCCEEDED — i.e.
        any album with a state.albums entry that hasn't been cleaned up.
        Used by adopt to avoid re-registering an album already on disk."""
        Q = Query()
        with self._tlock:
            return {
                d["album_id"]
                for d in self._albums.search(Q.state.one_of(list(TRACKED_STATES)))
            }

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
        return self.get_runtime("current_page", default)

    def set_current_page(self, page: int):
        self.set_runtime("current_page", page)

    def get_runtime(self, key: str, default=None):
        """Generic key/value lookup in the runtime table."""
        Q = Query()
        with self._tlock:
            doc = self._runtime.get(Q.key == key)
            return doc["value"] if doc else default

    def set_runtime(self, key: str, value):
        Q = Query()
        with self._flock():
            self._runtime.upsert({"key": key, "value": value}, Q.key == key)

    # ------------------------------------------------------------------
    # Tracked-folder lookup (used by orphan scan to skip in-flight folders)
    # ------------------------------------------------------------------
    def get_tracked_folder_names(self) -> set:
        """
        Return the set of slskd local-folder names (the last segment of file_dir)
        that correspond to currently in-flight transfers. Orphan scan should skip
        any /downloads subfolder whose name appears here.
        """
        names = set()
        with self._tlock:
            for album in self._albums.all():
                for t in album.get("transfers", {}).values():
                    fd = t.get("file_dir") or ""
                    if not fd:
                        continue
                    last = fd.replace("/", "\\").split("\\")[-1]
                    if last:
                        names.add(last)
        return names

    # ------------------------------------------------------------------
    # Orphans — Phase 2b
    # ------------------------------------------------------------------
    # Status values written into the orphans table.
    # Auto-import success is NOT recorded — the folder is deleted afterwards so
    # there is nothing to track. Anything else either awaits user action via the
    # orphans UI page or is already at a terminal state.
    ORPHAN_STATUS_PENDING = "pending"            # detected, not in wanted list — awaits UI action
    ORPHAN_STATUS_PARTIAL_IMPORTED = "partial_imported"  # auto-imported some, but residual audio remains
    ORPHAN_STATUS_NO_MATCH = "no_match"          # was wanted but Lidarr rejected every file
    ORPHAN_STATUS_ERROR = "error"                # ManualImport command failed / timed out
    ORPHAN_STATUS_EMPTY = "empty"                # no audio file in folder
    ORPHAN_STATUS_IGNORED = "ignored"            # user opted out via UI
    ORPHAN_STATUS_DELETED = "deleted"            # user deleted folder via UI (audit trail)

    # Pending orphans are RE-EVALUATED on every scan because the wanted list is
    # mutable: an album the user adds or re-monitors should auto-import the next
    # time we see its folder. Everything else is terminal until the user clears
    # the entry from the UI.
    _ORPHAN_TERMINAL_STATUSES = {
        ORPHAN_STATUS_PARTIAL_IMPORTED,
        ORPHAN_STATUS_NO_MATCH,
        ORPHAN_STATUS_ERROR,
        ORPHAN_STATUS_EMPTY,
        ORPHAN_STATUS_IGNORED,
        ORPHAN_STATUS_DELETED,
    }

    # Orphan statuses that mean "files are on disk and pending some action" —
    # if any orphan record with matched_album_id == X has one of these, soularr
    # should NOT grab a fresh copy of album X. The orphan flow (or user) will
    # handle import; re-grabbing wastes bandwidth and creates duplicates.
    _ORPHAN_BLOCKS_GRAB = {
        "pending",
        "downloading",
        "no_match",
        "partial_imported",
        "error",
    }

    def has_orphan_blocking_grab(self, album_id: int) -> bool:
        """True when an existing orphan record for this album means a fresh
        slskd grab would be redundant. Used by filter_list."""
        Q = Query()
        with self._tlock:
            for doc in self._orphans.search(Q.matched_album_id == album_id):
                if doc.get("status") in self._ORPHAN_BLOCKS_GRAB:
                    return True
        return False

    def is_orphan_resolved(self, orphan_id: str) -> bool:
        Q = Query()
        with self._tlock:
            doc = self._orphans.get(Q.id == orphan_id)
            return bool(doc) and doc.get("status") in self._ORPHAN_TERMINAL_STATUSES

    def mark_orphan_scanned(
        self,
        orphan_id: str,
        folder_path: str = None,
        status: str = None,
        artist: str = None,
        album: str = None,
        album_format: str = None,
        matched_album_id: int = None,
        lidarr_command_id: int = None,
        imported_count: int = None,
        rejections: list = None,
    ):
        """
        Upsert an orphan record. Identity is the composite `orphan_id` derived
        from (artist, album, format) — see orphans._orphan_id. The on-disk
        `folder_path` is stored alongside for filesystem actions but is NOT
        the primary key, so a folder rename/move doesn't fork the entry.

        Fields left as None are preserved when updating an existing record;
        callers performing a status-only update (ignore / delete) don't need
        to re-supply artist/album/folder_path.
        """
        Q = Query()
        with self._flock():
            existing = self._orphans.get(Q.id == orphan_id) or {}
            doc = dict(existing)
            doc["id"] = orphan_id
            doc["scanned_at"] = _now()
            for key, value in (
                ("folder_path", folder_path),
                ("status", status),
                ("artist", artist),
                ("album", album),
                ("format", album_format),
                ("matched_album_id", matched_album_id),
                ("lidarr_command_id", lidarr_command_id),
                ("imported_count", imported_count),
            ):
                if value is not None:
                    doc[key] = value
            if rejections is not None:
                doc["rejections"] = list(rejections)
            self._orphans.upsert(doc, Q.id == orphan_id)

    def get_orphan(self, orphan_id: str) -> dict:
        Q = Query()
        with self._tlock:
            return self._orphans.get(Q.id == orphan_id)

    def list_orphans(self) -> list:
        with self._tlock:
            return self._orphans.all()

    def remove_orphan(self, orphan_id: str):
        """Drop an orphan entry entirely (used after a fully successful auto-import)."""
        Q = Query()
        with self._flock():
            self._orphans.remove(Q.id == orphan_id)
