"""公开检索和正文服务适配；只在 Agent 端装配并持有密钥。"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from .research import ResearchError, SearchHit


async def post_json(http, url, *, api_key, body):
    try:
        response = await http.post(url, headers={'X-API-KEY': api_key}, json=body)
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException:
        raise ResearchError('网页资料服务连接或响应超时，请稍后重新提交') from None
    except httpx.HTTPStatusError as exc:
        raise ResearchError(f'网页资料服务请求失败（HTTP {exc.response.status_code}）') from None
    except httpx.RequestError:
        raise ResearchError('网页资料服务连接失败，请稍后重新提交') from None
    except ValueError:
        raise ResearchError('网页资料服务返回非 JSON 响应') from None
    if not isinstance(data, dict):
        raise ResearchError('网页资料服务响应不是对象')
    return data


@dataclass
class SerperSearch:
    http: httpx.AsyncClient
    api_key: str = field(repr=False)
    base_url: str = 'https://google.serper.dev'

    async def search(self, query: str) -> list[SearchHit]:
        data = await post_json(self.http, self.base_url.rstrip('/')+'/search',
                               api_key=self.api_key, body={'q': query, 'num': 5})
        if not isinstance(data.get('organic'), list):
            raise ResearchError('检索返回缺少 organic 结果列表')
        return [SearchHit(str(item.get('title', '')), item['link'], str(item.get('snippet', '')))
                for item in data['organic'] if isinstance(item, dict) and isinstance(item.get('link'), str)]


@dataclass
class SerperReader:
    http: httpx.AsyncClient
    api_key: str = field(repr=False)
    endpoint: str = 'https://scrape.serper.dev'

    async def read(self, url: str) -> str:
        data = await post_json(self.http, self.endpoint, api_key=self.api_key,
                               body={'url': url, 'includeMarkdown': True})
        content = data.get('markdown') or data.get('text')
        if not isinstance(content, str) or not content.strip():
            raise ResearchError('正文服务没有返回可读取内容')
        return content[:8000]
