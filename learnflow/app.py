import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from learnflow.db import connect, initialize
from learnflow.pipeline import recompute, stored, log_operation
from learnflow import learner_view


class Completion(BaseModel):
    student_id: int
    lesson_id: int


class Quiz(Completion):
    score: int = Field(ge=0, le=100)


def require(db, table, identifier):
    if not db.execute(f'SELECT 1 FROM {table} WHERE id = ?', (identifier,)).fetchone():
        raise HTTPException(404, f'{table} record not found')


def create_app(db_path=None):
    path = db_path or os.environ.get('LEARNFLOW_DB', 'learnflow.sqlite3')
    initialize(path)
    app = FastAPI(title='LearnFlow Recovery â€” Milestone 1')

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
            course_id = db.execute('SELECT course_id FROM lessons WHERE id=?', (event['lesson_id'],)).fetchone()[0]
            before = stored(db, event['student_id'], course_id)
            already_processed = event['processed_at'] is not None
            db.execute('UPDATE events SET processed_at = CURRENT_TIMESTAMP WHERE id = ? AND processed_at IS NULL', (event_id,))
            recompute(db, event['student_id'], course_id)
            log_operation(db, 'process_event', event['student_id'], course_id, before,
                          stored(db, event['student_id'], course_id), event_id=event_id)
            # Explicit, separately audited projection -> learner snapshot step.
            learner_view.refresh(db, event['student_id'], course_id)
            return {'event_id': event_id, 'already_processed': already_processed}

    @app.get('/students/{student_id}/progress')
    def progress(student_id: int):
        with connect(path) as db:
            require(db, 'students', student_id)
            rows = [dict(r) for r in db.execute('''SELECT p.*, l.position FROM progress p
                JOIN lessons l ON l.id = p.lesson_id WHERE student_id = ? ORDER BY l.position''', (student_id,))]
            return {'student_id': student_id, 'course_completed': bool(rows) and all(r['completed'] for r in rows),
                    'lessons': rows}

    @app.get('/students/{student_id}/courses/{course_id}/learner-view')
    def cached_progress(student_id: int, course_id: int):
        with connect(path) as db:
            require(db, 'students', student_id)
            require(db, 'courses', course_id)
            return learner_view.read(db, student_id, course_id)

    return app
