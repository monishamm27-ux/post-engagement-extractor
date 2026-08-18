"""Streamlit UI for the multi-platform Post Engagement Extractor."""
from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

from config import FACEBOOK_ACTORS, TWITTER_ACTORS, settings
from checkpoint import clear as clear_checkpoint, stats as checkpoint_stats
from file_io import (
    merge_metrics,
    prepare,
    read_input,
    to_csv_bytes,
    to_excel_bytes,
)
from router import fetch_all_metrics

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

st.set_page_config(page_title="Post Engagement Extractor", page_icon="📊", layout="centered")

# Password gate (no-op when APP_PASSWORD isn't set — local dev stays frictionless).
from auth import require_password
require_password()

st.title("Post Engagement Extractor")
st.caption(
    "Upload a CSV or Excel of Twitter/X or Facebook post URLs. The app detects "
    "the platform per row, fetches public engagement metrics via Apify, and "
    "returns an enriched file."
)

with st.sidebar:
    st.subheader("Configuration")
    if settings.has_token:
        st.success("Apify token detected")
    else:
        st.error("Apify token missing")
        st.markdown(
            "Create a `.env` file next to `app.py` containing:\n\n"
            "```\nAPIFY_API_TOKEN=your_token_here\n```"
        )
    st.subheader("Twitter/X actor fallback")
    for i, a in enumerate(TWITTER_ACTORS, start=1):
        st.markdown(f"{i}. `{a.label}`")
    st.subheader("Facebook actor fallback")
    for i, a in enumerate(FACEBOOK_ACTORS, start=1):
        st.markdown(f"{i}. `{a.label}`")

    st.subheader("Checkpoint")
    cps = checkpoint_stats()
    if cps["exists"]:
        st.info(
            f"💾 Cached: **{cps['twitter']}** Twitter + **{cps['facebook']}** Facebook "
            "posts.\n\nA re-run of the same URLs will skip these and pay nothing to fetch them again."
        )
        if st.button("Reset checkpoint"):
            clear_checkpoint()
            st.rerun()
    else:
        st.caption("No cached results yet. Successful fetches persist automatically.")

uploaded = st.file_uploader("Upload .xlsx or .csv", type=["xlsx", "csv"])

if uploaded is None:
    st.info("Upload a file with a **URL** column to begin. Twitter/X and Facebook URLs are supported.")
    st.stop()

# --- Read & validate input ---------------------------------------------------
try:
    df = read_input(uploaded.getvalue(), uploaded.name)
    prep = prepare(df)
except Exception as exc:
    st.error(f"Could not read file: {exc}")
    st.stop()

tw_count = len(prep.unique_by_platform.get("twitter", {}))
fb_count = len(prep.unique_by_platform.get("facebook", {}))
unique_total = tw_count + fb_count

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total rows", len(prep.df))
c2.metric("Twitter/X", tw_count)
c3.metric("Facebook", fb_count)
c4.metric("Duplicates", len(prep.duplicates))
c5.metric("Invalid", len(prep.invalid))

with st.expander("Preview input"):
    st.dataframe(prep.df.head(20), use_container_width=True)

if unique_total == 0:
    st.warning("No supported post URLs (Twitter/X or Facebook) found in this file.")
    st.stop()

# --- Run extraction ----------------------------------------------------------
if "result_df" not in st.session_state:
    st.session_state.result_df = None

run = st.button("Fetch engagement metrics", type="primary", disabled=not settings.has_token)

if run:
    total = unique_total
    progress = st.progress(0.0, text=f"Starting — 0 / {total}")
    status_line = st.empty()

    def _on_progress(completed: int, failed: int, total_: int) -> None:
        pct = (completed + failed) / max(total_, 1)
        progress.progress(min(pct, 1.0), text=f"{completed + failed} / {total_}")
        status_line.info(
            f"✅ Completed: {completed}   ❌ Failed: {failed}   Total: {total_}"
        )

    try:
        with st.spinner("Calling Apify… this can take a minute or two."):
            metrics_by_platform = fetch_all_metrics(
                prep.unique_by_platform, progress_cb=_on_progress
            )
        result = merge_metrics(prep, metrics_by_platform)
        st.session_state.result_df = result
        progress.progress(1.0, text="Done")
        ok = int((result["Status"] == "ok").sum())
        failed = len(result) - ok
        status_line.success(f"Finished — {ok} enriched rows, {failed} not enriched.")
    except Exception as exc:
        st.error(f"Extraction failed: {exc}")

# --- Show & download ---------------------------------------------------------
result_df: pd.DataFrame | None = st.session_state.result_df
if result_df is not None:
    st.subheader("Enriched results")
    st.dataframe(result_df, use_container_width=True)

    base = uploaded.name.rsplit(".", 1)[0]
    dl_a, dl_b = st.columns(2)
    dl_a.download_button(
        "⬇️ Download Excel",
        data=to_excel_bytes(result_df),
        file_name=f"{base}_enriched.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    dl_b.download_button(
        "⬇️ Download CSV",
        data=to_csv_bytes(result_df),
        file_name=f"{base}_enriched.csv",
        mime="text/csv",
    )

    with st.expander("Rows that could not be enriched"):
        problems = result_df[result_df["Status"] != "ok"][
            [prep.url_col, "Platform", "Status", "Note"]
        ]
        st.dataframe(problems, use_container_width=True)
