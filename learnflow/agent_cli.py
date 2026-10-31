"""Explicitly opt-in, potentially paid investigation CLI; trace viewing is offline."""
import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from learnflow.agent import INSTRUCTIONS, Investigator
from learnflow.agent_config import ConfigurationError, Redactor, Settings
from learnflow.agent_trace import TraceWriter
from learnflow.db import connect
from learnflow.tools import RecoveryTools, validate


def main(argv=None):
    parser = argparse.ArgumentParser(description='LearnFlow investigation agent')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('investigate', 'live-smoke'):
        command = commands.add_parser(name)
        command.add_argument('--db', default=os.environ.get('LEARNFLOW_DB', 'learnflow.sqlite3'))
        command.add_argument('--student', type=int, required=True)
        command.add_argument('--course', type=int, required=True)
        command.add_argument('--symptom', required=True)
        command.add_argument('--trace-file')
        command.add_argument('--env-file', default='.env')
        command.add_argument('--allow-paid', action='store_true', help='Explicitly opt in to live API calls')
    trace_command = commands.add_parser('trace')
    trace_command.add_argument('--file', required=True)
    args = parser.parse_args(argv)
    if args.command == 'trace':
        try:
            # Trace files were redacted before persistence; no credentials loaded here.
            for line in Path(args.file).read_text(encoding='utf-8').splitlines():
                print(json.dumps(Redactor()(json.loads(line)), indent=2))
        except (OSError, ValueError):
            print(json.dumps({'status': 'execution_failed', 'error_code': 'trace_read_failed'}))
            return 1
        return 0
    if not args.allow_paid:
        parser.error('Live investigations require --allow-paid; routine pytest tests are offline.')
    writer = model = None
    try:
        settings = Settings.from_environment(args.env_file)
        redact = Redactor(settings.api_key.get_secret_value())
        if args.student < 1 or args.course < 1 or not 1 <= len(args.symptom.strip()) <= 4000:
            raise ConfigurationError('Supply positive IDs and a symptom of 1–4000 characters.')
        # Do not create or reset a missing demo database as a side effect of investigation.
        if not Path(args.db).is_file():
            raise ConfigurationError('Initialize a synthetic database before investigating.')
        with connect(args.db) as db:
            validate(db, args.student, args.course)
        path = args.trace_file or f'.traces/{uuid4().hex}.jsonl'
        writer = TraceWriter(path, redact)
        writer.record('provider', provider='openai_responses', model=settings.model)
        from learnflow.openai_provider import OpenAIResponsesModel
        model = OpenAIResponsesModel(settings, INSTRUCTIONS)
        report = Investigator(model, RecoveryTools(args.db), writer, args.student, args.course,
                              settings.limits, redact, verification_scope='learner_view').run(args.symptom)
        print(json.dumps(redact({'trace_file': path, 'report': report}), indent=2))
        return 0 if report['status'] in ('recovered', 'no_fault_found') else 1
    except ConfigurationError as exc:
        print(json.dumps({'status': 'execution_failed', 'error_code': 'configuration_error', 'message': str(exc)}))
        return 1
    except Exception:
        # No traceback, exception message, environment dump, or credential output.
        failure = {'status': 'execution_failed', 'error_code': 'investigation_execution_failed'}
        if writer is not None:
            try:
                writer.record('final', report=failure)
            except OSError:
                pass
        print(json.dumps(failure))
        return 1
    finally:
        if model is not None:
            try:
                model.close()
            except Exception:
                pass
        if writer is not None:
            writer.close()


if __name__ == '__main__':
    raise SystemExit(main())
