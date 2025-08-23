from __future__ import annotations
from osu_http import OsuHttpClient


class OsuApi:
    def __init__(self, http_client: OsuHttpClient):
        self.http = http_client

    async def get_user(self, identifier: str) -> dict | None:
        # username oder id
        return await self.http.get(f"/users/{identifier}/osu")

    async def get_user_best(
        self, user_id: int | str, limit: int = 100, mode: str = "osu"
    ) -> list[dict]:
        """
        Holt bis zu 100 Best-Scores mit robuster Pagination via offset.
        """
        scores: list[dict] = []
        offset = 0
        while len(scores) < limit:
            per_page = min(50, limit - len(scores))  # v2 ist bei 50/100 limitiert
            params = {"limit": per_page, "offset": offset, "mode": mode}
            data = await self.http.get(f"/users/{user_id}/scores/best", params=params)
            if not data:
                break
            new_data = await OsuApi.apply_actual_sr_to_plays(data, mode="osu")
            scores.extend(new_data)
            got = len(new_data)
            if got < per_page:
                break
            offset += got
        return scores

    async def get_user_recent(
        self, user_id: int | str, limit: int = 50, mode: str = "osu"
    ) -> list[dict]:

        params = {"limit": min(50, limit), "mode": mode}
        data = await self.http.get(f"/users/{user_id}/scores/recent", params=params)
        new_data = await OsuApi.apply_actual_sr_to_plays(self, data, mode="osu")

        return new_data or []

    async def get_beatmap(self, beatmap_id: int | str) -> dict | None:
        return await self.http.get(f"/beatmaps/{beatmap_id}")

    async def apply_actual_sr_to_plays(
        self, plays: list[dict], mode: str = "osu"
    ) -> list[dict]:
        """
        Iterates over plays and updates each play's beatmap with the real star rating
        based on the mods applied.
        """
        for play in plays:
            mods = play.get("mods", [])
            beatmap_id = play["beatmap"]["id"]
            body = {"mods": mods, "ruleset": mode}
            sr_data = await self.http.post(
                f"/beatmaps/{beatmap_id}/attributes", body=body
            )

            if (
                sr_data
                and "attributes" in sr_data
                and "star_rating" in sr_data["attributes"]
            ):
                play["beatmap"]["difficulty_rating"] = sr_data["attributes"][
                    "star_rating"
                ]
            else:
                play["beatmap"]["difficulty_rating"] = None

        return plays
