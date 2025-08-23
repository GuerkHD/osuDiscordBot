from __future__ import annotations
import asyncio
from osu_http import OsuHttpClient


class OsuApi:
    def __init__(self, http_client: OsuHttpClient):
        self.http = http_client

    async def get_user(self, identifier: str) -> dict | None:
        # username oder id
        return await self.http.get(f"/users/{identifier}/osu")

    async def get_user_best(
        self, user_id: int | str, limit: int = 50, mode: str = "osu"
    ) -> dict:
        """
        Fetches up to 50 best scores. Only calculates star ratings for top 10.
        Returns a dict with top10 plays (with SR), and pp threshold for play 50.
        """
        print(f"[DEBUG] get_user_best: user_id={user_id}, limit={limit}, mode={mode}")
        params = {"limit": min(50, limit), "mode": mode}
        print(f"[DEBUG] get_user_best: Requesting best scores: params={params}")
        data = await self.http.get(f"/users/{user_id}/scores/best", params=params)
        print(f"[DEBUG] get_user_best: Received data...")
        print(f"[DEBUG] get_user_best: Calculating SR...")

        if not data:
            print("[DEBUG] get_user_best: No data received.")
            return {"top10": [], "pp_threshold": 0.0}

        # Only calculate SR for top 10
        top10 = data[:10]
        top10_with_sr = await self.apply_actual_sr_to_plays(top10, mode=mode)
        print(f"[DEBUG] get_user_best: Star ratings applied!")

        # Get pp threshold for play 50 (if available)
        pp_threshold = float(data[49]["pp"]) if len(data) >= 50 else 0.0

        # Return both top10 (with SR) and pp threshold
        return {
            "top10": top10_with_sr,
            "pp_threshold": pp_threshold,
            "all": data,  # Optional: all plays, with top10 having SR
        }

    async def get_user_recent(
        self, user_id: int | str, limit: int = 50, mode: str = "osu"
    ) -> list[dict]:
        print(f"[DEBUG] user_recent: user_id={user_id}, limit={limit}, mode={mode}")
        params = {"limit": min(50, limit), "mode": mode}
        print(f"[DEBUG] user_recent: Requesting recent scores: params={params}")
        data = await self.http.get(f"/users/{user_id}/scores/recent", params=params)
        print(f"[DEBUG] user_recent: Received data...")
        print(f"[DEBUG] user_recent: Calculating SR...")
        new_data = await OsuApi.apply_actual_sr_to_plays(self, data, mode="osu")
        print(f"[DEBUG] user_recent: Star ratings applied!")
        return new_data or []

    async def get_beatmap(self, beatmap_id: int | str) -> dict | None:
        return await self.http.get(f"/beatmaps/{beatmap_id}")

    async def apply_actual_sr_to_plays(
        self, plays: list[dict], mode: str = "osu"
    ) -> list[dict]:
        """
        Iterates over plays and updates each play's beatmap with the real star rating
        based on the mods applied, using asyncio.gather for concurrency.
        """

        async def fetch_sr(play):
            mods = []
            mod_settings = {}

            # Parse mods and their settings
            for mod in play.get("mods", []):
                acronym = mod.get("acronym")
                if acronym:
                    mods.append(acronym)
                settings = mod.get("settings")
                if settings:
                    mod_settings[acronym] = settings

            # TODO: if we have any settings associated with a mod, we need to do the sr calc here instead of sending away

            if len(mods) == 0 or (len(mods) == 1 and mods[0] == "HD"):
                return play

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

            return play

        # Run all fetch_sr coroutines concurrently
        return await asyncio.gather(*(fetch_sr(play) for play in plays))
