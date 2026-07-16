# 50+ Mobiel Home Assistant integration

Custom HA integration reporting 50PlusMobiel (Dutch MVNO) account status —
remaining data bundle (MB and %), bundle size, days/date until the bundle
refreshes, contract end date, minutes/SMS — for one or more personal
accounts. Distributed via HACS.

## Current state: full sensor set implemented, test-deployed and working

`custom_components/mobiel50plus/` has the full standard HA integration shape
(manifest, config flow, `DataUpdateCoordinator`, sensor platform), and
`api.py`'s `async_login()` / `async_get_status()` call the real
`mijn.50plusmobiel.nl` API (see "The real API" below) — verified working
end-to-end against a real account, both outside of HA and deployed on a real
HA instance. The integration ships an English source language
(`strings.json` / `translations/en.json`) plus a Dutch translation
(`translations/nl.json`) — entity names use `translation_key` +
`has_entity_name` so they follow the user's HA language setting rather than
being hardcoded in English.

### Why: the portal is a JS SPA, not scrapeable HTML

A plain HTTP GET to `mijn.50plusmobiel.nl` returns an empty HTML shell — all
content is rendered client-side. That rules out the "GET the page + BeautifulSoup"
scraping pattern some HACS integrations use, since there'd be nothing in the
raw response to parse.

Running a real headless browser (Playwright/Selenium) *inside* the HA
integration at runtime was also considered and rejected: HA's custom-component
`requirements:` mechanism pip-installs pure-Python packages into the running
container, it doesn't provision Chromium/browser binaries, and even if it
could, spinning up a browser per poll cycle is heavy for what should be a
lightweight periodic sensor poll.

**Conclusion: find the JSON API the SPA calls, and poll that directly** with
plain `aiohttp` — the same approach almost all "unofficial API" HACS
integrations use (log in once, get a session/token, hit JSON endpoints on an
interval).

### The real API

Captured via devtools (Playwright) against a real account and confirmed
against `api.py`'s live implementation:

- **Login**: `POST https://mijn.50plusmobiel.nl/token/login`, body
  `{"username": ..., "password": ...}`. Always returns HTTP 200 — success
  is signalled by an `access_token` field (a JWT good for ~2 years per
  `expires_in`), failure by `{"error": "invalid_grant", "error_description":
  "The user credentials were incorrect.", "token": null}` with no token.
  The status code does *not* distinguish success/failure — `api.py` checks
  for the presence of `access_token`, not the HTTP status.
- **Status**: `POST https://mijn.50plusmobiel.nl/api/graphql`, header
  `Authorization: Bearer <access_token>`, GraphQL query
  `getCustomerForMsisdn` (see `api.py`'s `STATUS_QUERY`). Response shape:
  `data.me.subscriptionGroups[].msisdns[].balance` with fields
  `dataAvailable`/`dataAssigned` (remaining/total, in MB) and
  `voiceAvailable`/`smsAvailable` (minutes/SMS remaining — `null` on
  unlimited calling/SMS plans; treat `null` as "unlimited", not an error).
- **The GraphQL endpoint has introspection enabled** (`{__schema{...}}`
  works with just a valid bearer token, no special access needed) — used to
  find fields the SPA's own query doesn't request, confirmed by querying
  them live against a real account:
  - `Balance.dataPercentage` — remaining-data percentage, computed
    server-side (matched `dataAvailable/dataAssigned` on the probed
    account: 9433/12000 ≈ 79%). Used directly instead of computing it
    client-side.
  - `SubscriptionGroup.remainingBeforeBill` — integer days until the data
    bundle resets. Confirmed against the probed account: value `18`,
    `activeSubscriptionGroupBundle.startDate` day-of-month `3`, and
    today+18 days lands on day-of-month `3` — consistent with a monthly
    billing-cycle reset. `api.py` derives `bundle_refresh_date` as
    `date.today() + timedelta(days=remainingBeforeBill)` rather than
    exposing a separate API-provided date field (none was found).
  - `SubscriptionGroup.activeContract.endDate` — contract end date
    (`"YYYY-MM-DD"` string, no time component, unlike other date fields on
    this schema which are full ISO timestamps). Can be `null` if the
    account has no `activeContract` — treated as "unknown", not an error.
  - `SubscriptionGroup.nextRenewedSubscriptionDate` was also considered for
    "next bundle date" but came back `null` on the probed account
    (`canRenew: false`) — it appears to track *contract renewal*, not the
    monthly data reset, so `remainingBeforeBill` was used instead.
- The SPA's login *form* actually drives a different, two-step
  `POST /verifyLogin` endpoint (email step, then password step) with a
  reCAPTCHA response slot — but that turned out to be a pure UI affordance.
  `/token/login` is stateless and works standalone with just
  username+password, no cookies or prior `verifyLogin` call needed, so
  `api.py` skips the `verifyLogin`/reCAPTCHA dance entirely.
- `getCustomerForMsisdn` was called with an explicit `selectedMsisdn`
  variable in the SPA (for accounts with multiple numbers/delegated
  access), but omitting that variable returns the account's own number(s)
  by default — one round-trip instead of two (`api.py` doesn't call the
  separate `msisdns` query the SPA also uses).

## Design decisions

- **Domain name is `mobiel50plus`, not `50plusmobiel`** — HA integration
  domains are Python package names and can't start with a digit. Display
  name "50+ Mobiel" (set in `manifest.json`) is what actually shows in the UI.
- **Multi-account = multiple config entries.** No custom multi-account data
  model in code — HA's native pattern (add the same integration again with
  different credentials) covers tracking several accounts. `config_flow.py`
  sets `unique_id` to the lowercased username specifically so the same
  account can't be added twice, while different accounts can each get their
  own entry.
- **Entity names are translated, not hardcoded.** `sensor.py` sets
  `translation_key` per `SensorEntityDescription` and `has_entity_name =
  True` instead of a literal `name=`, with the actual strings living in
  `strings.json` (source/English) and `translations/{en,nl}.json`. This is
  the standard HA i18n pattern — it's what lets entity names follow the
  user's configured HA language instead of always showing English.
- **HACS custom repository install now works.** A tagged GitHub release
  exists and CI (HACS validation + hassfest) is green, so HACS can install
  this as a custom repository. Default-store inclusion (searchable in HACS
  without adding a custom repository URL first) is the remaining step — see
  README's "HACS registration" section.
- **Polling interval defaults to 30 minutes** (`const.py`,
  `DEFAULT_SCAN_INTERVAL`) — a conservative default for an unofficial,
  reverse-engineered API; tighten only if 50+ Mobiel's portal turns out to
  tolerate more frequent polling without rate-limiting/blocking. All API
  requests also carry a 30s `aiohttp.ClientTimeout` (`api.py`'s
  `REQUEST_TIMEOUT`) so a stalled portal can't hang a poll cycle
  indefinitely, and `coordinator.py` catches `aiohttp.ClientError`/
  `asyncio.TimeoutError` as `UpdateFailed` rather than letting them surface
  as raw uncaught exceptions.
- **Icon is an original mark, not 50+ Mobiel's logo.**
  `custom_components/mobiel50plus/brand/icon.svg` (+ rasterized
  `icon.png`/`icon@2x.png`/`logo.png`/`logo@2x.png`) is a phone/signal/
  data-gauge glyph in a muted burnt-amber deliberately shifted away from
  50+ Mobiel's actual brand orange (`#EE7C00`, confirmed from their public
  marketing site's CSS) — evokes "mobile account monitoring" without
  copying their actual logo, so it reads as related-but-distinct/unofficial.
- **Icon lives in `custom_components/mobiel50plus/brand/`, not a
  `home-assistant/brands` PR.** A custom integration shipping its own
  top-level `brand/` folder (singular, inside the integration package) with
  `icon.png`/`icon@2x.png`/`logo.png`/`logo@2x.png` gets served directly by
  HA's own `/api/brands/integration/` endpoint on recent HA versions
  (`homeassistant/components/brands/__init__.py`'s
  `_serve_from_custom_integration`, gated on `Integration.has_branding` in
  `loader.py`) — no CDN fetch, no `home-assistant/brands` PR needed. If a
  future HA version changes this, a `home-assistant/brands` submission is
  the fallback path, but isn't needed now.

## Development notes

This integration was developed with AI assistance and has been tested and
confirmed working end-to-end by a human, against a real 50+ Mobiel account
and a real Home Assistant instance. Deploy/test tooling tied to any specific
person's own infrastructure is intentionally kept out of this repository.
