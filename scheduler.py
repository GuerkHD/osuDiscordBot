from __future__ import annotations
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

def build_scheduler():
    tz = pytz.UTC
    sched = AsyncIOScheduler(timezone=tz)
    return sched

def add_cron_jobs(sched: AsyncIOScheduler, half_hour_cb, monthly_cb):
    # "CRON: 0,30 * * * *"
    sched.add_job(half_hour_cb, CronTrigger(minute="0,30", timezone=pytz.UTC), id="half_hour_recent_sync")
    # "CRON: 0 0 1 * *"
    sched.add_job(monthly_cb, CronTrigger(minute=0, hour=0, day="1", timezone=pytz.UTC), id="monthly_top_init")
