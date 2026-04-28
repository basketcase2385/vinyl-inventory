import time, base64, requests

_token = None
_token_expiry = 0.0


def _get_token(client_id, client_secret):
    global _token, _token_expiry
    if _token and time.time() < _token_expiry:
        return _token
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {creds}"},
        timeout=5,
    )
    r.raise_for_status()
    data = r.json()
    _token = data["access_token"]
    _token_expiry = time.time() + data["expires_in"] - 60
    return _token


def find_album_url(artist, title, client_id, client_secret):
    """Return the Spotify album URL for the closest match, or None."""
    try:
        token = _get_token(client_id, client_secret)
        r = requests.get(
            "https://api.spotify.com/v1/search",
            params={"q": f"{artist} {title}", "type": "album", "limit": 1},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        r.raise_for_status()
        items = r.json().get("albums", {}).get("items", [])
        if items:
            return items[0]["external_urls"]["spotify"]
    except Exception:
        pass
    return None
