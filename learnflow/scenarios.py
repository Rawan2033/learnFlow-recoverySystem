"""Developer-only fixtures. Never register this module as an agent tool."""
import argparse
from learnflow.db import connect, reset
from learnflow.pipeline import recompute, stored, log_operation
from learnflow.learner_view import refresh


SCENARIOS = ('missed-processing', 'partial-update', 'healthy-control', 'stale-view', 'combined')


def setup(path, scenario):
    if scenario not in SCENARIOS:
        raise ValueError('Unknown developer scenario')
    reset(path)
    with connect(path) as db:
        score = 40 if scenario == 'healthy-control' else 90
        db.execute('INSERT INTO quiz_results(id,student_id,lesson_id,score) VALUES (1,1,1,?)', (score,))
        db.execute("INSERT INTO events(id,student_id,lesson_id,kind) VALUES (1,1,1,'lesson_completed')")
        db.execute("INSERT INTO events(id,student_id,lesson_id,kind,quiz_result_id) VALUES (2,1,1,'quiz_recorded',1)")
        if scenario != 'missed-processing':
            before = stored(db, 1, 1)
            db.execute('UPDATE events SET processed_at=CURRENT_TIMESTAMP')
            recompute(db, 1, 1)
            log_operation(db, 'process_events', 1, 1, before, stored(db, 1, 1))
        if scenario in ('partial-update', 'healthy-control'):
            # Existing scenarios isolate progress behavior; start with a published view.
            refresh(db, 1, 1, audit=False)
        if scenario == 'combined':
            # A partial publication occurred while the downstream eligibility was
            # still wrong; snapshot differs from both broken and repaired progress.
            db.execute('UPDATE progress SET eligible=0 WHERE student_id=1 AND lesson_id=2')
            refresh(db, 1, 1, audit=False)
            db.execute('UPDATE progress SET completed=0 WHERE student_id=1 AND lesson_id=1')
        if scenario == 'partial-update':
            db.execute('UPDATE progress SET eligible=0 WHERE student_id=1 AND lesson_id=2')


def main():
    parser = argparse.ArgumentParser(description='DEVELOPER ONLY: destroys and reseeds the selected demo database')
    parser.add_argument('operation', choices=['setup', 'reset'])
    parser.add_argument('scenario', choices=SCENARIOS)
    parser.add_argument('--db', required=True)
    args = parser.parse_args()
    if args.operation == 'setup':
        setup(args.db, args.scenario)
    else:
        reset(args.db)
    print('Synthetic database ready.')


if __name__ == '__main__':
    main()
