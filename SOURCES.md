# Sources: search terms, limits and how each one is read

This file covers the 23 sources registered in `app/sources/registry.py`. Terms and request URLs come from the PhonePe
scan recorded in `tmp/live-scan-PhonePe-4`. Limits come from the adapter code. Counts are from that recording:
- **kept** = hits the adapter returned after its own brand filter (`results` in `sources/<name>.json`), before scoring.
- **requests** = recorded HTTP/browser requests for that source in `requests/<name>/`.

## Scan-wide rules

| Rule | Value | Where |
|---|---|---|
| Countries for country-aware sources | `US, IN, GB, SG, AE` | `scan_countries` default in `app/config/settings.py` |
| Result cap | the first `max_results` hits per distinct query are kept (default 20) | `SourceAdapter._run` in `app/sources/base.py` |
| Time limit per source | 240 s | `source_timeout_seconds` |
| Time limit per scan | 600 s | `scan_timeout_seconds` |
| Brand filter | a hit is kept only when its name or package carries the brand or a near-miss spelling (`presence_any`, fuzzy) | `app/services/brand_match.py` |
| Description cap | 1000 characters | per adapter |

### How keywords are built (`app/services/keyword_expansion.py`, `expand()`)

The list is built once per scan (`app/services/orchestrator.py`) from the brand name and any user-supplied keywords.
Terms are added in this order. A term is skipped if it repeats an earlier one (case-insensitive). The whole list is
capped at `max_keywords`.

| Type | Rule | Cap | PhonePe example |
|---|---|---|---|
| original | the brand name, each brand word, then user-supplied keywords | none | `PhonePe` |
| variant | spacing forms of a multi-word brand: `a b`, `a-b`, `ab`, `a_b` | none | none (one word) |
| package | tokens of the Android and iOS package id, skipping `com`, `org`, `net`, `io`, `app` and tokens under 3 letters | none | `PhonePeApp` |
| lure | `<brand> <suffix>` from `default.lure_suffixes` in `config/phrases.yaml` (English only) | the suffix list (13) | `PhonePe apk`, `PhonePe pro`, … |
| typo | omitted letters, vowel swaps, adjacent swaps, doubled letters, keyboard-neighbour swaps (interleaved, so every kind gets a slot) | 5 | `honepe`, `phanepe`, … |
| homoglyph | look-alike characters: Cyrillic letters, `0` for `o`, `1` for `l`, `rn` for `m`, and so on | 5 | |
| phonetic | rules such as `ph→f`, `ck→k`, `tion→shun`, `ee→e` | 3 | `fonepe` |

Lure, typo, homoglyph and phonetic terms are built from the brand name only, never from user-supplied keywords.

### How each source chooses from the list

- **Store APIs** call `brand_terms(ctx, kinds, limit, variants)` (`app/services/brand_match.py`). It takes the first
  `limit` original or package terms that contain a distinctive brand token, then up to `variants` typo, homoglyph,
  phonetic or variant terms. Limits used: iTunes 5 (original + lure, 2 variants); Google Play 3 (original + package, 2
  variants); GetApps 4 (original + package, 2 variants); Samsung 3 (original + package, 2 variants); Aptoide 4 (original +
  package, 3 variants).
- **Scrapers** call `pkg_queries(ctx)`: the brand name, plus the package's last segment unless it is generic. Most take
  the first two. Exceptions are listed below.
- **Uptodown** takes up to 5 original or lure terms that contain a brand word.
- **Bing** and **DuckDuckGo** build their own queries (`<brand> apk`, plus localized phrases for Bing) and add the
  expanded keywords that contain the brand.
- **APKPure, GitHub, GitLab** send the package's last segment (`app`) as a term. It is generic, so it is noisy.
- **F-Droid** sends no term. It matches the original and package keywords against its local index.
- **Lookalike domains** send no term. They generate domain candidates from the official domain with the same typo and
  homoglyph functions (typo up to 40, homoglyph up to 15 per label).

### Keywords the scan starts from (PhonePe: 44 terms)

- **original (1):** `PhonePe`
- **package (1):** `PhonePeApp`
- **lure (13, English only):** `PhonePe apk`, `PhonePe mod apk`, `PhonePe premium apk`, `PhonePe pro`, `PhonePe lite`,
  `PhonePe latest version`, `PhonePe free download`, `PhonePe old version`, `PhonePe cracked`,
  `PhonePe unlocked`, `PhonePe for pc`, `PhonePe hack`, `PhonePe cheats`
- **typo (5):** `honepe`, `phanepe`, `hponepe`, `pphonepe`, `ohonepe`
- **homoglyph (5):** lookalike-letter spellings of the brand
- **phonetic (1):** `fonepe`
  `com.phonepe.free`, `org.phonepe.app`, `io.phonepe.app`

**How a source picks its terms.** API stores use `brand_terms(...)`: up to `limit` original/package terms, plus up to
`variants` typo/homoglyph/phonetic terms. Scrapers use `pkg_queries`: the brand name, plus the package's last segment
when it is not generic.

**Method key:** API = the source's own JSON/XML endpoint. Scrape (HTTP) = plain HTTP and HTML parsing. Scrape (browser)
= Chromium renders the page and the HTML is read.

---

## Per-source details

Each source lists: the terms, the request pattern, the limit, the fields stored, and the recorded result.

### itunes — API, app_store
- **Terms:** `brand_terms` original + lure, limit 5, variants 2 (sample: `PhonePe`, `PhonePe apk`, `PhonePe mod apk`, `PhonePe premium apk`, `PhonePe pro`, `honepe`, `phanepe`).
- **Request:** `itunes.apple.com/search?term=<term>&media=software&entity=software&country=<cc>&limit=50`, plus a bundle lookup per country.
- **Limit:** 25 per query.
- **Fields:** name, package, publisher, developer id, version, description, icon (`artworkUrl512`, else `artworkUrl100`), URL.
- **Recorded:** 40 requests, 323 kept. The recording predates the icon change, so its icon count is 0.

### google_play — scrape (google-play-scraper library), app_store
- **Terms:** `brand_terms` original + package, limit 3, variants 2 (`PhonePe`, `PhonePeApp`, `honepe`, `phanepe`).
- **Request:** Play store search per term and country (`gl=us/in/gb/sg/ae`).
- **Limit:** 30 per term.
- **Fields:** name, package, publisher, version, description, icon, URL.
- **Recorded:** 21 search requests, 84 kept.

### getapps — API, app_store
- **Terms:** `brand_terms` original + package, limit 4, variants 2 (`PhonePe`, `PhonePeApp`, `honepe`, `phanepe`).
- **Request:** `global.app.mi.com/intl/web/api/search?keyword=<term>&lo=<CC>&la=en`, per country.
- **Limit:** 10 per query.
- **Fields:** name, package, publisher, icon. **No description or version**: the detail page renders no data in our browser.
- **Recorded:** 20 requests, 1 kept (brand filter).

### samsung_galaxy — API, app_store
- **Terms:** `brand_terms` original + package, limit 3, variants 2 (`PhonePe`, `PhonePeApp`, `honepe`, `phanepe`).
- **Request:** `galaxystore.samsung.com/storeserver/search?keyword=<term>&startNum=1&endNum=20` (XML). Then `galaxystore.samsung.com/api/detail/<package>` (JSON) for the first 10 hits.
- **Limit:** 10 per query.
- **Fields:** name, package, publisher, icon, version, description.
- **Recorded:** 4 requests, 0 kept. The reason is not confirmed; no search result passed the brand filter.

### huawei_appgallery — scrape (browser), app_store
- **Terms:** `PhonePe`, `PhonePeApp`.
- **Request:** `appgallery.huawei.com/search/<term>`.
- **Limit:** 10 per query.
- **Fields:** name, icon, version, URL.
- **Recorded:** 2 requests, 0 kept.

### indus_appstore — scrape (browser), app_store
- **Terms:** `PhonePe` and the package's last segment (first 2 `pkg_queries`).
- **Request:** `phonepeappstore.com/search/?ts=<term>`, then each matched app's detail page.
- **Limit:** 10 per query.
- **Fields:** name, package, publisher (from detail), description (from detail, "About the app" block), icon, URL.
- **Recorded:** 8 requests, 3 kept.

### taptap — scrape (browser), app_store
- **Terms:** `pkg_queries` (first 2).
- **Request:** `taptap.io/search/<term>`.
- **Limit:** 10 per query.
- **Fields:** name, icon, URL.
- **Recorded:** 1 request, 0 kept.

### fdroid — API (index file), app_store
- **Terms:** none sent. The adapter downloads `f-droid.org/repo/index-v2.json` and matches the brand against each app's name locally.
- **Limit:** 10.
- **Fields:** name, package, version.
- **Recorded:** 1 request (the index), 0 kept. The index is tens of MB, so this is the most expensive source per scan.

---

### apkkit — scrape (browser) + HTTP, apk_site
- **Terms:** `pkg_queries` (first 2: `PhonePe`, package segment if not generic).
- **Request:** `apkkit.com/en/home/search?q=<term>` (browser), then each hit's app page (plain HTTP, first 10).
- **Limit:** 20 per query.
- **Fields:** name (from page title), icon, version, publisher, URL.
- **Recorded:** 3 requests, 1 kept.

### apk4fun — scrape (browser) + HTTP, apk_site
- **Terms:** `pkg_queries` (first 2).
- **Request:** `apk4fun.com/s.php?s=<term>` (browser), then `apk4fun.com/apps/<package>/` for each hit with a package (plain HTTP, first 10).
- **Limit:** 20 per query.
- **Fields:** name, package, version, publisher, description. `/apk/<id>/` hits keep the search fields only.
- **Recorded:** 3 requests, 8 kept.

### apkpure — scrape (browser), apk_site
- **Terms:** `PhonePe` and `app` (the package's last segment, which is not filtered).
- **Request:** `apkpure.com/search?q=<term>`.
- **Limit:** 20 per query.
- **Fields:** name, icon, publisher, URL.
- **Recorded:** 2 requests, blocked (HTTP 403).

### apkfab — scrape (browser), apk_site
- **Terms:** `pkg_queries` (first 2).
- **Request:** `apkfab.com/search?q=<term>`.
- **Limit:** 20 per query.
- **Fields:** name, publisher, package (from URL), URL.
- **Recorded:** 2 requests, blocked (challenge page).

### apkboat — scrape (HTTP), apk_site
- **Terms:** `pkg_queries` (first 2).
- **Request:** `apkboat.io/?s=<term>`.
- **Limit:** 20 per query.
- **Fields:** name, URL.
- **Recorded:** 1 request, 0 kept.

### apkmirror — scrape (RSS), apk_site
- **Terms:** `pkg_queries` (first 2).
- **Request:** `apkmirror.com/feed/?searchtype=apk&s=<term>`.
- **Limit:** 20 per query.
- **Fields:** name, publisher, URL.
- **Recorded:** 1 request, 0 kept.

### softonic — scrape (browser), apk_site
- **Terms:** `pkg_queries` (first 2).
- **Request:** `en.softonic.com/s/<term>:android`.
- **Limit:** 20 per query.
- **Fields:** name, icon, URL.
- **Recorded:** 1 request, 0 kept (blocked in earlier runs, HTTP 406).

### uptodown — scrape (browser), apk_site
- **Terms:** up to 5 original/lure terms that contain a brand word (`PhonePe`, `PhonePe apk`, `PhonePe mod apk`, `PhonePe premium apk`, `PhonePe pro`).
- **Request:** `en.uptodown.com/android/search?query=<term>`, then up to 8 app pages per term.
- **Limit:** 20 per query.
- **Fields:** name, package, publisher, icon, version, description, URL.
- **Recorded:** 45 requests, 1 kept.

### aptoide_store — API, apk_site (registered)
- **Terms:** `brand_terms` original + package, limit 4, variants 3 (`PhonePe`, `PhonePeApp`, `honepe`, `phanepe`).
- **Request:** `ws2-cache.aptoide.com/api/7/apps/search?...&aab=1&language=en_US&query=<term>&limit=20`. Then `.../api/7/app/getMeta?...&package_name=<pkg>` for the first 10 hits.
- **Limit:** 20 per query.
- **Fields:** name, package, version, publisher, icon, description (from getMeta), apk link.
- **Recorded:** 8 requests, 3 kept.

---

### bing — scrape (browser), search_engine
- **Terms:** `PhonePe apk`, `PhonePe apk download`, plus localized phrases per country (for example IN: `PhonePe download kare`).
- **Request:** `bing.com/search?q=<term>&setlang=en&cc=<CC>`.
- **Limit:** 20 per query.
- **Fields:** page title, URL, snippet; icon and publisher where the page has them.
- **Recorded:** 38 requests, 72 kept.

### duckduckgo — scrape (browser), search_engine
- **Terms:** `PhonePe apk`, `app apk`, `PhonePeApp apk` (the `app` term is generic and is not filtered).
- **Request:** `html.duckduckgo.com/html/?q=<term>`.
- **Limit:** 20 per query.
- **Fields:** page title, URL, snippet.
- **Recorded:** 6 requests, 0 kept.

### lookalike_domains — DNS + HTTP, lookalike_domain
- **Terms:** none sent to a search engine. Up to 300 generated candidate domains (typo, homoglyph, TLD swaps, brand + keyword) are resolved, and up to 30 live home pages are fetched.
- **Limit:** 40.
- **Fields:** domain, page title, APK link, probe status.
- **Recorded:** 16 requests, 0 kept. Needs an official domain in the request.

---

### github — API, code_host
- **Terms:** `PhonePe apk`, `app apk` (the `app` term is generic).
- **Request:** `api.github.com/search/repositories?q=<term>&sort=updated&per_page=30`.
- **Limit:** 20 per query.
- **Fields:** repo name, URL, publisher (owner).
- **No brand filter.**
- **Recorded:** 2 requests, 31 kept.

### gitlab — API, code_host
- **Terms:** `PhonePe apk`, `app apk` (the `app` term is generic).
- **Request:** `gitlab.com/api/v4/projects?search=<term>&order_by=last_activity_at&sort=desc&per_page=20`.
- **Limit:** 20 per query.
- **Fields:** project name, URL, publisher (namespace).
- **No brand filter.**
- **Recorded:** 2 requests, 21 kept.

### telegram — scrape (HTTP), channel
- **Terms:** the brand plus guessed channel names (`phonepe`, `phonepeapk`, `phonepe_apk`, `phonepeapp`, `getphonepe`), and Lyzem searches (`PhonePe apk`, `PhonePe mod apk`).
- **Request:** `t.me/s/<channel>` and `lyzem.com/search?q=<term>`.
- **Limit:** 12 channels.
- **Fields:** title (`Telegram @<channel>: <text>`), URL, apk link.
- **Recorded:** 8 requests, 1 kept.

---

## Summary

| Source | Method | Terms sent | Limit | Requests | Kept |
|---|---|---|---|---|---|
| itunes | API | PhonePe + lure/variant terms, 5 countries | 25 | 40 | 323 |
| google_play | scrape (library) | PhonePe, PhonePeApp, honepe, phanepe | 30/term | 21 | 84 |
| getapps | API | PhonePe, PhonePeApp, honepe, phanepe, 5 countries | 10 | 20 | 1 |
| samsung_galaxy | API + detail API | PhonePe, PhonePeApp, honepe, phanepe | 10 | 4 | 0 |
| huawei_appgallery | scrape (browser) | PhonePe, PhonePeApp | 10 | 2 | 0 |
| indus_appstore | scrape (browser) + detail | PhonePe, package segment | 10 | 8 | 3 |
| taptap | scrape (browser) | PhonePe, package segment | 10 | 1 | 0 |
| fdroid | API (index file) | local match only | 10 | 1 | 0 |
| apkkit | scrape (browser) + HTTP | PhonePe, package segment | 20 | 3 | 1 |
| apk4fun | scrape (browser) + HTTP | PhonePe, package segment | 20 | 3 | 8 |
| apkpure | scrape (browser) | PhonePe, app | 20 | 2 | blocked (403) |
| apkfab | scrape (browser) | PhonePe, package segment | 20 | 2 | blocked (challenge) |
| apkboat | scrape (HTTP) | PhonePe, package segment | 20 | 1 | 0 |
| apkmirror | scrape (RSS) | PhonePe, package segment | 20 | 1 | 0 |
| softonic | scrape (browser) | PhonePe, package segment | 20 | 1 | 0 |
| uptodown | scrape (browser) + app pages | up to 5 original/lure terms | 20 | 45 | 1 |
| aptoide_store | API + getMeta | PhonePe, PhonePeApp, honepe, phanepe | 20 | 8 | 3 |
| bing | scrape (browser) | PhonePe apk, localized phrases | 20 | 38 | 72 |
| duckduckgo | scrape (browser) | PhonePe apk, app apk, PhonePeApp apk | 20 | 6 | 0 |
| lookalike_domains | DNS + HTTP | generated domains | 40 | 16 | 0 |
| github | API | PhonePe apk, app apk | 20 | 2 | 31 |
| gitlab | API | PhonePe apk, app apk | 20 | 2 | 21 |
| telegram | scrape (HTTP) | channel names + Lyzem searches | 12 channels | 8 | 1 |

## Known gaps

1. **Generic package segment as a search term.** APKPure (`app`), DuckDuckGo (`app apk`), GitHub (`app apk`) and GitLab (`app apk`) send it unfiltered.
2. **No brand filter** on GitHub, GitLab, Bing, DuckDuckGo, Telegram, APKPure, APKFab, APKBoat, APKMirror, Softonic, Taptap and Huawei. Their results go straight to scoring. GitHub, GitLab and Bing returned 31, 21 and 72 hits in the recorded run.
3. **Blocked:** APKFab (challenge page) and APKPure (HTTP 403).
4. **Zero kept in the recorded run:** Samsung, Huawei, Taptap, F-Droid, APKBoat, APKMirror, Softonic, DuckDuckGo, lookalike domains. The reasons were not investigated per source; this list is what the recording shows.
5. **GetApps:** no description or version.
6. **Bing:** the browser sometimes reads a page while it is still navigating (`Page.content: navigating` error).

## Location

`SOURCES.md` at the repository root (the generated `docs/` folder is in `.gitignore`). Nothing has been committed.
