from __future__ import annotations
import asyncio
import time
from typing import Any, Callable, Awaitable
import httpx
from datetime import datetime, timezone

OSU_BASE = "https://osu.ppy.sh/api/v2"

class OsuApi:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._client = httpx.AsyncClient(timeout=20)
        # Queue kann schon existieren, aber der Worker darf erst starten,
        # wenn ein Event-Loop läuft.
        self._queue: asyncio.Queue[Callable[[], Awaitable[Any]]] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    async def _ensure_worker(self):
        # Wird aus async-Kontexten aufgerufen (da gibt es einen laufenden Loop).
        if self._worker_task is None or self._worker_task.done():
            loop = asyncio.get_running_loop()
            self._worker_task = loop.create_task(self._queue_worker())

    async def aclose(self):
        # Sauber schließen (optional, aber gut für Tests/Shutdown)
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
                "https://osu.ppy.sh/oauth/token",
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

    async def _get(self, path: str, params: dict | None = None) -> Any:
        await self._ensure_worker()  # <<<<< WICHTIG: Worker hier lazy starten
        await self._ensure_token()
        headers = {"Authorization": f"Bearer {self._token}"}

        result_holder = {}

        async def do_request():
            try:
                r = await self._client.get(f"{OSU_BASE}{path}", params=params, headers=headers)
                r.raise_for_status()
                result_holder["data"] = r.json()
            except httpx.HTTPError:
                # Backoff und Skip
                for t in (0.5, 1.0, 2.0):
                    await asyncio.sleep(t)
                    try:
                        r2 = await self._client.get(f"{OSU_BASE}{path}", params=params, headers=headers)
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

    # Rest wie gehabt ...
    async def get_user(self, identifier: str) -> dict | None:
        return await self._get(f"/users/{identifier}/osu")

    async def get_user_best(self, user_id: int | str, limit: int = 100) -> list[dict]:
        scores: list[dict] = []
        cursor = None
        remaining = limit
        while remaining > 0:
            per_page = min(50, remaining)
            params = {"limit": per_page}
            if cursor:
                params["cursor[page]"] = cursor
            data = await self._get(f"/users/{user_id}/scores/best", params=params)
            if not data:
                break
            scores.extend(data)
            if len(data) < per_page:
                break
            remaining -= per_page
            if len(data) == per_page:
                cursor = (cursor or 1) + 1
            else:
                break
        return scores

    async def get_user_recent(self, user_id: int | str, include_fails: bool = True, limit: int = 50) -> list[dict]:
        params = {"include_fails": int(include_fails), "limit": min(50, limit)}
        data = await self._get(f"/users/{user_id}/scores/recent", params=params)
        return data or []

    async def get_beatmap(self, beatmap_id: int | str) -> dict | None:
        return await self._get(f"/beatmaps/{beatmap_id}")
