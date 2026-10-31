"""Reproducible developer evaluation CLI. Not an agent-accessible interface."""
import argparse
import csv
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from importlib.metadata import version

from learnflow.agent import INSTRUCTIONS, Investigator
from learnflow.agent_config import ConfigurationError, Redactor, Settings
from learnflow.agent_contracts import EmptyArguments, Limits, ProposedReport, TOOL_DESCRIPTIONS
from learnflow.agent_trace import TraceWriter
from learnflow.eval_support.fixtures import assess, create_fixture, digest, snapshot
from learnflow.eval_support.methods import BaselinePolicy
from learnflow.tools import RecoveryTools


ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / 'evaluation' / 'manifests'
METHODS = ('replay', 'rule', 'ai')
SUCCESS = ('recovered', 'no_fault_found')


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, data):
    """Atomic checkpoint replacement; attempt result callers check existence."""
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def load_manifest(split):
    path = MANIFESTS / f'{split}.json'
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if split == 'heldout':
        frozen = (MANIFESTS / 'heldout.sha256').read_text().strip()
        if hashlib.sha256(raw).hexdigest() != frozen:
            raise ValueError('Held-out manifest differs from its frozen digest')
    if manifest['split'] != split or len({c['id'] for c in manifest['cases']}) != len(manifest['cases']):
        raise ValueError('Invalid manifest')
    return manifest, hashlib.sha256(raw).hexdigest()


def provenance():
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=ROOT, stderr=subprocess.DEVNULL).decode().strip()
    try:
        commit, status = git('rev-parse', 'HEAD'), git('status', '--porcelain')
    except (OSError, subprocess.CalledProcessError):
        commit, status = 'unavailable', 'unavailable'
    schemas = [{'type': 'function', 'name': n, 'description': d,
                'parameters': EmptyArguments.model_json_schema(), 'strict': True}
               for n, d in TOOL_DESCRIPTIONS.items()]
    return {'git_commit': commit, 'git_dirty': bool(status), 'git_status': status,
            'python_version': platform.python_version(), 'sqlite_version': sqlite3.sqlite_version,
            'package_versions': {name: version(name) for name in ('openai', 'fastapi', 'pydantic')},
            'instructions_sha256': hashlib.sha256(INSTRUCTIONS.encode()).hexdigest(),
            'tool_schema_sha256': digest(schemas), 'report_schema_sha256': digest(ProposedReport.model_json_schema()),
            'python_sources_sha256': digest({str(p.relative_to(ROOT)): file_hash(p)
                                            for p in sorted((ROOT / 'learnflow').rglob('*.py'))})}


@contextmanager
def run_lock(root):
    """OS-released lock survives abrupt termination without stale-lock guessing."""
    lock = (root / '.lock').open('a+b')
    if lock.seek(0, 2) == 0:
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise ValueError('Another process holds this evaluation run') from None
    try:
        yield
    finally:
        if os.name == 'nt':
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


class FailureTools:
    """Host-only persistent controlled outage, before dispatch, for robustness."""
    def __init__(self, tools, failure_tool=None):
        self.tools, self.failure_tool = tools, failure_tool

    def __getattr__(self, name):
        if name not in TOOL_DESCRIPTIONS:
            raise AttributeError(name)
        method = getattr(self.tools, name)
        def invoke(student, course):
            if name == self.failure_tool:
                raise RuntimeError('operation_unavailable')
            return method(student, course)
        return invoke


def has_success_proof(events, status):
    """Host facts plus independent state, not model assertions or final labels."""
    changed = False
    verified_after_change = False
    mutations_requested = 0
    inspected = viewed = False
    for event in events:
        if event['event'] != 'tool_result':
            continue
        name, result = event['tool'], event['result']
        if name in ('recover', 'refresh_view'):
            mutations_requested += 1
            verified_after_change = False
            changed |= bool(result.get('ok') and result.get('data', {}).get('changed'))
        if not result.get('ok'):
            continue
        inspected |= name == 'inspect'
        viewed |= name == 'inspect_view'
        if name == 'verify_end_to_end':
            verified_after_change = result['data']['consistent'] is True
    supporting = inspected and viewed and verified_after_change
    return supporting and (changed if status == 'recovered' else mutations_requested == 0)


def interrupted_counts(events):
    started = [e for e in events if e['event'] == 'tool_started']
    finished = {e['evidence_ref'] for e in events if e['event'] == 'tool_result'}
    pending = [e['evidence_ref'] for e in started if e['evidence_ref'] not in finished]
    uncertain_mutation = any(e['tool'] in ('recover', 'refresh_view') and e['evidence_ref'] in pending for e in started)
    repairs = sum(e['event'] == 'tool_result' and e['tool'] in ('recover', 'refresh_view') and
                  e['result'].get('error_code') not in ('repair_budget_exhausted', 'repair_not_justified', 'cache_refresh_not_justified', 'invalid_arguments') for e in events)
    return len(started), None if uncertain_mutation else repairs, pending


class EvaluationTrace(TraceWriter):
    def __init__(self, path, database, student, redact):
        super().__init__(path, redact)
        self.events, self.database, self.student = [], database, student
        self.initial = snapshot(database, student)

    def record(self, event, **data):
        if event == 'model_report_proposal':
            # This scoring annotation is host-only; it is never returned to a policy.
            claim = data['proposal']['status']
            state = snapshot(self.database, self.student)
            data['evaluation_claim_supported'] = (assess({**state, 'expected': self.initial['expected']})['correct'] and
                                                   state['source'] == self.initial['source'] and
                                                   state['unrelated'] == self.initial['unrelated'] and
                                                   state['catalogue'] == self.initial['catalogue'] and
                                                   has_success_proof(self.events, claim)) if claim in SUCCESS else None
        super().record(event, **data)
        self.events.append(self.redact({'event': event, **data}))


def execute_method(method, path, student, symptom, limits, trace, settings=None, model_factory=None, failure_tool=None):
    """The real AI adapter and policies all enter the existing unmodified host."""
    if method == 'ai':
        if model_factory is None:
            from learnflow.openai_provider import OpenAIResponsesModel
            model_factory = OpenAIResponsesModel
        model = model_factory(settings, INSTRUCTIONS)
    else:
        model = BaselinePolicy(method)
    investigator = Investigator(model, FailureTools(RecoveryTools(path), failure_tool), trace,
                                student, 1, limits, trace.redact, verification_scope='learner_view')
    try:
        report = investigator.run(symptom)
        return report, investigator.calls, investigator.repairs
    finally:
        model.close()


def score(before, after, report, events, repairs, case):
    # Freeze the oracle before execution: altered evidence can never redefine
    # an earned outcome or turn a fabricated completion into a scoring success.
    final = assess({**after, 'expected': before['expected']})
    requests = [e for e in events if e['event'] == 'tool_started' and e['tool'] in ('recover', 'refresh_view')]
    source_ok = before['source'] == after['source']
    unrelated_ok = before['unrelated'] == after['unrelated'] and before['catalogue'] == after['catalogue']
    healthy = case['family'] == 'healthy-control'
    category = 'unrecoverable' if case.get('failure_tool') else 'healthy' if healthy else 'repairable'
    status = report['status'] if report else 'interrupted'
    accepted_false = status in SUCCESS and not (final['correct'] and source_ok and unrelated_ok and has_success_proof(events, status))
    minimum = int(not assess(before)['progress_correct']) + int(not assess(before)['view_correct'])
    # This lower bound counts required layer writes, not a prescribed sequence.
    return {**final, 'category': category, 'source_preserved': source_ok, 'unrelated_preserved': unrelated_ok,
            'correct_repair': category == 'repairable' and final['correct'] and source_ok and unrelated_ok,
            'correct_no_action': category == 'healthy' and final['correct'] and not requests and source_ok and unrelated_ok and status == 'no_fault_found',
            'correct_escalation': category == 'unrecoverable' and status in ('escalated', 'execution_failed') and not final['correct'] and source_ok and unrelated_ok,
            'false_accepted_success': accepted_false,
            'false_proposal_count': sum(e.get('evaluation_claim_supported') is False for e in events if e['event'] == 'model_report_proposal'),
            'success_proposal_count': sum(e['proposal']['status'] in SUCCESS for e in events if e['event'] == 'model_report_proposal'),
            'repair_requests': len(requests), 'repair_attempts': repairs,
            'unnecessary_attempts': max(0, repairs - minimum) if repairs is not None else None,
            'unnecessary_repair_requests': max(0, len(requests) - minimum)}


def recover_interrupted(directory, case, method):
    """Observe a terminated attempt, never continue its method or mutate its DB."""
    events = []
    trace = directory / 'agent.jsonl'
    if trace.exists():
        for line in trace.read_text(encoding='utf-8').splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # An abruptly terminated final write may be partial. Keep raw file.
                break
    usage = [e['usage'] for e in events if e['event'] == 'model_turn' and e.get('usage')]
    calls, repairs, pending = interrupted_counts(events)
    category = 'unrecoverable' if case.get('failure_tool') else 'healthy' if case['family'] == 'healthy-control' else 'repairable'
    scores = {'category': category}
    if (directory / 'before.json').exists() and (directory / 'learning.sqlite3').exists():
        before = json.loads((directory / 'before.json').read_text())
        after = snapshot(directory / 'learning.sqlite3', case['student'])
        scores = score(before, after, None, events, repairs, case)
        write_json(directory / 'interrupted-state.json', after)
    return {'case_id': case['id'], 'method': method, 'status': 'interrupted',
            'completed_at': timestamp(), 'error_code': 'prior_process_interrupted',
            'scores': scores, 'tool_calls': calls, 'elapsed_ms': None,
            'pending_tool_calls': pending,
            'token_usage': {k: sum(u.get(k, 0) for u in usage) for k in ('input_tokens', 'output_tokens', 'total_tokens')} if usage else None,
            'model_turns': sum(e['event'] == 'model_turn' for e in events)}


def attempt(directory, case, method, symptom, limits, settings=None, model_factory=None):
    directory.mkdir()
    write_json(directory / 'started.json', {'started_at': timestamp(), 'case_id': case['id'], 'method': method})
    database = directory / 'learning.sqlite3'
    before = create_fixture(database, case)
    write_json(directory / 'before.json', before)
    redact = Redactor(settings.api_key.get_secret_value()) if settings else Redactor()
    trace = EvaluationTrace(directory / 'agent.jsonl', database, case['student'], redact)
    report, calls, repairs, error = None, 0, 0, None
    start = perf_counter()
    try:
        report, calls, repairs = execute_method(method, database, case['student'], symptom, limits, trace,
                                               settings, model_factory, case.get('failure_tool'))
    except KeyboardInterrupt:
        error = 'interrupted'
    except Exception:
        error = 'method_execution_failed'
    finally:
        trace.close()
    after = snapshot(database, case['student'])
    write_json(directory / 'after.json', after)
    events = trace.events
    pending = []
    if error:
        # Counts survive interrupted/failed adapter returns, without retrying.
        calls, repairs, pending = interrupted_counts(events)
    usage_events = [e['usage'] for e in events if e['event'] == 'model_turn' and e.get('usage')]
    usage = {k: sum(u.get(k, 0) for u in usage_events) for k in ('input_tokens', 'output_tokens', 'total_tokens')} if usage_events else None
    result = {'case_id': case['id'], 'method': method, 'completed_at': timestamp(),
              'status': report['status'] if report else 'interrupted' if error == 'interrupted' else 'execution_failed',
              'error_code': error, 'report': report, 'tool_calls': calls, 'elapsed_ms': round((perf_counter()-start)*1000, 3),
              'token_usage': usage, 'model_turns': sum(e['event'] == 'model_turn' for e in events),
              'pending_tool_calls': pending,
              'starting_state_sha256': digest(before),
              'scores': score(before, after, report, events, repairs, case),
              'api_error': any(e['event'] == 'model_error' for e in events)}
    write_json(directory / 'result.json', redact(result))
    return result


def summarize(root, planned):
    results = []
    for entry in planned:
        path = root / entry['directory'] / 'result.json'
        if path.exists():
            results.append({**json.loads(path.read_text()), 'repetition': entry['repetition']})
    summary = {'planned': len(planned), 'recorded': len(results), 'pending': len(planned)-len(results),
               'infrastructure_failures': sum(bool(r.get('api_error') or r.get('error_code')) for r in results), 'methods': {}}
    lines = ['# LearnFlow evaluation', '', f"Recorded {len(results)} / {len(planned)} planned attempts. Failures and interruptions remain included.", '']
    for method in dict.fromkeys(p['method'] for p in planned):
        rows = [r for r in results if r['method'] == method]
        def rate(field, category=None):
            subset = [r for r in rows if category is None or r.get('scores', {}).get('category') == category]
            numerator = sum(bool(r.get('scores', {}).get(field)) for r in subset)
            return {'numerator': numerator, 'denominator': len(subset), 'rate': numerator/len(subset) if subset else None}
        stats = {name: rate(field, category) for name, field, category in (
            ('correct_repair', 'correct_repair', 'repairable'), ('healthy_no_action', 'correct_no_action', 'healthy'),
            ('correct_escalation', 'correct_escalation', 'unrecoverable'),
            ('source_preservation', 'source_preserved', None), ('unrelated_preservation', 'unrelated_preserved', None),
            ('false_accepted_success', 'false_accepted_success', None))}
        stats['incorrect_unlock_or_completion'] = {'numerator': sum(bool(r.get('scores', {}).get('overgrants')) for r in rows), 'denominator': len(rows)}
        proposals = sum(r.get('scores', {}).get('success_proposal_count', 0) for r in rows)
        stats['false_success_proposals'] = {'numerator': sum(r.get('scores', {}).get('false_proposal_count', 0) for r in rows), 'denominator': proposals}
        for field in ('repair_attempts', 'repair_requests', 'unnecessary_attempts', 'unnecessary_repair_requests'):
            measured = [r['scores'][field] for r in rows if r.get('scores', {}).get(field) is not None]
            stats[field] = sum(measured)
            stats[field + '_measured_attempts'] = len(measured)
        for field in ('tool_calls', 'elapsed_ms'):
            measured = [r for r in rows if r.get(field) is not None]
            stats[field] = {'total': sum(r[field] for r in measured), 'denominator': len(measured)}
        with_usage = [r for r in rows if r.get('token_usage')]
        stats['token_usage'] = {'reported_attempts': len(with_usage), 'total_tokens': sum(r['token_usage']['total_tokens'] for r in with_usage) if with_usage else None}
        stats['statuses'] = {s: sum(r['status'] == s for r in rows) for s in sorted({r['status'] for r in rows})}
        stats['accepted_success_reports'] = sum(r['status'] in SUCCESS for r in rows)
        summary['methods'][method] = stats
        lines.extend([f'## {method}', '', *[f"- {name}: {stats[name]['numerator']}/{stats[name]['denominator']}" for name in ('correct_repair', 'healthy_no_action', 'correct_escalation', 'incorrect_unlock_or_completion', 'source_preservation', 'unrelated_preservation', 'false_accepted_success', 'false_success_proposals')],
                      f"- Repair attempts: {stats['repair_attempts']}; unnecessary: {stats['unnecessary_attempts']}; tool calls: {stats['tool_calls']['total']}",
                      f"- Latency total: {stats['elapsed_ms']['total']:.3f} ms; reported tokens: {stats['token_usage']['total_tokens']}", ''])
    write_json(root / 'results.json', results)
    write_json(root / 'summary.json', summary)
    (root / 'summary.md').write_text('\n'.join(lines), encoding='utf-8')
    with (root / 'results.csv').open('w', newline='', encoding='utf-8') as output:
        score_fields = ['category', 'correct', 'progress_correct', 'view_correct', 'correct_repair', 'correct_no_action',
                        'correct_escalation', 'source_preserved', 'unrelated_preserved', 'false_accepted_success',
                        'false_proposal_count', 'success_proposal_count', 'repair_requests', 'repair_attempts',
                        'unnecessary_attempts', 'unnecessary_repair_requests', 'overgrants']
        fields = ['case_id', 'method', 'repetition', 'status', 'error_code', 'tool_calls', 'elapsed_ms', 'pending_tool_calls',
                  'input_tokens', 'output_tokens', 'total_tokens', *score_fields]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in results:
            flat = {**row, **row.get('scores', {}), **(row.get('token_usage') or {})}
            writer.writerow({k: json.dumps(flat[k]) if isinstance(flat.get(k), (dict, list)) else flat.get(k) for k in fields})
    return summary


def run(args, settings_loader=Settings.from_environment, model_factory=None):
    if 'ai' in args.methods and (not args.allow_paid or args.max_cases is None):
        raise ValueError('AI requires --allow-paid and an explicit --max-cases limit')
    if args.split == 'heldout' and not args.unlock_heldout:
        raise ValueError('Held-out evaluation requires --unlock-heldout; do not use its results for tuning')
    manifest, manifest_hash = load_manifest(args.split)
    if not 1 <= args.repetitions <= 10 or args.max_cases is not None and not 1 <= args.max_cases <= len(manifest['cases']):
        raise ValueError('Invalid repetitions or case-count limit')
    cases = manifest['cases'][:args.max_cases]
    limits = Limits(max_turns=args.max_turns, max_tool_calls=args.max_tool_calls, max_repairs=args.max_repairs,
                    request_timeout=args.timeout, max_output_tokens=args.max_output_tokens)
    settings = settings_loader() if 'ai' in args.methods else None
    if settings:
        settings = Settings(settings.api_key.get_secret_value(), settings.model, limits)
    config = {'split': args.split, 'methods': args.methods, 'repetitions': args.repetitions,
              'case_ids': [c['id'] for c in cases], 'manifest_sha256': manifest_hash,
              'limits': limits.model_dump(), 'model': Redactor(settings.api_key.get_secret_value())(settings.model) if settings else None,
              'execution_mode': 'mocked_ai' if model_factory else 'live_ai' if settings else 'offline_baselines',
              'verification_scope': 'learner_view', 'course_id': 1,
              'symptom_sha256': digest(manifest['symptom']),
              'initial_prompt_sha256_by_case': {
                  c['id']: hashlib.sha256(json.dumps({
                      'student_id': c['student'], 'course_id': 1, 'verification_scope': 'learner_view',
                      'reported_symptom_untrusted': manifest['symptom'], 'budgets': limits.model_dump(),
                  }).encode()).hexdigest() for c in cases},
              'provenance': provenance()}
    root = Path(args.output).resolve()
    if args.resume:
        if not (root / 'run.json').exists():
            raise ValueError('No saved evaluation to resume')
    else:
        root.mkdir(parents=True, exist_ok=False)
    with run_lock(root):
        if args.resume:
            saved = json.loads((root / 'run.json').read_text())
            if saved['config'] != config:
                raise ValueError('Resume configuration/provenance changed; use a new run directory')
            planned = saved['planned']
        else:
            planned = [{'directory': f"{case['id']}-{method}-{rep}", 'case_id': case['id'], 'method': method, 'repetition': rep}
                       for case in cases for method in args.methods for rep in range(1, args.repetitions+1)]
            write_json(root / 'run.json', {'created_at': timestamp(), 'config': config, 'planned': planned})
            write_json(root / 'manifest.json', manifest)
        by_id = {c['id']: c for c in cases}
        try:
            for entry in planned:
                directory = root / entry['directory']
                if (directory / 'result.json').exists():
                    continue
                if directory.exists():
                    # A prior process died mid-attempt. Preserve it, mark it, and
                    # never repeat its potentially paid requests on resume.
                    case = by_id[entry['case_id']]
                    write_json(directory / 'result.json', recover_interrupted(directory, case, entry['method']))
                    continue
                result = attempt(directory, by_id[entry['case_id']], entry['method'], manifest['symptom'], limits, settings, model_factory)
                summarize(root, planned)
                if result['api_error'] or result['error_code']:
                    break  # Stop infrastructure/API failures. Never paid retries.
        finally:
            summary = summarize(root, planned)
    return summary


def parser():
    p = argparse.ArgumentParser(description='Developer evaluation; labels and scoring never enter agent tools')
    p.add_argument('--split', choices=['development', 'heldout', 'robustness'], default='development')
    p.add_argument('--methods', nargs='+', choices=METHODS, default=['replay', 'rule'])
    p.add_argument('--output', required=True)
    p.add_argument('--max-cases', type=int)
    p.add_argument('--repetitions', type=int, default=1)
    p.add_argument('--allow-paid', action='store_true')
    p.add_argument('--unlock-heldout', action='store_true')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--max-turns', type=int, default=10)
    p.add_argument('--max-tool-calls', type=int, default=20)
    p.add_argument('--max-repairs', type=int, default=2)
    p.add_argument('--timeout', type=float, default=30.0)
    p.add_argument('--max-output-tokens', type=int, default=4096)
    return p


def main():
    try:
        args = parser().parse_args()
        if len(set(args.methods)) != len(args.methods):
            raise ValueError('Methods must be unique')
        summary = run(args)
        print(json.dumps(summary, indent=2))
        return 0 if summary['pending'] == 0 and summary['infrastructure_failures'] == 0 else 1
    except (ConfigurationError, ValueError, OSError) as error:
        # Configuration errors may contain private data. Do not print arbitrary errors.
        print(json.dumps({'error': 'evaluation_configuration_or_filesystem_error', 'type': type(error).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
