from __future__ import annotations
import contextlib
from datetime import timedelta
from sqlalchemy import create_engine, func, select, desc, and_
from sqlalchemy.orm import sessionmaker
from models import Base, User, Play, TopStats, LeaderBoardSnapshot
from utils import utcnow_naive

class Storage:
    def __init__(self, db_url: str):
        self.engine = create_engine(db_url, future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False, future=True)

    @contextlib.contextmanager
    def session(self):
        s = self.Session()
        try:
            yield s
            s.commit()
        except:
            s.rollback()
            raise
        finally:
            s.close()

    # Users
    def upsert_user(self, discord_id: str, osu_user_id: str, osu_username: str) -> User:
        with self.session() as s:
            u = s.scalar(select(User).where(User.discord_id == discord_id))
            if u:
                u.osu_user_id = osu_user_id
                u.osu_username = osu_username
            else:
                u = User(discord_id=discord_id, osu_user_id=osu_user_id, osu_username=osu_username)
                s.add(u)
            return u

    def get_user_by_discord(self, discord_id: str) -> User | None:
        with self.session() as s:
            return s.scalar(select(User).where(User.discord_id == discord_id))

    def get_user_by_osu_username(self, username: str) -> User | None:
        with self.session() as s:
            return s.scalar(select(User).where(func.lower(User.osu_username) == username.lower()))

    def get_all_users(self) -> list[User]:
        with self.session() as s:
            return list(s.scalars(select(User)))

    # Plays
    def insert_play_if_new(self, p: Play) -> bool:
        with self.session() as s:
            exists = s.scalar(
                select(Play.id).where(
                    (Play.user_id == p.user_id)
                    & (Play.beatmap_id == p.beatmap_id)
                    & (Play.timestamp == p.timestamp)
                )
            )
            if exists:
                return False
            s.add(p)
            return True

    def recent_plays_for_user(self, user_id: str, hours: int | None = None) -> list[Play]:
        with self.session() as s:
            q = select(Play).where(Play.user_id == user_id)
            if hours is not None:
                since = utcnow_naive() - timedelta(hours=hours)
                q = q.where(Play.timestamp >= since)
            return list(s.scalars(q.order_by(Play.timestamp.desc())))

    def plays_in_month(self, user_id: str, year: int, month: int) -> list[Play]:
        from datetime import datetime as dt
        start = dt(year, month, 1)
        # nächster Monat:
        end = dt(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
        with self.session() as s:
            q = select(Play).where((Play.user_id == user_id) & (Play.timestamp >= start) & (Play.timestamp < end))
            return list(s.scalars(q))

    def cumulative_push(self, user_id: str, scope_hours: int | None = None) -> float:
        from sqlalchemy import select
        with self.session() as s:
            q = select(func.sum(Play.push_value)).where((Play.user_id == user_id) & (Play.push_value != None))
            if scope_hours is not None:
                since = utcnow_naive() - timedelta(hours=scope_hours)
                q = q.where(Play.timestamp >= since)
            val = s.scalar(q)
            return float(val or 0.0)

    # TopStats
    def get_topstats(self, user_id: str, month_str: str) -> TopStats | None:
        with self.session() as s:
            return s.scalar(select(TopStats).where((TopStats.user_id == user_id) & (TopStats.month == month_str)))

    def upsert_topstats(self, ts: TopStats) -> None:
        with self.session() as s:
            existing = s.scalar(
                select(TopStats).where((TopStats.user_id == ts.user_id) & (TopStats.month == ts.month))
            )
            if existing:
                existing.top10_avg_star_raw = ts.top10_avg_star_raw
                existing.top10_miss_sum = ts.top10_miss_sum
                existing.top_star_TS = ts.top_star_TS
                existing.top50_pp_threshold = ts.top50_pp_threshold
            else:
                s.add(ts)

    # Leaderboard
    def snapshot_leaderboard(self, scope_hours: int | None, entries: list[dict]) -> LeaderBoardSnapshot:
        from datetime import datetime
        snap = LeaderBoardSnapshot(generated_at=utcnow_naive(), scope_hours=scope_hours, entries=entries)
        with self.session() as s:
            s.add(snap)
        return snap
