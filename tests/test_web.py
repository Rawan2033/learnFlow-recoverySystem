"""Local API integration with real orchestration and explicitly mocked models."""
import json
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from learnflow.agent_config import ConfigurationError, Settings
from learnflow.agent_contracts import Limits, ModelTurn, ToolCall
from learnflow.db import connect
from learnflow.web import create_app


def settings():
    return Settings('unit-test-server-key', 'mock-model', Limits(max_repairs=2))


class ModelFactory:
    def __init__(self, names=None, gate=None, error=False):
        self.names = names or ['inspect', 'inspect_view', 'verify_end_to_end']
        self.gate, self.error, self.models = gate, error, []
    def __call__(self, configuration, instructions):
        owner = self
        class MockModel:
            index = 0
            def __init__(self):
                self.results, self.prompts = [], []
            def next_turn(self, prompt, results):
                self.prompts.append(prompt)
                if owner.gate and not owner.gate.wait(8):
                    raise RuntimeError('Test gate timed out')
                if owner.error:
                    raise RuntimeError('unit-test-server-key private provider response')
                self.results.extend(results)
                if self.index < len(owner.names):
                    name = owner.names[self.index]
                    self.index += 1
                    return ModelTurn(calls=[ToolCall(f'c{self.index}', name, '{}')])
                return ModelTurn(text=json.dumps({
                    'status': 'recovered' if 'recover' in owner.names else 'no_fault_found',
                    'evidence_references': [r.result['evidence_ref'] for r in self.results if r.result['ok']],
                    'diagnosis': 'Mocked test: conclusions follow the recorded evidence.',
                    'remaining_limitations': ['Mocked model test; no live AI call.'], 'recommended_next_steps': [],
                }))
            def close(self):
                pass
        model = MockModel()
        self.models.append(model)
        return model


def payload(student=1):
    return {'student_id': student, 'course_id': 1, 'symptom': 'Please investigate why my next lesson is locked.',
            'allow_paid': True, 'request_id': str(uuid4())}


def wait_run(client, run_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        state = client.get(f'/web/runs/{run_id}').json()
        if state['status'] != 'running':
            return state
        time.sleep(.02)
    pytest.fail('Mocked investigation did not finish')


def test_demo_disabled_no_credentials_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv('LEARNFLOW_DB', str(tmp_path/'normal.sqlite3'))
    def forbidden():
        pytest.fail('Read-only requests must not load credentials')
    client = TestClient(create_app(demo_mode=False, demo_root=tmp_path/'demo', settings_loader=forbidden), base_url='http://127.0.0.1:8000')
    assert client.get('/web/config').json()['demo_mode'] is False
    assert client.post('/web/demo/scenario', json={'scenario': 'combined'}).status_code == 404
    assert client.post('/web/runs', json=payload()).status_code == 404
    assert not (tmp_path/'demo').exists()


def test_paid_approval_schema_and_local_origin(tmp_path):
    calls = []
    def loader():
        calls.append(1)
        return settings()
    client = TestClient(create_app(True, tmp_path/'demo', loader, ModelFactory()), base_url='http://127.0.0.1:8000')
    assert client.get('/students').status_code == client.get('/courses').status_code == 200
    assert client.get('/web/config').json()['execution_mode'] == 'mocked_test'
    body = payload()
    assert client.post('/web/runs', json={**body, 'allow_paid': False}).status_code == 400
    assert client.post('/web/runs', json={**body, 'allow_paid': 'true'}).status_code == 422
    assert client.post('/web/runs', json={**body, 'db_path': 'old.sqlite3'}).status_code == 422
    assert client.post('/web/runs', json={**body, 'student_id': 999}).status_code == 404
    assert client.post('/web/runs', json={**body, 'request_id': 'x'*36}).status_code == 422
    assert client.post('/web/runs', json=body, headers={'Origin':'https://untrusted.example'}).status_code == 403
    assert client.get('/web/config', headers={'Host':'untrusted.example'}).status_code == 400
    assert client.post('/web/demo/scenario', json={'scenario':'unknown'}).status_code == 422
    assert calls == []


def test_active_run_blocks_resets_duplicate_submissions_and_demo_writes(tmp_path):
    gate = threading.Event()
    factory = ModelFactory(gate=gate)
    app = create_app(True, tmp_path/'demo', settings, factory)
    client = TestClient(app, base_url='http://127.0.0.1:8000')
    body = payload(student=2)
    response = client.post('/web/runs', json=body)
    assert response.status_code == 202
    run_id = response.json()['id']
    try:
        assert client.get('/web/runs/active').json()['run']['id'] == run_id
        assert client.post('/web/runs', json=payload()).status_code == 409
        assert client.post('/web/runs', json=body).json()['id'] == run_id
        assert client.post('/web/demo/scenario', json={'scenario':'combined'}).status_code == 409
        assert client.post('/events', json={'student_id':1,'lesson_id':1}).status_code == 409
        assert client.get(f'/web/runs/{run_id}').json()['status'] == 'running'
    finally:
        gate.set()
    result = wait_run(client, run_id)
    assert result['student_id'] == 2 and result['course_id'] == 1
    assert result['before']['learner_view'] == result['after']['learner_view']
    assert result['report']['status'] == 'no_fault_found'
    assert result['execution_mode'] == 'mocked_test'
    assert [e['tool'] for e in result['events'] if e['event']=='tool_started'] == ['inspect','inspect_view','verify_end_to_end']
    assert 'unit-test-server-key' not in json.dumps(result)
    assert client.get('/web/runs/active').json()['run'] is None
    assert client.post('/web/runs', json=body).json()['id'] == run_id
    assert len(factory.models) == 1
    assert client.post('/web/runs', json={**body, 'student_id':3}).status_code == 409


def test_combined_real_domain_repair_with_mocked_model_and_poll_snapshots(tmp_path):
    names = ['inspect','inspect_view','verify_end_to_end','recover','verify_end_to_end','refresh_view','verify_end_to_end']
    factory = ModelFactory(names)
    app = create_app(True, tmp_path/'demo', settings, factory)
    client = TestClient(app, base_url='http://127.0.0.1:8000')
    assert client.post('/web/demo/scenario', json={'scenario':'combined'}).status_code == 200
    source_before = client.get('/events').json(), client.get('/quiz-results').json()
    other_before = client.get('/students/2/courses/1/learner-view').json()
    cached_before = client.get('/students/1/courses/1/learner-view').json()
    with connect(app.state.demo_service.db_path) as db:
        assert db.execute('SELECT completed FROM progress WHERE student_id=1 AND lesson_id=1').fetchone()[0] == 0
    assert cached_before['lessons'][0]['completed'] == 1  # Dashboard must retain actual cached value.
    accepted = client.post('/web/runs', json=payload()).json()
    result = wait_run(client, accepted['id'])
    assert result['report']['status'] == 'recovered'
    assert result['after']['verification']['consistent'] is True
    assert result['before']['verification']['consistent'] is False
    assert result['after']['learner_view']['lessons'][1]['eligible'] == 1
    assert result['after']['learner_view']['lessons'][1]['completed'] == 0
    assert (client.get('/events').json(),client.get('/quiz-results').json()) == source_before
    assert client.get('/students/2/courses/1/learner-view').json() == other_before
    assert 'combined' not in factory.models[0].prompts[0]
    assert (tmp_path/'demo'/'runs'/accepted['id']/'agent.jsonl').exists()
    assert (tmp_path/'demo'/'runs'/accepted['id']/'after.json').exists()


def test_provider_failure_no_retry_and_safe_error_output(tmp_path):
    factory = ModelFactory(error=True)
    client = TestClient(create_app(True,tmp_path/'demo',settings,factory), base_url='http://127.0.0.1:8000')
    accepted = client.post('/web/runs', json=payload()).json()
    result = wait_run(client, accepted['id'])
    assert result['report']['status'] == 'execution_failed'
    assert result['before'] == result['after']
    assert len(factory.models) == 1 and len(factory.models[0].prompts) == 1
    assert 'private provider response' not in json.dumps(result)
    assert 'unit-test-server-key' not in json.dumps(result)


def test_config_error_before_run_and_marked_workspace_protection(tmp_path):
    def missing():
        raise ConfigurationError('private detail')
    app = create_app(True,tmp_path/'demo',missing,ModelFactory())
    client = TestClient(app, base_url='http://127.0.0.1:8000')
    response = client.post('/web/runs', json=payload())
    assert response.status_code == 503 and 'private detail' not in response.text
    assert app.state.demo_service.runs == {}
    old = tmp_path/'old-smoke'
    old.mkdir()
    (old/'learning.sqlite3').write_bytes(b'previous smoke evidence')
    with pytest.raises(ValueError, match='unmarked'):
        create_app(True,old,settings,ModelFactory())
    assert (old/'learning.sqlite3').read_bytes() == b'previous smoke evidence'
