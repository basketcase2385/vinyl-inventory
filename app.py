import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import Flask, request, jsonify, render_template, Response, redirect, make_response, g
from werkzeug.security import check_password_hash, generate_password_hash

import db
import discogs as dg
import vision
import spotify as sp

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB hard cap for request bodies

# --- Config ---------------------------------------------------------------
_config_path = Path(__file__).parent / "config.json"
if _config_path.exists():
    CONFIG = json.loads(_config_path.read_text())
else:
    CONFIG = {}
    print("WARNING: config.json not found. Copy config.example.json and fill in your tokens.")

DISCOGS_TOKEN         = CONFIG.get("discogs_token",         "")
GEMINI_API_KEY        = CONFIG.get("gemini_api_key",        "")
SPOTIFY_CLIENT_ID     = CONFIG.get("spotify_client_id",     "")
SPOTIFY_CLIENT_SECRET = CONFIG.get("spotify_client_secret", "")
OWNER_PASSWORD_HASH   = (os.environ.get("VINYL_OWNER_PASSWORD_HASH") or CONFIG.get("owner_password_hash", "")).strip()
if not OWNER_PASSWORD_HASH and (CONFIG.get("owner_pin", "").strip()):
    # Backward-compatible fallback: treat legacy owner_pin as initial password.
    OWNER_PASSWORD_HASH = generate_password_hash(CONFIG.get("owner_pin", "").strip())
SESSION_TTL_HOURS     = int(CONFIG.get("session_ttl_hours", 12))
SESSION_IDLE_MINUTES  = int(CONFIG.get("session_idle_minutes", 30))
COOKIE_NAME           = "__Host-vinyl_owner_session"
LOGIN_MAX_ATTEMPTS    = int(CONFIG.get("login_max_attempts", 8))
LOGIN_WINDOW_SECONDS  = int(CONFIG.get("login_window_seconds", 600))
LOCKOUT_SECONDS       = int(CONFIG.get("login_lockout_seconds", 900))
TRUSTED_ORIGINS       = set(CONFIG.get("trusted_origins", []))

# --- Auth -----------------------------------------------------------------
_login_attempts: dict[str, list[float]] = {}
_lockouts: dict[str, float] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _prune_login_attempts(ip: str, now_ts: float):
    window_start = now_ts - LOGIN_WINDOW_SECONDS
    attempts = [ts for ts in _login_attempts.get(ip, []) if ts >= window_start]
    _login_attempts[ip] = attempts


def _is_locked_out(ip: str, now_ts: float) -> bool:
    unlock_ts = _lockouts.get(ip, 0)
    if now_ts < unlock_ts:
        return True
    if ip in _lockouts and now_ts >= unlock_ts:
        _lockouts.pop(ip, None)
    return False


def _record_login_failure(ip: str, now_ts: float):
    _prune_login_attempts(ip, now_ts)
    attempts = _login_attempts.setdefault(ip, [])
    attempts.append(now_ts)
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        _lockouts[ip] = now_ts + LOCKOUT_SECONDS
        _login_attempts[ip] = []


def _create_session() -> tuple[str, str]:
    token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    now = _utcnow()
    expires = now + timedelta(hours=SESSION_TTL_HOURS)
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO auth_sessions (token_hash, csrf_token, expires_at, last_seen_at)
               VALUES (?,?,?,?)""",
            (_hash_token(token), csrf_token, _iso(expires), _iso(now)),
        )
    return token, csrf_token


def _get_session_from_cookie():
    token = request.cookies.get(COOKIE_NAME, "").strip()
    if not token:
        return None
    token_hash = _hash_token(token)
    now = _utcnow()
    with db.get_db() as conn:
        row = conn.execute(
            """SELECT id, csrf_token, expires_at, last_seen_at, revoked
               FROM auth_sessions WHERE token_hash=?""",
            (token_hash,),
        ).fetchone()
        if not row:
            return None
        if row["revoked"]:
            return None
        expires_at = _parse_iso(row["expires_at"])
        if expires_at <= now:
            conn.execute("UPDATE auth_sessions SET revoked=1 WHERE id=?", (row["id"],))
            return None
        last_seen = _parse_iso(row["last_seen_at"])
        if last_seen + timedelta(minutes=SESSION_IDLE_MINUTES) <= now:
            conn.execute("UPDATE auth_sessions SET revoked=1 WHERE id=?", (row["id"],))
            return None
        conn.execute("UPDATE auth_sessions SET last_seen_at=? WHERE id=?", (_iso(now), row["id"]))
        return {"id": row["id"], "csrf_token": row["csrf_token"], "token": token}


def _is_owner():
    if not OWNER_PASSWORD_HASH:
        return True
    if not hasattr(g, "owner_session"):
        g.owner_session = _get_session_from_cookie()
    return bool(g.owner_session)


def _require_csrf():
    if not OWNER_PASSWORD_HASH:
        return True
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    sess = getattr(g, "owner_session", None) or _get_session_from_cookie()
    if not sess:
        return False
    header = request.headers.get("X-CSRF-Token", "")
    return bool(header) and secrets.compare_digest(header, sess["csrf_token"])


def _num_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@app.before_request
def api_origin_guard():
    if not request.path.startswith("/api/"):
        return None
    origin = request.headers.get("Origin")
    if not origin:
        return None
    allowed = {f"{request.scheme}://{request.host}", f"https://{request.host}"}
    allowed.update(TRUSTED_ORIGINS)
    if origin not in allowed:
        return jsonify({"error": "Origin not allowed"}), 403
    return None


@app.after_request
def add_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=()"
    if request.path == "/" or request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    csp = (
        "default-src 'self'; "
        "img-src 'self' https: data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )
    resp.headers["Content-Security-Policy"] = csp
    if request.headers.get("X-Forwarded-Proto", request.scheme) == "https":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp

# --- DB init ---------------------------------------------------------------
db.init_db()


# --- PWA assets -----------------------------------------------------------

@app.route("/manifest.json")
def manifest():
    data = {
        "name": "Vinyl Inventory",
        "short_name": "Vinyl",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#121212",
        "theme_color": "#e63946",
        "icons": [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}
        ],
    }
    return Response(json.dumps(data), mimetype="application/manifest+json")


@app.route("/icon.svg")
def icon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<circle cx="50" cy="50" r="48" fill="#121212"/>'
        '<circle cx="50" cy="50" r="38" fill="none" stroke="#2a2a2a" stroke-width="3"/>'
        '<circle cx="50" cy="50" r="28" fill="none" stroke="#2a2a2a" stroke-width="2"/>'
        '<circle cx="50" cy="50" r="18" fill="none" stroke="#2a2a2a" stroke-width="1.5"/>'
        '<circle cx="50" cy="50" r="9"  fill="#e63946"/>'
        '<circle cx="50" cy="50" r="2.5" fill="#121212"/>'
        "</svg>"
    )
    return Response(svg, mimetype="image/svg+xml")


@app.route("/service-worker.js")
def service_worker():
    js = """
const CACHE = 'vinyl-v4';
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(['/', '/static/quagga.min.js'])));
  self.skipWaiting();
});
self.addEventListener('activate', e =>
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  )
);
self.addEventListener('fetch', e => {
  // Never cache API responses, especially authenticated owner traffic.
  if (e.request.url.includes('/api/')) {
    e.respondWith(fetch(e.request));
    return;
  }
  // Network-first for page navigation — always get fresh HTML
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request)
        .then(r => {
          const clone = r.clone();
          caches.open(CACHE).then(c => c.put('/', clone));
          return r;
        })
        .catch(() => caches.match('/'))
    );
    return;
  }
  // Static assets (quagga.min.js etc): cache-first so they load offline
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request).then(r => {
      const clone = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, clone));
      return r;
    }))
  );
});
"""
    return Response(js, mimetype="application/javascript")


# --- SPA ------------------------------------------------------------------

@app.route("/")
def index():
    if request.headers.get("X-Forwarded-Proto", request.scheme) == "http":
        host = request.host
        return redirect(f"https://{host}{request.full_path}".rstrip("?"), code=302)
    return render_template("index.html")


# --- Auth endpoints -------------------------------------------------------

@app.route("/api/auth/status")
def auth_status():
    return jsonify({"auth_enabled": bool(OWNER_PASSWORD_HASH)})


@app.route("/api/auth/session")
def auth_session():
    if not OWNER_PASSWORD_HASH:
        return jsonify({"owner": True, "csrf_token": ""})
    if not _is_owner():
        return jsonify({"owner": False}), 401
    return jsonify({"owner": True, "csrf_token": g.owner_session["csrf_token"]})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    if not OWNER_PASSWORD_HASH:
        return jsonify({"error": "Auth is not configured"}), 400
    now_ts = _utcnow().timestamp()
    ip = _client_ip()
    if _is_locked_out(ip, now_ts):
        return jsonify({"error": "Too many login attempts. Try again later."}), 429

    data = request.get_json(silent=True) or {}
    password = data.get("password", "")
    if not password or not check_password_hash(OWNER_PASSWORD_HASH, password):
        _record_login_failure(ip, now_ts)
        return jsonify({"error": "Invalid credentials"}), 401

    _login_attempts.pop(ip, None)
    _lockouts.pop(ip, None)
    token, csrf_token = _create_session()
    resp = make_response(jsonify({"ok": True, "csrf_token": csrf_token}))
    secure_cookie = not request.host.startswith("127.0.0.1") and not request.host.startswith("localhost")
    resp.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=secure_cookie,
        samesite="Strict",
        max_age=int(timedelta(hours=SESSION_TTL_HOURS).total_seconds()),
        path="/",
    )
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    if OWNER_PASSWORD_HASH and not _require_csrf():
        return jsonify({"error": "Invalid CSRF token"}), 403
    token = request.cookies.get(COOKIE_NAME, "")
    if token:
        with db.get_db() as conn:
            conn.execute("UPDATE auth_sessions SET revoked=1 WHERE token_hash=?", (_hash_token(token),))
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# --- Collection CRUD ------------------------------------------------------

@app.route("/api/records")
def list_records():
    q     = request.args.get("q", "").strip()
    sort  = request.args.get("sort", "date_added")
    order = request.args.get("order", "desc").upper()

    allowed_sorts = {"artist", "title", "year", "date_added", "format", "label", "average_sell_price"}
    if sort not in allowed_sorts:
        sort = "date_added"
    if order not in ("ASC", "DESC"):
        order = "DESC"

    # When sorting by artist, use year as secondary sort (nulls last)
    if sort == "artist":
        artist_sort_key = (
            "CASE "
            "WHEN LOWER(artist) LIKE 'the %' THEN SUBSTR(artist, 5) "
            "ELSE artist "
            "END"
        )
        order_clause = (
            f"{artist_sort_key} COLLATE NOCASE {order}, "
            f"CASE WHEN year IS NULL THEN 9999 ELSE year END ASC"
        )
    elif sort == "average_sell_price":
        order_clause = (
            f"CASE WHEN average_sell_price IS NULL THEN 1 ELSE 0 END ASC, "
            f"average_sell_price {order}"
        )
    else:
        order_clause = f"{sort} COLLATE NOCASE {order}"

    with db.get_db() as conn:
        if q:
            pat = f"%{q}%"
            rows = conn.execute(
                f"""SELECT * FROM records
                    WHERE artist LIKE ? OR title LIKE ? OR label LIKE ? OR genres LIKE ?
                    ORDER BY {order_clause}""",
                (pat, pat, pat, pat),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM records ORDER BY {order_clause}"
            ).fetchall()

    return jsonify([db.row_to_dict(r) for r in rows])


@app.route("/api/records/<int:record_id>")
def get_record(record_id):
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(db.row_to_dict(row))


@app.route("/api/records", methods=["POST"])
def add_record():
    if not _is_owner():
        return jsonify({"error": "Unauthorized"}), 401
    if not _require_csrf():
        return jsonify({"error": "Invalid CSRF token"}), 403
    data = request.get_json(silent=True) or {}
    artist = data.get("artist", "").strip()
    title  = data.get("title",  "").strip()
    if not artist or not title:
        return jsonify({"error": "'artist' and 'title' are required"}), 400

    spotify_url = None
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        spotify_url = sp.find_album_url(artist, title, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)

    try:
        with db.get_db() as conn:
            cur = conn.execute(
                """INSERT INTO records
                   (artist, title, year, format, label, catalog_number,
                    genres, purchase_price, market_value, average_sell_price,
                    cover_url, discogs_id, spotify_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    artist,
                    title,
                    data.get("year")            or None,
                    data.get("format",         "").strip(),
                    data.get("label",          "").strip(),
                    data.get("catalog_number", "").strip(),
                    data.get("genres",         "").strip(),
                    _num_or_none(data.get("purchase_price")),
                    _num_or_none(data.get("market_value")),
                    _num_or_none(data.get("average_sell_price")),
                    data.get("cover_url",      "").strip(),
                    data.get("discogs_id")     or None,
                    spotify_url,
                ),
            )
    except sqlite3.Error as e:
        return jsonify({"error": f"Database save failed: {e}"}), 500
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/records/<int:record_id>", methods=["PUT"])
def update_record(record_id):
    if not _is_owner():
        return jsonify({"error": "Unauthorized"}), 401
    if not _require_csrf():
        return jsonify({"error": "Invalid CSRF token"}), 403
    data = request.get_json(silent=True) or {}
    try:
        with db.get_db() as conn:
            if not conn.execute("SELECT id FROM records WHERE id=?", (record_id,)).fetchone():
                return jsonify({"error": "Not found"}), 404
            conn.execute(
                """UPDATE records SET
                   artist=?, title=?, year=?, format=?, label=?, catalog_number=?,
                   genres=?, purchase_price=?, market_value=?, average_sell_price=?,
                   cover_url=?, discogs_id=?
                   WHERE id=?""",
                (
                    data.get("artist",         "").strip(),
                    data.get("title",          "").strip(),
                    data.get("year")            or None,
                    data.get("format",         "").strip(),
                    data.get("label",          "").strip(),
                    data.get("catalog_number", "").strip(),
                    data.get("genres",         "").strip(),
                    _num_or_none(data.get("purchase_price")),
                    _num_or_none(data.get("market_value")),
                    _num_or_none(data.get("average_sell_price")),
                    data.get("cover_url",      "").strip(),
                    data.get("discogs_id")     or None,
                    record_id,
                ),
            )
    except sqlite3.Error as e:
        return jsonify({"error": f"Database update failed: {e}"}), 500
    return jsonify({"ok": True})


@app.route("/api/records/<int:record_id>", methods=["DELETE"])
def delete_record(record_id):
    if not _is_owner():
        return jsonify({"error": "Unauthorized"}), 401
    if not _require_csrf():
        return jsonify({"error": "Invalid CSRF token"}), 403
    with db.get_db() as conn:
        conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
    return jsonify({"ok": True})


@app.route("/api/records/<int:record_id>/spotify", methods=["POST"])
def refresh_spotify(record_id):
    if not _is_owner():
        return jsonify({"error": "Unauthorized"}), 401
    if not _require_csrf():
        return jsonify({"error": "Invalid CSRF token"}), 403
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return jsonify({"error": "Spotify not configured"}), 503
    with db.get_db() as conn:
        row = conn.execute("SELECT artist, title FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        url = sp.find_album_url(row["artist"], row["title"], SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
        if url:
            conn.execute("UPDATE records SET spotify_url=? WHERE id=?", (url, record_id))
    return jsonify({"spotify_url": url})


@app.route("/api/records/backfill-average-sell", methods=["POST"])
def backfill_average_sell_price():
    if not _is_owner():
        return jsonify({"error": "Unauthorized"}), 401
    if not _require_csrf():
        return jsonify({"error": "Invalid CSRF token"}), 403
    if not DISCOGS_TOKEN:
        return _discogs_error()

    data = request.get_json(silent=True) or {}
    try:
        limit = int(data.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))

    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, discogs_id
               FROM records
               WHERE average_sell_price IS NULL
                 AND discogs_id IS NOT NULL
               ORDER BY id ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()

        updated = 0
        checked = 0
        rate_limited = False
        for row in rows:
            checked += 1
            detail = dg.get_release(row["discogs_id"], DISCOGS_TOKEN)
            if detail is None:
                continue
            if detail.get("_rate_limited"):
                rate_limited = True
                break
            avg = detail.get("average_sell_price")
            if avg is None:
                continue
            conn.execute(
                "UPDATE records SET average_sell_price=? WHERE id=?",
                (avg, row["id"]),
            )
            updated += 1

    return jsonify({
        "checked": checked,
        "updated": updated,
        "rate_limited": rate_limited,
    })


# --- Discogs integration --------------------------------------------------

def _discogs_error():
    return jsonify({"error": "Discogs token not configured — check config.json"}), 503


@app.route("/api/discogs/barcode")
def discogs_barcode():
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "No barcode provided"}), 400
    if not DISCOGS_TOKEN:
        return _discogs_error()

    results = dg.search_barcode(code, DISCOGS_TOKEN)
    if results is None:
        return jsonify({"error": "rate_limited", "retry_after": 60}), 429
    return jsonify({"results": results})


@app.route("/api/discogs/search")
def discogs_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "No query provided"}), 400
    if not DISCOGS_TOKEN:
        return _discogs_error()

    results = dg.search_text(q, DISCOGS_TOKEN)
    if results is None:
        return jsonify({"error": "rate_limited", "retry_after": 60}), 429
    return jsonify({"results": results})


@app.route("/api/discogs/release/<int:release_id>")
def discogs_release(release_id):
    if not DISCOGS_TOKEN:
        return _discogs_error()

    detail = dg.get_release(release_id, DISCOGS_TOKEN)
    if detail is None:
        return jsonify({"error": "Release not found or network error"}), 404
    if detail.get("_rate_limited"):
        return jsonify({"error": "rate_limited", "retry_after": 60}), 429
    return jsonify(detail)


# --- Photo identification -------------------------------------------------

MAX_PHOTO_BYTES = 5 * 1024 * 1024  # 5 MB


@app.route("/api/photo/identify", methods=["POST"])
def photo_identify():
    if "photo" not in request.files:
        return jsonify({"error": "No photo file in request"}), 400

    photo = request.files["photo"]
    image_bytes = photo.read(MAX_PHOTO_BYTES + 1)
    if len(image_bytes) > MAX_PHOTO_BYTES:
        return jsonify({"error": "Photo too large (max 5 MB)"}), 413

    try:
        image_bytes, media_type = vision.resize_image(image_bytes)
        print(f"[photo/identify] Resized to {len(image_bytes):,} bytes", flush=True)
    except Exception as e:
        print(f"[photo/identify] Resize error: {e}", flush=True)
        return jsonify({"error": "Could not process image"}), 422

    result = {"artist": "", "title": ""}

    # Gemini 2.5 Flash — text + visual identification
    if GEMINI_API_KEY:
        try:
            gemini = vision.identify_record(image_bytes, media_type, GEMINI_API_KEY)
            result["artist"] = gemini.get("artist", "")
            result["title"]  = gemini.get("title",  "")
            print(f"[photo/identify] Gemini: artist='{result['artist']}' title='{result['title']}'", flush=True)
        except ValueError as e:
            print(f"[photo/identify] Gemini parse error: {e}", flush=True)
        except Exception as e:
            print(f"[photo/identify] Gemini error: {type(e).__name__}: {e}", flush=True)
    else:
        print("[photo/identify] Gemini API key not configured — skipping", flush=True)

    if not result["artist"] and not result["title"]:
        return jsonify({"error": "Could not identify record — try a clearer photo"}), 422

    return jsonify(result)


# --- Stats ----------------------------------------------------------------

@app.route("/api/stats")
def stats():
    with db.get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    return jsonify({
        "total_records": total,
        "discogs_token": bool(DISCOGS_TOKEN),
        "gemini_key":    bool(GEMINI_API_KEY),
    })


# --- Run ------------------------------------------------------------------

if __name__ == "__main__":
    from werkzeug.serving import make_server
    bind_host = os.environ.get("VINYL_BIND_HOST", "127.0.0.1")
    bind_port = int(os.environ.get("VINYL_BIND_PORT", "5003"))
    print(f"[startup] Internal app listener on {bind_host}:{bind_port}", flush=True)
    make_server(bind_host, bind_port, app).serve_forever()
