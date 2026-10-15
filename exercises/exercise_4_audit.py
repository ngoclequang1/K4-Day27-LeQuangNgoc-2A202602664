"""Exercise 4: durable HITL graph with a structured SQLite audit trail."""
from __future__ import annotations

# Support both `python -m exercises...` and the README script commands.
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio
import json
import os
import time
import uuid

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from common.db import db_path, write_audit_event
from common.github import fetch_pr, post_review_comment
from common.llm import get_llm
from common.schemas import AuditEntry, PRAnalysis, ReviewState, risk_level_for
from exercises.exercise_1_confidence import node_route as route_decision
from exercises.exercise_3_escalation import handle_interrupt, _render_comment_body
from exercises.exercise_1_confidence import REVIEW_PROMPT, approval_payload, validate_choice

AGENT_ID = "pr-review-agent@v0.1"


async def audit(state, entry: AuditEntry) -> None:
    await write_audit_event(thread_id=state["thread_id"], pr_url=state["pr_url"], entry=entry)


async def record(state, action, started, decision="pending", reason=None, human=False):
    analysis = state.get("analysis")
    confidence = analysis.confidence if analysis else 0.0
    await audit(state, AuditEntry(
        agent_id=AGENT_ID, action=action, confidence=confidence,
        risk_level=risk_level_for(confidence) if analysis else "med",
        reviewer_id=os.environ.get("GITHUB_USER") if human else None,
        decision=decision, reason=reason,
        execution_time_ms=int((time.monotonic() - started) * 1000),
    ))


async def node_fetch_pr(state):
    started = time.monotonic()
    pr = await asyncio.to_thread(fetch_pr, state["pr_url"])
    await record(state, "fetch_pr", started, reason=f"Fetched {len(pr.files_changed)} files; head={pr.head_sha}")
    return {"pr_title": pr.title, "pr_author": pr.author, "pr_diff": pr.diff,
            "pr_files": pr.files_changed, "pr_head_sha": pr.head_sha}


async def node_analyze(state):
    started = time.monotonic()
    analysis = await get_llm().with_structured_output(PRAnalysis).ainvoke([
        {"role": "system", "content": REVIEW_PROMPT},
        {"role": "user", "content": f"Title: {state['pr_title']}\nDiff:\n{state['pr_diff']}"},
    ])
    await record({**state, "analysis": analysis}, "analyze", started, reason=analysis.model_dump_json())
    return {"analysis": analysis}


async def node_route(state):
    started = time.monotonic()
    update = route_decision(state)
    await record(state, "route", started,
                 {"auto_approve": "auto", "escalate": "escalate", "human_approval": "pending"}[update["decision"]],
                 update["decision"])
    return update


# Pending events live in separate checkpointed nodes: interrupt nodes replay
# from their beginning on resume, so writing before interrupt there duplicates logs.
async def node_request_approval(state):
    await record(state, "human_approval", time.monotonic(), reason="Awaiting reviewer approval")
    return {}


async def node_human_approval(state):
    response = interrupt(approval_payload(state))
    started = time.monotonic()
    update = validate_choice(response)
    await record(state, "human_approval", started, update["human_choice"], update["human_feedback"], human=True)
    return update


async def node_request_escalation(state):
    await record(state, "escalate", time.monotonic(), "pending",
                 json.dumps(state["analysis"].escalation_questions))
    return {}


async def node_escalate(state):
    a = state["analysis"]
    questions = a.escalation_questions or ["What context or safeguards address the risks in this diff?"]
    answers = interrupt({
        "kind": "escalation", "pr_url": state["pr_url"], "confidence": a.confidence,
        "confidence_reasoning": a.confidence_reasoning, "summary": a.summary,
        "risk_factors": a.risk_factors, "questions": questions,
    })
    started = time.monotonic()
    if not isinstance(answers, dict) or any(not str(answers.get(q, "")).strip() for q in questions):
        raise ValueError("Answer every escalation question")
    await record(state, "escalate", started, "escalate", json.dumps(answers), human=True)
    return {"escalation_answers": answers}


async def node_synthesize(state):
    started = time.monotonic()
    refined = await get_llm().with_structured_output(PRAnalysis).ainvoke([
        {"role": "system", "content": REVIEW_PROMPT + " Refine the initial review using the answers; preserve unresolved risks."},
        {"role": "user", "content": f"Diff:\n{state['pr_diff']}\nInitial review: {state['analysis'].model_dump_json()}\nAnswers: {json.dumps(state['escalation_answers'])}"},
    ])
    await record({**state, "analysis": refined}, "synthesize", started, reason=refined.model_dump_json())
    return {"analysis": refined}


async def node_auto_approve(state):
    await record(state, "auto_approve", time.monotonic(), "auto", state["analysis"].confidence_reasoning)
    return {}


async def node_commit(state):
    started = time.monotonic()
    automatic = state["decision"] == "auto_approve"
    choice = state.get("human_choice")
    if not automatic and choice not in {"approve", "edit"}:
        await record(state, "commit", started, "reject", "No comment posted", human=True)
        return {"final_action": "rejected", "posted_comment_body": None}
    body = _render_comment_body(state)
    try:
        await asyncio.to_thread(post_review_comment, state["pr_url"], body)
    except Exception as exc:
        await record(state, "commit", started, "failed", f"{type(exc).__name__}: posting failed", human=not automatic)
        return {"final_action": "commit_failed", "posted_comment_body": None}
    await record(state, "commit", started, "auto" if automatic else choice, body, human=not automatic)
    return {"final_action": "auto_committed" if automatic else "committed", "posted_comment_body": body}


def build_graph(checkpointer):
    graph = StateGraph(ReviewState)
    for name in ("fetch_pr", "analyze", "route", "auto_approve", "request_approval",
                 "human_approval", "request_escalation", "escalate", "synthesize", "commit"):
        graph.add_node(name, globals()[f"node_{name}"])
    for source, target in ((START, "fetch_pr"), ("fetch_pr", "analyze"), ("analyze", "route"),
                           ("auto_approve", "commit"), ("request_approval", "human_approval"),
                           ("human_approval", "commit"), ("request_escalation", "escalate"),
                           ("escalate", "synthesize"), ("synthesize", "request_approval"), ("commit", END)):
        graph.add_edge(source, target)
    graph.add_conditional_edges("route", lambda s: s["decision"], {
        "auto_approve": "auto_approve", "human_approval": "request_approval", "escalate": "request_escalation",
    })
    return graph.compile(checkpointer=checkpointer)


def snapshot_result(snapshot):
    result = dict(snapshot.values)
    interrupts = tuple(i for task in snapshot.tasks for i in task.interrupts)
    if interrupts:
        result["__interrupt__"] = interrupts
    return result


async def run(pr_url, thread_id=None):
    load_dotenv()
    thread_id = thread_id or str(uuid.uuid4())
    print(f"thread_id = {thread_id}")
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        app = build_graph(cp)
        cfg = {"configurable": {"thread_id": thread_id}}
        snapshot = await app.aget_state(cfg)
        if snapshot.values:
            if snapshot.values["pr_url"] != pr_url:
                raise ValueError("Thread belongs to a different PR")
            result = snapshot_result(snapshot)
            if snapshot.next and "__interrupt__" not in result:
                result = await app.ainvoke(None, cfg)
        else:
            result = await app.ainvoke({"pr_url": pr_url, "thread_id": thread_id}, cfg)
        while result.get("__interrupt__"):
            answer = handle_interrupt(result["__interrupt__"][0].value)
            result = await app.ainvoke(Command(resume=answer), cfg)
        print(f"final_action = {result.get('final_action')}")
        print(f"Replay: uv run python -m audit.replay --thread {thread_id}")
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", required=True)
    parser.add_argument("--thread", help="Resume an existing session")
    args = parser.parse_args()
    asyncio.run(run(args.pr, args.thread))


if __name__ == "__main__":
    main()
