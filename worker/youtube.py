"""YouTube: OAuth token refresh, handle → channel id, RSS discovery, resumable uploads.
Standard library only."""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

from .net import request

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"

_token_cache: dict = {}


def have_credentials() -> bool:
    return all(os.environ.get(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))


def access_token() -> str:
    if _token_cache.get("token"):
        return _token_cache["token"]
    body = urllib.parse.urlencode({
        "client_id": os.environ["YT_CLIENT_ID"],
        "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    out = request("POST", TOKEN_URL, raw=body,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    _token_cache["token"] = out["access_token"]
    return out["access_token"]


def _auth() -> dict:
    return {"Authorization": f"Bearer {access_token()}"}


# ---------------------------------------------------------------- channels
CHANNEL_ID_RE = re.compile(r"(UC[0-9A-Za-z_-]{22})")


def parse_creator_input(text: str) -> tuple[str | None, str | None]:
    """Accepts '@handle', a channel URL, or a UC… id. Returns (channel_id, handle)."""
    text = (text or "").strip()
    m = CHANNEL_ID_RE.search(text)
    if m:
        return m.group(1), None
    m = re.search(r"@([A-Za-z0-9._-]{3,})", text)
    if m:
        return None, "@" + m.group(1)
    if re.fullmatch(r"[A-Za-z0-9._-]{3,}", text):
        return None, "@" + text
    return None, None


def resolve_handle(handle: str) -> tuple[str, str] | None:
    """@handle → (channel_id, title). Costs 1 quota unit."""
    q = urllib.parse.urlencode({"part": "id,snippet", "forHandle": handle})
    out = request("GET", f"{API}/channels?{q}", headers=_auth())
    items = out.get("items") or []
    if not items:
        return None
    return items[0]["id"], items[0]["snippet"]["title"]


def channel_info(channel_id: str) -> str | None:
    q = urllib.parse.urlencode({"part": "snippet", "id": channel_id})
    out = request("GET", f"{API}/channels?{q}", headers=_auth())
    items = out.get("items") or []
    return items[0]["snippet"]["title"] if items else None


# ---------------------------------------------------------------- discovery
_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    vids = []
    for e in root.findall("a:entry", _NS):
        vid = e.findtext("yt:videoId", namespaces=_NS)
        link = e.find("a:link", _NS)
        href = link.get("href", "") if link is not None else ""
        vids.append({
            "id": vid,
            "title": e.findtext("a:title", namespaces=_NS),
            "published_at": e.findtext("a:published", namespaces=_NS),
            "is_short": "/shorts/" in href,
        })
    return [v for v in vids if v["id"]]


def recent_uploads(channel_id: str) -> list[dict]:
    """Latest ~15 uploads via the public RSS feed (free, no API quota)."""
    xml_text = request("GET", RSS_URL.format(cid=channel_id), timeout=30)
    if isinstance(xml_text, bytes):
        xml_text = xml_text.decode("utf-8", "replace")
    return parse_feed(xml_text)


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- upload
def upload_video(path: str, *, title: str, description: str, tags: list[str],
                 privacy: str = "public", category_id: str = "20") -> str:
    """Resumable upload in a single PUT (clips are < 50 MB). Returns the new video id."""
    meta = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": _trim_tags(tags),
            "categoryId": category_id,  # 20 = Gaming
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        },
    }
    size = os.path.getsize(path)
    _, headers = request(
        "POST", f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status",
        headers={**_auth(), "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
        raw=json.dumps(meta).encode(), want_headers=True)
    location = headers.get("Location") or headers.get("location")
    if not location:
        raise RuntimeError("YouTube did not return an upload URL")
    with open(path, "rb") as f:
        data = f.read()
    out = request("PUT", location, raw=data, timeout=900, retries=3,
                  headers={**_auth(), "Content-Type": "video/mp4"})
    if isinstance(out, (bytes, str)):
        out = json.loads(out)
    return out["id"]


def _trim_tags(tags: list[str]) -> list[str]:
    """YouTube caps total tag length at ~500 chars."""
    out, total = [], 0
    for t in tags or []:
        t = t.strip().replace("<", "").replace(">", "")
        if not t:
            continue
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > 480:
            break
        out.append(t)
        total += cost
    return out
