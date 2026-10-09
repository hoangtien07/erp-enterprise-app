"""erp-ewcp-bridge — governed ERP access surface for EWCP kernel.

Implemented surfaces:
- A5b governed write: ``write.create_draft_po`` (whitelisted POST) —
  draft Purchase Order only, role+scope fail-closed, exactly-once via the
  ``ewcp_idempotency_key`` unique field. See ``docs/A5B_BRIDGE.md``.
- fixtures: ``fixtures.install`` deploys the ``EWCP Write`` role, PO
  custom fields, and the Custom DocPerm (create+write+read, no submit).

Read side (A4) is contract-only; see ``docs/A4A_READ_CONTRACT.md``.
"""
