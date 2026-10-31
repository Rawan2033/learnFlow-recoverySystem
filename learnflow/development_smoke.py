"""Developer-only labelled smoke checks, NOT held-out evaluation or an agent tool.

This runner may inject scenarios and read validation answers. None of its labels,
expectations, snapshots or filesystem paths are passed to the model.
"""
import argparse
import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from learnflow.agent import INSTRUCTIONS, Investigator
from learnflow.agent_config import ConfigurationError, Redactor, Settings
from learnflow.agent_trace import TraceWriter
from learnflow.db import connect
from learnflow.scenarios import setup
from learnflow.tools import RecoveryTools


CASES = ('stale-view', 'combined')
EXPECTED_REPAIRS = {'stale-view': ['refresh_view'], 'combined': ['recover', 'refresh_view']}
SYMPTOM = 'Please investigate why my next lesson is locked.'


def save_json(path, data, redact):
    with Path(path).open('x', encoding='utf-8') as output:
        json.dump(redact(data), output, indent=2)
        output.write('\n')


def capture(db_path):
    """Read raw source rows and all other students, including cache/audit metadata."""
    with connect(db_path) as db:
        db.execute('BEGIN')
        sources = {table: [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY id')]
                   for table in ('events', 'quiz_results')}
        unrelated = {table: [dict(row) for row in db.execute(
            f'SELECT * FROM {table} WHERE student_id != 1 ORDER BY rowid')]
            for table in ('progress', 'learner_views', 'operation_history')}
        unrelated['students'] = [dict(row) for row in db.execute('SELECT * FROM students WHERE id != 1 ORDER BY id')]
        unrelated['replay_receipts'] = [dict(row) for row in db.execute('''
            SELECT r.* FROM replay_receipts r JOIN events e ON e.id=r.event_id
            WHERE e.student_id != 1 ORDER BY r.event_id''')]
    tools = RecoveryTools(db_path)
    return {'source_evidence': sources, 'unrelated_students': unrelated,
            'progress': tools.read_progress(1, 1), 'learner_view': tools.inspect_view(1, 1),
            'progress_verification': tools.verify(1, 1),
            'end_to_end_verification': tools.verify_end_to_end(1, 1)}


def investigate_once(settings, db_path, trace_path, model_factory=None):
    """Reuse the real provider/agent loop exactly once; never retry an investigation."""
    redact = Redactor(settings.api_key.get_secret_value())
    writer = TraceWriter(trace_path, redact)
    model = None
    started = perf_counter()
    try:
        writer.record('provider', provider='openai_responses', model=settings.model)
        if model_factory is None:
            from learnflow.openai_provider import OpenAIResponsesModel
            model_factory = OpenAIResponsesModel
        model = model_factory(settings, INSTRUCTIONS)
        report = Investigator(model, RecoveryTools(db_path), writer, 1, 1,
                              settings.limits, redact, verification_scope='learner_view').run(SYMPTOM)
    except Exception:
        # Never include exception bodies, raw requests, prompts, or credentials.
        report = {'status': 'execution_failed', 'actions_performed': [],
                  'error_code': 'investigation_execution_failed'}
        writer.record('final', report=report)
    finally:
        if model is not None:
            try:
                model.close()
            except Exception:
                pass
        writer.close()
    elapsed = round(perf_counter() - started, 3)
    entries = [json.loads(line) for line in Path(trace_path).read_text(encoding='utf-8').splitlines()]
    return report, entries, elapsed


def token_usage(entries):
    usages = [entry['usage'] for entry in entries if entry.get('event') == 'model_turn' and entry.get('usage')]
    return {'provided': bool(usages), 'responses_with_usage': len(usages),
            **{name: sum(u.get(name) or 0 for u in usages) if usages else None
               for name in ('input_tokens', 'output_tokens', 'total_tokens')}}


def lesson_two_ready(rows):
    row = next((r for r in rows if r.get('lesson_id') == 2), None)
    return row is not None and row['eligible'] == 1 and row['completed'] == 0


def run_case(settings, case, directory, model_factory=None):
    started = perf_counter()
    redact = Redactor(settings.api_key.get_secret_value())
    directory.mkdir()  # Fail rather than overwrite any earlier case artifacts.
    db_path = directory / 'learning.sqlite3'
    db_path.touch(exist_ok=False)
    setup(db_path, case)  # Only this newly reserved, isolated file may be reset.
    before = capture(db_path)
    save_json(directory / 'before.json', before, redact)
    progress_before = before['progress_verification']
    path_before = before['end_to_end_verification']
    expected_progress = case == 'stale-view'
    initial_ok = (progress_before['consistent'] is expected_progress and
                  path_before['progress']['consistent'] is expected_progress and
                  path_before['consistent'] is False and path_before['learner_view']['consistent'] is False)
    report, entries, investigation_seconds = None, [], 0.0
    if initial_ok:
        report, entries, investigation_seconds = investigate_once(
            settings, db_path, directory / 'agent.jsonl', model_factory)
    else:
        report = {'status': 'execution_failed', 'error_code': 'unexpected_initial_state'}
    # Independent read-only checks happen even when the API/agent reports failure.
    after = capture(db_path)
    save_json(directory / 'after.json', after, redact)
    requested = [entry['tool'] for entry in entries if entry.get('event') == 'tool_started'
                 and entry.get('tool') in ('recover', 'refresh_view')]
    successful = [entry['tool'] for entry in entries if entry.get('event') == 'tool_result'
                  and entry.get('tool') in ('recover', 'refresh_view') and entry['result'].get('ok')]
    checks = {
        'initial_state_matches_expected': initial_ok,
        'agent_reported_recovered': report['status'] == 'recovered',
        'agent_end_to_end_consistent': (report.get('verification_scope') == 'learner_view' and
                                       report.get('verification_outcome', {}).get('consistent') is True),
        'expected_repair_sequence': requested == EXPECTED_REPAIRS[case] and successful == EXPECTED_REPAIRS[case],
        'independent_end_to_end_consistent': after['end_to_end_verification']['consistent'] is True,
        'source_evidence_unchanged': before['source_evidence'] == after['source_evidence'],
        'unrelated_students_unchanged': before['unrelated_students'] == after['unrelated_students'],
        'lesson_2_eligible_not_completed_in_progress': lesson_two_ready(after['progress']),
        'lesson_2_eligible_not_completed_in_view': lesson_two_ready(after['learner_view']['lessons']),
    }
    summary = {
        'case': case, 'kind': 'development_smoke_test', 'held_out_evaluation': False,
        'passed': all(checks.values()), 'agent_status': report['status'], 'checks': checks,
        'model_name': settings.model, 'repair_tools_requested': requested,
        'successful_repair_tools': successful, 'expected_repair_tools': EXPECTED_REPAIRS[case],
        'progress_verification_before': progress_before, 'end_to_end_verification_before': path_before,
        'independent_end_to_end_verification_after': after['end_to_end_verification'],
        'token_usage': token_usage(entries), 'investigation_elapsed_seconds': investigation_seconds,
        'case_elapsed_seconds': round(perf_counter() - started, 3),
        'database': str(db_path), 'trace_file': str(directory / 'agent.jsonl') if initial_ok else None,
        'report': report,
    }
    save_json(directory / 'summary.json', summary, redact)
    return summary


def run_suite(settings, output_root='.traces/milestone4', model_factory=None, selected_cases=CASES):
    if not selected_cases or len(set(selected_cases)) != len(selected_cases) or any(case not in CASES for case in selected_cases):
        raise ConfigurationError('Select supported development smoke cases only.')
    # Preflight the combined budget before spending on either case. Respect other limits.
    required_repairs = 2 if 'combined' in selected_cases else 1
    if settings.limits.max_repairs < required_repairs:
        raise ConfigurationError(f'Development smoke tests require LEARNFLOW_MAX_REPAIRS >= {required_repairs}.')
    started = perf_counter()
    redact = Redactor(settings.api_key.get_secret_value())
    directory = Path(output_root) / uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    cases = []
    for case in selected_cases:
        try:
            result = run_case(settings, case, directory / case, model_factory)
        except Exception:
            result = {'case': case, 'passed': False, 'agent_status': 'execution_failed',
                      'error_code': 'smoke_case_execution_failed'}
        cases.append(result)
        if not result['passed']:
            break  # No automatic retries or additional paid cases after any failure.
    summary = {'kind': 'development_smoke_tests', 'held_out_evaluation': False,
               'passed': len(cases) == len(selected_cases) and all(case['passed'] for case in cases),
               'model_name': settings.model, 'run_directory': str(directory), 'cases': cases,
               'selected_cases': list(selected_cases), 'skipped_cases': list(selected_cases[len(cases):]),
               'elapsed_seconds': round(perf_counter() - started, 3),
               'stop_policy': 'Stop after first failed case; never retry paid investigations.'}
    save_json(directory / 'summary.json', summary, redact)
    return redact(summary)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Labelled milestone 4 development smoke tests; NOT held-out evaluation')
    parser.add_argument('--allow-paid', action='store_true', help='Run at most one real investigation per case')
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--output-root', default='.traces/milestone4')
    parser.add_argument('--case', choices=CASES, help='Run only this case in a new isolated database; default runs both')
    args = parser.parse_args(argv)
    if not args.allow_paid:
        parser.error('Development live smoke tests require explicit --allow-paid. No investigation was started.')
    try:
        settings = Settings.from_environment(args.env_file)
        summary = (run_suite(settings, args.output_root, selected_cases=(args.case,)) if args.case
                   else run_suite(settings, args.output_root))
        print(json.dumps(summary, indent=2))
        return 0 if summary['passed'] else 1
    except ConfigurationError as exc:
        print(json.dumps({'kind': 'development_smoke_tests', 'passed': False,
                          'error_code': 'configuration_error', 'message': str(exc)}))
    except Exception:
        print(json.dumps({'kind': 'development_smoke_tests', 'passed': False,
                          'error_code': 'smoke_runner_execution_failed'}))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
