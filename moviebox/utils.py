import asyncio
import base64
import json

def get_event_loop():
    try:
        event_loop = asyncio.get_event_loop()
    except RuntimeError:
        event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(event_loop)
    return event_loop

def is_deprecation_notice_url(url: str) -> bool:
    lower = str(url).lower()
    return (
        "1c7de0bd3393702d9191801f15f88f8d" in lower
        or "9a0461bc39da389663bf3dbb17091d3f" in lower
        or "b164fbfb4347792950bdfbfb563d39d9" in lower
        or "/notice.mp4" in lower
        or ("macdn.aoneroom.com" in lower and "/other/" in lower)
    )

def decode_b64_padded(s: str) -> bytes | None:
    padding = (4 - len(s) % 4) % 4
    if padding > 0:
        s += "=" * padding
    try:
        return base64.b64decode(s.encode("ascii"))
    except Exception:
        return None

def resolve_dash_manifest_from_cookie(sign_cookie: str) -> str | None:
    if not sign_cookie:
        return None
    for part in sign_cookie.split(";"):
        trimmed = part.strip()
        # 1. Edge-Cache-Cookie urlprefix=...
        if "urlprefix=" in trimmed:
            prefix_part = trimmed.split("urlprefix=", 1)[1]
            b64_token = prefix_part.split(":", 1)[0].strip()
            normalized = b64_token.replace("-", "+").replace("_", "/")
            decoded = decode_b64_padded(normalized)
            if decoded:
                try:
                    url_str = decoded.decode("utf-8", errors="ignore").rstrip("*").rstrip("/")
                    if url_str.startswith("http://") or url_str.startswith("https://"):
                        return f"{url_str}/index.mpd"
                except Exception:
                    pass
        # 2. CloudFront-Policy=...
        if trimmed.startswith("CloudFront-Policy="):
            policy_raw = trimmed.split("CloudFront-Policy=", 1)[1].strip()
            normalized = policy_raw.replace("-", "+").replace("_", "=").replace("~", "/")
            decoded = decode_b64_padded(normalized)
            if decoded:
                try:
                    data = json.loads(decoded.decode("utf-8", errors="ignore"))
                    resource = data.get("Statement", [{}])[0].get("Resource", "")
                    if resource:
                        url_str = str(resource).rstrip("*").rstrip("/")
                        if url_str.startswith("http://") or url_str.startswith("https://"):
                            return f"{url_str}/index.mpd" if not url_str.endswith(".mpd") else url_str
                except Exception:
                    pass
    return None

def b64_url_encode(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")

def b64_url_decode(s: str) -> str:
    padding = (4 - len(s) % 4) % 4
    if padding > 0:
        s += "=" * padding
    return base64.urlsafe_b64decode(s.encode("ascii")).decode("utf-8")

