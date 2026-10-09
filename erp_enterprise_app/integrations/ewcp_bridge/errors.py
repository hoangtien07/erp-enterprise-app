"""Bridge rejection types — machine-readable error surface.

Every rejection raised inside ``ewcp_bridge`` carries:
- ``http_status_code`` — honored by ``frappe/app.py handle_exception`` (the
  attribute the Frappe exception→HTTP mapper reads),
- ``errcode`` — the stable contract label the kernel maps on (§6 of the
  A5b stage plan: ``permission_denied`` / ``not_found`` / ``schema_invalid``
  / ``idempotency_payload_mismatch`` / ``policy_violation`` /
  ``proposal_expired``),
- ``exc_type`` — the class name, serialized into the REST error body by
  Frappe, so HTTP consumers get the label without parsing message text.
"""

import frappe


class EWCPBridgeError(frappe.ValidationError):
	"""Base rejection for governed-write bridge failures."""

	http_status_code = 400
	errcode = "bridge_error"

	def __init__(self, message: str, *, errcode: str | None = None):
		super().__init__(message)
		if errcode:
			self.errcode = errcode


class SchemaInvalid(EWCPBridgeError):
	"""Payload failed schema/shape validation before any doc existed."""

	http_status_code = 400
	errcode = "schema_invalid"


class NotFound(EWCPBridgeError):
	"""A declared Link value (company/supplier/item/warehouse) does not exist."""

	http_status_code = 404
	errcode = "not_found"


class PermissionDenied(EWCPBridgeError):
	"""Role or User-Permission scope check failed."""

	http_status_code = 403
	errcode = "permission_denied"


class PolicyViolation(EWCPBridgeError):
	"""Declared ``permission_policy`` does not match the effective perms."""

	http_status_code = 403
	errcode = "policy_violation"


class ProposalExpired(EWCPBridgeError):
	"""``expires_at`` is in the past — the ProposedAction is dead."""

	http_status_code = 409
	errcode = "proposal_expired"


class IdempotencyConflict(EWCPBridgeError):
	"""Same ``ewcp_idempotency_key`` arrived with a different payload hash."""

	http_status_code = 409
	errcode = "idempotency_payload_mismatch"
