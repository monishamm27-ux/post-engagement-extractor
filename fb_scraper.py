"""Apify-backed Facebook post metric fetching with actor fallback."""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from apify_client import ApifyClient

from config import FACEBOOK_ACTORS, FacebookActor, settings
from scraper import TweetMetrics  # reuse the same shape

log = logging.getLogger(__name__)

# Actor-specific field variants for the same engagement metric.
_VIEW_KEYS = ("video_view_count", "videoViewCount", "viewCount", "views", "playCount")
_LIKE_KEYS = ("reactions_count", "reactionsCount", "likesCount", "likes",
              "reactionCount", "likeCount")
_COMMENT_KEYS = ("comments_count", "commentsCount", "comments", "commentCount")
_SHARE_KEYS = ("reshare_count", "reshareCount", "sharesCount", "shares",
               "shareCount", "shares_count", "repostCount")
_ID_KEYS = ("post_id", "postId", "topLevelPostId", "top_level_post_id",
            "postFacebookId", "story_fbid", "storyFbid", "fbid", "id")
_URL_KEYS = ("url", "postUrl", "post_url", "topLevelUrl", "top_level_url",
             "link", "permalink")


def _first_int(item: dict, keys) -> Optional[int]:
    for k in keys:
        v = item.get(k)
        if v is None:
            continue
        if isinstance(v, dict):
            # some actors nest counts: {"reactions": {"total_count": 12}}
            for inner in ("total_count", "count", "total", "value"):
                if inner in v:
                    try:
                        return int(v[inner])
                    except (TypeError, ValueError):
                        pass
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _extract_raw_post_id(item: dict) -> Optional[str]:
    """Return the actor's own post_id — matched later against our reverse index."""
    for k in _ID_KEYS:
        v = item.get(k)
        if v:
            return str(v)
    # Try URL parsing as a last resort (some actors omit post_id).
    from url_utils import parse_post_url  # local import avoids cycles
    for k in _URL_KEYS:
        u = item.get(k)
        if isinstance(u, str):
            ref = parse_post_url(u)
            if ref and ref.platform == "facebook":
                return ref.post_id
    return None


def _item_to_metrics(item: dict) -> TweetMetrics:
    err = item.get("error") or item.get("errorMessage")
    if err:
        return TweetMetrics(status="not_found", reason=str(err))
    return TweetMetrics(
        views=_first_int(item, _VIEW_KEYS),
        likes=_first_int(item, _LIKE_KEYS),
        replies=_first_int(item, _COMMENT_KEYS),
        retweets=_first_int(item, _SHARE_KEYS),
        status="ok",
    )


def _run_actor(
    client,
    actor: FacebookActor,
    urls: List[str],
    remaining_keys: Dict[str, str],
    timeout_secs: int,
) -> Dict[str, TweetMetrics]:
    """Call one actor. Returns metrics keyed by our composite id.

    remaining_keys maps composite `{page}:{post}` → canonical URL, so we can
    reverse-lookup the actor's returned post_id (which is just the numeric tail).
    """
    # Build a reverse index: raw post_id (tail) -> our composite key.
    # Composite keys are `{page}:{post}` or a plain string for other URL shapes.
    tail_to_key: Dict[str, str] = {}
    for key in remaining_keys:
        tail = key.rsplit(":", 1)[-1]
        tail_to_key[tail] = key

    run_input = actor.build_input(urls)
    log.info("Calling FB actor %s with %d urls", actor.label, len(urls))
    run = client.actor(actor.actor_id).call(run_input=run_input, timeout_secs=timeout_secs)
    if not run or not run.get("defaultDatasetId"):
        raise RuntimeError(f"FB actor {actor.label} returned no dataset")

    out: Dict[str, TweetMetrics] = {}
    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        raw = _extract_raw_post_id(item)
        if not raw:
            continue
        composite = tail_to_key.get(raw)
        if not composite:
            # Unmatched item — skip rather than misattribute.
            continue
        out[composite] = _item_to_metrics(item)
    return out


def fetch_facebook_metrics(
    urls_by_id: Dict[str, str],
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
    actors: Optional[List[FacebookActor]] = None,
) -> Dict[str, TweetMetrics]:
    """Fetch metrics for canonical Facebook post URLs — same interface as Twitter."""
    if not settings.has_token:
        raise RuntimeError(
            "APIFY_API_TOKEN is not set. Add it to a `.env` file or your environment."
        )
    if not urls_by_id:
        return {}

    actors = actors or FACEBOOK_ACTORS
    client = ApifyClient(settings.apify_token)

    from checkpoint import load_platform, save_platform

    cached = load_platform("facebook")
    results: Dict[str, TweetMetrics] = {
        k: cached[k] for k in urls_by_id if k in cached and cached[k].status == "ok"
    }
    remaining: Dict[str, str] = {k: v for k, v in urls_by_id.items() if k not in results}
    total = len(urls_by_id)
    last_error: Optional[str] = None
    if results:
        log.info("Resumed from checkpoint: %d/%d facebook posts already have data",
                 len(results), total)
        if progress_cb:
            progress_cb(len(results), 0, total)

    for actor in actors:
        if not remaining:
            break
        try:
            batch = _run_actor(client, actor, list(remaining.values()),
                               remaining, settings.actor_run_timeout_secs)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{actor.label}: {exc}"
            log.warning("FB actor %s failed: %s", actor.label, exc)
            continue

        just_added: Dict[str, TweetMetrics] = {}
        for pid, m in batch.items():
            if pid in remaining:
                results[pid] = m
                just_added[pid] = m
                remaining.pop(pid, None)

        save_platform("facebook", just_added)

        if progress_cb:
            progress_cb(len(results), 0, total)

    # --- Retry pass on small batches ---------------------------------------
    if remaining:
        log.info("FB retry pass for %d stragglers", len(remaining))
        for actor in actors:
            if not remaining:
                break
            keys = list(remaining.keys())
            for i in range(0, len(keys), 5):
                chunk_keys = keys[i : i + 5]
                chunk = {k: remaining[k] for k in chunk_keys}
                try:
                    r = _run_actor(client, actor, list(chunk.values()),
                                   chunk, settings.actor_run_timeout_secs)
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{actor.label} (retry): {exc}"
                    log.warning("FB retry on %s failed: %s", actor.label, exc)
                    continue
                just_added2: Dict[str, TweetMetrics] = {}
                for k, m in r.items():
                    if k in remaining:
                        results[k] = m
                        just_added2[k] = m
                        remaining.pop(k, None)
                save_platform("facebook", just_added2)
                if progress_cb:
                    progress_cb(len(results), 0, total)

    for pid in remaining:
        results[pid] = TweetMetrics(
            status="not_found",
            reason=last_error or "post not returned by any actor (deleted/private/unavailable)",
        )

    if progress_cb:
        failed = sum(1 for m in results.values() if m.status != "ok")
        progress_cb(total - failed, failed, total)
    return results
