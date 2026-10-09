# A5b — `ewcp_bridge` governed-write surface (draft Purchase Order)

**Status:** implemented, live-verified on G1′ (2026-10-09). Contract source:
kernel stage plan `docs/program/plans/2026-10-09-A5b-governed-write.md`
(`main@9aaab99`). This document is the ERP-side half; kernel-side
`ProposedAction`/decision binding and the `external_write` allowlist are
separate lanes (§11 checklist items 2–3 of that plan).

Claims are marked **VERIFIED-G1′** (measured against the live site) or
**[giả định]** / **[suy luận]** where not exercised.

---

## 1. API surface

One whitelisted method — the only sanctioned write route:

```
POST /api/method/erp_enterprise_app.integrations.ewcp_bridge.write.create_draft_po
Body: {"payload": <ProposedAction-object>}
Auth: Authorization: token <key>:<secret>   (never allow_guest)
```

`create_draft_po(payload: dict) -> dict` — module-level function.
`methods=["POST"]` narrows the whitelist defaults: **VERIFIED-G1′** a GET
returns 403; unauthenticated POST returns 403.

`frappe.call` → `get_newargs` drops every kwarg outside the signature, so
`ignore_permissions`/`docstatus`/`run_method`/`doctype` cannot be injected
as arguments; inside the payload, non-schema keys are ignored and logged
(`logger "ewcp_bridge"`). `doctype` is a server constant; fields are copied
one-by-one (`frappe.new_doc` + explicit `doc.append`/`doc.set`) — never
`doc.update(payload)`. **VERIFIED-G1′** by test `test_injected_fields_ignored`.

### Response (read-back of the stored row, never the request echo)

```json
{"replayed": false, "po_name": "PUR-ORD-2026-00001", "docstatus": 0,
 "ewcp_idempotency_key": "…", "ewcp_payload_hash": "…",
 "company": "…", "supplier": "…", "transaction_date": "YYYY-MM-DD",
 "grand_total": 75.0}
```

### Rejection surface (`exc_type` is the machine-readable label)

| errcode | exc_type | HTTP | When |
|---|---|---|---|
| `schema_invalid` | `SchemaInvalid` | 400 | payload shape/version/`method` literal, items bound (≤100), qty>0, rate required ≥0, key ≤140 chars, hash = 64-hex |
| `not_found` | `NotFound` | 404 | declared Link (Company/Supplier/Currency/Item/Warehouse) doesn't exist — fails fast before `insert()`'s own LinkValidationError |
| `permission_denied` | `PermissionDenied` | 403 | missing `EWCP Write` role, no create perm, company outside UP scope |
| `policy_violation` | `PolicyViolation` | 403 | declared `permission_policy` ≠ effective perms (role/UP/strict-UP mismatch) |
| `proposal_expired` | `ProposalExpired` | 409 | `expires_at` in the past at execution |
| `proposal_hash_mismatch` | `EWCPBridgeError` | 400 | `decision.payload_hash` ≠ `payload.payload_hash` (corruption signal; the real hash binding is kernel-side §2.3) |
| `idempotency_payload_mismatch` | `IdempotencyConflict` | 409 | same key, different stored hash |
| `draft_violation` | `DefectAlarm` | 500 | post-insert `docstatus != 0` — defect alarm, not a response |

Frappe-native exceptions still surface as-is (e.g. `ValidationError` from a
PO business rule → request rolls back per `app.py` exception path).

## 2. Role + field fixtures (`fixtures.install` → `after_install`/`after_migrate`)

| Fixture | Shape | Enforcement |
|---|---|---|
| Role `EWCP Write` | `desk_access=0` | `require_write_role` asserts literal membership — Administrator short-circuits `has_permission`, so the *role itself* is checked |
| `Custom DocPerm` on Purchase Order | `read+write+create+report=1`; submit/cancel/amend/delete/share=0 | generic submit surfaces deny: `frappe.client.submit`, `doc.submit()`, `savedocs Submit` → `PermissionError` **VERIFIED-G1′** |
| `ewcp_idempotency_key` | Data(140), `unique=1`, `set_only_once`, `no_copy`, `print_hide` | `SHOW INDEX` on `tabPurchase Order` shows UNIQUE index `ewcp_idempotency_key` **VERIFIED-G1′** |
| `ewcp_payload_hash` | Data(64), `set_only_once`, `no_copy` | stores H the PO was created under |
| `ewcp_requester` / `ewcp_approved_by` / `ewcp_workrun_id` | Data(140), `no_copy`, `print_hide` | audit join PO ↔ ProposedAction on the row itself (§5.1); `owner`/`modified_by` still name the service account (§3.1) |

## 3. Authorization (fail closed, resolved against the ERP principal)

Order inside the method: `require_write_role` → `has_permission("Purchase Order","create")` → `check_company_scope` (resolved UP allowlist must contain the payload's company) → `verify_permission_policy` (declared policy block verified, never trusted) → `doc.insert()` re-runs `check_permission("create")` + `has_user_permission` over every link field — the enforcement of record. **[giả định]** Supplier scope is unbounded (no Supplier UPs seeded — founder Q5).

`guest`/unauth callers can't reach the function over HTTP (non-guest
whitelist → 403 **VERIFIED-G1′**); in-process they die at `require_write_role`.

## 4. Exactly-once semantics

1. **Fast path:** permissioned `get_list` by `ewcp_idempotency_key` (never
   `get_all` — A4a ban). Hit → same hash ⇒ `{replayed:true, po_name}`;
   different hash ⇒ `IdempotencyConflict` 409.
2. **Race path:** unique-index violation at `db_insert` →
   `UniqueValidationError` → read the winner's row in the same txn (InnoDB
   duplicate-key errors are statement-atomic — the txn stays readable) →
   same comparison. Both racers converge; exactly one PO exists.
   **VERIFIED-G1′** via `test_two_worker_collision_converges` (pre-check
   forced to miss → real index collision → read-back).
3. **Layering:** this is the system-of-record anchor; kernel
   `Idempotency-Key` transport dedupes earlier (§5.3). Same key value at
   both layers.
4. **UNKNOWN reconcile (§6):** caller times out → read-back
   `GET /api/resource/Purchase Order?filters=[["ewcp_idempotency_key","=",key]]`
   **VERIFIED-G1′** — empty ⇒ safe retry; matching hash ⇒ done; different
   hash ⇒ `idempotency_payload_mismatch`, no blind retry.

## 5. Draft-only guarantee (3 layers, §4.4)

- **Code:** `doc.insert()` only — no `submit`/`save`/`docstatus` write, no
  mid-flow `frappe.db.commit()` (contract lint ban: `db.commit`, `db.sql`,
  `get_all`, `ignore_permissions` in `ewcp_bridge/`).
- **Role:** no `submit`/`cancel`/`amend`/`delete` ptype → generic surfaces
  403. Cancel on a draft additionally hits `DocstatusTransitionError`
  (0→2 illegal) **VERIFIED-G1′**.
- **Audit:** `assert_draft` post-insert + on every read-back path; nonzero
  docstatus ⇒ `DefectAlarm` 500.

## 6. Live verification record (G1′, 2026-10-09)

Site `ewcp-dev.localhost` @ `http://localhost:8080`
(`erp-platform-deploy@9f04be2` + local seed extension, principal
`ewcp-agent@ewcp.dev` roles `[Accounts User, EWCP Write]`, UP `Company→A`):

- `bench run-tests --module erp_enterprise_app.integrations.ewcp_bridge.tests.test_write`
  → **19/19 OK** (test list maps §8 rows: replay/conflict, wrong company,
  missing role, guest, injection, business-rule rollback, submit surfaces,
  expiry, hash-mismatch, schema guards).
- HTTP: `POST` create → 200 `{replayed:false, po_name:"PUR-ORD-2026-00001",
  docstatus:0, grand_total:75.0}`; same call again → `{replayed:true}`;
  GET → 403; unauth → 403; mutated payload same key → 409
  `IdempotencyConflict`; `company=B` → 403 `permission_denied`;
  `PUT /api/resource/PO {docstatus:1}` → 403; key read-back returns the row.
- Fixtures: `SHOW INDEX` unique `ewcp_idempotency_key`; Custom DocPerm row
  `read=1 write=1 create=1 submit=0 cancel=0 amend=0 delete=0`; Role exists.

## 7. Not verified here / [giả định]

- True two-process concurrency on one site DB — the race path is verified
  with a forced pre-check miss inside one txn; MariaDB serializes the index
  but no live parallel-worker capture (statement-atomicity claim is
  documented InnoDB behavior, marked [giả định] until a threaded run).
- `X-Frappe-Request-Id` correlation — caller-side contract (kernel sends
  hex/UUID; A4a R6 verified `wr-` ids are dropped by `monitor.py` regex).
- `qty`/`rate` decimal-string transport (§2.2 note) — bridge accepts
  JSON numbers/strings via `flt`; canonical-hash coercion tolerance is
  kernel-side.
- `bench run-tests` needs `allow_tests true` on the site (set manually
  here: `bench --site ewcp-dev.localhost set-config allow_tests true`).
- Seed extension required in `erp-platform-deploy/scripts/seed_dev.py`
  (Supplier `EWCP Dev Supplier A1` + `EWCP Write` role grant) — run locally
  in this verification; **not committed** (deploy-repo followup).
- `after_migrate` ordering on an *existing* site with POs present: adding a
  `unique` field is safe only while existing values are unique
  (`schema.py` refuses otherwise) — G1′'s PO table was empty; production
  adoption on populated tables needs a dedupe pass first.
