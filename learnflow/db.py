import sqlite3
from contextlib import contextmanager


@contextmanager
def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(path):
    with connect(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS courses(id INTEGER PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS lessons(
            id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL REFERENCES courses(id),
            position INTEGER NOT NULL, title TEXT NOT NULL, passing_score INTEGER NOT NULL,
            UNIQUE(course_id, position));
        CREATE TABLE IF NOT EXISTS quiz_results(
            id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL REFERENCES students(id),
            lesson_id INTEGER NOT NULL REFERENCES lessons(id),
            score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL REFERENCES students(id),
            lesson_id INTEGER NOT NULL REFERENCES lessons(id),
            kind TEXT NOT NULL CHECK(kind IN ('lesson_completed', 'quiz_recorded')),
            quiz_result_id INTEGER UNIQUE REFERENCES quiz_results(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            processed_at TEXT,
            CHECK((kind = 'quiz_recorded' AND quiz_result_id IS NOT NULL) OR
                  (kind = 'lesson_completed' AND quiz_result_id IS NULL)));
        CREATE TABLE IF NOT EXISTS progress(
            student_id INTEGER NOT NULL REFERENCES students(id),
            lesson_id INTEGER NOT NULL REFERENCES lessons(id),
            completion_seen INTEGER NOT NULL DEFAULT 0,
            quiz_passed INTEGER NOT NULL DEFAULT 0,
            eligible INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(student_id, lesson_id));
        CREATE TABLE IF NOT EXISTS replay_receipts(
            event_id INTEGER PRIMARY KEY REFERENCES events(id));
        CREATE TABLE IF NOT EXISTS learner_views(
            student_id INTEGER NOT NULL REFERENCES students(id),
            course_id INTEGER NOT NULL REFERENCES courses(id),
            version INTEGER NOT NULL CHECK(version > 0),
            payload TEXT NOT NULL,
            progress_fingerprint TEXT NOT NULL,
            refreshed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(student_id, course_id));
        CREATE TABLE IF NOT EXISTS operation_history(
            id INTEGER PRIMARY KEY,
            operation TEXT NOT NULL,
            student_id INTEGER,
            course_id INTEGER,
            event_id INTEGER,
            outcome TEXT NOT NULL,
            error_code TEXT,
            before_state TEXT NOT NULL,
            after_state TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        ''')


def reset(path):
    initialize(path)
    with connect(path) as db:
        for table in ('operation_history', 'learner_views', 'replay_receipts', 'progress', 'events', 'quiz_results', 'lessons', 'students', 'courses'):
            db.execute(f'DELETE FROM {table}')
        db.execute("INSERT INTO courses VALUES (1, 'Learning Foundations')")
        db.executemany('INSERT INTO students VALUES (?, ?)',
                       [(1, 'Synthetic Alex'), (2, 'Synthetic Sam'), (3, 'Synthetic Taylor')])
        db.executemany('INSERT INTO lessons VALUES (?, 1, ?, ?, 70)',
                       [(1, 1, 'Getting Started'), (2, 2, 'Practice'), (3, 3, 'Apply Your Skills')])
        db.execute('''INSERT INTO progress(student_id, lesson_id, eligible)
                      SELECT s.id, l.id, l.position = 1 FROM students s CROSS JOIN lessons l''')
        # Initial snapshots are seed state, not processing or recovery attempts.
        from learnflow.learner_view import refresh
        for student_id in (1, 2, 3):
            refresh(db, student_id, 1, audit=False)
