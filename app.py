"""Streamlit approval UI. Run: uv run streamlit run app.py."""
from __future__ import annotations

import asyncio
import uuid

import streamlit as st
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from audit.replay import recent_threads
from common.db import db_path, replay_events
from common.github import parse_pr_url
from exercises.exercise_4_audit import build_graph, snapshot_result

load_dotenv()
st.set_page_config(page_title="HITL PR Review", page_icon="🔎", layout="wide")
for key, default in {"thread_id": st.query_params.get("thread"), "pr_url": "",
                     "interrupt_payload": None, "final": None, "loaded_thread": None}.items():
    if key not in st.session_state:
        st.session_state[key] = default


async def run_graph(pr_url, thread_id, resume_value=None, restore=False):
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        app = build_graph(cp)
        cfg = {"configurable": {"thread_id": thread_id}}
        snapshot = await app.aget_state(cfg)
        if restore:
            if not snapshot.values:
                raise ValueError("Session not found")
            if snapshot.next and not any(task.interrupts for task in snapshot.tasks):
                return await app.ainvoke(None, cfg)
            return snapshot_result(snapshot)
        if resume_value is not None:
            if not snapshot.values or snapshot.values["pr_url"] != pr_url:
                raise ValueError("Session does not match this PR")
            return await app.ainvoke(Command(resume=resume_value), cfg)
        return await app.ainvoke({"pr_url": pr_url, "thread_id": thread_id}, cfg)


def save_result(result):
    st.session_state.pr_url = result.get("pr_url", st.session_state.pr_url)
    interrupts = result.get("__interrupt__")
    st.session_state.interrupt_payload = interrupts[0].value if interrupts else None
    st.session_state.final = None if interrupts else result
    st.session_state.loaded_thread = st.session_state.thread_id
    st.query_params["thread"] = st.session_state.thread_id


def render_approval_card(payload):
    st.subheader(f"Approval requested · confidence {payload['confidence']:.0%}")
    st.caption(payload["confidence_reasoning"])
    st.markdown(payload["summary"])
    if payload.get("risk_factors"):
        st.warning("\n".join(payload["risk_factors"]))
    for c in payload.get("comments", []):
        st.markdown(f"- **[{c['severity']}]** `{c['file']}:{c.get('line') or '?'}` — {c['body']}")
    with st.expander("PR diff"):
        st.code(payload.get("diff_preview", ""), language="diff")
    feedback = st.text_area("Reviewer note, or full replacement comment for Edit", key=f"feedback_{st.session_state.thread_id}")
    columns = st.columns(3)
    for column, label in zip(columns, ("Approve", "Reject", "Edit")):
        if column.button(label, type="primary" if label == "Approve" else "secondary"):
            if label == "Edit" and not feedback.strip():
                st.error("Enter the replacement review text before choosing Edit.")
                return None
            return {"choice": label.lower(), "feedback": feedback}
    return None


def render_escalation_card(payload):
    st.subheader(f"Escalation · confidence {payload['confidence']:.0%}")
    st.caption(payload["confidence_reasoning"])
    st.markdown(payload["summary"])
    if payload.get("risk_factors"):
        st.error("\n".join(payload["risk_factors"]))
    with st.form(f"escalation_{st.session_state.thread_id}"):
        answers = {q: st.text_area(q, key=f"question_{st.session_state.thread_id}_{i}")
                   for i, q in enumerate(payload["questions"])}
        if st.form_submit_button("Submit answers"):
            if any(not answer.strip() for answer in answers.values()):
                st.error("Please answer every question.")
            else:
                return answers
    return None


st.title("HITL PR Review Agent")
st.caption("Analyze a pull request, resolve uncertainty, and approve the review.")
with st.sidebar:
    st.header("Recent sessions")
    try:
        rows = asyncio.run(recent_threads())
        for row in rows:
            label = f"{row['thread_id'][:8]} · {row['worst_risk']} · {row['events']} events"
            if st.button(label, key=row["thread_id"], help=f"{row['pr_url']}\n{row['last_event']}"):
                st.session_state.thread_id = row["thread_id"]
                st.session_state.loaded_thread = None
                st.session_state.interrupt_payload = None
                st.session_state.final = None
                st.rerun()
            st.caption(row["pr_url"])
    except Exception as exc:
        st.error(f"Cannot load sessions ({type(exc).__name__}).")

if st.session_state.thread_id and st.session_state.loaded_thread != st.session_state.thread_id:
    try:
        save_result(asyncio.run(run_graph("", st.session_state.thread_id, restore=True)))
    except Exception as exc:
        st.error(f"Cannot restore session ({type(exc).__name__}). Check the thread ID and database.")
        st.stop()

with st.form("start"):
    pr_url = st.text_input("PR URL", value=st.session_state.pr_url,
                           placeholder="https://github.com/VinUni-AI20k/PR-Demo/pull/1")
    st.caption("High-confidence reviews post automatically. Other reviews wait for your response.")
    submitted = st.form_submit_button("Run review", type="primary")

if submitted:
    try:
        parse_pr_url(pr_url.strip())
        st.session_state.pr_url = pr_url.strip()
        st.session_state.thread_id = str(uuid.uuid4())
        st.query_params["thread"] = st.session_state.thread_id
        st.session_state.interrupt_payload = None
        st.session_state.final = None
        with st.spinner("Fetching PR and analyzing the diff..."):
            save_result(asyncio.run(run_graph(st.session_state.pr_url, st.session_state.thread_id)))
        st.rerun()
    except Exception as exc:
        st.error(f"Review failed ({type(exc).__name__}). Check the PR URL, API configuration, and connection.")

payload = st.session_state.interrupt_payload
if payload:
    answer = render_approval_card(payload) if payload["kind"] == "approval_request" else render_escalation_card(payload)
    if answer is not None:
        try:
            with st.spinner("Continuing review..."):
                save_result(asyncio.run(run_graph(st.session_state.pr_url, st.session_state.thread_id, answer)))
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to continue ({type(exc).__name__}). Your session is saved.")

final = st.session_state.final
if final:
    action = final.get("final_action")
    if action in {"committed", "auto_committed"}:
        st.success("Review comment posted to GitHub.")
        st.link_button("View PR comments on GitHub", final["pr_url"] + "#discussion_bucket")
    elif action == "rejected":
        st.warning("Review rejected. No comment posted.")
    elif action == "commit_failed":
        st.error("Posting failed. Check your GitHub token permissions and connection.")
    else:
        st.info("Session saved. Reopen it to continue.")
    if final.get("analysis"):
        st.metric("Final confidence", f"{final['analysis'].confidence:.0%}")
        st.markdown(final["analysis"].summary)
    if final.get("posted_comment_body"):
        with st.expander("Posted review"):
            st.markdown(final["posted_comment_body"])

if st.session_state.thread_id:
    st.caption(f"Session: {st.session_state.thread_id}")
    with st.expander("Audit trail"):
        st.dataframe(asyncio.run(replay_events(st.session_state.thread_id)), hide_index=True, width="stretch")
