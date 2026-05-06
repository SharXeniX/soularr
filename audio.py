"""
Audio file detection helper shared by orphans.py and webui.py.

Uses Python's stdlib `mimetypes` rather than a hardcoded extension list, so
new audio formats supported by the system MIME database are recognized
automatically. A handful of music-specific types missing from the default
DB are registered explicitly to keep the result consistent across hosts.
"""

import mimetypes
import os

# Augment the system MIME DB with music-specific types that aren't always
# registered (Opus is sometimes mapped to audio/ogg only, FLAC to
# application/x-flac, etc.). idempotent — repeated calls are no-ops.
for ext, mime in (
    (".flac", "audio/flac"),
    (".opus", "audio/opus"),
    (".m4a", "audio/mp4"),
    (".mp3", "audio/mpeg"),
    (".ogg", "audio/ogg"),
    (".aac", "audio/aac"),
    (".wav", "audio/wav"),
    (".aiff", "audio/aiff"),
    (".ape", "audio/x-ape"),
    (".alac", "audio/mp4"),
    (".wv", "audio/x-wavpack"),
):
    mimetypes.add_type(mime, ext)


def is_audio(name: str) -> bool:
    """Return True if `name` looks like an audio file based on its extension."""
    if not name:
        return False
    mime, _ = mimetypes.guess_type(name)
    return mime is not None and mime.startswith("audio/")


def list_audio(folder_path: str) -> list:
    """Return audio filenames in `folder_path` (non-recursive)."""
    try:
        return [n for n in os.listdir(folder_path) if is_audio(n)]
    except OSError:
        return []


def count_audio(folder_path: str) -> int:
    return len(list_audio(folder_path))
