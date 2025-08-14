from __future__ import annotations
import uuid
from datetime import datetime
from sqlalchemy import (
    String, Integer, Float, Boolean, DateTime, ForeignKey, UniqueConstraint, Index, JSON
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.sqlite import JSON as SQLITE_JSON

class Base(DeclarativeBase):
    pass

def _uuid() -> str:
    return str(uuid.uuid4())

class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    discord_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    osu_user_id: Mapped[str] = mapped_column(String, nullable=False)
    osu_username: Mapped[str] = mapped_column(String, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    plays: Mapped[list["Play"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    topstats: Mapped[list["TopStats"]] = relationship(back_populates="user", cascade="all, delete-orphan")

class Play(Base):
    __tablename__ = "plays"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)

    beatmap_id: Mapped[str] = mapped_column(String, nullable=False)
    map_length_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    star_rating: Mapped[float] = mapped_column(Float, nullable=False)
    miss_count: Mapped[int] = mapped_column(Integer, nullable=False)
    accuracy_percent: Mapped[float] = mapped_column(Float, nullable=False)
    pp: Mapped[float] = mapped_column(Float, nullable=False)
    failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    push_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)  # 'recent' | 'historical'

    user: Mapped["User"] = relationship(back_populates="plays")

    __table_args__ = (
        UniqueConstraint("user_id", "beatmap_id", "timestamp", name="uq_user_map_time"),
        Index("ix_user_timestamp", "user_id", "timestamp"),
        Index("ix_user_pp_desc", "user_id", "pp"),
    )

class TopStats(Base):
    __tablename__ = "topstats"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    month: Mapped[str] = mapped_column(String, nullable=False, index=True)  # YYYY-MM

    top10_avg_star_raw: Mapped[float] = mapped_column(Float, nullable=False)
    top10_miss_sum: Mapped[int] = mapped_column(Integer, nullable=False)
    top_star_TS: Mapped[float] = mapped_column(Float, nullable=False)  # computed TS
    top50_pp_threshold: Mapped[float] = mapped_column(Float, nullable=False)

    user: Mapped["User"] = relationship(back_populates="topstats")

    __table_args__ = (
        UniqueConstraint("user_id", "month", name="uq_user_month"),
    )

class LeaderBoardSnapshot(Base):
    __tablename__ = "leaderboard_snapshots"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    scope_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Store entries as JSON array [{user_id, osu_username, cumulative_push_value, rank}]
    entries: Mapped[dict] = mapped_column(SQLITE_JSON, nullable=False)
