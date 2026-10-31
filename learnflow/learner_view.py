"""Persisted presentation snapshot. This module never reads or processes evidence."""
import hashlib
import json

from learnflow.pipeline import FIELDS, log_operation, stored


def derived_payload(db, student_id, course_id):
    rows = stored(db, student_id, course_id)
    return {'lessons': rows, 'course_completed': bool(rows) and all(bool(r['completed']) for r in rows)}


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read(db, student_id, course_id):
    row = db.execute('SELECT * FROM learner_views WHERE student_id=? AND course_id=?',
                     (student_id, course_id)).fetchone()
    if row is None:
        return {'student_id': student_id, 'course_id': course_id, 'initialized': False,
                'version': 0, 'progress_fingerprint': None, 'refreshed_at': None,
                'lessons': [], 'course_completed': None}
    payload = json.loads(row['payload'])
    return {'student_id': student_id, 'course_id': course_id, 'initialized': True,
            'version': row['version'], 'progress_fingerprint': row['progress_fingerprint'],
            'refreshed_at': row['refreshed_at'], **payload}


def verify(db, student_id, course_id):
    cached = read(db, student_id, course_id)
    payload = derived_payload(db, student_id, course_id)
    mismatches = []
    if not cached['initialized']:
        mismatches.append({'field': 'initialized', 'actual': False, 'expected': True})
    else:
        cached_digest = fingerprint({'lessons': cached['lessons'], 'course_completed': cached['course_completed']})
        if cached_digest != cached['progress_fingerprint']:
            mismatches.append({'field': 'payload_fingerprint', 'actual': cached_digest,
                               'expected': cached['progress_fingerprint']})
        actual = {r['lesson_id']: r for r in cached['lessons']}
        desired = {r['lesson_id']: r for r in payload['lessons']}
        for lesson_id in sorted(actual.keys() | desired.keys()):
            for field in ('student_id', *FIELDS):
                value = actual.get(lesson_id, {}).get(field)
                expected = desired.get(lesson_id, {}).get(field)
                if value != expected:
                    mismatches.append({'lesson_id': lesson_id, 'field': field,
                                       'actual': value, 'expected': expected})
        if cached['course_completed'] != payload['course_completed']:
            mismatches.append({'field': 'course_completed', 'actual': cached['course_completed'],
                               'expected': payload['course_completed']})
        if cached['progress_fingerprint'] != fingerprint(payload):
            mismatches.append({'field': 'progress_fingerprint', 'actual': cached['progress_fingerprint'],
                               'expected': fingerprint(payload)})
    return {'consistent': not mismatches, 'initialized': cached['initialized'],
            'version': cached['version'], 'mismatches': mismatches}


def refresh(db, student_id, course_id, audit=True):
    """Copy *stored derived state*, even if it is wrong. Caller owns transaction."""
    before = read(db, student_id, course_id)
    payload = derived_payload(db, student_id, course_id)
    digest = fingerprint(payload)
    changed = (not before['initialized'] or before['progress_fingerprint'] != digest or
               before['lessons'] != payload['lessons'] or before['course_completed'] != payload['course_completed'])
    if changed:
        db.execute('''INSERT INTO learner_views(student_id,course_id,version,payload,progress_fingerprint)
            VALUES (?,?,?,?,?) ON CONFLICT(student_id,course_id) DO UPDATE SET
            version=excluded.version,payload=excluded.payload,
            progress_fingerprint=excluded.progress_fingerprint,refreshed_at=CURRENT_TIMESTAMP''',
            (student_id, course_id, before['version'] + 1, json.dumps(payload), digest))
    after = read(db, student_id, course_id)
    audit_id = log_operation(db, 'refresh_learner_view', student_id, course_id, before, after) if audit else None
    return {'student_id': student_id, 'course_id': course_id, 'outcome': 'success',
            'changed': changed, 'version': after['version'], 'audit_id': audit_id}
