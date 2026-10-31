import json

import pytest
from fastapi.testclient import TestClient

from learnflow.app import create_app
from learnflow.db import connect
from learnflow.scenarios import setup
from learnflow.tools import RecoveryTools


def snapshot(path):
    with connect(path) as db:
        return {table: [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY ' +
                                                   ('student_id,lesson_id' if table == 'progress' else '1'))]
                for table in ('events', 'quiz_results', 'progress', 'replay_receipts', 'operation_history')}


@pytest.fixture
def scenario(tmp_path):
    def build(name):
        path = tmp_path / 'demo.sqlite3'
        setup(path, name)
        return path, RecoveryTools(path)
    return build


@pytest.mark.parametrize('name', ['missed-processing', 'partial-update', 'healthy-control'])
def test_scenario_states_and_read_only_verification(scenario, name):
    path, tools = scenario(name)
    before = snapshot(path)
    state = tools.inspect(1, 1)
    rows = state['progress']
    assert len(state['events']) == 2 and len(state['quiz_results']) == 1
    assert [r['prerequisite_lesson_ids'] for r in state['requirements']] == [[], [1], [2]]
    assert [r['passing_score'] for r in state['requirements']] == [70, 70, 70]
    if name == 'missed-processing':
        assert all(e['processed_at'] is None for e in state['events'])
        assert rows[0]['completed'] == rows[0]['completion_seen'] == rows[0]['quiz_passed'] == 0
        assert rows[1]['eligible'] == 0
        assert state['processing_history'] == []
    elif name == 'partial-update':
        assert all(e['processed_at'] is not None for e in state['events'])
        assert rows[0]['completed'] == 1 and rows[1]['eligible'] == 0
        assert state['processing_history'][0]['operation'] == 'process_events'
    else:
        assert state['quiz_results'][0]['score'] == 40
        assert rows[0]['completion_seen'] == 1
        assert rows[0]['quiz_passed'] == rows[0]['completed'] == rows[1]['eligible'] == 0
    check = tools.verify(1, 1)
    assert check['consistent'] == (name == 'healthy-control')
    if name == 'partial-update':
        assert check['mismatches'] == [{'lesson_id': 2, 'field': 'eligible', 'actual': 0, 'expected': 1}]
    if name == 'missed-processing':
        assert { (m['lesson_id'], m['field'], m['expected']) for m in check['mismatches']} == {
            (1, 'completion_seen', 1), (1, 'quiz_passed', 1), (1, 'completed', 1), (2, 'eligible', 1)}
    assert snapshot(path) == before
    encoded = json.dumps(state)
    assert all(label not in encoded for label in ('missed-processing', 'partial-update', 'healthy-control'))


@pytest.mark.parametrize('name', ['missed-processing', 'partial-update', 'healthy-control'])
def test_recovery_preserves_sources_and_is_repeatable(scenario, name):
    path, tools = scenario(name)
    before = snapshot(path)
    assert tools.recover(1, 1)['outcome'] == 'success'
    first = snapshot(path)
    assert first['events'] == before['events']
    assert first['quiz_results'] == before['quiz_results']
    assert [r for r in first['progress'] if r['student_id'] != 1] == [r for r in before['progress'] if r['student_id'] != 1]
    rows = tools.read_progress(1, 1)
    earned = int(name != 'healthy-control')
    assert [(r['completion_seen'], r['quiz_passed'], r['eligible'], r['completed']) for r in rows] == [
        (1, earned, 1, earned), (0, 0, earned, 0), (0, 0, 0, 0)]
    assert tools.verify(1, 1)['consistent']
    assert tools.recover(1, 1)['changed'] is False
    second = snapshot(path)
    for table in ('events', 'quiz_results', 'progress', 'replay_receipts'):
        assert second[table] == first[table]
    audits = [h for h in tools.read_processing_history(1, 1) if h['operation'] == 'recover_course']
    assert len(audits) == 2
    assert audits[0]['before_state'] == [r for r in before['progress'] if r['student_id'] == 1]
    assert audits[0]['after_state'] == rows
    assert audits[1]['before_state'] == audits[1]['after_state']
    # Subsequent ordinary processing must retain recovered evidence.
    client = TestClient(create_app(path))
    assert client.post('/events/1/process').status_code == 200
    assert tools.read_progress(1, 1) == rows


def test_scope_includes_course_and_failures_are_audited(scenario):
    path, tools = scenario('missed-processing')
    with connect(path) as db:
        db.execute("INSERT INTO courses VALUES (2,'Other course')")
        db.execute("INSERT INTO lessons VALUES (4,2,1,'Other lesson',70)")
        db.execute('INSERT INTO progress VALUES (1,4,0,0,1,0)')
        db.execute("INSERT INTO events(student_id,lesson_id,kind) VALUES (1,4,'lesson_completed')")
    before = snapshot(path)
    result = tools.recover(1, 999)
    assert result['outcome'] == 'error' and result['error_code'] == 'unknown_course'
    failed = snapshot(path)
    assert failed['progress'] == before['progress'] and failed['replay_receipts'] == []
    assert failed['operation_history'][-1]['outcome'] == 'error'
    tools.recover(1, 1)
    after = snapshot(path)
    assert [r for r in after['progress'] if r['lesson_id'] == 4] == [r for r in before['progress'] if r['lesson_id'] == 4]
    assert 3 not in tools.inspect(1, 1)['replayed_event_ids']
    assert after['events'] == before['events']


def test_missing_rows_and_unsupported_completion(scenario):
    path, tools = scenario('healthy-control')
    with connect(path) as db:
        db.execute('DELETE FROM progress WHERE student_id=1 AND lesson_id=3')
        db.execute('UPDATE progress SET completed=1 WHERE student_id=1 AND lesson_id=1')
    check = tools.verify(1, 1)
    assert {'lesson_id': 1, 'field': 'completed', 'actual': 1, 'expected': 0} in check['mismatches']
    assert {'lesson_id': 3, 'field': 'eligible', 'actual': None, 'expected': 0} in check['mismatches']
    tools.recover(1, 1)
    assert tools.verify(1, 1)['consistent']
    assert tools.read_progress(1, 1)[0]['completed'] == 0


def test_recovery_rolls_back_and_logs_sanitized_error(scenario):
    path, tools = scenario('missed-processing')
    with connect(path) as db:
        db.execute("""CREATE TRIGGER reject_projection BEFORE UPDATE ON progress
            BEGIN SELECT RAISE(ABORT, 'private database detail'); END""")
    before = snapshot(path)
    result = tools.recover(1, 1)
    assert result['outcome'] == 'error' and result['error_code'] == 'database_error'
    after = snapshot(path)
    for table in ('events', 'quiz_results', 'progress', 'replay_receipts'):
        assert after[table] == before[table]
    history = tools.read_processing_history(1, 1)
    assert history[-1]['before_state'] == history[-1]['after_state']
    assert 'private database detail' not in json.dumps(history)


@pytest.mark.parametrize('name', ['missed-processing', 'partial-update', 'healthy-control'])
def test_scenario_setup_and_reset_are_repeatable(scenario, name):
    from learnflow.db import reset
    path, tools = scenario(name)
    initial = tools.read_progress(1, 1)
    tools.recover(1, 1)
    setup(path, name)
    assert tools.read_progress(1, 1) == initial
    assert all(r['operation'] != 'recover_course' for r in tools.read_processing_history(1, 1))
    reset(path)
    reset(path)
    assert tools.read_activity_events(1, 1) == tools.read_quiz_results(1, 1) == []
    assert tools.read_processing_history(1, 1) == []
    assert tools.verify(1, 1)['consistent']


def test_recovery_requires_completion_and_prior_lessons(scenario):
    path, tools = scenario('missed-processing')
    with connect(path) as db:
        db.execute('DELETE FROM events WHERE id=1')
        db.execute('INSERT INTO quiz_results VALUES (2,1,2,100,CURRENT_TIMESTAMP)')
        db.execute("INSERT INTO events(student_id,lesson_id,kind) VALUES (1,2,'lesson_completed')")
        db.execute("INSERT INTO events(student_id,lesson_id,kind,quiz_result_id) VALUES (1,2,'quiz_recorded',2)")
    assert tools.recover(1, 1)['outcome'] == 'success'
    rows = tools.read_progress(1, 1)
    assert [(r['completion_seen'], r['quiz_passed'], r['eligible'], r['completed']) for r in rows] == [
        (0, 1, 1, 0), (1, 1, 0, 0), (0, 0, 0, 0)]
    assert tools.verify(1, 1)['consistent']
