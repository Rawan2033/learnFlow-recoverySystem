"""Offline reproduction of the recorded refresh rejection and citation masking."""
import json

from learnflow.agent import Investigator
from learnflow.agent_contracts import Limits, ModelTurn, ToolCall
from learnflow.agent_trace import TraceWriter
from learnflow.development_smoke import capture
from learnflow.scenarios import setup
from learnflow.tools import RecoveryTools


class ScriptedModel:
    def __init__(self, names, status='escalated', extra_references=()):
        self.names = iter(enumerate(names, 1))
        self.results = []
        self.status = status
        self.extra_references = extra_references
    def next_turn(self, prompt, results):
        self.results.extend(results)
        number, name = next(self.names, (None, None))
        if name is not None:
            return ModelTurn(calls=[ToolCall(f'call-{number}', name, '{}')])
        return ModelTurn(text=json.dumps({
            'status': self.status,
            'evidence_references': [r.result['evidence_ref'] for r in self.results] + list(self.extra_references),
            'diagnosis': 'Progress was repaired, but the learner view remains stale because the refresh request was rejected.',
            'remaining_limitations': ['The cache remains inconsistent; refresh was not performed.'],
            'recommended_next_steps': ['Inspect the learner view before requesting a justified refresh, then verify.'],
        }))


OBSERVED_SEQUENCE = ['inspect', 'verify_end_to_end', 'recover', 'verify_end_to_end', 'refresh_view']


class CountingTools(RecoveryTools):
    refresh_dispatches = 0
    def refresh_view(self, student_id, course_id):
        self.refresh_dispatches += 1
        return super().refresh_view(student_id, course_id)


def investigate(tmp_path, names=OBSERVED_SEQUENCE, status='escalated', extra_references=(), tools_class=CountingTools):
    path = tmp_path / 'reproduction.sqlite3'
    setup(path, 'combined')
    before = capture(path)
    model = ScriptedModel(names, status, extra_references)
    tools = tools_class(path)
    writer = TraceWriter(tmp_path / 'trace.jsonl')
    investigator = Investigator(model, tools, writer, 1, 1, Limits(max_repairs=2), verification_scope='learner_view')
    try:
        report = investigator.run('Please investigate why my next lesson is locked.')
    finally:
        writer.close()
    return report, model, tools, investigator, before, capture(path)


def test_observed_sequence_preserves_failure_citation_and_explanation(tmp_path):
    report, model, tools, investigator, before, after = investigate(tmp_path)
    failure = model.results[4].result
    assert failure['evidence_ref'] == 'tool-0005'
    assert failure['ok'] is False and failure['error_code'] == 'cache_refresh_not_justified'
    assert failure['missing_preconditions'] == ['inspect_view']
    assert tools.refresh_dispatches == 0
    assert investigator.repairs == 1 and investigator.calls == 5
    assert investigator.view_inspection_ref is None
    assert investigator.last_check['consistent'] is True
    assert investigator.view_check['consistent'] is False
    assert after['source_evidence'] == before['source_evidence']
    assert after['unrelated_students'] == before['unrelated_students']
    assert after['progress_verification']['consistent'] is True
    assert after['end_to_end_verification']['consistent'] is False
    assert report['status'] == 'escalated'
    assert report['diagnosis'] == 'Progress was repaired, but the learner view remains stale because the refresh request was rejected.'
    assert 'tool-0005' in report['evidence_references']
    assert report['remaining_limitations'][0] == 'The cache remains inconsistent; refresh was not performed.'
    assert report['actions_performed'][-1] == {
        'tool': 'refresh_view', 'evidence_ref': 'tool-0005', 'outcome': 'error',
        'error_code': 'cache_refresh_not_justified', 'missing_preconditions': ['inspect_view']}
    assert 'tool-0005' in investigator.observed_evidence
    assert 'tool-0005' not in investigator.evidence


def test_model_may_inspect_then_retry_rejected_refresh_without_extra_repair_budget(tmp_path):
    names = [*OBSERVED_SEQUENCE, 'inspect_view', 'refresh_view', 'verify_end_to_end']
    report, model, tools, investigator, before, after = investigate(tmp_path, names, status='recovered')
    assert report['status'] == 'recovered'
    assert report['verification_outcome']['consistent'] is True
    assert tools.refresh_dispatches == 1
    assert investigator.repairs == 2 and investigator.calls == 8
    assert model.results[4].result['missing_preconditions'] == ['inspect_view']
    assert report['actions_performed'][4]['outcome'] == 'error'
    assert report['actions_performed'][6]['outcome'] == 'success'
    assert before['source_evidence'] == after['source_evidence']
    assert before['unrelated_students'] == after['unrelated_students']
    assert after['learner_view']['lessons'][1]['eligible'] == 1
    assert after['learner_view']['lessons'][1]['completed'] == 0


def test_genuinely_unknown_reference_is_still_rejected_and_failure_code_survives(tmp_path):
    report, _, tools, _, _, _ = investigate(tmp_path, extra_references=('tool-9999',))
    assert report['status'] == 'escalated'
    assert report['diagnosis'] == 'The report contains missing or unsupported evidence references.'
    assert 'tool-9999' not in report['evidence_references']
    assert 'tool-0005' in report['evidence_references']
    assert report['actions_performed'][-1]['error_code'] == 'cache_refresh_not_justified'
    assert tools.refresh_dispatches == 0


def test_failed_refresh_citation_cannot_prove_recovery_success(tmp_path):
    # The model repeats the unsupported claim after the one permitted feedback.
    # ScriptedModel returns that claim again; no further mutation is requested.
    report, _, tools, investigator, _, after = investigate(tmp_path, status='recovered')
    assert report['status'] == 'escalated'
    assert report['diagnosis'] == 'The proposed recovery claim is not supported by executed repair and verification.'
    assert after['end_to_end_verification']['consistent'] is False
    assert tools.refresh_dispatches == 0 and investigator.repairs == 1


def test_failed_tool_result_is_citable_for_execution_failure(tmp_path):
    report, _, _, _, _, _ = investigate(tmp_path, status='execution_failed')
    assert report['status'] == 'execution_failed'
    assert 'tool-0005' in report['evidence_references']
    assert report['actions_performed'][-1]['missing_preconditions'] == ['inspect_view']


def test_citable_verification_error_cannot_replace_successful_verification(tmp_path):
    class VerificationError(CountingTools):
        verification_count = 0
        def verify_end_to_end(self, student_id, course_id):
            self.verification_count += 1
            if self.verification_count == 2:
                raise RuntimeError('private verification detail')
            return super().verify_end_to_end(student_id, course_id)
    names = ['inspect', 'inspect_view', 'verify_end_to_end', 'recover', 'verify_end_to_end']
    report, model, _, investigator, _, _ = investigate(tmp_path, names, status='recovered', tools_class=VerificationError)
    assert model.results[-1].result['error_code'] == 'tool_execution_failed'
    assert 'tool-0005' in investigator.observed_evidence
    assert 'tool-0005' not in investigator.evidence
    assert report['status'] == 'escalated'
    assert report['verification_outcome'] == {'checked': False}
    assert report['actions_performed'][-1]['error_code'] == 'tool_execution_failed'
