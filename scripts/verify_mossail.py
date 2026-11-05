"""默认只检查 health；--live 明确发起两条真实 assistant 任务并保存 HTTP/WS 证据。"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import jsonpatch
import websockets

TERMINAL = {'complete', 'failed', 'abandon'}
QUESTIONS = ['请调用计算工具，计算 (17 + 25) * 3，并说明结果。',
             '把上一轮结果加4，必须调用计算工具。']


class Evidence:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.frames = directory / 'frames.jsonl'
        self.calls = directory / 'http.jsonl'

    def write(self, filename, value):
        (self.directory / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    def append(self, filename, value):
        with filename.open('a') as stream:
            stream.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(), **value}, ensure_ascii=False) + '\n')


class Acceptance:
    def __init__(self, client, evidence, base):
        self.client, self.evidence = client, evidence
        self.ws_url = base.replace('http://', 'ws://').replace('https://', 'wss://') + '/ws/chat/'
        self.connection = 0
        self.messages, self.heads, self.tasks = {}, {}, {}

    async def request(self, method, path, body=None, expected=200):
        response = await self.client.request(method, path, json=body)
        value = response.json()
        self.evidence.append(self.evidence.calls, {'method': method, 'path': path,
                             'request': body, 'status': response.status_code, 'response': value})
        if response.status_code != expected:
            raise AssertionError(f'{method} {path} 返回 {response.status_code}，预期 {expected}：{value}')
        return value

    def connect(self):
        self.connection += 1
        return websockets.connect(self.ws_url, open_timeout=10, close_timeout=5, max_queue=256)

    async def send(self, socket, frame):
        self.evidence.append(self.evidence.frames, {'connection': self.connection, 'direction': 'send', 'frame': frame})
        await socket.send(json.dumps(frame, ensure_ascii=False))

    async def receive(self, socket):
        raw = await asyncio.wait_for(socket.recv(), timeout=35)
        frame = json.loads(raw)
        self.evidence.append(self.evidence.frames, {'connection': self.connection, 'direction': 'receive',
                             'raw': raw, 'frame': frame})
        return frame

    def apply(self, frame):
        if frame.get('type') == 'sync':
            self.messages = {(item['task_id'], item['msg_id']): copy.deepcopy(item['value']) for item in frame['messages']}
            self.heads = dict(frame['heads'])
            self.tasks = {task['task_id']: task for task in frame['tasks']}
            return True
        if 'event_v2' not in frame:
            return True
        event, waterline = frame['event_v2'], frame['stream']
        task_id, seq = waterline['task_id'], waterline['seq']
        assert event['task_id'] == task_id
        head = self.heads.get(task_id, 0)
        if seq <= head:
            return True
        if seq != head + 1:
            return False
        key = (task_id, event['msg_id'])
        if event['op'] in {'add', 'replace'}:
            self.messages[key] = copy.deepcopy(event['value'])
        elif key not in self.messages:
            return False
        else:
            self.messages[key] = jsonpatch.apply_patch(self.messages[key], event['patches'], in_place=False)
        self.heads[task_id] = seq
        self.tasks[task_id] = {'task_id': task_id, 'seq': seq, 'status': event['task_status']}
        return True

    async def sync(self, socket, thread, kind='reconnect'):
        await self.send(socket, {'type': kind, 'thread_id': thread})
        while True:
            frame = await self.receive(socket)
            if frame.get('type') == 'error':
                raise AssertionError(frame)
            if frame.get('type') == 'sync':
                assert frame['thread_id'] == thread
                self.apply(frame)
                return frame

    async def wait_terminal(self, socket, thread, task_id):
        deadline = time.monotonic() + 300
        while self.tasks.get(task_id, {}).get('status') not in TERMINAL:
            if time.monotonic() >= deadline:
                raise TimeoutError('真实任务超过300秒')
            frame = await self.receive(socket)
            if frame.get('type') in {'error', 'recording_error'}:
                raise AssertionError(frame)
            if not self.apply(frame):
                await self.sync(socket, thread, 'sync_request')
            if frame.get('type') == 'heads' and frame['heads'].get(task_id, 0) > self.heads.get(task_id, 0):
                await self.sync(socket, thread, 'sync_request')
        result = await self.request('GET', f'/tasks/{task_id}')
        assert result['status'] == 'complete', f"真实模型任务失败：{result.get('error')}"
        observed = {mid: value for (tid, mid), value in self.messages.items() if tid == task_id}
        assert result['seq'] == self.heads[task_id]
        assert result['snapshots'] == observed
        return result

    async def submit_twice(self, thread, question, request_id):
        body = {'question': question, 'agent': 'assistant', 'client_request_id': request_id}
        first = await self.request('POST', f'/threads/{thread}/tasks', body, expected=202)
        duplicate = await self.request('POST', f'/threads/{thread}/tasks', body, expected=202)
        assert first['task_id'] == duplicate['task_id']
        return first['task_id']

    def assert_result(self, result, expected):
        tools = [value for value in result['snapshots'].values() if value['type'] == 'tool_call']
        assert tools and any(tool['meta_data'].get('name') == 'calculate' for tool in tools)
        outputs = [str(tool['meta_data'].get('output', '')) for tool in tools]
        assert any(str(expected) in output for output in outputs), outputs
        answers = [value['meta_data'].get('content', '') for value in result['snapshots'].values()
                   if value['type'] == 'markdown' and value['sender'] == 'assistant' and value['status'] == 'success']
        assert any(str(expected) in answer for answer in answers), answers
        return {'task_id': result['id'], 'status': result['status'], 'seq': result['seq'],
                'answers': answers, 'tools': tools, 'expected': expected}


async def main(args):
    base = args.base_url.rstrip('/')
    async with httpx.AsyncClient(base_url=base, timeout=20, trust_env=False) as client:
        health = (await client.get('/health')).json()
        if not args.live:
            print(json.dumps(health, ensure_ascii=False, indent=2))
            return
        directory = args.output / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8])
        evidence = Evidence(directory)
        evidence.write('health-before.json', health)
        summary = {'started_at': datetime.now(timezone.utc).isoformat(), 'base_url': base,
                   'declared_server_pid': args.server_pid, 'health_before': health, 'results': [], 'passed': False}
        try:
            assert health['ready']
            actual_sha = health['agent']['execution']['source_sha256']
            if args.expect_sha256:
                assert actual_sha == args.expect_sha256, 'Server 指纹与冻结版本不一致'
            probe = Acceptance(client, evidence, base)
            thread = (await probe.request('POST', '/threads', expected=201))['thread_id']
            summary['thread_id'] = thread
            before = await probe.request('GET', f'/threads/{thread}/history')
            invalid_results = []
            for body in [
                {'question': '非法场景不应执行', 'agent': '__invalid_scene__', 'client_request_id': uuid4().hex},
                {'question': '错误模型不应执行', 'agent': 'assistant', 'model': {'id': '__invalid_model__'},
                 'client_request_id': uuid4().hex},
            ]:
                invalid_results.append(await probe.request('POST', f'/threads/{thread}/tasks', body, expected=422))
            after = await probe.request('GET', f'/threads/{thread}/history')
            assert before == after and not after['tasks']
            summary['invalid_requests'] = {'responses': invalid_results, 'tasks_before': 0, 'tasks_after': 0}
            async with probe.connect() as socket:
                await probe.sync(socket, thread)
                first = await probe.submit_twice(thread, QUESTIONS[0], 'acceptance-first-' + uuid4().hex)
                summary['first_task_id'] = first
                while True:
                    frame = await probe.receive(socket)
                    probe.apply(frame)
                    if frame.get('event_v2', {}).get('task_id') == first:
                        assert frame['event_v2']['task_status'] not in TERMINAL
                        break
                running = await probe.request('GET', f'/tasks/{first}')
                assert running['status'] == 'running'
                summary['before_ws_disconnect'] = {'task_id': first, 'status': running['status'], 'seq': running['seq']}
            after_disconnect = await probe.request('GET', f'/tasks/{first}')
            assert after_disconnect['status'] != 'abandon'
            summary['after_ws_disconnect'] = {'task_id': first, 'status': after_disconnect['status'], 'seq': after_disconnect['seq']}
            async with probe.connect() as socket:
                reconnect = await probe.sync(socket, thread)
                summary['running_reconnect_sync'] = reconnect
                assert probe.tasks[first]['status'] != 'abandon'
                result = await probe.wait_terminal(socket, thread, first)
                summary['results'].append(probe.assert_result(result, 126))
                terminal_sync = await probe.sync(socket, thread, 'sync_request')
                assert terminal_sync['heads'][first] == result['seq']
                first_sync_values = {item['msg_id']: item['value'] for item in terminal_sync['messages'] if item['task_id'] == first}
                assert first_sync_values == result['snapshots']
                second = await probe.submit_twice(thread, QUESTIONS[1], 'acceptance-second-' + uuid4().hex)
                summary['second_task_id'] = second
                result = await probe.wait_terminal(socket, thread, second)
                summary['results'].append(probe.assert_result(result, 130))
            async with probe.connect() as socket:
                final_sync = await probe.sync(socket, thread)
                assert len(final_sync['tasks']) == 2
                for item in summary['results']:
                    final_get = await probe.request('GET', f"/tasks/{item['task_id']}")
                    assert final_get['status'] == 'complete' and final_get['seq'] == final_sync['heads'][item['task_id']]
                    values = {message['msg_id']: message['value'] for message in final_sync['messages'] if message['task_id'] == item['task_id']}
                    assert values == final_get['snapshots']
                evidence.write('final-sync.json', final_sync)
            summary['health_after'] = await probe.request('GET', '/health')
            assert summary['health_after']['agent']['execution']['source_sha256'] == actual_sha
            summary['passed'] = True
        except Exception as exc:
            summary['failure'] = {'type': type(exc).__name__, 'message': str(exc)}
            raise
        finally:
            summary['finished_at'] = datetime.now(timezone.utc).isoformat()
            evidence.write('summary.json', summary)
            print(json.dumps({'artifact_directory': str(directory.resolve()), 'passed': summary['passed'],
                              'thread_id': summary.get('thread_id'), 'results': summary['results'],
                              'failure': summary.get('failure')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8010')
    parser.add_argument('--live', action='store_true', help='明确允许两条真实模型任务；未指定时只请求health')
    parser.add_argument('--expect-sha256')
    parser.add_argument('--server-pid', type=int)
    parser.add_argument('--output', type=Path, default=Path('.data/agent-workspace/implementation/acceptance/http'))
    asyncio.run(main(parser.parse_args()))
