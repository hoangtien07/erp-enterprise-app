# A4a — ERP Read Contract & FAC Verdict Draft

**Status:** draft contract (site-independent phase of WP-A4a per `docs/program/MASTER_EXECUTION_PLAN.md` §3). The G1 ERPNext site is `BLOCKED_ENV` — nothing here asserts observed runtime behavior; every claim is either a `file:line` citation from the pinned source trees below or is labeled `[giả định]` / `[suy luận]`.

**Pins verified on this box (2026-10-09):**

| Tree | Ref | Commit | Version |
|---|---|---|---|
| `hoangtien07/erpnext` (upstream mirror, no product divergence) | `version-16` | `7474d9e` | v16.50.0 |
| `frappe/frappe` (not org-forked; cloned upstream) | `version-16` | `6b450a1` | v16.51.0 |
| `hoangtien07/enterprise-work-control-plane` | `main` | `2147443` | — |

Kernel-side contract target: `docs/program/plans/2026-10-09-A5a-capability-contract.md` (the `CapabilityDescriptor` shape the invoke path will implement). Prior audits this doc builds on, not re-derives: `docs/reviews/erpnext-council-2026-10/W2-erpnext-capability-audit.md`, `…/FAC-audit.md`.

**Terminology — two different "FAC"s are in play:**
- **FAC = Frappe Authorization Contract** (this document, §3): the conditions an ERP user/principal must satisfy for governed reads to pass. New contract authored here.
- **FAC-app = Frappe Assistant Core** (`buildswithpaul/Frappe_Assistant_Core` v3.0.0, AGPL): the third-party MCP transport audited in `FAC-audit.md`. The WP-A4a "FAC runtime decision" (C04: REUSE vs SKIP) is drafted in §4 and stays **pending-G1** for the runtime half.

---

## 1. Read-operation surface — verified endpoint behavior

All read paths the A4a read pack and the bridge will consume. Unless marked `[giả định]`, every row cites source read on the pins above.

### 1.1 Transport and routing

| Surface | Route | Verified at | Behavior that matters to A4a |
|---|---|---|---|
| API mount map | `/api/*` (v1), `/api/v1/*` (v1), `/api/v2/*` (v2) | `frappe/api/__init__.py:88-101` | One `API_URL_MAP`; v1 rules mounted at both `/api` and `/api/v1` |
| REST v1 list | `GET /api/resource/<doctype>` | `frappe/api/v1.py:144-152` (`url_rules`), handler `document_list` `v1.py:12-31` | `fields`/`expand` are JSON strings; `limit_page_length` defaults to `limit` or `20`; delegates to `frappe.client.get_list` |
| REST v1 doc | `GET /api/resource/<doctype>/<name>` | `v1.py:74-89` `read_doc` | `doc.check_permission("read")` + `doc.apply_fieldlevel_read_permissions()` on every doc read |
| REST v1 doc-method | `POST /api/resource/<doctype>/<name>` | `v1.py:115-127` `execute_doc_method` | `doc.is_whitelisted(method)` required; GET → `check_permission("read")`, POST → `check_permission("write")` |
| REST v2 list | `GET /api/v2/document/<doctype>` | `v2.py:78-160` `document_list` | `fields`/`filters` JSON; `start`+`limit` (default 20); explicit `ignore_permissions=False`; fetches `limit+1` and sets `frappe.response["has_next_page"]`; per-doctype controller `get_list(query)` hook exists |
| REST v2 doc | `GET /api/v2/document/<doctype>/<name>` | `v2.py:64-76` `read_doc` | same `check_permission("read")` + field-level filtering as v1 |
| REST v2 meta/count | `GET /api/v2/doctype/<doctype>/{meta,count}` | `v2.py:216` (`only_for("All")`), `v2.py:163` | meta endpoint is readable by role `All` — safe discovery primitive |
| RPC dispatch | `/api/method/<dotted.method>` and `/api/method/<doctype>/<method>` | `frappe/handler.py:66-84` `execute_cmd`; v2 `v2.py:28` `handle_rpc_call` | method resolved via Server-Script `_api` map first, then `get_attr`; `is_whitelisted` + `is_valid_http_method` enforced before `frappe.call(method, **form_dict)` |
| Whitelist gate | `@frappe.whitelist()` | `frappe/__init__.py:439-473` | default methods `GET/POST/PUT/DELETE`; `allow_guest` opt-in only; registered in `whitelisted` + `allowed_http_methods_for_whitelisted_func` |
| Whitelisted client ops | `frappe.client.{get_list,get,get_value,get_count,...}` | `frappe/client.py:27` (`get_list`), `:95` (`get`), `:114` (`get_value`), `:79` (`get_count`) | `get_list` params: `fields`, `filters`, `or_filters`, `group_by`, `order_by`, `limit_start`, `limit_page_length` (default 20), `parent`, `expand`; input hygiene via `validate_args` (`frappe/desk/reportview.py:134-145`) + `get_safe_filters` (`frappe/utils/__init__.py:898-908`) |
| Report run | `POST /api/method/frappe.desk.query_report.run` | `frappe/desk/query_report.py:249-270` (`@frappe.whitelist()` + `@frappe.read_only()`), `_run` `:276-330` | `validate_filters_permissions` (`:1189-1218`) then `get_report_doc` (`:25-60`) then `generate_report_result` |
| Version detect | `GET /api/method/frappe.utils.change_log.get_versions` | `frappe/utils/change_log.py:104-105` | whitelisted; usable for evidence stamping |
| Health | `GET /api/method/ping` | `v2.py:278` | guest-OK liveness |
| Request correlation | `X-Frappe-Request-Id` header | `frappe/monitor.py:81-84` | captured into request monitor data — kernel SHOULD send `workrun_id`/`invocation_id` here for audit join |
| API audit | `API Request Log` DocType | `frappe/api/__init__.py:46-53` | written when System Settings `log_api_requests` is on — a deploy-time toggle, not per-request |
| Rate limit | site config `rate_limit.limit`/`window` | `frappe/rate_limiter.py:16-18,48-49` | 429 enforcement is per-site config; contract assumes it configured at deploy |
| Txn semantics | 1 HTTP request = 1 DB txn | `frappe/app.py:461-468` | commit only for `UNSAFE_HTTP_METHODS` or `flags.commit`; **GET always rolls back** — reads can never mutate |

### 1.2 Where permissions are actually checked (v16 QB engine)

`frappe.get_list` (`frappe/__init__.py:1363-1384`) → `frappe/model/qb_query.py:18` `DatabaseQuery.execute` → `frappe.qb.get_query` (`frappe/database/query.py:215`). The QB engine is where list reads are scoped — this changed shape vs `db_query.py` (which still exists for the compat path); **cite `query.py`, not `db_query.py`, for v16 behavior**:

| Check | Code | Meaning for the contract |
|---|---|---|
| Top gate | `query.py:259-260` `self.apply_permissions = not ignore_permissions` → `check_select_permission` (`:1385-1390`: `has_permission(doctype,"select")` else `_raise_permission_error` `:1392-1397`) | A caller with neither `read` nor `select` role perm gets `PermissionError`, not an empty list |
| Row scope | `add_permission_conditions` (`query.py:1540-1583`) → `get_permission_conditions` (`:1585-1620`) | no read/select → shared-docs-only or throw; else `if_owner` constraint OR user-permission conditions AND permission_query_conditions; DocShare is OR'd in last (shared docs trump restrictions) |
| User Permissions on link fields | `get_user_permission_conditions` (`query.py:1485-1528`) | every Link field of the doctype (including `name` itself) is matched against the user's `User Permission` allowlist; `apply_strict_user_permissions` System Setting controls whether empty-valued links still match |
| Hook injection | `get_permission_query_conditions` (`query.py:1633-1661`) | `permission_query_conditions` hooks per doctype + `"*"` + `permission_query` Server Scripts — this is the bridge's injection point (`erp_enterprise_app/hooks.py` has the commented stub) |
| Field level on lists | `apply_field_permissions` (`query.py:1399-1407`, under `apply_fields`) | `permlevel` fields a role can't read are dropped from list output too, not just doc reads |
| Field level on docs | `document.py:959-1046` `apply_fieldlevel_read_permissions` + `get_permlevel_access` (`:1048-1056`) | invoked by `read_doc` on both v1 and v2 |

Single-doc path: `has_permission` (`frappe/permissions.py:81-219`) → role perms (`get_doc_permissions`/`get_role_permissions` `:284-319`) → DocShare fallback (`false_if_not_shared` `:176`) → `select` implied by `read` (`:206-217`) → `has_user_permission` (`:353-480`) which walks **all link fields of the doc and its child rows** via `check_user_permission_on_link_fields` (`:418-477`). `Administrator` short-circuits to allow at `permissions.py:108-109` — see §3.4.

**Bypasses the contract must ban in bridge code** (verified):
- `frappe.get_all` (`__init__.py:1386-1407`) = `get_list` with `ignore_permissions=True` forced and `limit_page_length=0` default — any bridge-internal use is an authorization bypass.
- `frappe.db.get_value`, `doc.get()` — no per-record check; use only on docs already permission-gated.
- Raw SQL / `frappe.db.sql` in bridge endpoints — outside the permission engine entirely.

### 1.3 ERPNext-side facts (erpnext mirror, verified)

| Fact | Evidence | Consequence |
|---|---|---|
| No `permission_query_conditions` registered by ERPNext | grep of `erpnext/hooks.py` (only `has_permission: erpnext.check_app_permission` `:20`, `has_website_permission` `:339-349`, `doc_events` `:377`) | Company row-scope is NOT enforced by stock ERPNext — it comes from `User Permission` records (link-field match) or a bridge hook we register |
| App-level gate is thin | `erpnext/__init__.py:158-166` `check_app_permission` | denies only website users; a System User with roles passes |
| ERPNext's own Company-scope convention | `erpnext/setup/doctype/employee/employee.py:197-208` `update_user_permissions` | `add_user_permission("Employee", …)` + `add_user_permission("Company", …)` — User Permissions on `Company` are the upstream-endorsed scoping mechanism |
| Receivables ledger = `Payment Ledger Entry` | `erpnext/accounts/doctype/payment_ledger_entry/payment_ledger_entry.json` | plain (non-submittable) DocType; `read` for roles `Accounts User`, `Accounts Manager`, `Auditor`; fields incl. `company`, `party_type`, `party`, `voucher_type/no`, `against_voucher_type/no`, `amount`, `amount_in_account_currency`, `account_currency`, `posting_date`, `due_date`, `delinked`, `cost_center` → readable via `get_list` with `company`+`party` filters |
| `Sales Invoice` fields + perms | `erpnext/accounts/doctype/sales_invoice/sales_invoice.json` | submittable; fields incl. `company`, `customer`, `customer_name`, `posting_date`, `due_date`, `grand_total`, `outstanding_amount`, `paid_amount`, `status`, `debit_to`, `currency`, `party_account_currency`; `read`/`write`/`report` for Accounts Manager+User, `read` for `All` |
| `Customer` has **no** `company` field | `erpnext/selling/doctype/customer/customer.json` | parties are global across companies — Company scope on a customer comes only via transaction-level `company` fields (PLE/SI) or explicit User Permission on `Customer`. Reads of a Customer record itself are NOT company-scoped — the contract must say so |
| Trusted report exists | `erpnext/accounts/report/accounts_receivable/accounts_receivable.json` | **Script Report** "Accounts Receivable", `ref_doctype: "Sales Invoice"`, role-gated; queries PLE via raw `qb.from_(ple)` (`accounts_receivable.py:860-870`) — i.e., report rows are whatever its SQL selects; the gates are Report-role + `report` perm on `Sales Invoice` + `validate_filters_permissions` on Link filters (§3.3) |

### 1.4 Pagination & parameter conventions for the contract

- v1 list: `limit_start` + `limit_page_length` (or `limit`). v2 list: `start` + `limit` + `has_next_page` in response. qb engine deprecates `limit_start`/`limit_page_length` → `offset`/`limit` (`deprecation_warning` v17, `qb_query.py:155-178`). **Contract: bridge endpoints expose `offset`+`limit` (bounded, default 50, hard cap 500 [giả định — cap value to be tuned at impl]) and translate internally.**
- `filters` accept both dict form and `[["doctype","field","op","value"]]`/`[[field,op,value]]` list form; `get_safe_filters` parses JSON strings (`utils/__init__.py:898`). Contract pins the list form with an operator allowlist (`=`, `!=`, `>`, `<`, `>=`, `<=`, `in`, `between`, `like`) — no `or_filters` from callers in A4a [giả định].
- `fields` is a JSON array; `*` allowed by core but the bridge MUST pin explicit field lists per endpoint (least-data principle).

---

## 2. Kernel contract mapping (OutcomeSpec / CapabilityDescriptor)

Per A5a plan §3 the descriptor carries: `capability_id`, `contract_version`, typed input/output schema, grant requirements, `timeout_s`, idempotency behavior, `side_effect_class`, evidence/source metadata. Mapping for the ERP read family:

| Capability (proposed `outcome_type`) | ERP path consumed | Typed inputs | Output (evidence artifacts) | side_effect_class | Idempotency |
|---|---|---|---|---|---|
| `erp_read_doc` (per-name fetch) | `GET /api/v2/document/<dt>/<name>` (v2) — or v1 `read_doc` | `doctype` ∈ allowlist, `name`, `fields` allowlist | `doc` (post-field-filter), `source_doc_id`, `principal`, `fetched_at` | `read_only` | reads are naturally idempotent; kernel `Idempotency-Key` replay still applies at invoke layer (A5a §3.2) |
| `erp_read_list` (bounded list) | `GET /api/v2/document/<dt>` or `frappe.client.get_list` | `doctype` ∈ allowlist, `filters` (allowlisted ops), `fields` allowlist, `offset`, `limit`, `order_by` | `rows[]`, `has_next_page` (v2) / count, `source_doc_ids[]`, `principal`, `queried_at` | `read_only` | same |
| `erp_receivables_lookup` (GP-02 pack) | PLE `get_list` + SI `get_list` + named Script Report (`Accounts Receivable`) as trusted second view | `company`, `customer`, `as_of_date`, `max_rows` | `rows[]` (per source_doc_id), `totals` (per currency), `company`, `principal`, `erp_snapshot_time` (server `now` at query — [giả định] no snapshot API exists; use response timestamp + report `report_date`), `reconciliation` (`match|mismatch|unverified_source` vs the named report) | `read_only` | same |
| `erp_meta` (discovery) | `GET /api/v2/doctype/<dt>/meta` + `get_versions` | `doctype` | `meta` (permitted fields), `versions` | `read_only` | same |

**Field notes:**
- `side_effect_class`: A5a's `OutcomeSpec.side_effect_class` currently defaults to `workspace_write` and rejects `external_write` at `register()`. `read_only` is the honest class for this family — the kernel enum/registry must accept it (one-line kernel change; flag in PR description, non-ADR per A5a §7 reasoning). `[giả định]` on enum name landing exactly as `read_only`.
- Grant requirements: each capability declares `required_scopes` derived per `deterministic.py:36-39` convention (kernel-side); the scope string binds `doctype`/`report` allowlists and `company` claims — grants stay kernel-minted, never caller-supplied (A5a §6).
- Correlation: kernel → ERP requests MUST propagate `workrun_id`/`invocation_id` as `X-Frappe-Request-Id` (monitor pickup verified at `monitor.py:81-84`) so ERP-side logs join the EWCP ledger; and `X-Ewcp-Actor` semantics per A5a §8 Q5 stay kernel-internal.
- Errors map to the A5a structured envelope: ERP `PermissionError`/403 → `permission_denied`; missing doc → `not_found`; schema-invalid filters → `schema_invalid`; unreachable ERP → `upstream_unavailable` `[giả định]` (code names follow the invoke-route convention, finalized at impl).
- Timeout: `timeout_s` declared-only (A5a non-goal) — contract recommends client-side deadline ≥ report `execution_time` p95; measure on G1.
- No `external_write` capability is defined here; governed writes are WP-A5b and MUST NOT reuse these endpoints.

---

## 3. FAC verdict draft — Frappe Authorization Contract (read path)

What an ERP principal must be able to do — and must NOT be able to do — for governed reads to pass. "Verifiable now" = decidable from source/pins without a live site; `[pending-G1]` = needs the real site.

### 3.1 Principal shapes

| Shape | Verified mechanics | Verdict line |
|---|---|---|
| Scoped service account (M2M) | `Authorization: token <api_key>:<api_secret>` → `validate_auth_via_api_keys` (`auth.py:695-719`) → `validate_api_key_secret` (`:721-745`): `api_key` lookup with `enabled=true`, decrypted `api_secret` compare, `frappe.set_user(user)` | **Supported.** One dedicated User, `api_key`/`api_secret` generated per site. Token carries the user's full permission surface — there is **no per-token scope narrowing** (`auth.py:721-745` checks key/enabled/secret only) → least privilege is entirely a function of that User's roles + User Permissions |
| Delegated user (end-user acts as self) | OAuth2 Bearer → `OAuth Bearer Token` `scopes` + `verify_request` → `set_user(token.user)` (`auth.py:670-692`); or the user's own api_key pair | **Supported.** Per-user audit + the cleanest story for "user can only read what they could read in Desk". Cost: per-user token lifecycle [pending-G1 for actual OAuth client setup] |
| Session cookie + CSRF | `auth.py:81-90` `validate_csrf_token` | **Not used** by bridge/M2M — token auth only |

### 3.2 Required conditions (the contract itself)

| # | Condition | Mechanism | Verifiable now? |
|---|---|---|---|
| F1 | Principal has `read` (or `select`) role perm on each allowlisted DocType | DocPerm role matrix; enforced at `check_select_permission` (`query.py:1385-1390`) and `has_permission` (`permissions.py:81`) | Yes — perms are in DocType JSON (`sales_invoice.json`, `payment_ledger_entry.json` show `Accounts User`/`Auditor` read) — role choice for bridge user is `[pending-G1]` config |
| F2 | Company scope is expressed as `User Permission` records on `Company` | link-field match in list (`query.py:1485-1528`) and doc (`permissions.py:418-477`) paths; ERPNext's own convention (`employee.py:197-208`) | Mechanism verified; actual allowlist + `applicable_for`/`hide_descendants` semantics on seeded companies `[pending-G1]` |
| F3 | `apply_strict_user_permissions` decision recorded | `permissions.py:368-380`, `query.py` (strict → no empty-link escape) | Setting is per-site System Setting `[pending-G1]`; contract RECOMMENDS strict=1 for the bridge user (non-strict lets docs with empty link fields through) |
| F4 | No `Administrator` principal | `permissions.py:108-109` short-circuit | Verifiable at config review `[pending-G1]`; also `Administrator` bypasses `check_app_permission` |
| F5 | Bridge denies `frappe.get_all`/`db.sql`/raw doc fetch in its own code | `__init__.py:1386-1407` | Code review + lint rule in bridge repo (pre-impl, enforceable now) |
| F6 | Field-level read perms pass through unchanged | `apply_fieldlevel_read_permissions` (`document.py:959`), `apply_field_permissions` (`query.py:1399`) | Verified — contract inherits; any field the bridge must return must sit at a permlevel the bridge role can read `[pending-G1]` for concrete permlevel audit on seeded DocTypes |
| F7 | Report calls limited to a named allowlist | `get_report_doc` gates: `Report.is_permitted` role list (`report.py:144-156`) + `has_permission(ref_doctype,"report")` (`query_report.py:50`,`:290`) + `validate_filters_permissions` on Link filters (`:1189-1218`) | Verified mechanics; the allowlist itself (`Accounts Receivable` first) `[pending-G1]` — Script Report SQL runs outside the permission engine (`accounts_receivable.py:860-870` raw `qb.from_`), so each report's SQL needs a scope review before allowlisting |
| F8 | Correlation header propagated | `monitor.py:81-84` | Verified pickup exists; end-to-end join `[pending-G1]` |
| F9 | Guest access impossible for bridge routes | `whitelist(allow_guest=False)` default (`__init__.py:439-473`); `handle_mcp`-style `allow_guest` endpoints are NOT our paths | Verified for chosen endpoints; must hold when bridge methods are authored (never `allow_guest`) |
| F10 | Deny must not reveal existence | `check_select_permission` throws before rows return; `has_permission(doc=...)` 403s on direct hit | Partially — cross-company direct-doc fetch returns PermissionError (existence revealed by 403 vs 404 distinction). Contract: bridge normalizes both to `permission_denied` without row data `[giả định — response-shape decision]` |

### 3.3 What the contract does NOT rely on (verified absences)

- No `permission_query_conditions` from ERPNext (`erpnext/hooks.py` — none). Company row-scope on stock v16 = User Permissions only; the bridge MAY register its own hook later (`erp_enterprise_app/hooks.py` stub + `query.py:1633-1661`), but A4a MUST work without it — hook is additive hardening, not load-bearing.
- No idempotency/duplicate suppression anywhere on reads — not needed (reads don't mutate; `app.py:461-468` GET→rollback).
- No per-token scoping — see §3.1.

### 3.4 Draft verdict

**PASS-conditional:** a dedicated ERP `User` (service account or delegated user) with read/report role perms on the allowlisted DocTypes, `User Permission` allowlist on `Company`, `apply_strict_user_permissions=1`, no `Administrator`, and bridge code held to §3.3's banned-path list satisfies the FAC for governed reads **as far as static source review can establish**. Runtime conformance (real denials on cross-company reads, report filter-value checks, audit-log join) is `[pending-G1]` and enumerated in §5's red matrix.

---

## 4. FAC-app (Frappe Assistant Core) decision draft — C04 input

Static audit verdict stands: **PARTIAL — usable for read-path with mandatory config** (`FAC-audit.md`: `validate_document_access` → `has_permission` doc-level; leak paths `analyze_business_data`/`run_database_query`/Query Reports must be disabled; no idempotency; AGPL-3.0 → stock-install + HTTP-only).

Draft decision frame for founder (runtime half pending G1):

| Option | When it's right | Cost |
|---|---|---|
| **REUSE** FAC transport | if A4 wants generic reads across *many* DocTypes + built-in per-call audit (`Assistant Audit Log`, `X-Assistant-Session-Id` correlation) and the site accepts an AGPL app installed stock | per-site config burden (~19 tools to disable), third-party upgrade track, bus-factor ≈1 org, plus a thin MCP client in kernel (~150 LOC) |
| **SKIP** → whitelisted bridge reads | if the read set is the small typed surface in §2 (4 capabilities, ≤6 DocTypes) | build `read.py` endpoints + auth + audit ourselves; no external dependency; exact control |

**Worker recommendation [suy luận]:** SKIP is the lower-risk default for WP-A4a's stated scope — §2's surface is deliberately narrow, the kernel already needs its own auth/audit/ledger story, and C04's recorded fallback ("whitelisted bridge reads only") matches. REUSE stays attractive only if the doc-type read surface grows broad before pilot. Either way the kernel CapabilityGateway grant check sits in front of the transport; FAC's permission layer is never the primary enforcement.

Runtime checks that still must run on G1 before REUSE is declared (from `FAC-audit.md` §5): disabled-tool list actually enforced, `assistant_enabled` gating, `server_enabled` non-kill-switch behavior, audit-log join via session header, plus AGPL posture sign-off.

---

## 5. Gap list — what only a live G1 site can settle

| # | Gap | Why it can't be settled from source |
|---|---|---|
| G1 | Real `User Permission` behavior on seeded Companies — `applicable_for`, `hide_descendants`, descendant expansion on Company trees (`user_permission.json` fields verified; runtime semantics need data) | needs seeded multi-company site |
| G2 | `apply_strict_user_permissions` effective behavior on our DocTypes (empty-link rows visible or not) | needs real docs + a user with Company UP |
| G3 | Field-level (`permlevel`) exposure on Sales Invoice/PLE/Customer for the chosen bridge role — which contract fields survive filtering | permlevel matrix is role×permlevel×site-config |
| G4 | Accounts Receivable report: does `validate_filters_permissions` actually deny `company=<other-co>` for a UP-scoped user, and do prepared-report paths respect owner checks | report code read; behavior needs execution |
| G5 | DocType inventory + DocPerm role reality on the G1 site (custom DocTypes/permlevels installed beyond stock) | site content unknown |
| G6 | Auth at rest: actual `api_key`/`api_secret` generation UX on v16 + whether `Frappe-Authorization-Source` non-User doctypes are needed | ops task on live site |
| G7 | FAC-app runtime: tool-disable config persistence, audit-log fields, session-header join, FAC Chat surface default-off | needs installed site |
| G8 | Read latency/limits for the report path (`prepared_report` async path on large sites) | performance is runtime-only |
| G9 | `X-Frappe-Request-Id` → observable join end-to-end (monitor data → logs a deploy can actually read) | infra-dependent |
| G10 | Error taxonomy actually returned by v1/v2 on perm deny (403 shape) vs HTTP layer of the deployment | reverse proxy/stack dependent |

## 6. Red-test matrix (to run on G1; contract obligations)

| # | Case | Contract clause | Pass means |
|---|---|---|---|
| R1 | Company-A principal `get_list` on PLE filtered to Company B | F2/F3 | zero rows or `permission_denied`, never Company-B data |
| R2 | Company-A principal `GET /api/v2/document/Sales Invoice/<B-owned>` | F2 | deny; response shape per F10 |
| R3 | Bridge code path cannot call `frappe.get_all` | F5 | lint/test in bridge repo (pre-site gate) |
| R4 | `Accounts Receivable` run with `company=B` by A-principal | F7 + `validate_filters_permissions` | deny at filter validation |
| R5 | Token revoked → same request | §3.1 | 401/`AuthenticationError`, no cached session |
| R6 | Request without `X-Frappe-Request-Id` still works; with it → joinable | F8 | correlation row present in monitor/log evidence |
| R7 | `permlevel`-restricted field on allowlisted DocType | F6 | field absent from response, no error |
| R8 | `limit` beyond cap | §1.4 | clamp or `schema_invalid`, never unbounded |

---

## Appendix — evidence map

Every endpoint/behavior in §1–3 cites `frappe-v16` (`6b450a1`, v16.51.0) or `erpnext-v16` (`7474d9e`, v16.50.0) `file:line` as read on this box today. Kernel-side contract context cites `enterprise-work-control-plane@2147443` (`docs/program/plans/2026-10-09-A5a-capability-contract.md`, `docs/reviews/erpnext-council-2026-10/{W2-erpnext-capability-audit,FAC-audit}.md`). FAC-app claims are second-order cites of `FAC-audit.md` (audit of `buildswithpaul/Frappe_Assistant_Core@99deda4` v3.0.0), not re-derived here.
