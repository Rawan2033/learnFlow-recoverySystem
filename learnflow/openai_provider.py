"""OpenAI-specific Responses API transport; no database or recovery access."""
import json

from openai import OpenAI

from learnflow.agent_contracts import (
    EmptyArguments, ModelTurn, ProposedReport, TOOL_DESCRIPTIONS, ToolCall,
)


class ProviderError(Exception):
    """Deliberately contains no upstream exception body or request headers."""


class OpenAIResponsesModel:
    def __init__(self, settings, instructions, client=None):
        self.client = client or OpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url='https://api.openai.com/v1',
            timeout=settings.limits.request_timeout,
            max_retries=0,
        )
        self.settings = settings
        self.instructions = instructions
        self.history = []

    def next_turn(self, prompt, results):
        if prompt is not None:
            self.history.append({'role': 'user', 'content': prompt})
        for result in results:
            self.history.append({'type': 'function_call_output', 'call_id': result.call_id,
                                 'output': json.dumps(result.result)})
        try:
            response = self.client.responses.create(
                model=self.settings.model,
                instructions=self.instructions,
                input=self.history,
                tools=[{'type': 'function', 'name': name, 'description': description,
                        'parameters': EmptyArguments.model_json_schema(), 'strict': True}
                       for name, description in TOOL_DESCRIPTIONS.items()],
                parallel_tool_calls=False,
                store=False,
                include=['reasoning.encrypted_content'],
                max_output_tokens=self.settings.limits.max_output_tokens,
                text={'format': {'type': 'json_schema', 'name': 'investigation_report',
                                 'schema': ProposedReport.model_json_schema(), 'strict': True}},
            )
            # Keep response items (including opaque reasoning continuation) only in
            # memory for protocol continuity. Never persist/log hidden reasoning.
            self.history.extend(response.output)
            calls = [ToolCall(item.call_id, item.name, item.arguments)
                     for item in response.output if item.type == 'function_call']
            usage = {}
            if response.usage:
                usage = {name: getattr(response.usage, name) for name in
                         ('input_tokens', 'output_tokens', 'total_tokens')}
            return ModelTurn(calls=calls, text=response.output_text, usage=usage,
                             response_status=response.status)
        except ProviderError:
            raise
        except Exception:
            # Raw SDK failures can contain response bodies, input, or credentials.
            raise ProviderError('openai_request_failed') from None

    def close(self):
        self.client.close()
