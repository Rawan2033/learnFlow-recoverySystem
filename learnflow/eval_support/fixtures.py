"""Privileged fixture construction and independent scoring; never agent inputs.

The reference evaluator uses raw rows and an independent prerequisite set. It
does not import the production pipeline, processing, recovery or verification.
"""
import hashlib
import json

from learnflow.db import connect, reset


STAMP = '2020-01-01 00:00:00'
FIELDS = ('completion_seen', 'quiz_passed', 'eligible', 'completed')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def reference(lessons, events, quizzes, student):
    """All authoritative evidence counts, including not-yet-processed events."""
    earned = set()
    rows = []
    prerequisites = []
    for lesson in sorted(lessons, key=lambda r: r['position']):
        lid = lesson['id']
        activity = any(e['student_id'] == student and e['lesson_id'] == lid and
                       e['kind'] == 'lesson_completed' for e in events)
        linked = {e['quiz_result_id'] for e in events if e['student_id'] == student and
                  e['lesson_id'] == lid and e['kind'] == 'quiz_recorded'}
        passed = any(q['id'] in linked and q['student_id'] == student and
                     q['lesson_id'] == lid and q['score'] >= lesson['passing_score'] for q in quizzes)
        eligible = all(previous in earned for previous in prerequisites)
        complete = eligible and activity and passed
        if complete:
            earned.add(lid)
        rows.append(dict(student_id=student, lesson_id=lid, completion_seen=int(activity),
                         quiz_passed=int(passed), eligible=int(eligible), completed=int(complete)))
        prerequisites.append(lid)
    return rows


def snapshot(path, student, course=1):
    with connect(path) as db:
        def rows(sql, parameters=()):
            return [dict(r) for r in db.execute(sql, parameters)]
        lessons = rows('SELECT * FROM lessons WHERE course_id=? ORDER BY position', (course,))
        events = rows('SELECT * FROM events ORDER BY id')
        quizzes = rows('SELECT * FROM quiz_results ORDER BY id')
        progress = rows('SELECT p.* FROM progress p JOIN lessons l ON l.id=p.lesson_id WHERE p.student_id=? AND l.course_id=? ORDER BY l.position', (student, course))
        cache = rows('SELECT * FROM learner_views WHERE student_id=? AND course_id=?', (student, course))
        unrelated = {}
        for table in ('progress', 'learner_views', 'operation_history'):
            unrelated[table] = rows(f'SELECT * FROM {table} WHERE student_id != ? OR student_id IS NULL ORDER BY rowid', (student,))
        unrelated['receipts'] = rows('SELECT r.* FROM replay_receipts r JOIN events e ON e.id=r.event_id WHERE e.student_id != ? ORDER BY r.event_id', (student,))
        catalogue = {t: rows(f'SELECT * FROM {t} ORDER BY id') for t in ('students', 'courses', 'lessons')}
        return {'source': {'events': events, 'quizzes': quizzes}, 'progress': progress,
                'cache': cache, 'unrelated': unrelated, 'catalogue': catalogue,
                'expected': reference(lessons, events, quizzes, student)}


def assess(state):
    desired = state['expected']
    payload = {'lessons': desired, 'course_completed': all(r['completed'] for r in desired)}
    cache = state['cache']
    cached = json.loads(cache[0]['payload']) if cache else {}
    progress_ok = state['progress'] == desired
    view_ok = (cached == payload and bool(cache) and cache[0]['version'] > 0 and
               cache[0]['progress_fingerprint'] == digest(payload))
    # Count overgranting separately for progress and presented view, never trust
    # the production verifier's expected values for scoring.
    overgrants = []
    expected_by_id = {r['lesson_id']: r for r in desired}
    for layer, rows in (('progress', state['progress']), ('view', cached.get('lessons', []))):
        for row in rows:
            expected = expected_by_id.get(row['lesson_id'], {})
            for field in ('eligible', 'completed'):
                if row.get(field, 0) > expected.get(field, 0):
                    overgrants.append({'layer': layer, 'lesson_id': row['lesson_id'], 'field': field})
    return {'correct': bool(progress_ok and view_ok), 'progress_correct': progress_ok,
            'view_correct': bool(view_ok), 'overgrants': overgrants}


def create_fixture(path, case):
    """Fresh fixed three-lesson course; caller must reserve a new path."""
    if path.exists():
        raise ValueError('Fixture database already exists')
    reset(path)
    student, target = case['student'], case['lesson']
    with connect(path) as db:
        def evidence(lid, scores, activity=True, link=True, processed=True, duplicates=0):
            if activity:
                for _ in range(1 + duplicates):
                    db.execute("INSERT INTO events(student_id,lesson_id,kind,created_at,processed_at) VALUES (?,?,'lesson_completed',?,?)",
                               (student, lid, STAMP, STAMP if processed else None))
            for score in scores:
                qid = db.execute('INSERT INTO quiz_results(student_id,lesson_id,score,created_at) VALUES (?,?,?,?)',
                                 (student, lid, score, STAMP)).lastrowid
                if link:
                    db.execute("INSERT INTO events(student_id,lesson_id,kind,quiz_result_id,created_at,processed_at) VALUES (?,?,'quiz_recorded',?,?,?)",
                               (student, lid, qid, STAMP, STAMP if processed else None))
        for lid in range(1, target):
            if not case.get('missing_prerequisite') or lid != target - 1:
                evidence(lid, [70 + lid])
        evidence(target, case['scores'], case.get('activity', True), case.get('linked', True),
                 case['family'] != 'missed-processing', case.get('duplicates', 0))
        if case.get('downstream_evidence') and target < 3:
            evidence(target + 1, [100])
        lessons = [dict(r) for r in db.execute('SELECT * FROM lessons ORDER BY position')]
        events = [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id')]
        quizzes = [dict(r) for r in db.execute('SELECT * FROM quiz_results ORDER BY id')]
        correct = reference(lessons, events, quizzes, student)
        processed = reference(lessons, [e for e in events if e['processed_at']], quizzes, student)
        prefix = reference(lessons, [e for e in events if e['lesson_id'] < target], quizzes, student)
        initial = processed if case['family'] == 'missed-processing' else correct
        if case['family'] == 'combined':
            initial = prefix
        for row in initial:
            db.execute('UPDATE progress SET completion_seen=?,quiz_passed=?,eligible=?,completed=? WHERE student_id=? AND lesson_id=?',
                       tuple(row[f] for f in FIELDS) + (student, row['lesson_id']))
        if case['family'] == 'partial-update':
            db.execute('UPDATE progress SET eligible=0 WHERE student_id=? AND lesson_id=?', (student, target + 1))
        view_rows = prefix if case['family'] in ('stale-view', 'combined', 'missed-processing') else correct
        payload = {'lessons': view_rows, 'course_completed': all(r['completed'] for r in view_rows)}
        db.execute('UPDATE learner_views SET payload=?,progress_fingerprint=? WHERE student_id=? AND course_id=1',
                   (json.dumps(payload), digest(payload), student))
        # Normalize seed metadata too, so identical configurations are byte-wise
        # comparable as logical rows across methods/repetitions.
        db.execute('UPDATE learner_views SET refreshed_at=?', (STAMP,))
    return snapshot(path, student)
