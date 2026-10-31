"""Deterministic course rules shared by ordinary processing and recovery."""
import json


FIELDS = ('completion_seen', 'quiz_passed', 'eligible', 'completed')


def stored(db, student_id, course_id):
    return [dict(r) for r in db.execute('''SELECT p.* FROM progress p
        JOIN lessons l ON l.id=p.lesson_id
        WHERE p.student_id=? AND l.course_id=? ORDER BY l.position''',
        (student_id, course_id))]


def expected(db, student_id, course_id, processed_only=False):
    lessons = db.execute('SELECT * FROM lessons WHERE course_id=? ORDER BY position', (course_id,)).fetchall()
    events = db.execute('''SELECT e.* FROM events e JOIN lessons l ON l.id=e.lesson_id
        WHERE e.student_id=? AND l.course_id=?''', (student_id, course_id)).fetchall()
    receipts = {r[0] for r in db.execute('SELECT event_id FROM replay_receipts')}
    quizzes = {r['id']: r for r in db.execute('SELECT * FROM quiz_results WHERE student_id=?', (student_id,))}
    result = []
    eligible = True
    for lesson in lessons:
        evidence = [e for e in events if e['lesson_id'] == lesson['id'] and
                    (not processed_only or e['processed_at'] is not None or e['id'] in receipts)]
        completion = any(e['kind'] == 'lesson_completed' for e in evidence)
        passed = any(e['kind'] == 'quiz_recorded' and
                     (q := quizzes.get(e['quiz_result_id'])) is not None and
                     q['lesson_id'] == lesson['id'] and q['score'] >= lesson['passing_score']
                     for e in evidence)
        completed = eligible and completion and passed
        result.append(dict(student_id=student_id, lesson_id=lesson['id'],
                           completion_seen=int(completion), quiz_passed=int(passed),
                           eligible=int(eligible), completed=int(completed)))
        eligible = completed
    return result


def recompute(db, student_id, course_id=None):
    courses = [course_id] if course_id is not None else [r[0] for r in db.execute('SELECT id FROM courses ORDER BY id')]
    for course in courses:
        for row in expected(db, student_id, course, processed_only=True):
            db.execute('''INSERT INTO progress VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(student_id, lesson_id) DO UPDATE SET
                completion_seen=excluded.completion_seen, quiz_passed=excluded.quiz_passed,
                eligible=excluded.eligible, completed=excluded.completed''', tuple(row.values()))


def log_operation(db, operation, student_id, course_id, before, after,
                  outcome='success', error_code=None, event_id=None):
    return db.execute('''INSERT INTO operation_history
        (operation,student_id,course_id,event_id,outcome,error_code,before_state,after_state)
        VALUES (?,?,?,?,?,?,?,?)''',
        (operation, student_id, course_id, event_id, outcome, error_code,
         json.dumps(before), json.dumps(after))).lastrowid
