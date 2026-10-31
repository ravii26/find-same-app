# API flow: what happens at each call

This file explains each API call in order: which file handles it, what that file checks first, what it
writes, and what runs next. It describes the code as it is now. For scoring, sources and configuration, see
`README.md`.

---

## 0. Words used here

| Word | Meaning |
|---|---|
| **brand record** | A JSON file at `data/brands/<brand_id>.json`. It holds the customer's input, the candidate apps found by discovery, and the apps the customer confirmed. |
| **candidate** | An app that discovery found for the brand. It is not trusted yet. |
| **confirmed** | A candidate the customer selected. Only confirmed items are used by a scan that has a `brand_id`. |
| **scan** | One run of the source search, scoring and report. It has a `scan_id` (starts with `scn_`). |
| **report** | The finished result: `data/results/<scan_id>/output.json`. |
| **Redis** | Holds the live state of a scan (queued, running, progress) and the queues. |
| **Dramatiq worker** | A separate process that runs discovery and scans in the background. |

Every API call except `/health` needs the header `X-API-Key: <your key>`. The local dev value is
`change-me-in-production`. The check is in `app/api/middleware/auth.py` (`require_api_key`).

---

## 1. The two flows

There are two parts. You can use either one alone.

```
A. Brand confirmation (brand record must already exist)

   POST /v1/brands/{brand_id}/confirm  (customer picks apps)
          │   brand record now has confirmed apps
          ▼

B. Scan

   POST /v1/scans  ──► (202 scan_id, status "queued")
          │   with brand_id: merges the confirmed apps into the request
          │   without brand_id: uses the ids sent in the body
          ▼
   worker runs the pipeline
          │
          ▼
   GET  /v1/progress/{scan_id}  ◄── poll until status is final
          │
          ▼
   report is on disk: data/results/<scan_id>/output.json
```

Discovery and scans never call each other. The scan endpoint reads the brand record once, when the request
arrives. It does not start discovery.

---

## 2. Brand onboarding

### Discovery and the discovery read endpoints are removed from the API

`POST /v1/brands/discover` and `GET /v1/brands/{brand_id}` were removed, so no new brand record is created
through the API. The discovery code (`app/services/discovery.py`) and its worker job (`discover_brand_actor`) are
still in the code, but nothing sends them. Confirm works only on brand records that already exist as files.

### Brand details (Elasticsearch): create, read, update, delete

These four endpoints store the brand's input in Elasticsearch, in the index `rogue-brands` (the prefix comes from
`ELASTICSEARCH_INDEX_PREFIX`). The body is the same as the former `POST /v1/brands/discover`, so the same rules apply.

| Call | File and function | What happens |
|---|---|---|
| `POST /v1/brand-details` | `app/api/routes/brand_details.py` → `create_brand_details()` → `brand_index.create()` | Makes a new `brd_` + ULID id, stores the details with `created_at` and `updated_at`, returns `201` with the document. |
| `GET /v1/brand-details/{brand_id}` | `read_brand_details()` → `brand_index.read()` | Returns the stored document, or `404`. |
| `PUT /v1/brand-details/{brand_id}` | `update_brand_details()` → `brand_index.update()` | Replaces the details, keeps `created_at`, sets `updated_at`. `404` if the brand is unknown. |
| `DELETE /v1/brand-details/{brand_id}` | `delete_brand_details()` → `brand_index.delete()` | Removes the document. `404` if the brand is unknown. |

If Elasticsearch cannot be reached, each call returns `503`. A malformed id is `404`, with no query sent.


After each scan with a `brand_id`, the worker keeps the brand's own apps that this scan allowlisted on the brand
record (`allowlisted_apps`). An app is kept only when it is listed on Google Play or the App Store, its id is one
the brand declares for that platform, and its publisher is known. Later scans with the same `brand_id` allowlist
those apps too. A copy on another store is never kept.

Brand details do no discovery. They only store the brand. The brand's own apps are allowlisted when a scan with
its `brand_id` runs: the scan reads each store link from its store, so the publisher is the one the store shows.

### 2.1 `POST /v1/brands/{brand_id}/confirm`

**Handled by:** `brands.py` → `confirm_brand()`, which calls `brand_store.confirm()`

**What it checks first, in order:**

1. API key check.
2. `404` if the brand is unknown.
3. `409` if `status` is not `done`. Discovery must finish first.
4. The body against `ConfirmRequest`: `confirmed_by` (required) and `selected`, a list of
   `{"store": "play" | "app_store", "id": "..."}`. At least one item.
5. `brand_store.confirm()` checks that each selected `(store, id)` is in the candidate list.
   Any other id gives `422` and nothing is saved.

**What it does:**

1. Adds the selected items to the confirmed list. Each item keeps its name, publisher, `confirmed_by` and
   `confirmed_at`.
2. Saves the record and returns `{brand_id, status, confirmed_at, confirmed}`.

Apps behind the customer's own links are confirmed when discovery writes the brand record. Confirm never removes
them. Confirming again adds to the list. Only the apps that came from a link are confirmed without a
customer step: other apps the developer or seller publishes still need to be selected here.

**Manual add is not built.** An id that is not in the candidate list is refused.

---

## 3. Scan

### 3.1 `POST /v1/scans`

**Handled by:** `app/api/routes/scans.py` → `create_scan()`

**What it checks first, in order:**

1. API key check.
2. The body against `ScanRequest` (`app/api/models.py`). `brand_name` may be left out only when `brand_id` is
   given. These fields are refused with `422`: `android_package`, `ios_bundle`, `countries`. Use the
   `android_packages` and `ios_bundles` lists.
3. If `brand_id` is set, the brand is read from the `rogue-brands` index (see 2.0): `404` if it is unknown, `503`
   if Elasticsearch cannot be reached. Then each store link the brand has is read from its store (Play or App
   Store). `503` if a store cannot be read, so no app is allowlisted on a guessed publisher.
4. The queue is full (`running + pending >= max_queue_depth`, default 100): `429` with `Retry-After: 60`.

**What it does:**

1. Keeps a copy of the body exactly as sent (`submitted`). This copy is reported as the scan's `query`.
2. If `brand_id` is set, `brand_index.merge_into_request()` adds the stored brand's aliases and official domains,
   and the apps behind its store links, read from the stores. Those apps are allowlisted for this scan: each
   adds its id on its platform and its publisher. The
   values sent in the body are kept and come first. The brand name is taken from the stored brand when the body
   has none.
3. Makes a scan id: `scn_` + a ULID.
4. **Dedup:** the request's `dedup_hash()` is computed. If an identical request is still queued or running,
   the API returns `202` with that existing scan's id and does not start a second scan.
5. Creates the folder `data/results/<scan_id>/`, and writes the first line of `scan.log`.
6. Sets Redis: status `queued`, meta (brand name, created time), and adds the request to the pending queue.
7. `promote_pending()` moves the scan into the running queue if a slot is free (`max_concurrent_scans`,
   default 4). It then sends the scan to the worker queue `scans`.
8. Returns `202` with `{"scan_id": "...", "message": "Job queued"}`.

**What runs next, in the worker:** `app/jobs/actors.py` → `scan_actor` → `_execute_scan()`

1. Status becomes `running`. Writes `started_at`.
2. Builds a `ScanRequest` again from the stored copy and calls `orchestrator.run_scan()`.
3. Writes the report to `data/results/<scan_id>/output.json`. Sets the final status.
4. Mirrors the report into Elasticsearch if it is enabled. Failures there only add a warning.
5. Frees the slot and starts the next pending scan.

**The pipeline** (`app/services/orchestrator.py` → `_pipeline()`). Each phase is shown in progress as `phase`:

| Order | Phase | What happens |
|---|---|---|
| 1 | (start) | Keywords are expanded (`keyword_expansion.expand`). |
| 2 | `collecting` | Every source adapter (`app/sources/`) runs concurrently, with at most 8 outbound requests at a time. Hits are collected. |
| 3 | `deduplicating` | Hits with the same normalized URL are merged, and so are hits with the same package id in the same store. Each merged hit keeps every source that found it. The same package id on Google Play and on an APK mirror stays as two hits. |
| 4 | `looking for offered apks` | Only if APK analysis is on. Off by default for now. |
| 5 | `scoring` | Each hit is scored (`app/services/scoring.py`). Name, package, domain, publisher and other signals add up to a score. Caps apply, for example when no brand presence is found. |
| 6 | (allowlist) | Hits that match a confirmed publisher plus a confirmed id on the same platform, or an official domain, are removed and listed in `allowlisted`. |
| 7 | `analyzing apks` | Only if APK analysis is on. Off now. |
| 8 | `capturing evidence` | Only if evidence capture is on. |
| 9 | (report) | `_build_report()` builds the report. Scans that hit the time limit (`scan_ceiling_seconds`) or raise an error still return a report, with `error` set. |

A source that fails or is blocked is recorded in `source_statuses`. It does not stop the scan.

### 3.2 `GET /v1/progress/{scan_id}`

**Handled by:** `scans.py` → `get_progress()`

**What it checks first:**

1. API key check.
2. `scan_id` format. A malformed id gives `404`.
3. Reads status from Redis. If Redis has no record (for example after a restart), it reads the report on
   disk. If neither exists, `404`.

**What it returns, by state:**

| `status` | What you get |
|---|---|
| `queued` | `queue_position`, `overall_progress: 0` |
| `running` | `phase` (the table above), `overall_progress` (sources done / sources total), `sources` summary and `source_detail` (each source done or pending) |
| `completed`, `completed_with_errors`, `failed` | `overall_progress: 100`, source counts, `finished_at`. The report itself is not returned. |

**The report is not returned by any API call.** Read it from disk:

```
data/results/<scan_id>/output.json
```

The log is in `data/results/<scan_id>/scan.log`. There is no `GET` route for the report yet.

### 3.3 `DELETE /v1/scans/{scan_id}`

**Handled by:** `scans.py` → `delete_scan()`

1. API key check, and the id format.
2. If the scan is `running`: `409`. A running scan cannot be removed.
3. Otherwise, deletes the scan's folder on disk, its Redis keys, and its Elasticsearch documents
   (if enabled). It also removes the scan from the queues.
4. A queued scan returns "Job removed from queue". A finished scan returns "Scan deleted".

### 3.4 `GET /health`

**Handled by:** `app/main.py`. No API key. Returns `{"status": "ok", "redis": "ok"}` when Redis answers.

---

## 4. Example order in Swagger

1. `GET /health`: check the API and Redis.
2. `POST /v1/brand-details`: create the brand. Copy the `brand_id` from the response.
3. `POST /v1/scans` with `{"brand_id": "..."}`: copy the `scan_id`.
4. `GET /v1/progress/{scan_id}`: repeat until the status is final.
5. Open `data/results/<scan_id>/output.json` on disk to read the report.

Steps 2 to 4 are optional. Without them, send the ids directly in the scan body (`android_packages`,
`ios_bundles`, and so on).

---

## 5. Known gaps

- **No endpoint returns the report.** Read `output.json` from disk.
- **The webhook is not sent.** There is a `webhook_timeout` setting and a `webhooks` queue, but no code
  sends a webhook. The `scans.py` comment that mentions a webhook is out of date.
- **Discovery is one-way.** A brand has no re-run endpoint. To refresh candidates, create a new brand.
- **Some sources fail** (APKPure, APKFab, Softonic, APKCombo, HappyMod, DuckDuckGo). Their status shows in
  `source_statuses` of the report.
- **Elasticsearch is not running**, so indexing is skipped. A scan still completes, with a warning.

---

## 6. Which file does what

| File | Role |
|---|---|
| `app/main.py` | Creates the app, mounts the routes, starts the embedded worker (queues `scans`, `webhooks`, `discovery`). |
| `app/api/middleware/auth.py` | API key check. |
| `app/api/models.py` | Request and report models. `ScanRequest` holds the input rules. |
| `app/api/routes/brands.py` | Brand confirm endpoint. Also holds `DiscoverRequest`, the body shared with brand details. |
| `app/api/routes/scans.py` | Scan endpoints: create, progress, delete. |
| `app/services/brand_store.py` | Reads and writes brand records. Merges confirmed apps into a scan request. |
| `app/services/discovery.py` | Finds candidate apps from the store links and the name search. |
| `app/jobs/actors.py` | Background jobs: discovery and scan execution, the queue slots. |
| `app/jobs/broker.py` | Sets up the Dramatiq broker (Redis). |
| `app/services/orchestrator.py` | The scan pipeline and the report. |
| `app/services/scoring.py` | Scores each hit. |
| `app/services/allowlist.py` | Removes the brand's own apps. |
| `app/sources/` | One adapter per source. |
| `app/storage/status_store.py` | Scan status, queues, progress in Redis. |
| `app/storage/result_store.py` | Reads and writes the report and the scan folder. |
| `app/utils/browser.py` | Headless browser helpers (developer page scrolling, rendered pages). |
