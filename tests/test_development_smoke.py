"""Offline runner tests: scripted models only; no live calls or real credentials."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from learnflow import development_smoke as smoke
from learnflow.agent_config import ConfigurationError, Settings
from learnflow.agent_contracts import Limits, ModelTurn, ToolCall
from learnflow.db import connect


STALE_PLAN = ['inspect', 'inspect_view', 'verify_end_to_end', 'refresh_view', 'verify_end_to_end']
COMBINED_PLAN = ['inspect', 'inspect_view', 'verify_end_to_end', 'recover', 'verify_end_to_end',
                 'refresh_view', 'verify_end_to_end']


@pytest.fixture(autouse=True)
def forbid_real_provider(monkeypatch):
    import learnflow.openai_provider as provider
    def forbidden(*args, **kwargs):
        raise AssertionError('Runner unit tests must never construct a live provider')
    monkeypatch.setattr(provider, 'OpenAIResponsesModel', forbidden)


@pytest.fixture
def settings():
    return Settings('unit-test-credential', 'offline-test-model', Limits(max_repairs=2))


def scripted_factory(plans):
    remaining = iter(plans)
    models = []
    class ScriptedModel:
        def __init__(self, plan):
            self.plan = plan
            self.index = 0
            self.prompts = []
            self.results = []
            self.closed = False
        def next_turn(self, prompt, results):
            self.prompts.append(prompt)
            self.results.extend(results)
            if isinstance(self.plan, Exception):
                raise self.plan
            usage = {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}
            if self.index < len(self.plan):
                name = self.plan[self.index]
                self.index += 1
                return ModelTurn(calls=[ToolCall(f'c{self.index}', name, '{}')], usage=usage)
            refs = [r.result['evidence_ref'] for r in self.results if r.result['ok']]
            return ModelTurn(text=json.dumps({'status': 'recovered', 'evidence_references': refs,
                'diagnosis': 'Cited checks support the backend repair.',
                'remaining_limitations': [], 'recommended_next_steps': []}), usage=usage)
        def close(self):
            self.closed = True
    def factory(configuration, instructions):
        model = ScriptedModel(next(remaining))
        models.append(model)
        return model
    return factory, models


def test_both_cases_reuse_agent_and_preserve_state(settings, tmp_path):
    factory, models = scripted_factory([STALE_PLAN, COMBINED_PLAN])
    summary = smoke.run_suite(settings, tmp_path, factory)
    assert summary['passed'] and summary['held_out_evaluation'] is False
    assert summary['kind'] == 'development_smoke_tests'
    assert summary['model_name'] == 'offline-test-model'
    assert summary['skipped_cases'] == [] and len(models) == 2
    assert all(model.closed for model in models)
    assert [c['case'] for c in summary['cases']] == ['stale-view', 'combined']
    for case, plan in zip(summary['cases'], [STALE_PLAN, COMBINED_PLAN]):
        assert case['passed'] and all(case['checks'].values())
        assert case['agent_status'] == 'recovered'
        assert case['successful_repair_tools'] == smoke.EXPECTED_REPAIRS[case['case']]
        assert case['repair_tools_requested'] == smoke.EXPECTED_REPAIRS[case['case']]
        assert case['token_usage']['provided'] is True
        assert case['token_usage']['total_tokens'] == (len(plan) + 1) * 15
        assert case['investigation_elapsed_seconds'] >= 0
        assert case['case_elapsed_seconds'] >= case['investigation_elapsed_seconds']
        assert case['progress_verification_before']['consistent'] == (case['case'] == 'stale-view')
        assert case['end_to_end_verification_before']['consistent'] is False
        assert case['end_to_end_verification_before']['learner_view']['consistent'] is False
        assert case['independent_end_to_end_verification_after']['consistent'] is True
        directory = Path(case['database']).parent
        before = json.loads((directory / 'before.json').read_text())
        after = json.loads((directory / 'after.json').read_text())
        assert before['source_evidence'] == after['source_evidence']
        assert before['unrelated_students'] == after['unrelated_students']
        assert after['progress'][1]['eligible'] == 1 and after['progress'][1]['completed'] == 0
        assert after['learner_view']['lessons'][1]['eligible'] == 1
        assert after['learner_view']['lessons'][1]['completed'] == 0
        assert Path(case['trace_file']).is_file()
    # Labels/answers live only in runner artifacts, never in inputs sent to a model.
    for model in models:
        payload = json.dumps([model.prompts, [r.result for r in model.results]])
        assert 'stale-view' not in payload and 'combined' not in payload
        assert 'expected_repair_tools' not in payload
        assert smoke.SYMPTOM in model.prompts[0]
    artifact = (Path(summary['run_directory']) / 'summary.json').read_text()
    assert 'unit-test-credential' not in artifact


def test_each_explicit_run_is_fresh_and_never_resets_previous_databases(settings, tmp_path):
    old = tmp_path / 'old-smoke.sqlite3'
    old.write_bytes(b'previous smoke must remain untouched')
    factory, _ = scripted_factory([STALE_PLAN, COMBINED_PLAN, STALE_PLAN, COMBINED_PLAN])
    first = smoke.run_suite(settings, tmp_path, factory)
    previous_bytes = {c['database']: Path(c['database']).read_bytes() for c in first['cases']}
    second = smoke.run_suite(settings, tmp_path, factory)
    assert first['run_directory'] != second['run_directory']
    assert all(Path(path).read_bytes() == data for path, data in previous_bytes.items())
    assert old.read_bytes() == b'previous smoke must remain untouched'


def test_api_error_stops_before_second_case_without_retry(settings, tmp_path):
    factory, models = scripted_factory([RuntimeError('unit-test-credential private API body'), COMBINED_PLAN])
    summary = smoke.run_suite(settings, tmp_path, factory)
    assert not summary['passed'] and len(models) == 1
    assert summary['skipped_cases'] == ['combined']
    case = summary['cases'][0]
    assert case['agent_status'] == 'execution_failed'
    assert case['checks']['source_evidence_unchanged'] and case['checks']['unrelated_students_unchanged']
    assert case['independent_end_to_end_verification_after']['consistent'] is False
    assert case['token_usage']['provided'] is False and case['token_usage']['total_tokens'] is None
    artifact = (Path(summary['run_directory']) / 'summary.json').read_text()
    assert 'private API body' not in artifact and 'unit-test-credential' not in artifact
    assert len(models[0].prompts) == 1


def test_second_case_failure_is_not_retried(settings, tmp_path):
    factory, models = scripted_factory([STALE_PLAN, RuntimeError('API failure')])
    summary = smoke.run_suite(settings, tmp_path, factory)
    assert not summary['passed'] and len(models) == 2
    assert summary['cases'][0]['passed'] and not summary['cases'][1]['passed']
    assert len(models[1].prompts) == 1


def test_constructor_failure_is_sanitized_and_independently_checked(settings, tmp_path):
    constructions = []
    def broken(*args):
        constructions.append(1)
        raise RuntimeError('unit-test-credential private constructor details')
    summary = smoke.run_suite(settings, tmp_path, broken)
    assert len(constructions) == 1 and summary['skipped_cases'] == ['combined']
    case = summary['cases'][0]
    assert case['agent_status'] == 'execution_failed'
    assert 'independent_end_to_end_verification_after' in case
    assert 'private constructor details' not in json.dumps(summary)


def test_configuration_preflight_before_any_paid_case(settings, tmp_path):
    settings.limits = Limits(max_repairs=1)
    factory, models = scripted_factory([STALE_PLAN])
    with pytest.raises(ConfigurationError, match='MAX_REPAIRS >= 2'):
        smoke.run_suite(settings, tmp_path, factory)
    assert models == [] and list(tmp_path.iterdir()) == []


def test_existing_run_directory_is_never_overwritten(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, 'uuid4', lambda: SimpleNamespace(hex='existing-run'))
    existing = tmp_path / 'existing-run'
    existing.mkdir()
    sentinel = existing / 'learning.sqlite3'
    sentinel.write_bytes(b'preserve this database')
    factory, models = scripted_factory([STALE_PLAN])
    with pytest.raises(FileExistsError):
        smoke.run_suite(settings, tmp_path, factory)
    assert sentinel.read_bytes() == b'preserve this database' and models == []


def test_bad_initial_state_stops_before_model(settings, tmp_path, monkeypatch):
    original = smoke.capture
    def incorrect(path):
        data = original(path)
        data['progress_verification']['consistent'] = False
        return data
    monkeypatch.setattr(smoke, 'capture', incorrect)
    factory, models = scripted_factory([STALE_PLAN])
    summary = smoke.run_suite(settings, tmp_path, factory)
    assert models == [] and not summary['passed']
    assert summary['cases'][0]['report']['error_code'] == 'unexpected_initial_state'


def test_unnecessary_recover_request_fails_development_expectation(settings, tmp_path):
    plan = ['inspect', 'inspect_view', 'verify_end_to_end', 'recover', 'refresh_view', 'verify_end_to_end']
    factory, models = scripted_factory([plan, COMBINED_PLAN])
    summary = smoke.run_suite(settings, tmp_path, factory)
    case = summary['cases'][0]
    assert case['agent_status'] == 'recovered'  # Guard rejected unnecessary replay; refresh succeeded.
    assert case['checks']['independent_end_to_end_consistent'] is True
    assert case['checks']['expected_repair_sequence'] is False
    assert not summary['passed'] and len(models) == 1
    assert case['repair_tools_requested'] == ['recover', 'refresh_view']


@pytest.mark.parametrize('sql,failed_check', [
    ('UPDATE quiz_results SET score=95 WHERE id=1', 'source_evidence_unchanged'),
    ('UPDATE progress SET completed=1 WHERE student_id=2 AND lesson_id=1', 'unrelated_students_unchanged'),
    ('UPDATE learner_views SET version=version+1 WHERE student_id=2', 'unrelated_students_unchanged'),
])
def test_preservation_failures_are_detected(settings, tmp_path, monkeypatch, sql, failed_check):
    original = smoke.investigate_once
    def corrupt(settings, db_path, trace_path, factory):
        result = original(settings, db_path, trace_path, factory)
        with connect(db_path) as db:
            db.execute(sql)
        return result
    monkeypatch.setattr(smoke, 'investigate_once', corrupt)
    factory, models = scripted_factory([STALE_PLAN, COMBINED_PLAN])
    summary = smoke.run_suite(settings, tmp_path, factory)
    assert summary['cases'][0]['checks'][failed_check] is False
    assert not summary['passed'] and len(models) == 1


def test_cli_requires_opt_in_before_configuration_or_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke.Settings, 'from_environment', lambda *args: pytest.fail('No config should be read'))
    with pytest.raises(SystemExit) as error:
        smoke.main(['--output-root', str(tmp_path)])
    assert error.value.code == 2 and list(tmp_path.iterdir()) == []


def test_cli_configuration_error_without_paid_calls(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    monkeypatch.setenv('OPENAI_MODEL', '')
    assert smoke.main(['--allow-paid', '--env-file', str(tmp_path/'missing.env'), '--output-root', str(tmp_path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['error_code'] == 'configuration_error'
    assert list(tmp_path.iterdir()) == []


def test_cli_success_with_offline_models(settings, tmp_path, monkeypatch, capsys):
    original = smoke.run_suite
    factory, _ = scripted_factory([STALE_PLAN, COMBINED_PLAN])
    monkeypatch.setattr(smoke.Settings, 'from_environment', lambda *args: settings)
    monkeypatch.setattr(smoke, 'run_suite', lambda cfg, root: original(cfg, root, factory))
    assert smoke.main(['--allow-paid', '--output-root', str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['passed'] and len(result['cases']) == 2


def test_combined_only_uses_fresh_database_and_one_investigation(settings, tmp_path):
    factory, models = scripted_factory([COMBINED_PLAN])
    summary = smoke.run_suite(settings, tmp_path, factory, selected_cases=('combined',))
    assert summary['passed'] and summary['selected_cases'] == ['combined']
    assert len(models) == 1 and [c['case'] for c in summary['cases']] == ['combined']
    assert summary['skipped_cases'] == []
    assert not (Path(summary['run_directory']) / 'stale-view').exists()


def test_combined_only_cli_keeps_paid_opt_in_and_scope(settings, tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as error:
        smoke.main(['--case', 'combined', '--output-root', str(tmp_path)])
    assert error.value.code == 2
    original = smoke.run_suite
    factory, models = scripted_factory([COMBINED_PLAN])
    monkeypatch.setattr(smoke.Settings, 'from_environment', lambda *args: settings)
    monkeypatch.setattr(smoke, 'run_suite', lambda cfg, root, selected_cases: original(cfg, root, factory, selected_cases))
    capsys.readouterr()
    assert smoke.main(['--allow-paid', '--case', 'combined', '--output-root', str(tmp_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['selected_cases'] == ['combined'] and len(models) == 1
