import json, secrets, sqlite3
from pathlib import Path
from flask import Flask, request, jsonify, render_template, Response, redirect

import db
import discogs as dg
import vision
import spotify as sp

app = Flask(__name__)

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
OWNER_PIN             = CONFIG.get("owner_pin",             "").strip()

# --- Auth -----------------------------------------------------------------
_valid_tokens: set = set()

def _is_owner():
    if not OWNER_PIN:
        return True
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return token in _valid_tokens


def _num_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

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
const CACHE = 'vinyl-v3';
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
  // Always hit the network for API calls — never serve from cache
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
    # On the HTTP port, redirect non-iOS devices to HTTPS for camera support.
    # Skip when behind Cloudflare — CF already provides HTTPS to the visitor.
    if request.scheme == "http" and "CF-Visitor" not in request.headers:
        ua = request.headers.get("User-Agent", "")
        is_ios = "iPhone" in ua or "iPad" in ua or "iPod" in ua
        cert = Path(__file__).parent / "cert.pem"
        if not is_ios and cert.exists():
            host = request.host.split(":")[0]
            return redirect(f"https://{host}:5001", code=302)
    return render_template("index.html")


# --- Auth endpoints -------------------------------------------------------

@app.route("/api/auth/status")
def auth_status():
    return jsonify({"auth_enabled": bool(OWNER_PIN)})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    if OWNER_PIN and data.get("pin") == OWNER_PIN:
        token = secrets.token_hex(32)
        _valid_tokens.add(token)
        return jsonify({"token": token})
    return jsonify({"error": "Wrong PIN"}), 401


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    _valid_tokens.discard(token)
    return jsonify({"ok": True})


# --- Collection CRUD ------------------------------------------------------

@app.route("/api/records")
def list_records():
    q     = request.args.get("q", "").strip()
    sort  = request.args.get("sort", "date_added")
    order = request.args.get("order", "desc").upper()

    allowed_sorts = {"artist", "title", "year", "date_added", "format", "label"}
    if sort not in allowed_sorts:
        sort = "date_added"
    if order not in ("ASC", "DESC"):
        order = "DESC"

    # When sorting by artist, use year as secondary sort (nulls last)
    if sort == "artist":
        order_clause = (
            f"artist COLLATE NOCASE {order}, "
            f"CASE WHEN year IS NULL THEN 9999 ELSE year END ASC"
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
        return jsonify({"error": "Forbidden"}), 403
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
        return jsonify({"error": "Forbidden"}), 403
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
        return jsonify({"error": "Forbidden"}), 403
    with db.get_db() as conn:
        conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
    return jsonify({"ok": True})


@app.route("/api/records/<int:record_id>/spotify", methods=["POST"])
def refresh_spotify(record_id):
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
        return jsonify({"error": "Forbidden"}), 403
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
    import threading

    cert = Path(__file__).parent / "cert.pem"
    key  = Path(__file__).parent / "key.pem"

    if cert.exists() and key.exists():
        # HTTP on 5002 — works on any device without certificate trust (browsing, no camera)
        http_server = make_server("0.0.0.0", 5002, app)
        threading.Thread(target=http_server.serve_forever, daemon=True).start()
        print("[startup] HTTP  on :5002 (all devices — browsing)", flush=True)
        print("[startup] HTTPS on :5001 (camera scanning — Android)", flush=True)
        # HTTPS on 5001 — required for getUserMedia (camera scanning)
        make_server("0.0.0.0", 5001, app, ssl_context=(str(cert), str(key))).serve_forever()
    else:
        print("[startup] HTTP on :5001", flush=True)
        make_server("0.0.0.0", 5001, app).serve_forever()
