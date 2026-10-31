"""Local demo API adapters. No provider credentials or scenario labels reach the agent."""
import copy
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from learnflow.agent import INSTRUCTIONS, Investigator
from learnflow.agent_config import ConfigurationError, Redactor, Settings
from learnflow.agent_trace import TraceWriter
from learnflow.app import create_app as domain_app
from learnflow.db import connect, initialize, reset
from learnflow.tools import RecoveryTools, validate


def now():
    return datetime.now(timezone.utc).isoformat()


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    student_id: int = Field(ge=1)
    course_id: int = Field(ge=1)
    symptom: str = Field(min_length=1, max_length=4000)
    allow_paid: bool
    request_id: str = Field(min_length=36, max_length=36)


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    scenario: Literal['missed-processing', 'partial-update', 'healthy-control', 'stale-view', 'combined']


class ObservedTrace(TraceWriter):
    def __init__(self, path, redact, publish):
        super().__init__(path, redact)
        self.publish = publish

    def record(self, event, **data):
        super().record(event, **data)
        self.publish(self.redact({'time': now(), 'event': event, **data}))


class DemoService:
    def __init__(self, root, settings_loader, model_factory=None):
        self.root = Path(root).resolve()
        marker = self.root / 'purpose.txt'
        if self.root.exists() and not marker.is_file() and any(self.root.iterdir()):
            raise ValueError('Refusing to use an unmarked existing directory for demo data')
        self.root.mkdir(parents=True, exist_ok=True)
        if marker.exists() and marker.read_text() != 'learnflow-local-demo-v1':
            raise ValueError('Wrong demo workspace purpose')
        if not marker.exists():
            marker.write_text('learnflow-local-demo-v1')
        self.db_path = self.root / 'learning.sqlite3'
        if self.db_path.exists():
            initialize(self.db_path)
        else:
            reset(self.db_path)
        self.settings_loader, self.model_factory = settings_loader, model_factory
        self.lock = threading.RLock()
        self.runs = {}
        self.requests = {}
        self.active_id = None

    def snapshot(self, student_id, course_id):
        tools = RecoveryTools(self.db_path)
        return {'progress': tools.read_progress(student_id, course_id),
                'learner_view': tools.inspect_view(student_id, course_id),
                'verification': tools.verify_end_to_end(student_id, course_id)}

    def get(self, run_id):
        with self.lock:
            if run_id not in self.runs:
                raise HTTPException(404, 'Unknown investigation')
            return copy.deepcopy(self.runs[run_id])

    def start(self, body):
        if body.allow_paid is not True:
            raise HTTPException(400, 'Explicit paid API approval is required')
        try:
            request_key = str(UUID(body.request_id))
        except ValueError:
            raise HTTPException(422, 'Invalid request ID') from None
        if not body.symptom.strip():
            raise HTTPException(422, 'Describe the reported symptom')
        identity = (body.student_id, body.course_id, hashlib.sha256(body.symptom.encode()).hexdigest())
        with self.lock:
            if request_key in self.requests:
                prior_id, prior_identity = self.requests[request_key]
                if prior_identity != identity:
                    raise HTTPException(409, 'Request ID already used for another input')
                return self.get(prior_id)
            if self.active_id:
                raise HTTPException(409, 'An investigation is already active')
            with connect(self.db_path) as db:
                try:
                    validate(db, body.student_id, body.course_id)
                except ValueError:
                    raise HTTPException(404, 'Unknown student or course') from None
            try:
                settings = self.settings_loader()  # Only explicit POST loads server credentials.
            except (ConfigurationError, ValueError, OSError):
                raise HTTPException(503, 'Server AI configuration is unavailable; check local server settings') from None
            redact = Redactor(settings.api_key.get_secret_value())
            run_id = uuid4().hex
            directory = self.root / 'runs' / run_id
            directory.mkdir(parents=True)
            before = redact(self.snapshot(body.student_id, body.course_id))
            job = {'id': run_id, 'student_id': body.student_id, 'course_id': body.course_id,
                   'status': 'running', 'started_at': now(), 'completed_at': None,
                   'execution_mode': 'mocked_test' if self.model_factory else 'live_openai',
                   'model_name': redact(settings.model), 'limits': settings.limits.model_dump(),
                   'events': [], 'before': before, 'after': None, 'report': None, 'error_code': None}
            self.runs[run_id] = job
            self.requests[request_key] = (run_id, identity)
            self.active_id = run_id
            (directory / 'before.json').write_text(json.dumps(before, indent=2), encoding='utf-8')
            worker = threading.Thread(target=self.execute, args=(run_id, body.symptom, settings, directory, redact), daemon=True)
            try:
                worker.start()
            except RuntimeError:
                self.runs[run_id].update(status='execution_failed', completed_at=now(), error_code='worker_start_failed')
                self.active_id = None
                raise HTTPException(503, 'Investigation worker could not start; no automatic retry was made') from None
            return self.get(run_id)

    def execute(self, run_id, symptom, settings, directory, redact):
        writer = model = None
        with self.lock:
            job = self.runs[run_id]
            student_id, course_id = job['student_id'], job['course_id']
        def publish(entry):
            with self.lock:
                self.runs[run_id]['events'].append(entry)
        report, error, after = None, None, None
        try:
            writer = ObservedTrace(directory / 'agent.jsonl', redact, publish)
            writer.record('provider', provider='openai_responses', model=settings.model)
            factory = self.model_factory
            if factory is None:
                from learnflow.openai_provider import OpenAIResponsesModel
                factory = OpenAIResponsesModel
            model = factory(settings, INSTRUCTIONS)
            report = Investigator(model, RecoveryTools(self.db_path), writer, student_id, course_id,
                                  settings.limits, redact, verification_scope='learner_view').run(symptom)
        except Exception:
            error = 'investigation_execution_failed'
        finally:
            if model is not None:
                try:
                    model.close()
                except Exception:
                    pass
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    error = 'trace_write_failed'
            try:
                after = redact(self.snapshot(student_id, course_id))
                (directory / 'after.json').write_text(json.dumps(after, indent=2), encoding='utf-8')
            except Exception:
                error = error or 'post_run_snapshot_failed'
            with self.lock:
                self.runs[run_id].update(status='execution_failed' if error else 'finished',
                                        completed_at=now(), report=redact(report), after=after, error_code=error)
                try:
                    (directory / 'run.json').write_text(json.dumps(self.runs[run_id], indent=2), encoding='utf-8')
                except OSError:
                    self.runs[run_id].update(status='execution_failed', error_code='run_record_write_failed')
                self.active_id = None

    def load_scenario(self, name):
        with self.lock:
            if self.active_id:
                raise HTTPException(409, 'Cannot reset demo data while an investigation is active')
            from learnflow.scenarios import setup
            setup(self.db_path, name)
            return {'student_id': 1, 'course_id': 1, 'message': 'Dedicated synthetic demo data reset'}


def create_app(demo_mode=False, demo_root=None, settings_loader=None, model_factory=None):
    service = DemoService(demo_root or Path.cwd() / '.local-demo', settings_loader or Settings.from_environment,
                          model_factory) if demo_mode else None
    app = domain_app(service.db_path if service else None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]'])
    app.state.demo_service = service

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            allowed = {'http://127.0.0.1:5173', 'http://localhost:5173',
                       'http://127.0.0.1:8000', 'http://localhost:8000'}
            if origin and origin not in allowed:
                return JSONResponse({'detail': 'Non-local origin rejected'}, status_code=403)
            if request.client and request.client.host not in ('127.0.0.1', '::1', 'testclient'):
                return JSONResponse({'detail': 'Local access only'}, status_code=403)
            if service and service.active_id and not request.url.path.startswith('/web/'):
                return JSONResponse({'detail': 'Investigation active; demo writes are locked'}, status_code=409)
        return await call_next(request)

    def require_demo():
        if service is None:
            raise HTTPException(404, 'Local demo mode is disabled')
        return service

    @app.get('/web/config')
    def config():
        return {'demo_mode': demo_mode, 'paid_investigations': demo_mode,
                'execution_mode': 'mocked_test' if model_factory else 'live_openai'}

    @app.get('/web/runs/active')
    def active():
        current = require_demo()
        with current.lock:
            return {'run': current.get(current.active_id) if current.active_id else None}

    @app.get('/web/runs/{run_id}')
    def get_run(run_id: str):
        return require_demo().get(run_id)

    @app.post('/web/runs', status_code=202)
    def start_run(body: InvestigationRequest):
        return require_demo().start(body)

    @app.post('/web/demo/scenario')
    def scenario(body: ScenarioRequest):
        return require_demo().load_scenario(body.scenario)

    return app


def local_app():
    return create_app(demo_mode=os.environ.get('LEARNFLOW_DEMO_MODE') == '1')
