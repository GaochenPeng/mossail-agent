import json

import httpx
import pytest

from agent_runtime.research import ResearchError, ResearchSession, SearchHit, public_url
from agent_runtime.research_providers import SerperReader, SerperSearch


class Search:
    async def search(self, query):
        return [SearchHit('PEP 654', 'https://peps.python.org/pep-0654/', 'Exception Groups'),
                SearchHit('duplicate', 'https://peps.python.org/pep-0654/', 'duplicate'),
                SearchHit('private', 'http://127.0.0.1/config', 'must be ignored')]


class Reader:
    def __init__(self):
        self.calls = []

    async def read(self, url):
        self.calls.append(url)
        return 'Python-Version: 3.11. ExceptionGroup and except*.'


@pytest.mark.asyncio
async def test_sources_are_task_local_deduplicated_and_read_before_citing():
    reader = Reader()
    session = ResearchSession(Search(), reader)
    results = await session.search('PEP 654')
    assert {v['citation_id'] for v in results['results']} == {'cit-1'}
    assert len(session.sources) == 1
    with pytest.raises(ResearchError, match='未读取'):
        session.finalize('3.11 [cit-1]')
    await session.fetch(['cit-1'])
    await session.fetch(['cit-1'])
    assert len(reader.calls) == 1
    final = session.finalize('3.11 引入异常组与 except* [cit-1]')
    assert '[cit-1](https://peps.python.org/pep-0654/)' in final.content
    assert final.sources[0]['retrieved_at'] and '3.11' in final.sources[0]['content']
    assert not ResearchSession(Search(), reader).sources


@pytest.mark.asyncio
async def test_unknown_or_missing_citations_and_model_urls_are_rejected():
    session = ResearchSession(Search(), Reader())
    with pytest.raises(ResearchError, match='没有执行检索'):
        session.finalize('an unsupported answer')
    await session.search('PEP')
    await session.fetch(['cit-1'])
    for answer in ['no citation', '[cit-99]', '〔cit-99〕', '[cit-1](https://invented.example/)']:
        with pytest.raises(ResearchError):
            session.finalize(answer)
    with pytest.raises(ResearchError, match='不存在'):
        await session.fetch(['cit-99'])


@pytest.mark.asyncio
@pytest.mark.parametrize('marker', ['[cit-1]', '〔cit-1〕', '【cit-1】'])
async def test_registered_read_sources_support_chinese_citation_brackets(marker):
    session = ResearchSession(Search(), Reader())
    await session.search('PEP 654')
    with pytest.raises(ResearchError, match='未读取'):
        session.finalize('Python 3.11 ' + marker)
    await session.fetch(['cit-1'])
    final = session.finalize('Python 3.11 ' + marker)
    assert final.content == 'Python 3.11 [cit-1](https://peps.python.org/pep-0654/)'
    assert [source['id'] for source in final.sources] == ['cit-1']


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://127.0.0.1/a', 'http://localhost/a',
    'http://10.0.0.1/a', 'https://user:password@example.com/a', 'javascript:alert(1)'])
def test_non_public_or_credential_bearing_urls_are_not_registered(url):
    assert not public_url(url)


@pytest.mark.parametrize('url', ['https://[', 'http://[bad]/x',
    'https://example.com:wrong/path', 'https://example.com:99999/'])
def test_malformed_provider_urls_are_rejected_without_raising(url):
    assert not public_url(url)


def test_valid_explicit_ports_remain_supported():
    assert public_url('https://example.com:8443/path')
    assert public_url('http://example.com:80/')


@pytest.mark.asyncio
async def test_bad_provider_entries_do_not_abort_remaining_research():
    class MixedSearch:
        async def search(self, query):
            return [SearchHit('bad',url,'bad') for url in ['https://[', 'http://[bad]/x',
                'https://example.com:wrong/path', 'https://example.com:99999/']]+[
                SearchHit('PEP 654','https://peps.python.org/pep-0654/','valid')]
    session = ResearchSession(MixedSearch(),Reader())
    result = await session.search('PEP 654')
    assert len(result['results'])==1
    assert result['results'][0]['citation_id']=='cit-1'
    await session.fetch(['cit-1'])
    assert session.finalize('结果 [cit-1]').sources[0]['url']=='https://peps.python.org/pep-0654/'


@pytest.mark.asyncio
async def test_provider_request_and_return_shape_without_real_credentials():
    def handle(request):
        if request.url.host == 'google.serper.dev':
            assert request.headers['X-API-KEY'] == 'test-only'
            assert json.loads(request.content) == {'q': 'PEP 654', 'num': 5}
            return httpx.Response(200, json={'organic': [{'title': 'PEP', 'link': 'https://peps.python.org/pep-0654/', 'snippet': 'ExceptionGroup'}]})
        assert request.url.host == 'scrape.serper.dev'
        assert request.headers['X-API-KEY'] == 'test-only'
        assert json.loads(request.content)['includeMarkdown'] is True
        return httpx.Response(200, json={'markdown': 'Python-Version: 3.11'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        hits = await SerperSearch(http, 'test-only').search('PEP 654')
        assert hits[0].title == 'PEP'
        assert '3.11' in await SerperReader(http, 'test-only').read(hits[0].url)


@pytest.mark.asyncio
async def test_provider_failure_is_not_a_successful_empty_search():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as http:
        with pytest.raises(ResearchError, match='HTTP 403'):
            await SerperSearch(http, 'invalid-test-key').search('PEP')


@pytest.mark.asyncio
async def test_provider_timeout_exposes_safe_reason_without_supplier_details():
    def unavailable(request):
        raise httpx.ConnectTimeout('private supplier detail', request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as http:
        with pytest.raises(ResearchError, match='超时') as error:
            await SerperSearch(http, 'test-secret').search('PEP')
        assert 'private' not in str(error.value) and 'test-secret' not in str(error.value)


@pytest.mark.asyncio
async def test_empty_search_can_report_no_evidence_and_budget_is_enforced():
    class Empty:
        async def search(self, query): return []
    session = ResearchSession(Empty(), Reader())
    for _ in range(4):
        assert not (await session.search('no evidence'))['results']
    assert not session.finalize('没有检索到依据。').sources
    with pytest.raises(ResearchError, match='上限'):
        await session.search('one too many')
