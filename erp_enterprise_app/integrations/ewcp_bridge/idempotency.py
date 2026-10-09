"""ERP-anchored idempotency for governed writes (A5b §5).

The anchor is ``ewcp_idempotency_key`` — a ``unique`` custom Data field
on ``Purchase Order``. The MariaDB unique index
(``frappe/database/mariadb/schema.py`` emits ``ADD UNIQUE INDEX`` for
``unique`` DocFields) is the only correct serialization point for the
two-worker race; the pre-insert ``get_list`` below is an optimization
that skips a doomed insert, never a correctness mechanism.

Semantics (§5.2):
- same key + same ``ewcp_payload_hash``  → replay the stored row (200)
- same key + different ``ewcp_payload_hash`` → ``idempotency_payload_mismatch`` (409)
- unique-index collision on insert → read back the winner's row and apply
  the same comparison — both racers converge on the same answer and
  exactly one PO exists.

No ``frappe.db.rollback()`` is issued here: on MariaDB/InnoDB a
duplicate-key ``INSERT`` fails at statement level and the request
transaction stays valid for reads. (On a real request the framework
rolls the whole txn back anyway if we raise.)
"""

import frappe

from .errors import IdempotencyConflict, SchemaInvalid

PO_DOCTYPE = "Purchase Order"
IDEMPOTENCY_FIELD = "ewcp_idempotency_key"
PAYLOAD_HASH_FIELD = "ewcp_payload_hash"
REQUESTER_FIELD = "ewcp_requester"
APPROVED_BY_FIELD = "ewcp_approved_by"
WORKRUN_FIELD = "ewcp_workrun_id"

KEY_MAX_LEN = 140  # Data(140) — stage plan §5.1
HASH_LEN = 64  # sha256 hex
KEY_ALLOWED_CHARS = frozenset(
	"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)


def validate_key(key) -> str:
	"""Shape-check an ``ewcp_idempotency_key`` value. Fail-closed."""

	if not isinstance(key, str) or not key:
		raise SchemaInvalid("schema_invalid: idempotency_key must be a non-empty string")
	if len(key) > KEY_MAX_LEN:
		raise SchemaInvalid(
			f"schema_invalid: idempotency_key exceeds {KEY_MAX_LEN} chars"
		)
	if not set(key) <= KEY_ALLOWED_CHARS:
		raise SchemaInvalid(
			"schema_invalid: idempotency_key contains characters outside [A-Za-z0-9._:-]"
		)
	return key


def validate_payload_hash(value) -> str:
	"""Shape-check a sha256 hex ``ewcp_payload_hash`` value.

	The bridge verifies *shape* only — recomputing the canonical hash is
	the kernel's binding step (stage plan §2.2/§2.3); ERP stores whatever
	H the governed write carried.
	"""

	if (
		not isinstance(value, str)
		or len(value) != HASH_LEN
		or any(c not in "0123456789abcdefABCDEF" for c in value)
	):
		raise SchemaInvalid(
			"schema_invalid: payload_hash must be a 64-char sha256 hex string"
		)
	return value.lower()


def lookup(key: str) -> dict | None:
	"""Read back the stored PO carrying ``key``, if any.

	Permissioned ``get_list`` (never ``get_all`` — A4a contract ban): the
	row is additionally User-Permission scoped, which is correct — a
	created PO is always inside the principal's company scope.
	"""

	rows = frappe.get_list(
		PO_DOCTYPE,
		filters={IDEMPOTENCY_FIELD: key},
		fields=["name", PAYLOAD_HASH_FIELD, "docstatus"],
	)
	return rows[0] if rows else None


def replay_or_conflict(stored: dict, payload_hash: str, key: str = "") -> dict:
	"""Decide the outcome for an already-stored ``key`` row.

	Same hash → ``replayed`` response naming the winner; different hash →
	``IdempotencyConflict`` (409). Both racers converge on this answer.
	"""

	if (stored.get(PAYLOAD_HASH_FIELD) or "").lower() == payload_hash.lower():
		return {
			"replayed": True,
			"po_name": stored["name"],
			"docstatus": int(stored.get("docstatus") or 0),
		}
	raise IdempotencyConflict(
		f"idempotency_payload_mismatch: key {key!r} "
		f"already committed under a different payload_hash on {stored['name']}"
	)


def check_preexisting(key: str, payload_hash: str) -> dict | None:
	"""Fast path (§5.2.1): a stored row settles the request pre-insert."""

	stored = lookup(key)
	if stored is None:
		return None
	return replay_or_conflict(stored, payload_hash, key)


def resolve_after_collision(key: str, payload_hash: str) -> dict:
	"""Post-``UniqueValidationError`` path (§5.2.3).

	The losing request's failed ``db_insert`` is statement-atomic on
	InnoDB, so the winner's row is readable in this same transaction.
	"""

	stored = lookup(key)
	if stored is None:
		# Unreachable in a correct deployment: a unique violation on our
		# key means a row with that key committed. Surface as conflict
		# rather than retry — never a blind second create (§6).
		raise IdempotencyConflict(
			"idempotency_payload_mismatch: unique-key collision but no "
			"surviving row readable for key — operator reconcile required"
		)
	return replay_or_conflict(stored, payload_hash, key)
