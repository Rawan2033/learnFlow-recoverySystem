"""Allowlisted future-agent tools. No scenario imports or arbitrary SQL interface."""
import argparse
import json
import os
import sqlite3

from learnflow.db import connect
from learnflow.pipeline import FIELDS, expected, stored, recompute, log_operation


def validate(db, student_id, course_id):
    if not db.execute('SELECT 1 FROM students WHERE id=?', (student_id,)).fetchone():
        raise ValueError('unknown_student')
    if not db.execute('SELECT 1 FROM courses WHERE id=?', (course_id,)).fetchone():
        raise ValueError('unknown_course')


def verification(db, student_id, course_id):
    actual = {r['lesson_id']: r for r in stored(db, student_id, course_id)}
    mismatches = []
    for row in expected(db, student_id, course_id):
        current = actual.get(row['lesson_id'])
        for field in FIELDS:
            value = current[field] if current else None
            if value != row[field]:
                mismatches.append({'lesson_id': row['lesson_id'], 'field': field,
                                   'actual': value, 'expected': row[field]})
    return {'student_id': student_id, 'course_id': course_id,
            'consistent': not mismatches, 'mismatches': mismatches}


class RecoveryTools:
    def __init__(self, db_path):
        self.db_path = db_path

    def read_activity_events(self, student_id, course_id):
        return self.inspect(student_id, course_id)['events']

    def read_quiz_results(self, student_id, course_id):
        return self.inspect(student_id, course_id)['quiz_results']

    def read_requirements(self, student_id, course_id):
        return self.inspect(student_id, course_id)['requirements']

    def read_progress(self, student_id, course_id):
        return self.inspect(student_id, course_id)['progress']

    def read_processing_history(self, student_id, course_id):
        return self.inspect(student_id, course_id)['processing_history']

    def inspect(self, student_id, course_id):
        with connect(self.db_path) as db:
            db.execute('BEGIN')  # One read snapshot for all diagnostic collections.
            validate(db, student_id, course_id)
            lessons = [dict(r) for r in db.execute('SELECT * FROM lessons WHERE course_id=? ORDER BY position', (course_id,))]
            previous = None
            for lesson in lessons:
                lesson['prerequisite_lesson_ids'] = [] if previous is None else [previous]
                lesson['requires_completion_event'] = True
                previous = lesson['id']
            def evidence(table):
                return [dict(r) for r in db.execute(f'''SELECT x.* FROM {table} x
                    JOIN lessons l ON l.id=x.lesson_id WHERE x.student_id=? AND l.course_id=? ORDER BY x.id''',
                    (student_id, course_id))]
            history = [dict(r) for r in db.execute('''SELECT * FROM operation_history
                WHERE student_id=? AND course_id=? ORDER BY id''', (student_id, course_id))]
            for row in history:
                for key in ('before_state', 'after_state'):
                    row[key] = json.loads(row[key])
            receipts = [r[0] for r in db.execute('''SELECT r.event_id FROM replay_receipts r
                JOIN events e ON e.id=r.event_id JOIN lessons l ON l.id=e.lesson_id
                WHERE e.student_id=? AND l.course_id=? ORDER BY r.event_id''', (student_id, course_id))]
            return {'student_id': student_id, 'course_id': course_id,
                    'events': evidence('events'), 'quiz_results': evidence('quiz_results'),
                    'requirements': lessons, 'progress': stored(db, student_id, course_id),
                    'processing_history': history, 'replayed_event_ids': receipts}

    def verify(self, student_id, course_id):
        with connect(self.db_path) as db:
            db.execute('BEGIN')
            validate(db, student_id, course_id)
            return verification(db, student_id, course_id)

    def recover(self, student_id, course_id):
        with connect(self.db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            before = stored(db, student_id, course_id)
            db.execute('SAVEPOINT recovery')
            try:
                validate(db, student_id, course_id)
                db.execute('''INSERT OR IGNORE INTO replay_receipts(event_id)
                    SELECT e.id FROM events e JOIN lessons l ON l.id=e.lesson_id
                    WHERE e.student_id=? AND l.course_id=?''', (student_id, course_id))
                recompute(db, student_id, course_id)
                check = verification(db, student_id, course_id)
                if not check['consistent']:
                    raise ValueError('verification_failed')
                after = stored(db, student_id, course_id)
                outcome, error = 'success', None
            except (ValueError, sqlite3.Error) as exc:
                db.execute('ROLLBACK TO recovery')
                after = before
                outcome = 'error'
                # Never serialize arbitrary exception messages or SQL/data into logs.
                error = str(exc) if isinstance(exc, ValueError) else 'database_error'
            db.execute('RELEASE recovery')
            audit_id = log_operation(db, 'recover_course', student_id, course_id,
                                     before, after, outcome, error)
            return {'audit_id': audit_id, 'student_id': student_id, 'course_id': course_id,
                    'outcome': outcome, 'error_code': error, 'changed': before != after}


def main():
    parser = argparse.ArgumentParser(description='Structured diagnostics and controlled recovery')
    parser.add_argument('operation', choices=['inspect', 'verify', 'recover'])
    parser.add_argument('--db', default=os.environ.get('LEARNFLOW_DB', 'learnflow.sqlite3'))
    parser.add_argument('--student', type=int, required=True)
    parser.add_argument('--course', type=int, required=True)
    args = parser.parse_args()
    tools = RecoveryTools(args.db)
    result = getattr(tools, args.operation)(args.student, args.course)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
