"""Simple password gate for the app.

Reads the expected password from:
  1. env var  `APP_PASSWORD`
  2. Streamlit secret  `APP_PASSWORD`

If neither is set, the gate is disabled (useful for local dev — you're the
only user). On Streamlit Cloud, set the secret to enable the gate.
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

import streamlit as st


def _expected_password() -> Optional[str]:
    tok = os.getenv("APP_PASSWORD", "")
    if tok:
        return tok
    try:
        return st.secrets.get("APP_PASSWORD", None) or None
    except Exception:  # noqa: BLE001
        return None


def require_password() -> bool:
    """Show a login form and block the app until the correct password is entered.

    Returns True when the caller should proceed (either authed or gate disabled).
    """
    expected = _expected_password()
    if not expected:
        # No password configured — gate disabled.
        return True

    if st.session_state.get("_auth_ok"):
        return True

    st.markdown("### 🔒 Access required")
    st.caption("This tool consumes shared Apify credit. Ask Monisha for the password.")
    pw = st.text_input("Password", type="password", key="_auth_pw")
    if st.button("Sign in"):
        if pw and hmac.compare_digest(pw, expected):
            st.session_state["_auth_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()
    return False  # unreachable; st.stop() ends the run
