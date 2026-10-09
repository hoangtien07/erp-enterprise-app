# A4a Runtime Evidence — red-test matrix run on the live G1′ site

**Date:** 2026-10-09 UTC
**Site under test:** `erp-platform-deploy` G1′ dev stack, `docker compose` project `ewcp-erp-dev`, site `ewcp-dev.localhost` reachable at `http://localhost:8080` (127.0.0.1 loopback, no TLS).
**Principal:** `ewcp-agent@ewcp.dev` — dedicated scoped user, role **Accounts User only** (not Administrator), User Permissions `Company→EWCP Dev Company A` (`apply_to_all_doctypes`) + `Customer→EWCP Dev Customer A1`, `apply_strict_user_permissions=1` (set by `scripts/seed_dev.py`).
**Auth:** `Authorization: token $ERPNEXT_API_KEY:$ERPNEXT_API_SECRET` from `secrets/ewcp-agent.env` (minted by seed_dev; secrets never stored in this repo).
**Fixtures:** Companies EDA/EDB, Customers A1/B1, Items EWCP-ITEM-001/002, SI `ACC-SINV-2026-00001` (A, submitted, outstanding 300 USD), PLE `c6njshn57g` (A). Added for R1/R2/R4 coverage via bench (admin-side): SI `ACC-SINV-2026-00002` (B, submitted, 500 USD) and draft PI `ACC-PINV-2026-00001` (A) with `ignore_pricing_rule=1` (permlevel-1 field, Accounts Manager only).

Convention: `auth_curl` = `curl -s -w '\nHTTP %{http_code}\n' -H "Authorization: token $ERPNEXT_API_KEY:$ERPNEXT_API_SECRET"`. Verdicts are for the *raw ERP surface*; what the kernel pack must normalize is stated per row.

---

## 1. §6 red-test matrix — results

| # | Case | Result | Verdict |
|---|---|---|---|
| R1 | `get_list` PLE filtered `company=B` (A-principal) | `[]` (200) on v2 and v1 | **PASS** — zero rows, no leak |
| R2 | `GET /api/v2/document/Sales Invoice/ACC-SINV-2026-00002` (B-owned) | 403 `PermissionError`, message **names the company**; nonexistent name → 404 `DoesNotExistError` | **PASS (data)** / **F10 oracle confirmed** — 403-vs-404 + message body reveal existence; bridge must normalize |
| R3 | bridge can't call `frappe.get_all` | n/a at runtime — enforced by pack construction (external HTTP client; pack-side lint is a kernel-CI concern, not site behavior) | **N/A→pack-side** |
| R4 | `Accounts Receivable` `company=B` via REST report runner | **200 with empty `result`** — NOT a deny | **FAIL (contract correction)** — see §2.1 |
| R5 | revoked token | immediate 401 `AuthenticationError` on next call | **PASS** |
| R6 | `X-Frappe-Request-Id` | valid hex/UUID marker → captured **and echoed back** in response header; `wr-<hex>` (kernel workrun_id shape) → **silently dropped** (monitor emits a random uuid instead) | **PASS with contract correction** — see §2.2 |
| R7 | permlevel-1 field `ignore_pricing_rule` on PI | **list `fields=` drops it**; **doc GET returns the key** but value masked to default (stored `1` → returns `0`) | **PARTIAL** — value safe, key presence leaks; see §2.3 |
| R8 | `limit` beyond cap | `limit=10000` honored (no server cap); `limit=-5` → **500 TypeError** with traceback body | **FAIL→pack obligation** — ERP has no cap nor validation shape; bridge must clamp + validate |

### 1.1 Raw evidence (verbatim)

**R1 — PLE list, company=B (v2):**
```
$ auth_curl ".../api/v2/document/Payment%20Ledger%20Entry?limit=10&filters=%7B%22company%22%3A%20%22EWCP%20Dev%20Company%20B%22%7D&fields=%5B%22name%22%2C%22company%22%2C%22party%22%2C%22amount%22%5D"
{"has_next_page":false,"data":[]}
HTTP 200
```
Same `[]` on v1 `/api/resource/Payment Ledger Entry?filters=[["company","=","EWCP Dev Company B"]]`.

**R2 — B-owned doc GET (v2):**
```
{"errors":[{"type":"PermissionError","exception":"…PermissionError","message":"You are not allowed to access this Sales Invoice record because it is linked to Company 'EWCP Dev Company B' in field Company"}]}
HTTP 403
```
Nonexistent: `{"errors":[{"type":"DoesNotExistError","message":"Sales Invoice ACC-SINV-9999-XXXX not found"}]}` HTTP 404. v1 surface: same 403 with the same existence-revealing `_server_messages`.

**R4 — AR report via REST runner:**
```
$ auth_curl ".../api/method/frappe.desk.query_report.run?report_name=Accounts%20Receivable&filters=%7B%22company%22%3A%22EWCP%20Dev%20Company%20B%22%7D"
{"message":{"result":[],…}}   HTTP 200  — empty rows, NOT a deny
```
Bench-side probe (same user via `query_report.run`): caller *without* `js_filters` skips `validate_filters_permissions` entirely; the empty result comes from the report's own `add_user_permission_filters` (accounts_receivable.py). Callers *with* `js_filters` containing `company=B` DO get `ValidationError` deny; a nonexistent company raises `DoesNotExistError` (existence oracle at filter level, F10 class).

**R5 — revoked token:** after `generate_keys` rotation on the user, old `key:secret` → `{"errors":[{"type":"AuthenticationError",…}]}` HTTP 401. No session caching.

**R6 — correlation:**
```
$ curl -i -H "X-Frappe-Request-Id: a4ab1e7c-273a-44a2-aa5c-68504abcd8a2" .../api/method/ping
HTTP/1.1 200 OK
X-Frappe-Request-Id: a4ab1e7c-273a-44a2-aa5c-68504abcd8a2      ← captured + echoed
$ curl -i -H "X-Frappe-Request-Id: wr-deadbeef1234" .../api/method/ping
HTTP/1.1 200 OK
X-Frappe-Request-Id: 64727196-9f0b-4b7e-b05a-8b568a1b09ee      ← dropped, replaced
```
`monitor.py` `TRACE_ID_PATTERN = [0-9a-fA-F-]{8,64}` fullmatch. Pickup requires `site_config.monitor` truthy (set via `bench set-config monitor 1`); entries accumulate in Redis list `monitor-transactions` (`redis-cache`) and flush to `logs/monitor.json.log` by `frappe.monitor.flush()`.

**R7 — field-level perm:**
```
$ auth_curl ".../api/v2/document/Purchase%20Invoice?fields=%5B%22name%22%2C%22supplier%22%2C%22ignore_pricing_rule%22%5D"
{"data":[{"name":"ACC-PINV-2026-00001","supplier":"EWCP Dev Supplier A1"}]}   ← field dropped from list
$ auth_curl ".../api/v2/document/Purchase%20Invoice/ACC-PINV-2026-00001"
{"data":{… "ignore_pricing_rule":0 …}}                                     ← key present, value masked
```
Bench confirm: `doc.get_permlevel_access()` = `[0]` for the scoped user; stored value is `1`; response shows `0` — the real value does NOT leak. [suy luận] Mechanism: `apply_fieldlevel_read_permissions` delattrs the attr, but `as_dict`/`get_valid_dict` re-emits the DocField key at its schema default — so the key survives while the value is masked.

**R8 — limits:**
```
limit=10000 → HTTP 200 (all rows honored — no cap)
limit=-5    → HTTP 500 TypeError "Limit must be a non-negative integer" (full traceback body — ugly but a reject)
```

**F9 — guest:** unauthenticated `GET` → 403 `PermissionError` ("User Guest does not have doctype access"). Guest never reaches data.

---

## 2. Contract corrections required by runtime evidence

### 2.1 R4 — report filter validation is opt-in, not default
`validate_filters_permissions` (`query_report.py:1189-1218`) iterates `report.filters + js_filters`; a REST caller that omits `js_filters` gets **no filter-value validation at all**. Cross-company reads of `Accounts Receivable` are still safe in practice — the report's own `add_user_permission_filters` zeroes B rows — but the contract's "deny at filter validation" claim is wrong for the REST path. **Correction:** the pass criterion is "no B data returned" (met); the pack MUST NOT rely on `validate_filters_permissions` and MUST scope its allowlisted report filters itself. Existence oracle exists at filter level: `DoesNotExistError` on unknown company.

### 2.2 R6/F8 — kernel `workrun_id` does not survive the monitor regex
`wr-<hex12>` fails `TRACE_ID_PATTERN` (non-hex `w`,`r`). **Correction:** the bridge MUST emit a separate `[0-9a-fA-F-]{8,64}`-compatible correlation id per call (uuid4().hex works and is echoed back), and record the workrun↔correlation mapping kernel-side in evidence. Raw workrun_id must NOT be sent as `X-Frappe-Request-Id`.

### 2.3 R7/F6 — doc GET leaks key presence, not values
permlevel-restricted fields are dropped from `fields=` list requests but appear in doc GET responses with masked/default values. **Correction:** field-level masking on doc GET is value-safe but NOT shape-safe — any contract text implying "field absent" must be relaxed to "value absent/masked". Packs returning specific fields should prefer list `fields=` where the drop IS clean.

### 2.4 R8/§1.4 — no server-side limit/validation
**Correction:** the read contract's row-cap is a *bridge obligation*. `limit` must be clamped and negative/zero validated client-side (pack does `min(max(limit,0),MAX=100)`); raw ERP returns 500-with-traceback on bad input rather than a stable schema error.

### 2.5 erp_meta — `get_versions` endpoint does not exist
`GET|POST /api/v2/doctype/<dt>/get_versions` → 404 `DoesNotExistError` in v16.51.0 (function exists only in frontend JS/print code). **Correction:** drop `get_versions` from erp_meta's surface or replace with a whitelisted method; `GET /api/v2/doctype/<dt>/meta` works (returns 234 fields for Sales Invoice).

---

## 3. FAC conditions F1–F10 — runtime verdicts

| # | Condition | Runtime verdict | Evidence |
|---|---|---|---|
| F1 | role `read`/`select` perms on allowlisted DocTypes | **PASS** | scoped user reads SI/PI/PLE/Customer/Item/Company on v1+v2 |
| F2 | Company scope via User Permission | **PASS** | R1 zero-rows; R2 403; seeded UP `Company→A` + `Customer→A1` effective on list+doc |
| F3 | `apply_strict_user_permissions` decision | **PASS** (enabled) | `apply_strict_user_permissions=1` set by seed_dev — the "config flip" is already the seed default; no runtime flip needed |
| F4 | no Administrator principal | **PASS** | UP scoping proves requests ran as `ewcp-agent@ewcp.dev` (Admin would see B rows); `whoami` confirms |
| F5 | bridge never calls `frappe.get_all`/raw db | **N/A by construction** | pack is an external HTTP client — no frappe imports; kernel-side lint belongs to kernel CI |
| F6 | field-level read perms | **PASS with caveat** | value masked (R7); key presence leaks — contract corrected §2.3 |
| F7 | report allowlist + filter validation | **PARTIAL** | `Accounts Receivable` scoped correctly at data level, but REST path skips `validate_filters_permissions` — allowlist must carry pack-side filter scoping (§2.1) |
| F8 | correlation header | **PASS with format fix** | pickup+echo confirmed under `monitor=1`; only `[0-9a-fA-F-]{8,64}` shapes survive (§2.2); redis/file flush path verified to redis list |
| F9 | guest denied | **PASS** | unauthenticated → 403 PermissionError both surfaces |
| F10 | deny must not reveal existence | **RESOLVED at bridge (P1) · residual at raw REST** | Bridge: `not_found_or_denied` 403 byte-identical for missing vs denied (A5B_BRIDGE.md §3.1). Raw `/api/resource`+`/api/v2/document` still oracle — residual; kernel pack normalizes client-side |

**Tally: pass 6 (F1,F2,F3,F4,F5-construction,F9) · conditional/corrected 3 (F6,F7,F8) · bridge obligation 1 (F10).**

## 4. Gap list updates (§5 of contract)

| # | Status |
|---|---|
| G1 | **RESOLVED** — UP enforcement live-verified (list+doc, link-field scope) |
| G2 | **RESOLVED** — strict mode is the seeded default; no escape observed on B data |
| G3 | **RESOLVED** — permlevel exposure measured: value-masked on doc GET, dropped on list fields (R7) |
| G4 | **RESOLVED with correction** — `validate_filters_permissions` is caller-opt-in (`js_filters`); AR internal UP filters do the real scoping |
| G5 | **PARTIAL** — stock doctype inventory confirmed for the touched set; no custom doctypes exist on this site yet |
| G6 | **RESOLVED** — api_key/secret generation works via seed (`bench`-level); token UX verified |
| G7 | **[pending]** — FAC-app not installed on G1′ (separate workstream) |
| G8 | **[pending]** — report latency/prepared-report paths not exercised (small seed data) |
| G9 | **PARTIAL** — pickup+echo verified; monitor flush path = redis list → `frappe.monitor.flush()` → `logs/monitor.json.log`; deploy still needs a flush schedule + log shipping |
| G10 | **RESOLVED** — v1 error shape `{"exception", "exc_type", "_server_messages"}`; v2 `{"errors":[{type,message}]}`; both give 403/404/500 with tracebacks — stable-enough classes: `unauthenticated`/`permission_denied`/`not_found`/`schema_invalid`/`upstream_error` |

## 5. Failure summary (honest FAILs)

- **R4** contract pass-criterion wrong for REST: report calls are not denied at filter validation when `js_filters` is omitted — they return scoped (empty) data.
- **R7** "field absent" criterion not met on doc GET: key remains, value masked to default.
- **R8** no server cap/validation: pack-side clamp is the contract.
- **F10** existence oracle confirmed at both doc level (403-vs-404 + company named in body) and filter level (`DoesNotExistError`).
- **erp_meta.get_versions** endpoint absent in v16.51.0 — contract surface needs revision.
- `monitor` must be enabled via `bench set-config monitor 1` for header pickup (now set on the dev site).

## 6. Kernel-side consequence (implemented in `enterprise-work-control-plane` PR)

The `erp_reads` pack (`side_effect_class="read_only"`) encodes §2 corrections: uuid-hex correlation ids (R6), `limit` clamp ≤100 (R8), 403/404 → `permission_denied`/`not_found` normalization hooks (F10), allowlists for doctypes + `Accounts Receivable` report (F7), env-only credentials with fail-closed `ErpConfigError`, evidence.json sealing `source_doc_ids`/`principal`/`erp_snapshot_time`/per-call correlation ids.
