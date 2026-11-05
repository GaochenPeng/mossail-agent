"""本机 HTTP 转发验证；需要 loopback，不纳入禁网的候选工程回归。"""
import json
import time

import httpx
import pytest

from rsi.model_gateway import ModelGateway, OFFICIAL_RESPONSES

PAYLOAD = {'model': 'test-model', 'input': [{'role': 'user', 'content': 'test'}],
           'store': False, 'stream': True}
STREAM = b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'


def test_forwarding_preserves_stream_and_never_returns_oauth_or_cookies():
    calls = []

    def upstream(request):
        calls.append(request)
        assert str(request.url) == OFFICIAL_RESPONSES
        assert request.headers['Authorization'] == 'Bearer personal-test-token'
        assert json.loads(request.content) == PAYLOAD
        return httpx.Response(200, content=STREAM, headers={
            'content-type': 'text/event-stream', 'x-request-id': 'request-test',
            'set-cookie': 'upstream-private-cookie'})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as upstream_http:
        gate = ModelGateway('test-model', lambda: 'personal-test-token', upstream_http, max_requests=1)
        with gate, httpx.Client(trust_env=False) as local:
            endpoint = gate.environment['RSI_MODEL_GATEWAY_URL'] + '/responses'
            headers = {'Authorization': 'Bearer ' + gate.credential}
            response = local.post(endpoint, headers=headers, json=PAYLOAD)
            assert response.status_code == 200 and response.content == STREAM
            assert response.headers['x-request-id'] == 'request-test'
            assert 'set-cookie' not in response.headers
            assert local.post(endpoint, headers=headers, json=PAYLOAD).status_code == 429
            assert len(calls) == 1
        assert gate.proof['closed']
        assert 'personal-test-token' not in json.dumps(gate.proof)
        assert gate.credential not in json.dumps(gate.proof)
        with httpx.Client(trust_env=False, timeout=1) as local:
            with pytest.raises(httpx.ConnectError):
                local.post(endpoint, headers=headers, json=PAYLOAD)


def test_invalid_credential_and_route_never_reach_upstream():
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('不应请求上游'))) as upstream:
        gate = ModelGateway('test-model', lambda: pytest.fail('不应读取个人令牌'), upstream)
        with gate, httpx.Client(trust_env=False) as local:
            base = gate.environment['RSI_MODEL_GATEWAY_URL']
            assert local.post(base + '/responses', json=PAYLOAD).status_code == 401
            assert local.post(base + '/responses?redirect=other', json=PAYLOAD).status_code == 404
            assert local.get(base + '/models').status_code == 405
        assert not gate.proof['requests']


def test_expired_temporary_credential_cannot_start_inference():
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('不应请求上游'))) as upstream:
        gate = ModelGateway('test-model', lambda: pytest.fail('不应读取个人令牌'), upstream, ttl_seconds=.01)
        with gate, httpx.Client(trust_env=False) as local:
            time.sleep(.02)
            result = local.post(gate.environment['RSI_MODEL_GATEWAY_URL'] + '/responses',
                headers={'Authorization': 'Bearer ' + gate.credential}, json=PAYLOAD)
            assert result.status_code == 403
        assert not gate.proof['requests']


def test_upstream_failure_is_forwarded_without_switching_model_or_billing():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429,
            json={'error': {'code': 'subscription_sharing_usage_limit_exceeded'}},
            headers={'x-request-id': 'limited-request'}))) as upstream:
        gate = ModelGateway('test-model', lambda: 'personal-test-token', upstream)
        with gate, httpx.Client(trust_env=False) as local:
            response = local.post(gate.environment['RSI_MODEL_GATEWAY_URL'] + '/responses',
                headers={'Authorization': 'Bearer ' + gate.credential}, json=PAYLOAD)
            assert response.status_code == 429
            assert response.json()['error']['code'] == 'subscription_sharing_usage_limit_exceeded'
        assert len(gate.proof['requests']) == 1
