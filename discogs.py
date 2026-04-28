import time
import requests

DISCOGS_BASE = "https://api.discogs.com"
_HEADERS = {
    "User-Agent": "VinylInventory/1.0",
    "Accept": "application/json",
}
_RATE_INTERVAL = 2.5  # 24 req/min — safely under the 25/min authenticated limit
_last_request = 0.0


def _get(endpoint: str, params: dict):
    global _last_request
    wait = _RATE_INTERVAL - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = requests.get(
            f"{DISCOGS_BASE}{endpoint}",
            headers=_HEADERS,
            params=params,
            timeout=12,
        )
        _last_request = time.time()
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            return {"_rate_limited": True}
        return None
    except requests.RequestException:
        return None


def _parse_artist_title(raw_title: str) -> tuple[str, str]:
    """Split Discogs 'Artist - Title' format into (artist, title)."""
    parts = raw_title.split(" - ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", raw_title.strip()


def _extract_results(data) -> list[dict]:
    if not data or data.get("_rate_limited"):
        return []
    results = data.get("results", [])
    out = []
    for r in results[:10]:
        raw_title = r.get("title", "")
        artist, title = _parse_artist_title(raw_title)
        out.append({
            "discogs_id":     r.get("id"),
            "artist":         artist,
            "title":          title,
            "year":           r.get("year"),
            "format":         ", ".join(r.get("format", [])),
            "label":          r.get("label", [""])[0] if r.get("label") else "",
            "catalog_number": r.get("catno", ""),
            "genres":         ", ".join(r.get("genre", []) + r.get("style", [])),
            "cover_url":      r.get("cover_image", ""),
            "thumb":          r.get("thumb", ""),
        })
    return out


def _normalize_release(data: dict) -> dict:
    labels = data.get("labels", [{}])
    label = labels[0].get("name", "") if labels else ""
    catno = labels[0].get("catno", "") if labels else ""
    formats = data.get("formats", [{}])
    fmt_name = formats[0].get("name", "") if formats else ""
    images = data.get("images", [])
    cover = images[0].get("uri", "") if images else ""
    genres = data.get("genres", []) + data.get("styles", [])

    artists_list = data.get("artists", [])
    if artists_list:
        artist = artists_list[0].get("name", "").rstrip(" (2)(3)(4)")
    else:
        artist, _ = _parse_artist_title(data.get("title", ""))

    title = data.get("title", "")
    average_sell_price = _extract_average_sell_price(data)

    return {
        "discogs_id":     data.get("id"),
        "artist":         artist,
        "title":          title,
        "year":           data.get("year"),
        "format":         fmt_name,
        "label":          label,
        "catalog_number": catno,
        "genres":         ", ".join(genres),
        "cover_url":      cover,
        "average_sell_price": average_sell_price,
    }


def _extract_average_sell_price(data: dict):
    candidates = [
        data.get("average_sale_price"),
        data.get("marketplace_stats", {}).get("average_price"),
        data.get("marketplace_stats", {}).get("median_price"),
        data.get("lowest_price"),
    ]

    for value in candidates:
        if isinstance(value, dict):
            value = value.get("value")
        if value is None:
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if num >= 0:
            return round(num, 2)
    return None


def search_barcode(barcode: str, token: str) -> list[dict]:
    data = _get("/database/search", {"barcode": barcode, "token": token, "type": "release", "format": "Vinyl"})
    if data and data.get("_rate_limited"):
        return None
    return _extract_results(data)


def search_text(query: str, token: str) -> list[dict]:
    data = _get("/database/search", {"q": query, "type": "release", "token": token, "format": "Vinyl"})
    if data and data.get("_rate_limited"):
        return None
    return _extract_results(data)


def get_release(release_id: int, token: str):
    data = _get(f"/releases/{release_id}", {"token": token})
    if data is None:
        return None
    if data.get("_rate_limited"):
        return {"_rate_limited": True}
    return _normalize_release(data)
