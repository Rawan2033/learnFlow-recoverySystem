"""Mocked model choices, not live-model evaluation or scenario-aware agent logic."""
import json

import pytest

from learnflow.agent import Investigator
from learnflow.agent_config import Settings
from learnflow.agent_contracts import Limits, ModelTurn, ToolCall
from learnflow.agent_trace import TraceWriter
from learnflow.db import connect
from learnflow.scenarios import setup
from learnflow.tools import RecoveryTools


class ScriptedModel:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.results = []

    def next_turn(self, prompt, results):
        self.results.extend(results)
        return next(self.turns)


def calls(names):
    return [ModelTurn(calls=[ToolCall(f'c{i}', name, '{}')]) for i, name in enumerate(names, 1)]


def finish(status, references):
    return ModelTurn(text=json.dumps({'status': status, 'evidence_references': references,
                                     'diagnosis': 'The cited checks distinguish derived progress from the learner snapshot.',
                                     'remaining_limitations': [], 'recommended_next_steps': []}))


def snapshot(path):
    with connect(path) as db:
        return {table: [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('events', 'quiz_results', 'progress', 'learner_views',
                              'replay_receipts', 'operation_history')}


@pytest.fixture
def run(tmp_path):
    def execute(scenario, names, status, references, limits=None):
        path = tmp_path / 'agent.sqlite3'
        setup(path, scenario)
        before = snapshot(path)
        trace_path = tmp_path / 'trace.jsonl'
        writer = TraceWriter(trace_path)
        model = ScriptedModel([*calls(names), finish(status, references)])
        tools = RecoveryTools(path)
        try:
            report = Investigator(model, tools, writer, 1, 1,
                                  limits or Limits(max_repairs=2), verification_scope='learner_view').run(
                                      'Please investigate why my next lesson is locked.')
        finally:
            writer.close()
        return report, model, tools, before, snapshot(path), trace_path.read_text()
    return execute


@pytest.mark.parametrize('scenario,names,expected_mutations,expected_status', [
    ('stale-view', ['inspect', 'inspect_view', 'verify_end_to_end', 'refresh_view', 'verify_end_to_end'],
     ['refresh_view'], 'recovered'),
    ('combined', ['inspect', 'inspect_view', 'verify_end_to_end', 'recover', 'verify_end_to_end',
                  'refresh_view', 'verify_end_to_end'], ['recover', 'refresh_view'], 'recovered'),
    ('partial-update', ['inspect', 'inspect_view', 'verify_end_to_end', 'recover', 'verify_end_to_end'],
     ['recover'], 'recovered'),
    ('healthy-control', ['inspect', 'inspect_view', 'verify_end_to_end'], [], 'no_fault_found'),
    ('missed-processing', ['inspect', 'inspect_view', 'verify_end_to_end', 'recover', 'verify_end_to_end',
                           'refresh_view', 'verify_end_to_end'], ['recover', 'refresh_view'], 'recovered'),
])
def test_agent_selects_only_justified_operations(run, scenario, names, expected_mutations, expected_status):
    refs = [f'tool-{i:04d}' for i in range(1, len(names) + 1)]
    report, model, tools, before, after, trace = run(scenario, names, expected_status, refs)
    assert report['status'] == expected_status
    assert report['verification_scope'] == 'learner_view'
    assert report['verification_outcome']['consistent'] is True
    assert report['progress_verification_outcome']['consistent'] is True
    assert report['learner_view_verification_outcome']['consistent'] is True
    assert report['browser_ui_verified'] is False and report['reported_symptom_resolved'] is None
    assert [a['tool'] for a in report['actions_performed'] if a['tool'] in ('recover', 'refresh_view')] == expected_mutations
    assert all(a['outcome'] == 'success' for a in report['actions_performed'])
    assert after['events'] == before['events'] and after['quiz_results'] == before['quiz_results']
    assert [r for r in after['progress'] if r['student_id'] != 1] == [r for r in before['progress'] if r['student_id'] != 1]
    assert [r for r in after['learner_views'] if r['student_id'] != 1] == [r for r in before['learner_views'] if r['student_id'] != 1]
    if expected_status == 'no_fault_found':
        assert after == before
        assert tools.inspect_view(1, 1)['lessons'][0]['completed'] == 0
        assert tools.inspect_view(1, 1)['lessons'][1]['eligible'] == 0
    assert all(label not in trace for label in ('missed-processing', 'partial-update', 'healthy-control', 'stale-view', 'combined'))


def test_progress_only_verification_cannot_claim_learner_path_recovered(run):
    report, _, _, _, _, _ = run('combined',
        ['inspect', 'inspect_view', 'verify', 'recover', 'verify'], 'recovered',
        ['tool-0001', 'tool-0002', 'tool-0004', 'tool-0005'])
    assert report['status'] == 'escalated'
    assert report['verification_outcome'] == {'checked': False}


def test_cache_alone_pass_is_not_end_to_end_success(run):
    report, _, _, _, _, _ = run('combined',
        ['inspect', 'inspect_view', 'verify_end_to_end'], 'no_fault_found',
        ['tool-0001', 'tool-0002', 'tool-0003'])
    assert report['status'] == 'escalated'
    assert report['verification_outcome']['progress']['consistent'] is False


def test_refresh_is_rejected_until_progress_is_correct(run):
    report, model, _, before, after, _ = run('combined',
        ['inspect', 'inspect_view', 'verify_end_to_end', 'refresh_view'], 'escalated',
        ['tool-0001', 'tool-0002', 'tool-0003'])
    assert model.results[-1].result['error_code'] == 'cache_refresh_not_justified'
    assert after == before and report['status'] == 'escalated'


def test_combined_case_exhausts_explicit_one_repair_budget(run):
    report, model, _, _, _, _ = run('combined',
        ['inspect', 'inspect_view', 'verify_end_to_end', 'recover', 'verify_end_to_end', 'refresh_view'],
        'recovered', ['tool-0001', 'tool-0002', 'tool-0004', 'tool-0005'], Limits(max_repairs=1))
    assert model.results[-1].result['error_code'] == 'repair_budget_exhausted'
    assert report['status'] == 'escalated'
    assert report['verification_outcome']['progress']['consistent'] is True
    assert report['verification_outcome']['learner_view']['consistent'] is False


def test_view_inspection_required_before_refresh(run):
    report, model, _, before, after, _ = run('stale-view',
        ['inspect', 'verify_end_to_end', 'refresh_view'], 'escalated',
        ['tool-0001', 'tool-0002'])
    assert model.results[-1].result['error_code'] == 'cache_refresh_not_justified'
    assert after == before and report['status'] == 'escalated'


def test_old_progress_only_claim_cannot_hide_stale_view(run):
    report, _, _, _, _, _ = run('stale-view', ['inspect', 'verify'],
                               'no_fault_found', ['tool-0001', 'tool-0002'])
    assert report['status'] == 'escalated'


def test_runtime_default_allows_two_distinct_repairs(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-only')
    monkeypatch.setenv('OPENAI_MODEL', 'mock-model')
    monkeypatch.delenv('LEARNFLOW_MAX_REPAIRS', raising=False)
    settings = Settings.from_environment(tmp_path / 'missing.env')
    assert settings.limits.max_repairs == 2


def test_healthy_case_rejects_unnecessary_cache_mutation(run):
    report, model, _, before, after, _ = run('healthy-control',
        ['inspect', 'inspect_view', 'verify_end_to_end', 'refresh_view', 'recover'], 'no_fault_found',
        ['tool-0001', 'tool-0002', 'tool-0003'])
    assert model.results[-2].result['error_code'] == 'cache_refresh_not_justified'
    assert model.results[-1].result['error_code'] == 'repair_not_justified'
    assert report['status'] == 'no_fault_found' and after == before


@pytest.mark.parametrize('tool', ['inspect_view', 'refresh_view', 'verify_end_to_end'])
def test_new_tools_keep_scope_outside_model_control(tmp_path, tool):
    path = tmp_path / 'agent.sqlite3'
    setup(path, 'stale-view')
    before = snapshot(path)
    model = ScriptedModel([
        ModelTurn(calls=[ToolCall('c1', tool, '{"student_id":2,"course_id":1}')]),
        finish('execution_failed', []),
    ])
    writer = TraceWriter(tmp_path / 'trace.jsonl')
    try:
        report = Investigator(model, RecoveryTools(path), writer, 1, 1,
                              verification_scope='learner_view').run('Locked lesson')
    finally:
        writer.close()
    assert model.results[-1].result['error_code'] == 'invalid_arguments'
    assert report['status'] == 'execution_failed' and snapshot(path) == before
