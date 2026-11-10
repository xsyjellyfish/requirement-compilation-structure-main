import base64
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('disconnect', [False, True])
def test_entry_live_progress(tmp_path, disconnect):
    """A piped CLI must publish request-start before the local server responds."""
    released = threading.Event()
    received = []
    def response(request_id, command):
        return {'id': request_id, 'object': 'chat.completion', 'created': 0,
            'model': 'local', 'choices': [{'index': 0, 'finish_reason': 'tool_calls',
            'message': {'role': 'assistant', 'content': None,
            'reasoning_content': 'CANARY-REASONING',
            'tool_calls': [{'id': f'call-{request_id}',
            'type': 'function', 'function': {'name': 'bash', 'arguments': json.dumps({
            'command': command})}}]}}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
    responses = [response('local-1', 'ls -la'), response('local-2', 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT')]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            assert released.wait(10), 'stdout did not flush while model was pending'
            if disconnect:
                self.close_connection = True
                return
            body = json.dumps(responses.pop(0)).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = HTTPServer(('127.0.0.1',0), Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    src = tmp_path/'requirements'
    src.mkdir()
    (src/'requirements.yaml').write_text('id: ROOT\nchildren: []\n')
    (src/'reference').mkdir()
    (src/'reference'/'home.png').write_bytes(base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=='))
    env = dict(os.environ, OPENAI_API_KEY='CANARY_SECRET_123',
               OPENAI_BASE_URL=f'http://127.0.0.1:{server.server_port}/v1',
               MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT='2', ARC_REQUEST_TIMEOUT='3',
               MSWEA_GLOBAL_CONFIG_DIR=str(tmp_path/'config'))
    process = subprocess.Popen([sys.executable,str(ROOT/'main.py'),str(src),
        '--output-dir',str(tmp_path/'output'),'--model','local'],env=env,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    timer = threading.Timer(45, process.kill)
    timer.start()
    lines = []
    events = []
    events_data = []
    try:
        for line in process.stdout:
            lines.append(line)
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = item.get('event')
            if event:
                events.append(event)
                events_data.append(item)
            if event == 'model_request_start':
                released.set()
        process.wait(timeout=5)
        assert released.is_set()
        text = ''.join(lines)
        assert 'CANARY_SECRET_123' not in text
        assert 'engine_import_end' in events
        assert 'environment_check' in events
        arc = tmp_path / 'output' / '.arc_adapter'
        calls = [json.loads(line) for line in (arc / 'calls.jsonl').read_text().splitlines()]
        if disconnect:
            assert process.returncode == 1
            assert events.count('model_request_start') == 2
            assert events.count('model_request_error') == 2
            assert events.index('model_request_error') < len(events)-1-events[::-1].index('model_request_start')
            assert 'RemoteProtocolError' in text
            assert 'Server disconnected' in text
            # Failed attempts: records carry the redacted cause chain, delta log exists.
            assert len(calls) == 2 and all(c['error'] for c in calls)
            deltas = [json.loads(line) for line in (arc / 'payloads.jsonl').read_text().splitlines()]
            assert len(deltas) == 2 and deltas[1]['append'] == []
        else:
            assert process.returncode == 0, text
            assert 'model_request_end' in events
            assert 'bash_start' in events
            assert 'bash_submitted' in events
            # Successful attempts: token usage on stdout and in the per-attempt log.
            end = next(item for item in events_data if item['event'] == 'model_request_end')
            assert end['usage']['prompt_tokens'] == 1 and end['usage']['completion_tokens'] == 1
            assert calls[0]['usage'] == end['usage'] and len(calls) == 2
            # Append-only delta log: fold of appends reconstructs each exact payload.
            deltas = [json.loads(line) for line in (arc / 'payloads.jsonl').read_text().splitlines()]
            assert len(deltas) == 2
            assert deltas[0]['prefix_len'] == 0 and [m['role'] for m in deltas[0]['append']] == ['system', 'user']
            assert deltas[1]['prefix_len'] == 2 and len(deltas[1]['append']) == 2
            # Image data URLs are elided from audit artifacts by default (verifiable placeholders).
            first_line = (arc / 'payloads.jsonl').read_text().splitlines()[0]
            assert 'base64,[elided sha256=' in first_line and 'iVBORw0KGgo' not in first_line
            tr_text = (arc / 'trajectory.json').read_text()
            assert 'iVBORw0KGgo' not in tr_text and 'elided sha256=' in tr_text
            # Default transport policy replays reasoning byte-exact (passthrough).
            assert 'CANARY-REASONING' in (arc.parent / '.arc_adapter' / 'trajectory.json').read_text()
            assert any('CANARY-REASONING' in json.dumps(req) for req in received[1:])
            summary = json.loads((arc / 'run_summary.json').read_text())
            assert summary['input_tokens'] == 2 and summary['output_tokens'] == 2
            assert summary['model_http_attempts'] == 2
        assert received
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait()
        server.shutdown()
        server.server_close()


def test_exception_redacts_secret(monkeypatch, capsys):
    import time
    from progress import failure
    monkeypatch.setenv('OPENAI_API_KEY','CANARY_SECRET_456')
    try:
        try:
            raise OSError('transport CANARY_SECRET_456')
        except OSError as exc:
            raise RuntimeError('outer') from exc
    except RuntimeError as exc:
        failure('model_request_error',exc,time.monotonic())
    text = capsys.readouterr().out
    assert 'CANARY_SECRET_456' not in text
    assert '[REDACTED]' in text and 'OSError' in text and 'RuntimeError' in text


def test_reasoning_transport_policy(monkeypatch):
    from progress import ModelProgress
    history = [
        {'role': 'system', 'content': 's'},
        {'role': 'assistant', 'content': 'x', 'function_call': None,
         'reasoning_content': 'CANARY-REASONING',
         'provider_specific_fields': {'reasoning_content': 'CANARY-REASONING'}},
        {'role': 'tool', 'content': 'out'},
    ]
    model = ModelProgress()
    # Default passthrough: upstream replay behavior, input list untouched.
    assert model._sanitize_for_transport(history) is history
    # Explicit opt-in strip: copy-on-write, in-memory history keeps reasoning for audit.
    monkeypatch.setenv('ARC_TRANSPORT_REASONING', 'strip')
    cleaned = model._sanitize_for_transport(history)
    assert 'CANARY-REASONING' not in json.dumps(cleaned)
    assert history[1]['reasoning_content'] == 'CANARY-REASONING'
    assert cleaned[1]['function_call'] is None
