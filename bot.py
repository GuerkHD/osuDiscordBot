import os
import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
load_dotenv()

import aiohttp
import discord
from discord.ext import tasks

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

# ----------------------------
# Configuration
# ----------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "PUT-YOUR-DISCORD-BOT-TOKEN-HERE")
OSU_CLIENT_ID = os.getenv("OSU_CLIENT_ID", "1234")
OSU_CLIENT_SECRET = os.getenv("OSU_CLIENT_SECRET", "SECRET")
OSU_API_BASE = "https://osu.ppy.sh/api/v2"
TZ = ZoneInfo("Europe/Berlin")
DB_PATH = os.getenv("OSU_BOT_DB", "osu_bot.sqlite3")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # optional: restrict to one guild for faster command sync

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("osu-bot")

# ----------------------------
# Database layer
# ----------------------------
class Database:
    def __init__(self, path: str):
        self.path = path
        self._ensure_schema()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self):
        with self._connect() as con:
            cur = con.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    osu_user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    added_by INTEGER,
                    added_at TEXT
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    osu_user_id INTEGER,
                    score_id INTEGER,
                    beatmap_id INTEGER,
                    mode TEXT,
                    rank TEXT,
                    mods TEXT,
                    score INTEGER,
                    pp REAL,
                    accuracy REAL,
                    max_combo INTEGER,
                    misses INTEGER,
                    ar REAL,
                    cs REAL,
                    od REAL,
                    hp REAL,
                    timestamp TEXT,
                    month_key TEXT,
                    raw_json TEXT,
                    UNIQUE(osu_user_id, score_id)
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_scores_month ON scores(month_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_scores_user_month ON scores(osu_user_id, month_key)")
            con.commit()

    # Players
    def add_player(self, osu_user_id: int, username: str, added_by: int):
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO players(osu_user_id, username, added_by, added_at) VALUES (?,?,?,?)",
                (osu_user_id, username, added_by, datetime.now(timezone.utc).isoformat()),
            )
            con.commit()

    def remove_player(self, osu_user_id: int) -> bool:
        with self._connect() as con:
            cur = con.execute("DELETE FROM players WHERE osu_user_id=?", (osu_user_id,))
            con.commit()
            return cur.rowcount > 0

    def list_players(self) -> List[sqlite3.Row]:
        with self._connect() as con:
            cur = con.execute("SELECT * FROM players ORDER BY username COLLATE NOCASE")
            return cur.fetchall()

    # Scores
    def insert_score(self, row: Dict[str, Any]):
        with self._connect() as con:
            con.execute(
                """
                INSERT OR IGNORE INTO scores (
                    osu_user_id, score_id, beatmap_id, mode, rank, mods, score, pp, accuracy, max_combo, misses,
                    ar, cs, od, hp, timestamp, month_key, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["osu_user_id"],
                    row["score_id"],
                    row.get("beatmap_id"),
                    row.get("mode"),
                    row.get("rank"),
                    row.get("mods"),
                    row.get("score"),
                    row.get("pp"),
                    row.get("accuracy"),
                    row.get("max_combo"),
                    row.get("misses"),
                    row.get("ar"),
                    row.get("cs"),
                    row.get("od"),
                    row.get("hp"),
                    row["timestamp"],
                    row["month_key"],
                    row.get("raw_json"),
                ),
            )
            con.commit()

    def scores_for_month(self, month_key: str) -> List[sqlite3.Row]:
        with self._connect() as con:
            cur = con.execute("SELECT * FROM scores WHERE month_key=?", (month_key,))
            return cur.fetchall()

    def scores_for_user_month(self, osu_user_id: int, month_key: str) -> List[sqlite3.Row]:
        with self._connect() as con:
            cur = con.execute(
                "SELECT * FROM scores WHERE osu_user_id=? AND month_key=? ORDER BY timestamp",
                (osu_user_id, month_key),
            )
            return cur.fetchall()

    def top_players_by_pp(self, month_key: str, limit: int = 10) -> List[sqlite3.Row]:
        with self._connect() as con:
            cur = con.execute(
                """
                SELECT p.username, s.osu_user_id, ROUND(SUM(COALESCE(s.pp, 0)), 2) as total_pp, COUNT(*) as plays
                FROM scores s
                JOIN players p ON p.osu_user_id = s.osu_user_id
                WHERE s.month_key=?
                GROUP BY s.osu_user_id
                ORDER BY total_pp DESC
                LIMIT ?
                """,
                (month_key, limit),
            )
            return cur.fetchall()

# ----------------------------
# Osu! API client (OAuth2 client credentials flow)
# ----------------------------
class OsuClient:
    def __init__(self, client_id: str, client_secret: str, session: aiohttp.ClientSession):
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session
        self._token: Optional[str] = None
        self._token_expiry: datetime = datetime.now(timezone.utc)

    async def _ensure_token(self):
        now = datetime.now(timezone.utc)
        if self._token and now < self._token_expiry:
            return
        data = {
            "client_id": int(self.client_id),
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "scope": "public",
        }
        async with self.session.post("https://osu.ppy.sh/oauth/token", json=data) as resp:
            resp.raise_for_status()
            payload = await resp.json()
            self._token = payload["access_token"]
            self._token_expiry = now + timedelta(seconds=int(payload.get("expires_in", 3600)) - 60)
            log.info("Obtained osu! API token valid until %s", self._token_expiry.isoformat())

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        await self._ensure_token()
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{OSU_API_BASE}{path}"
        async with self.session.get(url, headers=headers, params=params) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            return await resp.json()

    async def resolve_user(self, username_or_id: str) -> Optional[Dict[str, Any]]:
        path = f"/users/{username_or_id}"
        return await self._get(path)

    async def recent_scores(self, user_id: int, include_fails: bool = True, limit: int = 100) -> List[Dict[str, Any]]:
        params = {"include_fails": 1 if include_fails else 0, "limit": limit}
        data = await self._get(f"/users/{user_id}/scores/recent", params=params)
        return data or []

# ----------------------------
# Utilities
# ----------------------------

def month_key_for(dt: datetime) -> str:
    dt = dt.astimezone(TZ)
    return f"{dt.year:04d}-{dt.month:02d}"


def month_start(dt: Optional[datetime] = None) -> datetime:
    dt = dt.astimezone(TZ) if dt else datetime.now(TZ)
    return datetime(dt.year, dt.month, 1, tzinfo=TZ)


@dataclass
class ScoreRow:
    osu_user_id: int
    score_id: int
    beatmap_id: Optional[int]
    mode: Optional[str]
    rank: Optional[str]
    mods: Optional[str]
    score: Optional[int]
    pp: Optional[float]
    accuracy: Optional[float]
    max_combo: Optional[int]
    misses: Optional[int]
    ar: Optional[float]
    cs: Optional[float]
    od: Optional[float]
    hp: Optional[float]
    timestamp: str
    month_key: str
    raw_json: str

    @staticmethod
    def from_osu_json(osu_user_id: int, score: Dict[str, Any]) -> "ScoreRow":
        # Parse timestamp
        ended_at_str = score.get("ended_at") or score.get("created_at") or score.get("date")
        # ended_at is ISO 8601 with Z
        dt = datetime.fromisoformat(ended_at_str.replace("Z", "+00:00")) if ended_at_str else datetime.now(timezone.utc)
        # Beatmap data might be included inline
        beatmap_id = None
        try:
            if score.get("beatmap") and isinstance(score["beatmap"], dict):
                beatmap_id = int(score["beatmap"].get("id"))
            else:
                beatmap_id = int(score.get("beatmap_id")) if score.get("beatmap_id") else None
        except Exception:
            beatmap_id = None

        statistics = score.get("statistics") or {}
        mods = score.get("mods")
        if isinstance(mods, list):
            mods = ",".join(mods)

        attrs = score.get("beatmap") and score["beatmap"].get("difficulty_rating")
        # Some attributes like AR/CS/OD/HP are not always present in v2 score payload; set None safely
        ar = score.get("beatmap") and score["beatmap"].get("ar")
        cs = score.get("beatmap") and score["beatmap"].get("cs")
        od = score.get("beatmap") and score["beatmap"].get("accuracy")
        hp = score.get("beatmap") and score["beatmap"].get("drain")

        return ScoreRow(
            osu_user_id=int(osu_user_id),
            score_id=int(score.get("id") or score.get("score_id") or 0),
            beatmap_id=beatmap_id,
            mode=score.get("mode") or (score.get("ruleset_id") and str(score.get("ruleset_id"))),
            rank=score.get("rank"),
            mods=mods,
            score=score.get("score") or score.get("total_score"),
            pp=(score.get("pp") if score.get("pp") is not None else None),
            accuracy=score.get("accuracy"),
            max_combo=score.get("max_combo"),
            misses=(statistics.get("count_miss") if isinstance(statistics, dict) else None),
            ar=ar if isinstance(ar, (int, float)) else None,
            cs=cs if isinstance(cs, (int, float)) else None,
            od=od if isinstance(od, (int, float)) else None,
            hp=hp if isinstance(hp, (int, float)) else None,
            timestamp=dt.astimezone(timezone.utc).isoformat(),
            month_key=month_key_for(dt),
            raw_json=str(score),
        )

    def to_db_row(self) -> Dict[str, Any]:
        return self.__dict__


# ----------------------------
# Discord Bot
# ----------------------------
class OsuBot(discord.Client):
    def __init__(self, *, intents: discord.Intents, db: Database):
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)
        self.db = db
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.osu: Optional[OsuClient] = None
        # Schedule the daily task at 03:00 Europe/Berlin
        self.daily_fetch_task.change_interval(time=time(3, 0, tzinfo=TZ))

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.osu = OsuClient(OSU_CLIENT_ID, OSU_CLIENT_SECRET, self.http_session)
        # Sync commands
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Commands synced to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Commands synced globally")
        # Start task
        self.daily_fetch_task.start()

    async def close(self):
        await super().close()
        if self.http_session:
            await self.http_session.close()

    # ------------------------
    # Task: Fetch last 24h plays every day at 03:00 Berlin time
    # ------------------------
    @tasks.loop(time=time(3, 0, tzinfo=TZ))
    async def daily_fetch_task(self):
        try:
            await self.fetch_last_24h_for_all()
        except Exception as e:
            log.exception("Daily fetch failed: %s", e)

    async def fetch_last_24h_for_all(self):
        assert self.osu is not None
        # Time window: last 24h relative to run time
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=24)
        month_start_utc = month_start(now).astimezone(timezone.utc)
        players = self.db.list_players()
        log.info("Fetching last 24h scores for %d players", len(players))
        for p in players:
            user_id = int(p["osu_user_id"])
            try:
                scores = await self.osu.recent_scores(user_id, include_fails=True, limit=100)
            except Exception:
                log.exception("Failed to fetch recent scores for %s", user_id)
                continue

            for s in scores:
                # Parse time and filter by 24h window and by month-start
                ended_at_str = s.get("ended_at") or s.get("created_at") or s.get("date")
                if not ended_at_str:
                    continue
                dt = datetime.fromisoformat(ended_at_str.replace("Z", "+00:00")).astimezone(timezone.utc)
                if dt < since:
                    continue
                if dt < month_start_utc:
                    # Only store data from first of current month
                    continue
                row = ScoreRow.from_osu_json(user_id, s).to_db_row()
                self.db.insert_score(row)
        log.info("Fetch completed.")

    # ------------------------
    # Commands
    # ------------------------
    @discord.app_commands.command(name="add_player", description="Fügt einen osu!-Spieler hinzu (Username oder ID)")
    async def add_player(self, interaction: discord.Interaction, username_or_id: str):
        assert self.osu is not None
        await interaction.response.defer(thinking=True, ephemeral=True)
        user = await self.osu.resolve_user(username_or_id)
        if not user:
            await interaction.followup.send("Spieler nicht gefunden.")
            return
        osu_user_id = int(user["id"])
        username = user.get("username") or str(osu_user_id)
        self.db.add_player(osu_user_id, username, interaction.user.id)
        await interaction.followup.send(f"Hinzugefügt: {username} (ID {osu_user_id})")

    @discord.app_commands.command(name="remove_player", description="Entfernt einen osu!-Spieler (Username oder ID)")
    async def remove_player(self, interaction: discord.Interaction, username_or_id: str):
        assert self.osu is not None
        await interaction.response.defer(ephemeral=True)
        user = await self.osu.resolve_user(username_or_id)
        user_id = None
        if user:
            user_id = int(user["id"])
        else:
            # maybe it's a pure numeric ID
            if username_or_id.isdigit():
                user_id = int(username_or_id)
        if not user_id:
            await interaction.followup.send("Spieler nicht gefunden.")
            return
        ok = self.db.remove_player(user_id)
        await interaction.followup.send("Entfernt." if ok else "War nicht in der Liste.")

    @discord.app_commands.command(name="list_players", description="Zeigt die gespeicherten osu!-Spieler")
    async def list_players(self, interaction: discord.Interaction):
        players = self.db.list_players()
        if not players:
            await interaction.response.send_message("Keine Spieler gespeichert.")
            return
        lines = [f"• {p['username']} (ID {p['osu_user_id']})" for p in players]
        await interaction.response.send_message("Gespeicherte Spieler:\n" + "\n".join(lines))

    @discord.app_commands.command(name="fetch_now", description="Holt Plays der letzten 24h sofort")
    async def fetch_now(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        await self.fetch_last_24h_for_all()
        await interaction.followup.send("Fertig. Neue Scores (sofern vorhanden) gespeichert.")

    @discord.app_commands.command(name="leaderboard", description="Top-Spieler des Monats nach PP")
    async def leaderboard(self, interaction: discord.Interaction, limit: int = 10):
        month = month_key_for(datetime.now(TZ))
        rows = self.db.top_players_by_pp(month, limit=limit)
        if not rows:
            await interaction.response.send_message("Noch keine Daten für diesen Monat.")
            return
        embed = discord.Embed(title=f"Leaderboard {month}")
        for i, r in enumerate(rows, start=1):
            embed.add_field(
                name=f"#{i} {r['username']}",
                value=f"PP gesamt: {r['total_pp']}, Plays: {r['plays']}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(name="user_stats", description="Zeigt Monatsstatistiken für einen Spieler")
    async def user_stats(self, interaction: discord.Interaction, username_or_id: str):
        assert self.osu is not None
        await interaction.response.defer(thinking=True)
        user = await self.osu.resolve_user(username_or_id)
        if not user:
            await interaction.followup.send("Spieler nicht gefunden.")
            return
        osu_user_id = int(user["id"])
        username = user.get("username") or str(osu_user_id)
        month = month_key_for(datetime.now(TZ))
        rows = self.db.scores_for_user_month(osu_user_id, month)
        if not rows:
            await interaction.followup.send(f"Keine Daten für {username} in {month}.")
            return
        total_pp = round(sum((r["pp"] or 0.0) for r in rows), 2)
        avg_acc = round(sum((r["accuracy"] or 0.0) for r in rows) / len(rows) * 100, 2)
        best_pp = round(max((r["pp"] or 0.0) for r in rows), 2)
        embed = discord.Embed(title=f"{username} · {month}")
        embed.add_field(name="Plays", value=str(len(rows)))
        embed.add_field(name="PP gesamt", value=str(total_pp))
        embed.add_field(name="Beste PP", value=str(best_pp))
        embed.add_field(name="Ø Accuracy (%)", value=str(avg_acc))
        await interaction.followup.send(embed=embed)

    @discord.app_commands.command(name="user_graph", description="Zeigt eine Graphik (PP über Zeit) für einen Spieler im aktuellen Monat")
    async def user_graph(self, interaction: discord.Interaction, username_or_id: str):
        import matplotlib.pyplot as plt
        assert self.osu is not None
        await interaction.response.defer(thinking=True)
        user = await self.osu.resolve_user(username_or_id)
        if not user:
            await interaction.followup.send("Spieler nicht gefunden.")
            return
        osu_user_id = int(user["id"])
        username = user.get("username") or str(osu_user_id)
        month = month_key_for(datetime.now(TZ))
        rows = self.db.scores_for_user_month(osu_user_id, month)
        if not rows:
            await interaction.followup.send(f"Keine Daten für {username} in {month}.")
            return
        # Prepare data
        dts = [datetime.fromisoformat(r["timestamp"]) for r in rows]
        pps = [r["pp"] or 0.0 for r in rows]
        # Plot
        fig, ax = plt.subplots()
        ax.plot(dts, pps, marker="o")
        ax.set_title(f"PP über Zeit – {username} ({month})")
        ax.set_xlabel("Zeit")
        ax.set_ylabel("PP")
        fig.autofmt_xdate()
        buf = BytesIO()
        plt.tight_layout()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0)
        file = discord.File(buf, filename=f"pp_{username}_{month}.png")
        await interaction.followup.send(file=file)


# ----------------------------
# Entrypoint
# ----------------------------

def main():
    intents = discord.Intents.default()
    intents.guilds = True
    client = OsuBot(intents=intents, db=Database(DB_PATH))
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
