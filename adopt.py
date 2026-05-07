"""
Reconciliation between Soularr's State store and slskd's reality.

Two cases this handles, both at the start of a cycle (before the wanted-list
filter runs):

  1. State has an album marked downloading, but the slskd transfers are gone
     (user cancelled / removed them from the slskd UI). The album entry needs
     to drop so the next phase can either find a replacement or re-grab.

  2. The user manually grabbed an album in the slskd UI (or a previous
     Soularr cycle picked one before we had Phase 1 dedup). slskd has an
     active download but state.albums doesn't know about it. Without
     adoption, Soularr would re-grab the same album from another user at
     the next cycle.

The adoption flow:
  - Walk slskd's active downloads, build a list of (user, full_path, files)
    candidates that aren't already tracked by state.
  - For each Lidarr wanted album that's NOT in state, fuzzy-match the album
    metadata (artist + title) against each candidate's slskd path.
  - On a match, register the slskd transfers as state.albums so the
    subsequent filter_list call treats the album as in-flight.

Fuzzy match runs against the FULL slskd path (e.g. "music\\Currents\\[2020]
The Way It Ends"), so artist segments embedded in the path help disambiguate.
"""

import difflib
import logging
from state import State

logger = logging.getLogger("adopt")

# Transfer states slskd uses; everything that starts with "Completed," is
# terminal. We treat "Completed, Succeeded" as still "alive" for adoption
# purposes because Soularr's existing flow needs to import those.
TERMINAL_BAD = (
    "Completed, Cancelled",
    "Completed, TimedOut",
    "Completed, Errored",
    "Completed, Aborted",
    "Completed, Rejected",
)


def _is_terminal_bad(state_str: str) -> bool:
    return any(state_str.startswith(t) for t in TERMINAL_BAD)


def _is_alive(state_str: str) -> bool:
    """True for Queued / Requested / InProgress / Initializing / Completed,Succeeded."""
    if not state_str:
        return False
    if _is_terminal_bad(state_str):
        return False
    return True


def _fuzzy_score(slskd_path: str, artist: str, title: str) -> float:
    """
    Score how well a slskd directory path matches an artist+title pair.
    The path uses backslashes; we lowercase everything for a case-insensitive
    SequenceMatcher comparison and add a bonus when the album title appears
    verbatim in the leaf folder.
    """
    path = (slskd_path or "").lower().replace("\\", " ").replace("_", " ").replace("/", " ")
    target = f"{artist} {title}".lower()
    base = difflib.SequenceMatcher(None, target, path).ratio()
    title_l = title.lower()
    if title_l and title_l in path:
        # Strong evidence; raise the floor.
        base = max(base, 0.85)
    artist_l = artist.lower()
    if artist_l and artist_l in path:
        base = min(1.0, base + 0.05)
    return base


def _build_slskd_index(slskd):
    """
    Snapshot slskd downloads into:
      - by_id: {transfer_id: state_str}
      - candidates: [{user, full_path, leaf, files: [{id,filename,size,state}]}]
    Only directories with at least one alive transfer make the candidate list.
    """
    try:
        all_dl = slskd.transfers.get_all_downloads()
    except Exception:
        logger.warning("Could not fetch slskd downloads", exc_info=True)
        return {}, []

    by_id = {}
    candidates = []
    for u in all_dl or []:
        user = u.get("username") or ""
        for d in u.get("directories", []):
            files = d.get("files", []) or []
            alive = []
            for f in files:
                tid = f.get("id")
                state = (f.get("state") or "").strip()
                if tid:
                    by_id[tid] = state
                if tid and _is_alive(state):
                    alive.append(f)
            if not alive:
                continue
            full_path = d.get("directory") or ""
            leaf = full_path.replace("/", "\\").split("\\")[-1]
            candidates.append({
                "user": user,
                "full_path": full_path,
                "leaf": leaf,
                "files": alive,
            })
    return by_id, candidates


def _refresh_existing_state(state: State, by_id: dict):
    """
    For each tracked album, sync each transfer's state from slskd. Transfers
    that no longer exist in slskd (deleted by the user) are marked as
    Cancelled. _compute_album_state then collapses the doc to the right
    overall state, and cleanup_terminal removes it if everything is gone.
    """
    for doc in state.list_albums():
        album_id = doc["album_id"]
        snapshot = {}
        for tid in (doc.get("transfers") or {}).keys():
            snapshot[tid] = by_id.get(tid, "Completed, Cancelled")
        if snapshot:
            state.update_transfers_bulk(album_id, snapshot)
        # If the album is now terminal-and-not-imported, drop it. Successful
        # imports are handled by the rest of the pipeline; we only clean up
        # plainly failed/abandoned entries.
        refreshed = state.get_album(album_id)
        if refreshed and refreshed.get("state") in ("failed", "abandoned"):
            state.cleanup_terminal(album_id)


def _register_candidate(state: State, lidarr, album_id: int, candidate: dict):
    """Translate a slskd candidate into a state.register_grab call."""
    try:
        a = lidarr.get_album(album_id)
        if isinstance(a, list):
            a = a[0] if a else None
        if not a:
            return False
    except Exception:
        return False
    artist = (a.get("artist") or {}).get("artistName") or ""
    title = a.get("title") or ""
    year = (a.get("releaseDate") or "")[:4]

    transfers = {}
    for f in candidate["files"]:
        tid = f.get("id")
        if not tid:
            continue
        full = f.get("filename", "")
        if "\\" in full:
            file_dir, basename = full.rsplit("\\", 1)
        else:
            file_dir, basename = "", full
        transfers[tid] = {
            "filename": basename,
            "file_dir": file_dir,
            "size": f.get("size", 0),
            "state": (f.get("state") or "Queued, Locally").strip(),
            "imported": False,
        }
    if not transfers:
        return False

    state.register_grab(
        album_id=album_id,
        artist=artist,
        title=title,
        year=year,
        current_user=candidate["user"],
        transfers=transfers,
    )
    return True


def sync_state_with_slskd(
    state: State,
    slskd,
    lidarr,
    wanted_album_ids: set,
    fuzzy_threshold: float = 0.7,
) -> int:
    """
    Reconcile state.albums with slskd's reality. Returns the number of newly
    adopted albums. Should be called at the start of every cycle, after the
    wanted list is fetched but before filter_list.
    """
    by_id, candidates = _build_slskd_index(slskd)

    # Phase 1: refresh existing state, drop entries whose transfers are gone.
    _refresh_existing_state(state, by_id)

    # Phase 2: discover and adopt active slskd downloads that match a wanted
    # album we don't yet track. Use tracked_album_ids (broader than in-flight)
    # so an album whose transfers all completed but Lidarr hasn't imported
    # yet (state=SUCCEEDED) doesn't get re-adopted with fresh transfers.
    tracked = state.tracked_album_ids()
    needs = [aid for aid in wanted_album_ids if aid not in tracked]
    if not needs or not candidates:
        return 0

    tracked_leaves = state.get_tracked_folder_names()
    candidates = [c for c in candidates if c["leaf"] not in tracked_leaves]
    if not candidates:
        return 0

    adopted = 0
    for album_id in needs:
        try:
            a = lidarr.get_album(album_id)
            if isinstance(a, list):
                a = a[0] if a else None
            if not a:
                continue
        except Exception:
            continue
        title = a.get("title", "")
        artist = (a.get("artist") or {}).get("artistName", "")

        best = None
        best_score = 0.0
        for c in candidates:
            score = _fuzzy_score(c["full_path"], artist, title)
            if score > best_score:
                best_score = score
                best = c

        if best and best_score >= fuzzy_threshold:
            if _register_candidate(state, lidarr, album_id, best):
                logger.info(
                    f"Adopted slskd download '{best['leaf']}' from {best['user']} "
                    f"as album_id={album_id} ({artist} - {title}, score={best_score:.2f})"
                )
                adopted += 1
                # Don't let the same candidate adopt twice.
                candidates = [c for c in candidates if c["full_path"] != best["full_path"]]

    return adopted
