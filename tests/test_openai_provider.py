"""Official SDK + mocked HTTP transport. No network or paid API calls."""
import json

import httpx
import pytest
from openai import OpenAI

from learnflow.agent import INSTRUCTIONS
from learnflow.agent_config import ConfigurationError, Settings
from learnflow.agent_contracts import Limits, ToolResult
from learnflow.openai_provider import OpenAIResponsesModel, ProviderError


def settings():
    return Settings('sk-unit-test-placeholder', 'gpt-5.4-mini', Limits(request_timeout=12.0))


def response_body(output, status='completed'):
    return {'id': 'resp_test', 'object': 'response', 'created_at': 1,
            'model': 'gpt-5.4-mini', 'status': status, 'output': output,
            'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15,
                      'input_tokens_details': {'cached_tokens': 0},
                      'output_tokens_details': {'reasoning_tokens': 0}},
            'parallel_tool_calls': False, 'tools': [], 'tool_choice': 'auto',
            'error': None, 'incomplete_details': None, 'instructions': None,
            'metadata': {}, 'temperature': 1, 'top_p': 1}


def test_responses_protocol_and_strict_allowlist():
    requests = []
    outputs = [
        [{'id': 'rs_1', 'type': 'reasoning', 'summary': [], 'encrypted_content': 'opaque-continuation'},
         {'type': 'function_call', 'id': 'fc_1', 'call_id': 'c1', 'name': 'inspect',
          'arguments': '{}', 'status': 'completed'}],
        [{'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'status': 'completed',
          'content': [{'type': 'output_text', 'text': '{"status":"escalated"}', 'annotations': []}]}],
    ]
    def transport(request):
        assert request.url.host == 'api.openai.com' and request.url.path == '/v1/responses'
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_body(outputs[len(requests)-1]))
    client = OpenAI(api_key='sk-unit-test-placeholder', max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    model = OpenAIResponsesModel(settings(), INSTRUCTIONS, client=client)
    first = model.next_turn('{"reported_symptom_untrusted":"locked lesson"}', [])
    assert first.calls[0].name == 'inspect' and first.calls[0].call_id == 'c1'
    assert first.usage == {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}
    second = model.next_turn(None, [ToolResult('c1', {'ok': True, 'data': {'events': []}})])
    assert second.text == '{"status":"escalated"}'
    request = requests[0]
    assert request['store'] is False and request['parallel_tool_calls'] is False
    assert request['include'] == ['reasoning.encrypted_content']
    assert {t['name'] for t in request['tools']} == {
        'inspect', 'verify', 'recover', 'inspect_view', 'refresh_view', 'verify_end_to_end'}
    for tool in request['tools']:
        assert tool['type'] == 'function' and tool['strict'] is True
        assert tool['parameters']['additionalProperties'] is False
        assert tool['parameters']['properties'] == {}
    assert request['text']['format']['type'] == 'json_schema'
    assert request['text']['format']['strict'] is True
    continuation = requests[1]['input']
    assert any(i.get('encrypted_content') == 'opaque-continuation' for i in continuation)
    assert continuation[-1]['type'] == 'function_call_output'
    assert continuation[-1]['call_id'] == 'c1'
    assert json.loads(continuation[-1]['output'])['ok'] is True
    assert 'sk-unit-test-placeholder' not in json.dumps(requests)
    model.close()


def test_api_failure_has_no_raw_body_and_no_retry():
    count = 0
    def transport(request):
        nonlocal count
        count += 1
        return httpx.Response(429, json={'error': {'message': 'private server details', 'type': 'rate_limit_error'}})
    client = OpenAI(api_key='sk-unit-test-placeholder', max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    model = OpenAIResponsesModel(settings(), INSTRUCTIONS, client=client)
    with pytest.raises(ProviderError) as error:
        model.next_turn('synthetic symptom', [])
    assert str(error.value) == 'openai_request_failed' and count == 1
    model.close()


def test_all_function_calls_and_corrective_continuation_keep_protocol_history():
    requests = []
    proposal = json.dumps({'status': 'recovered', 'evidence_references': ['tool-0001', 'tool-0002'],
                          'diagnosis': 'Synthetic early claim.', 'remaining_limitations': [],
                          'recommended_next_steps': []})
    outputs = [
        [{'type': 'function_call', 'id': f'fc_{i}', 'call_id': f'c{i}', 'name': name,
          'arguments': '{}', 'status': 'completed'} for i, name in enumerate(['inspect', 'verify_end_to_end'], 1)],
        [{'id': 'msg_early', 'type': 'message', 'role': 'assistant', 'status': 'completed',
          'content': [{'type': 'output_text', 'text': proposal, 'annotations': []}]}],
        [{'type': 'function_call', 'id': 'fc_3', 'call_id': 'c3', 'name': 'recover',
          'arguments': '{}', 'status': 'completed'}],
    ]
    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_body(outputs[len(requests)-1]))
    client = OpenAI(api_key='sk-unit-test-placeholder', max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    model = OpenAIResponsesModel(settings(), INSTRUCTIONS, client=client)
    first = model.next_turn('synthetic symptom', [])
    assert [(c.name, c.call_id) for c in first.calls] == [('inspect', 'c1'), ('verify_end_to_end', 'c2')]
    second = model.next_turn(None, [ToolResult('c1', {'ok': True}), ToolResult('c2', {'ok': True})])
    assert second.calls == [] and second.text == proposal
    feedback = json.dumps({'message_type': 'host_validation_feedback', 'error_code': 'unsupported_recovery_claim'})
    third = model.next_turn(feedback, [])
    assert [(c.name, c.call_id) for c in third.calls] == [('recover', 'c3')]
    assert [i['call_id'] for i in requests[1]['input'] if i.get('type') == 'function_call_output'] == ['c1', 'c2']
    assert requests[2]['input'][-1] == {'role': 'user', 'content': feedback}
    assert any(i.get('id') == 'msg_early' for i in requests[2]['input'])
    assert 'previous_response_id' not in requests[2]  # Same explicit in-memory history.
    model.close()


def test_default_sdk_client_limits_and_official_endpoint(monkeypatch):
    import learnflow.openai_provider as provider
    captured = {}
    def factory(**kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr(provider, 'OpenAI', factory)
    model = OpenAIResponsesModel(settings(), INSTRUCTIONS)
    assert captured['max_retries'] == 0
    assert captured['timeout'] == 12.0
    assert captured['base_url'] == 'https://api.openai.com/v1'
    assert model.history == []


def test_timeout_is_sanitized_and_not_retried():
    count = 0
    def transport(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout('private timeout detail', request=request)
    client = OpenAI(api_key='sk-unit-test-placeholder', max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    model = OpenAIResponsesModel(settings(), INSTRUCTIONS, client=client)
    with pytest.raises(ProviderError) as error:
        model.next_turn('synthetic symptom', [])
    assert str(error.value) == 'openai_request_failed' and count == 1
    model.close()


def test_env_loading_is_local_and_environment_wins(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setenv('OPENAI_MODEL', 'environment-model')
    env = tmp_path / '.env'
    env.write_text('OPENAI_API_KEY=unit-test-value\nOPENAI_MODEL=file-model\nLEARNFLOW_MAX_TURNS=4\n')
    monkeypatch.delenv('LEARNFLOW_MAX_TURNS', raising=False)
    loaded = Settings.from_environment(env)
    assert loaded.model == 'environment-model' and loaded.limits.max_turns == 4
    assert 'unit-test-value' not in repr(loaded.api_key)
    assert loaded.api_key.get_secret_value() == 'unit-test-value'


def test_missing_config_and_invalid_budgets_are_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    monkeypatch.setenv('OPENAI_MODEL', '')
    with pytest.raises(ConfigurationError, match='Set OPENAI_API_KEY'):
        Settings.from_environment(tmp_path / 'missing.env')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-secret')
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    monkeypatch.setenv('LEARNFLOW_MAX_TURNS', 'test-secret')
    with pytest.raises(ConfigurationError) as error:
        Settings.from_environment(tmp_path / 'missing.env')
    assert str(error.value) == 'Invalid agent budget configuration.'
