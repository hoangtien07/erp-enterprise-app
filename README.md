# erp-enterprise-app

Custom Frappe app cho EWCP stack: chứa `erp-ewcp-bridge` module
(scoped reads, whitelisted atomic writes, idempotency, Company-scope
hardening) + tùy biến nghiệp vụ VN.

Governance: `docs/architecture/REPOSITORY_MAP.md` trong
`enterprise-work-control-plane`. ERPNext core không sửa — mọi
customization sống trong app này. Spec: ERPNext council
`docs/reviews/erpnext-council-2026-10/` (3 build-items W2).
