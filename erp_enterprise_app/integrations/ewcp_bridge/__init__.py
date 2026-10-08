"""erp-ewcp-bridge — governed ERP access surface for EWCP kernel.

Implemented in Track A A4 (reads) / A5 (writes) per
EXECUTION_STRATEGY_VNEXT + W2 build-items:
- scoped whitelisted read methods (Company/User-Permission enforced)
- atomic whitelisted write methods (multi-step in one txn)
- idempotency layer (unique key + dup-catch + read-back)
"""
