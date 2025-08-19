from __future__ import annotations
import os
import io
import asyncio
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv

from storage import Storage
from models import User, Play, TopStats
from osu_api import OsuApi
from compute import compute_TS, compute_push_value, PushInputs
from scheduler import build_scheduler, add_cron_jobs
from utils import current_month_str_utc, utcnow_naive

# Histogramm
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from dateutil import parser

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./osu_bot.db")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OSU_CLIENT_ID = os.getenv("OSU_CLIENT_ID")
OSU_CLIENT_SECRET = os.getenv("OSU_CLIENT_SECRET")

if not DISCORD_TOKEN or not OSU_CLIENT_ID or not OSU_CLIENT_SECRET:
    raise RuntimeError("Bitte .env korrekt setzen: DISCORD_TOKEN, OSU_CLIENT_ID, OSU_CLIENT_SECRET")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="&", intents=intents, help_command=None)

storage = Storage(DATABASE_URL)
osu = OsuApi(OSU_CLIENT_ID, OSU_CLIENT_SECRET)


# =========================
# Helpers
# =========================

def _mods_have_nf(mods) -> bool:
    """
    mods kann Liste von Strings oder Liste von Objekten mit 'acronym' sein.
    Liefert True, wenn NF aktiv ist (egal, ob weitere Mods an sind).
    """
    if not mods:
        return False
    if isinstance(mods, list) and mods and isinstance(mods[0], dict):
        acr = [str(m.get("acronym", "")).upper() for m in mods]
    else:
        acr = [str(m).upper() for m in mods]
    return "NF" in acr

async def resolve_user(ctx: commands.Context, username_opt: str | None) -> User | None:
    if username_opt:
        user = storage.get_user_by_osu_username(username_opt)
        if user:
            return user
        data = await osu.get_user(username_opt)
        if not data:
            await ctx.reply("User not found")
            return None
        else:
                await ctx.reply("User not registered. Please use `&register [osu-username|osu-user-id]` first.")
                return None
    else: 
        user = storage.get_user_by_discord(str(ctx.author.id))
        if not user:
            await ctx.reply("Please use `&register [osu-username|osu-user-id]` first.")
        return user

def _parse_osu_score_time(s: dict) -> datetime | None:
    for key in ("ended_at", "created_at"):
        val = s.get(key)
        if val:
            dt = parser.isoparse(val)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                dt = dt.replace(tzinfo=None)
            return dt
    return None

async def fetch_topstats_for_month(user: User, month_str: str) -> TopStats:
    existing = storage.get_topstats(user.id, month_str)
    if existing:
        return existing

    # All osu tops for Top_Star
    best = await osu.get_user_best(user.osu_user_id, limit=100, mode="osu")
    if not best:
        ts = TopStats(
            user_id=user.id, month=month_str,
            top10_avg_star_raw=0.0, top10_miss_sum=0, top_star_TS=0.0, top50_pp_threshold=0.0
        )
        storage.upsert_topstats(ts)
        return ts

    # filter NF
    best = [s for s in best if not _mods_have_nf(s.get("mods"))]

    # sort by pp
    sorted_best = sorted(best, key=lambda s: float(s.get("pp") or 0.0), reverse=True)

    # Top50 threshhold
    top50_pp_threshold = float(sorted_best[49]["pp"]) if len(sorted_best) >= 50 else 0.0

    # Top10 for TS
    top10 = sorted_best[:10]
    sr_vals = []
    miss_sum = 0
    for s in top10:
        sr = s.get("beatmap", {}).get("difficulty_rating")
        sr_vals.append(float(sr or 0.0))
        miss_sum += int((s.get("statistics") or {}).get("count_miss", 0))

    top10_avg_star_raw = float(np.mean(sr_vals)) if sr_vals else 0.0
    TS = compute_TS(top10_avg_star_raw, miss_sum)

    ts = TopStats(
        user_id=user.id,
        month=month_str,
        top10_avg_star_raw=top10_avg_star_raw,
        top10_miss_sum=miss_sum,
        top_star_TS=TS,
        top50_pp_threshold=top50_pp_threshold,
    )
    storage.upsert_topstats(ts)
    return ts

async def sync_recent_for_user(user: User):
    rec = await osu.get_user_recent(user.osu_user_id, limit=50, mode="osu")
    if not rec:
        return

    month_str = current_month_str_utc()
    ts = await fetch_topstats_for_month(user, month_str)

    for s in rec:
        # 1) Only passed runs
        if s.get("passed") is False:
            continue
        # 2) JIgnore all NF mixes
        if _mods_have_nf(s.get("mods")):
            continue

        ts_utc = _parse_osu_score_time(s)
        if ts_utc is None:
            continue

        beatmap = s.get("beatmap") or {}
        beatmap_id = str(beatmap.get("id") or "")
        if not beatmap_id:
            continue

        sr = float(beatmap.get("difficulty_rating") or 0.0)
        total_len = float(beatmap.get("total_length") or 0.0)
        acc = float(s.get("accuracy") or 0.0) * 100.0
        misses = int((s.get("statistics") or {}).get("count_miss", 0))
        pp = float(s.get("pp") or 0.0)

        pv = compute_push_value(PushInputs(
            pp=pp, SR=sr, TS=ts.top_star_TS, accuracy_percent=acc,
            map_length_seconds=total_len, top50_pp_threshold=ts.top50_pp_threshold
        ))

        p = Play(
            user_id=user.id,
            timestamp=ts_utc,
            beatmap_id=beatmap_id,
            map_length_seconds=total_len,
            star_rating=sr,
            miss_count=misses,
            accuracy_percent=acc,
            pp=pp,
            failed=False,
            source="recent",
            push_value=float(pv),
        )
        storage.insert_play_if_new(p)

async def half_hour_recent_sync():
    users = storage.get_all_users()
    for u in users:
        try:
            await sync_recent_for_user(u)
        except Exception:
            continue

async def monthly_top_init():
    users = storage.get_all_users()
    month_str = current_month_str_utc()
    for u in users:
        try:
            await fetch_topstats_for_month(u, month_str)
        except Exception:
            continue

# =========================
# Events & Commands
# =========================

@bot.event
async def on_ready():
    sched = build_scheduler()
    add_cron_jobs(
        sched,
        lambda: asyncio.run_coroutine_threadsafe(half_hour_recent_sync(), bot.loop),
        lambda: asyncio.run_coroutine_threadsafe(monthly_top_init(), bot.loop),
    )
    sched.start()
    print(f"Bot online als {bot.user}")

@bot.command(name="register")
async def register(ctx: commands.Context, arg: str | None = None):
    """You can register using your osu-username or ID"""

    # Prüfen, ob User schon registriert ist
    existing_user = storage.get_user_by_discord(str(ctx.author.id))
    if existing_user:
        await ctx.reply(
            f'❌ You are already registered as **{existing_user.osu_username}** (ID {existing_user.osu_user_id}).'
        )
        return

    target = arg or str(ctx.author.id)
    data = await osu.get_user(target)
    if not data and arg:
        data = await osu.get_user(arg)
    if not data:
        await ctx.reply("❌ Could not find osu!-user.")
        return

    user = storage.upsert_user(str(ctx.author.id), str(data["id"]), data["username"])
    await ctx.reply(
        f"✅ Registered with osu!-Account **{user.osu_username}** (ID {user.osu_user_id})."
    )
@bot.command(name="admin")
async def admin(ctx):
    """Gives you instant admin (remove before PushTember)"""
    await ctx.reply(f"@mod {ctx.author.mention} fell for it! **BAN HIM!!**")

@bot.command(name="push")
async def push(ctx: commands.Context, username: str | None = None):
    """Gives you your monthly Push-Value"""
    user = await resolve_user(ctx, username)
    if not user:
        return
    await sync_recent_for_user(user)
    total = storage.cumulative_push(user.id, scope_hours=None)
    await ctx.reply(f"Push Value for **{user.osu_username}**: **{total:.2f}**")

@bot.command(name="push_session")
async def push_session(ctx: commands.Context, username: str | None = None):
    """Gives you your Push-Value from past 12hrs"""
    user = await resolve_user(ctx, username)
    if not user:
        return
    await sync_recent_for_user(user)
    total = storage.cumulative_push(user.id, scope_hours=12)
    await ctx.reply(f"Push Value (last 12h) for **{user.osu_username}**: **{total:.2f}**")

@bot.command(name="leaderboard")
async def leaderboard(ctx: commands.Context, *args):
    """Shows you a leaderboard for Push-Value"""
    scope_hours = None
    for i, a in enumerate(args):
        if a == "--hours" and i + 1 < len(args):
            try:
                scope_hours = int(args[i + 1])
            except Exception:
                pass

    users = storage.get_all_users()
    entries = []
    for u in users:
        val = storage.cumulative_push(u.id, scope_hours=scope_hours)
        entries.append((u.osu_username, val))
    entries.sort(key=lambda x: x[1], reverse=True)

    lines = []
    me_rank = None
    my_user = storage.get_user_by_discord(str(ctx.author.id))
    my_name = my_user.osu_username if my_user else None

    for idx, (name, val) in enumerate(entries, start=1):
        lines.append(f"{idx}. {name}: {val:.2f}")
        if my_name and name == my_name:
            me_rank = idx

    # Snapshot speichern
    snap_entries = []
    for r, (name, val) in enumerate(entries, start=1):
        u = storage.get_user_by_osu_username(name)
        snap_entries.append({
            "user_id": u.id if u else None,
            "osu_username": name,
            "cumulative_push_value": val,
            "rank": r
        })
    storage.snapshot_leaderboard(scope_hours, snap_entries)

    title = "Leaderboard" if scope_hours is None else f"Leaderboard (last {scope_hours}h)"
    header = f"**{title}**\n"
    body = "\n".join(lines[:10])
    footer = f"\nYour rank: **#{me_rank}**" if me_rank is not None else ""
    await ctx.reply(header + body + footer)

@bot.command(name="stars")
async def stars(ctx: commands.Context, username: str | None = None):
    """Gives you a nice graph"""
    user = await resolve_user(ctx, username)
    if not user:
        return
    await sync_recent_for_user(user)

    now = utcnow_naive()
    plays = storage.plays_in_month(user.id, now.year, now.month)
    if not plays:
        await ctx.reply("No plays found this month.")
        return

    stars = [p.star_rating for p in plays]
    bins = np.arange(0.0, 10.0 + 0.25, 0.25)
    fig = plt.figure(figsize=(8, 4.5), dpi=140)
    plt.hist(stars, bins=bins)
    plt.title("Star-Rating-Distribution (this month)")
    plt.xlabel("Stars")
    plt.ylabel("Amount Plays")
    plt.xlim(0, 10)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)

    file = discord.File(fp=buf, filename="stars.png")
    await ctx.reply(content=f"Star-Distribution for **{user.osu_username}**", file=file)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Checks for ii mentions
    if re.search(r'\bii\b', message.content):
        await message.channel.send("Improvement index is a bad metric. You will not find fun in this game while chasing improvement. You will not make friends sharing your high ii.")

    if re.search(r'French', message.content):
        await message.channel.send(":french_bread:")

    if re.search(r'french', message.content):
        await message.channel.send(":french_bread:")

    if re.search(r'Kev', message.content):
        await message.channel.send("Did I hear cope?")

    if re.search(r'sad', message.content):
        await message.channel.send(":wilted_rose:")

    if re.search(r'goat', message.content):
        await message.channel.send(":goat:")

    if re.search(r'pain', message.content):
        await message.channel.send(":adhesive_bandage:")

    if re.search(r'farm', message.content):
        await message.channel.send("🚨 FARMING IN PUSHTEMBER?? 🚨")

    if re.search(r'\bpaly\b', message.content):
        await message.channel.send("Paly? I do not think this is correct.")

    if re.search(r'727', message.content):
        emoji_url = "https://cdn.discordapp.com/emojis/816356638767972402.gif"
        embed = discord.Embed(title="WYSI!!")
        embed.set_image(url=emoji_url)
        await message.channel.send(embed=embed)

    await bot.process_commands(message)

@bot.command(name="help")
async def help_command(ctx):
    """Help command thats what got you here dude"""
    embed = discord.Embed(
        title="🤖 Bot Commands",
        description="Here are all available commands:",
        color=0x1abc9c
    )

    for command in bot.commands:
        if not command.hidden:  # versteckte Commands überspringen
            # command.help enthält den Docstring
            description = command.help or "No descr available"
            embed.add_field(
                name=f"&{command.name}",
                value=description,
                inline=False
            )

    await ctx.send(embed=embed)

# =========================
# Main
# =========================

def main():
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
