"""
Post-download organizer fired by slskd's DownloadFileComplete webhook.

Reads tags from the just-completed file and moves it to a canonical layout
under the downloads root:

    {Artist}/{Album}/{Format}/{TrackNumber:02d} - {Title}.{ext}

Format bucket rules:
    FLAC:   FLAC-16 / FLAC-24 / FLAC-{N} (split by bits_per_sample)
    MP3:    MP3-{snap to 96/128/160/192/224/256/320} for CBR
            MP3-VBR for any VBR/ABR/UNKNOWN encoding mode
    Others: OGG / OPUS / M4A / AAC / WAV / AIFF / APE / <ext upper>

Empty source directories are pruned bottom-up after the move.
"""

import logging
import os
import re
import shutil
from typing import Optional

import music_tag

logger = logging.getLogger("slskd_hook")

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_LEADING_NUM = re.compile(r"^\s*(?:CD\s*\d+\s*[-_/\s]+)?(\d{1,3})(?!\d)")
_CBR_TARGETS = (96, 128, 160, 192, 224, 256, 320)


def _clean(value: str) -> str:
    if not value:
        return ""
    out = _ILLEGAL_CHARS.sub("_", str(value))
    out = re.sub(r"\s+", " ", out).strip()
    return out.rstrip(".")


def _snap_cbr(kbps: float) -> int:
    return min(_CBR_TARGETS, key=lambda t: abs(kbps - t))


def _format_bucket(local_path: str, mfile) -> str:
    ext = os.path.splitext(local_path)[1].lower().lstrip(".")

    if ext == "flac":
        bps = getattr(mfile.info, "bits_per_sample", None) or 16
        return f"FLAC-{int(bps)}"

    if ext == "mp3":
        kbps = (getattr(mfile.info, "bitrate", 0) or 0) / 1000.0
        is_cbr = False
        try:
            from mutagen.mp3 import BitrateMode
            is_cbr = getattr(mfile.info, "bitrate_mode", None) == BitrateMode.CBR
        except ImportError:
            pass
        if is_cbr and kbps > 0:
            return f"MP3-{_snap_cbr(kbps)}"
        return "MP3-VBR"

    if ext in ("ogg", "oga"):
        return "OGG"
    if ext == "opus":
        return "OPUS"
    if ext == "m4a":
        return "M4A"
    if ext == "aac":
        return "AAC"
    if ext == "wav":
        return "WAV"
    if ext in ("aiff", "aif"):
        return "AIFF"
    if ext == "ape":
        return "APE"
    return ext.upper() or "UNKNOWN"


def _read_track_number(f, local_path: str) -> int:
    raw = ""
    try:
        raw = str(f.get("tracknumber") or "").split("/")[0].strip()
    except Exception:
        raw = ""
    if raw.isdigit():
        return int(raw)
    m = _LEADING_NUM.search(os.path.basename(local_path))
    if m:
        n = int(m.group(1))
        if n < 1000:
            return n
    return 0


def _prune_empty_parents(start_dir: str, stop_at: str) -> None:
    cur = os.path.normpath(start_dir)
    stop = os.path.normpath(stop_at)
    while cur and cur != stop and cur.startswith(stop + os.sep):
        try:
            if os.listdir(cur):
                return
            os.rmdir(cur)
        except OSError:
            return
        cur = os.path.dirname(cur)


def organize(local_path: str, downloads_root: str = "/downloads") -> Optional[str]:
    """Move `local_path` to its canonical location. Returns target path, or None
    if the file could not be organized (missing tags, unsupported format)."""
    if not os.path.isfile(local_path):
        logger.info(f"organize: file gone or not a regular file: {local_path}")
        return None

    try:
        f = music_tag.load_file(local_path)
    except Exception:
        logger.warning(f"organize: cannot read tags from {local_path}", exc_info=True)
        return None

    artist = str(f.get("albumartist") or f.get("artist") or "").strip()
    album = str(f.get("album") or "").strip()
    title = str(f.get("title") or "").strip()

    if not (artist and album and title):
        logger.info(
            f"organize: missing tags (artist={artist!r} album={album!r} title={title!r}), "
            f"leaving file in place: {local_path}"
        )
        return None

    track_num = _read_track_number(f, local_path)
    bucket = _format_bucket(local_path, f.mfile)
    ext = os.path.splitext(local_path)[1]

    rel = os.path.join(
        _clean(artist),
        _clean(album),
        bucket,
        f"{track_num:02d} - {_clean(title)}{ext}",
    )
    target = os.path.join(downloads_root, rel)

    if os.path.normpath(target) == os.path.normpath(local_path):
        return target

    if os.path.exists(target):
        try:
            same_size = os.path.getsize(target) == os.path.getsize(local_path)
        except OSError:
            same_size = False
        if same_size:
            logger.info(f"organize: target exists with same size, removing dup: {local_path}")
            try:
                os.remove(local_path)
                _prune_empty_parents(os.path.dirname(local_path), downloads_root)
            except OSError:
                pass
            return target
        base, e = os.path.splitext(target)
        i = 1
        while os.path.exists(f"{base}.{i}{e}"):
            i += 1
        target = f"{base}.{i}{e}"

    target_parent = os.path.dirname(target)
    _makedirs_group_writable(target_parent, downloads_root)
    src_parent = os.path.dirname(local_path)
    shutil.move(local_path, target)
    _prune_empty_parents(src_parent, downloads_root)
    logger.info(f"organize: moved -> {target}")
    return target


def _makedirs_group_writable(target_dir: str, stop_at: str) -> None:
    """Like os.makedirs, but chmod each newly-created intermediate to mode 0o2775
    so consumers running as a different uid in the same group (e.g. Lidarr's `abc`
    user when this hook runs as root) can still delete files after import."""
    if not target_dir:
        return
    parts = []
    cur = os.path.normpath(target_dir)
    stop = os.path.normpath(stop_at)
    while cur and cur != stop and cur.startswith(stop + os.sep):
        if not os.path.isdir(cur):
            parts.append(cur)
        cur = os.path.dirname(cur)
    os.makedirs(target_dir, exist_ok=True)
    for d in reversed(parts):
        try:
            os.chmod(d, 0o2775)
        except OSError:
            pass
