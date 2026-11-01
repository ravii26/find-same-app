# app/sources/registry.py
"""Source adapter registry — returns all adapters for a scan."""

from __future__ import annotations

from app.sources.apk4fun_adapter import APK4FunAdapter
from app.sources.apkboat_adapter import APKBoatAdapter
from app.sources.apkcombo_adapter import APKComboAdapter
from app.sources.apkfab_adapter import APKFabAdapter
from app.sources.apkkit_adapter import APKKitAdapter
from app.sources.apkmirror_adapter import APKMirrorAdapter
from app.sources.apkpure_adapter import APKPureAdapter
from app.sources.aptoide_adapter import AptoideAdapter
from app.sources.base import SourceAdapter
from app.sources.bing_adapter import BingAdapter
from app.sources.duckduckgo_adapter import DuckDuckGoAdapter
from app.sources.fdroid_adapter import FDroidAdapter
from app.sources.getapps_adapter import GetAppsAdapter
from app.sources.github_adapter import GitHubAdapter
from app.sources.gitlab_adapter import GitLabAdapter
from app.sources.google_play_scraper_adapter import GooglePlayScraperAdapter
from app.sources.happymod_adapter import HappyModAdapter
from app.sources.huawei_adapter import HuaweiAdapter
from app.sources.indus_appstore_adapter import IndusAppstoreAdapter
from app.sources.itunes import ItunesAdapter
from app.sources.lookalike_domain_adapter import LookalikeDomainAdapter
from app.sources.samsung_adapter import SamsungAdapter
from app.sources.softonic_adapter import SoftonicAdapter
from app.sources.taptap_adapter import TapTapAdapter
from app.sources.telegram_adapter import TelegramAdapter
from app.sources.uptodown_adapter import UptodownAdapter


def get_all_adapters() -> list[SourceAdapter]:
    """Instantiate and return all registered source adapters."""
    return [
        # Official / major app stores — no credentials
        ItunesAdapter(),
        GooglePlayScraperAdapter(),
        GetAppsAdapter(),
        SamsungAdapter(),           # Playwright
        # Web search / domains — no credentials
        BingAdapter(),
        DuckDuckGoAdapter(),
        LookalikeDomainAdapter(),   # needs official_domain in the request
        # APK sites — HTTP/scrape, no credentials
        AptoideAdapter(),
        APKPureAdapter(),
        APKMirrorAdapter(),
        APKComboAdapter(),
        APKFabAdapter(),
        APK4FunAdapter(),
        APKKitAdapter(),
        APKBoatAdapter(),
        SoftonicAdapter(),
        HappyModAdapter(),          # specialises in modded APKs — Playwright
        # Third-party / regional app stores
        TapTapAdapter(),
        IndusAppstoreAdapter(),     # PhonePe Appstore — static HTTP
        # Code / package repositories — no credentials (tokens only raise rate limits)
        GitHubAdapter(),
        GitLabAdapter(),
        FDroidAdapter(),
        # Social — no credentials
        TelegramAdapter(),
        # Playwright-based scrapers
        HuaweiAdapter(),
        UptodownAdapter(),
    ]
