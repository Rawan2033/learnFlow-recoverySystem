"""Development-only evaluation checks. No held-out fixture or paid call is run."""
import copy
import json

import pytest

from learnflow.agent_config import Redactor, Settings
from learnflow.agent_contracts import Limits, ModelTurn, ToolCall
from learnflow.db import connect
from learnflow.eval_support.fixtures import assess, create_fixture, digest, reference, snapshot
from learnflow.eval_support.methods import BaselinePolicy
from learnflow.evaluation import (
    EvaluationTrace, attempt, file_hash, load_manifest, parser, provenance, recover_interrupted,
    run, score, write_json,
)
from learnflow.tools import RecoveryTools


DEVELOPMENT = load_manifest('development')[0]


def arguments(tmp_path, *extra):
    return parser().parse_args(['--output', str(tmp_path/'evaluation'), *extra])


def no_credentials():
    pytest.fail('Offline evaluation must not read credentials or contact a provider')


@pytest.fixture
def stable_provenance(monkeypatch):
    monkeypatch.setattr('learnflow.evaluation.provenance', lambda: {'git_commit': 'test-commit', 'git_dirty': True})


def test_split_counts_disjoint_configurations_and_frozen_digest():
    # Static manifest validation only: no held-out database/method is executed.
    heldout, frozen = load_manifest('heldout')
    assert len(DEVELOPMENT['cases']) == 12
    assert len(heldout['cases']) == 24 and len(frozen) == 64
    assert len(load_manifest('robustness')[0]['cases']) == 3
    def signature(case):
        defaults = dict(activity=True, linked=True, duplicates=0, downstream_evidence=False, missing_prerequisite=False)
        return digest({**defaults, **{k: v for k, v in case.items() if k != 'id'}})
    dev = {signature(c) for c in DEVELOPMENT['cases']}
    held = {signature(c) for c in heldout['cases']}
    assert len(dev) == 12 and len(held) == 24 and not dev & held


@pytest.mark.parametrize('index', range(12))
def test_development_fixture_observable_states_and_explicit_earned_completion(tmp_path, index):
    case = DEVELOPMENT['cases'][index]
    state = create_fixture(tmp_path/'case.sqlite3', case)
    target = state['expected'][case['lesson']-1]
    assert target['completed'] == int(index < 8)
    check = RecoveryTools(tmp_path/'case.sqlite3').verify_end_to_end(case['student'], 1)
    if case['family'] == 'stale-view':
        assert check['progress']['consistent'] is True
        assert check['learner_view']['consistent'] is False
    elif case['family'] == 'partial-update':
        assert check['progress']['consistent'] is False
        assert check['learner_view']['consistent'] is False
        assert assess(state)['view_correct'] is True  # Published truth precedes broken progress.
    elif case['family'] == 'healthy-control':
        assert check['consistent'] is True
    else:
        assert check['progress']['consistent'] is False
    if index == 8:
        assert [(r['quiz_passed'], r['eligible'], r['completed']) for r in state['expected']] == [(0, 1, 0), (0, 0, 0), (0, 0, 0)]
    if index == 9:
        assert [(r['quiz_passed'], r['eligible'], r['completed']) for r in state['expected']] == [(1, 1, 1), (0, 1, 0), (1, 0, 0)]
    if index == 10:
        assert (target['completion_seen'], target['quiz_passed'], target['eligible'], target['completed']) == (0, 1, 1, 0)
    if index == 11:
        assert (target['completion_seen'], target['quiz_passed'], target['eligible'], target['completed']) == (1, 1, 0, 0)


def test_reference_explicit_thresholds_links_and_any_pass_history():
    lessons = [{'id': 1, 'position': 1, 'passing_score': 70}]
    events = [{'student_id': 2, 'lesson_id': 1, 'kind': 'lesson_completed'},
              {'student_id': 2, 'lesson_id': 1, 'kind': 'quiz_recorded', 'quiz_result_id': 4}]
    for score_value, earned in ((69, 0), (70, 1), (71, 1)):
        quizzes = [{'id': 4, 'student_id': 2, 'lesson_id': 1, 'score': score_value}]
        assert reference(lessons, events, quizzes, 2)[0]['completed'] == earned
    assert reference(lessons, events[:1], quizzes, 2)[0]['completed'] == 0  # Unlinked quiz is insufficient.
    assert reference(lessons, events, quizzes, 1)[0]['completed'] == 0  # Other student's evidence.
    events.append({'student_id': 2, 'lesson_id': 1, 'kind': 'quiz_recorded', 'quiz_result_id': 5})
    quizzes.append({'id': 5, 'student_id': 2, 'lesson_id': 1, 'score': 0})
    assert reference(lessons, events, quizzes, 2)[0]['completed'] == 1  # Later failure does not erase a pass.


def test_baselines_development_results_identical_starting_states_and_resume(tmp_path, stable_provenance):
    args = arguments(tmp_path, '--max-cases', '12', '--repetitions', '2')
    summary = run(args, settings_loader=no_credentials)
    assert summary['recorded'] == 48 and summary['pending'] == 0
    assert summary['methods']['rule']['correct_repair']['numerator'] == 16
    assert summary['methods']['replay']['correct_repair']['numerator'] == 4
    for method in ('replay', 'rule'):
        metrics = summary['methods'][method]
        assert metrics['healthy_no_action']['numerator'] == metrics['healthy_no_action']['denominator'] == 8
        assert metrics['source_preservation']['numerator'] == metrics['unrelated_preservation']['numerator'] == 24
        assert metrics['incorrect_unlock_or_completion']['numerator'] == 0
        assert metrics['false_accepted_success']['numerator'] == 0
        assert metrics['token_usage']['total_tokens'] is None
    root = tmp_path/'evaluation'
    results = json.loads((root/'results.json').read_text())
    for case in DEVELOPMENT['cases']:
        assert len({r['starting_state_sha256'] for r in results if r['case_id'] == case['id']}) == 1
    completed = {p: file_hash(p) for p in root.glob('*/result.json')}
    args.resume = True
    assert run(args, settings_loader=no_credentials)['recorded'] == 48
    assert all(file_hash(p) == value for p, value in completed.items())
    assert len((root/'results.csv').read_text().splitlines()) == 49
    assert (root/'summary.md').read_text().startswith('# LearnFlow evaluation')


class MockFactory:
    def __init__(self, fail=False, unsupported=False, premature=False):
        self.models, self.fail, self.unsupported, self.premature = [], fail, unsupported, premature

    def __call__(self, settings, instructions):
        owner = self
        class MockModel:
            def __init__(self):
                self.policy, self.prompts, self.results = BaselinePolicy('rule'), [], []
                self.proposed_early = False
            def next_turn(self, prompt, results):
                self.prompts.append(prompt)
                self.results.extend(r.result for r in results)
                if owner.fail:
                    raise RuntimeError('test-private-api-key provider detail')
                if owner.unsupported:
                    return ModelTurn(text=json.dumps({'status': 'recovered', 'evidence_references': ['tool-9999'],
                        'diagnosis': 'Unsupported mock claim', 'remaining_limitations': [], 'recommended_next_steps': []}))
                turn = self.policy.next_turn(prompt, results)
                if owner.premature and not self.proposed_early and turn.calls and turn.calls[0].name == 'recover':
                    self.proposed_early = True
                    turn = ModelTurn(text=json.dumps({'status': 'recovered',
                        'evidence_references': [r['evidence_ref'] for r in self.results],
                        'diagnosis': 'Premature mocked claim', 'remaining_limitations': [], 'recommended_next_steps': []}))
                turn.usage = {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}
                return turn
            def close(self):
                pass
        model = MockModel()
        self.models.append(model)
        return model


def mocked_settings():
    return Settings('test-private-api-key', 'mock-model-not-live', Limits(max_repairs=2))


def test_mocked_ai_adapter_uses_existing_host_and_no_case_labels(tmp_path, stable_provenance):
    factory = MockFactory()
    args = arguments(tmp_path, '--methods', 'ai', '--allow-paid', '--max-cases', '2')
    summary = run(args, settings_loader=mocked_settings, model_factory=factory)
    assert summary['methods']['ai']['correct_repair']['numerator'] == 2
    assert summary['methods']['ai']['token_usage'] == {'reported_attempts': 2, 'total_tokens': 240}
    assert len(factory.models) == 2
    for student, model in zip((1, 2), factory.models):
        prompt = json.loads(model.prompts[0])
        assert prompt['student_id'] == student and prompt['course_id'] == 1
        assert prompt['verification_scope'] == 'learner_view'
        assert 'dev-0' not in json.dumps(model.results) and 'missed-processing' not in json.dumps(model.results)
        assert 'expected' not in prompt and 'family' not in prompt
    run_data = (tmp_path/'evaluation'/'run.json').read_text()
    assert 'test-private-api-key' not in run_data and 'mocked_ai' in run_data


def test_false_proposal_separate_from_accepted_report(tmp_path, stable_provenance):
    factory = MockFactory(unsupported=True)
    summary = run(arguments(tmp_path, '--methods', 'ai', '--allow-paid', '--max-cases', '1'), mocked_settings, factory)
    metrics = summary['methods']['ai']
    assert metrics['false_success_proposals'] == {'numerator': 1, 'denominator': 1}
    assert metrics['false_accepted_success']['numerator'] == 0
    assert metrics['statuses'] == {'escalated': 1}


def test_premature_proposal_scored_at_proposal_time_even_after_valid_recovery(tmp_path, stable_provenance):
    factory = MockFactory(premature=True)
    summary = run(arguments(tmp_path, '--methods', 'ai', '--allow-paid', '--max-cases', '1'), mocked_settings, factory)
    metrics = summary['methods']['ai']
    assert metrics['statuses'] == {'recovered': 1}
    assert metrics['correct_repair']['numerator'] == 1
    assert metrics['false_success_proposals'] == {'numerator': 1, 'denominator': 2}
    assert metrics['false_accepted_success']['numerator'] == 0


def test_api_error_stops_without_retry_and_keeps_failure(tmp_path, stable_provenance):
    factory = MockFactory(fail=True)
    args = arguments(tmp_path, '--methods', 'ai', '--allow-paid', '--max-cases', '2')
    summary = run(args, mocked_settings, factory)
    assert summary['recorded'] == 1 and summary['pending'] == 1
    assert summary['infrastructure_failures'] == 1
    assert len(factory.models) == 1 and len(factory.models[0].prompts) == 1
    result_path = tmp_path/'evaluation'/'dev-01-ai-1'/'result.json'
    original = file_hash(result_path)
    assert 'provider detail' not in result_path.read_text() and 'test-private-api-key' not in result_path.read_text()
    args.resume = True
    assert run(args, mocked_settings, factory)['recorded'] == 2
    assert file_hash(result_path) == original and len(factory.models) == 2


@pytest.mark.parametrize('index,failed_tool', [(0, 'recover'), (4, 'refresh_view'), (6, 'verify_end_to_end')])
def test_development_controlled_outages_escalate_bounded_and_preserve(tmp_path, index, failed_tool):
    # Use DEVELOPMENT fixtures with controlled failures, not held-out/robust manifests.
    case = {**DEVELOPMENT['cases'][index], 'failure_tool': failed_tool}
    result = attempt(tmp_path/'attempt', case, 'rule', DEVELOPMENT['symptom'], Limits(max_repairs=2))
    assert result['status'] == 'escalated'
    assert result['scores']['correct_escalation'] is True
    assert result['scores']['source_preserved'] and result['scores']['unrelated_preserved']
    assert result['tool_calls'] <= 4 and result['scores']['repair_attempts'] <= 1


def test_combined_exhausted_repair_budget_cannot_claim_success(tmp_path):
    result = attempt(tmp_path/'attempt', DEVELOPMENT['cases'][6], 'rule', DEVELOPMENT['symptom'], Limits(max_repairs=1))
    assert result['status'] == 'escalated' and result['scores']['repair_attempts'] == 1
    assert result['scores']['correct_repair'] is False
    assert result['scores']['progress_correct'] is True and result['scores']['view_correct'] is False


def test_oracle_detects_overgrant_and_never_redefines_truth_after_source_write(tmp_path):
    case = DEVELOPMENT['cases'][8]
    before = create_fixture(tmp_path/'case.sqlite3', case)
    with connect(tmp_path/'case.sqlite3') as db:
        db.execute('UPDATE quiz_results SET score=100')
        db.execute('UPDATE progress SET completed=1 WHERE student_id=1 AND lesson_id=1')
        db.execute('UPDATE progress SET eligible=1 WHERE student_id=1 AND lesson_id=2')
    after = snapshot(tmp_path/'case.sqlite3', 1)
    metrics = score(before, after, {'status': 'recovered'}, [], 1, case)
    assert metrics['source_preserved'] is False and metrics['false_accepted_success'] is True
    assert metrics['progress_correct'] is False
    assert metrics['overgrants'] == [{'layer': 'progress', 'lesson_id': 1, 'field': 'completed'},
                                     {'layer': 'progress', 'lesson_id': 2, 'field': 'eligible'}]
    after = copy.deepcopy(before)
    after['unrelated']['progress'][0]['completed'] = 1
    assert score(before, after, {'status': 'no_fault_found'}, [], 0, case)['unrelated_preserved'] is False


def test_interrupted_attempt_preserved_resume_runs_only_pending(tmp_path, stable_provenance, monkeypatch):
    args = arguments(tmp_path, '--methods', 'rule', '--max-cases', '2')
    from learnflow import evaluation
    execute = evaluation.execute_method
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(evaluation, 'execute_method', interrupted)
    summary = run(args, settings_loader=no_credentials)
    assert summary['recorded'] == 1 and summary['pending'] == 1
    completed = tmp_path/'evaluation'/'dev-01-rule-1'/'result.json'
    original = file_hash(completed)
    monkeypatch.setattr(evaluation, 'execute_method', execute)
    args.resume = True
    summary = run(args, settings_loader=no_credentials)
    assert summary['recorded'] == 2 and file_hash(completed) == original
    assert summary['methods']['rule']['correct_repair']['denominator'] == 2
    assert summary['methods']['rule']['correct_repair']['numerator'] == 1


def test_hard_interruption_retains_trace_usage_and_no_reexecution(tmp_path, stable_provenance, monkeypatch):
    args = arguments(tmp_path, '--methods', 'rule', '--max-cases', '1')
    run(args, settings_loader=no_credentials)
    root = tmp_path/'evaluation'
    directory = root/'dev-01-rule-1'
    (directory/'result.json').unlink()  # Simulate process death before its result checkpoint.
    trace_hash = file_hash(directory/'agent.jsonl')
    monkeypatch.setattr('learnflow.evaluation.attempt', lambda *a, **kw: pytest.fail('Interrupted attempts must not repeat'))
    args.resume = True
    summary = run(args, settings_loader=no_credentials)
    result = json.loads((directory/'result.json').read_text())
    assert result['status'] == 'interrupted' and result['error_code'] == 'prior_process_interrupted'
    assert result['tool_calls'] == 7 and result['elapsed_ms'] is None
    assert summary['methods']['rule']['elapsed_ms']['denominator'] == 0
    assert file_hash(directory/'agent.jsonl') == trace_hash


def test_resume_rejects_changed_code_or_configuration(tmp_path, stable_provenance, monkeypatch):
    args = arguments(tmp_path, '--methods', 'rule', '--max-cases', '1')
    run(args, settings_loader=no_credentials)
    original = file_hash(tmp_path/'evaluation'/'dev-01-rule-1'/'result.json')
    args.resume = True
    monkeypatch.setattr('learnflow.evaluation.provenance', lambda: {'git_commit': 'different'})
    with pytest.raises(ValueError, match='provenance changed'):
        run(args, settings_loader=no_credentials)
    assert file_hash(tmp_path/'evaluation'/'dev-01-rule-1'/'result.json') == original


def test_interrupted_in_flight_mutation_is_unknown_not_fabricated_zero(tmp_path):
    case = DEVELOPMENT['cases'][0]
    database = tmp_path/'learning.sqlite3'
    before = create_fixture(database, case)
    write_json(tmp_path/'before.json', before)
    trace = EvaluationTrace(tmp_path/'agent.jsonl', database, case['student'], Redactor())
    trace.record('tool_started', evidence_ref='tool-0001', tool='recover', validated_arguments={})
    trace.close()
    result = recover_interrupted(tmp_path, case, 'rule')
    assert result['scores']['repair_attempts'] is None
    assert result['pending_tool_calls'] == ['tool-0001'] and result['tool_calls'] == 1
    assert result['scores']['source_preserved'] is True


def test_paid_and_heldout_gates_precede_loading_or_execution(tmp_path):
    for flags in (['--methods', 'ai'], ['--methods', 'ai', '--allow-paid'], ['--split', 'heldout']):
        with pytest.raises(ValueError):
            run(arguments(tmp_path, *flags), settings_loader=no_credentials)
    assert not (tmp_path/'evaluation').exists()


def test_provenance_hashes_without_secrets():
    data = provenance()
    assert len(data['instructions_sha256']) == len(data['tool_schema_sha256']) == len(data['python_sources_sha256']) == 64
    assert isinstance(data['git_dirty'], bool) and len(data['git_commit']) >= 7
