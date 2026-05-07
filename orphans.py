"""
Orphan recovery for Soularr.

An "orphan" is an album-shaped pile of audio files inside the slskd downloads
directory that is not currently tracked by the State store and has not been
resolved by a previous scan. This typically happens after Soularr restarts,
after the user manually grabs an album in the slskd UI, or when a previous
Soularr cycle fails to finalize an import.

Detection groups files by their `albumartist`+`album` ID3 tags rather than
by directory, so a download tree shaped like
`{Artist}/{Album}/{Format}/{Track}.flac` (e.g. produced by the slskd post-
download organizer) yields one orphan entry per album rather than one entry
per artist with all albums merged together. The "folder" for an orphan is the
lowest common ancestor of the files in the group.

Recovery flow:
  1. Walk the downloads dir, group audio files by (albumartist, album) tags.
     Files without usable tags fall back to grouping by their parent dir.
  2. For each group: best-effort fuzzy match the metadata against Lidarr.
  3. If the matched album is currently wanted, trigger Lidarr ManualImport
     (DownloadedAlbumsScan silently refuses without a download-client tracking
     entry; ManualImport is what actually imports unattached files).
  4. Wait for the command and parse imported_count from the result message.
  5. Record the outcome in state.orphans so the same folder is not
     reprocessed on the next cycle.
"""

import difflib
import hashlib
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


# Format-bucket folder names produced by slskd_hook (FLAC-16, MP3-320, OPUS, ...)
_FORMAT_BUCKET_RE = re.compile(
    r"^(FLAC-\d+|MP3-(?:VBR|\d+)|OGG|OPUS|M4A|AAC|WAV|AIFF|APE)$"
)


def _common_folder(files: list, downloads_dir: str) -> str:
    """LCA of `files`, clamped to one level below downloads_dir at minimum."""
    if not files:
        return ""
    if len(files) == 1:
        folder = os.path.dirname(files[0])
    else:
        folder = os.path.commonpath(files)
    if os.path.normpath(folder) == os.path.normpath(downloads_dir):
        folder = os.path.dirname(files[0])
    return folder


def _norm_key(s: str) -> str:
    """Normalize artist/album text for grouping: lowercase, collapse whitespace,
    strip spaces around `/`, `-`, and `:` so 'Either / Or' and 'Either/Or'
    collapse to one bucket."""
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*([/\-:])\s*", r"\1", s)
    return s


def _orphan_id(artist: str, album: str, fmt: str, scope: str = "", fallback_path: str = "") -> str:
    """
    Composite orphan key: a stable hash of (norm_artist, norm_album, format,
    album-folder basename). The on-disk album-folder name disambiguates
    case-variant duplicates that resolve to the same canonical metadata
    (e.g. 'To Be Everywhere Is to Be Nowhere' and 'To Be Everywhere Is To Be
    Nowhere' tags pointing at two different physical folders); without it,
    they'd collide under one id and the LCA would collapse up to the artist
    root, polluting the preview with files from sibling albums.
    """
    if artist and album:
        raw = f"{_norm_key(artist)}|{_norm_key(album)}|{fmt}|{scope}"
    else:
        raw = f"__fallback__|{fallback_path}|{fmt}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _format_for(file_path: str) -> str:
    """Return the format-bucket label for `file_path`. Uses the slskd_hook
    bucket name when the file sits directly in such a folder (FLAC-24,
    MP3-VBR, ...); otherwise falls back to the bare extension uppercased."""
    parent = os.path.basename(os.path.dirname(file_path))
    if _FORMAT_BUCKET_RE.match(parent):
        return parent
    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    return ext.upper() or "UNKNOWN"


def _scope_for(file_path: str, downloads_dir: str) -> str:
    """Return the basename of the on-disk album folder for `file_path` —
    the file's parent dir, climbed one level if that dir is a format bucket.
    Empty string if the file sits directly at downloads_dir.

    Used to keep two case-variant folders for the same canonical (artist,
    album, format) tuple from being grouped together (which would collapse
    the LCA up to the artist root)."""
    parent = os.path.dirname(file_path)
    if _FORMAT_BUCKET_RE.match(os.path.basename(parent)):
        parent = os.path.dirname(parent)
    if os.path.normpath(parent) == os.path.normpath(downloads_dir):
        return ""
    return os.path.basename(parent)


def _read_album_tags(path: str) -> tuple:
    """Return (albumartist, album, year) read from `path`, blanks on failure."""
    try:
        f = music_tag.load_file(path)
        artist = str(f.get("albumartist") or f.get("artist") or "").strip()
        album = str(f.get("album") or "").strip()
        year = str(f.get("year") or "").strip()
        return artist, album, year
    except Exception:
        return "", "", ""


def find_orphan_albums(downloads_dir: str) -> list:
    """
    Walk `downloads_dir` and return per-(artist, album, format) orphan
    candidates.

    Audio files are grouped by (norm_artist, norm_album, format, scope), where
    format is the slskd_hook format-bucket folder name (FLAC-24, MP3-VBR, ...)
    or the bare extension uppercased; scope is the on-disk album-folder
    basename (parent of the format bucket). Artist/album keys are normalized
    (lowercase, whitespace collapsed, no spaces around `/-:`) so 'Either / Or'
    and 'Either/Or' tags merge into one row when they share an album folder;
    scope keeps two case-variant album folders for the same canonical metadata
    distinct so the LCA doesn't collapse up to the artist root.

    Files whose tags can't be read fall back to grouping by their immediate
    parent directory + format. Fallback buckets are absorbed into a tagged
    group when the parent lies inside that group's folder so partially-tagged
    albums don't split into multiple rows.

    Each returned dict has: folder_path, audio_files, metadata (may be None),
    format.
    """
    try:
        top_entries = sorted(os.listdir(downloads_dir))
    except OSError:
        return []

    groups: dict = {}      # (norm_artist, norm_album, format, scope) -> {files, metadata, format, scope}
    fallback: dict = {}    # (parent_dir, format) -> {files, format}

    for top in top_entries:
        if any(top.startswith(p) for p in SKIP_FOLDER_PREFIXES):
            continue
        top_path = os.path.join(downloads_dir, top)
        if not os.path.isdir(top_path):
            continue
        for dirpath, dirnames, filenames in os.walk(top_path):
            dirnames.sort()
            for name in sorted(filenames):
                if not is_audio(name):
                    continue
                full = os.path.join(dirpath, name)
                fmt = _format_for(full)
                scope = _scope_for(full, downloads_dir)
                artist, album, year = _read_album_tags(full)
                if artist and album:
                    key = (_norm_key(artist), _norm_key(album), fmt, scope)
                    g = groups.setdefault(
                        key,
                        {
                            "files": [],
                            "metadata": {"artist": artist, "album": album, "year": year},
                            "format": fmt,
                            "scope": scope,
                        },
                    )
                    g["files"].append(full)
                else:
                    fb_key = (dirpath, fmt)
                    fallback.setdefault(
                        fb_key, {"files": [], "format": fmt}
                    )["files"].append(full)

    # First pass: compute folders for tagged groups.
    for g in groups.values():
        g["folder"] = _common_folder(g["files"], downloads_dir)

    # Absorb fallback buckets whose parent dir lies inside a tagged group's
    # folder AND whose format matches (covers / .nfo / one untagged track in a
    # tagged album); a different format must stay its own row.
    leftover_fallback: dict = {}
    for (parent, fmt), fb in fallback.items():
        absorbed = False
        for g in groups.values():
            folder = g["folder"]
            if g["format"] == fmt and (parent == folder or parent.startswith(folder + os.sep)):
                g["files"].extend(fb["files"])
                absorbed = True
                break
        if not absorbed:
            leftover_fallback[(parent, fmt)] = fb

    candidates = []
    for g in groups.values():
        folder = _common_folder(g["files"], downloads_dir)
        meta = g["metadata"]
        candidates.append({
            "id": _orphan_id(meta["artist"], meta["album"], g["format"], g["scope"]),
            "folder_path": folder,
            "audio_files": sorted(g["files"]),
            "metadata": meta,
            "format": g["format"],
        })
    for (parent, fmt), fb in leftover_fallback.items():
        candidates.append({
            "id": _orphan_id("", "", fmt, fallback_path=parent),
            "folder_path": parent,
            "audio_files": sorted(fb["files"]),
            "metadata": None,
            "format": fmt,
        })
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
    orphan_id: str,
    folder_path: str,
    state: State,
    lidarr,
    wanted_album_ids: set,
    soularr_downloads_dir: str,
    lidarr_downloads_dir: str,
    artist_match_ratio: float,
    album_match_ratio: float,
    command_timeout: int = 60,
    metadata: dict = None,
    album_format: str = "",
) -> str:
    """
    Process one orphan release.

    Identity is the composite `orphan_id` derived from (artist, album, format)
    — see `_orphan_id`. The on-disk `folder_path` is used for filesystem
    actions but is not the storage key.

    Auto-import is attempted ONLY when the fuzzy-matched album is currently in
    Lidarr's wanted list (missing or cutoff_unmet). For everything else the
    orphan is recorded with status `pending` and surfaced through the orphans
    UI for manual user action.

    `metadata` is supplied by `find_orphan_albums` (the only caller). When the
    folder had no readable audio at scan time, metadata is None and the entry
    is recorded as `empty`.
    """
    if not metadata:
        logger.info(f"Orphan: no audio metadata available for {folder_path}")
        state.mark_orphan_scanned(
            orphan_id,
            folder_path=folder_path,
            status=State.ORPHAN_STATUS_EMPTY,
            album_format=album_format,
        )
        return State.ORPHAN_STATUS_EMPTY

    artist = metadata["artist"]
    album = metadata["album"]

    matched_id = find_lidarr_album_id(
        metadata, lidarr, artist_match_ratio, album_match_ratio
    )

    # An album currently being grabbed by the normal soularr flow is not an
    # orphan in the strict sense — monitor_downloads will handle the import.
    # Surface it in the UI with status `downloading` so the user can see what's
    # on disk and where it's going, but skip the wanted-list check and
    # ManualImport call so we don't race with monitor_downloads.
    if matched_id and matched_id in state.in_flight_album_ids():
        state.mark_orphan_scanned(
            orphan_id,
            folder_path=folder_path,
            status=State.ORPHAN_STATUS_DOWNLOADING,
            artist=artist,
            album=album,
            album_format=album_format,
            matched_album_id=matched_id,
        )
        logger.info(
            f"Orphan {folder_path} -> downloading "
            f"(artist='{artist}' album='{album}' matched_album_id={matched_id})"
        )
        return State.ORPHAN_STATUS_DOWNLOADING

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

    common_kwargs = dict(
        folder_path=folder_path,
        artist=artist,
        album=album,
        album_format=album_format,
    )

    if not matched_id or matched_id not in wanted_album_ids:
        # Not in the user's current wanted list — record for UI review and stop.
        state.mark_orphan_scanned(
            orphan_id,
            status=State.ORPHAN_STATUS_PENDING,
            matched_album_id=matched_id,
            rejections=rejections,
            **common_kwargs,
        )
        logger.info(
            f"Orphan {folder_path} -> pending "
            f"(artist='{artist}' album='{album}' "
            f"matched_album_id={matched_id})"
        )
        return State.ORPHAN_STATUS_PENDING

    # Album is wanted. Try ManualImport.
    logger.info(
        f"Orphan {folder_path} matches wanted album_id={matched_id} "
        f"(artist='{artist}' album='{album}'). Auto-importing."
    )

    try:
        command_id, candidate_count, accepted_count = _manual_import(
            lidarr, lidarr_path, preview=preview
        )
    except Exception:
        logger.exception(f"Orphan: failed to enqueue Lidarr ManualImport for {folder_path}")
        state.mark_orphan_scanned(
            orphan_id,
            status=State.ORPHAN_STATUS_ERROR,
            matched_album_id=matched_id,
            rejections=rejections,
            **common_kwargs,
        )
        return State.ORPHAN_STATUS_ERROR

    if accepted_count == 0:
        state.mark_orphan_scanned(
            orphan_id,
            status=State.ORPHAN_STATUS_NO_MATCH,
            matched_album_id=matched_id,
            imported_count=0,
            rejections=rejections,
            **common_kwargs,
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
            orphan_id,
            status=State.ORPHAN_STATUS_ERROR,
            matched_album_id=matched_id,
            lidarr_command_id=command_id,
            rejections=rejections,
            **common_kwargs,
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
        state.remove_orphan(orphan_id)
        # Drop the matching state.albums entry too — the album reached its
        # destination, so the SUCCEEDED tracker has served its purpose. Without
        # this, filter_list keeps treating the album as in-flight forever
        # (since SUCCEEDED is in TRACKED_STATES).
        if matched_id:
            state.cleanup_terminal(matched_id)
        return "auto_imported"  # not a stored status — folder is gone

    state.mark_orphan_scanned(
        orphan_id,
        status=State.ORPHAN_STATUS_NO_MATCH,
        matched_album_id=matched_id,
        lidarr_command_id=command_id,
        imported_count=0,
        rejections=rejections,
        **common_kwargs,
    )
    logger.info(f"Orphan {folder_path} -> no_match (command completed but 0 imported)")
    return State.ORPHAN_STATUS_NO_MATCH


# Statuses set by an explicit user action — never auto-superseded.
_USER_STATUSES = {State.ORPHAN_STATUS_IGNORED, State.ORPHAN_STATUS_DELETED}


def _prune_superseded(state: State, candidates: list) -> None:
    """
    Drop stale orphan entries:
      a) whose folder is now an ancestor of a deeper detected album folder
         (e.g. the artist root being superseded by per-format children); and
      b) whose folder_path matches a current candidate but whose id no
         longer does — happens when the id derivation rule changes (scope
         disambiguator added, etc.) and old records would otherwise live
         alongside the freshly-keyed ones.

    `ignored` / `deleted` entries are preserved — they reflect explicit user
    choices. Everything else (pending, downloading, no_match, error,
    partial_imported, empty) is system-set and gets re-evaluated against
    the new structure.
    """
    if not candidates:
        return
    candidate_paths = [c["folder_path"] for c in candidates]
    expected_ids_by_folder: dict = {}
    for c in candidates:
        expected_ids_by_folder.setdefault(c["folder_path"], set()).add(c["id"])

    for entry in list(state.list_orphans()):
        if entry.get("status") in _USER_STATUSES:
            continue
        old = entry.get("folder_path") or ""
        old_id = entry.get("id") or ""
        if not old:
            continue
        # (a) ancestor relationship
        ancestor_drop = False
        for new in candidate_paths:
            if new != old and new.startswith(old + os.sep):
                state.remove_orphan(old_id)
                logger.info(f"Orphan {old} superseded by deeper entries; removed")
                ancestor_drop = True
                break
        if ancestor_drop:
            continue
        # (b) same folder, mismatched id (id format change)
        expected = expected_ids_by_folder.get(old, set())
        if expected and old_id and old_id not in expected:
            state.remove_orphan(old_id)
            logger.info(f"Orphan {old} stale id {old_id} replaced; removed")


def process_all_orphans(
    soularr_downloads_dir: str,
    lidarr_downloads_dir: str,
    state: State,
    lidarr,
    artist_match_ratio: float = 0.85,
    album_match_ratio: float = 0.85,
    command_timeout: int = 60,
) -> int:
    """Entry point. Returns the count of orphans evaluated this cycle."""
    found = find_orphan_albums(soularr_downloads_dir)
    if not found:
        return 0

    _prune_superseded(state, found)

    # Skip already-resolved entries (terminal statuses set by previous scans
    # or explicit UI actions). Pending entries are re-evaluated each cycle so
    # they auto-import once the album lands in the wanted list.
    candidates = [c for c in found if not state.is_orphan_resolved(c["id"])]
    if not candidates:
        return 0

    wanted_album_ids = fetch_wanted_album_ids(lidarr)
    logger.info(
        f"Orphan scan: {len(candidates)} album-level candidate(s) "
        f"({len(wanted_album_ids)} wanted album ids loaded)"
    )
    for c in candidates:
        try:
            process_orphan(
                orphan_id=c["id"],
                folder_path=c["folder_path"],
                state=state,
                lidarr=lidarr,
                wanted_album_ids=wanted_album_ids,
                soularr_downloads_dir=soularr_downloads_dir,
                lidarr_downloads_dir=lidarr_downloads_dir,
                artist_match_ratio=artist_match_ratio,
                album_match_ratio=album_match_ratio,
                command_timeout=command_timeout,
                metadata=c["metadata"],
                album_format=c["format"],
            )
        except Exception:
            logger.exception(f"Unhandled error while processing orphan {c['folder_path']}")
    return len(candidates)
