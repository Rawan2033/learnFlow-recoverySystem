"""Synthetic regression for the observed early recovered proposal, not raw replay.

The failed live trace retained tool results and host rejection, not the raw model
report. These tests reconstruct its observable sequence with a scripted model.
"""
import json
from copy import deepcopy

import pytest

from learnflow.agent import Investigator
from learnflow.agent_contracts import Limits, ModelTurn, ToolCall
from learnflow.agent_trace import TraceWriter
from learnflow.development_smoke import capture
from learnflow.scenarios import setup
from learnflow.tools import RecoveryTools


def tool(name, number):
    return ModelTurn(calls=[ToolCall(f'c{number}', name, '{}')])


def report(status='recovered', references=None):
    return ModelTurn(text=json.dumps({
        'status': status, 'evidence_references': references or ['tool-0001', 'tool-0002'],
        'diagnosis': 'Synthetic proposal for regression coverage.',
        'remaining_limitations': [], 'recommended_next_steps': [],
    }))


class ScriptedModel:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.inputs = []
    def next_turn(self, prompt, results):
        self.inputs.append(deepcopy((prompt, results)))
        turn = next(self.turns)
        if isinstance(turn, Exception):
            raise turn
        return turn


def run(tmp_path, turns, limits=None):
    path = tmp_path / 'regression.sqlite3'
    setup(path, 'combined')
    tools = RecoveryTools(path)
    before = capture(path)
    model = ScriptedModel(turns)
    trace_path = tmp_path / 'trace.jsonl'
    writer = TraceWriter(trace_path)
    try:
        final = Investigator(model, tools, writer, 1, 1,
                             limits or Limits(max_repairs=2), verification_scope='learner_view').run(
                                 'Please investigate why my next lesson is locked.')
    finally:
        writer.close()
    trace = [json.loads(line) for line in trace_path.read_text().splitlines()]
    return final, model, trace, before, capture(path)


def test_observed_premature_finalization_can_continue_within_original_budget(tmp_path):
    final, model, trace, before, after = run(tmp_path, [
        tool('inspect', 1), tool('verify_end_to_end', 2), report(),
        tool('inspect_view', 3), tool('recover', 4), tool('verify_end_to_end', 5),
        tool('refresh_view', 6), tool('verify_end_to_end', 7),
        report(references=[f'tool-{i:04d}' for i in range(1, 8)]),
    ])
    feedback = json.loads(model.inputs[3][0])
    assert feedback['message_type'] == 'host_validation_feedback'
    assert feedback['error_code'] == 'unsupported_recovery_claim'
    assert feedback['remaining_budget'] == {'model_turns': 7, 'tool_calls': 18, 'repair_attempts': 2}
    assert feedback['progress_verification']['consistent'] is False
    assert feedback['learner_view_verification']['consistent'] is False
    assert feedback['learner_view_inspected'] is False
    assert [a['tool'] for a in feedback['actions_performed']] == ['inspect', 'verify_end_to_end']
    assert 'combined' not in json.dumps(feedback) and 'expected_repair_tools' not in json.dumps(feedback)
    assert model.inputs[3][1] == []  # No fake tool outputs or automatic repairs.
    assert final['status'] == 'recovered' and final['verification_outcome']['consistent'] is True
    assert [a['tool'] for a in final['actions_performed'] if a['tool'] in ('recover', 'refresh_view')] == ['recover', 'refresh_view']
    assert len(model.inputs) == 9  # Same 10-turn investigation, not a restart.
    assert len([e for e in trace if e['event'] == 'report_validation_feedback']) == 1
    proposals = [e for e in trace if e['event'] == 'model_report_proposal']
    assert len(proposals) == 2 and proposals[0]['proposal']['status'] == 'recovered'
    assert [e['report']['status'] for e in trace if e['event'] == 'final'] == ['recovered']
    assert after['source_evidence'] == before['source_evidence']
    assert after['unrelated_students'] == before['unrelated_students']
    assert after['progress'][1]['eligible'] == 1 and after['progress'][1]['completed'] == 0


def test_repeated_unsupported_claim_gets_only_one_correction(tmp_path):
    final, model, trace, before, after = run(tmp_path, [
        tool('inspect', 1), tool('verify_end_to_end', 2), report(), report(),
    ])
    assert final['status'] == 'escalated' and len(model.inputs) == 4
    assert before == after
    assert len([e for e in trace if e['event'] == 'report_validation_feedback']) == 1


@pytest.mark.parametrize('status', ['escalated', 'execution_failed'])
def test_honest_escalation_and_failure_remain_terminal(tmp_path, status):
    final, model, trace, before, after = run(tmp_path, [
        tool('inspect', 1), tool('verify_end_to_end', 2), report(status),
    ])
    assert final['status'] == status and len(model.inputs) == 3
    assert final['diagnosis'] == 'Synthetic proposal for regression coverage.'
    assert not any(e['event'] == 'report_validation_feedback' for e in trace)
    assert before == after


def test_model_can_choose_escalation_after_feedback(tmp_path):
    final, model, trace, before, after = run(tmp_path, [
        tool('inspect', 1), tool('verify_end_to_end', 2), report(), report('escalated'),
    ])
    assert final['status'] == 'escalated' and len(model.inputs) == 4
    assert before == after


@pytest.mark.parametrize('limits', [Limits(max_turns=3, max_repairs=2),
                                    Limits(max_tool_calls=2, max_repairs=2), Limits(max_repairs=0)])
def test_correction_never_extends_exhausted_budgets(tmp_path, limits):
    final, model, trace, before, after = run(tmp_path, [
        tool('inspect', 1), tool('verify_end_to_end', 2), report(),
    ], limits)
    assert final['status'] == 'escalated' and len(model.inputs) == 3
    assert not any(e['event'] == 'report_validation_feedback' for e in trace)
    assert before == after


def test_unknown_state_does_not_trigger_corrective_repair_pressure(tmp_path):
    final, model, trace, before, after = run(tmp_path, [tool('inspect', 1), report()])
    assert final['status'] == 'escalated' and len(model.inputs) == 2
    assert not any(e['event'] == 'report_validation_feedback' for e in trace)
    assert before == after


def test_api_failure_after_feedback_stops_without_retry(tmp_path):
    final, model, trace, before, after = run(tmp_path, [
        tool('inspect', 1), tool('verify_end_to_end', 2), report(), RuntimeError('private API detail'),
    ])
    assert final['status'] == 'execution_failed' and len(model.inputs) == 4
    assert before == after
    assert 'private API detail' not in json.dumps(trace)


def test_verification_explicitly_names_each_reference_without_changing_rules(tmp_path):
    path = tmp_path / 'references.sqlite3'
    setup(path, 'combined')
    tools = RecoveryTools(path)
    progress_only = tools.verify(1, 1)
    check = tools.verify_end_to_end(1, 1)
    assert check['progress'] == progress_only  # Original progress-only response preserved.
    assert check['comparison_references']['progress']['actual'] == 'stored_derived_progress'
    assert check['comparison_references']['progress']['expected'] == 'authoritative_activity_and_quiz_evidence_evaluated_with_course_rules'
    assert check['comparison_references']['learner_view']['actual'] == 'persisted_learner_view'
    assert check['comparison_references']['learner_view']['expected'] == 'stored_derived_progress_which_may_be_incorrect'
    assert {'lesson_id': 1, 'field': 'completed', 'actual': 0, 'expected': 1} in check['progress']['mismatches']
    assert {'lesson_id': 1, 'field': 'completed', 'actual': 1, 'expected': 0} in check['learner_view']['mismatches']
    tools.refresh_view(1, 1)  # A cache can agree perfectly with WRONG progress.
    check = tools.verify_end_to_end(1, 1)
    assert check['learner_view']['consistent'] is True
    assert check['progress']['consistent'] is False and check['consistent'] is False
