# app/sources/telegram_adapter.py
"""Telegram public channel scrape adapter.

Strategy:
  1. Guess candidate channel handles from brand name (fast, no extra HTTP).
  2. Search Lyzem.com for "{brand} apk" to discover non-obvious channels
     that share rogue APKs without using the brand in their channel name.
  3. For each channel, fetch t.me/s/{channel} (server-rendered HTML, no auth)
     and parse messages that explicitly mention the brand — one Hit per match.

Lyzem.com is a free Telegram search aggregator with server-rendered HTML.
It replaces the previous dependency on Brave Search for seeding channel lists.

Rate limits: ~1 req/s on both t.me and lyzem.com.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re
import time
import urllib.parse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.api.models import (
    FoundVia,
    Hit,
    HitType,
    HostInfo,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.services.brand_match import distinctive_tokens, mentions_any
from app.sources.base import SourceAdapter
from app.utils.http_client import BlockedError, FetchError, decode_response, fetch

log = logging.getLogger(__name__)

_LYZEM_SEARCH = "https://lyzem.com/search?q={q}&lang=en"
_TG_CHANNEL = "https://t.me/s/{channel}"
_LYZEM_INCLUDE_TYPES = {"channel", "group"}

# Maximum channels to visit per scan (guards HTTP budget)
_MAX_CHANNELS = 12

# Handles t.me answered with a redirect (no channel / no public preview): not asked again for a day.
# In-process memory, shared by all scans of this worker.
_ABSENT_TTL = 24 * 3600
_absent: dict[str, float] = {}


def _known_absent(channel: str) -> bool:
    ts = _absent.get(channel)
    if ts is None:
        return False
    if time.monotonic() - ts > _ABSENT_TTL:
        del _absent[channel]
        return False
    return True

# Matches direct .apk URLs in message text or hrefs (e.g. MediaFire, Drive, CDN links)
_APK_URL_RE = re.compile(r'https?://[^\s<>"\']+\.apk(?:\?[^\s<>"\']*)?', re.IGNORECASE)


def _extract_apk_url(msg_node: object) -> str | None:
    """Return the first APK download URL found in a Telegram message node."""
    # 1. Document attachment block — Telegram shows uploaded files as:
    #    <a class="tgme_widget_message_document" href="https://cdn.telegram-cdn.org/...">
    #      <div class="tgme_widget_message_document_title">app.apk</div>
    #    </a>
    for doc in msg_node.css(".tgme_widget_message_document"):  # type: ignore[union-attr]
        title_node = doc.css_first(".tgme_widget_message_document_title")
        title = title_node.text(strip=True) if title_node else ""
        if title.lower().endswith(".apk"):
            # The document block itself is often the <a> tag
            href = doc.attributes.get("href", "") if hasattr(doc, "attributes") else ""
            if not href:
                a = doc.css_first("a")
                href = a.attributes.get("href", "") if a else ""
            if href and href.startswith("http"):
                return href

    # 2. Explicit .apk hrefs anywhere in the message (e.g. MediaFire/Drive links)
    for a in msg_node.css("a"):  # type: ignore[union-attr]
        href = (a.attributes.get("href") or "").strip()
        if _APK_URL_RE.match(href):
            return href

    # 3. Raw URL in message text (unlinked paste)
    text_node = msg_node.css_first(".tgme_widget_message_text")
    if text_node:
        m = _APK_URL_RE.search(text_node.text(strip=True))
        if m:
            return m.group(0)

    return None


class TelegramAdapter(SourceAdapter):
    name = "telegram"
    method = SourceMethod.scrape
    host_group = "social"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        brand = ctx.request.brand_name
        brand_lower = brand.lower().replace(" ", "")
        brand_tokens = set(distinctive_tokens(brand))

        # Guessed channel handles — cheap, no extra HTTP
        guessed = [
            brand_lower,
            f"{brand_lower}apk",
            f"{brand_lower}_apk",
            f"{brand_lower}app",
            f"get{brand_lower}",
        ]

        # Lyzem-discovered channels — catches handles with no brand in the name
        lyzem_channels = await self._lyzem_search(brand, brand_tokens)

        # Merge, dedup, cap
        seen_ch: set[str] = set()
        channels: list[str] = []
        for ch in guessed + lyzem_channels:
            ch = ch.strip("/").lower()
            if ch and ch not in seen_ch:
                seen_ch.add(ch)
                channels.append(ch)

        hits: list[Hit] = []
        seen_msgs: set[str] = set()
        error: str | None = None

        for channel in [c for c in channels if not _known_absent(c)][:_MAX_CHANNELS]:
            url = _TG_CHANNEL.format(channel=channel)
            try:
                # t.me/s/<name> answers 302 (to the plain t.me/<name> page) when there is no public channel
                # preview: one request tells us, so do not follow it.
                resp = await fetch(url, rotate_ua=True, follow_redirects=False)
            except BlockedError as e:
                error = str(e)
                log.warning("telegram: channel %r blocked: %s", channel, e)
                continue
            except FetchError as e:
                error = str(e)
                log.warning("telegram: channel %r fetch failed: %s", channel, e)
                continue

            if resp.is_redirect or resp.status_code == 404:
                _absent[channel] = time.monotonic()
                log.info("telegram: %r has no public preview (HTTP %s); skipping it for 24h", channel, resp.status_code)
                continue

            html = decode_response(resp)
            hits.extend(self._parse_channel(html, channel, url, brand_tokens, seen_msgs))

        return hits, SourceStatus(
            name=self.name,
            method=self.method,
            status="ok" if not error else "failed",
            results=len(hits),
            error=error,
        )

    async def _lyzem_search(self, brand: str, brand_tokens: set[str]) -> list[str]:
        """Query Lyzem.com and return t.me channel handles that mention the brand."""
        queries = [f"{brand} apk", f"{brand} mod apk"]
        channels: list[str] = []
        seen: set[str] = set()

        for q in queries:
            url = _LYZEM_SEARCH.format(q=urllib.parse.quote(q))
            try:
                resp = await fetch(url, rotate_ua=True)
            except (BlockedError, FetchError) as e:
                log.warning("telegram: Lyzem search failed for %r: %s", q, e)
                continue

            if resp.status_code != 200:
                continue

            tree = HTMLParser(decode_response(resp))

            for item in tree.css("li.search-result"):
                # Skip bots and Telegraph articles — only channels and groups
                type_div = item.css_first("div.search-result-type-wrapper")
                result_type = (
                    type_div.attributes.get("title", "") if type_div else ""
                ).lower()
                if result_type not in _LYZEM_INCLUDE_TYPES:
                    continue

                a = item.css_first(".search-result-title a")
                if not a:
                    continue
                href = a.attributes.get("href", "")
                name = a.text(strip=True)

                # Only direct t.me links (skip telegra.ph / t.me/joinchat / etc.)
                if "t.me/" not in href:
                    continue
                handle = href.split("t.me/")[-1].strip("/").split("/")[0].lower()
                if not handle or handle in seen:
                    continue

                # Brand-token filter: name or description must mention the brand
                descr_node = item.css_first(".search-result-descr")
                descr = descr_node.text(strip=True) if descr_node else ""
                if brand_tokens and not mentions_any(name + " " + descr, brand_tokens):
                    continue

                seen.add(handle)
                channels.append(handle)

        return channels

    def _parse_channel(
        self,
        html: str,
        channel: str,
        channel_url: str,
        brand_tokens: set[str],
        seen: set[str],
    ) -> list[Hit]:
        tree = HTMLParser(html)
        hits: list[Hit] = []

        if not tree.css(".tgme_widget_message"):
            return []

        for msg in tree.css(".tgme_widget_message"):
            text_node = msg.css_first(".tgme_widget_message_text")
            text = text_node.text(strip=True) if text_node else ""
            if not text:
                continue

            # Only messages that explicitly mention the brand
            if brand_tokens and not mentions_any(text, brand_tokens):
                continue

            link_node = msg.css_first(".tgme_widget_message_date")
            msg_href = (
                link_node.attributes.get("href", channel_url) if link_node else channel_url
            )
            uid = hashlib.sha256(msg_href.encode()).hexdigest()[:32]
            if uid in seen:
                continue
            seen.add(uid)

            # app_name = cleaned channel handle for scoring — WRatio against brand
            # scores much higher than raw message text (a sentence).
            # title carries the message preview for display.
            # APK-distributing channels get host_group="apk_site" for the scoring bonus.
            channel_name = channel.replace("_", " ").replace("-", " ")
            apk_url = _extract_apk_url(msg)
            is_apk_channel = "apk" in channel.lower() or "apk" in text.lower() or bool(apk_url)
            host_group = "apk_site" if is_apk_channel else self.host_group
            hits.append(
                Hit(
                    id=uid,
                    type=HitType.channel_post,
                    app_name=channel_name,
                    title=f"Telegram @{channel}: {text[:120]}",
                    url=msg_href,
                    apk_url=apk_url,
                    host=HostInfo(
                        group=host_group,
                        name="Telegram",
                        domain="t.me",
                        is_official_store=False,
                    ),
                    found_via=[
                        FoundVia(source=self.name, method=self.method, query=channel)
                    ],
                    relevance_score=0,
                    discovered_at=datetime.datetime.now(datetime.timezone.utc),
                )
            )

        return hits