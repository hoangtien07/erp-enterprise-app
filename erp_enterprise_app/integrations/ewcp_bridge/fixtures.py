"""App-deployed fixtures for the A5b governed-write lane.

Wired via ``after_install`` + ``after_migrate`` in ``hooks.py`` so both a
fresh ``install-app`` and an existing site converge on the same shape:

- Custom Fields on ``Purchase Order`` — ``ewcp_idempotency_key`` is
  ``unique`` so MariaDB emits ``ADD UNIQUE INDEX`` on schema sync
  (``frappe/database/mariadb/schema.py``); that index is the exactly-once
  serialization point (stage plan §5.1). ``set_only_once`` + ``no_copy``
  keep the key immutable and non-propagating on amend/duplicate.
- Role ``EWCP Write`` — desk-access off; the *only* role a governed
  execution principal holds.
- ``Custom DocPerm`` granting EWCP Write **create+write+read+report** on
  Purchase Order — deliberately NO submit/cancel/amend/delete/share, so
  every generic submit surface (``frappe.client.submit``, ``savedocs``
  Submit, ``doc.submit`` doc-method) 403s for this principal (§4.4.2).

Idempotent: safe to re-run on every migrate.
"""

import frappe

ROLE_NAME = "EWCP Write"
PO_DOCTYPE = "Purchase Order"

CUSTOM_FIELDS = {
	PO_DOCTYPE: [
		{
			"fieldname": "ewcp_idempotency_key",
			"label": "EWCP Idempotency Key",
			"fieldtype": "Data",
			"length": 140,
			"unique": 1,
			"set_only_once": 1,
			"no_copy": 1,
			"print_hide": 1,
			"insert_after": "transaction_date",
		},
		{
			"fieldname": "ewcp_payload_hash",
			"label": "EWCP Payload Hash",
			"fieldtype": "Data",
			"length": 64,
			"set_only_once": 1,
			"no_copy": 1,
			"print_hide": 1,
			"insert_after": "ewcp_idempotency_key",
		},
		{
			"fieldname": "ewcp_requester",
			"label": "EWCP Requester",
			"fieldtype": "Data",
			"length": 140,
			"no_copy": 1,
			"print_hide": 1,
			"insert_after": "ewcp_payload_hash",
		},
		{
			"fieldname": "ewcp_approved_by",
			"label": "EWCP Approved By",
			"fieldtype": "Data",
			"length": 140,
			"no_copy": 1,
			"print_hide": 1,
			"insert_after": "ewcp_requester",
		},
		{
			"fieldname": "ewcp_workrun_id",
			"label": "EWCP Workrun ID",
			"fieldtype": "Data",
			"length": 140,
			"no_copy": 1,
			"print_hide": 1,
			"insert_after": "ewcp_approved_by",
		},
	]
}

# Grant set is explicit: every ptype NOT listed True stays absent.
PO_PERMISSION = {
	"read": 1,
	"write": 1,
	"create": 1,
	"report": 1,
	"submit": 0,
	"cancel": 0,
	"amend": 0,
	"delete": 0,
	"share": 0,
	"export": 0,
	"import": 0,
	"print": 0,
	"email": 0,
	"if_owner": 0,
	"select": 0,
}


def _ensure_role() -> None:
	if frappe.db.exists("Role", ROLE_NAME):
		return
	doc = frappe.new_doc("Role")
	doc.role_name = ROLE_NAME
	doc.desk_access = 0
	# `is_custom` exists on Role in v16; set defensively if present.
	if any(df.fieldname == "is_custom" for df in doc.meta.fields):
		doc.is_custom = 1
	doc.insert(ignore_permissions=True)
	frappe.logger("ewcp_bridge").info(f"fixtures: created Role {ROLE_NAME}")


def _ensure_custom_docperm() -> None:
	exists = frappe.db.exists(
		"Custom DocPerm", {"parent": PO_DOCTYPE, "role": ROLE_NAME, "permlevel": 0}
	)
	if exists:
		doc = frappe.get_doc("Custom DocPerm", exists)
		changed = False
		for key, val in PO_PERMISSION.items():
			if doc.get(key) != val:
				doc.set(key, val)
				changed = True
		if changed:
			doc.save(ignore_permissions=True)
		return
	doc = frappe.new_doc("Custom DocPerm")
	doc.parent = PO_DOCTYPE
	doc.role = ROLE_NAME
	doc.permlevel = 0
	for key, val in PO_PERMISSION.items():
		doc.set(key, val)
	doc.insert(ignore_permissions=True)
	frappe.logger("ewcp_bridge").info(
		f"fixtures: granted {ROLE_NAME} create+write+read on {PO_DOCTYPE} (no submit)"
	)


def _ensure_custom_fields() -> None:
	from frappe.custom.doctype.custom_field.custom_field import (
		create_custom_fields,
	)

	create_custom_fields(CUSTOM_FIELDS, update=True)


def install() -> None:
	"""Idempotent fixture install — called by after_install/after_migrate."""

	_ensure_custom_fields()
	_ensure_role()
	_ensure_custom_docperm()
	frappe.clear_cache(doctype=PO_DOCTYPE)
