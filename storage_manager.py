"""
storage_manager.py - Server Storage Device Discovery, Formatting & Target Management.

Provides full admin capabilities to:
1. Discover all connected storage devices (SATA, NVMe, USB, external drives) using lsblk and mount tables.
2. Inspect filesystem type, capacity, mount status, write permissions, and Mateo app file structure.
3. Format storage devices / mountpoints to the exact file structure required by Mateo:
   - videos/
   - music/
   - photos/
   - files/
   - incoming/
   - user_drive/
   - .mateo_storage (metadata marker)
4. Select which connected storage device / directory is the ACTIVE target for saving data.
   Persists choice across server restarts and updates runtime config seamlessly.
"""

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone

logger = logging.getLogger("mateo.storage")

REQUIRED_APP_DIRS = ("videos", "music", "photos", "files", "incoming", "user_drive")
MARKER_FILENAME = ".mateo_storage"

SYSTEM_CRITICAL_DIRS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64",
    "/proc", "/root", "/run", "/sbin", "/sys", "/usr", "/var/lib", "/var/log"
}


def human_size(num):
    """Format bytes into human-readable string."""
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def check_app_structure(path):
    """
    Check if the specified directory has the Mateo application file structure.
    Returns a dict with health details.
    """
    if not path or not os.path.exists(path) or not os.path.isdir(path):
        return {
            "exists": False,
            "formatted": False,
            "writable": False,
            "existing_folders": [],
            "missing_folders": list(REQUIRED_APP_DIRS),
            "has_marker": False,
            "marker_data": None,
        }

    writable = os.access(path, os.W_OK)
    existing_folders = []
    missing_folders = []

    for d in REQUIRED_APP_DIRS:
        sub = os.path.join(path, d)
        if os.path.isdir(sub):
            existing_folders.append(d)
        else:
            missing_folders.append(d)

    marker_path = os.path.join(path, MARKER_FILENAME)
    has_marker = os.path.isfile(marker_path)
    marker_data = None
    if has_marker:
        try:
            with open(marker_path, "r", encoding="utf-8") as fh:
                marker_data = json.load(fh)
        except Exception:
            marker_data = {"status": "present_unreadable"}

    formatted = (len(missing_folders) == 0)

    return {
        "exists": True,
        "formatted": formatted,
        "writable": writable,
        "existing_folders": existing_folders,
        "missing_folders": missing_folders,
        "has_marker": has_marker,
        "marker_data": marker_data,
        "folders_count": f"{len(existing_folders)}/{len(REQUIRED_APP_DIRS)}",
    }


def format_app_structure(target_path, wipe=False):
    """
    Format a directory or mounted drive to the Mateo app file structure.
    Creates: videos/, music/, photos/, files/, incoming/, user_drive/ and .mateo_storage.
    If wipe is True, clears existing files inside those specific 6 subdirectories only.
    """
    if not target_path or not isinstance(target_path, str):
        return False, "Target path cannot be empty."

    real_path = os.path.realpath(target_path.strip())

    # Protect operating system critical paths
    if real_path in SYSTEM_CRITICAL_DIRS or os.path.dirname(real_path) == real_path:
        return False, f"Refusing to format system critical directory: '{real_path}'"

    # Ensure parent or target exists and is writable
    try:
        os.makedirs(real_path, exist_ok=True)
    except Exception as e:
        return False, f"Failed to access or create directory '{real_path}': {e}"

    if not os.access(real_path, os.W_OK):
        return False, f"Directory '{real_path}' is not writable. Check filesystem permissions or mount options."

    # Test write ability
    test_file = os.path.join(real_path, f".write_test_{os.getpid()}")
    try:
        with open(test_file, "w") as fh:
            fh.write("test")
        os.remove(test_file)
    except Exception as e:
        return False, f"Write check failed on '{real_path}': {e}"

    # If wipe requested, clean inside the designated Mateo folders only
    if wipe:
        for d in REQUIRED_APP_DIRS:
            sub = os.path.join(real_path, d)
            if os.path.isdir(sub):
                try:
                    for item in os.listdir(sub):
                        item_path = os.path.join(sub, item)
                        if os.path.isdir(item_path):
                            shutil.rmtree(item_path, ignore_errors=True)
                        else:
                            os.remove(item_path)
                except Exception as e:
                    logger.warning(f"Error while wiping '{sub}': {e}")

    # Create the required 6 application folders
    created_folders = []
    for d in REQUIRED_APP_DIRS:
        sub = os.path.join(real_path, d)
        os.makedirs(sub, exist_ok=True)
        created_folders.append(d)

    # Write marker metadata file
    marker_content = {
        "app": "Mateo Media Server",
        "version": "1.0",
        "formatted_at": datetime.now(timezone.utc).isoformat(),
        "target_path": real_path,
        "directories": list(REQUIRED_APP_DIRS),
        "status": "ready",
    }
    marker_path = os.path.join(real_path, MARKER_FILENAME)
    try:
        with open(marker_path, "w", encoding="utf-8") as fh:
            json.dump(marker_content, fh, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write marker file in '{real_path}': {e}")

    action = "Wiped and formatted" if wipe else "Formatted"
    return True, f"{action} storage at '{real_path}' with Mateo app file structure ({', '.join(created_folders)})."


def _get_storage_config_path(instance_dir):
    return os.path.join(instance_dir, "storage_config.json")


def load_stored_active_storage(instance_dir, default_media_root):
    """Load previously saved active storage target path, falling back to default."""
    cfg_path = _get_storage_config_path(instance_dir)
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                saved = data.get("active_media_root")
                if saved and os.path.exists(saved) and os.path.isdir(saved):
                    return os.path.realpath(saved)
        except Exception as e:
            logger.warning(f"Failed to read storage_config.json: {e}")
    return os.path.realpath(default_media_root)


def set_active_storage(app, target_path, auto_format=True):
    """
    Set the active storage target for saving data.
    Updates app.config['MEDIA_ROOT'] and persists in storage_config.json and DB.
    """
    if not target_path or not isinstance(target_path, str):
        return False, "Target path cannot be empty."

    real_path = os.path.realpath(target_path.strip())

    if not os.path.exists(real_path) or not os.path.isdir(real_path):
        return False, f"Directory does not exist: '{real_path}'"

    if not os.access(real_path, os.W_OK):
        return False, f"Directory '{real_path}' is not writable. Cannot save data there."

    # Auto-format or verify structure
    health = check_app_structure(real_path)
    if not health["formatted"]:
        if auto_format:
            success, msg = format_app_structure(real_path, wipe=False)
            if not success:
                return False, f"Failed to initialize app file structure: {msg}"
        else:
            return False, f"Directory '{real_path}' is missing required folders: {health['missing_folders']}"

    # Update Flask application config
    app.config["MEDIA_ROOT"] = real_path
    app.config["TORRENT_INCOMING_DIR"] = os.path.join(real_path, "incoming")

    # Persist in instance/storage_config.json
    instance_dir = app.instance_path
    os.makedirs(instance_dir, exist_ok=True)
    cfg_path = _get_storage_config_path(instance_dir)
    try:
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({
                "active_media_root": real_path,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, fh, indent=2)
    except Exception as e:
        logger.warning(f"Could not persist to storage_config.json: {e}")

    # Persist in SQLite DB system_settings table if possible
    try:
        import models
        models.set_setting("active_media_root", real_path)
    except Exception as e:
        logger.debug(f"DB setting update note: {e}")

    return True, f"Active storage device switched to '{real_path}'. All future uploads and torrents will save here."


def get_active_storage_info(app_config):
    """Retrieve detailed stats and structure health for the currently active storage target."""
    active_root = os.path.realpath(app_config.get("MEDIA_ROOT", ""))
    default_root = os.path.realpath(app_config.get("DEFAULT_MEDIA_ROOT", active_root))

    usage = {"total": 0, "used": 0, "free": 0, "percent": 0}
    try:
        if os.path.exists(active_root):
            u = shutil.disk_usage(active_root)
            usage = {
                "total": u.total,
                "used": u.used,
                "free": u.free,
                "percent": round((u.used / u.total) * 100, 1) if u.total else 0,
            }
    except Exception:
        pass

    health = check_app_structure(active_root)

    return {
        "path": active_root,
        "is_default": (active_root == default_root),
        "total_bytes": usage["total"],
        "used_bytes": usage["used"],
        "free_bytes": usage["free"],
        "total_human": human_size(usage["total"]),
        "used_human": human_size(usage["used"]),
        "free_human": human_size(usage["free"]),
        "percent_used": usage["percent"],
        "writable": health["writable"],
        "formatted": health["formatted"],
        "existing_folders": health["existing_folders"],
        "missing_folders": health["missing_folders"],
        "folders_count": health["folders_count"],
        "has_marker": health["has_marker"],
    }


def list_connected_devices(app_config):
    """
    Enumerate all connected storage devices and partitions using lsblk,
    enriching each with mount information, usage stats, and Mateo app file structure.
    Also compiles available storage targets that can be formatted or chosen.
    """
    active_root = os.path.realpath(app_config.get("MEDIA_ROOT", ""))
    default_root = os.path.realpath(app_config.get("DEFAULT_MEDIA_ROOT", active_root))

    raw_block_devices = []
    try:
        cmd = [
            "lsblk", "-J", "-b",
            "-o", "NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINT,MOUNTPOINTS,MODEL,RO,RM,HOTPLUG,UUID,LABEL,STATE"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=5)
        parsed = json.loads(res.stdout)
        raw_block_devices = parsed.get("blockdevices", [])
    except Exception as e:
        logger.warning(f"lsblk enumeration failed or unavailable: {e}")

    # Build structured device list
    devices = []
    storage_targets = []
    seen_paths = set()

    # Always add Default Application Storage as a primary option
    default_usage = {"total": 0, "used": 0, "free": 0, "percent": 0}
    try:
        if os.path.exists(default_root):
            u = shutil.disk_usage(default_root)
            default_usage = {
                "total": u.total, "used": u.used, "free": u.free,
                "percent": round((u.used / u.total) * 100, 1) if u.total else 0
            }
    except Exception:
        pass

    default_health = check_app_structure(default_root)
    storage_targets.append({
        "label": "Default App Media Storage",
        "device_name": "Internal Application Root",
        "path": default_root,
        "is_active": (active_root == default_root),
        "is_default": True,
        "fstype": "Local Host Filesystem",
        "total_human": human_size(default_usage["total"]),
        "used_human": human_size(default_usage["used"]),
        "free_human": human_size(default_usage["free"]),
        "percent_used": default_usage["percent"],
        "writable": default_health["writable"],
        "formatted": default_health["formatted"],
        "folders_count": default_health["folders_count"],
    })
    seen_paths.add(default_root)

    # If active storage is not default, add it to seen
    if active_root not in seen_paths:
        active_info = get_active_storage_info(app_config)
        storage_targets.append({
            "label": "Custom Active Storage Target",
            "device_name": "Active Media Root",
            "path": active_root,
            "is_active": True,
            "is_default": False,
            "fstype": "Mounted Storage",
            "total_human": active_info["total_human"],
            "used_human": active_info["used_human"],
            "free_human": active_info["free_human"],
            "percent_used": active_info["percent_used"],
            "writable": active_info["writable"],
            "formatted": active_info["formatted"],
            "folders_count": active_info["folders_count"],
        })
        seen_paths.add(active_root)

    # Process block devices from lsblk
    for dev in raw_block_devices:
        dev_name = dev.get("name") or "unknown"
        dev_path = dev.get("path") or f"/dev/{dev_name}"
        dev_model = (dev.get("model") or "").strip()
        dev_size = dev.get("size") or 0
        dev_type = dev.get("type") or "disk"
        is_removable = bool(dev.get("rm") or dev.get("hotplug"))
        is_ro = bool(dev.get("ro"))

        # Determine device display kind
        kind = "SSD / HDD"
        if is_removable:
            kind = "USB / Removable Drive"
        elif "nvme" in dev_name:
            kind = "NVMe High-Speed SSD"
        elif dev_type == "loop":
            kind = "Virtual Loop Device"

        partitions = []
        children = dev.get("children") or []

        # If disk has no partition children, check if the disk itself is formatted / mounted
        items_to_check = children if children else [dev]

        for item in items_to_check:
            p_name = item.get("name") or dev_name
            p_path = item.get("path") or f"/dev/{p_name}"
            p_size = item.get("size") or 0
            p_fstype = item.get("fstype") or "None (Unformatted)"
            p_uuid = item.get("uuid") or ""
            p_label = item.get("label") or ""

            # Extract mountpoints
            mps = item.get("mountpoints") or []
            if not mps and item.get("mountpoint"):
                mps = [item.get("mountpoint")]
            mps = [m for m in mps if m and m != "[SWAP]"]

            part_info = {
                "name": p_name,
                "path": p_path,
                "size_bytes": p_size,
                "size_human": human_size(p_size),
                "fstype": p_fstype,
                "uuid": p_uuid,
                "label": p_label,
                "is_mounted": len(mps) > 0,
                "mountpoints": mps,
                "mount_targets": [],
            }

            for mp in mps:
                real_mp = os.path.realpath(mp)
                mp_usage = {"total": 0, "used": 0, "free": 0, "percent": 0}
                try:
                    u = shutil.disk_usage(real_mp)
                    mp_usage = {
                        "total": u.total, "used": u.used, "free": u.free,
                        "percent": round((u.used / u.total) * 100, 1) if u.total else 0
                    }
                except Exception:
                    pass

                mp_health = check_app_structure(real_mp)
                is_active = (real_mp == active_root)

                target_item = {
                    "device_name": f"{p_name} ({p_path})",
                    "label": f"{p_name} @ {mp}",
                    "path": real_mp,
                    "mountpoint": mp,
                    "fstype": p_fstype,
                    "is_active": is_active,
                    "is_default": (real_mp == default_root),
                    "total_human": human_size(mp_usage["total"]),
                    "used_human": human_size(mp_usage["used"]),
                    "free_human": human_size(mp_usage["free"]),
                    "percent_used": mp_usage["percent"],
                    "writable": mp_health["writable"],
                    "formatted": mp_health["formatted"],
                    "folders_count": mp_health["folders_count"],
                    "existing_folders": mp_health["existing_folders"],
                    "missing_folders": mp_health["missing_folders"],
                }
                part_info["mount_targets"].append(target_item)

                if real_mp not in seen_paths:
                    storage_targets.append(target_item)
                    seen_paths.add(real_mp)

            if children:
                partitions.append(part_info)

        devices.append({
            "name": dev_name,
            "path": dev_path,
            "model": dev_model or "Standard Storage Block Device",
            "size_bytes": dev_size,
            "size_human": human_size(dev_size),
            "type": dev_type,
            "kind": kind,
            "is_removable": is_removable,
            "is_ro": is_ro,
            "partitions": partitions,
            "has_partitions": len(partitions) > 0,
            "standalone": len(children) == 0,
            "fstype": dev.get("fstype") or ("Multiple Partitions" if children else "Unformatted"),
        })

    return {
        "active_storage": get_active_storage_info(app_config),
        "block_devices": devices,
        "storage_targets": storage_targets,
    }


def init_storage(app):
    """
    Application startup initialization for storage.
    Ensures active MEDIA_ROOT is loaded and initial folders are in place.
    """
    default_root = os.path.realpath(app.config.get("MEDIA_ROOT", os.path.join(app.root_path, "media")))
    app.config["DEFAULT_MEDIA_ROOT"] = default_root

    active_root = load_stored_active_storage(app.instance_path, default_root)
    app.config["MEDIA_ROOT"] = active_root
    app.config["TORRENT_INCOMING_DIR"] = os.path.join(active_root, "incoming")

    # Ensure required folders exist on active root
    for d in REQUIRED_APP_DIRS:
        os.makedirs(os.path.join(active_root, d), exist_ok=True)

    logger.info(f"Initialized active media storage target at: {active_root}")
