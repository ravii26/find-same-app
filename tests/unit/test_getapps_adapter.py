"""GetApps (Xiaomi) adapter: the publisher comes from publisherName, as the search API returns it."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.api.models import ScanContext, ScanRequest
from app.services.keyword_expansion import expand
from app.sources import getapps_adapter

# Shape of a live response (search "zomato", 2026-11-01): publisherName is always there, developerName only sometimes
_RESPONSE = {"list": [{"type": "recZone", "data": {"listApp": [
    {"displayName": "Zomato- Food Delivery & Dining", "publisherName": "Zomato",
     "packageName": "com.application.zomato", "icon": "AppStore/028f"},
    {"displayName": "District- Movies Events Dining", "publisherName": "Aptoide", "developerName": "Zomato",
     "packageName": "com.application.zomato.district", "icon": "AppStore/0a1b"},
    {"displayName": "Zomato Delivery Partner", "developerName": "Eternal Limited",
     "packageName": "com.carthero.driver"},
]}}]}


async def test_publisher_is_read_from_publisher_name(monkeypatch):
    async def fetch(url, **kw):
        return SimpleNamespace(content=json.dumps(_RESPONSE).encode())

    monkeypatch.setattr(getapps_adapter, "fetch", fetch)
    request = ScanRequest(brand_name="Zomato", android_packages=["com.application.zomato"])
    ctx = ScanContext(scan_id="scn_x", request=request, expanded_keywords=expand(request))
    hits, status = await getapps_adapter.GetAppsAdapter().search(ctx)

    publishers = {h.package_id: h.publisher for h in hits}
    assert status.status == "ok"
    assert publishers["com.application.zomato"] == "Zomato"
    # an app GetApps re-hosts from Aptoide shows Aptoide, the account that lists it, not the copied developer name
    assert publishers["com.application.zomato.district"] == "Aptoide"
    # without publisherName the developer name is still used
    assert publishers["com.carthero.driver"] == "Eternal Limited"
