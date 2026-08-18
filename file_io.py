"""Read the uploaded file, merge metrics, and write enriched outputs."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Dict, List

import pandas as pd

from scraper import TweetMetrics
from url_utils import PostRef, parse_post_url

METRIC_COLUMNS = ["Views", "Likes", "Comments", "Retweets"]
PLATFORM_COLUMN = "Platform"
STATUS_COLUMN = "Status"
NOTE_COLUMN = "Note"


@dataclass
class PreparedInput:
    df: pd.DataFrame                                      # original DF, row order preserved
    url_col: str                                           # detected URL column name
    parsed: List[PostRef | None]                           # per-row parsed URL (None if invalid)
    duplicates: List[int]                                  # row indices marked as duplicates
    invalid: List[int]                                     # row indices marked as invalid
    unique_by_platform: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # unique_by_platform["twitter"] = {tweet_id: canonical_url}
    # unique_by_platform["facebook"] = {post_id: canonical_url}


def _detect_url_column(df: pd.DataFrame) -> str:
    for name in df.columns:
        if str(name).strip().lower() in {"url", "urls", "link", "post url", "post_url",
                                          "tweet url", "tweet_url"}:
            return name
    raise ValueError(
        "No URL column found. Please include a column named 'URL' (or 'url'/'link')."
    )


def read_input(file_bytes: bytes, filename: str) -> pd.DataFrame:
    name = filename.lower()
    buf = io.BytesIO(file_bytes)
    if name.endswith(".csv"):
        return pd.read_csv(buf)
    if name.endswith((".xlsx", ".xlsm")):
        return pd.read_excel(buf, engine="openpyxl")
    raise ValueError("Unsupported file type. Upload a .csv or .xlsx file.")


def prepare(df: pd.DataFrame) -> PreparedInput:
    url_col = _detect_url_column(df)
    parsed: List[PostRef | None] = []
    seen: Dict[str, int] = {}   # platform-qualified id -> first row idx
    duplicates: List[int] = []
    invalid: List[int] = []
    unique: Dict[str, Dict[str, str]] = {}

    for idx, raw in enumerate(df[url_col].tolist()):
        ref = parse_post_url(raw)
        parsed.append(ref)
        if ref is None:
            invalid.append(idx)
            continue
        key = f"{ref.platform}:{ref.post_id}"
        if key in seen:
            duplicates.append(idx)
            continue
        seen[key] = idx
        unique.setdefault(ref.platform, {})[ref.post_id] = ref.canonical_url

    return PreparedInput(
        df=df, url_col=url_col, parsed=parsed,
        duplicates=duplicates, invalid=invalid,
        unique_by_platform=unique,
    )


def merge_metrics(
    prep: PreparedInput,
    metrics_by_platform: Dict[str, Dict[str, TweetMetrics]],
) -> pd.DataFrame:
    """Append platform/metric/status columns preserving original row order."""
    out = prep.df.copy()

    out[PLATFORM_COLUMN] = ""
    for col in METRIC_COLUMNS:
        out[col] = pd.Series([pd.NA] * len(out), dtype="Int64")
    out[STATUS_COLUMN] = ""
    out[NOTE_COLUMN] = ""

    for idx, ref in enumerate(prep.parsed):
        if ref is None:
            out.at[idx, STATUS_COLUMN] = "invalid_url"
            out.at[idx, NOTE_COLUMN] = "URL is not a recognizable Twitter/X or Facebook post URL"
            continue

        out.at[idx, PLATFORM_COLUMN] = ref.platform
        m = (metrics_by_platform.get(ref.platform) or {}).get(ref.post_id)

        if m is None:
            out.at[idx, STATUS_COLUMN] = "no_data"
            out.at[idx, NOTE_COLUMN] = "Actor did not return this post"
            continue

        if m.status == "ok":
            if m.views is not None:
                out.at[idx, "Views"] = m.views
            if m.likes is not None:
                out.at[idx, "Likes"] = m.likes
            if m.replies is not None:
                out.at[idx, "Comments"] = m.replies
            if m.retweets is not None:
                out.at[idx, "Retweets"] = m.retweets
            if idx in prep.duplicates:
                out.at[idx, STATUS_COLUMN] = "duplicate"
                out.at[idx, NOTE_COLUMN] = "duplicate URL — metrics copied from first occurrence"
            else:
                out.at[idx, STATUS_COLUMN] = "ok"
        else:
            out.at[idx, STATUS_COLUMN] = m.status
            out.at[idx, NOTE_COLUMN] = m.reason

    return out


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="engagement")
    return buf.getvalue()


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")
