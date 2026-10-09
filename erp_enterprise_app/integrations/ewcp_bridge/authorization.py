"""Authorization surface for governed writes (A5b).

Three independent checks, all fail-closed:

1. ``require_write_role`` — the ERP execution principal must hold the
   custom ``EWCP Write`` role. ``frappe.has_permission`` alone is not
   enough: Administrator short-circuits every perm check
   (``frappe/permissions.py``), so the *literal role* is asserted too.
2. ``check_create_permission`` — role-level ``create`` on Purchase Order.
3. ``check_company_scope`` + ``verify_permission_policy`` — the
   User-Permission allowlist is resolved against the ERP session user and
   compared to the company being written; the declared
   ``permission_policy`` block of the ProposedAction is *verified, never
   trusted* (stage plan §4.3.4).

``doc.insert()`` itself re-runs ``check_permission("create")`` and
``has_user_permission`` on every link field — the checks here are an
earlier, cleaner failure surface; the insert-time check is the
enforcement of record.
"""

import frappe

from .errors import (
	EWCPBridgeError,
	NotFoundOrDenied,
	PermissionDenied,
	PolicyViolation,
)

REQUIRED_ROLE = "EWCP Write"
PO_DOCTYPE = "Purchase Order"


class DefectAlarm(EWCPBridgeError):
	"""Post-condition violation — a returned PO was not a draft."""

	http_status_code = 500
	errcode = "draft_violation"


def require_write_role(user: str | None = None) -> None:
	"""Session user must literally hold ``EWCP Write``.

	Deliberately strict: Administrator does not pass unless the role was
	granted — the governed lane is for the scoped service principal, and
	fail-closed beats convenient.
	"""

	user = user or frappe.session.user
	if REQUIRED_ROLE not in frappe.get_roles(user):
		raise PermissionDenied(
			f"permission_denied: user {user} lacks required role {REQUIRED_ROLE}"
		)


def check_create_permission(user: str | None = None) -> None:
	"""Role-level ``create`` on Purchase Order for the session user."""

	user = user or frappe.session.user
	if not frappe.has_permission(PO_DOCTYPE, "create", user=user):
		raise PermissionDenied(
			f"permission_denied: user {user} has no create permission on {PO_DOCTYPE}"
		)


def _effective_user_permissions(user: str) -> dict:
	"""Resolved User-Permission allowlists for ``user``.

	Returns ``{allow_doctype: [permitted doc names]}``; empty dict means
	either no UP records exist (unrestricted) or the user is
	Administrator — callers must treat "no UP rows" as *unrestricted*, the
	same semantics ``has_user_permission`` applies.
	"""

	raw = frappe.permissions.get_user_permissions(user) or {}
	# get_user_permissions returns {doctype: [row dicts with `doc` + meta]}
	return {
		doctype: [row.get("doc") for row in rows if isinstance(row, dict)]
		for doctype, rows in raw.items()
	}


def name_in_scope(
	link_doctype: str,
	name: str,
	user: str | None = None,
	*,
	for_doctype: str = PO_DOCTYPE,
) -> bool:
	"""True iff ``name`` is usable as a Link target on ``for_doctype``.

	Mirrors the link-field step of ``has_user_permission``
	(``frappe/permissions.py`` ``check_user_permission_on_link_fields``):
	User Permission rows on ``link_doctype`` restrict the usable values to
	their allowlist — but only rows applicable to ``for_doctype`` (empty
	``applicable_for`` = all doctypes). Existence folds into the same
	boolean, so callers emit ONE denial that cannot distinguish a missing
	document from a denied one (F10).
	"""

	user = user or frappe.session.user
	ups = frappe.permissions.get_user_permissions(user) or {}
	allowed = [
		row.get("doc")
		for row in ups.get(link_doctype, [])
		if not row.get("applicable_for") or row.get("applicable_for") == for_doctype
	]
	if allowed and name not in allowed:
		return False
	return bool(frappe.db.exists(link_doctype, name))


def check_company_scope(company: str, user: str | None = None) -> None:
	"""The declared company must resolve inside the user's visible scope.

	This is the *independent* scope check: it resolves the permission
	against the ERP execution principal, never against caller text.
	``doc.insert()`` will re-run the full link-field UP walk anyway —
	this earlier check exists to fail before insert.

	F10: the rejection is the uniform ``not_found_or_denied`` — a
	nonexistent company and an out-of-scope one are indistinguishable.
	"""

	if not name_in_scope("Company", company, user, for_doctype=PO_DOCTYPE):
		raise NotFoundOrDenied()


def verify_permission_policy(policy: dict, user: str | None = None) -> None:
	"""The payload's declared ``permission_policy`` must match reality.

	Policy shape (stage plan §2.1):
	``{"role": "EWCP Write",
	   "user_permissions": {"Company": [names...]},
	   "strict_user_permissions": true}``

	Every declared element is checked against the *effective* perms of the
	session principal — a declaration can never widen rights:
	- declared role must equal ``REQUIRED_ROLE`` and be held;
	- every declared UP ``for_value`` must appear in the user's resolved
	  UP allowlist for that doctype;
	- declared ``strict_user_permissions`` must match System Settings.
	"""

	user = user or frappe.session.user
	if not isinstance(policy, dict):
		raise PolicyViolation("policy_violation: permission_policy must be an object")

	declared_role = policy.get("role")
	if declared_role is not None:
		if declared_role != REQUIRED_ROLE:
			raise PolicyViolation(
				f"policy_violation: declared role {declared_role!r} != required {REQUIRED_ROLE!r}"
			)
		if REQUIRED_ROLE not in frappe.get_roles(user):
			raise PolicyViolation(
				f"policy_violation: declared role {REQUIRED_ROLE!r} not held by {user}"
			)

	declared_ups = policy.get("user_permissions") or {}
	if not isinstance(declared_ups, dict):
		raise PolicyViolation("policy_violation: user_permissions must be an object")
	effective = _effective_user_permissions(user)
	for doctype, values in declared_ups.items():
		if not isinstance(values, list):
			raise PolicyViolation(
				f"policy_violation: user_permissions[{doctype!r}] must be a list"
			)
		actual = effective.get(doctype) or []
		missing = [v for v in values if v not in actual]
		if missing:
			raise PolicyViolation(
				f"policy_violation: declared {doctype} UP {missing} not covered by "
				f"effective allowlist of {user}"
			)

	if "strict_user_permissions" in policy:
		actual_strict = bool(
			frappe.db.get_single_value("System Settings", "apply_strict_user_permissions")
		)
		if bool(policy["strict_user_permissions"]) != actual_strict:
			raise PolicyViolation(
				f"policy_violation: strict_user_permissions declared "
				f"{policy['strict_user_permissions']!r} but system setting is {actual_strict!r}"
			)


def assert_draft(doc) -> None:
	"""Post-condition: a document produced by the bridge is always draft.

	A nonzero docstatus here means a defect in the bridge or a hook that
	promoted the doc mid-insert — it is an alarm, not a response.
	"""

	if int(doc.docstatus) != 0:
		raise DefectAlarm(
			f"draft_violation: {doc.doctype} {doc.name} has docstatus={doc.docstatus}"
		)
