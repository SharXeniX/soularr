import os
import json
import shutil
import sys
import time
import logging
import configparser
import argparse
from datetime import datetime, timezone
from flask import Flask, Response, render_template, send_from_directory, jsonify, request
from waitress import serve

# Allow imports of state/orphans which sit at /app while webui.py is at /app/webui/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import State
import orphans as orphans_mod

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s|%(module)s|L%(lineno)d] %(asctime)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger(__name__)


def _fmt(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    return f"[{level}|webui] {ts}: {msg}"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCES_DIR = os.path.join(BASE_DIR, "..", "resources")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(
    __name__,
    template_folder="templates",
    static_folder=RESOURCES_DIR,
    static_url_path="/resources",
)
app.config["TEMPLATES_AUTO_RELOAD"] = True

def get_var_dir():
    parser = argparse.ArgumentParser(add_help=False)
    default = "/data" if os.environ.get("IN_DOCKER") else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--var-dir", default=default)
    args, _ = parser.parse_known_args()
    return args.var_dir


def get_config_path(var_dir):
    for path in [os.path.join(var_dir, "config.ini"), "config.ini"]:
        if os.path.exists(path):
            return path
    return os.path.join(var_dir, "config.ini")


def get_log_path(var_dir):
    config = configparser.ConfigParser()
    for path in [os.path.join(var_dir, "config.ini"), "config.ini"]:
        if os.path.exists(path):
            config.read(path)
            break
    log_file = config.get("Logging", "log_file", fallback="soularr.log")
    return os.path.join(var_dir, log_file)

@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    config_path = get_config_path(get_var_dir())
    if not os.path.exists(config_path):
        return jsonify({"content": "", "path": config_path, "exists": False})
    with open(config_path, "r") as f:
        content = f.read()
    return jsonify({"content": content, "path": config_path, "exists": True})


@app.route("/api/config", methods=["POST"])
def save_config():
    config_path = get_config_path(get_var_dir())
    data = request.get_json()
    if not data or "content" not in data:
        return jsonify({"error": "No content provided"}), 400
    try:
        with open(config_path, "w") as f:
            f.write(data["content"])
        logger.info(f"Config saved: {config_path}")
        return jsonify({"ok": True, "path": config_path})
    except Exception as e:
        logger.exception(f"Failed to save config: {config_path}")
        return jsonify({"error": str(e)}), 500


@app.route("/stream")
def stream():
    log_path = get_log_path(get_var_dir())

    def generate():
        while not os.path.exists(log_path):
            config = configparser.ConfigParser()
            config.read(get_config_path(get_var_dir()))
            log_to_file = config.getboolean("Logging", "log_to_file", fallback=False)
            if not log_to_file:
                yield f"data: {_fmt('Log file not found. Make sure log_to_file = True is set in your config.ini')}\n\n"
            else:
                yield f"data: {_fmt(f'Waiting for log file: {log_path}')}\n\n"
            time.sleep(5)
        with open(log_path, "r") as f:
            for line in f:
                line = line.rstrip("\n")
                if line:
                    yield f"data: {line}\n\n"
            while True:
                line = f.readline()
                if line:
                    line = line.rstrip("\n")
                    if line:
                        yield f"data: {line}\n\n"
                else:
                    time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

def _get_state():
    return State(get_var_dir())


def _read_config():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(get_config_path(get_var_dir()))
    return cfg


def _get_lidarr():
    from pyarr import LidarrAPI
    cfg = _read_config()
    return LidarrAPI(cfg["Lidarr"]["host_url"], cfg["Lidarr"]["api_key"])


def _to_lidarr_path(folder_path: str) -> str:
    """
    Translate a soularr-POV path (/downloads/Foo) into the Lidarr-POV path
    (/data/torrents/music/soulseek/Foo) using the two download_dir config values.
    """
    cfg = _read_config()
    soularr_dl = cfg["Slskd"]["download_dir"]
    lidarr_dl = cfg["Lidarr"]["download_dir"]
    rel = os.path.relpath(folder_path, soularr_dl)
    return os.path.join(lidarr_dl, rel)


@app.route("/api/failed-imports", methods=["GET"])
def get_failed_imports():
    try:
        return jsonify(_get_state().list_failed_imports())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/failed-imports/<album_id>", methods=["DELETE"])
def delete_failed_import(album_id):
    try:
        state = _get_state()
        try:
            album_id_int = int(album_id)
        except ValueError:
            album_id_int = album_id
        entry = state.remove_failed_import(album_id_int)
        if entry and entry.get("folder_path") and os.path.isdir(entry["folder_path"]):
            shutil.rmtree(entry["folder_path"])
            logger.info(f"Deleted failed import folder: {entry['folder_path']}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ----------------------------------------------------------------------------
# Phase 2c — orphan management endpoints
# ----------------------------------------------------------------------------

AUDIO_EXTS = (".flac", ".mp3", ".m4a", ".ogg", ".opus")


def _audio_count(folder_path: str) -> int:
    try:
        return sum(
            1 for n in os.listdir(folder_path) if n.lower().endswith(AUDIO_EXTS)
        )
    except OSError:
        return 0


@app.route("/api/orphans", methods=["GET"])
def list_orphans():
    """List all orphan entries with their current filesystem state and Lidarr metadata."""
    try:
        state = _get_state()
        items = state.list_orphans()
        # Hide entries marked deleted — they're audit-only
        items = [it for it in items if it.get("status") != State.ORPHAN_STATUS_DELETED]

        for it in items:
            path = it.get("folder_path", "")
            it["folder_exists"] = os.path.isdir(path)
            it["audio_file_count"] = _audio_count(path) if it["folder_exists"] else 0

        # Enrich with Lidarr album metadata (artist, title, year). Query each unique
        # album_id once to avoid hammering Lidarr when several orphans match the
        # same album.
        unique_ids = {it.get("matched_album_id") for it in items if it.get("matched_album_id")}
        album_meta = {}
        if unique_ids:
            try:
                lidarr = _get_lidarr()
                for aid in unique_ids:
                    try:
                        a = lidarr.get_album(aid)
                        # pyarr returns a dict for a single id and a list for multiple
                        if isinstance(a, list):
                            a = a[0] if a else None
                        if not a:
                            continue
                        artist_name = (a.get("artist") or {}).get("artistName") or ""
                        year = (a.get("releaseDate") or "")[:4]
                        album_meta[aid] = {
                            "artist": artist_name,
                            "title": a.get("title") or "",
                            "year": year,
                        }
                    except Exception:
                        continue
            except Exception:
                logger.warning("Failed to build Lidarr client for orphan enrichment", exc_info=True)

        for it in items:
            aid = it.get("matched_album_id")
            meta = album_meta.get(aid) if aid else None
            it["artist"] = meta["artist"] if meta else ""
            it["album_title"] = meta["title"] if meta else ""
            it["year"] = meta["year"] if meta else ""

        return jsonify(items)
    except Exception as e:
        logger.exception("Failed to list orphans")
        return jsonify({"error": str(e)}), 500


@app.route("/api/orphans/preview", methods=["POST"])
def preview_orphan():
    """
    Return Lidarr's manualimport preview for the orphan folder so the UI can
    display per-file rejections, quality, and the matched album.
    """
    data = request.get_json() or {}
    folder = data.get("folder_path")
    if not folder:
        return jsonify({"error": "folder_path required"}), 400
    try:
        import requests as _r
        cfg = _read_config()
        lidarr_path = _to_lidarr_path(folder)
        url = cfg["Lidarr"]["host_url"].rstrip("/")
        r = _r.get(
            f"{url}/api/v1/manualimport",
            params={"folder": lidarr_path},
            headers={"X-Api-Key": cfg["Lidarr"]["api_key"]},
            timeout=30,
        )
        r.raise_for_status()
        return jsonify({"folder_path": folder, "files": r.json()})
    except Exception as e:
        logger.exception(f"Preview failed for {folder}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/orphans/import", methods=["POST"])
def import_orphan():
    """
    Trigger Lidarr ManualImport on an orphan folder.
    `force=true` includes files with soft rejections (Has missing tracks, Album
    match too low, etc.); Lidarr accepts them via the explicit ManualImport
    command.
    """
    data = request.get_json() or {}
    folder = data.get("folder_path")
    force = bool(data.get("force", False))
    if not folder:
        return jsonify({"error": "folder_path required"}), 400
    try:
        lidarr = _get_lidarr()
        lidarr_path = _to_lidarr_path(folder)
        # Reuse the orphans module logic but allow forcing rejected files.
        import requests as _r
        cfg = _read_config()
        url = cfg["Lidarr"]["host_url"].rstrip("/")
        preview = _r.get(
            f"{url}/api/v1/manualimport",
            params={"folder": lidarr_path},
            headers={"X-Api-Key": cfg["Lidarr"]["api_key"]},
            timeout=30,
        ).json()
        accepted = []
        for it in preview:
            if not force and it.get("rejections"):
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
            return jsonify({
                "ok": False,
                "imported_count": 0,
                "accepted_count": 0,
                "candidates": len(preview),
                "message": "No files accepted (use force=true to override soft rejections)",
            })
        cmd = lidarr.post_command(
            name="ManualImport",
            files=accepted,
            importMode="auto",
            replaceExistingFiles=False,
        )
        # Wait for the command and report imported count.
        result = orphans_mod._wait_for_command(lidarr, cmd["id"], timeout=60)
        imported = orphans_mod._parse_imported_count(result.get("message", ""))
        # If anything imported, the folder is considered done — rmtree it entirely
        # (covers residual audio Lidarr didn't accept, covers, .nfo, etc.) and
        # drop the orphan entry. If nothing imported, leave the folder as-is so
        # the user can retry with `force` or take other action.
        state = _get_state()
        if imported > 0:
            if os.path.isdir(folder):
                try:
                    shutil.rmtree(folder)
                except OSError:
                    logger.warning(f"rmtree failed for {folder}", exc_info=True)
            state.remove_orphan(folder)
        else:
            state.mark_orphan_scanned(
                folder,
                status=State.ORPHAN_STATUS_NO_MATCH,
                lidarr_command_id=cmd["id"],
                imported_count=0,
            )
        return jsonify({
            "ok": imported > 0,
            "imported_count": imported,
            "accepted_count": len(accepted),
            "candidates": len(preview),
            "message": result.get("message", ""),
        })
    except Exception as e:
        logger.exception(f"Import failed for {folder}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/orphans/ignore", methods=["POST"])
def ignore_orphan():
    data = request.get_json() or {}
    folder = data.get("folder_path")
    if not folder:
        return jsonify({"error": "folder_path required"}), 400
    try:
        _get_state().mark_orphan_scanned(folder, status=State.ORPHAN_STATUS_IGNORED)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orphans/rescan", methods=["POST"])
def rescan_orphan():
    """Drop the entry so the next scan re-evaluates the folder from scratch."""
    data = request.get_json() or {}
    folder = data.get("folder_path")
    if not folder:
        return jsonify({"error": "folder_path required"}), 400
    try:
        _get_state().remove_orphan(folder)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orphans/delete", methods=["POST"])
def delete_orphan():
    """Delete the orphan folder from disk and mark the entry as deleted."""
    data = request.get_json() or {}
    folder = data.get("folder_path")
    if not folder:
        return jsonify({"error": "folder_path required"}), 400
    try:
        if os.path.isdir(folder):
            shutil.rmtree(folder)
            logger.info(f"Deleted orphan folder: {folder}")
        _get_state().mark_orphan_scanned(folder, status=State.ORPHAN_STATUS_DELETED)
        return jsonify({"ok": True})
    except Exception as e:
        logger.exception(f"Delete failed for {folder}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Soularr Web UI")
    default = "/data" if os.environ.get("IN_DOCKER") else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--var-dir", default=default, help="Directory containing config.ini and soularr.log")
    parser.add_argument("--port", type=int, default=8265, help="Port to listen on (default: 8265)")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    args = parser.parse_args()

    log_path = get_log_path(args.var_dir)
    logger.info(f"Soularr Web UI starting on http://{args.host}:{args.port}")
    logger.info(f"Reading log from: {log_path}")

    serve(app, host=args.host, port=args.port, threads=16)
