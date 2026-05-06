"""
Pre-import preparation: rewrite tags and rename files in a download folder so
that Lidarr's ManualImport sees clean, canonical metadata and a layout that
matches the user's naming pattern.

The goal is to reduce Lidarr's soft rejections like 'Album match is not close
enough' on folders where the uploader's tags are weak or wrong, and to make
the on-disk layout match the user's Lidarr naming pattern so the import is a
near-no-op move.

Conservative by default:
  - Only rewrites albumartist / album / year (the tags Lidarr uses to match
    the album). Title / track / disc are left intact unless missing, in
    which case track number is derived from the filename.
  - Renaming is opt-in via `rename_pattern` (empty string disables it).

The rename pattern uses Lidarr's token syntax. `/` separates folder from
filename — any leading parts become a subfolder under the source dir. The
familiar `{track:00}` zero-pad spec is translated to Python's `{track:02d}`
internally.
"""

import logging
import os
import re
import shutil
import difflib
from typing import Optional

import music_tag

logger = logging.getLogger("prepare")

# Filesystem-illegal characters across Linux/Windows/macOS — replaced with '_'.
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean(value: str) -> str:
    """Sanitize a single path component for filesystem use."""
    if value is None:
        return ""
    out = _ILLEGAL_CHARS.sub("_", str(value))
    out = re.sub(r"\s+", " ", out).strip()
    out = out.rstrip(".")  # Windows hates trailing dots
    return out


# Match Lidarr's token zero-pad spec like {track:00} or {Disc:000}.
_PAD_TOKEN = re.compile(r"\{(\w+):(0+)\}")


def compile_pattern(pattern: str) -> str:
    """
    Translate Lidarr-style tokens into Python str.format compatible ones:
        {track:00}  -> {track:02d}
        {Disc:000}  -> {Disc:03d}
    Other tokens are left untouched.
    """
    return _PAD_TOKEN.sub(
        lambda m: "{" + m.group(1) + ":0" + str(len(m.group(2))) + "d}",
        pattern or "",
    )


def _build_variables(album: dict, track: dict) -> dict:
    """Build the substitution dict for one (album, track) pair."""
    artist_name = (album.get("artist") or {}).get("artistName") or ""
    album_artist_name = artist_name  # Lidarr exposes only one artist per album
    album_title = album.get("title") or ""
    year = (album.get("releaseDate") or "")[:4]
    track_title = (track or {}).get("title") or ""
    track_number = (track or {}).get("trackNumber") or 0
    medium_number = (track or {}).get("mediumNumber") or 1

    try:
        track_number = int(track_number)
    except (TypeError, ValueError):
        track_number = 0
    try:
        medium_number = int(medium_number)
    except (TypeError, ValueError):
        medium_number = 1

    return {
        "Album Title":        album_title,
        "Album CleanTitle":   _clean(album_title),
        "Release Year":       year,
        "Artist Name":        artist_name,
        "Artist CleanName":   _clean(artist_name),
        "Album Artist Name":  album_artist_name,
        "Album Artist CleanName": _clean(album_artist_name),
        "Track Title":        track_title,
        "Track CleanTitle":   _clean(track_title),
        "track":              track_number,
        "Disc":               medium_number,
    }


def _format_path(compiled_pattern: str, variables: dict) -> str:
    """
    Apply a compiled pattern and sanitize each path segment. Returns a relative
    path (no leading slash). The format string is split on '/' first so that
    '/' characters that appear inside variable values don't accidentally split
    into more subfolders.
    """
    parts = compiled_pattern.split("/")
    out_parts = []
    for part in parts:
        if not part:
            continue
        try:
            rendered = part.format(**variables)
        except (KeyError, ValueError, IndexError):
            # Unknown token or malformed spec — leave the part as-is.
            rendered = part
        out_parts.append(_clean(rendered))
    return os.path.join(*out_parts) if out_parts else ""


# ----------------------------------------------------------------------------
# Track mapping
# ----------------------------------------------------------------------------

_LEADING_TRACK = re.compile(r"^\s*(?:CD\s*\d+\s*[-_/\s]+)?(\d{1,3})(?!\d)")


def _track_number_from_filename(filename: str) -> Optional[int]:
    """Pick the leading track number from filenames like '01 - Title.flac'."""
    base = os.path.basename(filename or "")
    m = _LEADING_TRACK.search(base)
    if not m:
        return None
    n = int(m.group(1))
    # Treat 4-digit values as years, not track numbers.
    if n >= 1000:
        return None
    return n


def _read_existing_track_number(audio_path: str) -> Optional[int]:
    try:
        f = music_tag.load_file(audio_path)
        raw = f.get("tracknumber")
        if not raw:
            return None
        s = str(raw).split("/")[0].strip()
        return int(s) if s.isdigit() else None
    except Exception:
        return None


def _read_existing_title(audio_path: str) -> str:
    try:
        f = music_tag.load_file(audio_path)
        return str(f.get("title") or "").strip()
    except Exception:
        return ""


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _match_track(
    audio_path: str,
    lidarr_tracks: list,
    infer_from_filename: bool = True,
) -> Optional[dict]:
    """
    Resolve which Lidarr track an audio file corresponds to.

    Strategy (highest confidence first):
        1. Existing 'tracknumber' tag matches a Lidarr trackNumber.
        2. Filename starts with a track number that matches.
        3. Fuzzy-match the existing title tag (or basename without extension)
           against Lidarr track titles; pick the best above 0.7.
    """
    if not lidarr_tracks:
        return None

    by_number = {int(t.get("trackNumber") or 0): t for t in lidarr_tracks}

    n = _read_existing_track_number(audio_path)
    if n and n in by_number:
        return by_number[n]

    if infer_from_filename:
        n = _track_number_from_filename(audio_path)
        if n and n in by_number:
            return by_number[n]

    candidate_title = _read_existing_title(audio_path)
    if not candidate_title:
        candidate_title = os.path.splitext(os.path.basename(audio_path))[0]

    best = None
    best_score = 0.0
    for t in lidarr_tracks:
        score = _ratio(candidate_title, t.get("title") or "")
        if score > best_score:
            best_score = score
            best = t
    if best and best_score >= 0.7:
        return best
    return None


# ----------------------------------------------------------------------------
# Tag rewriting
# ----------------------------------------------------------------------------

def _rewrite_tags(audio_path: str, album: dict, track: dict) -> bool:
    """
    Conservatively rewrite the album-identifying tags so Lidarr's matching is
    confident. Only writes when the existing value is missing or differs from
    the canonical value. Returns True if any tag was written.
    """
    artist_name = (album.get("artist") or {}).get("artistName") or ""
    album_title = album.get("title") or ""
    year = (album.get("releaseDate") or "")[:4]

    try:
        f = music_tag.load_file(audio_path)
    except (NotImplementedError, Exception):
        return False

    changed = False

    def _maybe_set(field: str, desired):
        nonlocal changed
        if not desired:
            return
        try:
            current = str(f.get(field) or "").strip()
        except Exception:
            current = ""
        if current != str(desired):
            try:
                f[field] = desired
                changed = True
            except Exception:
                pass

    _maybe_set("albumartist", artist_name)
    _maybe_set("album", album_title)
    if year:
        _maybe_set("year", year)
    # Track number/title only filled in if missing — never overwrite.
    if track:
        try:
            existing_tracknum = str(f.get("tracknumber") or "").strip()
        except Exception:
            existing_tracknum = ""
        if not existing_tracknum and track.get("trackNumber"):
            try:
                f["tracknumber"] = str(track["trackNumber"])
                changed = True
            except Exception:
                pass

    if changed:
        try:
            f.save()
        except Exception:
            logger.warning(f"Tag save failed for {audio_path}", exc_info=True)
            return False
    return changed


# ----------------------------------------------------------------------------
# Top-level entry
# ----------------------------------------------------------------------------

def prepare_folder(
    folder_path: str,
    album: dict,
    tracks: list,
    rewrite_tags: bool = True,
    infer_track_number_from_filename: bool = True,
    rename_pattern: str = "",
) -> dict:
    """
    Rewrite tags and optionally rename audio files within `folder_path` to
    canonicalize them against the given Lidarr album + tracks.

    Returns a dict with counts: {tagged, renamed, skipped, errors}.
    """
    from audio import is_audio  # local import so unit tests don't need audio.py

    if not os.path.isdir(folder_path):
        return {"tagged": 0, "renamed": 0, "skipped": 0, "errors": 0}

    compiled = compile_pattern(rename_pattern) if rename_pattern else ""

    counts = {"tagged": 0, "renamed": 0, "skipped": 0, "errors": 0}

    for name in sorted(os.listdir(folder_path)):
        if not is_audio(name):
            continue
        path = os.path.join(folder_path, name)
        if not os.path.isfile(path):
            continue

        track = _match_track(path, tracks, infer_track_number_from_filename)
        if not track:
            counts["skipped"] += 1
            continue

        if rewrite_tags:
            try:
                if _rewrite_tags(path, album, track):
                    counts["tagged"] += 1
            except Exception:
                logger.exception(f"Tag rewrite failed for {path}")
                counts["errors"] += 1

        if compiled:
            variables = _build_variables(album, track)
            ext = os.path.splitext(name)[1]
            try:
                rel = _format_path(compiled, variables) + ext
                dst = os.path.join(folder_path, rel)
                if os.path.normpath(dst) == os.path.normpath(path):
                    continue  # already correctly named
                os.makedirs(os.path.dirname(dst) or folder_path, exist_ok=True)
                if os.path.exists(dst):
                    logger.info(f"Rename target already exists, skipping: {dst}")
                    counts["skipped"] += 1
                    continue
                shutil.move(path, dst)
                counts["renamed"] += 1
            except Exception:
                logger.exception(f"Rename failed for {path}")
                counts["errors"] += 1

    if counts["tagged"] or counts["renamed"]:
        logger.info(
            f"prepare_folder({folder_path}): tagged={counts['tagged']} "
            f"renamed={counts['renamed']} skipped={counts['skipped']} "
            f"errors={counts['errors']}"
        )
    return counts


# ----------------------------------------------------------------------------
# Convenience wrapper used by orphans.py and webui.py
# ----------------------------------------------------------------------------

def prepare_for_import(
    folder_path: str,
    lidarr,
    album_id: int,
    options: dict = None,
) -> Optional[dict]:
    """
    High-level entry point. Fetches the album + tracks from Lidarr and runs
    prepare_folder with the user's options.

    `options` keys (all optional):
        enabled                              default True
        rewrite_tags                         default True
        infer_track_number_from_filename     default True
        rename_pattern                       default '' (no rename)

    Returns the counts dict from prepare_folder, or None if prepare is
    disabled or the album/tracks could not be fetched.
    """
    options = options or {}
    if not options.get("enabled", True):
        return None
    if not album_id:
        return None

    try:
        album = lidarr.get_album(album_id)
        if isinstance(album, list):
            album = album[0] if album else None
    except Exception:
        logger.warning(f"prepare: get_album({album_id}) failed", exc_info=True)
        return None
    if not album:
        return None

    artist_id = (album.get("artist") or {}).get("id")
    releases = album.get("releases") or []
    monitored_release = next((r for r in releases if r.get("monitored")), None) or (releases[0] if releases else None)
    release_id = (monitored_release or {}).get("id")

    tracks = []
    try:
        tracks = lidarr.get_tracks(artistId=artist_id, albumId=album_id, albumReleaseId=release_id) or []
    except Exception:
        logger.warning(f"prepare: get_tracks for album {album_id} failed", exc_info=True)
        # Continue with empty track list — _match_track will simply return None
        # for every file, only the album-level tags get rewritten.

    return prepare_folder(
        folder_path=folder_path,
        album=album,
        tracks=tracks,
        rewrite_tags=options.get("rewrite_tags", True),
        infer_track_number_from_filename=options.get("infer_track_number_from_filename", True),
        rename_pattern=options.get("rename_pattern", ""),
    )
