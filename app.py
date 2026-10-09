import os
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from analyzer import (
    CLAUSES,
    MAX_RETRY_ATTEMPTS,
    analyze_contract,
    clear_cache,
    compare_to_form,
    demo_mode_enabled,
)

load_dotenv()

DEMO_MODE = demo_mode_enabled()
DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

STATUS_OPTIONS = ["same", "modified", "major_deviation", "missing", "added"]
STATUS_LABELS = {
    "same": "Same",
    "modified": "Modified",
    "major_deviation": "Major Deviation",
    "missing": "Missing",
    "added": "Added",
}
STATUS_COLORS = {
    "same": "#d8f3dc",
    "modified": "#fff3bf",
    "major_deviation": "#ffc9c9",
    "missing": "#e9ecef",
    "added": "#cfe8ff",
    "unknown": "#f1f3f5",
}

st.set_page_config(page_title="Kira-style Contract Heatmap", layout="wide")

st.title("Kira-style Contract Heatmap")
st.caption("Claude-powered form comparison prototype")

if DEMO_MODE:
    st.warning("**DEMO MODE — Claude API calls disabled.** Showing deterministic mock extraction/comparison results.")

with st.sidebar:
    st.header("Settings")
    model = st.text_input("Claude model", DEFAULT_MODEL)
    st.caption("Configured via ANTHROPIC_MODEL in .env; this field overrides it for the current session only.")
    if st.button("Clear local cache"):
        clear_cache()
        st.success("Cache cleared.")
    st.markdown("Upload one form agreement and one or more contracts.")

form_file = st.file_uploader("1. Upload the form / reference agreement", type=["pdf"], key="form")
contract_files = st.file_uploader(
    "2. Upload contracts to compare",
    type=["pdf"],
    accept_multiple_files=True,
    key="contracts",
)

st.session_state.setdefault("form_bytes", None)
st.session_state.setdefault("form_name", None)
st.session_state.setdefault("contract_bytes", {})
st.session_state.setdefault("form_result", None)
st.session_state.setdefault("contract_results", {})
st.session_state.setdefault("overrides", {})
st.session_state.setdefault("run_model", DEFAULT_MODEL)


def _retry_notifier(box, label):
    def _on_attempt(attempt, wait_seconds, exc):
        box.markdown(
            f"⚠️ {label} — retrying in {wait_seconds}s "
            f"(attempt {attempt}/{MAX_RETRY_ATTEMPTS})... *{exc}*"
        )

    return _on_attempt


def process_reference(path: Path, model: str, box) -> None:
    box.markdown("⏳ Reference Agreement — processing...")
    try:
        data = analyze_contract(str(path), model=model, on_retry=_retry_notifier(box, "Reference Agreement"))
        st.session_state["form_result"] = {"status": "success", "data": data, "error": None}
        box.markdown("✅ Reference Agreement")
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as-is
        st.session_state["form_result"] = {"status": "failed", "data": None, "error": str(exc)}
        box.markdown(f"❌ Reference Agreement — Failed: {exc}")


def process_contract(name: str, path: Path, model: str, form_data: dict, box) -> None:
    box.markdown(f"⏳ {name} — processing...")
    try:
        analysis = analyze_contract(str(path), model=model, on_retry=_retry_notifier(box, name))
        comparison = compare_to_form(form_data, analysis, model=model, on_retry=_retry_notifier(box, name))
        st.session_state["contract_results"][name] = {
            "status": "success",
            "analysis": analysis,
            "comparison": comparison,
            "error": None,
        }
        box.markdown(f"✅ {name}")
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as-is
        st.session_state["contract_results"][name] = {
            "status": "failed",
            "analysis": None,
            "comparison": None,
            "error": str(exc),
        }
        box.markdown(f"❌ {name} — Failed: {exc}")


def run_pipeline(model: str, contract_names: list, retry_reference: bool) -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        if retry_reference:
            ref_box = st.empty()
            form_path = td / st.session_state["form_name"]
            form_path.write_bytes(st.session_state["form_bytes"])
            process_reference(form_path, model, ref_box)

        form_result = st.session_state["form_result"]
        if not form_result or form_result["status"] != "success":
            return

        form_data = form_result["data"]
        boxes = {name: st.empty() for name in contract_names}
        for name in contract_names:
            contract_path = td / name
            contract_path.write_bytes(st.session_state["contract_bytes"][name])
            process_contract(name, contract_path, model, form_data, boxes[name])


if form_file and contract_files:
    if st.button("Run analysis", type="primary"):
        st.session_state["form_bytes"] = form_file.getvalue()
        st.session_state["form_name"] = form_file.name
        st.session_state["contract_bytes"] = {f.name: f.getvalue() for f in contract_files}
        st.session_state["form_result"] = None
        st.session_state["contract_results"] = {}
        st.session_state["overrides"] = {}
        st.session_state["run_model"] = model

        st.subheader("Progress")
        run_pipeline(model, list(st.session_state["contract_bytes"].keys()), retry_reference=True)
else:
    st.info("Upload a form agreement and at least one contract to begin.")

# --------------------------------------------------------------------------
# Results, retry, heatmap, and reviewer overrides (persist across reruns)
# --------------------------------------------------------------------------

if st.session_state["form_result"] is not None:
    form_result = st.session_state["form_result"]
    contract_results = st.session_state["contract_results"]
    failed_contracts = [n for n, r in contract_results.items() if r["status"] == "failed"]
    ref_failed = form_result["status"] == "failed"

    if ref_failed or failed_contracts:
        st.subheader("Retry")
        if ref_failed:
            st.error(f"Reference agreement failed: {form_result['error']}")
        if failed_contracts:
            st.error("Failed contracts: " + ", ".join(failed_contracts))
        if st.button("Retry failed documents"):
            retry_model = st.session_state.get("run_model", model)
            run_pipeline(retry_model, failed_contracts, retry_reference=ref_failed)

    if st.session_state["form_result"]["status"] == "success":
        successful = {n: r for n, r in st.session_state["contract_results"].items() if r["status"] == "success"}

        if successful:
            st.subheader("Heatmap")

            rows = []
            for name, result in successful.items():
                comparison = result["comparison"]
                row = {"Contract": name}
                for clause in CLAUSES:
                    item = comparison["comparisons"].get(clause, {})
                    override = st.session_state["overrides"].get(name, {}).get(clause, {})
                    row[clause] = override.get("status", item.get("status", "unknown"))
                rows.append(row)

            df = pd.DataFrame(rows)

            def color_cell(value):
                return f"background-color: {STATUS_COLORS.get(value, '#ffffff')};"

            display = df.style.map(color_cell, subset=df.columns[1:])
            st.dataframe(display, use_container_width=True, hide_index=True)

            st.markdown(
                "**Legend:** \U0001f7e9 same · \U0001f7e8 modified · \U0001f7e5 major deviation · "
                "⬜ missing · \U0001f7e6 added"
            )

            st.subheader("Clause details")

            def _make_override_callback(contract_name, clause, status_key, note_key):
                def _cb():
                    st.session_state["overrides"].setdefault(contract_name, {})[clause] = {
                        "status": st.session_state[status_key],
                        "note": st.session_state[note_key],
                    }

                return _cb

            for name, result in successful.items():
                comparison = result["comparison"]
                with st.expander(name):
                    for clause in CLAUSES:
                        item = comparison["comparisons"].get(clause, {})
                        status_key = f"status_{name}_{clause}"
                        note_key = f"note_{name}_{clause}"
                        existing_override = st.session_state["overrides"].get(name, {}).get(clause, {})
                        default_status = existing_override.get("status", item.get("status", "unknown"))
                        default_note = existing_override.get("note", "")

                        st.markdown(f"### {clause.replace('_', ' ').title()}")
                        c1, c2 = st.columns(2)

                        with c1:
                            st.markdown("**Form**")
                            st.write(item.get("form_text") or "Not found")
                            if item.get("form_page"):
                                st.caption(f"Page {item['form_page']}")

                        with c2:
                            st.markdown("**Contract**")
                            st.write(item.get("contract_text") or "Not found")
                            if item.get("contract_page"):
                                st.caption(f"Page {item['contract_page']}")

                        st.info(item.get("explanation", "No explanation returned."))

                        status_index = (
                            STATUS_OPTIONS.index(default_status) if default_status in STATUS_OPTIONS else 0
                        )
                        st.selectbox(
                            "Reviewer status",
                            STATUS_OPTIONS,
                            index=status_index,
                            format_func=lambda s: STATUS_LABELS.get(s, s),
                            key=status_key,
                            on_change=_make_override_callback(name, clause, status_key, note_key),
                        )
                        st.text_input(
                            "Reviewer note (optional)",
                            value=default_note,
                            key=note_key,
                            on_change=_make_override_callback(name, clause, status_key, note_key),
                        )

                        st.divider()
