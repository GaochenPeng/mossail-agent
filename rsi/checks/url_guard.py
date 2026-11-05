"""冻结验收：不向候选生成模型提供本文件，只提供业务需求。"""
import asyncio
import json
import os
from pathlib import Path
import sys
from agent_runtime.research import ResearchSession, SearchHit, public_url


async def check():
    checks, errors = {}, {}
    malformed = ['https://[', 'http://[bad]/x', 'https://example.com:wrong/path', 'https://example.com:99999/']
    for index,url in enumerate(malformed):
        name='malformed-'+str(index)
        try: checks[name]=public_url(url) is False
        except Exception as exc:
            checks[name]=False;errors[name]=type(exc).__name__
    checks['valid_public']=public_url('https://peps.python.org/pep-0654/')
    checks['private_denied']=not public_url('http://127.0.0.1/private')
    checks['credentials_denied']=not public_url('https://user:password@example.com/')

    class Search:
        async def search(self, query):
            return [SearchHit('bad',url,'bad') for url in malformed[:2]]+[
                SearchHit('PEP','https://peps.python.org/pep-0654/','valid')]
    class Reader:
        async def read(self,url): return 'PEP content'
    try:
        session = ResearchSession(Search(),Reader())
        results = (await session.search('PEP'))['results']
        assert len(results)==1 and results[0]['citation_id']=='cit-1'
        await session.fetch(['cit-1'])
        assert session.finalize('结果 [cit-1]').sources[0]['url']=='https://peps.python.org/pep-0654/'
        checks['mixed_search_flow']=True
    except Exception as exc:
        checks['mixed_search_flow']=False;errors['mixed_search_flow']=type(exc).__name__
    result={'checks':checks,'errors':errors,'passed':all(checks.values())}
    Path(os.environ['RSI_TARGET_REPORT']).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result['passed']


if __name__=='__main__':
    sys.exit(0 if asyncio.run(check()) else 1)
