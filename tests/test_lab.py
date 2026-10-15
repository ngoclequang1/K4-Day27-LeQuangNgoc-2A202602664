"""Offline integration tests: never call GitHub or an LLM."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from streamlit.testing.v1 import AppTest

from common.db import replay_events
from common.schemas import PRAnalysis
from audit.replay import recent_threads
from exercises import exercise_1_confidence as ex1
from exercises import exercise_2_hitl as ex2
from exercises import exercise_3_escalation as ex3
from exercises import exercise_4_audit as ex4

PR = 'https://github.com/example/demo/pull/1'


def analysis(confidence):
    return PRAnalysis(summary='Review summary', confidence=confidence,
                      confidence_reasoning='Needs context', risk_factors=['Migration'],
                      escalation_questions=['How is migration handled?'])


class FakeLLM:
    def __init__(self, confidence):
        self.confidence = confidence

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        return analysis(self.confidence)

    async def ainvoke(self, messages):
        return self.invoke(messages)


def fake_pr(url):
    return SimpleNamespace(title='Demo PR', author='author', diff='+ change',
                           files_changed=['demo.py'], head_sha='abc1234')


class LabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'HITL_DB_PATH': str(Path(self.temp.name) / 'audit.db'), 'GITHUB_USER': 'tester'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_routing_boundaries(self):
        for confidence, expected in [(0, 'escalate'), (.579, 'escalate'), (.58, 'human_approval'),
                                     (.72, 'human_approval'), (.729, 'human_approval'), (.73, 'auto_approve'), (1, 'auto_approve')]:
            self.assertEqual(ex1.node_route({'analysis': analysis(confidence)})['decision'], expected)
        with patch.object(ex1, 'fetch_pr', fake_pr), patch.object(ex1, 'get_llm', return_value=FakeLLM(.65)):
            self.assertEqual(ex1.build_graph().invoke({'pr_url': PR})['final_action'], 'pending_human_approval')

    def test_memory_exercises(self):
        for module, confidence in [(ex2, .65), (ex3, .4)]:
            with self.subTest(module=module.__name__), patch.object(module, 'fetch_pr', fake_pr), patch.object(module, 'get_llm', return_value=FakeLLM(confidence)), patch.object(module, 'post_review_comment') as post:
                graph = module.build_graph()
                cfg = {'configurable': {'thread_id': module.__name__}}
                result = graph.invoke({'pr_url': PR}, cfg)
                if module is ex3:
                    self.assertEqual(result['__interrupt__'][0].value['kind'], 'escalation')
                    result = graph.invoke(Command(resume={'How is migration handled?': 'Backfill'}), cfg)
                self.assertEqual(result['__interrupt__'][0].value['kind'], 'approval_request')
                result = graph.invoke(Command(resume={'choice': 'edit', 'feedback': 'Replacement review'}), cfg)
                self.assertEqual(result['final_action'], 'committed')
                post.assert_called_once_with(PR, 'Replacement review')

    async def durable_case(self, confidence, choice, fail=False):
        thread = f'{confidence}-{choice}-{fail}'
        cfg = {'configurable': {'thread_id': thread}}
        async with AsyncSqliteSaver.from_conn_string(os.environ['HITL_DB_PATH']) as cp:
            await cp.setup()
            result = await ex4.build_graph(cp).ainvoke({'thread_id': thread, 'pr_url': PR}, cfg)
        # Close and reopen the DB/checkpointer to simulate a process restart.
        async with AsyncSqliteSaver.from_conn_string(os.environ['HITL_DB_PATH']) as cp:
            await cp.setup()
            graph = ex4.build_graph(cp)
            snapshot = await graph.aget_state(cfg)
            result = ex4.snapshot_result(snapshot)
            if confidence < .58:
                self.assertEqual(result['__interrupt__'][0].value['kind'], 'escalation')
                result = await graph.ainvoke(Command(resume={'How is migration handled?': 'Backfill'}), cfg)
                self.assertEqual(result['__interrupt__'][0].value['kind'], 'approval_request')
            if confidence < .73:
                result = await graph.ainvoke(Command(resume={'choice': choice, 'feedback': 'Replacement'}), cfg)
            expected = 'commit_failed' if fail else 'auto_committed' if confidence >= .73 else 'rejected' if choice == 'reject' else 'committed'
            self.assertEqual(result['final_action'], expected)
            events = await replay_events(thread)
            pending = [e for e in events if e['action'] == 'human_approval' and e['decision'] == 'pending']
            self.assertEqual(len(pending), 0 if confidence >= .73 else 1)
            self.assertEqual(events[-1]['action'], 'commit')
            if confidence < .73:
                self.assertEqual(events[-1]['reviewer_id'], 'tester')
            before = len(events)
        with patch.object(ex4, 'handle_interrupt', side_effect=AssertionError('Completed thread prompted again')):
            await ex4.run(PR, thread)
        self.assertEqual(len(await replay_events(thread)), before)
        rows = await recent_threads()
        if confidence < .58:
            self.assertEqual(next(row for row in rows if row['thread_id'] == thread)['worst_risk'], 'high')

    def test_durable_branches(self):
        for confidence, choice, fail in [(.9, 'approve', False), (.65, 'approve', False),
                                        (.65, 'reject', False), (.65, 'edit', False),
                                        (.4, 'approve', False), (.4, 'reject', False), (.9, 'approve', True)]:
            with self.subTest(confidence=confidence, choice=choice, fail=fail), patch.object(ex4, 'fetch_pr', fake_pr), patch.object(ex4, 'get_llm', return_value=FakeLLM(confidence)), patch.object(ex4, 'post_review_comment', side_effect=RuntimeError('network') if fail else None) as post:
                asyncio.run(self.durable_case(confidence, choice, fail))
                self.assertEqual(post.call_count, 0 if choice == 'reject' else 1)
                if choice == 'edit':
                    post.assert_called_once_with(PR, 'Replacement')

    def test_streamlit_escalation_and_reload(self):
        with patch.object(ex4, 'fetch_pr', fake_pr), patch.object(ex4, 'get_llm', return_value=FakeLLM(.4)), patch.object(ex4, 'post_review_comment') as post:
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=20).run()
            self.assertFalse(app.exception)
            app.text_input[0].set_value(PR)
            next(b for b in app.button if b.label == 'Run review').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['interrupt_payload']['kind'], 'escalation')
            thread = app.session_state['thread_id']
            # A fresh browser session restores pending work from the URL.
            restored = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=20)
            restored.query_params['thread'] = thread
            restored.run()
            self.assertFalse(restored.exception)
            restored.text_area[0].set_value('Backfill first')
            next(b for b in restored.button if b.label == 'Submit answers').click().run()
            self.assertFalse(restored.exception)
            self.assertEqual(restored.session_state['interrupt_payload']['kind'], 'approval_request')
            post.assert_not_called()
            next(b for b in restored.button if b.label == 'Reject').click().run()
            self.assertFalse(restored.exception)
            self.assertEqual(restored.session_state['final']['final_action'], 'rejected')
            post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
