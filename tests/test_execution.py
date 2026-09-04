from __future__ import annotations

from dataclasses import replace
import json
import re
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import DeepSeekAgent, ModelCall, Submission
import app
from benchmark_cases import ALL_CASES, BenchmarkResult, POSITIVE_CASES, preflight_case, reference_for_contract, run_case
from loop import LoopResult, run_verification_loop
from verifier import VerificationResult, verify_against_contract, verify_lean


class ScriptedAgent:
    def __init__(self, submissions):
        self.submissions = iter(submissions)
        self.contexts = []
        self.diagnoses = []

    def submit(self, spec, contract, previous, feedback, strategy, history, fragment, plan):
        self.contexts.append((previous, feedback, strategy, list(history), fragment, list(plan)))
        return next(self.submissions)

    def diagnose(self, spec, contract, code, error, history):
        self.diagnoses.append(error)
        return 'Specific repair hypothesis', ModelCall('diagnosis', 'fake', '', '', '', {'total_tokens': 8})


class ModelPathTests(unittest.TestCase):
    def test_every_reference_passes_actual_model_contract(self):
        for case in POSITIVE_CASES:
            with self.subTest(case=case.id):
                preflight_case(case)

    def test_clamp_missing_h_is_a_contract_failure(self):
        case = POSITIVE_CASES[-1]
        source = reference_for_contract(case)
        self.assertTrue(verify_lean(source).passed)
        broken = case.contract.verification_wrapper.replace('lo hi x h', 'lo hi x')
        result = verify_against_contract(source, broken)
        self.assertFalse(result.passed)
        self.assertEqual(result.stage, 'contract')

    def test_broken_fixture_never_calls_model(self):
        case = POSITIVE_CASES[-1]
        broken = replace(case, contract=replace(case.contract, verification_wrapper=case.contract.verification_wrapper.replace('lo hi x h', 'lo hi x')))
        agent = ScriptedAgent([])
        with self.assertRaisesRegex(ValueError, 'preflight failed'):
            run_case(broken, agent)
        self.assertEqual(agent.contexts, [])

    def test_raw_and_diagnosis_feedback_are_routed_differently(self):
        case = POSITIVE_CASES[0]
        for strategy in ('raw', 'diagnostic'):
            agent = ScriptedAgent([Submission('bad'), Submission('good')])
            with patch('loop.verify_against_contract', side_effect=[VerificationResult(False, 'specific Lean error'), VerificationResult(True, 'accepted')]):
                result = run_verification_loop(case.specification, case.contract, agent, 2, strategy=strategy)
            self.assertTrue(result.passed)
            self.assertEqual(agent.contexts[1][0], 'bad')
            self.assertIn('specific Lean error', agent.contexts[1][1])
            self.assertEqual('Specific repair hypothesis' in agent.contexts[1][1], strategy != 'raw')
            self.assertEqual(len(agent.diagnoses), int(strategy != 'raw'))
            self.assertEqual(result.attempts[1].feedback_received, result.attempts[0].repair_feedback)

    def test_actual_intermediate_checked_but_not_final_pass(self):
        case = POSITIVE_CASES[0]
        partial = 'import Init.Omega\ndef addOne (n : Nat) : Nat := n + 1\ntheorem helper (n : Nat) : addOne n = n + 1 := by rfl'
        agent = ScriptedAgent([Submission(partial, 'continue', ['Implement', 'Prove']), Submission(reference_for_contract(case))])
        events = []
        result = run_verification_loop(case.specification, case.contract, agent, 2, strategy='staged', on_event=events.append)
        self.assertTrue(result.passed)
        self.assertEqual(result.attempts[0].verification.stage, 'intermediate')
        self.assertFalse([e for e in events if e['kind'] == 'attempt_done'][0]['final_pass'])
        self.assertEqual(agent.contexts[1][4], partial)
        self.assertIn('not final PASS', app.render_attempt_card(result.attempts[0]))

    def test_intermediate_only_never_completes(self):
        case = POSITIVE_CASES[0]
        agent = ScriptedAgent([Submission('theorem helper : True := by trivial', 'continue')])
        with patch('loop.verify_lean', return_value=VerificationResult(True, 'accepted')):
            result = run_verification_loop('', case.contract, agent, 1, strategy='staged')
        self.assertFalse(result.passed)
        self.assertIsNone(result.to_dict()['attempts_to_success'])

    def test_contract_error_stops_instead_of_retrying(self):
        case = POSITIVE_CASES[0]
        agent = ScriptedAgent([Submission('good')])
        with patch('loop.verify_against_contract', return_value=VerificationResult(False, 'broken wrapper', 'contract')):
            result = run_verification_loop('', case.contract, agent, 20)
        self.assertEqual(result.stop_reason, 'contract')
        self.assertEqual(len(result.attempts), 1)
        self.assertFalse(agent.diagnoses)

    def test_limits_are_not_hardcoded_to_three(self):
        case = POSITIVE_CASES[0]
        for limit in (3, 5, None):
            expected = limit or 20
            agent = ScriptedAgent([Submission(f'candidate_{i}') for i in range(expected)])
            with patch('loop.verify_against_contract', return_value=VerificationResult(False, 'error')):
                result = run_verification_loop('', case.contract, agent, limit, strategy='raw')
            self.assertEqual(len(result.attempts), expected)
            self.assertEqual(result.stop_reason, 'retry_limit')

    def test_identical_failure_stops_explicitly(self):
        case = POSITIVE_CASES[0]
        agent = ScriptedAgent([Submission('same') for _ in range(3)])
        with patch('loop.verify_against_contract', return_value=VerificationResult(False, 'same error')):
            result = run_verification_loop('', case.contract, agent, 20, strategy='raw')
        self.assertEqual(result.stop_reason, 'stagnation')

    def test_declaration_mismatch_is_repairable_not_system_error(self):
        case = POSITIVE_CASES[0]
        result = verify_against_contract(reference_for_contract(case).replace('solution_correct', 'other'),
                                        case.contract.verification_wrapper, signature=case.contract.function_signature,
                                        theorem=case.contract.theorem_statement)
        self.assertEqual(result.stage, 'declaration')

    def test_raw_prompt_does_not_include_structured_history(self):
        agent = DeepSeekAgent.__new__(DeepSeekAgent)
        def response(system, task, purpose):
            return ModelCall(purpose, 'fake', system, task, '```lean\ndef x := 1\n```', {})
        case = POSITIVE_CASES[0]
        with patch.object(agent, '_call', side_effect=response):
            raw = agent.submit('', case.contract, 'old', 'raw error', 'raw', [{'diagnosis': 'SECRET_MARKER'}], '', [])
            diag = agent.submit('', case.contract, 'old', 'hypothesis + raw error', 'diagnostic', [{'diagnosis': 'HISTORY_MARKER'}], '', [])
        self.assertNotIn('SECRET_MARKER', raw.call.user_prompt)
        self.assertIn('HISTORY_MARKER', diag.call.user_prompt)

    def test_unknown_token_usage_is_not_zero(self):
        case = POSITIVE_CASES[0]
        with patch('loop.verify_against_contract', return_value=VerificationResult(True, 'ok')):
            result = run_verification_loop('', case.contract, ScriptedAgent([Submission('x')]), 1)
        self.assertIsNone(result.total_tokens)


class WebOptionsTests(unittest.TestCase):
    def test_stopped_cases_count_as_completed_in_live_progress(self):
        class InlineThread:
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                self.target()
        def fake_run(agent, label, progress, limit, **options):
            results = []
            for case in ALL_CASES:
                progress(case.id, 3, False)
                result = BenchmarkResult(case, False, 'FAIL', 'Stopped')
                options['on_case_done'](result)
                results.append(result)
            return results
        with patch('app.threading.Thread', InlineThread), patch('app.DeepSeekAgent', return_value=SimpleNamespace(model='fake')), patch('app.run_all_cases', side_effect=fake_run):
            job_id = app.start_benchmark_job(None, 'raw')
        job = app.BENCHMARK_JOBS.pop(job_id)
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['completed'], 15)

    def test_page_scripts_parse(self):
        if not shutil.which('node'):
            self.skipTest('Node unavailable; browser scripts checked manually')
        for body in (app.render_start(), app.render_verify_loading('test'), app.render_benchmark_loading('test')):
            for script in re.findall(r'<script>(.*?)</script>', body.decode(), re.S):
                checked = subprocess.run(['node', '--check'], input=script, text=True, capture_output=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_saved_report_path_cannot_escape_runs_directory(self):
        with patch('app.Path.glob', return_value=[__import__('pathlib').Path('/tmp/comparison-example.json')]):
            self.assertIn(b'Unknown comparison', app.render_comparison('../../.env'))

    def test_mode_survives_confirmation(self):
        for mode in ('raw', 'diagnostic', 'staged'):
            token = app.store_contract('spec', 'deepseek', POSITIVE_CASES[0].contract, 5, mode)
            self.assertEqual(app.take_contract(token)[-2:], (5, mode))

    def test_legacy_and_new_options(self):
        self.assertEqual(app.parse_run_options({'attempts': ['until_success_raw']}), (None, 'raw'))
        self.assertEqual(app.parse_run_options({'attempts': ['5'], 'strategy': ['staged']}), (5, 'staged'))
        for choice in ('0', '21', 'bad'):
            with self.assertRaises(ValueError):
                app.parse_run_options({'attempts': [choice]})

    def test_ui_escapes_model_content(self):
        from loop import Attempt
        record = Attempt(1, '<script>bad()</script>', VerificationResult(False, '<img src=x>'), current_step='<svg>')
        html = app.render_attempt_card(record)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)


if __name__ == '__main__':
    unittest.main()
