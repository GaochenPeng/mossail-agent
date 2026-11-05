import json

import httpx
import pytest
from agents.exceptions import ModelBehaviorError
from openai import AsyncOpenAI
from open_protocol import AgentInput

from agent_service.agents.model_access import ModelAccess, SubscriptionResponsesModel
from agent_service.agents.model_access import subscription_endpoint
from agent_runtime.runtime import AgentSpec
from agent_runtime.tools import calculate


@pytest.mark.parametrize('url', ['https://127.0.0.1:1234/v1', 'http://localhost:1234/v1',
    'http://example.com:1234/v1', 'http://user@127.0.0.1:1234/v1',
    'http://127.0.0.1:1234/other', 'http://127.0.0.1:bad/v1'])
def test_evaluation_endpoint_cannot_redirect_personal_auth_to_another_host(monkeypatch, url):
    monkeypatch.setenv('RSI_MODEL_GATEWAY_URL', url)
    monkeypatch.setenv('RSI_MODEL_GATEWAY_TOKEN', 'temporary-test-token')
    with httpx.Client() as http:
        with pytest.raises(ValueError, match='127.0.0.1'):
            subscription_endpoint(http)


def test_evaluation_endpoint_does_not_open_personal_credentials(monkeypatch):
    monkeypatch.setenv('RSI_MODEL_GATEWAY_URL', 'http://127.0.0.1:1234/v1')
    monkeypatch.setenv('RSI_MODEL_GATEWAY_TOKEN', 'temporary-test-token')
    monkeypatch.setattr('agent_service.agents.model_access.CredentialStore',
                        lambda *_: pytest.fail('实验进程不应访问个人凭证目录'))
    with httpx.Client() as http:
        endpoint = subscription_endpoint(http)
    assert endpoint.key == 'temporary-test-token'
    assert endpoint.transport == 'evaluation_gateway'
    assert 'temporary-test-token' not in repr(endpoint)


def response_event(output):
    return {'type': 'response.completed', 'sequence_number': 1, 'response': {
        'id': 'response-test', 'object': 'response', 'created_at': 0, 'status': 'completed',
        'model': 'test', 'output': output, 'parallel_tool_calls': False, 'tools': [], 'tool_choice': 'auto'}}


def stream_response(event):
    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                          content=('data: ' + json.dumps(event) + '\n\n').encode())


@pytest.mark.asyncio
async def test_subscription_sdk_roundtrip_executes_namespaced_function_and_sends_full_history():
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers['Authorization'] == 'Bearer test-only'
        assert body['store'] is False and body['stream'] is True
        assert body['tools'][0]['type'] == 'namespace'
        assert body['tools'][0]['name'] == 'open_repo'
        forbidden = {'previous_response_id', 'conversation', 'temperature', 'metadata',
                     'max_output_tokens', 'truncation', 'top_p'}
        assert not forbidden.intersection(body)
        if len(requests) == 1:
            return stream_response(response_event([{'type': 'function_call', 'id': 'fc-test',
                'call_id': 'call-test', 'namespace': 'open_repo', 'name': 'calculate',
                'arguments': '{"expression":"(17+25)*3"}', 'status': 'completed'}]))
        assert any(item.get('type') == 'function_call_output' and item['output'] == '126.0'
                   for item in body['input'])
        return stream_response(response_event([{'id': 'msg-test', 'type': 'message',
            'role': 'assistant', 'status': 'completed', 'content': [
                {'type': 'output_text', 'text': '工具结果是126。', 'annotations': [], 'logprobs': []}]}]))

    async with AsyncOpenAI(api_key='test-only', base_url='https://api.openai.com/v1',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
        runtime = ModelAccess(SubscriptionResponsesModel('test', client), 'chatgpt').runtime(
            AgentSpec('test', 'Use the calculator.', [calculate]), {'auth_mode': 'chatgpt'})
        updates = [item async for item in runtime.stream(AgentInput(
            thread_id='t-test', task_id='r-test', user_prompt='calculate'))]
    assert len(requests) == 2
    assert requests[0]['include'] == ['reasoning.encrypted_content']
    tool = next(item.event_v2.value for item in updates if item.event_v2.msg_id == 'call-test')
    assert tool.meta_data['name'] == 'calculate'
    assert updates[-1].event_v2.task_status == 'complete'
    assert any(patch.value == '126.0' for item in updates for patch in item.event_v2.patches or [])


@pytest.mark.parametrize('termination', ['interrupted', 'failed', 'incomplete'])
@pytest.mark.asyncio
async def test_subscription_partial_or_failed_stream_never_becomes_success(termination):
    if termination == 'interrupted':
        event = {'type': 'response.output_text.delta', 'delta': 'partial',
                 'item_id': 'msg-test', 'content_index': 0, 'output_index': 0, 'sequence_number': 1}
    else:
        event = response_event([])
        event['type'] = 'response.' + termination
        event['response']['status'] = termination
        event['response']['error'] = {'code': 'subscription_sharing_usage_limit_exceeded', 'message': 'usage limit'}

    async with AsyncOpenAI(api_key='test-only', base_url='https://api.openai.com/v1',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: stream_response(event)))) as client:
        runtime = ModelAccess(SubscriptionResponsesModel('test', client), 'chatgpt').runtime(
            AgentSpec('test', 'Reply.', []), {})
        updates = []
        with pytest.raises(ModelBehaviorError):
            async for item in runtime.stream(AgentInput(thread_id='t', task_id='r', user_prompt='reply')):
                updates.append(item)
    assert not any(item.event_v2.task_status == 'complete' for item in updates)
