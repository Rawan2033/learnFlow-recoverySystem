"""Deterministic policies using only the same model/tool protocol as the AI.

No fixtures, labels, database paths or scoring expectations enter these policies.
Investigator supplies scope binding, preconditions, limits and final validation.
"""
import json

from learnflow.agent_contracts import ModelTurn, ToolCall


class BaselinePolicy:
    def __init__(self, method):
        if method not in ('replay', 'rule'):
            raise ValueError('Unknown baseline')
        self.method = method
        self.results = []
        self.initial = iter(('inspect', 'inspect_view', 'verify_end_to_end'))
        self.counter = 0

    def call(self, name):
        self.counter += 1
        return ModelTurn(calls=[ToolCall(str(self.counter), name, '{}')])

    def finish(self, status, diagnosis):
        return ModelTurn(text=json.dumps({
            'status': status,
            'evidence_references': [r['evidence_ref'] for _, r in self.results],
            'diagnosis': diagnosis,
            'remaining_limitations': ['Tool-observable backend consistency only.'],
            'recommended_next_steps': [] if status in ('recovered', 'no_fault_found') else ['Review recorded evidence and operational errors.'],
        }))

    def next_turn(self, prompt, results):
        # Only tool outputs are used, never the reported symptom as instructions.
        for item in results:
            self.results.append((self.pending, item.result))
        if self.results and not self.results[-1][1]['ok']:
            return self.finish('escalated', 'A required operation failed; stopped without retry.')
        name = next(self.initial, None)
        if name:
            self.pending = name
            return self.call(name)
        last_name, last = self.results[-1]
        if last_name in ('recover', 'refresh_view'):
            self.pending = 'verify_end_to_end'
            return self.call(self.pending)
        check = last['data']
        mutated = any(n in ('recover', 'refresh_view') for n, _ in self.results)
        if check['consistent']:
            return self.finish('recovered' if mutated else 'no_fault_found',
                               'The recorded checks agree with evidence and the stored learner view.')
        if not check['progress']['consistent'] and not mutated:
            self.pending = 'recover'
            return self.call(self.pending)
        if (self.method == 'rule' and check['progress']['consistent'] and
                not check['learner_view']['consistent'] and
                not any(n == 'refresh_view' for n, _ in self.results)):
            self.pending = 'refresh_view'
            return self.call(self.pending)
        return self.finish('escalated', 'Remaining mismatches cannot be resolved by this policy within its supported operations.')

    def close(self):
        pass
