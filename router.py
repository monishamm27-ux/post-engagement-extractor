"""Platform-aware metric fetching. Splits URLs by platform and merges results."""
from __future__ import annotations

from typing import Callable, Dict, Optional

from fb_scraper import fetch_facebook_metrics
from scraper import TweetMetrics, fetch_metrics as fetch_twitter_metrics


def fetch_all_metrics(
    urls_by_platform_and_id: Dict[str, Dict[str, str]],
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
) -> Dict[str, Dict[str, TweetMetrics]]:
    """Route URLs to the right platform fetcher.

    Args:
        urls_by_platform_and_id: {"twitter": {tweet_id: url}, "facebook": {post_id: url}}
        progress_cb: called as (completed, failed, total) — aggregated across platforms

    Returns:
        {"twitter": {id: TweetMetrics}, "facebook": {id: TweetMetrics}}
    """
    total = sum(len(v) for v in urls_by_platform_and_id.values())
    completed_running = 0
    failed_running = 0

    def _wrap(platform_total: int):
        def cb(done: int, failed: int, _t: int) -> None:
            nonlocal completed_running, failed_running
            if progress_cb:
                # Report aggregate progress across all platforms.
                progress_cb(completed_running + done, failed_running + failed, total)
        return cb

    results: Dict[str, Dict[str, TweetMetrics]] = {}

    twitter_urls = urls_by_platform_and_id.get("twitter") or {}
    if twitter_urls:
        results["twitter"] = fetch_twitter_metrics(twitter_urls, progress_cb=_wrap(len(twitter_urls)))
        completed_running += sum(1 for m in results["twitter"].values() if m.status == "ok")
        failed_running += sum(1 for m in results["twitter"].values() if m.status != "ok")

    fb_urls = urls_by_platform_and_id.get("facebook") or {}
    if fb_urls:
        results["facebook"] = fetch_facebook_metrics(fb_urls, progress_cb=_wrap(len(fb_urls)))
        completed_running += sum(1 for m in results["facebook"].values() if m.status == "ok")
        failed_running += sum(1 for m in results["facebook"].values() if m.status != "ok")

    if progress_cb:
        progress_cb(completed_running, failed_running, total)

    return results
