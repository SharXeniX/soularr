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
import shutil
import time
from typing import Optional

import music_tag

from state import State
from audio import is_audio, list_audio

logger = logging.getLogger("orphans")

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
        if is_audio(name):
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


def fetch_lidarr_preview(lidarr, folder_path_lidarr: str) -> list:
    """
    Return Lidarr's manualimport preview for the given folder. Each entry is a
    candidate file with `rejections`, `album`, `artist`, `quality`, etc.
    """
    import requests
    # pyarr's LidarrAPI stores the host url as `host_url` in newer versions.
    url = getattr(lidarr, "host_url", None) or getattr(lidarr, "_session_url", "")
    api_key = getattr(lidarr, "api_key", None) or getattr(lidarr, "_session_api_key", "")
    r = requests.get(
        f"{url.rstrip('/')}/api/v1/manualimport",
        params={"folder": folder_path_lidarr},
        headers={"X-Api-Key": api_key},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def unique_rejections(preview: list) -> list:
    """Collapse the per-file rejections in the preview into a sorted unique list."""
    seen = set()
    for f in preview:
        for r in f.get("rejections", []):
            reason = r.get("reason") if isinstance(r, dict) else r
            if reason:
                seen.add(reason)
    return sorted(seen)


def _manual_import(lidarr, folder_path_lidarr: str, preview: list = None) -> tuple:
    """
    Use Lidarr's ManualImport flow which actually imports unattached files
    (DownloadedAlbumsScan needs a download-client tracking entry and silently
    refuses without one). Returns (command_id, candidate_count, accepted_count).
    """
    if preview is None:
        preview = fetch_lidarr_preview(lidarr, folder_path_lidarr)

    accepted = []
    for it in preview:
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
        return (None, len(preview), 0)

    cmd = lidarr.post_command(
        name="ManualImport",
        files=accepted,
        importMode="auto",
        replaceExistingFiles=False,
    )
    return (cmd.get("id"), len(preview), len(accepted))


def find_orphan_folders(downloads_dir: str, state: State) -> list:
    """
    Return absolute paths of subfolders of `downloads_dir` that need evaluation.

    A folder is included when it is not currently tracked by an in-flight slskd
    grab and does not already carry a terminal status in the orphans table.
    Pending entries are NOT skipped — they get re-evaluated each cycle so that
    folders matching newly wanted albums get auto-imported on the next scan.
    """
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


def fetch_wanted_album_ids(lidarr) -> set:
    """
    Return the set of Lidarr album ids that are currently wanted (missing or
    cutoff_unmet). We page through both endpoints because they don't return
    the full list in one call by default.
    """
    wanted: set = set()
    for missing_flag in (True, False):
        page = 1
        while True:
            try:
                resp = lidarr.get_wanted(
                    page=page,
                    page_size=200,
                    sort_dir="ascending",
                    sort_key="albums.title",
                    missing=missing_flag,
                )
            except Exception:
                logger.warning(
                    f"Could not fetch wanted/{('missing' if missing_flag else 'cutoff')} page {page}",
                    exc_info=True,
                )
                break
            for rec in resp.get("records", []):
                if rec.get("id"):
                    wanted.add(rec["id"])
            total = resp.get("totalRecords", 0)
            if page * 200 >= total:
                break
            page += 1
    return wanted


def _audio_files_in(folder_path: str) -> list:
    return list_audio(folder_path)


def process_orphan(
    folder_path: str,
    state: State,
    lidarr,
    wanted_album_ids: set,
    soularr_downloads_dir: str,
    lidarr_downloads_dir: str,
    artist_match_ratio: float,
    album_match_ratio: float,
    command_timeout: int = 60,
    prepare_options: dict = None,
) -> str:
    """
    Process one orphan folder.

    Auto-import is attempted ONLY when the fuzzy-matched album is currently in
    Lidarr's wanted list (missing or cutoff_unmet). For everything else the
    orphan is recorded with status `pending` and surfaced through the orphans
    UI for manual user action.
    """
    metadata = read_album_metadata(folder_path)
    if not metadata:
        logger.info(f"Orphan: no audio metadata readable in {folder_path}")
        state.mark_orphan_scanned(folder_path, status=State.ORPHAN_STATUS_EMPTY)
        return State.ORPHAN_STATUS_EMPTY

    matched_id = find_lidarr_album_id(
        metadata, lidarr, artist_match_ratio, album_match_ratio
    )

    # Always fetch Lidarr's manualimport preview so we can store the rejection
    # reasons regardless of whether we end up auto-importing — it gives the
    # user useful 'why is this here' info in the orphans UI.
    lidarr_path = _to_lidarr_path(folder_path, soularr_downloads_dir, lidarr_downloads_dir)
    try:
        preview = fetch_lidarr_preview(lidarr, lidarr_path)
    except Exception:
        logger.warning(f"Orphan: preview fetch failed for {folder_path}", exc_info=True)
        preview = []
    rejections = unique_rejections(preview)

    if not matched_id or matched_id not in wanted_album_ids:
        # Not in the user's current wanted list — record for UI review and stop.
        state.mark_orphan_scanned(
            folder_path,
            status=State.ORPHAN_STATUS_PENDING,
            matched_album_id=matched_id,
            rejections=rejections,
        )
        logger.info(
            f"Orphan {folder_path} -> pending "
            f"(artist='{metadata['artist']}' album='{metadata['album']}' "
            f"matched_album_id={matched_id})"
        )
        return State.ORPHAN_STATUS_PENDING

    # Album is wanted. Try ManualImport.
    logger.info(
        f"Orphan {folder_path} matches wanted album_id={matched_id} "
        f"(artist='{metadata['artist']}' album='{metadata['album']}'). Auto-importing."
    )

    # Optional Phase 3 preparation: rewrite weak tags / rename files so Lidarr
    # has a clean folder to import. Discard the cached preview because the
    # rewrite changes file paths and tags.
    if prepare_options and prepare_options.get("enabled", True):
        try:
            from prepare import prepare_for_import
            result = prepare_for_import(folder_path, lidarr, matched_id, prepare_options)
            if result and (result.get("tagged") or result.get("renamed")):
                preview = None  # force fresh preview after files moved/retagged
        except Exception:
            logger.warning("Prepare step failed; continuing with original files", exc_info=True)

    try:
        command_id, candidate_count, accepted_count = _manual_import(
            lidarr, lidarr_path, preview=preview
        )
    except Exception:
        logger.exception(f"Orphan: failed to enqueue Lidarr ManualImport for {folder_path}")
        state.mark_orphan_scanned(
            folder_path,
            status=State.ORPHAN_STATUS_ERROR,
            matched_album_id=matched_id,
            rejections=rejections,
        )
        return State.ORPHAN_STATUS_ERROR

    if accepted_count == 0:
        state.mark_orphan_scanned(
            folder_path,
            status=State.ORPHAN_STATUS_NO_MATCH,
            matched_album_id=matched_id,
            imported_count=0,
            rejections=rejections,
        )
        logger.info(
            f"Orphan {folder_path} -> no_match "
            f"(Lidarr rejected all {candidate_count} candidates)"
        )
        return State.ORPHAN_STATUS_NO_MATCH

    result = _wait_for_command(lidarr, command_id, timeout=command_timeout)
    imported = _parse_imported_count(result.get("message", ""))

    if result.get("status") == "timeout":
        state.mark_orphan_scanned(
            folder_path,
            status=State.ORPHAN_STATUS_ERROR,
            matched_album_id=matched_id,
            lidarr_command_id=command_id,
            rejections=rejections,
        )
        logger.warning(f"Orphan {folder_path} -> error (Lidarr command timeout)")
        return State.ORPHAN_STATUS_ERROR

    # If anything imported, the folder is considered done. Lidarr already moved
    # the matching audio files to the library; we rmtree the source folder
    # entirely (residual audio, covers, .nfo) and drop the orphan entry.
    if imported > 0:
        try:
            shutil.rmtree(folder_path)
            logger.info(
                f"Orphan {folder_path} -> imported {imported} files, source folder removed"
            )
        except OSError:
            logger.warning(
                f"Orphan {folder_path}: imported {imported} but rmtree failed",
                exc_info=True,
            )
        state.remove_orphan(folder_path)
        return "auto_imported"  # not a stored status — folder is gone

    state.mark_orphan_scanned(
        folder_path,
        status=State.ORPHAN_STATUS_NO_MATCH,
        matched_album_id=matched_id,
        lidarr_command_id=command_id,
        imported_count=0,
        rejections=rejections,
    )
    logger.info(f"Orphan {folder_path} -> no_match (command completed but 0 imported)")
    return State.ORPHAN_STATUS_NO_MATCH


def process_all_orphans(
    soularr_downloads_dir: str,
    lidarr_downloads_dir: str,
    state: State,
    lidarr,
    artist_match_ratio: float = 0.85,
    album_match_ratio: float = 0.85,
    command_timeout: int = 60,
    prepare_options: dict = None,
) -> int:
    """Entry point. Returns the count of folders evaluated this cycle."""
    candidates = find_orphan_folders(soularr_downloads_dir, state)
    if not candidates:
        return 0
    wanted_album_ids = fetch_wanted_album_ids(lidarr)
    logger.info(
        f"Orphan scan: {len(candidates)} candidate folder(s) "
        f"({len(wanted_album_ids)} wanted album ids loaded)"
    )
    for path in candidates:
        try:
            process_orphan(
                folder_path=path,
                state=state,
                lidarr=lidarr,
                wanted_album_ids=wanted_album_ids,
                soularr_downloads_dir=soularr_downloads_dir,
                lidarr_downloads_dir=lidarr_downloads_dir,
                artist_match_ratio=artist_match_ratio,
                album_match_ratio=album_match_ratio,
                command_timeout=command_timeout,
                prepare_options=prepare_options,
            )
        except Exception:
            logger.exception(f"Unhandled error while processing orphan {path}")
    return len(candidates)
