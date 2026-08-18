"""On-disk checkpoint so crash / restart doesn't lose enriched rows."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict
from typing import Dict

from scraper import TweetMetrics

log = logging.getLogger(__name__)

CHECKPOINT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".checkpoint.json"
)


def _load() -> dict:
    if not os.path.exists(CHECKPOINT_PATH):
        return {"twitter": {}, "facebook": {}}
    try:
        with open(CHECKPOINT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("twitter", {})
        data.setdefault("facebook", {})
        return data
    except Exception as exc:  # noqa: BLE001
        log.warning("checkpoint unreadable, starting fresh: %s", exc)
        return {"twitter": {}, "facebook": {}}


def _atomic_write(data: dict) -> None:
    dir_ = os.path.dirname(CHECKPOINT_PATH)
    fd, tmp_path = tempfile.mkstemp(prefix=".checkpoint_", dir=dir_)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, CHECKPOINT_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def load_platform(platform: str) -> Dict[str, TweetMetrics]:
    """Return {id: TweetMetrics} previously saved for this platform."""
    data = _load()
    raw = data.get(platform, {}) or {}
    out: Dict[str, TweetMetrics] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        try:
            out[k] = TweetMetrics(**v)
        except TypeError:
            continue
    return out


def save_platform(platform: str, metrics: Dict[str, TweetMetrics]) -> None:
    """Merge new results into the on-disk checkpoint for this platform."""
    if not metrics:
        return
    data = _load()
    bucket = data.setdefault(platform, {})
    for k, m in metrics.items():
        bucket[k] = asdict(m)
    _atomic_write(data)


def clear() -> None:
    """Delete the checkpoint file — call when starting a truly fresh run."""
    if os.path.exists(CHECKPOINT_PATH):
        os.remove(CHECKPOINT_PATH)


def stats() -> dict:
    data = _load()
    return {
        "twitter": len(data.get("twitter", {})),
        "facebook": len(data.get("facebook", {})),
        "path": CHECKPOINT_PATH,
        "exists": os.path.exists(CHECKPOINT_PATH),
    }
