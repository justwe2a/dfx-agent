"""Local estimate of the complete outgoing context; never an API usage claim."""
import json
from pathlib import Path


class ContextBudget:
    def __init__(self, window=1_000_000, headroom=0.10, tokenizer_dir=None):
        if type(window) is not int or window <= 0:
            raise ValueError('context_window_tokens must be a positive integer')
        if type(headroom) not in (int, float) or not 0 <= headroom < 1:
            raise ValueError('context_headroom_ratio must be in [0, 1)')
        from tokenizers import Tokenizer
        self.directory = (Path(tokenizer_dir) if tokenizer_dir else
                          Path(__file__).parent / 'data/deepseek-v4').resolve()
        self.tokenizer = Tokenizer.from_file(str(self.directory / 'tokenizer.json'))
        self.window, self.headroom = window, headroom

    def configuration(self):
        return {'context_window_tokens': self.window, 'context_headroom_ratio': self.headroom,
                'tokenizer_dir': str(self.directory)}

    def measure(self, history, tools, output_tokens):
        encoded = json.dumps({'messages': history, **({'tools': tools} if tools else {})},
                             ensure_ascii=False, separators=(',', ':'))
        tokens = len(self.tokenizer.encode(encoded, add_special_tokens=False).ids)
        capacity = int(self.window * (1 - self.headroom))
        return {'estimated_input_tokens': tokens, 'max_output_tokens': output_tokens,
                'capacity_tokens': capacity, 'fits': tokens + output_tokens <= capacity,
                'history_chars': len(json.dumps(history, ensure_ascii=False)),
                'serialized_context_bytes': len(encoded.encode('utf-8')),
                'actual_prompt_tokens': None, 'estimate_minus_actual': None}

    def prepare(self, history, tools, output_tokens, used, maximum, *, closing_requests=0):
        # A request-local status suffix: persisted assistant/tool history is unchanged.
        initial = self.measure(history, tools, output_tokens)
        remaining = max(0, maximum-used)
        closing = (f' Of these, {max(0, remaining-1)} may use working tools; the last request is finish-only. '
                   'Submit actual results and unverified requirements, including incomplete work; '
                   'no extra request or automatic retry follows. ' if closing_requests else '')
        if closing_requests and remaining == 1:
            closing = (' FINAL DECLARATION REQUEST: 0 may use working tools. '
                       'Call finish now using the evidence already in this history. '
                       'Do not call a working tool or continue an unfinished UI interaction. '
                       'Describe unfinished checks and any uncertain current state in unverified; '
                       'do not claim success for them. Plain text is not a submission. '
                       'This request is within the original limit; no further request or retry follows. ')
        status = (f'\nRuntime status: completed requests {used}; remaining at most {remaining}. '
                  + closing +
                  f'Input estimate before this status {initial["estimated_input_tokens"]} tokens; '
                  f'combined input/output capacity {initial["capacity_tokens"]}; '
                  f'output allowance {output_tokens}. Full history retained. '
                  'Finish with actual results or remaining uncertainty when necessary.')
        outgoing = [dict(m) for m in history]
        # Keep the initial instructions and complete conversation prefix stable.
        # This request-local status follows complete tool observations, never splits a call/result pair.
        outgoing.append({'role': 'system', 'content': status})
        if closing_requests and remaining == 1:
            # Place the transition after the last tool result as well as in the
            # system status; preserve every earlier message and call/result pair.
            outgoing.append({'role': 'user', 'content': closing.strip()})
        return outgoing, self.measure(outgoing, tools, output_tokens)
