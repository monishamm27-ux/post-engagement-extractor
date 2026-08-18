"""Apify-backed tweet metric fetching with automatic actor fallback."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

from apify_client import ApifyClient

from config import ApifyActor, DEFAULT_ACTORS, settings

log = logging.getLogger(__name__)


@dataclass
class TweetMetrics:
    views: Optional[int] = None
    likes: Optional[int] = None
    replies: Optional[int] = None
    retweets: Optional[int] = None
    status: str = "ok"          # ok | not_found | error
    reason: str = ""             # human-readable note when not ok


# Keys that different Apify actors use for the same fields.
_VIEW_KEYS = ("viewCount", "views", "view_count", "impressions", "impression_count")
_LIKE_KEYS = ("likeCount", "likes", "favorite_count", "favoriteCount", "favouriteCount")
_REPLY_KEYS = ("replyCount", "replies", "reply_count", "conversationCount")
_RT_KEYS = ("retweetCount", "retweets", "repost_count", "retweet_count", "quoteCount")
_URL_KEYS = ("url", "twitterUrl", "tweetUrl", "permanentUrl", "link")
_ID_KEYS = ("id", "id_str", "tweetId", "conversationId")


def _first_int(item: dict, keys: Iterable[str]) -> Optional[int]:
    for k in keys:
        if k in item and item[k] is not None:
            try:
                return int(item[k])
            except (TypeError, ValueError):
                continue
    # Nested "public_metrics" (Twitter v2 style) sometimes returned by actors.
    metrics = item.get("public_metrics") or item.get("stats") or {}
    if isinstance(metrics, dict):
        for k in keys:
            if k in metrics and metrics[k] is not None:
                try:
                    return int(metrics[k])
                except (TypeError, ValueError):
                    continue
    return None


def _candidate_ids(item: dict) -> List[str]:
    """Return every plausible tweet ID this item could correspond to.

    Different actors set the tweet id under different keys and sometimes the
    canonical `id` is the *reply target* id, not the tweet we asked for.
    We collect all candidates so the reverse-lookup can match on any of them.
    """
    ids: List[str] = []
    for k in _ID_KEYS:
        v = item.get(k)
        if v is not None:
            ids.append(str(v))
    for k in _URL_KEYS:
        url = item.get(k)
        if isinstance(url, str) and "/status/" in url:
            tail = url.split("/status/", 1)[1]
            ids.append(tail.split("?", 1)[0].split("/", 1)[0])
    # dedupe, preserving order
    seen = set()
    return [x for x in ids if not (x in seen or seen.add(x))]


def _is_placeholder(item: dict) -> bool:
    """Some actors emit paywall/mock placeholders instead of real data."""
    if item.get("noResults") is True:
        return True
    if item.get("type") == "mock_tweet":
        return True
    if item.get("demo") is not None and len(item) <= 2:
        return True
    return False


def _item_to_metrics(item: dict) -> TweetMetrics:
    # Detect "unavailable" markers some actors emit.
    err = item.get("error") or item.get("errorMessage")
    if err:
        return TweetMetrics(status="not_found", reason=str(err))

    return TweetMetrics(
        views=_first_int(item, _VIEW_KEYS),
        likes=_first_int(item, _LIKE_KEYS),
        replies=_first_int(item, _REPLY_KEYS),
        retweets=_first_int(item, _RT_KEYS),
        status="ok",
    )


def _run_actor(
    client: ApifyClient,
    actor: ApifyActor,
    urls: List[str],
    expected_ids: Optional[set] = None,
    timeout_secs: int = 600,
) -> Dict[str, TweetMetrics]:
    """Run one actor over the URLs; return a {tweet_id: TweetMetrics} map.

    `expected_ids` is the set of tweet IDs we're still trying to fetch. When
    provided, each returned item is checked against every candidate ID it
    exposes (id, id_str, conversationId, URL tail…) so a returned item that
    reports the tweet under any of those matches back correctly.
    """
    run_input = actor.build_input(urls)
    log.info("Calling actor %s with %d urls", actor.label, len(urls))
    # Don't pass timeout_secs — its arg name differs across apify-client versions;
    # the actor's own default timeout is sane for our workload.
    run = client.actor(actor.actor_id).call(run_input=run_input)
    if not run or not run.get("defaultDatasetId"):
        raise RuntimeError(f"actor {actor.label} returned no dataset")

    out: Dict[str, TweetMetrics] = {}
    placeholder_count = 0
    total_items = 0
    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        total_items += 1
        if _is_placeholder(item):
            placeholder_count += 1
            continue
        candidates = _candidate_ids(item)
        if not candidates:
            continue
        # Prefer a candidate that actually matches an expected id.
        chosen = None
        if expected_ids:
            for cid in candidates:
                if cid in expected_ids:
                    chosen = cid
                    break
        if chosen is None:
            chosen = candidates[0]
        out[chosen] = _item_to_metrics(item)

    # Every returned item was a paywall/mock placeholder — treat as failure so
    # the next actor is tried.
    if total_items > 0 and placeholder_count == total_items:
        raise RuntimeError(
            f"actor {actor.label} returned only placeholder/paywall items "
            f"(count={placeholder_count}); likely plan-restricted"
        )
    return out


def fetch_metrics(
    urls_by_id: Dict[str, str],
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
    actors: Optional[List[ApifyActor]] = None,
) -> Dict[str, TweetMetrics]:
    """Fetch metrics for a batch of canonical tweet URLs.

    Args:
        urls_by_id: {tweet_id: canonical_url} of tweets we still need metrics for.
        progress_cb: called as (completed, failed, total) after each actor run.
        actors: override the default actor list (mainly for tests).

    Returns:
        {tweet_id: TweetMetrics}. Missing IDs → mark not_found by caller.
    """
    if not settings.has_token:
        raise RuntimeError(
            "APIFY_API_TOKEN is not set. Add it to a `.env` file or your environment."
        )

    actors = actors or DEFAULT_ACTORS
    client = ApifyClient(settings.apify_token)

    # Import here to avoid a hard cycle with a top-level import.
    from checkpoint import load_platform, save_platform

    # Resume: pull previously-saved metrics for any ids we've already fetched.
    cached = load_platform("twitter")
    results: Dict[str, TweetMetrics] = {
        k: cached[k] for k in urls_by_id if k in cached and cached[k].status == "ok"
    }
    remaining: Dict[str, str] = {k: v for k, v in urls_by_id.items() if k not in results}
    total = len(urls_by_id)
    last_error: Optional[str] = None
    if results:
        log.info("Resumed from checkpoint: %d/%d twitter tweets already have data",
                 len(results), total)
        if progress_cb:
            progress_cb(len(results), 0, total)

    for actor in actors:
        if not remaining:
            break
        try:
            batch_result = _run_actor(
                client=client,
                actor=actor,
                urls=list(remaining.values()),
                expected_ids=set(remaining.keys()),
                timeout_secs=settings.actor_run_timeout_secs,
            )
        except Exception as exc:  # noqa: BLE001 — bubble up as fallback trigger
            last_error = f"{actor.label}: {exc}"
            log.warning("Actor %s failed: %s", actor.label, exc)
            if progress_cb:
                progress_cb(len(results), total - len(results) - len(remaining), total)
            continue

        just_added: Dict[str, TweetMetrics] = {}
        for tid, metrics in batch_result.items():
            if tid in remaining:
                results[tid] = metrics
                just_added[tid] = metrics
                remaining.pop(tid, None)

        save_platform("twitter", just_added)  # persist after every actor pass

        if progress_cb:
            progress_cb(len(results), 0, total)

    # --- Retry pass ---------------------------------------------------------
    # For anything the batch phase didn't return, try each actor again on a
    # much smaller group (5 URLs at a time). Rate-limits and mid-batch failures
    # commonly resolve when the request set is small.
    if remaining:
        log.info("Retry pass for %d stragglers, small batches", len(remaining))
        for actor in actors:
            if not remaining:
                break
            straggler_ids = list(remaining.keys())
            for i in range(0, len(straggler_ids), 5):
                chunk_ids = straggler_ids[i : i + 5]
                chunk_urls = [remaining[k] for k in chunk_ids]
                try:
                    r = _run_actor(
                        client=client, actor=actor,
                        urls=chunk_urls, expected_ids=set(chunk_ids),
                        timeout_secs=settings.actor_run_timeout_secs,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{actor.label} (retry): {exc}"
                    log.warning("Retry on %s failed: %s", actor.label, exc)
                    continue
                just_added2: Dict[str, TweetMetrics] = {}
                for tid, m in r.items():
                    if tid in remaining:
                        results[tid] = m
                        just_added2[tid] = m
                        remaining.pop(tid, None)
                save_platform("twitter", just_added2)  # persist each retry batch
                if progress_cb:
                    progress_cb(len(results), 0, total)

    # Anything still remaining after every actor + retry was tried is unavailable.
    for tid in remaining:
        results[tid] = TweetMetrics(
            status="not_found",
            reason=last_error or "tweet not returned by any actor (deleted/private/unavailable)",
        )

    if progress_cb:
        failed = sum(1 for m in results.values() if m.status != "ok")
        progress_cb(total - failed, failed, total)

    return results
