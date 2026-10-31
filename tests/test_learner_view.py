"""Independent expected values for source -> progress -> learner-view behavior."""
import json

import pytest
from fastapi.testclient import TestClient

from learnflow.app import create_app
from learnflow.db import connect, initialize, reset
from learnflow.scenarios import setup
from learnflow.tools import RecoveryTools


def snapshot(path):
    with connect(path) as db:
        return {table: [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('students', 'courses', 'lessons', 'events', 'quiz_results',
                              'progress', 'learner_views', 'replay_receipts', 'operation_history')}


@pytest.fixture
def demo(tmp_path):
    def build(scenario):
        path = tmp_path / 'cache.sqlite3'
        setup(path, scenario)
        return path, RecoveryTools(path)
    return build


def test_seed_read_and_additive_initialization(tmp_path):
    path = tmp_path / 'cache.sqlite3'
    reset(path)
    tools = RecoveryTools(path)
    initial = snapshot(path)
    view = tools.inspect_view(1, 1)
    assert view['initialized'] and view['version'] == 1 and view['refreshed_at']
    assert len(view['progress_fingerprint']) == 64
    assert [(r['completed'], r['eligible']) for r in view['lessons']] == [(0, 1), (0, 0), (0, 0)]
    client = TestClient(create_app(path))
    endpoint = '/students/1/courses/1/learner-view'
    assert client.get(endpoint).json() == view
    assert client.get('/students/999/courses/1/learner-view').status_code == 404
    assert client.get('/students/1/courses/999/learner-view').status_code == 404
    assert snapshot(path) == initial  # Reads and startup must not publish implicitly.
    with connect(path) as db:
        db.execute('DELETE FROM learner_views WHERE student_id=1 AND course_id=1')
    initialize(path)  # Additive schema initialization never fabricates cache data.
    absent = tools.inspect_view(1, 1)
    assert absent['initialized'] is False and absent['version'] == 0
    assert client.get(endpoint).json() == absent
    check = tools.verify_end_to_end(1, 1)
    assert check['progress']['consistent'] is True
    assert check['learner_view']['mismatches'] == [{'field': 'initialized', 'actual': False, 'expected': True}]
    assert check['consistent'] is False
    assert tools.refresh_view(1, 1)['version'] == 1
    assert tools.verify_end_to_end(1, 1)['consistent'] is True


def test_normal_processing_explicitly_publishes_and_logs(tmp_path):
    path = tmp_path / 'cache.sqlite3'
    reset(path)
    tools = RecoveryTools(path)
    client = TestClient(create_app(path))
    initial = tools.inspect_view(1, 1)
    payload = {'student_id': 1, 'lesson_id': 1}
    completion = client.post('/events', json=payload).json()['id']
    quiz = client.post('/quiz-results', json={**payload, 'score': 90}).json()['event_id']
    assert tools.inspect_view(1, 1) == initial  # Ingestion still does not process/publish.
    client.post(f'/events/{completion}/process')
    intermediate = tools.inspect_view(1, 1)
    assert intermediate['version'] == 2
    assert intermediate['lessons'][0]['completion_seen'] == 1
    assert intermediate['lessons'][0]['completed'] == 0
    client.post(f'/events/{quiz}/process')
    published = tools.inspect_view(1, 1)
    assert published['version'] == 3
    assert [r['eligible'] for r in published['lessons']] == [1, 1, 0]
    assert [r['completed'] for r in published['lessons']] == [1, 0, 0]
    assert tools.verify_end_to_end(1, 1)['consistent'] is True
    history = tools.read_processing_history(1, 1)
    assert [r['operation'] for r in history] == [
        'process_event', 'refresh_learner_view', 'process_event', 'refresh_learner_view']
    client.post(f'/events/{quiz}/process')
    assert tools.inspect_view(1, 1) == published
    assert tools.read_processing_history(1, 1)[-1]['before_state'] == tools.read_processing_history(1, 1)[-1]['after_state']


def test_stale_view_correct_progress_and_refresh_only(demo):
    path, tools = demo('stale-view')
    before = snapshot(path)
    check = tools.verify_end_to_end(1, 1)
    assert tools.verify(1, 1)['consistent'] is True
    assert check['progress']['consistent'] is True and check['learner_view']['consistent'] is False
    assert tools.read_progress(1, 1)[0]['completed'] == 1
    assert tools.inspect_view(1, 1)['lessons'][0]['completed'] == 0
    assert tools.read_progress(1, 1)[1]['eligible'] == 1
    assert tools.inspect_view(1, 1)['lessons'][1]['eligible'] == 0
    assert snapshot(path) == before  # Verification is read-only.
    result = tools.refresh_view(1, 1)
    assert result['changed'] is True and result['version'] == 2
    assert tools.verify_end_to_end(1, 1)['consistent'] is True
    after = snapshot(path)
    for table in ('events', 'quiz_results', 'progress', 'replay_receipts'):
        assert after[table] == before[table]


def test_progress_recovery_alone_leaves_cache_fault(demo):
    _, tools = demo('combined')
    before = tools.inspect_view(1, 1)
    check = tools.verify_end_to_end(1, 1)
    assert check['progress']['consistent'] is False and check['learner_view']['consistent'] is False
    assert tools.read_progress(1, 1)[0]['completed'] == 0
    assert tools.inspect_view(1, 1)['lessons'][0]['completed'] == 1
    tools.recover(1, 1)
    assert tools.verify(1, 1)['consistent'] is True
    assert tools.inspect_view(1, 1) == before
    check = tools.verify_end_to_end(1, 1)
    assert check['consistent'] is False and check['learner_view']['consistent'] is False
    assert { (r.get('lesson_id'), r['field']) for r in check['learner_view']['mismatches']} == {
        (2, 'eligible'), (None, 'progress_fingerprint')}


def test_refresh_before_progress_repair_does_not_solve_combined(demo):
    _, tools = demo('combined')
    progress_before = tools.read_progress(1, 1)
    tools.refresh_view(1, 1)
    assert tools.read_progress(1, 1) == progress_before
    check = tools.verify_end_to_end(1, 1)
    assert check['learner_view']['consistent'] is True
    assert check['progress']['consistent'] is False and check['consistent'] is False
    assert tools.inspect_view(1, 1)['lessons'][0]['completed'] == 0
    tools.recover(1, 1)
    assert tools.verify_end_to_end(1, 1)['consistent'] is False
    tools.refresh_view(1, 1)
    assert tools.verify_end_to_end(1, 1)['consistent'] is True


def test_combined_recovery_repeatability_and_scope_isolation(demo):
    path, tools = demo('combined')
    with connect(path) as db:
        db.execute("INSERT INTO courses VALUES(2,'Other course')")
        db.execute("INSERT INTO lessons VALUES(4,2,1,'Other lesson',70)")
        db.execute('INSERT INTO progress VALUES(1,4,0,0,1,0)')
    tools.refresh_view(1, 2)
    before = snapshot(path)
    other_course = tools.inspect_view(1, 2)
    others = [tools.inspect_view(s, 1) for s in (2, 3)]
    tools.recover(1, 1)
    tools.refresh_view(1, 1)
    after = snapshot(path)
    assert tools.verify_end_to_end(1, 1)['consistent'] is True
    view = tools.inspect_view(1, 1)
    assert [(r['completed'], r['eligible']) for r in view['lessons']] == [(1, 1), (0, 1), (0, 0)]
    assert tools.recover(1, 1)['changed'] is False
    assert tools.refresh_view(1, 1)['changed'] is False
    assert tools.inspect_view(1, 1) == view
    assert tools.inspect_view(1, 2) == other_course
    assert [tools.inspect_view(s, 1) for s in (2, 3)] == others
    for table in ('events', 'quiz_results', 'students', 'courses', 'lessons'):
        assert after[table] == before[table]
    assert [r for r in after['progress'] if r['student_id'] != 1 or r['lesson_id'] == 4] == [
        r for r in before['progress'] if r['student_id'] != 1 or r['lesson_id'] == 4]
    assert [h['operation'] for h in tools.read_processing_history(1, 1)][-4:] == [
        'recover_course', 'refresh_learner_view', 'recover_course', 'refresh_learner_view']


def test_healthy_failed_quiz_requires_no_mutation(demo):
    path, tools = demo('healthy-control')
    before = snapshot(path)
    assert tools.verify_end_to_end(1, 1)['consistent'] is True
    view = tools.inspect_view(1, 1)
    assert [(r['completion_seen'], r['quiz_passed'], r['completed'], r['eligible']) for r in view['lessons']] == [
        (1, 0, 0, 1), (0, 0, 0, 0), (0, 0, 0, 0)]
    assert snapshot(path) == before


@pytest.mark.parametrize('scenario', ['missed-processing', 'partial-update', 'healthy-control', 'stale-view', 'combined'])
def test_scenarios_repeat_without_label_leakage(demo, scenario):
    path, tools = demo(scenario)
    progress = tools.read_progress(1, 1)
    view = tools.inspect_view(1, 1)
    check = tools.verify_end_to_end(1, 1)
    tools.recover(1, 1)
    tools.refresh_view(1, 1)
    setup(path, scenario)
    second = tools.inspect_view(1, 1)
    assert tools.read_progress(1, 1) == progress
    for field in ('lessons', 'course_completed', 'version', 'progress_fingerprint'):
        assert second[field] == view[field]
    assert tools.verify_end_to_end(1, 1) == check
    encoded = json.dumps([tools.inspect(1, 1), second, check])
    assert all(label not in encoded for label in ('missed-processing', 'partial-update', 'healthy-control', 'stale-view', 'combined'))


def test_cache_failure_rolls_back_and_audits(demo):
    path, tools = demo('stale-view')
    with connect(path) as db:
        db.execute("""CREATE TRIGGER reject_cache BEFORE UPDATE ON learner_views
            BEGIN SELECT RAISE(ABORT,'private cache failure'); END""")
    before = tools.inspect_view(1, 1)
    result = tools.refresh_view(1, 1)
    assert result['outcome'] == 'error' and result['error_code'] == 'database_error'
    assert tools.inspect_view(1, 1) == before
    record = tools.read_processing_history(1, 1)[-1]
    assert record['outcome'] == 'error' and record['before_state'] == record['after_state']
    assert 'private cache failure' not in json.dumps(record)


def test_normal_processing_rolls_back_if_cache_publication_fails(tmp_path):
    path = tmp_path / 'cache.sqlite3'
    reset(path)
    tools = RecoveryTools(path)
    client = TestClient(create_app(path), raise_server_exceptions=False)
    event = client.post('/events', json={'student_id': 1, 'lesson_id': 1}).json()['id']
    with connect(path) as db:
        db.execute("""CREATE TRIGGER reject_cache BEFORE UPDATE ON learner_views
            BEGIN SELECT RAISE(ABORT,'cache unavailable'); END""")
    before = snapshot(path)
    assert client.post(f'/events/{event}/process').status_code == 500
    assert snapshot(path) == before
    assert tools.read_activity_events(1, 1)[0]['processed_at'] is None


def test_verification_detects_payload_metadata_corruption(demo):
    path, tools = demo('healthy-control')
    with connect(path) as db:
        payload = json.loads(db.execute('SELECT payload FROM learner_views WHERE student_id=1 AND course_id=1').fetchone()[0])
        payload['lessons'].append(payload['lessons'][0].copy())
        db.execute('UPDATE learner_views SET payload=? WHERE student_id=1 AND course_id=1', (json.dumps(payload),))
    check = tools.verify_end_to_end(1, 1)
    assert check['progress']['consistent'] is True and check['consistent'] is False
    assert check['learner_view']['mismatches'][0]['field'] == 'payload_fingerprint'
    tools.refresh_view(1, 1)
    assert tools.verify_end_to_end(1, 1)['consistent'] is True
