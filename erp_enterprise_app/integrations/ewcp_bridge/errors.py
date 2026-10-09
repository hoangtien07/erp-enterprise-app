"""Bridge rejection types — machine-readable error surface.

Every rejection raised inside ``ewcp_bridge`` carries:
- ``http_status_code`` — honored by ``frappe/app.py handle_exception`` (the
  attribute the Frappe exception→HTTP mapper reads),
- ``errcode`` — the stable contract label the kernel maps on (§6 of the
  A5b stage plan: ``permission_denied`` / ``not_found`` / ``schema_invalid``
  / ``idempotency_payload_mismatch`` / ``policy_violation`` /
  ``proposal_expired``, plus F10's ``not_found_or_denied``),
- ``exc_type`` — the class name, serialized into the REST error body by
  Frappe, so HTTP consumers get the label without parsing message text.
"""

import functools

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
	"""A declared Link value does not exist — unsanitized variant.

	NOT raised on resource-name probes anymore (F10: existence must not be
	distinguishable); kept for surfaces where the caller is authorized to
	resolve the name anyway.
	"""

	http_status_code = 404
	errcode = "not_found"


class NotFoundOrDenied(EWCPBridgeError):
	"""Uniform denial for resource-identifier probes (F10).

	A missing document and an out-of-scope document produce the SAME
	rejection — same status, ``exc_type``, ``errcode`` and message text —
	so a scoped principal cannot enumerate documents it cannot see. The
	message carries no doctype/name/user.
	"""

	http_status_code = 403
	errcode = "not_found_or_denied"
	MESSAGE = (
		"not_found_or_denied: resource does not exist or is outside the "
		"permitted scope"
	)

	def __init__(self, message: str = MESSAGE):
		super().__init__(message)


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


def normalize_existence(fn):
	"""Method-boundary normalization (F10).

	Frappe-native existence/permission signals raised inside a governed
	method — ``frappe.PermissionError`` from ``check_permission`` or the
	link-field User-Permission walk inside ``doc.insert()``, and
	``frappe.DoesNotExistError`` — are re-thrown as the uniform
	``NotFoundOrDenied``. Both carry doctype + doc name in their default
	text (e.g. ``"...linked to Company 'X' in field Company"``), which is
	an existence oracle; the re-thrown error's text is generic.

	Only those two classes are caught: ``frappe.PermissionError`` is a
	bare ``Exception`` and ``frappe.DoesNotExistError`` a direct
	``ValidationError`` subclass — neither is an ancestor of
	``EWCPBridgeError``, so the bridge's own contract errors propagate
	unchanged. The original is logged server-side for forensics.
	"""

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		try:
			return fn(*args, **kwargs)
		except (frappe.PermissionError, frappe.DoesNotExistError) as exc:
			frappe.logger("ewcp_bridge").warning(
				f"normalized {type(exc).__name__} -> not_found_or_denied: {exc}"
			)
			raise NotFoundOrDenied() from None

	return wrapper
