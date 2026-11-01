"""Samsung Galaxy Store adapter — the store's own search API.

Endpoint: GET https://galaxystore.samsung.com/storeserver/search?keyword={term}&startNum=1&endNum={n}
Auth:     none. It is the call the store's own web page makes to fill its search results.
Returns:  XML (SamsungProtocol). Each <list> is one app: productName, GUID (the package id), sellerName,
          productImgUrl, playStoreProductYN (Y = the store shows the app's Google Play listing).

The web page itself (/search?q=) is a single-page app: the server answers it with 404 and the browser fetches the
results from this endpoint afterwards. Reading the page instead of the endpoint only finds the store's promoted apps.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from app.api.models import FoundVia, Hit, HitType, HostInfo, ScanContext, SourceMethod, SourceStatus
from app.services.brand_match import brand_terms
from app.sources.base import SourceAdapter, scrape_status
from app.utils.http_client import BlockedError, FetchError, decode_response, fetch

log = logging.getLogger(__name__)

_SEARCH = "https://galaxystore.samsung.com/storeserver/search"
_DETAIL = "https://galaxystore.samsung.com/detail/{guid}"
_PER_TERM = 20


def parse_results(xml_text: str) -> list[dict[str, str]]:
    """Apps in one search response: [{name, package, publisher, icon}]. Raises ValueError on an error response.

    Pure, so it is unit-testable.
    """
    root = ET.fromstring(xml_text)
    response = root.find("response")
    if response is None:
        raise ValueError("no <response> in the reply")
    error = response.find("errorInfo/errorString")
    code = response.get("returnCode", "0")
    if code != "0" or (error is not None and error.get("errorCode", "0") != "0"):
        raise ValueError(f"search returned code {code}")
    apps = []
    for item in response.findall("list"):
        values = {v.get("name"): (v.text or "").strip() for v in item.findall("value")}
        if values.get("GUID"):
            apps.append({
                "name": values.get("productName") or values["GUID"],
                "package": values["GUID"],
                "publisher": values.get("sellerName") or None,
                "icon": values.get("productImgUrl") or None,
            })
    return apps


class SamsungAdapter(SourceAdapter):
    name = "samsung_galaxy"
    method = SourceMethod.api
    host_group = "app_store"
    max_results = 10  # official store — expect 1 official app + a few lookalikes

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        terms = brand_terms(ctx, kinds=("original", "package"), limit=3, variants=2)
        if not terms:
            return [], SourceStatus(name=self.name, method=self.method, status="skipped",
                                    error="no brand-relevant keywords")

        hits: list[Hit] = []
        seen: set[str] = set()
        errors: list[str] = []
        blocked: list[str] = []

        for term in terms:
            url = f"{_SEARCH}?{urlencode({'keyword': term, 'startNum': 1, 'endNum': _PER_TERM})}"
            try:
                apps = parse_results(decode_response(await fetch(url, headers={"Accept": "application/xml"})))
            except BlockedError as exc:
                blocked.append(str(exc))
                continue
            except (FetchError, ValueError, ET.ParseError) as exc:
                log.warning("samsung: search failed for %r: %s", term, exc)
                errors.append(f"{term!r}: {type(exc).__name__}: {exc}")
                continue

            for app in apps:
                if app["package"] in seen:
                    continue
                seen.add(app["package"])
                detail = _DETAIL.format(guid=app["package"])
                hits.append(Hit(
                    id=hashlib.sha256(detail.encode()).hexdigest()[:32],
                    type=HitType.app_listing,
                    title=app["name"],
                    app_name=app["name"],
                    package_id=app["package"],
                    publisher=app["publisher"],
                    icon_url=app["icon"],
                    url=detail,
                    host=HostInfo(group=self.host_group, name="Samsung Galaxy Store",
                                  domain="galaxystore.samsung.com", is_official_store=True),
                    found_via=[FoundVia(source=self.name, method=self.method, query=term)],
                    relevance_score=0,
                    discovered_at=datetime.datetime.now(datetime.timezone.utc),
                ))

        return hits, scrape_status(self.name, self.method, hits, errors=errors, blocked=blocked)
