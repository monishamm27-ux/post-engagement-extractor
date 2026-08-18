"""URL parsing, validation, and normalization for supported platforms."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

# --- Twitter / X ------------------------------------------------------------
_TWEET_RE = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/"
    r"(?P<user>[A-Za-z0-9_]{1,15})/status/(?P<id>\d+)",
    re.IGNORECASE,
)

# --- Facebook ---------------------------------------------------------------
# posts URL variants covered:
#   /{user}/posts/{id}
#   /{user}/posts/pfbid...
#   /{user}/videos/{id}
#   /{user}/photos/...{id}
#   /permalink.php?story_fbid=...&id=...
#   /story.php?story_fbid=...&id=...
#   /watch/?v=...
#   fb.watch/{shortcode}
#   /share/p/{code}   /share/v/{code}   /share/r/{code}
_FB_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com",
             "mobile.facebook.com", "fb.watch", "fb.com"}
_FB_PATH_RE = re.compile(
    r"^/(?P<user>[^/]+)/(?:posts|videos|photos|reel)/(?P<id>[A-Za-z0-9_.-]+)/?",
    re.IGNORECASE,
)
_FB_SHARE_RE = re.compile(r"^/share/[a-zA-Z]/(?P<id>[A-Za-z0-9_-]+)/?", re.IGNORECASE)
# CrowdTangle / Meta Content Library composite id: /{page_id}_{post_id}
# Both segments are numeric; used by facebook.com export tools.
_FB_COMPOSITE_RE = re.compile(r"^/(?P<page>\d+)_(?P<post>\d+)/?$")


@dataclass(frozen=True)
class PostRef:
    platform: str            # "twitter" | "facebook"
    user: str                 # may be "" for FB URLs that don't expose one
    post_id: str              # canonical identifier used for de-duplication
    canonical_url: str


def _parse_tweet(url: str) -> Optional[PostRef]:
    m = _TWEET_RE.match(url)
    if not m:
        return None
    user = m.group("user")
    tweet_id = m.group("id")
    return PostRef(
        platform="twitter",
        user=user,
        post_id=tweet_id,
        canonical_url=f"https://x.com/{user}/status/{tweet_id}",
    )


def _parse_facebook(url: str) -> Optional[PostRef]:
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    host = (p.hostname or "").lower()
    if host not in _FB_HOSTS:
        return None

    # fb.watch/{code}
    if host == "fb.watch":
        code = p.path.strip("/").split("/", 1)[0]
        if code:
            return PostRef(platform="facebook", user="", post_id=code,
                           canonical_url=f"https://fb.watch/{code}")
        return None

    qs = parse_qs(p.query or "")

    # /permalink.php?story_fbid=...&id=...
    # /story.php?story_fbid=...&id=...
    if p.path in {"/permalink.php", "/story.php"}:
        story = (qs.get("story_fbid") or [""])[0]
        uid = (qs.get("id") or [""])[0]
        if story:
            pid = f"{uid}:{story}" if uid else story
            return PostRef(platform="facebook", user=uid, post_id=pid,
                           canonical_url=f"https://www.facebook.com{p.path}?story_fbid={story}"
                                         + (f"&id={uid}" if uid else ""))
        return None

    # /watch/?v=<video_id>
    if p.path.rstrip("/") == "/watch":
        vid = (qs.get("v") or [""])[0]
        if vid:
            return PostRef(platform="facebook", user="", post_id=vid,
                           canonical_url=f"https://www.facebook.com/watch/?v={vid}")

    # /share/p|v|r/{code}
    m = _FB_SHARE_RE.match(p.path)
    if m:
        code = m.group("id")
        return PostRef(platform="facebook", user="", post_id=code,
                       canonical_url=f"https://www.facebook.com{p.path.rstrip('/')}")

    # /reel/{id} (no user segment)
    parts = [x for x in p.path.split("/") if x]
    if len(parts) == 2 and parts[0].lower() == "reel":
        return PostRef(platform="facebook", user="", post_id=parts[1],
                       canonical_url=f"https://www.facebook.com/reel/{parts[1]}")

    # CrowdTangle composite: /{page_id}_{post_id}. Normalize to the standard
    # /permalink form so the scraper actor can resolve it.
    m = _FB_COMPOSITE_RE.match(p.path)
    if m:
        page_id = m.group("page")
        post_id = m.group("post")
        return PostRef(
            platform="facebook", user=page_id, post_id=f"{page_id}:{post_id}",
            canonical_url=f"https://www.facebook.com/permalink.php"
                          f"?story_fbid={post_id}&id={page_id}",
        )

    # /{user}/posts|videos|photos|reel/{id}
    m = _FB_PATH_RE.match(p.path)
    if m:
        user = m.group("user")
        pid = m.group("id")
        return PostRef(platform="facebook", user=user, post_id=f"{user}:{pid}",
                       canonical_url=f"https://www.facebook.com/{user}/"
                                     f"{p.path.strip('/').split('/')[1]}/{pid}")
    return None


def parse_post_url(raw: str) -> Optional[PostRef]:
    """Detect the platform and return a canonical PostRef, or None if unsupported."""
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url:
        return None
    return _parse_tweet(url) or _parse_facebook(url)


# Back-compat shim — keep the old name working for existing callers.
parse_tweet_url = _parse_tweet
TweetRef = PostRef
