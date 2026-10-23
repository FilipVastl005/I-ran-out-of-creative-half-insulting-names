"""
models.py - SQLite data layer with granular age constraints.

Uses only the standard-library sqlite3 module (no ORM) to keep RAM and CPU
usage minimal on old hardware. One connection per request, stored on flask.g.

Age constraint filtering is enforced HERE at the SQL engine level:
Media with min_age constraint is filtered out if user.age < media.min_age.
Admin accounts can view all media.
"""

import sqlite3
from datetime import datetime, timezone

from flask import current_app, g
from werkzeug.security import check_password_hash, generate_password_hash

ROLES = ("admin", "user", "child")
CATEGORIES = ("video", "music", "photo", "file")

# Root dir name inside MEDIA_ROOT for personal drives
USER_DRIVE_DIR = "user_drive"

SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'user'
                          CHECK (role IN ('admin', 'user', 'child')),
    age           INTEGER NOT NULL DEFAULT 18,
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_login    TEXT
);

CREATE TABLE IF NOT EXISTS media (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    category       TEXT    NOT NULL
                           CHECK (category IN ('video', 'music', 'photo', 'file')),
    rel_path       TEXT    NOT NULL UNIQUE,   -- path relative to MEDIA_ROOT, '/' separated
    folder         TEXT    NOT NULL DEFAULT '', -- sub-folder inside the category dir
    filename       TEXT    NOT NULL,
    mime_type      TEXT    NOT NULL DEFAULT 'application/octet-stream',
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    min_age        INTEGER NOT NULL DEFAULT 0,  -- 0=All, 6=6+, 12=12+, 13=13+, 16=16+, 18=18+
    age_restricted INTEGER NOT NULL DEFAULT 0 CHECK (age_restricted IN (0, 1)),
    uploaded_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS torrents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transmission_id INTEGER,
    name            TEXT    NOT NULL,
    source          TEXT    NOT NULL,          -- magnet link or .torrent URL
    category        TEXT    NOT NULL
                            CHECK (category IN ('video', 'music', 'photo', 'file')),
    min_age         INTEGER NOT NULL DEFAULT 0,
    age_restricted  INTEGER NOT NULL DEFAULT 0 CHECK (age_restricted IN (0, 1)),
    status          TEXT    NOT NULL DEFAULT 'queued',
    imported        INTEGER NOT NULL DEFAULT 0 CHECK (imported IN (0, 1)),
    added_by        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS user_drive (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder        TEXT    NOT NULL DEFAULT '',   -- virtual folder path, '/' separated
    filename      TEXT    NOT NULL,              -- sanitised filename on disk
    orig_name     TEXT    NOT NULL,              -- original upload name shown in UI
    rel_path      TEXT    NOT NULL UNIQUE,       -- path relative to MEDIA_ROOT
    mime_type     TEXT    NOT NULL DEFAULT 'application/octet-stream',
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_media_category_folder ON media (category, folder);
CREATE INDEX IF NOT EXISTS idx_media_category_id     ON media (category, id);
CREATE INDEX IF NOT EXISTS idx_media_min_age         ON media (min_age);
CREATE INDEX IF NOT EXISTS idx_udrive_owner_folder   ON user_drive (owner_id, folder);
"""

# Hash used to equalise login timing when the username does not exist,
# so attackers cannot enumerate valid usernames by response time.
_DUMMY_HASH = None


# --------------------------------------------------------------------------- #
# Connection handling & Schema Migrations
# --------------------------------------------------------------------------- #
def _connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -8000")  # ~8 MB page cache
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_db():
    if "_db" not in g:
        g._db = _connect(current_app.config["DATABASE"])
    return g._db


def close_db(_exc=None):
    conn = g.pop("_db", None)
    if conn is not None:
        conn.close()


def _migrate_db(conn):
    """Gracefully migrates existing databases to support granular age and min_age."""
    # Check users table columns
    user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "age" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN age INTEGER NOT NULL DEFAULT 18")
        conn.execute("UPDATE users SET age = 12 WHERE role = 'child'")
        conn.commit()

    # Check media table columns
    media_cols = {r["name"] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
    if "min_age" not in media_cols:
        conn.execute("ALTER TABLE media ADD COLUMN min_age INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE media SET min_age = 18 WHERE age_restricted = 1")
        conn.commit()

    # Check torrents table columns
    torrent_cols = {r["name"] for r in conn.execute("PRAGMA table_info(torrents)").fetchall()}
    if "min_age" not in torrent_cols:
        conn.execute("ALTER TABLE torrents ADD COLUMN min_age INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE torrents SET min_age = 18 WHERE age_restricted = 1")
        conn.commit()


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA_TABLES)
    _migrate_db(conn)
    conn.executescript(SCHEMA_INDEXES)
    conn.commit()


# --------------------------------------------------------------------------- #
# User Drive (personal Google-Drive-style file space)
# --------------------------------------------------------------------------- #
def udrive_add(owner_id, folder, filename, orig_name, rel_path, mime_type, size_bytes):
    """Register a newly uploaded file into the user's personal drive."""
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO user_drive "
            "(owner_id, folder, filename, orig_name, rel_path, mime_type, size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_id, folder, filename, orig_name, rel_path, mime_type, int(size_bytes)),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"'{rel_path}' already exists in your drive.") from None
    return cur.lastrowid


def udrive_get(file_id, owner_id=None):
    """Fetch one drive file. If owner_id given, restrict to that owner (non-admins)."""
    sql = "SELECT * FROM user_drive WHERE id = ?"
    params = [file_id]
    if owner_id is not None:
        sql += " AND owner_id = ?"
        params.append(owner_id)
    return get_db().execute(sql, params).fetchone()


def udrive_list(owner_id, folder=""):
    """List files in the given virtual folder for this owner."""
    return get_db().execute(
        "SELECT * FROM user_drive WHERE owner_id = ? AND folder = ? "
        "ORDER BY filename COLLATE NOCASE",
        (owner_id, folder),
    ).fetchall()


def udrive_subfolders(owner_id, parent=""):
    """Return immediate child virtual folders visible to owner."""
    sql = "SELECT DISTINCT folder FROM user_drive WHERE owner_id = ? AND folder != ''"
    params = [owner_id]
    prefix = ""
    if parent:
        prefix = parent + "/"
        sql += " AND substr(folder, 1, ?) = ?"
        params += [len(prefix), prefix]
    names = set()
    for row in get_db().execute(sql, params):
        names.add(row["folder"][len(prefix):].split("/", 1)[0])
    return [{"name": n, "path": prefix + n} for n in sorted(names, key=str.lower)]


def udrive_mkdir(owner_id, folder):
    """Ensure a virtual folder is registered (no-op if already exists).
    We use a placeholder entry with empty filename; actual folders are just
    a convention on the 'folder' column — no dedicated table row needed,
    but we do need to create the directory on disk (handled in the route).
    This function is a no-op DB-wise; folders are implicit from file rows.
    """
    pass  # Folders are implicit; caller creates the OS directory.


def udrive_count(owner_id):
    return get_db().execute(
        "SELECT COUNT(*) FROM user_drive WHERE owner_id = ?", (owner_id,)
    ).fetchone()[0]


def udrive_total_bytes(owner_id):
    result = get_db().execute(
        "SELECT SUM(size_bytes) FROM user_drive WHERE owner_id = ?", (owner_id,)
    ).fetchone()[0]
    return result or 0


def udrive_delete(file_id, owner_id=None):
    """Delete DB row; returns the row so the caller can remove the file from disk."""
    conn = get_db()
    sql = "SELECT * FROM user_drive WHERE id = ?"
    params = [file_id]
    if owner_id is not None:
        sql += " AND owner_id = ?"
        params.append(owner_id)
    row = conn.execute(sql, params).fetchone()
    if row is not None:
        conn.execute("DELETE FROM user_drive WHERE id = ?", (file_id,))
        conn.commit()
    return row


def udrive_rename(file_id, new_orig_name, owner_id=None):
    """Rename the display name of a drive file."""
    conn = get_db()
    sql = "UPDATE user_drive SET orig_name = ? WHERE id = ?"
    params = [new_orig_name.strip(), file_id]
    if owner_id is not None:
        sql = "UPDATE user_drive SET orig_name = ? WHERE id = ? AND owner_id = ?"
        params.append(owner_id)
    conn.execute(sql, params)
    conn.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_filter(user_or_role):
    """
    SQL fragment enforcing age constraints:
    - Admins see everything.
    - If user is provided, filters media where min_age <= user.age.
      (e.g., a 13yo user cannot see 16+ or 18+ media, but a 16yo or 20yo user can).
    - Backwards compatibility with role strings ('child', 'user', 'admin').
    """
    if user_or_role is None:
        return " AND min_age = 0"

    if isinstance(user_or_role, str):
        role = user_or_role
        if role == "admin":
            return ""
        if role == "child":
            return " AND min_age <= 12 AND age_restricted = 0"
        return " AND min_age <= 18"

    # sqlite3.Row or dictionary
    try:
        role = user_or_role["role"]
    except (KeyError, TypeError):
        role = "user"

    if role == "admin":
        return ""

    try:
        user_age = int(user_or_role["age"])
    except (KeyError, TypeError, ValueError):
        user_age = 12 if role == "child" else 18

    # Safe: user_age is strictly cast to integer
    return f" AND min_age <= {user_age}"


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def count_users():
    return get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(username, password, role="user", age=None):
    username = (username or "").strip()
    if not username or len(username) > 64:
        raise ValueError("Username must be 1-64 characters.")
    if role not in ROLES:
        raise ValueError(f"Invalid role '{role}'.")
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")

    if age is None:
        age = 12 if role == "child" else 18
    try:
        age = int(age)
        if age < 0 or age > 150:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("User age must be a valid number between 0 and 150.")

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, age) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), role, age),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"User '{username}' already exists.") from None
    return cur.lastrowid


def get_user_by_id(user_id):
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(username):
    return get_db().execute(
        "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
    ).fetchone()


def authenticate(username, password):
    """Return the user row on success, otherwise None."""
    global _DUMMY_HASH
    user = get_user_by_username(username)
    if user is None or not user["is_active"]:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = generate_password_hash("timing-equaliser")
        check_password_hash(_DUMMY_HASH, password or "")
        return None
    if not check_password_hash(user["password_hash"], password or ""):
        return None
    conn = get_db()
    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (_now(), user["id"]))
    conn.commit()
    return user


def list_users():
    return get_db().execute(
        "SELECT id, username, role, age, is_active, created_at, last_login "
        "FROM users ORDER BY username COLLATE NOCASE"
    ).fetchall()


def set_user_role(user_id, role):
    if role not in ROLES:
        raise ValueError(f"Invalid role '{role}'.")
    conn = get_db()
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()


def set_user_age(user_id, age):
    try:
        age_val = int(age)
        if age_val < 0 or age_val > 150:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("Age must be between 0 and 150.")
    conn = get_db()
    conn.execute("UPDATE users SET age = ? WHERE id = ?", (age_val, user_id))
    conn.commit()


def set_user_active(user_id, active):
    conn = get_db()
    conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))
    conn.commit()


def set_user_password(user_id, password):
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(password), user_id),
    )
    conn.commit()


def delete_user(user_id):
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()


# --------------------------------------------------------------------------- #
# Media
# --------------------------------------------------------------------------- #
def add_media(title, category, rel_path, folder, filename, mime_type,
              size_bytes, min_age=0, age_restricted=False, uploaded_by=None):
    if category not in CATEGORIES:
        raise ValueError(f"Invalid category '{category}'.")
    min_age_val = max(0, int(min_age or 0))
    is_restricted = 1 if (age_restricted or min_age_val >= 18) else 0

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO media (title, category, rel_path, folder, filename, mime_type, "
            "size_bytes, min_age, age_restricted, uploaded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, category, rel_path, folder, filename, mime_type,
             int(size_bytes), min_age_val, is_restricted, uploaded_by),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"'{rel_path}' is already in the library.") from None
    return cur.lastrowid


def get_media(media_id, user_or_role):
    """Fetch one item, or None if missing OR hidden based on age constraint."""
    sql = "SELECT * FROM media WHERE id = ?" + _age_filter(user_or_role)
    return get_db().execute(sql, (media_id,)).fetchone()


def get_media_by_path(rel_path):
    return get_db().execute("SELECT * FROM media WHERE rel_path = ?", (rel_path,)).fetchone()


def list_media(category, user_or_role, folder=None, newest_first=False, limit=None, offset=0):
    sql = "SELECT * FROM media WHERE category = ?"
    params = [category]
    if folder is not None:
        sql += " AND folder = ?"
        params.append(folder)
    sql += _age_filter(user_or_role)
    if newest_first:
        sql += " ORDER BY id DESC"
    else:
        sql += " ORDER BY folder COLLATE NOCASE, filename COLLATE NOCASE"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
    return get_db().execute(sql, params).fetchall()


def count_media(category, user_or_role):
    sql = "SELECT COUNT(*) FROM media WHERE category = ?" + _age_filter(user_or_role)
    return get_db().execute(sql, (category,)).fetchone()[0]


def recent_media(category, user_or_role, limit=12):
    return list_media(category, user_or_role, newest_first=True, limit=limit)


def list_subfolders(category, user_or_role, parent=""):
    """
    Immediate child folders of `parent` that contain at least one item
    visible to the given user's age.
    """
    sql = "SELECT DISTINCT folder FROM media WHERE category = ? AND folder != ''"
    params = [category]
    prefix = ""
    if parent:
        prefix = parent + "/"
        sql += " AND substr(folder, 1, ?) = ?"
        params += [len(prefix), prefix]
    sql += _age_filter(user_or_role)
    names = set()
    for row in get_db().execute(sql, params):
        names.add(row["folder"][len(prefix):].split("/", 1)[0])
    return [{"name": n, "path": prefix + n} for n in sorted(names, key=str.lower)]


def set_media_min_age(media_id, min_age):
    """Set the minimum age required to view/stream this media (e.g. 0, 13, 16, 18)."""
    min_age_val = max(0, int(min_age or 0))
    is_restricted = 1 if min_age_val >= 18 else 0
    conn = get_db()
    conn.execute(
        "UPDATE media SET min_age = ?, age_restricted = ? WHERE id = ?",
        (min_age_val, is_restricted, media_id)
    )
    conn.commit()


def set_age_restricted(media_id, restricted):
    """Legacy helper: toggling restricted sets min_age to 18 or 0."""
    min_age = 18 if restricted else 0
    set_media_min_age(media_id, min_age)


def update_media_title(media_id, title):
    conn = get_db()
    conn.execute("UPDATE media SET title = ? WHERE id = ?", (title.strip(), media_id))
    conn.commit()


def delete_media(media_id):
    """Remove the DB row and return it, so the caller can delete the file on disk."""
    conn = get_db()
    row = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    if row is not None:
        conn.execute("DELETE FROM media WHERE id = ?", (media_id,))
        conn.commit()
    return row


def list_all_media_admin(limit=200, category=None):
    """Admin view: returns all media items regardless of age constraint."""
    sql = "SELECT * FROM media"
    params = []
    if category:
        sql += " WHERE category = ?"
        params.append(category)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    return get_db().execute(sql, params).fetchall()


# --------------------------------------------------------------------------- #
# Torrents
# --------------------------------------------------------------------------- #
def add_torrent(name, source, category, min_age=0, age_restricted=False, added_by=None, transmission_id=None, status="queued"):
    min_age_val = max(0, int(min_age or 0))
    is_restricted = 1 if (age_restricted or min_age_val >= 18) else 0
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO torrents (name, source, category, min_age, age_restricted, added_by, transmission_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, source, category, min_age_val, is_restricted, added_by, transmission_id, status),
    )
    conn.commit()
    return cur.lastrowid


def list_torrents():
    return get_db().execute(
        "SELECT t.*, u.username as added_by_name FROM torrents t "
        "LEFT JOIN users u ON t.added_by = u.id ORDER BY t.id DESC"
    ).fetchall()


def get_torrent(torrent_id):
    return get_db().execute("SELECT * FROM torrents WHERE id = ?", (torrent_id,)).fetchone()


def update_torrent_status(torrent_id, status, imported=False):
    conn = get_db()
    conn.execute(
        "UPDATE torrents SET status = ?, imported = ? WHERE id = ?",
        (status, 1 if imported else 0, torrent_id)
    )
    conn.commit()


def update_torrent(torrent_id, **fields):
    """Update arbitrary torrent fields (transmission_id, name, status, imported, min_age)."""
    if not fields:
        return
    conn = get_db()
    sets = []
    params = []
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        params.append(1 if (k in ('age_restricted', 'imported') and isinstance(v, bool)) else v)
    params.append(torrent_id)
    conn.execute(f"UPDATE torrents SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()


def get_pending_torrents():
    """Torrents waiting to be handed off to Transmission daemon."""
    return get_db().execute(
        "SELECT * FROM torrents WHERE transmission_id IS NULL OR status LIKE 'queued%' "
        "ORDER BY id ASC"
    ).fetchall()


def get_unimported_torrents():
    """Torrents that have not had their files indexed into the media library yet."""
    return get_db().execute(
        "SELECT * FROM torrents WHERE imported = 0 AND transmission_id IS NOT NULL "
        "ORDER BY id ASC"
    ).fetchall()


def delete_torrent(torrent_id):
    conn = get_db()
    conn.execute("DELETE FROM torrents WHERE id = ?", (torrent_id,))
    conn.commit()
