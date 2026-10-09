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
| `not_found_or_denied` | `NotFoundOrDenied` | 403 | **any resource-identifier probe fails** — declared Link (Company/Supplier/Currency/Item/Warehouse) missing OR outside the principal's User-Permission scope; also every Frappe-native existence/permission signal inside the method. See §3.1 |
| `permission_denied` | `PermissionDenied` | 403 | principal-level denial only: missing `EWCP Write` role, no create perm |
| `policy_violation` | `PolicyViolation` | 403 | declared `permission_policy` ≠ effective perms (role/UP/strict-UP mismatch) |
| `proposal_expired` | `ProposalExpired` | 409 | `expires_at` in the past at execution |
| `proposal_hash_mismatch` | `EWCPBridgeError` | 400 | `decision.payload_hash` ≠ `payload.payload_hash` (corruption signal; the real hash binding is kernel-side §2.3) |
| `idempotency_payload_mismatch` | `IdempotencyConflict` | 409 | same key, different stored hash |
| `draft_violation` | `DefectAlarm` | 500 | post-insert `docstatus != 0` — defect alarm, not a response |

Frappe-native exceptions still surface as-is (e.g. `ValidationError` from a
PO business rule → request rolls back per `app.py` exception path), EXCEPT
`frappe.PermissionError`/`frappe.DoesNotExistError`, which are normalized
per §3.1.

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

### 3.1 F10 — no existence disclosure (P1 fix, VERIFIED-G1′)

ERPNext's default denial leaks existence: 403-with-name for a denied doc
vs 404 for a missing one (`docs/A4A_RUNTIME_EVIDENCE.md` F10). The bridge
normalizes so the two are **byte-identical**:

- **`authorization.name_in_scope(doctype, name, user, for_doctype)`** —
  one predicate folding "exists" (`frappe.db.exists`) and "in UP scope"
  (mirrors `check_user_permission_on_link_fields`,
  `frappe/permissions.py`: only UP rows whose `applicable_for` is empty or
  equals `for_doctype` restrict; descendants pre-expanded by
  `get_user_permissions`). `_require_link` and `check_company_scope`
  raise the uniform `NotFoundOrDenied` on `False`.
- **`errors.normalize_existence`** — method-boundary decorator re-throws
  `frappe.PermissionError`/`frappe.DoesNotExistError` as
  `NotFoundOrDenied` (generic message, no doctype/name/user). Covers
  insert-time UP denials on link fields the schema doesn't pre-check
  (`uom`, `buying_price_list`, computed child rows) and doc read-backs.
  `frappe.PermissionError`/`DoesNotExistError` share no ancestry with
  `EWCPBridgeError`, so contract errors propagate unchanged.
- **`NotFoundOrDenied`**: HTTP 403, `errcode=not_found_or_denied`,
  fixed message. `NotFound` (404) is retained but no longer raised on
  probes.

**Measured (G1′, 2026-10-09):** `company=EWCP Dev Company B` vs
`company=<bogus>` → both `HTTP 403`, body **byte-identical** (same
`exc_type`, `exception` traceback, message). Same for supplier/item/
warehouse probes. Success path unchanged (create 200 → replay 200).

**Residual gaps (out of bridge scope):**

- Raw REST `/api/resource/*` + `/api/v2/document/*` still oracle
  (403-names-doc vs 404) — cannot be normalized from an app without
  patching Frappe; kernel `erp_reads` pack maps 403/404 client-side
  (A4a §6). Governed callers MUST NOT hit raw document endpoints for
  doc-existence-sensitive reads.
- Idempotency-key space: a collision with an *invisible* row yields the
  same `IdempotencyConflict` as a hash mismatch — but free-key vs
  taken-key still differs (200 vs 409). Keys are unguessable 140-char
  values and the winner's `po_name` is never disclosed — weak oracle,
  accepted.
- Denial `exc` traceback reveals the raise site (schema vs authz vs
  insert-time) — internal code-path info, not existence data.
- `permission_denied` (role/create-perm) and `policy_violation` messages
  echo the user and *declared* values — caller-supplied, no existence.
- Strict-mode caveat (measured): a User Permission on a doctype that
  appears as an EMPTY link field anywhere in the doc tree (e.g. the
  auto-generated `Item Default` row's `default_supplier`) denies EVERY
  insert — uniformly `not_found_or_denied`. UP on write-path doctypes is
  effectively a kill-switch for this principal; treat as break-glass.

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
  → **22/22 OK** (test list maps §8 rows: replay/conflict, wrong company,
  missing role, guest, injection, business-rule rollback, submit surfaces,
  expiry, hash-mismatch, schema guards, plus P1 F10 rows: company
  denied-vs-nonexistent identity, scoped-link identity, insert-time UP
  normalization).
- HTTP: `POST` create → 200 `{replayed:false, po_name:"PUR-ORD-2026-00001",
  docstatus:0, grand_total:75.0}`; same call again → `{replayed:true}`;
  GET → 403; unauth → 403; mutated payload same key → 409
  `IdempotencyConflict`; `company=B` → 403 `not_found_or_denied`
  (post-P1; was `permission_denied` naming the company);
  `PUT /api/resource/PO {docstatus:1}` → 403; key read-back returns the row.
- F10 oracle kill (P1, 2026-10-09): `company=B` vs `company=<bogus>` →
  identical 403 `NotFoundOrDenied` body (before: 403 `PermissionDenied`
  naming the company vs 404 `NotFound`).
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
