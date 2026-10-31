"""Local configuration; no configuration object is logged or printed."""
import os
import re

from dotenv import load_dotenv
from pydantic import SecretStr

from learnflow.agent_contracts import Limits


class ConfigurationError(Exception):
    pass


class Redactor:
    def __init__(self, secret=''):
        self._secret = secret

    def __call__(self, value):
        if isinstance(value, str):
            if self._secret:
                value = value.replace(self._secret, '[REDACTED]')
            return re.sub(r'\bsk-[A-Za-z0-9_-]+', '[REDACTED]', value)
        if isinstance(value, dict):
            return {self(str(k)): self(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self(v) for v in value]
        return value


class Settings:
    def __init__(self, api_key, model, limits):
        self.api_key = SecretStr(api_key)
        self.model = model
        self.limits = limits

    @classmethod
    def from_environment(cls, env_file='.env'):
        # Explicit local path, no parent-directory search and no interpolation of secrets.
        load_dotenv(env_file, override=False, interpolate=False)
        key = os.environ.get('OPENAI_API_KEY', '').strip()
        model = os.environ.get('OPENAI_MODEL', '').strip()
        if not key or key == 'your-api-key-here' or not model or model == 'your-function-calling-model-here':
            raise ConfigurationError('Set OPENAI_API_KEY and OPENAI_MODEL locally.')
        try:
            limits = Limits(
                max_turns=int(os.environ.get('LEARNFLOW_MAX_TURNS', '10')),
                max_tool_calls=int(os.environ.get('LEARNFLOW_MAX_TOOL_CALLS', '20')),
                max_repairs=int(os.environ.get('LEARNFLOW_MAX_REPAIRS', '2')),
                request_timeout=float(os.environ.get('LEARNFLOW_REQUEST_TIMEOUT', '30')),
                max_output_tokens=int(os.environ.get('LEARNFLOW_MAX_OUTPUT_TOKENS', '4096')),
            )
        except ValueError:
            raise ConfigurationError('Invalid agent budget configuration.') from None
        return cls(key, model, limits)
