import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from learnflow.db import connect, initialize


class Completion(BaseModel):
    student_id: int
    lesson_id: int


class Quiz(Completion):
    score: int = Field(ge=0, le=100)


def require(db, table, identifier):
    if not db.execute(f'SELECT 1 FROM {table} WHERE id = ?', (identifier,)).fetchone():
        raise HTTPException(404, f'{table} record not found')


def recompute(db, student_id):
    # Only explicitly processed evidence participates in this projection.
    previous_complete = True
    for lesson in db.execute('SELECT * FROM lessons ORDER BY course_id, position').fetchall():
        if lesson['position'] == 1:
            previous_complete = True
        completion = bool(db.execute('''SELECT 1 FROM events WHERE student_id = ?
            AND lesson_id = ? AND kind = 'lesson_completed' AND processed_at IS NOT NULL''',
            (student_id, lesson['id'])).fetchone())
        passed = bool(db.execute('''SELECT 1 FROM events e JOIN quiz_results q ON q.id = e.quiz_result_id
            WHERE e.student_id = ? AND e.lesson_id = ? AND e.processed_at IS NOT NULL
            AND q.score >= ?''', (student_id, lesson['id'], lesson['passing_score'])).fetchone())
        completed = previous_complete and completion and passed
        db.execute('''INSERT INTO progress VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(student_id, lesson_id) DO UPDATE SET
            completion_seen=excluded.completion_seen, quiz_passed=excluded.quiz_passed,
            eligible=excluded.eligible, completed=excluded.completed''',
            (student_id, lesson['id'], completion, passed, previous_complete, completed))
        previous_complete = completed


def create_app(db_path=None):
    path = db_path or os.environ.get('LEARNFLOW_DB', 'learnflow.sqlite3')
    initialize(path)
    app = FastAPI(title='LearnFlow Recovery — Milestone 1')

    @app.get('/lessons')
    def lessons():
        with connect(path) as db:
            return [dict(r) for r in db.execute('SELECT * FROM lessons ORDER BY course_id, position')]

    @app.get('/students')
    def students():
        with connect(path) as db:
            return [dict(r) for r in db.execute('SELECT * FROM students ORDER BY id')]

    @app.post('/events', status_code=201)
    def completion(body: Completion):
        with connect(path) as db:
            require(db, 'students', body.student_id)
            require(db, 'lessons', body.lesson_id)
            cursor = db.execute("INSERT INTO events(student_id, lesson_id, kind) VALUES (?, ?, 'lesson_completed')",
                                (body.student_id, body.lesson_id))
            return dict(db.execute('SELECT * FROM events WHERE id = ?', (cursor.lastrowid,)).fetchone())

    @app.post('/quiz-results', status_code=201)
    def quiz(body: Quiz):
        with connect(path) as db:
            require(db, 'students', body.student_id)
            require(db, 'lessons', body.lesson_id)
            cursor = db.execute('INSERT INTO quiz_results(student_id, lesson_id, score) VALUES (?, ?, ?)',
                                (body.student_id, body.lesson_id, body.score))
            quiz_id = cursor.lastrowid
            event = db.execute("INSERT INTO events(student_id, lesson_id, kind, quiz_result_id) VALUES (?, ?, 'quiz_recorded', ?)",
                               (body.student_id, body.lesson_id, quiz_id))
            return {**dict(db.execute('SELECT * FROM quiz_results WHERE id = ?', (quiz_id,)).fetchone()),
                    'event_id': event.lastrowid}

    @app.get('/events')
    def events():
        with connect(path) as db:
            return [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id')]

    @app.get('/quiz-results')
    def quizzes():
        with connect(path) as db:
            return [dict(r) for r in db.execute('SELECT * FROM quiz_results ORDER BY id')]

    @app.post('/events/{event_id}/process')
    def process(event_id: int):
        with connect(path) as db:
            # Serialize the marker and projection in one transaction, including concurrent requests.
            db.execute('BEGIN IMMEDIATE')
            require(db, 'events', event_id)
            event = db.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
            already_processed = event['processed_at'] is not None
            db.execute('UPDATE events SET processed_at = CURRENT_TIMESTAMP WHERE id = ? AND processed_at IS NULL', (event_id,))
            recompute(db, event['student_id'])
            return {'event_id': event_id, 'already_processed': already_processed}

    @app.get('/students/{student_id}/progress')
    def progress(student_id: int):
        with connect(path) as db:
            require(db, 'students', student_id)
            rows = [dict(r) for r in db.execute('''SELECT p.*, l.position FROM progress p
                JOIN lessons l ON l.id = p.lesson_id WHERE student_id = ? ORDER BY l.position''', (student_id,))]
            return {'student_id': student_id, 'course_completed': bool(rows) and all(r['completed'] for r in rows),
                    'lessons': rows}

    return app
