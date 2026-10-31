"""Offline CLI integration tests. Live opt-in is never exercised against OpenAI."""
import json

import pytest

from learnflow.agent_cli import main
from learnflow.agent_contracts import ModelTurn, ToolCall
from learnflow.scenarios import setup


@pytest.mark.parametrize('command', ['investigate', 'live-smoke'])
def test_cli_requires_explicit_live_opt_in(command):
    with pytest.raises(SystemExit) as error:
        main([command, '--student', '1', '--course', '1', '--symptom', 'Locked lesson'])
    assert error.value.code == 2


def test_cli_with_mocked_model_and_offline_trace(tmp_path, monkeypatch, capsys):
    import learnflow.openai_provider as provider
    path = tmp_path / 'demo.sqlite3'
    trace = tmp_path / 'trace.jsonl'
    setup(path, 'healthy-control')
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-credential')
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    turns = iter([
        ModelTurn(calls=[ToolCall('c1', 'inspect', '{}')]),
        ModelTurn(calls=[ToolCall('c2', 'inspect_view', '{}')]),
        ModelTurn(calls=[ToolCall('c3', 'verify_end_to_end', '{}')]),
        ModelTurn(text=json.dumps({'status': 'no_fault_found', 'evidence_references': ['tool-0001', 'tool-0002', 'tool-0003'],
                                  'diagnosis': 'The failed quiz correctly prevents completion.',
                                  'remaining_limitations': [], 'recommended_next_steps': []})),
    ])
    class FakeModel:
        def __init__(self, *args):
            pass
        def next_turn(self, *args):
            return next(turns)
        def close(self):
            pass
    monkeypatch.setattr(provider, 'OpenAIResponsesModel', FakeModel)
    result = main(['investigate', '--allow-paid', '--db', str(path), '--student', '1', '--course', '1',
                   '--symptom', 'Locked lesson', '--trace-file', str(trace), '--env-file', str(tmp_path/'missing.env')])
    output = capsys.readouterr().out
    assert result == 0 and json.loads(output)['report']['status'] == 'no_fault_found'
    assert json.loads(output)['report']['verification_scope'] == 'learner_view'
    assert json.loads(output)['report']['verification_outcome']['consistent'] is True
    assert 'unit-test-credential' not in output + trace.read_text()
    assert main(['trace', '--file', str(trace)]) == 0
    assert 'no_fault_found' in capsys.readouterr().out
    previous = trace.read_text()
    # A second run must not overwrite an earlier trace.
    assert main(['investigate', '--allow-paid', '--db', str(path), '--student', '1', '--course', '1',
                 '--symptom', 'Locked lesson', '--trace-file', str(trace), '--env-file', str(tmp_path/'missing.env')]) == 1
    assert trace.read_text() == previous
