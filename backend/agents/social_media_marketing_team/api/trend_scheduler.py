"""Trend discovery scheduler and background job for the social media marketing team."""

from __future__ import annotations

import logging
import threading
from typing import Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from blog_research_agent.tools.web_search import OllamaWebSearch

from llm_service import get_client
from social_media_marketing_team.trend_discovery_agent import TrendDiscoveryAgent
from social_media_marketing_team.trend_models import TrendDigest

logger = logging.getLogger(__name__)

_latest_digest: Optional[TrendDigest] = None
_digest_lock = threading.Lock()
_scheduler: Optional[BackgroundScheduler] = None


def get_latest_digest() -> Optional[TrendDigest]:
    """Return the most recently computed trend digest (thread-safe)."""
    with _digest_lock:
        return _latest_digest


def run_trend_job() -> None:
    """Run trend discovery and update the cached digest. Safe to call from any thread."""
    global _latest_digest
    logger.info("TrendDiscovery: starting run")
    try:
        llm = get_client("trend_discovery")
        searcher = OllamaWebSearch()
        agent = TrendDiscoveryAgent(llm_client=llm, web_search=searcher)
        digest = agent.run()
        with _digest_lock:
            _latest_digest = digest
        logger.info(
            "TrendDiscovery: completed — %d topics, generated_at=%s",
            len(digest.topics),
            digest.generated_at,
        )
    except Exception as exc:
        logger.error("TrendDiscovery: run failed: %s", exc, exc_info=True)


def start_scheduler() -> None:
    """Start the APScheduler background scheduler for daily trend discovery."""
    global _scheduler
    et = pytz.timezone("America/New_York")
    _scheduler = BackgroundScheduler(timezone=et)
    _scheduler.add_job(
        run_trend_job,
        CronTrigger(hour=8, minute=0, timezone=et),
        id="trend_discovery_daily",
        name="Daily social media trend discovery",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("TrendDiscovery: scheduler started — daily job at 08:00 America/New_York")


def stop_scheduler() -> None:
    """Shut down the scheduler if it is running."""
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("TrendDiscovery: scheduler stopped")
