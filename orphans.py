"""
Orphan recovery for Soularr.

An "orphan" is a subfolder of the slskd downloads directory that contains
audio files but is not tracked by the State store and has not been resolved
by a previous scan. This typically happens after Soularr restarts, after the
user manually grabs an album in the slskd UI, or when a previous Soularr
cycle fails to finalize an import.

Recovery flow (Option C — pre-match + trigger + verify):
  1. Walk the downloads dir, list folders that aren't tracked nor resolved.
  2. For each folder: read ID3 tags of the first audio file.
  3. Best-effort fuzzy match the metadata against Lidarr's library to record
     the intended album_id. The match is informational; we always trigger
     Lidarr's scan because Lidarr's own matching is often more permissive.
  4. POST /api/v1/command DownloadedAlbumsScan with the path Lidarr sees.
  5. Wait for the command and parse imported_count from the result message.
  6. Record the outcome in state.orphans so the same folder is not
     reprocessed on the next cycle.
"""

import difflib
import logging
import os
import re
import time
from typing import Optional

import music_tag

from state import State

logger = logging.getLogger("orphans")

AUDIO_EXTS = (".flac", ".mp3", ".m4a", ".ogg", ".opus")

# Folders under the downloads dir that should never be treated as orphans.
SKIP_FOLDER_PREFIXES = ("failed_imports", ".incomplete", ".")


def _to_lidarr_path(folder_path: str, soularr_downloads_dir: str, lidarr_downloads_dir: str) -> str:
    """Translate a soularr-POV path into the Lidarr-POV path."""
    rel = os.path.relpath(folder_path, soularr_downloads_dir)
    return os.path.join(lidarr_downloads_dir, rel)


def _first_audio_file(folder_path: str) -> Optional[str]:
    try:
        names = sorted(os.listdir(folder_path))
    except OSError:
        return None
    for name in names:
        if name.lower().endswith(AUDIO_EXTS):
            return os.path.join(folder_path, name)
    return None


def read_album_metadata(folder_path: str) -> Optional[dict]:
    """Return {artist, album, year} from the first audio file's ID3 tags, or None."""
    audio = _first_audio_file(folder_path)
    if not audio:
        return None
    try:
        f = music_tag.load_file(audio)
        artist = str(f.get("albumartist") or f.get("artist") or "").strip()
        album = str(f.get("album") or "").strip()
        year = str(f.get("year") or "").strip()
        if not artist or not album:
            return None
        return {"artist": artist, "album": album, "year": year}
    except Exception:
        logger.warning(f"Could not read tags from {audio}", exc_info=True)
        return None


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_lidarr_album_id(
    metadata: dict,
    lidarr,
    artist_match_ratio: float = 0.85,
    album_match_ratio: float = 0.85,
) -> Optional[int]:
    """
    Best-effort fuzzy match of {artist, album} metadata against Lidarr's library.
    Returns the album id when both ratios pass their threshold, else None.
    """
    if not metadata:
        return None
    artist_query = metadata["artist"]

    # pyarr exposes search via lookup_artist; older versions only have get_artist
    artist_results = None
    for fn_name in ("lookup_artist", "search_artist", "get_artist"):
        fn = getattr(lidarr, fn_name, None)
        if fn is None:
            continue
        try:
            if fn_name == "get_artist":
                artist_results = fn()  # whole library; we'll fuzzy match locally
            else:
                artist_results = fn(term=artist_query)
            break
        except Exception:
            continue

    if not artist_results:
        logger.info(f"Orphan: no Lidarr lookup result for artist '{artist_query}'")
        return None

    best_artist, best_artist_score = None, 0.0
    for ar in artist_results:
        name = ar.get("artistName") or ar.get("name") or ""
        s = _ratio(artist_query, name)
        if s > best_artist_score:
            best_artist_score = s
            best_artist = ar

    if not best_artist or best_artist_score < artist_match_ratio:
        logger.info(
            f"Orphan: artist '{artist_query}' best match score {best_artist_score:.2f} "
            f"below threshold {artist_match_ratio}"
        )
        return None

    artist_id = best_artist.get("id")
    if not artist_id:
        # Lookup hit MusicBrainz but the artist isn't in Lidarr's local DB.
        return None

    try:
        albums = lidarr.get_album(artistId=artist_id)
    except Exception:
        return None

    album_query = metadata["album"]
    best_album, best_album_score = None, 0.0
    for al in albums or []:
        title = al.get("title", "")
        s = _ratio(album_query, title)
        if s > best_album_score:
            best_album_score = s
            best_album = al

    if not best_album or best_album_score < album_match_ratio:
        logger.info(
            f"Orphan: artist '{best_artist.get('artistName')}' matched ({best_artist_score:.2f}) "
            f"but album '{album_query}' best score {best_album_score:.2f} below {album_match_ratio}"
        )
        return None

    return best_album.get("id")


def _wait_for_command(lidarr, command_id: int, timeout: int = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cmd = lidarr.get_command(command_id)
        except Exception:
            time.sleep(2)
            continue
        if cmd.get("status") in ("completed", "failed"):
            return cmd
        time.sleep(2)
    return {"status": "timeout", "message": ""}


def _parse_imported_count(message: str) -> int:
    """
    Extract the imported track count from Lidarr's command result message.
    Examples: "Manually imported 11 files" -> 11
              "Completed. 11 tracks imported." -> 11
              "No imports detected."          -> 0
    """
    if not message:
        return 0
    for pat in (
        r"(?:manually imported|imported)\s+(\d+)\s+(?:files|tracks)",
        r"(\d+)\s+tracks?\s+imported",
    ):
        m = re.search(pat, message, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return 0


def _manual_import(lidarr, folder_path_lidarr: str) -> tuple:
    """
    Use Lidarr's ManualImport flow which actually imports unattached files
    (DownloadedAlbumsScan needs a download-client tracking entry and silently
    refuses without one). Returns (command_id, candidate_count, accepted_count).
    """
    candidates = lidarr.request_get(
        f"manualimport?folder={folder_path_lidarr}"
    ) if hasattr(lidarr, "request_get") else None
    if candidates is None:
        # pyarr exposes manual import via raw HTTP; fall back to lidarr._session
        import urllib.parse
        url = (
            lidarr._session_url if hasattr(lidarr, "_session_url") else lidarr.host_url
        )
        api_key = (
            lidarr._session_api_key if hasattr(lidarr, "_session_api_key") else lidarr.api_key
        )
        # Use the underlying session if pyarr exposes one; otherwise build a request
        import requests
        r = requests.get(
            f"{url.rstrip('/')}/api/v1/manualimport",
            params={"folder": folder_path_lidarr},
            headers={"X-Api-Key": api_key},
            timeout=30,
        )
        r.raise_for_status()
        candidates = r.json()

    accepted = []
    for it in candidates:
        if it.get("rejections"):
            continue
        if not it.get("album") or not it.get("artist"):
            continue
        accepted.append({
            "path": it["path"],
            "artistId": it["artist"]["id"],
            "albumId": it["album"]["id"],
            "albumReleaseId": it.get("albumReleaseId"),
            "trackIds": [t["id"] for t in it.get("tracks", [])],
            "quality": it.get("quality"),
            "disableReleaseSwitching": False,
        })

    if not accepted:
        return (None, len(candidates), 0)

    cmd = lidarr.post_command(
        name="ManualImport",
        files=accepted,
        importMode="auto",
        replaceExistingFiles=False,
    )
    return (cmd.get("id"), len(candidates), len(accepted))


def find_orphan_folders(downloads_dir: str, state: State) -> list:
    """Return absolute paths of unresolved, untracked subfolders of downloads_dir."""
    try:
        entries = sorted(os.listdir(downloads_dir))
    except OSError:
        return []

    tracked = state.get_tracked_folder_names()
    candidates = []
    for name in entries:
        if any(name.startswith(p) for p in SKIP_FOLDER_PREFIXES):
            continue
        path = os.path.join(downloads_dir, name)
        if not os.path.isdir(path):
            continue
        if name in tracked:
            continue
        if state.is_orphan_resolved(path):
            continue
        candidates.append(path)
    return candidates


def process_orphan(
    folder_path: str,
    state: State,
    lidarr,
    soularr_downloads_dir: str,
    lidarr_downloads_dir: str,
    artist_match_ratio: float,
    album_match_ratio: float,
    command_timeout: int = 60,
) -> str:
    """Process one orphan folder end-to-end. Returns the status string written to state."""
    metadata = read_album_metadata(folder_path)
    if not metadata:
        logger.info(f"Orphan: no readable audio metadata in {folder_path}")
        state.mark_orphan_scanned(folder_path, status=State.ORPHAN_STATUS_EMPTY)
        return State.ORPHAN_STATUS_EMPTY

    logger.info(
        f"Orphan candidate: {folder_path} "
        f"(artist='{metadata['artist']}' album='{metadata['album']}')"
    )

    matched_id = find_lidarr_album_id(
        metadata, lidarr, artist_match_ratio, album_match_ratio
    )

    lidarr_path = _to_lidarr_path(folder_path, soularr_downloads_dir, lidarr_downloads_dir)
    try:
        command_id, candidate_count, accepted_count = _manual_import(lidarr, lidarr_path)
    except Exception:
        logger.exception(f"Orphan: failed to enqueue Lidarr ManualImport for {folder_path}")
        state.mark_orphan_scanned(
            folder_path,
            status=State.ORPHAN_STATUS_ERROR,
            matched_album_id=matched_id,
        )
        return State.ORPHAN_STATUS_ERROR

    if accepted_count == 0:
        # Folder had files but none survived Lidarr's matching/quality checks.
        state.mark_orphan_scanned(
            folder_path,
            status=State.ORPHAN_STATUS_NO_MATCH,
            matched_album_id=matched_id,
            imported_count=0,
        )
        logger.info(
            f"Orphan {folder_path} -> no_match (Lidarr accepted 0 of "
            f"{candidate_count} candidates)"
        )
        return State.ORPHAN_STATUS_NO_MATCH

    result = _wait_for_command(lidarr, command_id, timeout=command_timeout)
    imported = _parse_imported_count(result.get("message", ""))

    if result.get("status") == "timeout":
        status = State.ORPHAN_STATUS_ERROR
    elif imported > 0:
        status = State.ORPHAN_STATUS_IMPORTED
    else:
        status = State.ORPHAN_STATUS_NO_MATCH

    state.mark_orphan_scanned(
        folder_path,
        status=status,
        matched_album_id=matched_id,
        lidarr_command_id=command_id,
        imported_count=imported,
    )
    logger.info(
        f"Orphan {folder_path} -> status={status} imported={imported}/{accepted_count} "
        f"album_id={matched_id} cmd_id={command_id}"
    )
    return status


def process_all_orphans(
    soularr_downloads_dir: str,
    lidarr_downloads_dir: str,
    state: State,
    lidarr,
    artist_match_ratio: float = 0.85,
    album_match_ratio: float = 0.85,
    command_timeout: int = 60,
) -> int:
    """Entry point. Returns the count of folders processed this cycle."""
    candidates = find_orphan_folders(soularr_downloads_dir, state)
    if not candidates:
        return 0
    logger.info(f"Orphan scan: {len(candidates)} candidate folder(s) to process")
    for path in candidates:
        try:
            process_orphan(
                folder_path=path,
                state=state,
                lidarr=lidarr,
                soularr_downloads_dir=soularr_downloads_dir,
                lidarr_downloads_dir=lidarr_downloads_dir,
                artist_match_ratio=artist_match_ratio,
                album_match_ratio=album_match_ratio,
                command_timeout=command_timeout,
            )
        except Exception:
            logger.exception(f"Unhandled error while processing orphan {path}")
    return len(candidates)
