import json

import httpx
import pytest

from rsi.model_gateway import GatewayRejected, MAX_BODY_BYTES, ModelGateway


def body(**updates):
    return json.dumps({'model': 'test-model', 'input': [{'role': 'user', 'content': 'test'}],
                       'stream': True, 'store': False, **updates}).encode()


@pytest.mark.parametrize('changes', [
    {'model': 'different-model'}, {'stream': False}, {'store': True}, {'input': 'not-an-array'},
    {'metadata': {}}, {'tools': [{'type': 'web_search'}]},
    {'tools': [{'type': 'function', 'name': 'flat-tool'}]},
    {'tools': [{'type': 'namespace', 'tools': [{'type': 'tool_search'}]}]},
])
def test_unapproved_request_is_rejected_before_upstream_token_is_read(changes):
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('不应访问上游'))) as http:
        gate = ModelGateway('test-model', lambda: pytest.fail('不应读取 OAuth 凭证'), http)
        with pytest.raises(GatewayRejected):
            gate.admit('/v1/responses', 'Bearer ' + gate.credential, body(**changes))
    assert gate.proof['requests'] == []


def test_body_limit_applies_before_json_parsing():
    with httpx.Client() as http:
        gate = ModelGateway('test-model', lambda: 'not-used', http)
        with pytest.raises(GatewayRejected) as rejected:
            gate.admit('/v1/responses', 'Bearer ' + gate.credential, b'x' * (MAX_BODY_BYTES + 1))
    assert rejected.value.status == 413
    assert gate.proof['requests'] == []
