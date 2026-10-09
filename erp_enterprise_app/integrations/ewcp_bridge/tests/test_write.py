"""A5b threat-matrix tests for ``ewcp_bridge.write.create_draft_po``.

Runs against the live G1′ site (fixtures seeded by
``erp-platform-deploy/scripts/seed_dev.py``): companies A/B, Supplier A1,
Items 001/002, service user ``ewcp-agent@ewcp.dev`` with roles
``Accounts User`` + ``EWCP Write`` and UP ``Company→A``.

Covers stage-plan §8 ERP-side rows: replay (2/4/5), wrong company (6),
injection (9), business-rule rollback (10), submit via generic surfaces
(11), guest/unauth (13), policy mismatch (16), draft assertion (17),
expired proposal (8 shape), mutated payload post-key (1 ERP-side twin).

Run:
    bench --site ewcp-dev.localhost run-tests \
        --module erp_enterprise_app.integrations.ewcp_bridge.tests.test_write
"""

import unittest.mock

import frappe
import frappe.desk.form.save
from frappe.tests.classes.integration_test_case import IntegrationTestCase
from frappe.utils import add_days, nowdate

from erp_enterprise_app.integrations.ewcp_bridge import idempotency, write
from erp_enterprise_app.integrations.ewcp_bridge.errors import (
	EWCPBridgeError,
	IdempotencyConflict,
	NotFound,
	PermissionDenied,
	PolicyViolation,
	ProposalExpired,
	SchemaInvalid,
)

AGENT = "ewcp-agent@ewcp.dev"
COMPANY_A = "EWCP Dev Company A"
COMPANY_B = "EWCP Dev Company B"
SUPPLIER_A = "EWCP Dev Supplier A1"
ITEM = "EWCP-ITEM-001"
ITEM2 = "EWCP-ITEM-002"


def _hash(n: int) -> str:
	return f"{n:064x}"


def _payload(key: str, n: int = 1, **over) -> dict:
	p = {
		"version": "1",
		"action_type": "erp.draft_po",
		"capability_id": "pack:erp_write_po",
		"method": "create_draft_po",
		"tenant_id": "demo",
		"company": COMPANY_A,
		"supplier": SUPPLIER_A,
		"currency": "USD",
		"transaction_date": nowdate(),
		"items": [{"item_code": ITEM, "qty": 2, "rate": 10}],
		"source_refs": {"workrun_id": f"wr-{n:08x}"},
		"requester": "user:pha@tenant:demo",
		"permission_policy": {
			"role": "EWCP Write",
			"user_permissions": {"Company": [COMPANY_A]},
			"strict_user_permissions": True,
		},
		"idempotency_key": key,
		"expires_at": frappe.utils.now_datetime().timestamp() + 3600,
		"payload_hash": _hash(n),
		"decision": {
			"decision_id": f"dec-{n}",
			"approved_by": "user:tien@tenant:demo",
			"approved_at": "2026-10-09T00:00:00Z",
			"payload_hash": _hash(n),
		},
	}
	for k, v in over.items():
		if v is ...:
			p.pop(k, None)
		else:
			p[k] = v
	return p


class TestCreateDraftPO(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for name in frappe.get_all(
			"Purchase Order", filters={"ewcp_idempotency_key": ("like", "t-%")}, pluck="name"
		):
			frappe.delete_doc("Purchase Order", name, force=True, ignore_permissions=True)
		frappe.set_user(AGENT)

	def _po_count(self, key: str) -> int:
		return frappe.db.count("Purchase Order", {"ewcp_idempotency_key": key})

	# --- §8.17 + happy path -------------------------------------------------

	def test_happy_path_creates_draft_po(self):
		res = write.create_draft_po(_payload("t-happy"))
		self.assertFalse(res["replayed"])
		self.assertEqual(res["docstatus"], 0)
		self.assertEqual(res["ewcp_idempotency_key"], "t-happy")
		po = frappe.get_doc("Purchase Order", res["po_name"])
		self.assertEqual(po.docstatus, 0)
		self.assertEqual(po.company, COMPANY_A)
		self.assertEqual(po.supplier, SUPPLIER_A)
		self.assertEqual(po.ewcp_requester, "user:pha@tenant:demo")
		self.assertEqual(po.ewcp_approved_by, "user:tien@tenant:demo")
		self.assertEqual(po.ewcp_payload_hash, _hash(1))
		self.assertGreater(po.grand_total, 0)
		self.assertEqual(po.owner, AGENT)

	# --- §8.2/4/5 replay ----------------------------------------------------

	def test_replay_same_payload_returns_winner_no_second_po(self):
		r1 = write.create_draft_po(_payload("t-replay"))
		r2 = write.create_draft_po(_payload("t-replay"))
		self.assertTrue(r2["replayed"])
		self.assertEqual(r2["po_name"], r1["po_name"])
		self.assertEqual(self._po_count("t-replay"), 1)

	def test_two_worker_collision_converges(self):
		# simulate the race the pre-check can't see: patch it to miss, then
		# let the unique index serialize the second insert.
		winner = write.create_draft_po(_payload("t-race"))
		with unittest.mock.patch.object(
			write.idempotency, "check_preexisting", return_value=None
		):
			r2 = write.create_draft_po(_payload("t-race"))
		self.assertTrue(r2["replayed"])
		self.assertEqual(r2["po_name"], winner["po_name"])
		self.assertEqual(self._po_count("t-race"), 1)

	def test_same_key_different_payload_conflicts(self):
		write.create_draft_po(_payload("t-conflict", n=1))
		mutated = _payload("t-conflict", n=2)
		mutated["items"] = [{"item_code": ITEM, "qty": 5, "rate": 10}]
		with self.assertRaises(IdempotencyConflict):
			write.create_draft_po(mutated)
		self.assertEqual(self._po_count("t-conflict"), 1)

	# --- §8.6 company scope --------------------------------------------------

	def test_wrong_company_denied_no_po(self):
		bad = _payload("t-wrongco")
		bad["company"] = COMPANY_B
		bad["permission_policy"]["user_permissions"] = {"Company": [COMPANY_A]}
		with self.assertRaises(PermissionDenied):
			write.create_draft_po(bad)
		self.assertEqual(self._po_count("t-wrongco"), 0)

	def test_policy_declared_scope_not_covering_write_denies(self):
		# declared policy says Company B allowed, actual UP only covers A →
		# verify fails even though the write itself is in-scope.
		bad = _payload("t-policy")
		bad["permission_policy"]["user_permissions"] = {"Company": [COMPANY_B]}
		with self.assertRaises(PolicyViolation):
			write.create_draft_po(bad)
		self.assertEqual(self._po_count("t-policy"), 0)

	def test_policy_declared_wrong_role_denies(self):
		bad = _payload("t-policyrole")
		bad["permission_policy"]["role"] = "Purchase User"
		with self.assertRaises(PolicyViolation):
			write.create_draft_po(bad)

	# --- §8.13 missing role / guest -----------------------------------------

	def test_missing_role_denied(self):
		user = "ewcp-nowrite@ewcp.dev"
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user,
					"first_name": "NoWrite",
					"enabled": 1,
					"user_type": "System User",
					"send_welcome_email": 0,
					"roles": [{"role": "Accounts User"}],
				}
			).insert(ignore_permissions=True)
		frappe.set_user(user)
		try:
			with self.assertRaises(PermissionDenied):
				write.create_draft_po(_payload("t-norole"))
		finally:
			frappe.set_user(AGENT)
		self.assertEqual(self._po_count("t-norole"), 0)

	def test_guest_denied(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(PermissionDenied):
				write.create_draft_po(_payload("t-guest"))
		finally:
			frappe.set_user(AGENT)

	# --- §8.9 injection ------------------------------------------------------

	def test_injected_fields_ignored(self):
		p = _payload("t-inject")
		p["docstatus"] = 1
		p["ignore_permissions"] = True
		p["run_method"] = "submit"
		p["doctype"] = "Sales Invoice"
		p["items"][0]["evil_field"] = "x"
		res = write.create_draft_po(p)
		po = frappe.get_doc("Purchase Order", res["po_name"])
		self.assertEqual(po.docstatus, 0)
		self.assertFalse(hasattr(po.items[0], "evil_field"))

	# --- §8.10 business-rule rollback ----------------------------------------

	def test_business_rule_failure_full_rollback(self):
		# schedule_date < transaction_date violates validate_schedule_date
		# inside doc.insert() — the whole request txn must roll back: no
		# parent, no child rows, no idempotency record.
		bad = _payload("t-rollback")
		bad["items"] = [
			{
				"item_code": ITEM,
				"qty": 1,
				"rate": 5,
				"schedule_date": add_days(nowdate(), -7),
			}
		]
		with self.assertRaises(Exception):
			write.create_draft_po(bad)
		self.assertEqual(self._po_count("t-rollback"), 0)
		self.assertEqual(
			frappe.db.count(
				"Purchase Order Item", filters={"item_code": ITEM, "rate": 5}
			),
			0,
		)

	def test_nonexistent_supplier_rejected_before_insert(self):
		bad = _payload("t-badsup")
		bad["supplier"] = "No Such Supplier Co"
		with self.assertRaises(NotFound):
			write.create_draft_po(bad)
		self.assertEqual(self._po_count("t-badsup"), 0)

	def test_qty_zero_rejected(self):
		bad = _payload("t-zeroqty")
		bad["items"] = [{"item_code": ITEM, "qty": 0, "rate": 10}]
		with self.assertRaises(SchemaInvalid):
			write.create_draft_po(bad)

	# --- §8.11 submit via generic surfaces -----------------------------------

	def test_submit_and_cancel_denied_for_write_role(self):
		res = write.create_draft_po(_payload("t-submit"))
		po_name = res["po_name"]
		doc_dict = {"doctype": "Purchase Order", "name": po_name}
		# submit paths: EWCP Write role has no `submit` ptype → PermissionError
		for fn in (
			lambda: frappe.client.submit(doc_dict),
			lambda: frappe.get_doc("Purchase Order", po_name).submit(),
			lambda: frappe.desk.form.save.savedocs(
				frappe.as_json(frappe.get_doc("Purchase Order", po_name).as_dict()),
				"Submit",
			),
		):
			with self.assertRaises(frappe.PermissionError):
				fn()
		# cancel on a *draft* can't even transition docstatus — the role
		# also lacks `cancel`, so either way the write stays a draft.
		with self.assertRaises(Exception):
			frappe.client.cancel("Purchase Order", po_name)
		po = frappe.get_doc("Purchase Order", po_name)
		self.assertEqual(po.docstatus, 0)

	# --- §8.8 expiry ----------------------------------------------------------

	def test_expired_proposal_rejected(self):
		p = _payload("t-expired")
		p["expires_at"] = frappe.utils.now_datetime().timestamp() - 10
		with self.assertRaises(ProposalExpired):
			write.create_draft_po(p)
		self.assertEqual(self._po_count("t-expired"), 0)

	# --- §8.1 shape: decision hash binding ------------------------------------

	def test_decision_hash_mismatch_rejected(self):
		p = _payload("t-hashmis")
		p["decision"]["payload_hash"] = _hash(99)
		with self.assertRaises(EWCPBridgeError) as ctx:
			write.create_draft_po(p)
		self.assertIn("proposal_hash_mismatch", str(ctx.exception))
		self.assertEqual(self._po_count("t-hashmis"), 0)

	# --- schema shape ---------------------------------------------------------

	def test_missing_idempotency_key_rejected(self):
		p = _payload("t-nokey", idempotency_key=...)
		with self.assertRaises(SchemaInvalid):
			write.create_draft_po(p)

	def test_wrong_method_literal_rejected(self):
		p = _payload("t-method", method="delete_everything")
		with self.assertRaises(SchemaInvalid):
			write.create_draft_po(p)

	def test_read_back_by_key(self):
		write.create_draft_po(_payload("t-lookup"))
		row = idempotency.lookup("t-lookup")
		self.assertIsNotNone(row)
		self.assertEqual(row["ewcp_payload_hash"], _hash(1))
