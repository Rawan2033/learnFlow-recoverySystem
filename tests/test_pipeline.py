import pytest
from fastapi.testclient import TestClient
from learnflow.app import create_app
from learnflow.db import reset


@pytest.fixture
def client(tmp_path):
    path = tmp_path / 'test.sqlite3'
    reset(path)
    with TestClient(create_app(path)) as client:
        yield client


def evidence(client, lesson=1, score=90, student=1):
    payload = {'student_id': student, 'lesson_id': lesson}
    completion = client.post('/events', json=payload)
    quiz = client.post('/quiz-results', json={**payload, 'score': score})
    assert completion.status_code == quiz.status_code == 201
    return completion.json()['id'], quiz.json()['event_id']


def process(client, ids):
    for identifier in ids:
        assert client.post(f'/events/{identifier}/process').status_code == 200


def progress(client):
    return client.get('/students/1/progress').json()


def test_successful_sequential_course(client):
    for lesson in (1, 2, 3):
        ids = evidence(client, lesson)
        assert not progress(client)['lessons'][lesson - 1]['completed']
        process(client, ids)
        assert progress(client)['lessons'][lesson - 1]['completed']
    assert progress(client)['course_completed']


def test_failing_quiz_and_retry(client):
    process(client, evidence(client, score=69))
    row = progress(client)['lessons'][0]
    assert row['completion_seen'] and not row['quiz_passed'] and not row['completed']
    assert not progress(client)['lessons'][1]['eligible']
    quiz = client.post('/quiz-results', json={'student_id': 1, 'lesson_id': 1, 'score': 70}).json()
    process(client, [quiz['event_id']])
    assert progress(client)['lessons'][0]['completed']
    assert [r['score'] for r in client.get('/quiz-results').json()] == [69, 70]


def test_repeated_processing_is_idempotent(client):
    ids = evidence(client)
    process(client, ids)
    before = progress(client)
    events = client.get('/events').json()
    for identifier in ids:
        assert client.post(f'/events/{identifier}/process').json()['already_processed']
    assert progress(client) == before
    assert client.get('/events').json() == events
    assert len(client.get('/quiz-results').json()) == 1


def test_out_of_order_evidence_obeys_prerequisites(client):
    process(client, evidence(client, lesson=2))
    assert not progress(client)['lessons'][1]['completed']
    assert not progress(client)['lessons'][2]['eligible']
    process(client, evidence(client, lesson=1))
    assert progress(client)['lessons'][1]['completed']
    assert progress(client)['lessons'][2]['eligible']


def test_quiz_alone_and_unprocessed_completion_do_not_complete(client):
    completion, quiz = evidence(client)
    process(client, [quiz])
    assert not progress(client)['lessons'][0]['completed']
    assert not progress(client)['lessons'][0]['completion_seen']
    assert client.get('/events').json()[0]['processed_at'] is None
    process(client, [completion])
    assert progress(client)['lessons'][0]['completed']


def test_validation_and_student_isolation(client):
    assert client.post('/quiz-results', json={'student_id': 1, 'lesson_id': 1, 'score': 101}).status_code == 422
    assert client.post('/events', json={'student_id': 999, 'lesson_id': 1}).status_code == 404
    assert client.post('/events/999/process').status_code == 404
    process(client, evidence(client, student=2))
    assert not progress(client)['lessons'][0]['completed']


def test_reset_is_repeatable(tmp_path):
    path = tmp_path / 'reset.sqlite3'
    reset(path)
    client = TestClient(create_app(path))
    process(client, evidence(client))
    reset(path)
    reset(path)
    assert len(client.get('/lessons').json()) == 3
    assert len(client.get('/students').json()) == 3
    assert client.get('/events').json() == []
    assert client.get('/quiz-results').json() == []
    assert not progress(client)['course_completed']
