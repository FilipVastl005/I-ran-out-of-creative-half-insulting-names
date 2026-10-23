"""
downloader.py - Background torrent ingestion and lifecycle manager for Mateo.

Optimized for low-spec ThinkPad T530/T430s running on a 500GB HDD:
- Uses transmission-rpc to delegate heavy peer-to-peer downloading to transmission-daemon.
- Downloads directly into target media directories so no heavy copy/move operations thrash the HDD.
- Automatically monitors download completion and indexes files into the SQLite media library
  with the appropriate age-restriction access control.
- Operates safely even when transmission-daemon is temporarily offline or not yet installed.
"""

import logging
import mimetypes
import os
import re
import threading
import time
from urllib.parse import parse_qs, unquote_plus, urlparse

import models

logger = logging.getLogger("mateo.downloader")


# --------------------------------------------------------------------------- #
# Transmission Client Connection
# --------------------------------------------------------------------------- #
def get_client(config):
    """
    Connect to the local transmission-daemon RPC interface.
    Returns transmission_rpc.Client or None if unreachable.
    """
    try:
        import transmission_rpc
        host = config.get("TRANSMISSION_HOST", "127.0.0.1")
        port = int(config.get("TRANSMISSION_PORT", 9091))
        user = config.get("TRANSMISSION_USER") or None
        password = config.get("TRANSMISSION_PASS") or None

        client = transmission_rpc.Client(
            host=host,
            port=port,
            username=user,
            password=password,
            timeout=4.0,  # low timeout to never stall Flask threads
        )
        # Test basic connection with a fast RPC call
        client.get_session()
        return client
    except Exception as e:
        logger.debug("Transmission daemon unavailable: %s", e)
        return None


def is_daemon_online(config):
    """Fast check whether transmission-daemon is responding."""
    return get_client(config) is not None


# --------------------------------------------------------------------------- #
# Torrent Source Ingestion & Parsing
# --------------------------------------------------------------------------- #
def parse_torrent_name(source):
    """Extract or deduce a human-readable name from a magnet link or torrent URL."""
    source = (source or "").strip()
    if source.startswith("magnet:"):
        # Magnet link: look for dn= parameter
        match = re.search(r"[?&]dn=([^&]+)", source)
        if match:
            return unquote_plus(match.group(1)).replace("+", " ").strip()
        # Fallback to info hash preview
        xt_match = re.search(r"urn:btih:([a-zA-Z0-9]+)", source)
        if xt_match:
            return f"Magnet-{xt_match.group(1)[:10]}"
        return "Magnet Download"

    # URL or file path: use the base filename
    parsed = urlparse(source)
    path = parsed.path or source
    basename = os.path.basename(path)
    if basename:
        name = unquote_plus(basename)
        if name.lower().endswith(".torrent"):
            name = name[:-8]
        return name.strip() or "Torrent Download"

    return "Torrent Download"


def validate_torrent_source(source):
    """Validate that the source is a valid magnet link or HTTP/HTTPS torrent URL."""
    s = (source or "").strip()
    if s.startswith("magnet:?xt=urn:btih:") or s.startswith("magnet:?xt=urn:btmh:"):
        return True
    if s.startswith("http://") or s.startswith("https://"):
        return True
    return False


def get_target_download_dir(app_config, category, subfolder=""):
    """
    Determine the direct target directory on the 500GB HDD for this category.
    Downloading directly to the target directory eliminates post-download file moves
    which would otherwise create severe disk I/O bottlenecks on spinning drives.
    """
    cat_dir = app_config["CATEGORY_DIRS"].get(category, "files")
    target = os.path.join(app_config["MEDIA_ROOT"], cat_dir)
    if subfolder:
        parts = [p for p in subfolder.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        if parts:
            target = os.path.join(target, *parts)
    os.makedirs(target, exist_ok=True)
    return target


def ingest_torrent(app, source, category, min_age=0, age_restricted=False, added_by=None, subfolder=""):
    """
    Ingest a magnet link or .torrent URL:
    1. Validates parameters.
    2. Hands off to Transmission if online, pointing download_dir to the category folder.
    3. Saves record into SQLite torrents table.
    """
    source = (source or "").strip()
    if not validate_torrent_source(source):
        raise ValueError("Invalid torrent source. Must be a magnet link (magnet:?xt=urn:btih:...) or HTTP/HTTPS URL.")

    if category not in models.CATEGORIES:
        category = "video"

    min_age_val = max(0, int(min_age or 0))
    if age_restricted and min_age_val == 0:
        min_age_val = 18

    target_dir = get_target_download_dir(app.config, category, subfolder)
    display_name = parse_torrent_name(source)

    client = get_client(app.config)
    transmission_id = None
    status = "queued (daemon offline)"

    if client is not None:
        try:
            torrent = client.add_torrent(source, download_dir=target_dir)
            transmission_id = torrent.id
            if getattr(torrent, "name", None):
                display_name = torrent.name
            status = "downloading"
            logger.info("Queued torrent '%s' in Transmission (ID=%s)", display_name, transmission_id)
        except Exception as e:
            logger.warning("Failed to submit torrent to Transmission: %s", e)
            status = f"queued ({e})"
    else:
        logger.info("Transmission offline. Torrent '%s' saved to database queue.", display_name)

    torrent_id = models.add_torrent(
        name=display_name,
        source=source,
        category=category,
        min_age=min_age_val,
        age_restricted=bool(age_restricted or min_age_val >= 18),
        added_by=added_by,
        transmission_id=transmission_id,
        status=status,
    )

    return {
        "id": torrent_id,
        "name": display_name,
        "transmission_id": transmission_id,
        "status": status,
        "online": client is not None,
    }


def ingest_torrent_file(app, file_storage, category, min_age=0, age_restricted=False, added_by=None, subfolder=""):
    """
    Ingest an uploaded .torrent binary file from a form upload.
    Saves the .torrent file to the incoming dir and passes it to transmission.
    """
    if not file_storage or not file_storage.filename:
        raise ValueError("No .torrent file provided.")

    if category not in models.CATEGORIES:
        category = "video"

    min_age_val = max(0, int(min_age or 0))
    if age_restricted and min_age_val == 0:
        min_age_val = 18

    raw_filename = os.path.basename(file_storage.filename)
    if not raw_filename.lower().endswith(".torrent"):
        raise ValueError("Uploaded file must have a .torrent extension.")

    incoming_dir = app.config.get("TORRENT_INCOMING_DIR", os.path.join(app.config["MEDIA_ROOT"], "incoming"))
    os.makedirs(incoming_dir, exist_ok=True)

    dest_torrent_path = os.path.join(incoming_dir, raw_filename)
    file_storage.save(dest_torrent_path)

    display_name = raw_filename[:-8] if raw_filename.lower().endswith(".torrent") else raw_filename
    target_dir = get_target_download_dir(app.config, category, subfolder)

    client = get_client(app.config)
    transmission_id = None
    status = "queued (daemon offline)"

    if client is not None:
        try:
            with open(dest_torrent_path, "rb") as fh:
                torrent = client.add_torrent(fh, download_dir=target_dir)
                transmission_id = torrent.id
                if getattr(torrent, "name", None):
                    display_name = torrent.name
                status = "downloading"
        except Exception as e:
            logger.warning("Transmission error with uploaded .torrent: %s", e)
            status = f"queued ({e})"

    torrent_id = models.add_torrent(
        name=display_name,
        source=f"file://{raw_filename}",
        category=category,
        min_age=min_age_val,
        age_restricted=bool(age_restricted or min_age_val >= 18),
        added_by=added_by,
        transmission_id=transmission_id,
        status=status,
    )

    return {
        "id": torrent_id,
        "name": display_name,
        "transmission_id": transmission_id,
        "status": status,
        "online": client is not None,
    }


# --------------------------------------------------------------------------- #
# Completed Media Indexing (Auto-Import into SQLite)
# --------------------------------------------------------------------------- #
def import_completed_torrent_files(app, torrent_record):
    """
    When a torrent finishes downloading, scan its target directory and register
    any new media items into the SQLite database.
    Users under min_age will have content automatically restricted.
    """
    media_root = os.path.realpath(app.config["MEDIA_ROOT"])
    cat = torrent_record["category"]
    cat_dir_name = app.config["CATEGORY_DIRS"].get(cat, "files")
    cat_root = os.path.join(media_root, cat_dir_name)

    if not os.path.exists(cat_root):
        return 0

    new_indexed_count = 0
    t_min_age = 0
    try:
        t_min_age = int(torrent_record["min_age"])
    except (KeyError, TypeError, ValueError):
        t_min_age = 18 if torrent_record["age_restricted"] else 0

    # Scan the category directory tree for newly downloaded files
    for dirpath, _, filenames in os.walk(cat_root):
        rel_dir = os.path.relpath(dirpath, cat_root)
        folder = "" if rel_dir == "." else rel_dir.replace("\\", "/")

        for fname in filenames:
            # Skip hidden, system, or incomplete torrent part files
            if fname.startswith(".") or fname.endswith(".part"):
                continue

            full_path = os.path.join(dirpath, fname)
            rel_path = f"{cat_dir_name}/{folder + '/' if folder else ''}{fname}"

            # Check if this file is already in the SQLite database
            if models.get_media_by_path(rel_path) is None:
                size_bytes = os.path.getsize(full_path)
                # Ignore empty marker files
                if size_bytes == 0:
                    continue

                mime_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
                name_root, _ = os.path.splitext(fname)
                title = name_root.replace(".", " ").replace("-", " ").replace("_", " ").strip() or fname

                try:
                    models.add_media(
                        title=title,
                        category=cat,
                        rel_path=rel_path,
                        folder=folder,
                        filename=fname,
                        mime_type=mime_type,
                        size_bytes=size_bytes,
                        min_age=t_min_age,
                        age_restricted=bool(torrent_record["age_restricted"] or t_min_age >= 18),
                        uploaded_by=torrent_record["added_by"],
                    )
                    new_indexed_count += 1
                    logger.info("Auto-indexed completed torrent media: %s (%s)", rel_path, cat)
                except Exception as e:
                    logger.debug("Could not add media %s: %s", rel_path, e)

    return new_indexed_count


# --------------------------------------------------------------------------- #
# Live Stats & Controller
# --------------------------------------------------------------------------- #
def format_speed(bps):
    """Format bytes per second into human-readable rate."""
    bps = float(bps or 0)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024 or unit == "GB/s":
            return f"{bps:.1f} {unit}"
        bps /= 1024
    return f"{bps:.1f} MB/s"


def format_eta_seconds(seconds):
    """Format estimated time arrival."""
    if seconds is None or seconds < 0:
        return "-"
    if seconds == 0:
        return "Complete"
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m {secs}s"


def get_live_torrents(app):
    """
    Returns list of torrent records combined with live transmission status.
    """
    records = models.list_torrents()
    client = get_client(app.config)

    transmission_map = {}
    if client is not None:
        try:
            for t in client.get_torrents():
                transmission_map[t.id] = t
        except Exception as e:
            logger.debug("Error fetching transmission torrents: %s", e)

    results = []
    for r in records:
        t_id = r["transmission_id"]
        live = transmission_map.get(t_id) if t_id else None

        if live is not None:
            # Calculate progress percentage (0.0 - 100.0)
            progress = getattr(live, "percent_done", 0.0) or getattr(live, "progress", 0.0) or 0.0
            percent_pct = round(float(progress) * 100, 1)

            rate_dl = getattr(live, "rate_download", 0)
            rate_ul = getattr(live, "rate_upload", 0)
            eta_val = getattr(live, "eta", None)

            # Determine human-readable status
            status_str = getattr(live, "status", None)
            if hasattr(status_str, "value"):
                status_str = status_str.value
            status_str = str(status_str or "downloading").lower()

            results.append({
                "id": r["id"],
                "transmission_id": t_id,
                "name": getattr(live, "name", r["name"]) or r["name"],
                "source": r["source"],
                "category": r["category"],
                "age_restricted": bool(r["age_restricted"]),
                "imported": bool(r["imported"]),
                "status": status_str,
                "progress": percent_pct,
                "rate_download": format_speed(rate_dl),
                "rate_upload": format_speed(rate_ul),
                "eta": format_eta_seconds(eta_val),
                "total_size": getattr(live, "total_size", 0),
                "created_at": r["created_at"],
                "added_by_name": r["added_by_name"],
                "can_pause": status_str in ("downloading", "seeding"),
                "can_resume": status_str in ("stopped", "paused"),
            })
        else:
            results.append({
                "id": r["id"],
                "transmission_id": t_id,
                "name": r["name"],
                "source": r["source"],
                "category": r["category"],
                "age_restricted": bool(r["age_restricted"]),
                "imported": bool(r["imported"]),
                "status": r["status"],
                "progress": 100.0 if r["imported"] else 0.0,
                "rate_download": "-",
                "rate_upload": "-",
                "eta": "-",
                "total_size": 0,
                "created_at": r["created_at"],
                "added_by_name": r["added_by_name"],
                "can_pause": False,
                "can_resume": False,
            })

    return results


def pause_torrent(app, torrent_id):
    """Pause an active torrent in transmission."""
    record = models.get_torrent(torrent_id)
    if not record or not record["transmission_id"]:
        return False
    client = get_client(app.config)
    if client is None:
        return False
    try:
        client.stop_torrent(record["transmission_id"])
        models.update_torrent(torrent_id, status="paused")
        return True
    except Exception as e:
        logger.error("Failed to pause torrent: %s", e)
        return False


def resume_torrent(app, torrent_id):
    """Resume a paused torrent in transmission."""
    record = models.get_torrent(torrent_id)
    if not record or not record["transmission_id"]:
        return False
    client = get_client(app.config)
    if client is None:
        return False
    try:
        client.start_torrent(record["transmission_id"])
        models.update_torrent(torrent_id, status="downloading")
        return True
    except Exception as e:
        logger.error("Failed to resume torrent: %s", e)
        return False


def remove_torrent(app, torrent_id, delete_data=False):
    """Remove a torrent from transmission and SQLite."""
    record = models.get_torrent(torrent_id)
    if record and record["transmission_id"]:
        client = get_client(app.config)
        if client is not None:
            try:
                client.remove_torrent(record["transmission_id"], delete_data=delete_data)
            except Exception as e:
                logger.debug("Transmission remove notice: %s", e)
    models.delete_torrent(torrent_id)


# --------------------------------------------------------------------------- #
# Low-I/O Background Worker Thread
# --------------------------------------------------------------------------- #
class TorrentBackgroundWorker:
    """
    Background worker that monitors torrents at low frequency (12-15s),
    submits queued torrents when Transmission comes online, and automatically
    imports completed media into SQLite without user intervention.
    """
    def __init__(self, app, interval=12):
        self.app = app
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="MateoTorrentWorker", daemon=True)
        self._thread.start()
        logger.info("Mateo Torrent background worker started (interval=%ss)", self.interval)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        while not self._stop_event.is_set():
            try:
                with self.app.app_context():
                    self._tick()
            except Exception as e:
                logger.debug("Torrent background worker tick error: %s", e)
            self._stop_event.wait(self.interval)

    def _tick(self):
        client = get_client(self.app.config)
        if client is None:
            return  # Transmission daemon not online, sleep until next check

        # 1. Process any pending/queued torrents that were added while daemon was offline
        pending = models.get_pending_torrents()
        for p in pending:
            try:
                target_dir = get_target_download_dir(self.app.config, p["category"])
                t = client.add_torrent(p["source"], download_dir=target_dir)
                t_name = getattr(t, "name", p["name"]) or p["name"]
                models.update_torrent(p["id"], transmission_id=t.id, name=t_name, status="downloading")
                logger.info("Submitted queued torrent #%s to Transmission (ID=%s)", p["id"], t.id)
            except Exception as e:
                logger.debug("Could not dispatch queued torrent #%s: %s", p["id"], e)

        # 2. Check active torrents for completion
        try:
            live_torrents = client.get_torrents()
        except Exception:
            return

        live_map = {t.id: t for t in live_torrents}
        unimported = models.get_unimported_torrents()

        for u in unimported:
            live = live_map.get(u["transmission_id"])
            if not live:
                continue

            # Check if torrent has finished downloading
            pct = getattr(live, "percent_done", 0.0) or getattr(live, "progress", 0.0) or 0.0
            is_finished = getattr(live, "is_finished", False) or pct >= 1.0 or getattr(live, "left_until_done", 1) == 0

            status_str = str(getattr(live, "status", "")).lower()
            if "seed" in status_str or "complete" in status_str:
                is_finished = True

            if is_finished:
                # Trigger automatic media library import
                indexed = import_completed_torrent_files(self.app, u)
                models.update_torrent(u["id"], status="completed", imported=True)
                logger.info("Torrent #%s '%s' completed! Indexed %d media items.", u["id"], u["name"], indexed)


_worker = None


def init_app(app):
    """Initialize downloader background worker with the Flask application."""
    global _worker
    # Ensure worker starts only once per process
    if _worker is None:
        _worker = TorrentBackgroundWorker(app)
        _worker.start()
