import asyncio
import httpx
from .errors import ReviewError


async def bounded_json(client, method, url, *, limit=2_000_000, **kwargs):
    # Only callers with fixed trusted endpoints use this primitive. Never follows redirects.
    async with client.stream(method, url, follow_redirects=False, **kwargs) as response:
        if response.status_code != 200:
            raise ReviewError(f'http_{response.status_code}', f'外部服务返回 HTTP {response.status_code}。')
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > limit:
                raise ReviewError('too_large', '外部响应超过大小限制。')
        try:
            import json
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise ReviewError('invalid_json', '外部服务返回格式不正确。') from None


async def fetch_public_json(client, url):
    # GET retries only; never automatically repeats paid analysis POSTs.
    for attempt in range(2):
        try:
            async with asyncio.timeout(20):
                return await bounded_json(client, 'GET', url)
        except (httpx.TransportError, TimeoutError):
            if attempt == 1:
                raise ReviewError('fetch_timeout', '棋谱接口连接失败或超时，请稍后再试。') from None
            await asyncio.sleep(.3)
