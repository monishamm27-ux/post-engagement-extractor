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
    """Return the actor's own post_id — matched later against our reverse index.

    Kept for backward compatibility; new code should prefer _extract_id_candidates
    which returns every plausible identifier the item exposes.
    """
    cands = _extract_id_candidates(item)
    return cands[0] if cands else None


def _extract_id_candidates(item: dict) -> list:
    """Return every plausible identifier the item exposes, in preference order.

    Different actors label the ID differently and sometimes return only a URL.
    We collect all candidates so the reverse-index lookup can try each one and
    match on whichever the caller's tail_to_key happens to know about.
    """
    cands: list = []
    seen: set = set()

    def _add(val) -> None:
        if val is None:
            return
        s = str(val).strip()
        if not s or s in seen:
            return
        seen.add(s)
        cands.append(s)

    for k in _ID_KEYS:
        _add(item.get(k))

    # URL-derived candidates — the apify/facebook-posts-scraper actor often
    # returns items whose only stable link back to our input is the URL field.
    from url_utils import parse_post_url  # local import avoids cycles
    for k in _URL_KEYS:
        u = item.get(k)
        if isinstance(u, str):
            ref = parse_post_url(u)
            if ref and ref.platform == "facebook":
                _add(ref.post_id)
    return cands


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
    # Build a reverse index: raw post_id -> our composite key.
    # Composite keys look like `{page}:{post}` or a plain string for other URL shapes.
    # Different Apify actors return the ID in wildly different formats:
    #   - just the post tail:     "1094243529784620"
    #   - underscore composite:   "100933695690337_1094243529784620"
    #   - colon composite (URL):  "100933695690337:1094243529784620"
    # Register every reasonable alias so any of them find the right key.
    tail_to_key: Dict[str, str] = {}
    for key in remaining_keys:
        tail_to_key[key] = key                     # full composite (colon form)
        if ":" in key:
            page, post = key.rsplit(":", 1)
            tail_to_key[post] = key                # bare post segment (may still contain underscore)
            tail_to_key[f"{page}_{post}"] = key    # underscore composite of the whole thing
            # If the post segment itself is an underscore-composite (e.g.
            # `/user/posts/{page_id}_{post_id}` inputs), also register the trailing
            # numeric segment — that's the shape most FB actors return in `post_id`.
            if "_" in post:
                tail_to_key[post.rsplit("_", 1)[-1]] = key

    run_input = actor.build_input(urls)
    log.info("Calling FB actor %s with %d urls", actor.label, len(urls))
    run = client.actor(actor.actor_id).call(run_input=run_input)
    if not run or not run.get("defaultDatasetId"):
        raise RuntimeError(f"FB actor {actor.label} returned no dataset")

    out: Dict[str, TweetMetrics] = {}
    unmatched = 0
    first_item_logged = False
    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        if not first_item_logged:
            # Dump the first item's shape so we can see exactly what the actor
            # returned — invaluable when matching silently produces 0 rows.
            log.warning("FB DEBUG [%s] first item keys: %s",
                        actor.label, sorted(item.keys()))
            log.warning("FB DEBUG [%s] first item id-ish fields: %s",
                        actor.label,
                        {k: item.get(k) for k in
                         ("post_id", "postId", "topLevelPostId", "id",
                          "story_fbid", "storyFbid", "url", "postUrl",
                          "topLevelUrl", "permalink")
                         if item.get(k) is not None})
            first_item_logged = True
        candidates = _extract_id_candidates(item)
        if not candidates:
            unmatched += 1
            continue
        composite = None
        for raw in candidates:
            composite = tail_to_key.get(raw)
            if composite:
                break
            if "_" in raw:
                composite = tail_to_key.get(raw.rsplit("_", 1)[-1])
                if composite:
                    break
            if ":" in raw:
                composite = tail_to_key.get(raw.rsplit(":", 1)[-1])
                if composite:
                    break
        if not composite:
            unmatched += 1
            continue
        out[composite] = _item_to_metrics(item)
    if unmatched:
        log.info("FB actor %s: %d items didn't match any input URL", actor.label, unmatched)
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
