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
    """Return audio file paths under `folder_path`, walked recursively.

    Paths are returned relative to `folder_path` so callers can preserve the
    nested layout (e.g. "The Way It Ends (2020)/01 - Never There.flac").
    Some Soulseek peers share albums one level deep inside the leaf folder,
    and a non-recursive listing would miss every track in those layouts.
    """
    results: list = []
    try:
        for dirpath, _dirnames, filenames in os.walk(folder_path):
            for name in filenames:
                if is_audio(name):
                    full = os.path.join(dirpath, name)
                    results.append(os.path.relpath(full, folder_path))
    except OSError:
        return []
    return results


def count_audio(folder_path: str) -> int:
    return len(list_audio(folder_path))
