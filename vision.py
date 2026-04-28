import base64
import io
import json
import re
import requests
from PIL import Image, ImageOps

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1"
    "/models/gemini-2.5-flash:generateContent"
)
MAX_SIDE = 1568


def resize_image(image_bytes: bytes) -> tuple[bytes, str]:
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > MAX_SIDE:
        ratio = MAX_SIDE / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue(), "image/jpeg"


def identify_record(image_bytes: bytes, media_type: str, api_key: str) -> dict:
    """
    Send photo to Gemini 2.5 Flash for artist/title extraction.
    Returns {"artist": "...", "title": "..."}.
    Raises ValueError if response cannot be parsed.
    Raises requests.HTTPError on API failures.
    """
    b64 = base64.b64encode(image_bytes).decode()

    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": media_type, "data": b64}},
                {"text": (
                    "This is a photo of a vinyl record sleeve or label. "
                    "Identify the artist name and album title — use both the visual artwork "
                    "and any text visible on the sleeve or label. "
                    'Return ONLY valid JSON in this exact format: {"artist": "...", "title": "..."}. '
                    "Use an empty string for any value you cannot determine. "
                    "No explanation, no markdown, just the JSON object."
                )},
            ]
        }],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 512},
    }

    resp = requests.post(
        GEMINI_URL,
        params={"key": api_key},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()

    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        result = json.loads(raw)
        return {
            "artist": str(result.get("artist", "")).strip(),
            "title":  str(result.get("title",  "")).strip(),
        }
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{[^{}]*"artist"[^{}]*\}', raw, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            return {
                "artist": str(result.get("artist", "")).strip(),
                "title":  str(result.get("title",  "")).strip(),
            }
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse Gemini response: {raw[:200]}")
