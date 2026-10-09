"""The single governed-write verb of A5b: ``create_draft_po``.

Contract anchor: kernel stage plan
``docs/program/plans/2026-10-09-A5b-governed-write.md`` §4–§6.

Guarantees this method must keep:

- **Draft only** — calls ``doc.insert()`` and returns; there is no
  ``submit()``/``save()``/``db_set("docstatus")`` path anywhere in this
  module, and a post-insert assertion proves ``docstatus == 0`` before
  the response is built.
- **Fail closed** — role, create-permission and User-Permission scope are
  all resolved against the ERP execution principal; the declared
  ``permission_policy`` is verified, never trusted.
- **Exactly once** — ``ewcp_idempotency_key`` unique field is the anchor;
  a replay returns the stored row, a hash mismatch is a 409 conflict, and
  a unique-index collision converges to the same answer via read-back.
- **Allowlist input** — only schema fields are read; ``doctype`` is a
  server constant and stray kwargs never reach this function
  (``frappe.call`` → ``get_newargs`` drops them), so ``docstatus``,
  ``ignore_permissions``, ``run_method`` and friends cannot be injected.
- **Atomic** — no mid-flow ``frappe.db.commit()`` (contract lint ban);
  any throw propagates and ``app.py`` rolls the whole request txn back.

Banned in this module by contract lint: ``frappe.db.commit``,
``frappe.db.sql``, ``frappe.get_all``, ``ignore_permissions=True``.
"""

import frappe
from frappe.utils import flt, getdate, now_datetime

from . import authorization, idempotency
from .errors import (
	EWCPBridgeError,
	NotFoundOrDenied,
	ProposalExpired,
	SchemaInvalid,
	normalize_existence,
)

PO_DOCTYPE = "Purchase Order"

CONTRACT_VERSION = "1"
ACTION_TYPE = "erp.draft_po"
METHOD_NAME = "create_draft_po"
DEFAULT_NAMING_SERIES = "PUR-ORD-.YYYY.-"

MAX_ITEMS = 100  # [giả định] bounded list per stage plan §4.2.3

# The only fields read out of a ProposedAction payload — everything else
# is ignored and logged for audit.
ITEM_ALLOWED_FIELDS = frozenset(
	{"item_code", "qty", "uom", "rate", "schedule_date", "warehouse"}
)

logger = frappe.logger("ewcp_bridge")


def _reject_extra_fields(payload: dict) -> None:
	"""Audit-log payload keys outside the schema (they are ignored)."""

	known = {
		"version",
		"action_type",
		"capability_id",
		"method",
		"tenant_id",
		"company",
		"supplier",
		"items",
		"currency",
		"conversion_rate",
		"transaction_date",
		"source_refs",
		"requester",
		"permission_policy",
		"idempotency_key",
		"expires_at",
		"payload_hash",
		"decision",
	}
	extra = sorted(set(payload) - known)
	if extra:
		logger.warning(f"create_draft_po: ignored non-schema payload keys {extra}")


def _require_str(payload: dict, field: str) -> str:
	value = payload.get(field)
	if not isinstance(value, str) or not value.strip():
		raise SchemaInvalid(f"schema_invalid: {field} must be a non-empty string")
	return value.strip()


def _require_link(field: str, doctype: str, value: str) -> str:
	"""Declared Link must resolve inside the caller's effective scope.

	F10: missing and out-of-scope collapse into one uniform denial —
	the response can never leak which of the two it was.
	"""
	if not authorization.name_in_scope(doctype, value):
		raise NotFoundOrDenied()
	return value


def _validate_items(raw) -> list[dict]:
	if not isinstance(raw, list) or not raw:
		raise SchemaInvalid("schema_invalid: items must be a non-empty array")
	if len(raw) > MAX_ITEMS:
		raise SchemaInvalid(
			f"schema_invalid: items exceeds bound of {MAX_ITEMS} rows"
		)
	items = []
	for idx, row in enumerate(raw):
		if not isinstance(row, dict):
			raise SchemaInvalid(f"schema_invalid: items[{idx}] must be an object")
		extra = sorted(set(row) - ITEM_ALLOWED_FIELDS)
		if extra:
			logger.warning(
				f"create_draft_po: ignored non-schema item fields {extra} at items[{idx}]"
			)
		item_code = row.get("item_code")
		if not isinstance(item_code, str) or not item_code.strip():
			raise SchemaInvalid(
				f"schema_invalid: items[{idx}].item_code must be a non-empty string"
			)
		_require_link("item_code", "Item", item_code.strip())
		if "qty" not in row or row.get("qty") in (None, ""):
			raise SchemaInvalid(f"schema_invalid: items[{idx}].qty is required")
		qty = flt(row.get("qty"))
		if qty <= 0:
			raise SchemaInvalid(f"schema_invalid: items[{idx}].qty must be > 0")
		if "rate" not in row or row.get("rate") in (None, ""):
			raise SchemaInvalid(f"schema_invalid: items[{idx}].rate is required")
		rate = flt(row.get("rate"))
		if rate < 0:
			raise SchemaInvalid(f"schema_invalid: items[{idx}].rate must be >= 0")
		item = {"item_code": item_code.strip(), "qty": qty, "rate": rate}
		for opt in ("uom", "schedule_date"):
			if row.get(opt) not in (None, ""):
				item[opt] = row[opt]
		if row.get("warehouse"):
			_require_link("warehouse", "Warehouse", row["warehouse"])
			item["warehouse"] = row["warehouse"]
		items.append(item)
	return items


def _validate_expiry(payload: dict) -> None:
	expires_at = payload.get("expires_at")
	if expires_at in (None, ""):
		raise SchemaInvalid("schema_invalid: expires_at is required")
	try:
		expires_at = float(expires_at)
	except (TypeError, ValueError):
		raise SchemaInvalid("schema_invalid: expires_at must be a unix timestamp")
	if now_datetime().timestamp() > expires_at:
		raise ProposalExpired(
			"proposal_expired: ProposedAction expired before execution"
		)


def _validate_actor_and_decision(payload: dict) -> tuple[str, str]:
	"""Requester + approver presence/shape; both get stamped on the PO.

	The bridge validates shape only — the *binding* between approval and
	payload hash is enforced kernel-side (stage plan §2.3). A
	``decision.payload_hash`` present but unequal to the payload's own
	hash is a corruption signal, rejected here as
	``proposal_hash_mismatch``.
	"""

	requester = _require_str(payload, "requester")
	if not requester.startswith("user:") or "@tenant:" not in requester:
		raise SchemaInvalid(
			"schema_invalid: requester must have shape user:<u>@tenant:<t>"
		)

	decision = payload.get("decision")
	if not isinstance(decision, dict):
		raise SchemaInvalid("schema_invalid: decision object is required")
	approved_by = decision.get("approved_by")
	if not isinstance(approved_by, str) or not approved_by.strip():
		raise SchemaInvalid(
			"schema_invalid: decision.approved_by must be a non-empty string"
		)
	decision_hash = decision.get("payload_hash")
	if decision_hash is not None:
		idempotency.validate_payload_hash(decision_hash)
		if decision_hash.lower() != payload["payload_hash"].lower():
			raise EWCPBridgeError(
				"proposal_hash_mismatch: decision.payload_hash != payload.payload_hash",
				errcode="proposal_hash_mismatch",
			)
	return requester, approved_by.strip()


def _validate_payload(payload) -> dict:
	if not isinstance(payload, dict):
		raise SchemaInvalid("schema_invalid: payload must be an object")
	_reject_extra_fields(payload)

	if payload.get("version") != CONTRACT_VERSION:
		raise SchemaInvalid(
			f"schema_invalid: unsupported version {payload.get('version')!r}"
		)
	if payload.get("action_type") != ACTION_TYPE:
		raise SchemaInvalid(
			f"schema_invalid: action_type {payload.get('action_type')!r} != {ACTION_TYPE!r}"
		)
	if payload.get("method") != METHOD_NAME:
		raise SchemaInvalid(
			f"schema_invalid: method {payload.get('method')!r} != {METHOD_NAME!r}"
		)

	key = idempotency.validate_key(payload.get("idempotency_key"))
	payload_hash = idempotency.validate_payload_hash(payload.get("payload_hash"))
	_validate_expiry(payload)
	requester, approved_by = _validate_actor_and_decision(payload)

	# company existence is NOT checked here — resolving the name would be
	# an existence oracle at schema phase. check_company_scope in the
	# authorization phase emits the uniform F10 denial.
	company = _require_str(payload, "company")
	supplier = _require_link(
		"supplier", "Supplier", _require_str(payload, "supplier")
	)
	currency = _require_link(
		"currency", "Currency", _require_str(payload, "currency")
	)

	raw_date = _require_str(payload, "transaction_date")
	try:
		transaction_date = getdate(raw_date)
	except Exception:
		raise SchemaInvalid(
			"schema_invalid: transaction_date must be YYYY-MM-DD"
		) from None

	items = _validate_items(payload.get("items"))

	conversion_rate = payload.get("conversion_rate")
	if conversion_rate is not None and flt(conversion_rate) <= 0:
		raise SchemaInvalid("schema_invalid: conversion_rate must be > 0")

	return {
		"key": key,
		"payload_hash": payload_hash,
		"company": company,
		"supplier": supplier,
		"currency": currency,
		"conversion_rate": flt(conversion_rate) if conversion_rate else None,
		"transaction_date": transaction_date,
		"items": items,
		"requester": requester,
		"approved_by": approved_by,
		"workrun_id": (payload.get("source_refs") or {}).get("workrun_id")
		if isinstance(payload.get("source_refs"), dict)
		else None,
		"policy": payload.get("permission_policy"),
	}


def _build_doc(v: dict):
	"""Construct the PO from the validated field set — never doc.update()."""

	doc = frappe.new_doc(PO_DOCTYPE)
	doc.company = v["company"]
	doc.supplier = v["supplier"]
	doc.transaction_date = v["transaction_date"]
	doc.currency = v["currency"]
	if v["conversion_rate"]:
		doc.conversion_rate = v["conversion_rate"]
	if not doc.get("naming_series"):
		doc.naming_series = DEFAULT_NAMING_SERIES
	doc.buying_price_list = doc.buying_price_list or "Standard Buying"

	for item in v["items"]:
		# schedule_date defaults to the order date — PO validates
		# schedule_date >= transaction_date, equality is allowed.
		item.setdefault("schedule_date", v["transaction_date"])
		doc.append("items", dict(item))

	# EWCP stamp fields — the audit join PO ↔ ProposedAction lives on the
	# row itself (stage plan §5.1).
	doc.set(idempotency.IDEMPOTENCY_FIELD, v["key"])
	doc.set(idempotency.PAYLOAD_HASH_FIELD, v["payload_hash"])
	doc.set(idempotency.REQUESTER_FIELD, v["requester"])
	doc.set(idempotency.APPROVED_BY_FIELD, v["approved_by"])
	if v["workrun_id"]:
		doc.set(idempotency.WORKRUN_FIELD, v["workrun_id"])
	return doc


def _response(doc, *, replayed: bool) -> dict:
	"""Read-back of the stored row — never the request echo (§4.1)."""

	return {
		"replayed": replayed,
		"po_name": doc.name,
		"docstatus": int(doc.docstatus),
		"ewcp_idempotency_key": doc.get(idempotency.IDEMPOTENCY_FIELD),
		"ewcp_payload_hash": doc.get(idempotency.PAYLOAD_HASH_FIELD),
		"company": doc.company,
		"supplier": doc.supplier,
		"transaction_date": str(doc.transaction_date),
		"grand_total": flt(doc.get("grand_total")),
	}


@frappe.whitelist(methods=["POST"])
@normalize_existence
def create_draft_po(payload: dict) -> dict:
	"""Create one draft Purchase Order under the governed-write contract.

	Order of operations per stage plan §4–§5: schema validation →
	authorization (role → create perm → company scope → policy verify) →
	idempotency pre-check → build → insert (single request txn) →
	draft assertion → read-back response.

	``@normalize_existence`` re-throws any Frappe-native existence or
	permission signal from inside the method (insert-time UP walk,
	doc read-back) as the uniform ``not_found_or_denied`` — F10.
	"""

	v = _validate_payload(payload)

	# --- authorization: fail closed, verified against the session user ---
	authorization.require_write_role()
	authorization.check_create_permission()
	authorization.check_company_scope(v["company"])
	if v["policy"] is not None:
		authorization.verify_permission_policy(v["policy"])

	# --- idempotency fast path (§5.2.1) ---
	preexisting = idempotency.check_preexisting(v["key"], v["payload_hash"])
	if preexisting is not None:
		stored = frappe.get_doc(PO_DOCTYPE, preexisting["po_name"])
		authorization.assert_draft(stored)
		return _response(stored, replayed=True)

	doc = _build_doc(v)
	try:
		doc.insert()  # check_permission("create") + UP walk rerun inside
	except frappe.UniqueValidationError:
		# Two-worker race (§5.2.3): the winner's row is readable in this
		# txn — InnoDB statement-atomicity — converge on the same answer.
		winner = idempotency.resolve_after_collision(v["key"], v["payload_hash"])
		stored = frappe.get_doc(PO_DOCTYPE, winner["po_name"])
		authorization.assert_draft(stored)
		return _response(stored, replayed=True)

	authorization.assert_draft(doc)
	# Re-read from DB so the response is the stored row, not our object.
	stored = frappe.get_doc(PO_DOCTYPE, doc.name)
	authorization.assert_draft(stored)
	logger.info(
		f"create_draft_po: created {stored.name} key={v['key']} "
		f"requester={v['requester']} approved_by={v['approved_by']}"
	)
	return _response(stored, replayed=False)
