from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import time
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest

from agent_service.agents.chatgpt_auth import (AUTHORITY, PLAN_SCOPE, RESOURCE, AuthError,
    ChatGPTAuth, CredentialStore, Credentials, Registration)


@pytest.fixture
def signer():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public['kid'] = 'test-key'
    return private, public


def token_response(**overrides):
    return {'access_token': 'access-test', 'refresh_token': 'refresh-test',
            'token_type': 'Bearer', 'scope': 'openid resource.invoke ' + PLAN_SCOPE,
            'expires_in': 3600, **overrides}


def save_session(store, **overrides):
    credentials = Credentials(registration=Registration('issued-test', 'subject-test'),
        access_token='access-old', refresh_token='refresh-old',
        scopes=['resource.invoke', PLAN_SCOPE], expires_at=time.time() - 1)
    store.save(replace(credentials, **overrides))


def test_registration_pkce_identity_and_private_storage(tmp_path, signer):
    store = CredentialStore(tmp_path / 'auth')
    issued = {}

    def handle(request):
        if request.url.path.endswith('jwks.json'):
            return httpx.Response(200, json={'keys': [signer[1]]})
        issued.update(parse_qs(request.content.decode()))
        identity = jwt.encode({'iss': AUTHORITY, 'aud': 'issued-test', 'sub': 'subject-test',
            'iat': int(time.time()), 'exp': int(time.time()) + 300, 'nonce': attempt.nonce},
            signer[0], algorithm='RS256', headers={'kid': 'test-key'})
        return httpx.Response(200, json=token_response(id_token=identity))

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        auth = ChatGPTAuth(store, http)
        attempt = auth.begin('http://127.0.0.1:1234/auth/callback')
        params = parse_qs(urlsplit(attempt.authorize_url()).query)
        assert params['client_id'] == ['dynamic_agent_client']
        assert params['agent_name_hint'] == ['Open Repo']
        assert params['code_challenge_method'] == ['S256']
        assert params['code_challenge'] != [attempt.verifier]
        auth.finish(attempt, {'state': attempt.state, 'code': 'test-code', 'client_id': 'issued-test'})
        assert issued['client_id'] == ['issued-test']
        assert issued['redirect_uri'] == [attempt.redirect_uri]
        assert issued['resource'] == [RESOURCE]
        assert store.load().plan_enabled
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert store.directory.stat().st_mode & 0o777 == 0o700
        renewed = auth.begin(attempt.redirect_uri)
        params = parse_qs(urlsplit(renewed.authorize_url()).query)
        assert params['client_id'] == ['issued-test']
        assert 'agent_name_hint' not in params
        assert renewed.host_id == attempt.host_id
        assert 'access-test' not in repr(store.load())


@pytest.mark.parametrize('failure', ['state', 'nonce', 'signature', 'audience', 'issuer', 'expiry', 'identity'])
def test_invalid_callbacks_and_identity_cannot_replace_session(tmp_path, signer, failure):
    store = CredentialStore(tmp_path)
    save_session(store, expires_at=time.time() + 1000)
    original = store.load()

    def handle(request):
        if request.url.path.endswith('jwks.json'):
            return httpx.Response(200, json={'keys': [signer[1]]})
        identity = {'iss': AUTHORITY, 'aud': 'issued-test', 'sub': 'subject-test',
                    'iat': int(time.time()) - 60, 'exp': int(time.time()) + 300, 'nonce': attempt.nonce}
        identity.update({'nonce': {'nonce': 'wrong'}, 'audience': {'aud': 'other'},
                         'issuer': {'iss': 'https://other.example'}, 'expiry': {'exp': 0},
                         'identity': {'sub': 'other-subject'}}.get(failure, {}))
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048) if failure == 'signature' else signer[0]
        encoded = jwt.encode(identity, private, algorithm='RS256', headers={'kid': 'test-key'})
        return httpx.Response(200, json=token_response(id_token=encoded))

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        auth = ChatGPTAuth(store, http)
        attempt = auth.begin('http://127.0.0.1:1234/auth/callback')
        with pytest.raises(AuthError):
            auth.finish(attempt, {'state': 'wrong' if failure == 'state' else attempt.state,
                                  'code': 'test-code', 'client_id': 'issued-test'})
    assert store.load() == original


def test_failed_code_exchange_retains_registration_for_next_login(tmp_path):
    store = CredentialStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400,
            json={'error': 'invalid_grant'}))) as http:
        auth = ChatGPTAuth(store, http)
        attempt = auth.begin('http://127.0.0.1:1234/auth/callback')
        with pytest.raises(AuthError, match='invalid_grant'):
            auth.finish(attempt, {'state': attempt.state, 'code': 'expired', 'client_id': 'issued-test'})
    assert store.load().registration.client_id == 'issued-test'
    assert not store.load().access_token


def test_refresh_is_serialized_and_replaces_rotating_tokens(tmp_path):
    store = CredentialStore(tmp_path)
    save_session(store)
    requests = []

    def handle(request):
        requests.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json=token_response())

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        with ThreadPoolExecutor(max_workers=4) as executor:
            tokens = list(executor.map(lambda _: ChatGPTAuth(CredentialStore(tmp_path), http).access_token(), range(4)))
    assert tokens == ['access-test'] * 4
    assert len(requests) == 1
    assert requests[0]['refresh_token'] == ['refresh-old']
    assert requests[0]['client_id'] == ['issued-test']
    assert store.load().refresh_token == 'refresh-test'


@pytest.mark.parametrize('code,status,cleared', [('invalid_grant', 400, True),
    ('invalid_client', 400, False), ('temporarily_unavailable', 503, False)])
def test_refresh_error_preserves_registration_and_only_clears_revoked_tokens(tmp_path, code, status, cleared):
    store = CredentialStore(tmp_path)
    save_session(store)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status,
        json={'error': code}, headers={'x-request-id': 'request-test'}))) as http:
        with pytest.raises(AuthError) as failure:
            ChatGPTAuth(store, http).access_token()
    assert failure.value.request_id == 'request-test'
    assert store.load().registration.client_id == 'issued-test'
    assert bool(store.load().access_token) is not cleared


def test_identity_only_permission_cannot_make_model_requests(tmp_path):
    store = CredentialStore(tmp_path)
    save_session(store, scopes=['openid'])
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('不应调用外部服务'))) as http:
        with pytest.raises(AuthError, match='授权'):
            ChatGPTAuth(store, http).models()
