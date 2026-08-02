"""CTF Agent Dashboard — Streamlit web interface.

Provides real-time challenge status, solver trace viewing,
human intervention controls, and global settings management.

Usage:
    streamlit run dashboard/app.py
    # or via main.py: python main.py --dashboard
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
import streamlit as st
import yaml

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CTF Agent Dashboard",
    page_icon="🏴",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Constants ────────────────────────────────────────────────────────────────
LOG_DIR = Path("logs")
CHALLENGES_DIR = Path("challenges")
CONFIG_PATH = Path("config.yaml")

# Cache TTL in seconds
CACHE_TTL = 2  # Match auto-refresh interval

# Coordinator message endpoint
COORDINATOR_HOST = os.environ.get("COORDINATOR_HOST", "127.0.0.1")
COORDINATOR_MSG_PORT = int(os.environ.get("COORDINATOR_MSG_PORT", "9400"))

STATUS_COLORS: dict[str, str] = {
    "new": "#6b7280",
    "analyzing": "#3b82f6",
    "exploiting": "#f59e0b",
    "submitted": "#8b5cf6",
    "solved": "#10b981",
    "failed": "#ef4444",
    "needs_human": "#ec4899",
    "cancelled": "#9ca3af",
    "flag_found": "#10b981",
    "gave_up": "#ef4444",
    "error": "#dc2626",
}

CATEGORY_ICONS: dict[str, str] = {
    "rev": "🔧",
    "pwn": "💥",
    "web": "🌐",
    "crypto": "🔐",
    "misc": "🎲",
    "forensics": "🔍",
    "osint": "🕵️",
}

# ── Helpers ──────────────────────────────────────────────────────────────────


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _load_config() -> dict[str, Any]:
    """Load config.yaml with defaults."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _get_trace_files() -> list[tuple[str, float]]:
    """Get all solver trace JSONL file paths and mtimes, newest first."""
    if not LOG_DIR.exists():
        return []
    files = sorted(LOG_DIR.glob("trace-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [(str(f), f.stat().st_mtime) for f in files]


def _get_trace_path(index: int = 0) -> Path | None:
    """Get the nth trace file path from cache."""
    files = _get_trace_files()
    if not files:
        return None
    return Path(files[index][0])


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _parse_trace_file(path_str: str) -> list[dict[str, Any]]:
    """Parse a JSONL trace file into a list of events."""
    events: list[dict[str, Any]] = []
    try:
        for line in Path(path_str).read_text().strip().split("\n"):
            if line.strip():
                events.append(json.loads(line))
    except (json.JSONDecodeError, FileNotFoundError):
        pass
    return events


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _get_challenge_metas() -> dict[str, dict[str, Any]]:
    """Load all challenge metadata from challenges/ directory."""
    metas: dict[str, dict[str, Any]] = {}
    if not CHALLENGES_DIR.exists():
        return metas
    for d in CHALLENGES_DIR.iterdir():
        meta_path = d / "metadata.yml"
        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    data = yaml.safe_load(f) or {}
                    metas[data.get("name", d.name)] = {
                        "name": data.get("name", d.name),
                        "category": data.get("category", ""),
                        "value": data.get("value", 0),
                        "solves": data.get("solves", 0),
                        "description": data.get("description", "")[:200],
                        "dir": str(d),
                    }
            except Exception:
                pass
    return metas


def _send_operator_message(message: str, timeout: float = 2.0) -> bool:
    """Send a message to the running coordinator via HTTP."""
    try:
        body = json.dumps({"message": message}).encode()
        req = httpx.Request(
            "POST",
            f"http://{COORDINATOR_HOST}:{COORDINATOR_MSG_PORT}/msg",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        # NOTE: httpx.Client.send() takes NO timeout kwarg — set it on the Client.
        with httpx.Client(timeout=timeout) as client:
            resp = client.send(req)
            return resp.status_code == 200
    except Exception:
        return False


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _check_coordinator() -> bool:
    """Check coordinator connectivity (cached to avoid blocking every render)."""
    # Short timeout: connection refused fails instantly; only a genuinely
    # starting engine hits the timeout, and that's capped at 0.5s.
    return _send_operator_message("ping", timeout=0.5)


# ── Sidebar ──────────────────────────────────────────────────────────────────

def _render_sidebar() -> None:
    """Render the sidebar with global controls."""
    st.sidebar.title("🏴 CTF Agent")
    st.sidebar.caption("Human-Machine Collaboration Dashboard")

    # Coordinator connection status (cached — doesn't block every render)
    st.sidebar.subheader("🔗 Coordinator")
    if _check_coordinator():
        st.sidebar.success("✅ Connected")
    else:
        st.sidebar.warning("⚠️ Not connected")
        st.sidebar.info("Start the coordinator first: `python main.py`")

    st.sidebar.divider()

    # Global settings
    st.sidebar.subheader("⚙️ Settings")
    config = _load_config()

    max_concurrent = st.sidebar.slider(
        "Max Concurrent Challenges",
        min_value=1, max_value=50, value=config.get("solver", {}).get("max_concurrent_challenges", 10),
        help="Maximum number of challenges solved simultaneously",
    )
    max_attempts = st.sidebar.slider(
        "Max Attempts Per Challenge",
        min_value=1, max_value=10, value=config.get("solver", {}).get("max_attempts_per_challenge", 3),
    )
    rate_limit = st.sidebar.slider(
        "API Rate Limit (RPS)",
        min_value=1, max_value=50, value=config.get("rate_limit", {}).get("requests_per_second", 5),
    )

    if st.sidebar.button("Apply Settings", type="primary", use_container_width=True):
        msg = (
            f"SETTINGS max_concurrent={max_concurrent} "
            f"max_attempts={max_attempts} rate_limit={rate_limit}"
        )
        if _send_operator_message(msg):
            st.sidebar.success("Settings sent to coordinator!")
        else:
            st.sidebar.error("Coordinator not reachable")

    st.sidebar.divider()

    # Manual refresh
    if st.sidebar.button("🔄 Refresh", use_container_width=True):
        st.rerun()

    # Auto-refresh info
    st.sidebar.info("Auto-refreshes every 3 seconds")


# ── Challenge cards ──────────────────────────────────────────────────────────

def _get_status_from_traces(trace_events: list[dict]) -> str:
    """Infer current status from trace events."""
    if not trace_events:
        return "new"
    for event in reversed(trace_events):
        ev_type = event.get("type", "")
        if ev_type == "flag_confirmed":
            return "solved"
        if ev_type in ("finish",):
            data = json.dumps(event)
            if "flag_found" in data:
                return "solved"
            if "gave_up" in data or "GAVE_UP" in data:
                return "gave_up"
        if ev_type == "error":
            return "error"
        if ev_type == "start":
            return "analyzing"
        if ev_type == "tool_call":
            return "exploiting"
    return "analyzing"


def _render_challenge_card(name: str, meta: dict[str, Any], status: str, trace_count: int) -> None:
    """Render a single challenge card."""
    category = meta.get("category", "unknown").lower()
    icon = CATEGORY_ICONS.get(category, "📦")
    color = STATUS_COLORS.get(status, "#6b7280")

    with st.container(border=True):
        cols = st.columns([1, 3, 1, 1, 1])

        with cols[0]:
            st.markdown(f"<h2 style='margin:0'>{icon}</h2>", unsafe_allow_html=True)

        with cols[1]:
            st.markdown(f"**{name}**")
            st.caption(f"{category.capitalize()} · {meta.get('value', '?')} pts")

        with cols[2]:
            st.markdown(
                f"<div style='background:{color}; color:white; padding:2px 8px; "
                f"border-radius:12px; text-align:center; font-size:0.8em'>"
                f"{status.upper()}</div>",
                unsafe_allow_html=True,
            )

        with cols[3]:
            if trace_count > 0:
                st.markdown(f"**{trace_count}** steps")
            else:
                st.markdown("—")

        with cols[4]:
            if st.button("View", key=f"view_{name}", use_container_width=True):
                st.session_state.selected_challenge = name
                st.rerun()


# ── Challenge detail page ────────────────────────────────────────────────────

def _render_challenge_detail(name: str, meta: dict[str, Any]) -> None:
    """Render the detail view for a single challenge."""
    st.subheader(f"📋 {name}")
    st.caption(f"{meta.get('category', '?')} · {meta.get('value', '?')} pts")

    # Challenge info
    with st.expander("Challenge Description", expanded=False):
        st.markdown(meta.get("description", "_No description_") or "_No description_")

    # Find trace files for this challenge
    trace_files = sorted(
        LOG_DIR.glob(f"trace-{name.replace('/', '_')}-*.jsonl"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )

    if not trace_files:
        st.info("No solver trace data available yet.")
        return

    # Solver traces
    for tf in trace_files:
        model_id = tf.stem.replace(f"trace-{name.replace('/', '_')}-", "").rsplit("-", 1)[0]
        events = _parse_trace_file(str(tf))

        with st.expander(f"🤖 {model_id} ({len(events)} events)", expanded=True):
            # Summary stats
            tool_calls = [e for e in events if e.get("type") == "tool_call"]
            tool_results = [e for e in events if e.get("type") == "tool_result"]
            usage_events = [e for e in events if e.get("type") == "usage"]

            cols = st.columns(4)
            cols[0].metric("Tool Calls", len(tool_calls))
            cols[1].metric("Results", len(tool_results))
            if usage_events:
                total_cost = sum(u.get("cost_usd", 0) for u in usage_events)
                total_in = sum(u.get("input_tokens", 0) for u in usage_events)
                cols[2].metric("Cost", f"${total_cost:.4f}")
                cols[3].metric("Input Tokens", f"{total_in:,}")
            else:
                cols[2].metric("Cost", "—")
                cols[3].metric("Tokens", "—")

            # Event log (filtered)
            st.markdown("#### Recent Activity")
            event_container = st.container(height=400, border=True)
            with event_container:
                for event in events[-50:]:  # Last 50 events
                    ev_type = event.get("type", "?")
                    step = event.get("step", "")
                    ts = event.get("ts", 0)
                    time_str = time.strftime("%H:%M:%S", time.localtime(ts))

                    if ev_type == "tool_call":
                        tool = event.get("tool", "?")
                        args = str(event.get("args", ""))[:120]
                        st.text(f"[{time_str}] ⚡ Step {step}: {tool}({args})")
                    elif ev_type == "tool_result":
                        tool = event.get("tool", "?")
                        result = str(event.get("result", ""))[:120]
                        st.text(f"[{time_str}] ✅ {tool} → {result}")
                    elif ev_type == "flag_confirmed":
                        st.success(f"[{time_str}] 🚩 FLAG CONFIRMED at step {step}!")
                    elif ev_type == "loop_break":
                        st.warning(f"[{time_str}] 🔄 Loop detected and broken at step {step}")
                    elif ev_type == "error":
                        st.error(f"[{time_str}] ❌ Error: {str(event.get('error', ''))[:200]}")
                    elif ev_type == "usage":
                        st.caption(
                            f"[{time_str}] 💰 in={event.get('input_tokens',0)} "
                            f"out={event.get('output_tokens',0)} "
                            f"cached={event.get('cache_read_tokens',0)} "
                            f"cost=${event.get('cost_usd',0):.6f}"
                        )
                    elif ev_type == "findings_injected":
                        st.info(f"[{time_str}] 📨 Cross-solver findings injected")
                    elif ev_type == "bump":
                        st.info(f"[{time_str}] 📨 Coordinator bump: {str(event.get('insights',''))[:100]}")
                    elif ev_type == "start":
                        st.text(f"[{time_str}] 🚀 Solver started")
                    elif ev_type == "model_response":
                        text = str(event.get("text", ""))[:100]
                        st.text(f"[{time_str}] 💬 Model: {text}")

    # Human intervention
    st.divider()
    st.subheader("🫵 Human Intervention")

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("⏸️ Pause", key=f"pause_{name}", use_container_width=True):
            _send_operator_message(f"PAUSE {name}")
            st.success(f"Pause signal sent for {name}")

    with col2:
        if st.button("▶️ Resume", key=f"resume_{name}", use_container_width=True):
            _send_operator_message(f"RESUME {name}")
            st.success(f"Resume signal sent for {name}")

    with col3:
        if st.button("⏹️ Kill", key=f"kill_{name}", type="secondary", use_container_width=True):
            _send_operator_message(f"KILL {name}")
            st.warning(f"Kill signal sent for {name}")

    # Send hint
    st.markdown("#### Send Hint")
    hint = st.text_area(
        "Provide a hint or strategy for the solver:",
        placeholder="e.g., Try XOR with key length 5, check the least significant bit of each pixel...",
        key=f"hint_{name}",
        label_visibility="collapsed",
    )
    if st.button("Send Hint", key=f"send_hint_{name}", type="primary"):
        if hint.strip():
            _send_operator_message(f"HINT {name}: {hint}")
            st.success(f"Hint sent to {name}!")
        else:
            st.warning("Please enter a hint first.")

    # Back button
    st.divider()
    if st.button("← Back to Overview", use_container_width=True):
        st.session_state.selected_challenge = None
        st.rerun()


# ── Status overview ──────────────────────────────────────────────────────────

def _render_overview() -> None:
    """Render the top-level status overview."""
    trace_files = _get_trace_files()
    challenge_metas = _get_challenge_metas()

    # Derive status from traces
    challenge_status: dict[str, str] = {}
    challenge_steps: dict[str, int] = {}
    for path_str, _mtime in trace_files:
        tf_path = Path(path_str)
        parts = tf_path.stem.replace("trace-", "").rsplit("-", 2)
        if len(parts) >= 2:
            ch_name = parts[0]
            events = _parse_trace_file(path_str)
            status = _get_status_from_traces(events)
            if ch_name not in challenge_status or status == "solved":
                challenge_status[ch_name] = status
            challenge_steps[ch_name] = max(challenge_steps.get(ch_name, 0), len(events))

    # Summary metrics
    total = len(challenge_metas)
    solved = sum(1 for s in challenge_status.values() if s == "solved")
    analyzing = sum(1 for s in challenge_status.values() if s in ("analyzing", "exploiting"))
    failed = sum(1 for s in challenge_status.values() if s in ("error", "gave_up"))

    mcol1, mcol2, mcol3, mcol4, mcol5 = st.columns(5)
    mcol1.metric("Total Challenges", total)
    mcol2.metric("✅ Solved", solved, delta_color="off")
    mcol3.metric("🔄 In Progress", analyzing)
    mcol4.metric("❌ Failed", failed)
    mcol5.metric("💰 Total Cost", "—")  # Calculated from traces

    # Challenge cards grid
    st.divider()
    st.subheader("Challenges")

    # Filter bar
    filter_col1, filter_col2 = st.columns([3, 1])
    with filter_col1:
        status_filter = st.multiselect(
            "Filter by status",
            options=["solved", "analyzing", "exploiting", "failed", "new", "needs_human"],
            default=[],
            placeholder="All statuses",
        )
    with filter_col2:
        search = st.text_input("🔍 Search", placeholder="Challenge name...")

    if not challenge_metas:
        st.info("No challenges found. Pull challenges first with the coordinator.")
        return

    # Grid display
    cols_per_row = 3
    sorted_challenges = sorted(challenge_metas.items())

    for i in range(0, len(sorted_challenges), cols_per_row):
        row_challenges = sorted_challenges[i:i + cols_per_row]
        cols = st.columns(cols_per_row)
        for col, (name, meta) in zip(cols, row_challenges):
            status = challenge_status.get(name, "new")
            trace_count = challenge_steps.get(name, 0)

            # Apply filters
            if status_filter and status not in status_filter:
                continue
            if search and search.lower() not in name.lower():
                continue

            with col:
                _render_challenge_card(name, meta, status, trace_count)


# ── Main app ─────────────────────────────────────────────────────────────────

def main() -> None:
    """Main dashboard entry point."""
    _render_sidebar()

    # Main content area
    st.title("🏴 CTF Agent Dashboard")

    # Check if a specific challenge is selected
    if st.session_state.get("selected_challenge"):
        name = st.session_state.selected_challenge
        metas = _get_challenge_metas()
        if name in metas:
            _render_challenge_detail(name, metas[name])
            return
        else:
            st.warning(f"Challenge '{name}' not found.")
            st.session_state.selected_challenge = None
            st.rerun()

    # Overview page
    _render_overview()

    # Auto-refresh
    st.markdown(
        "<div style='position:fixed; bottom:10px; right:10px; opacity:0.5; font-size:0.8em'>"
        "Auto-refreshing every 3s</div>",
        unsafe_allow_html=True,
    )
    time.sleep(0.1)  # Allow Streamlit's auto-refresh mechanism


if __name__ == "__main__":
    main()
