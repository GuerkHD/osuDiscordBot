import asyncio
import time
from typing import Any, Callable, Awaitable
import httpx

OSU_BASE = "https://osu.ppy.sh/api/v2"
OSU_OAUTH_URL = "https://osu.ppy.sh/oauth/token"


class OsuHttpClient:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._client = httpx.AsyncClient(timeout=20)
        self._queue: asyncio.Queue[Callable[[], Awaitable[Any]]] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    async def _ensure_worker(self):
        if self._worker_task is None or self._worker_task.done():
            loop = asyncio.get_running_loop()
            self._worker_task = loop.create_task(self._queue_worker())

    async def aclose(self):
        try:
            if self._worker_task:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass
        finally:
            await self._client.aclose()

    async def _queue_worker(self):
        last_call = 0.0
        while True:
            job = await self._queue.get()
            wait = 1.0 - (time.time() - last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await job()
            finally:
                last_call = time.time()
                self._queue.task_done()

    async def _ensure_token(self):
        if self._token and time.time() < self._token_exp - 30:
            return
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.post(
                OSU_OAUTH_URL,
                json={
                    "grant_type": "client_credentials",
                    "client_id": int(self.client_id),
                    "client_secret": self.client_secret,
                    "scope": "public",
                },
            )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data["expires_in"])

    async def get(self, path: str, params: dict | None = None) -> Any:
        await self._ensure_worker()
        await self._ensure_token()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "x-api-version": "20240705",
        }
        result_holder = {}

        async def do_request():
            try:
                r = await self._client.get(
                    f"{OSU_BASE}{path}", params=params, headers=headers
                )
                r.raise_for_status()
                result_holder["data"] = r.json()
            except httpx.HTTPError:
                for t in (0.5, 1.0, 2.0):
                    await asyncio.sleep(t)
                    try:
                        r2 = await self._client.get(
                            f"{OSU_BASE}{path}", params=params, headers=headers
                        )
                        r2.raise_for_status()
                        result_holder["data"] = r2.json()
                        return
                    except httpx.HTTPError:
                        continue
                result_holder["data"] = None

        fut = asyncio.get_running_loop().create_future()

        async def jobwrap():
            await do_request()
            fut.set_result(True)

        await self._queue.put(jobwrap)
        await fut
        return result_holder.get("data")

    async def post(
        self, path: str, params: dict | None = None, body: dict | None = None
    ) -> Any:
        await self._ensure_worker()
        await self._ensure_token()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "x-api-version": "20240705",
        }
        result_holder = {}

        async def do_request():
            try:
                r = await self._client.post(
                    f"{OSU_BASE}{path}", params=params, json=body, headers=headers
                )
                r.raise_for_status()
                result_holder["data"] = r.json()
            except httpx.HTTPError:
                for t in (0.5, 1.0, 2.0):
                    await asyncio.sleep(t)
                    try:
                        r2 = await self._client.post(
                            f"{OSU_BASE}{path}",
                            params=params,
                            json=body,
                            headers=headers,
                        )
                        r2.raise_for_status()
                        result_holder["data"] = r2.json()
                        return
                    except httpx.HTTPError:
                        continue
                result_holder["data"] = None

        fut = asyncio.get_running_loop().create_future()

        async def jobwrap():
            await do_request()
            fut.set_result(True)

        await self._queue.put(jobwrap)
        await fut
        return result_holder.get("data")
