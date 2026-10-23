"""
app.py - Mateo: a lightweight all-in-one home media server.

Video / Music / Photos / Files, served by direct-play only (no transcoding).
Designed for old hardware (ThinkPad T430s/T530 + HDD): low CPU, low disk I/O.

Run (development / small household):
    python app.py
Create users from the shell:
    flask --app app create-user alice --role admin
"""

import functools
import hmac
import mimetypes
import os
import re
import secrets
import threading
import shutil
import time
from datetime import timedelta
from email.utils import formatdate, parsedate_to_datetime
from urllib.parse import quote, unquote_plus

import click
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from jinja2 import TemplateNotFound
from werkzeug.exceptions import RequestedRangeNotSatisfiable
from werkzeug.utils import secure_filename

import downloader
import models

# --------------------------------------------------------------------------- #
# Paths & configuration
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.environ.get("MATEO_INSTANCE_DIR", os.path.join(BASE_DIR, "instance"))
MEDIA_ROOT = os.environ.get("MATEO_MEDIA_ROOT", os.path.join(BASE_DIR, "media"))

# category -> directory name inside MEDIA_ROOT
CATEGORY_DIRS = {"video": "videos", "music": "music", "photo": "photos", "file": "files"}
TORRENT_INCOMING_DIR = "incoming"

os.makedirs(INSTANCE_DIR, exist_ok=True)
for _d in list(CATEGORY_DIRS.values()) + [TORRENT_INCOMING_DIR, models.USER_DRIVE_DIR]:
    os.makedirs(os.path.join(MEDIA_ROOT, _d), exist_ok=True)


def _load_secret_key():
    """Use $MATEO_SECRET_KEY, else a persistent random key in instance/secret_key."""
    env_key = os.environ.get("MATEO_SECRET_KEY")
    if env_key:
        return env_key
    path = os.path.join(INSTANCE_DIR, "secret_key")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


app = Flask(__name__, instance_path=INSTANCE_DIR)
app.config.update(
    SECRET_KEY=_load_secret_key(),
    DATABASE=os.path.join(INSTANCE_DIR, "mateo.db"),
    MEDIA_ROOT=MEDIA_ROOT,
    CATEGORY_DIRS=CATEGORY_DIRS,
    TORRENT_INCOMING_DIR=os.path.join(MEDIA_ROOT, TORRENT_INCOMING_DIR),
    # Uploads: cap a single request at 20 GB by default (override via env, in bytes).
    MAX_CONTENT_LENGTH=int(os.environ.get("MATEO_MAX_UPLOAD", 20 * 1024 ** 3)),
    # 512 KiB reads: large enough for efficient sequential HDD reads,
    # small enough to keep per-stream RAM tiny.
    STREAM_CHUNK_SIZE=512 * 1024,
    PHOTOS_PER_PAGE=60,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("MATEO_SECURE_COOKIES", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    TRANSMISSION_HOST=os.environ.get("MATEO_TRANSMISSION_HOST", "127.0.0.1"),
    TRANSMISSION_PORT=int(os.environ.get("MATEO_TRANSMISSION_PORT", 9091)),
    TRANSMISSION_USER=os.environ.get("MATEO_TRANSMISSION_USER") or None,
    TRANSMISSION_PASS=os.environ.get("MATEO_TRANSMISSION_PASS") or None,
)

# Make sure common media types resolve correctly (distro mime DBs vary).
for _ext, _type in {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".avif": "image/avif", ".heic": "image/heic",
    ".vtt": "text/vtt", ".srt": "application/x-subrip", ".pdf": "application/pdf",
}.items():
    mimetypes.add_type(_type, _ext)

models.init_app(app)
downloader.init_app(app)


# --------------------------------------------------------------------------- #
# Template helpers
# --------------------------------------------------------------------------- #
def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


@app.template_filter("human_size")
def human_size(num):
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


@app.context_processor
def inject_globals():
    user = g.get("user")
    return {
        "current_user": user,
        "is_admin": bool(user and user["role"] == "admin"),
        "csrf_token": csrf_token,
    }


# --------------------------------------------------------------------------- #
# Request hooks: CSRF, session -> user, first-run setup gate
# --------------------------------------------------------------------------- #
_setup_done = False


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("_csrf", "")
        if not sent or not expected or not hmac.compare_digest(sent, expected):
            abort(400, description="Invalid or missing CSRF token. Reload the page and try again.")


@app.before_request
def load_user():
    global _setup_done
    g.user = None
    if request.endpoint == "static":
        return None  # no DB hit for CSS/JS

    if not _setup_done:
        if models.count_users() > 0:
            _setup_done = True
        elif request.endpoint != "setup":
            return redirect(url_for("setup"))

    uid = session.get("uid")
    if uid is None:
        return None
    user = models.get_user_by_id(uid)
    # pwv = fingerprint of the password hash -> changing a password logs out all sessions.
    if user is None or not user["is_active"] or session.get("pwv") != _pw_fingerprint(user):
        session.clear()
        return None
    g.user = user
    return None


def _pw_fingerprint(user):
    return user["password_hash"][-16:]


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            nxt = request.full_path if request.query_string else request.path
            return redirect(url_for("login", next=nxt))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


# --------------------------------------------------------------------------- #
# Simple in-memory login throttle (per IP)
# --------------------------------------------------------------------------- #
_LOGIN_WINDOW = 15 * 60
_LOGIN_MAX_FAILS = 10
_login_fails = {}
_login_lock = threading.Lock()


def _login_blocked(ip):
    with _login_lock:
        entry = _login_fails.get(ip)
        if entry and time.monotonic() - entry[1] > _LOGIN_WINDOW:
            _login_fails.pop(ip, None)
            return False
        return bool(entry and entry[0] >= _LOGIN_MAX_FAILS)


def _register_login_fail(ip):
    with _login_lock:
        count, first = _login_fails.get(ip, (0, time.monotonic()))
        _login_fails[ip] = (count + 1, first)


def _clear_login_fails(ip):
    with _login_lock:
        _login_fails.pop(ip, None)


def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("home")


# --------------------------------------------------------------------------- #
# Path / folder helpers
# --------------------------------------------------------------------------- #
def clean_folder(folder):
    """Normalise a user-supplied sub-folder ('a/b'); reject traversal."""
    parts = [p for p in (folder or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        abort(400, description="Invalid folder.")
    return "/".join(parts)


def breadcrumbs(folder):
    crumbs, acc = [], []
    for part in folder.split("/") if folder else []:
        acc.append(part)
        crumbs.append({"name": part, "path": "/".join(acc)})
    return crumbs


def resolve_media_path(rel_path):
    """Absolute path for a DB rel_path, guaranteed to stay inside MEDIA_ROOT."""
    root = os.path.realpath(app.config["MEDIA_ROOT"])
    full = os.path.realpath(os.path.join(root, rel_path))
    if os.path.commonpath([root, full]) != root or not os.path.isfile(full):
        abort(404)
    return full


# --------------------------------------------------------------------------- #
# Direct-play streaming with HTTP Range support (RFC 7233)
# --------------------------------------------------------------------------- #
def parse_range(header, size):
    """
    Parse a single 'bytes=' range.
    Returns (start, end) inclusive, or None if the header should be ignored
    (malformed / multi-range -> serve the full file with 200, as RFC allows).
    Raises 416 if the range is syntactically valid but unsatisfiable.
    """
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, sep, last = spec.strip().partition("-")
    first, last = first.strip(), last.strip()
    if not sep or not re.fullmatch(r"[0-9]*", first) or not re.fullmatch(r"[0-9]*", last):
        return None

    if first == "":                      # suffix range: last N bytes
        if last == "":
            return None
        suffix = int(last)
        if suffix == 0 or size == 0:
            raise RequestedRangeNotSatisfiable(length=size)
        return max(0, size - suffix), size - 1

    start = int(first)
    if start >= size:
        raise RequestedRangeNotSatisfiable(length=size)
    end = size - 1 if last == "" else min(int(last), size - 1)
    if end < start:
        return None
    return start, end


def _file_chunks(path, start, length, chunk_size):
    """Lazily read [start, start+length). File opens on first iteration only."""
    with open(path, "rb", buffering=0) as fh:
        if hasattr(os, "posix_fadvise"):
            try:  # tell the kernel to read ahead aggressively - great for HDDs
                os.posix_fadvise(fh.fileno(), start, length, os.POSIX_FADV_SEQUENTIAL)
            except OSError:
                pass
        fh.seek(start)
        remaining = length
        while remaining > 0:
            data = fh.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def _content_disposition(filename):
    fallback = filename.encode("ascii", "ignore").decode("ascii")
    fallback = fallback.replace('"', "").replace("\\", "").strip() or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def send_ranged(path, mimetype, download_name=None):
    st = os.stat(path)
    size = st.st_size
    mtime = int(st.st_mtime)
    etag = f'"{st.st_ino:x}-{size:x}-{mtime:x}"'
    last_modified = formatdate(mtime, usegmt=True)

    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Last-Modified": last_modified,
        "Cache-Control": "private, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    }
    if download_name:
        headers["Content-Disposition"] = _content_disposition(download_name)

    # Conditional GET -> 304 means zero disk reads for already-cached photos etc.
    inm = request.headers.get("If-None-Match")
    if inm:
        tags = [t.strip() for t in inm.split(",")]
        if etag in tags or "*" in tags:
            return Response(status=304, headers=headers)
    else:
        ims = request.headers.get("If-Modified-Since")
        if ims:
            try:
                if int(parsedate_to_datetime(ims).timestamp()) >= mtime:
                    return Response(status=304, headers=headers)
            except (TypeError, ValueError):
                pass

    status, start, end = 200, 0, size - 1
    range_header = request.headers.get("Range")
    if range_header:
        if_range = request.headers.get("If-Range")
        if if_range and if_range.strip() not in (etag, last_modified):
            range_header = None  # file changed since client cached it -> full body
    if range_header:
        try:
            parsed = parse_range(range_header, size)
        except RequestedRangeNotSatisfiable:
            return Response(status=416, headers={"Content-Range": f"bytes */{size}",
                                                 "Accept-Ranges": "bytes"})
        if parsed is not None:
            start, end = parsed
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    length = max(0, end - start + 1)
    headers["Content-Length"] = str(length)

    return Response(
        _file_chunks(path, start, length, app.config["STREAM_CHUNK_SIZE"]),
        status=status,
        headers=headers,
        mimetype=mimetype,
        direct_passthrough=True,
    )


# --------------------------------------------------------------------------- #
# Auth routes
# --------------------------------------------------------------------------- #
@app.route("/setup", methods=["GET", "POST"])
def setup():
    global _setup_done
    if _setup_done or models.count_users() > 0:
        _setup_done = True
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if password != request.form.get("password_confirm", ""):
            flash("Passwords do not match.", "error")
        else:
            try:
                models.create_user(username, password, role="admin")
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                _setup_done = True
                flash("Admin account created. Please sign in.", "success")
                return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("home"))
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if _login_blocked(ip):
            flash("Too many failed attempts. Try again in a few minutes.", "error")
            return render_template("login.html"), 429
        user = models.authenticate(request.form.get("username", ""),
                                   request.form.get("password", ""))
        if user is None:
            _register_login_fail(ip)
            flash("Invalid username or password.", "error")
            return render_template("login.html"), 401
        _clear_login_fails(ip)
        session.clear()  # prevent session fixation
        session.permanent = True
        session["uid"] = user["id"]
        session["pwv"] = _pw_fingerprint(user)
        return redirect(_safe_next(request.args.get("next")))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


def get_disk_stats():
    try:
        usage = shutil.disk_usage(app.config["MEDIA_ROOT"])
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
        }
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "percent": 0}


def get_transmission_client():
    try:
        import transmission_rpc
        return transmission_rpc.Client(
            host=app.config["TRANSMISSION_HOST"],
            port=app.config["TRANSMISSION_PORT"],
            username=app.config["TRANSMISSION_USER"],
            password=app.config["TRANSMISSION_PASS"],
            timeout=3,
        )
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Browse routes
# --------------------------------------------------------------------------- #
@app.route("/")
@app.route("/dashboard")
@login_required
def home():
    user = g.user
    sections = {}
    for cat in models.CATEGORIES:
        sections[cat] = {
            "count": models.count_media(cat, user),
            "recent": models.recent_media(cat, user, limit=12),
        }
    return render_template("dashboard.html", sections=sections, disk_stats=get_disk_stats())


@app.route("/videos")
@login_required
def videos():
    user = g.user
    folder = clean_folder(request.args.get("folder", ""))
    return render_template(
        "videos.html",
        folder=folder,
        crumbs=breadcrumbs(folder),
        subfolders=models.list_subfolders("video", user, folder),
        items=models.list_media("video", user, folder=folder),
    )


@app.route("/videos/<int:media_id>")
@login_required
def watch(media_id):
    media = models.get_media(media_id, g.user)
    if media is None or media["category"] != "video":
        abort(404)
    siblings = models.list_media("video", g.user, folder=media["folder"])
    ids = [m["id"] for m in siblings]
    idx = ids.index(media_id) if media_id in ids else -1
    prev_id = ids[idx - 1] if idx > 0 else None
    next_id = ids[idx + 1] if 0 <= idx < len(ids) - 1 else None
    return render_template("watch.html", media=media, prev_id=prev_id, next_id=next_id,
                           crumbs=breadcrumbs(media["folder"]))


@app.route("/music")
@login_required
def music():
    user = g.user
    folder = clean_folder(request.args.get("folder", ""))
    return render_template(
        "music.html",
        folder=folder,
        crumbs=breadcrumbs(folder),
        subfolders=models.list_subfolders("music", user, folder),
        tracks=models.list_media("music", user, folder=folder),
    )


@app.route("/photos")
@login_required
def photos():
    user = g.user
    per_page = app.config["PHOTOS_PER_PAGE"]
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    total = models.count_media("photo", user)
    items = models.list_media("photo", user, newest_first=True,
                              limit=per_page, offset=(page - 1) * per_page)
    pages = max(1, (total + per_page - 1) // per_page)
    return render_template("photos.html", items=items, page=page, pages=pages, total=total)


@app.route("/files")
@login_required
def files():
    user = g.user
    folder = clean_folder(request.args.get("folder", ""))
    return render_template(
        "files.html",
        folder=folder,
        crumbs=breadcrumbs(folder),
        subfolders=models.list_subfolders("file", user, folder),
        items=models.list_media("file", user, folder=folder),
    )


# --------------------------------------------------------------------------- #
# Direct-play streaming & downloads
# --------------------------------------------------------------------------- #
@app.route("/stream/<int:media_id>", methods=["GET", "HEAD"])
@login_required
def stream(media_id):
    media = models.get_media(media_id, g.user)  # age constraint filter applied here
    if media is None:
        abort(404)
    path = resolve_media_path(media["rel_path"])
    return send_ranged(path, media["mime_type"])


@app.route("/download/<int:media_id>", methods=["GET", "HEAD"])
@login_required
def download(media_id):
    media = models.get_media(media_id, g.user)  # age constraint filter applied here
    if media is None:
        abort(404)
    path = resolve_media_path(media["rel_path"])
    return send_ranged(path, media["mime_type"], download_name=media["filename"])


# --------------------------------------------------------------------------- #
# User Drive: personal cloud storage (all roles)
# --------------------------------------------------------------------------- #
def _user_drive_root(username):
    """Absolute path to the user's personal drive directory."""
    safe = secure_filename(username) or f"user_{username}"
    return os.path.join(app.config["MEDIA_ROOT"], models.USER_DRIVE_DIR, safe)


def _resolve_drive_path(rel_path):
    """Absolute path for a user_drive rel_path, with traversal guard."""
    root = os.path.realpath(app.config["MEDIA_ROOT"])
    full = os.path.realpath(os.path.join(root, rel_path))
    if os.path.commonpath([root, full]) != root or not os.path.isfile(full):
        abort(404)
    return full


@app.route("/drive")
@login_required
def user_drive():
    user = g.user
    folder = clean_folder(request.args.get("folder", ""))
    # Create user drive dir on first visit
    os.makedirs(_user_drive_root(user["username"]), exist_ok=True)
    return render_template(
        "drive.html",
        folder=folder,
        crumbs=breadcrumbs(folder),
        subfolders=models.udrive_subfolders(user["id"], parent=folder),
        items=models.udrive_list(user["id"], folder=folder),
        file_count=models.udrive_count(user["id"]),
        used_bytes=models.udrive_total_bytes(user["id"]),
    )


@app.route("/drive/upload", methods=["POST"])
@login_required
def drive_upload():
    user = g.user
    folder = clean_folder(request.form.get("folder", ""))
    uploaded_files = request.files.getlist("files")

    if not uploaded_files or (len(uploaded_files) == 1 and not uploaded_files[0].filename):
        flash("No files selected.", "error")
        return redirect(url_for("user_drive", folder=folder))

    drive_root = _user_drive_root(user["username"])
    target_dir = os.path.join(drive_root, folder) if folder else drive_root
    os.makedirs(target_dir, exist_ok=True)

    success_count = 0
    for file in uploaded_files:
        if not file.filename:
            continue
        raw_name = os.path.basename(file.filename)
        orig_name = raw_name  # keep original for display
        clean_name = secure_filename(raw_name) or f"file_{secrets.token_hex(4)}"
        name_root, ext = os.path.splitext(clean_name)
        clean_name = f"{name_root}{ext.lower()}"

        dest_path = os.path.join(target_dir, clean_name)
        counter = 1
        while os.path.exists(dest_path):
            clean_name = f"{name_root}_{counter}{ext.lower()}"
            dest_path = os.path.join(target_dir, clean_name)
            counter += 1

        with open(dest_path, "wb") as out:
            while chunk := file.read(64 * 1024):
                out.write(chunk)

        size_bytes = os.path.getsize(dest_path)
        mime_type = mimetypes.guess_type(dest_path)[0] or "application/octet-stream"
        safe_uname = secure_filename(user["username"]) or f"user_{user['id']}"
        folder_suffix = f"{folder}/" if folder else ""
        rel_path = f"{models.USER_DRIVE_DIR}/{safe_uname}/{folder_suffix}{clean_name}"

        try:
            models.udrive_add(
                owner_id=user["id"],
                folder=folder,
                filename=clean_name,
                orig_name=orig_name,
                rel_path=rel_path,
                mime_type=mime_type,
                size_bytes=size_bytes,
            )
            success_count += 1
        except Exception as e:
            flash(f"Error saving {clean_name}: {e}", "error")

    if success_count:
        flash(f"Uploaded {success_count} file(s) to your drive.", "success")
    return redirect(url_for("user_drive", folder=folder))


@app.route("/drive/mkdir", methods=["POST"])
@login_required
def drive_mkdir():
    user = g.user
    parent = clean_folder(request.form.get("parent", ""))
    name = secure_filename(request.form.get("name", "").strip())
    if not name:
        flash("Folder name cannot be empty.", "error")
        return redirect(url_for("user_drive", folder=parent))

    new_folder = f"{parent}/{name}" if parent else name
    new_folder = clean_folder(new_folder)
    drive_root = _user_drive_root(user["username"])
    os.makedirs(os.path.join(drive_root, new_folder), exist_ok=True)
    # Create a .keep file so folder is browsable even when empty
    keep_path = os.path.join(drive_root, new_folder, ".keep")
    if not os.path.exists(keep_path):
        open(keep_path, "w").close()
        # Register a zero-byte placeholder so subfolders() can find it
        safe_uname = secure_filename(user["username"]) or f"user_{user['id']}"
        rel = f"{models.USER_DRIVE_DIR}/{safe_uname}/{new_folder}/.keep"
        try:
            models.udrive_add(
                owner_id=user["id"],
                folder=new_folder,
                filename=".keep",
                orig_name=".keep",
                rel_path=rel,
                mime_type="application/octet-stream",
                size_bytes=0,
            )
        except Exception:
            pass  # already exists is fine
    flash(f"Folder '{name}' created.", "success")
    return redirect(url_for("user_drive", folder=parent))


@app.route("/drive/delete/<int:file_id>", methods=["POST"])
@login_required
def drive_delete(file_id):
    user = g.user
    # Admins may delete any file; regular users only their own
    owner_id = None if user["role"] == "admin" else user["id"]
    row = models.udrive_delete(file_id, owner_id=owner_id)
    if row:
        try:
            full = _resolve_drive_path(row["rel_path"])
            if os.path.exists(full):
                os.remove(full)
        except Exception:
            pass
        flash(f"Deleted '{row['orig_name']}'.", "info")
    else:
        flash("File not found or permission denied.", "error")
    folder = request.form.get("folder", "")
    return redirect(url_for("user_drive", folder=folder))


@app.route("/drive/rename/<int:file_id>", methods=["POST"])
@login_required
def drive_rename(file_id):
    user = g.user
    new_name = (request.form.get("name") or "").strip()
    if not new_name:
        flash("Name cannot be empty.", "error")
    else:
        owner_id = None if user["role"] == "admin" else user["id"]
        models.udrive_rename(file_id, new_name, owner_id=owner_id)
        flash(f"Renamed to '{new_name}'.", "success")
    folder = request.form.get("folder", "")
    return redirect(url_for("user_drive", folder=folder))


@app.route("/drive/download/<int:file_id>", methods=["GET", "HEAD"])
@login_required
def drive_download(file_id):
    user = g.user
    owner_id = None if user["role"] == "admin" else user["id"]
    row = models.udrive_get(file_id, owner_id=owner_id)
    if row is None:
        abort(404)
    path = _resolve_drive_path(row["rel_path"])
    return send_ranged(path, row["mime_type"], download_name=row["orig_name"])


@app.route("/drive/stream/<int:file_id>", methods=["GET", "HEAD"])
@login_required
def drive_stream(file_id):
    """Inline stream (no Content-Disposition) for images/audio/pdf preview."""
    user = g.user
    owner_id = None if user["role"] == "admin" else user["id"]
    row = models.udrive_get(file_id, owner_id=owner_id)
    if row is None:
        abort(404)
    path = _resolve_drive_path(row["rel_path"])
    return send_ranged(path, row["mime_type"])


# --------------------------------------------------------------------------- #
# Admin routes: Dashboard, Uploads, Torrents, User Management
# --------------------------------------------------------------------------- #
@app.route("/admin")
@admin_required
def admin_panel():
    users = models.list_users()
    torrents = downloader.get_live_torrents(app)
    media_items = models.list_all_media_admin(limit=250)
    disk_stats = get_disk_stats()
    is_online = downloader.is_daemon_online(app.config)
    transmission_status = "Online" if is_online else "Offline (daemon not running)"
    return render_template(
        "admin.html",
        users=users,
        torrents=torrents,
        media_items=media_items,
        disk_stats=disk_stats,
        transmission_status=transmission_status,
        transmission_online=is_online,
        active_tab=request.args.get("tab", "upload"),
    )


@app.route("/admin/upload", methods=["POST"])
@admin_required
def admin_upload():
    category = request.form.get("category")
    if category not in models.CATEGORIES:
        flash("Invalid media category.", "error")
        return redirect(url_for("admin_panel", tab="upload"))

    folder = clean_folder(request.form.get("folder", ""))
    min_age = request.form.get("min_age", type=int)
    if min_age is None:
        min_age = 18 if request.form.get("age_restricted") else 0
    age_restricted = bool(request.form.get("age_restricted") or min_age >= 18)
    uploaded_files = request.files.getlist("files")

    if not uploaded_files or (len(uploaded_files) == 1 and not uploaded_files[0].filename):
        flash("No files selected for upload.", "error")
        return redirect(url_for("admin_panel", tab="upload"))

    cat_dir = app.config["CATEGORY_DIRS"][category]
    target_dir = os.path.join(app.config["MEDIA_ROOT"], cat_dir)
    if folder:
        target_dir = os.path.join(target_dir, folder)
    os.makedirs(target_dir, exist_ok=True)

    success_count = 0
    for file in uploaded_files:
        if not file.filename:
            continue
        raw_name = os.path.basename(file.filename)
        clean_name = secure_filename(raw_name) or f"upload_{secrets.token_hex(4)}"
        name_root, ext = os.path.splitext(clean_name)
        clean_name = f"{name_root}{ext.lower()}"

        dest_path = os.path.join(target_dir, clean_name)
        counter = 1
        while os.path.exists(dest_path):
            clean_name = f"{name_root}_{counter}{ext.lower()}"
            dest_path = os.path.join(target_dir, clean_name)
            counter += 1

        with open(dest_path, "wb") as out:
            while chunk := file.read(64 * 1024):
                out.write(chunk)

        size_bytes = os.path.getsize(dest_path)
        mime_type = mimetypes.guess_type(dest_path)[0] or "application/octet-stream"
        rel_path = f"{cat_dir}/{folder + '/' if folder else ''}{clean_name}"
        title = name_root.replace("-", " ").replace("_", " ").strip() or clean_name

        try:
            models.add_media(
                title=title,
                category=category,
                rel_path=rel_path,
                folder=folder,
                filename=clean_name,
                mime_type=mime_type,
                size_bytes=size_bytes,
                min_age=min_age,
                age_restricted=age_restricted,
                uploaded_by=g.user["id"],
            )
            success_count += 1
        except Exception as e:
            flash(f"Error registering {clean_name}: {e}", "error")

    flash(f"Successfully uploaded {success_count} file(s) into {category.capitalize()}.", "success")
    return redirect(url_for("admin_panel", tab="upload"))


@app.route("/admin/torrent", methods=["POST"])
@admin_required
def admin_torrent():
    category = request.form.get("category", "video")
    subfolder = clean_folder(request.form.get("folder", ""))
    min_age = request.form.get("min_age", type=int)
    if min_age is None:
        min_age = 18 if request.form.get("age_restricted") else 0
    age_restricted = bool(request.form.get("age_restricted") or min_age >= 18)

    # 1. Check if a .torrent file was uploaded
    torrent_file = request.files.get("torrent_file")
    if torrent_file and torrent_file.filename:
        try:
            res = downloader.ingest_torrent_file(
                app=app,
                file_storage=torrent_file,
                category=category,
                min_age=min_age,
                age_restricted=age_restricted,
                added_by=g.user["id"],
                subfolder=subfolder,
            )
            if res["online"]:
                flash(f"Torrent file '{res['name']}' uploaded and started in Transmission.", "success")
            else:
                flash(f"Transmission is offline. Torrent file '{res['name']}' queued in database.", "info")
        except ValueError as e:
            flash(str(e), "error")
        except Exception as e:
            flash(f"Torrent file upload error: {e}", "error")
        return redirect(url_for("admin_panel", tab="torrent"))

    # 2. Check if a magnet link or .torrent URL was supplied
    source = request.form.get("source", "").strip()
    if not source:
        flash("Please provide a magnet link, .torrent URL, or upload a .torrent file.", "error")
        return redirect(url_for("admin_panel", tab="torrent"))

    try:
        res = downloader.ingest_torrent(
            app=app,
            source=source,
            category=category,
            min_age=min_age,
            age_restricted=age_restricted,
            added_by=g.user["id"],
            subfolder=subfolder,
        )
        if res["online"]:
            flash(f"Torrent '{res['name']}' added to Transmission.", "success")
        else:
            flash(f"Transmission is offline. Torrent '{res['name']}' saved to database queue.", "info")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"Torrent ingestion error: {e}", "error")

    return redirect(url_for("admin_panel", tab="torrent"))


@app.route("/admin/torrent/<int:torrent_id>/pause", methods=["POST"])
@admin_required
def admin_torrent_pause(torrent_id):
    if downloader.pause_torrent(app, torrent_id):
        flash("Torrent paused.", "info")
    else:
        flash("Could not pause torrent (Transmission may be offline or torrent not found).", "warning")
    return redirect(url_for("admin_panel", tab="torrent"))


@app.route("/admin/torrent/<int:torrent_id>/resume", methods=["POST"])
@admin_required
def admin_torrent_resume(torrent_id):
    if downloader.resume_torrent(app, torrent_id):
        flash("Torrent resumed.", "success")
    else:
        flash("Could not resume torrent (Transmission may be offline or torrent not found).", "warning")
    return redirect(url_for("admin_panel", tab="torrent"))


@app.route("/admin/torrent/<int:torrent_id>/delete", methods=["POST"])
@admin_required
def admin_torrent_delete(torrent_id):
    delete_data = bool(request.form.get("delete_data"))
    downloader.remove_torrent(app, torrent_id, delete_data=delete_data)
    flash("Torrent removed.", "info")
    return redirect(url_for("admin_panel", tab="torrent"))


@app.route("/api/torrents")
@admin_required
def api_torrents():
    """Live JSON endpoint for client-side AJAX polling of download speeds & progress."""
    is_online = downloader.is_daemon_online(app.config)
    items = downloader.get_live_torrents(app)
    return jsonify({
        "online": is_online,
        "transmission_status": "Online" if is_online else "Offline",
        "torrents": items,
    })


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_user_create():
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    age = request.form.get("age", type=int)
    if age is None:
        age = 12 if role == "child" else 18
    try:
        models.create_user(username, password, role, age=age)
        flash(f"User '{username}' (Age: {age}) created with role '{role}'.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_panel", tab="users"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_user_role(user_id):
    new_role = request.form.get("role")
    try:
        models.set_user_role(user_id, new_role)
        flash("User role updated.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_panel", tab="users"))


@app.route("/admin/users/<int:user_id>/age", methods=["POST"])
@admin_required
def admin_user_age(user_id):
    new_age = request.form.get("age", type=int)
    if new_age is None or new_age < 0 or new_age > 150:
        flash("Invalid age value (must be 0-150).", "error")
    else:
        try:
            models.set_user_age(user_id, new_age)
            flash("User age updated successfully.", "success")
        except ValueError as e:
            flash(str(e), "error")
    return redirect(url_for("admin_panel", tab="users"))


@app.route("/admin/users/<int:user_id>/toggle-active", methods=["POST"])
@admin_required
def admin_user_toggle_active(user_id):
    if user_id == g.user["id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("admin_panel", tab="users"))
    user = models.get_user_by_id(user_id)
    if user:
        models.set_user_active(user_id, not bool(user["is_active"]))
        flash(f"User '{user['username']}' status updated.", "info")
    return redirect(url_for("admin_panel", tab="users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    if user_id == g.user["id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin_panel", tab="users"))
    models.delete_user(user_id)
    flash("User deleted.", "info")
    return redirect(url_for("admin_panel", tab="users"))


@app.route("/admin/media/<int:media_id>/min-age", methods=["POST"])
@admin_required
def admin_media_min_age(media_id):
    min_age = request.form.get("min_age", 0, type=int)
    if min_age < 0 or min_age > 150:
        flash("Invalid minimum age constraint.", "error")
    else:
        models.set_media_min_age(media_id, min_age)
        flash(f"Age constraint updated to {min_age}+.", "success")
    return redirect(request.referrer or url_for("admin_panel", tab="library"))


@app.route("/admin/media/<int:media_id>/toggle-restricted", methods=["POST"])
@admin_required
def admin_media_toggle_restricted(media_id):
    row = models.get_db().execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    if row:
        new_state = not bool(row["age_restricted"])
        models.set_age_restricted(media_id, new_state)
        flash(f"Age restriction {'enabled (18+)' if new_state else 'disabled (All Ages)'} for '{row['filename']}'.", "info")
    return redirect(request.referrer or url_for("admin_panel", tab="library"))


@app.route("/admin/media/<int:media_id>/delete", methods=["POST"])
@admin_required
def admin_media_delete(media_id):
    row = models.delete_media(media_id)
    if row:
        try:
            full = resolve_media_path(row["rel_path"])
            if os.path.exists(full):
                os.remove(full)
        except Exception:
            pass
        flash(f"Deleted '{row['filename']}'.", "info")
    return redirect(request.referrer or url_for("admin_panel", tab="library"))


@app.route("/admin/scan", methods=["POST"])
@admin_required
def admin_scan_media():
    """Scan MEDIA_ROOT directory structure to index existing files directly on the HDD."""
    root = os.path.realpath(app.config["MEDIA_ROOT"])
    count = 0
    for cat, sub in app.config["CATEGORY_DIRS"].items():
        cat_path = os.path.join(root, sub)
        if not os.path.isdir(cat_path):
            continue
        for dirpath, _, filenames in os.walk(cat_path):
            rel_dir = os.path.relpath(dirpath, cat_path)
            folder = "" if rel_dir == "." else rel_dir.replace("\\", "/")
            for fname in filenames:
                if fname.startswith("."):
                    continue
                full_path = os.path.join(dirpath, fname)
                rel_path = f"{sub}/{folder + '/' if folder else ''}{fname}"
                if models.get_media_by_path(rel_path) is None:
                    size = os.path.getsize(full_path)
                    mime = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
                    title = os.path.splitext(fname)[0].replace("-", " ").replace("_", " ").strip() or fname
                    try:
                        models.add_media(
                            title=title,
                            category=cat,
                            rel_path=rel_path,
                            folder=folder,
                            filename=fname,
                            mime_type=mime,
                            size_bytes=size,
                            age_restricted=False,
                            uploaded_by=g.user["id"]
                        )
                        count += 1
                    except Exception:
                        pass
    flash(f"Disk scan completed. Indexed {count} new media items.", "success")
    return redirect(url_for("admin_panel", tab="library"))


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(500)
def handle_error(err):
    code = getattr(err, "code", 500) or 500
    messages = {
        400: "Bad request.",
        403: "You do not have permission to view this page.",
        404: "Not found.",
        413: "The uploaded file is too large.",
        500: "Internal server error.",
    }
    description = getattr(err, "description", None) if code == 400 else None
    message = description or messages.get(code, "Error.")
    try:
        return render_template("error.html", code=code, message=message), code
    except TemplateNotFound:
        return Response(f"{code} - {message}", status=code, mimetype="text/plain")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
@app.cli.command("init-db")
def init_db_cmd():
    """Create database tables (safe to run repeatedly)."""
    models.init_db()
    click.echo(f"Database ready at {app.config['DATABASE']}")


@app.cli.command("create-user")
@click.argument("username")
@click.option("--role", type=click.Choice(models.ROLES), default="user", show_default=True)
@click.option("--age", type=int, default=None, help="User age (e.g. 13, 16, 20)")
@click.password_option()
def create_user_cmd(username, role, age, password):
    """Create a user: flask --app app create-user NAME --role admin|user|child --age 16"""
    try:
        uid = models.create_user(username, password, role, age=age)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    actual_age = age if age is not None else (12 if role == "child" else 18)
    click.echo(f"Created {role} '{username}' (id={uid}, age={actual_age}).")


if __name__ == "__main__":
    app.run(
        host=os.environ.get("MATEO_HOST", "172.21.210.78"),
        port=int(os.environ.get("MATEO_PORT", 8080)),
        threaded=True,
        debug=False,
    )
