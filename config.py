"""Central configuration for the Twitter/X Engagement Extractor."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _resolve_token() -> str:
    """Read the Apify token from env first, then Streamlit Cloud secrets."""
    tok = os.getenv("APIFY_API_TOKEN", "")
    if tok:
        return tok
    # st.secrets is only available inside a Streamlit runtime, and only when
    # a secrets.toml has been configured. Wrap so local/non-Streamlit imports
    # don't blow up.
    try:
        import streamlit as st  # noqa: WPS433 — deliberately deferred
        return st.secrets.get("APIFY_API_TOKEN", "")
    except Exception:  # noqa: BLE001
        return ""


@dataclass(frozen=True)
class ApifyActor:
    """A single Apify actor candidate and how to build its run input."""

    actor_id: str
    label: str
    input_style: str = "urls"   # "urls" | "ids"

    def build_input(self, urls: List[str]) -> dict:
        if self.input_style == "ids":
            # Actor takes bare tweet IDs (e.g. danek/twitter-scraper).
            ids = [u.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0] for u in urls]
            return {"lookup_post_ids": ids, "max_posts": max(len(ids), 1)}
        # Default: URL-based. Most tweet-URL scrapers accept `startUrls`;
        # unknown keys are ignored so we send common variants for tolerance.
        return {
            "startUrls": [{"url": u} for u in urls],
            "postUrls": urls,
            "tweetUrls": urls,
            "urls": urls,
            "maxItems": len(urls),
        }


# ---------------------------------------------------------------------------
# Twitter / X actors — first entry is primary; rest are fallbacks tried
# automatically when the primary fails or omits some tweets.
# Verified 2026-08-04: accept tweet URLs, return engagement fields, no manual
# permission approval required.
# ---------------------------------------------------------------------------
TWITTER_ACTORS: List[ApifyActor] = [
    ApifyActor(actor_id="xquik/x-tweet-scraper", label="xquik/x-tweet-scraper"),
    ApifyActor(actor_id="parseforge/x-com-scraper", label="parseforge/x-com-scraper"),
    ApifyActor(actor_id="danek/twitter-scraper", label="danek/twitter-scraper (by id)",
               input_style="ids"),
]


@dataclass(frozen=True)
class FacebookActor:
    actor_id: str
    label: str
    url_field: str = "direct_urls"  # danek uses direct_urls; apify uses startUrls

    def build_input(self, urls: List[str]) -> dict:
        # Both danek and apify use the `requestListSources` editor, which
        # requires {"url": "..."} objects — a plain string array fails
        # validation with "Items in input.<field> at positions [...] not valid".
        as_objects = [{"url": u} for u in urls]
        payload = {
            self.url_field: as_objects,
            "startUrls": as_objects,
            "maxItems": len(urls),
            "max_posts": 1,       # one post per URL, since each URL IS the post
            "resultsLimit": len(urls),
        }
        return payload


# ---------------------------------------------------------------------------
# Facebook post actors — verified to accept individual post URLs and return
# reactions/comments/shares/views on paid Apify plans.
# ---------------------------------------------------------------------------
FACEBOOK_ACTORS: List[FacebookActor] = [
    FacebookActor(actor_id="danek/facebook-posts-fast",
                  label="danek/facebook-posts-fast",
                  url_field="direct_urls"),
    FacebookActor(actor_id="apify/facebook-posts-scraper",
                  label="apify/facebook-posts-scraper",
                  url_field="startUrls"),
]


# Back-compat — some older tests/scripts reference DEFAULT_ACTORS.
DEFAULT_ACTORS = TWITTER_ACTORS


@dataclass(frozen=True)
class Settings:
    apify_token: str = field(default_factory=_resolve_token)
    actor_run_timeout_secs: int = 600
    batch_size: int = 50  # URLs per actor call

    @property
    def has_token(self) -> bool:
        return bool(self.apify_token and self.apify_token != "your_apify_api_token_here")


settings = Settings()
