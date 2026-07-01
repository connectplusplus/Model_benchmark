"""
Frontier benchmark tasks for enterprise IT buyers.

The original suite tested compact textbook algorithms. That is useful as a
smoke test, but it does not answer the buyer question: "When does the premium
model justify its cost?" This suite keeps one calibration task, then moves into
ambiguous, stateful, policy-heavy work that rewards robust reasoning.

Each task provides:
  - prompt: fixed implementation contract given to every model
  - test_code: deterministic edge-case checks appended to the model code
  - forbidden: banned shortcuts
  - weight: contribution to the buyer-facing frontier score
"""

CODE_ONLY = (
    "Respond with a single Python code block and nothing else: no explanation, "
    "no prose before or after. Define exactly the requested function or class. "
    "Use only the Python standard library."
)

TASKS = [
    {
        "id": "calibration_acl_merge",
        "level": "calibration",
        "weight": 1,
        "buyer_signal": "Baseline correctness on a compact deterministic task.",
        "prompt": (
            "Implement `def effective_acl(entries: list[dict]) -> dict[str, set[str]]`.\n\n"
            "Each entry has `principal`, `action`, and `effect`, where effect is "
            "`allow` or `deny`. Return a mapping from principal to the set of "
            "actions that remain allowed after processing all entries. Denies "
            "override allows for the same principal and action, even if an allow "
            "appears later. Ignore malformed entries missing any required key.\n\n"
            + CODE_ONLY
        ),
        "forbidden": [],
        "test_code": r'''
entries = [
    {"principal": "team-a", "action": "read", "effect": "allow"},
    {"principal": "team-a", "action": "write", "effect": "allow"},
    {"principal": "team-a", "action": "write", "effect": "deny"},
    {"principal": "team-a", "action": "write", "effect": "allow"},
    {"principal": "team-b", "action": "read", "effect": "deny"},
    {"principal": "team-b", "action": "read", "effect": "allow"},
    {"principal": "team-c", "action": "deploy", "effect": "allow"},
    {"principal": "broken", "effect": "allow"},
]
out = effective_acl(entries)
normalized = {principal: actions for principal, actions in out.items() if actions}
assert normalized == {"team-a": {"read"}, "team-c": {"deploy"}}, out
assert effective_acl([]) == {}
print("ALL_TESTS_PASSED")
''',
    },
    {
        "id": "enterprise_incident_correlator",
        "level": "advanced",
        "weight": 3,
        "buyer_signal": "Turns noisy operational telemetry into a defensible incident timeline.",
        "prompt": (
            "Implement `def correlate_incidents(events: list[dict], window_seconds: int = 300) -> list[dict]`.\n\n"
            "You are correlating enterprise monitoring events. Each event may contain:\n"
            "  - `ts`: ISO-8601 timestamp ending in Z, or integer epoch seconds\n"
            "  - `service`: service name\n"
            "  - `host`: host name\n"
            "  - `severity`: one of info, warning, error, critical\n"
            "  - `message`: free text\n"
            "  - `fingerprint`: optional stable incident key\n\n"
            "Rules:\n"
            "  1. Ignore malformed events missing ts, service, host, severity, or message.\n"
            "  2. Normalize timestamps to epoch seconds.\n"
            "  3. Events belong to the same incident when they share a fingerprint. If no "
            "fingerprint is present, group by service and a normalized message signature: "
            "lowercase, replace digit runs with #, collapse whitespace, strip punctuation "
            "at word boundaries.\n"
            "  4. Within each group, split into separate incidents when the gap between "
            "consecutive events is greater than window_seconds.\n"
            "  5. Return incidents sorted by start_ts, then service, then id. Each incident "
            "dict must contain id, service, hosts, count, max_severity, start_ts, end_ts, "
            "messages. Hosts and messages are sorted unique lists. id must be stable and "
            "deterministic from service/signature/start_ts, not a random UUID.\n"
            "  6. Severity order is info < warning < error < critical.\n\n"
            + CODE_ONLY
        ),
        "forbidden": ["uuid", "random"],
        "test_code": r'''
events = [
    {"ts": "2026-06-30T10:00:00Z", "service": "payments", "host": "p1", "severity": "error", "message": "DB timeout after 1200 ms"},
    {"ts": "2026-06-30T10:03:00Z", "service": "payments", "host": "p2", "severity": "critical", "message": "db timeout after 900 ms!!!"},
    {"ts": "2026-06-30T10:09:01Z", "service": "payments", "host": "p3", "severity": "error", "message": "DB timeout after 700 ms"},
    {"ts": 1782813660, "service": "auth", "host": "a1", "severity": "warning", "message": "token issuer lag 42", "fingerprint": "issuer-lag"},
    {"ts": "2026-06-30T10:02:00Z", "service": "auth", "host": "a2", "severity": "error", "message": "different words", "fingerprint": "issuer-lag"},
    {"ts": "bad", "service": "auth", "host": "a3", "severity": "error", "message": "ignore"},
]
out = correlate_incidents(events, window_seconds=300)
assert len(out) == 3, out
first = out[0]
assert first["service"] == "payments"
assert first["hosts"] == ["p1", "p2"], first
assert first["count"] == 2 and first["max_severity"] == "critical", first
assert first["start_ts"] == 1782813600 and first["end_ts"] == 1782813780, first
assert first["messages"] == ["DB timeout after 1200 ms", "db timeout after 900 ms!!!"], first
assert out[1]["service"] == "auth" and out[1]["count"] == 2, out[1]
assert out[2]["service"] == "payments" and out[2]["hosts"] == ["p3"], out[2]
assert correlate_incidents(list(reversed(events)), 300) == out
print("ALL_TESTS_PASSED")
''',
    },
    {
        "id": "frontier_policy_engine",
        "level": "frontier",
        "weight": 5,
        "buyer_signal": "Models enterprise authorization with deny precedence, inheritance, conditions, and audit explanations.",
        "prompt": (
            "Implement `def authorize(subject: dict, action: str, resource: dict, policies: list[dict], context: dict) -> dict`.\n\n"
            "Return `{'decision': 'allow'|'deny', 'matched': list[str], 'reason': str}`.\n\n"
            "Policy schema:\n"
            "  - id: stable policy id\n"
            "  - effect: allow or deny\n"
            "  - subjects: list of subject ids, roles like `role:admin`, departments like "
            "`department:finance`, or `*`\n"
            "  - actions: list of exact actions or wildcards ending in `*`, such as `invoice:*`\n"
            "  - resources: list of exact resource ids, resource types like `type:invoice`, "
            "tags like `tag:regulated`, or `*`\n"
            "  - conditions: optional dict. Supported keys are `ip_cidr`, `mfa`, `before`, "
            "`after`, `resource_owner`, and `risk_lte`.\n\n"
            "Subject has id, roles, department, and optional clearance. Resource has id, type, "
            "owner, tags, and optional classification. Context has ip, mfa, now, and risk.\n\n"
            "Semantics:\n"
            "  1. A policy matches only when subject, action, resource, and all conditions match.\n"
            "  2. Deny overrides allow. If any deny matches, decision is deny.\n"
            "  3. If no deny matches and one or more allows match, decision is allow.\n"
            "  4. Otherwise deny by default.\n"
            "  5. `before` and `after` compare ISO-8601 UTC timestamps lexically after parsing.\n"
            "  6. `resource_owner: true` requires subject.id == resource.owner.\n"
            "  7. `risk_lte` requires numeric context risk <= threshold.\n"
            "  8. `matched` must include every policy whose subject, action, resource, and "
            "conditions match, sorted by original policy order. Include both allow and deny "
            "matches even when deny_override determines the final decision, because the caller "
            "needs a complete audit trail. Reason should briefly name deny_override, allowed, "
            "or default_deny.\n\n"
            + CODE_ONLY
        ),
        "forbidden": ["eval(", "exec(", "compile("],
        "test_code": r'''
policies = [
    {"id": "p1", "effect": "allow", "subjects": ["role:analyst"], "actions": ["invoice:read"], "resources": ["type:invoice"], "conditions": {"mfa": True, "risk_lte": 40}},
    {"id": "p2", "effect": "deny", "subjects": ["department:contractor"], "actions": ["invoice:*"], "resources": ["tag:regulated"]},
    {"id": "p3", "effect": "allow", "subjects": ["u-owner"], "actions": ["invoice:update"], "resources": ["type:invoice"], "conditions": {"resource_owner": True, "ip_cidr": "10.0.0.0/8"}},
    {"id": "p4", "effect": "deny", "subjects": ["*"], "actions": ["invoice:delete"], "resources": ["*"], "conditions": {"after": "2026-06-30T18:00:00Z"}},
]
subject = {"id": "u1", "roles": ["analyst"], "department": "finance"}
resource = {"id": "inv-7", "type": "invoice", "owner": "u-owner", "tags": ["regulated"]}
ctx = {"ip": "10.2.3.4", "mfa": True, "now": "2026-06-30T12:00:00Z", "risk": 25}
assert authorize(subject, "invoice:read", resource, policies, ctx)["decision"] == "allow"
contractor = {"id": "u2", "roles": ["analyst"], "department": "contractor"}
r = authorize(contractor, "invoice:read", resource, policies, ctx)
assert r["decision"] == "deny" and r["matched"] == ["p1", "p2"] and "deny" in r["reason"], r
owner = {"id": "u-owner", "roles": [], "department": "finance"}
r = authorize(owner, "invoice:update", resource, policies, ctx)
assert r["decision"] == "allow" and r["matched"] == ["p3"], r
r = authorize(owner, "invoice:update", resource, policies, {**ctx, "ip": "192.168.1.5"})
assert r["decision"] == "deny" and r["matched"] == [], r
r = authorize(subject, "invoice:delete", resource, policies, {**ctx, "now": "2026-06-30T19:00:00Z"})
assert r["decision"] == "deny" and r["matched"] == ["p4"], r
r = authorize(subject, "invoice:read", resource, policies, {**ctx, "risk": 41})
assert r["decision"] == "deny" and r["matched"] == [], r
print("ALL_TESTS_PASSED")
''',
    },
    {
        "id": "frontier_migration_planner",
        "level": "frontier",
        "weight": 5,
        "buyer_signal": "Plans a safe production migration under dependency, rollback, and zero-downtime constraints.",
        "prompt": (
            "Implement `def plan_migration(current: dict, target: dict, constraints=None) -> list`.\n\n"
            "Inputs describe database schemas. `current` and `target` have a `tables` dict. "
            "Each table maps to `columns`, `primary_key`, `foreign_keys`, and optional `indexes`.\n"
            "A column has type, nullable, default, and optional renamed_from. A foreign key has "
            "columns, ref_table, ref_columns, and optional name.\n\n"
            "Return an ordered list of operation dicts. Required operation types: "
            "add_table, drop_table, add_column, backfill_column, alter_nullable, add_index, "
            "add_foreign_key, validate_foreign_key, drop_column. Every operation needs a "
            "stable `id`, `type`, `table`, `risk` low|medium|high, and `rollback` string.\n\n"
            "Rules:\n"
            "  1. Add parent tables before child foreign keys.\n"
            "  2. For a new NOT NULL column without a default on an existing table, emit "
            "add_column as nullable, then backfill_column, then alter_nullable.\n"
            "  3. Add indexes before validating foreign keys that use those columns.\n"
            "  4. New foreign keys should be emitted as add_foreign_key then validate_foreign_key.\n"
            "  5. Dropping a column or table is high risk and must happen after additive changes.\n"
            "  6. If constraints contains zero_downtime=True, do not emit a direct high-risk "
            "drop operation unless the target object has `defer_drop: False`; otherwise include "
            "a deferred drop operation with risk medium.\n"
            "  7. Be deterministic: same input means exactly same list.\n\n"
            + CODE_ONLY
        ),
        "forbidden": ["eval(", "exec(", "compile("],
        "test_code": r'''
current = {"tables": {
    "customers": {"columns": {"id": {"type": "int", "nullable": False}, "email": {"type": "text", "nullable": False}}, "primary_key": ["id"], "foreign_keys": [], "indexes": []},
    "orders": {"columns": {"id": {"type": "int", "nullable": False}, "customer_id": {"type": "int", "nullable": False}, "legacy_code": {"type": "text", "nullable": True}}, "primary_key": ["id"], "foreign_keys": [], "indexes": []},
}}
target = {"tables": {
    "customers": {"columns": {"id": {"type": "int", "nullable": False}, "email": {"type": "text", "nullable": False}}, "primary_key": ["id"], "foreign_keys": [], "indexes": []},
    "orders": {"columns": {
        "id": {"type": "int", "nullable": False},
        "customer_id": {"type": "int", "nullable": False},
        "status": {"type": "text", "nullable": False},
    }, "primary_key": ["id"], "foreign_keys": [
        {"name": "fk_orders_customer", "columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["id"]}
    ], "indexes": [{"name": "idx_orders_customer_id", "columns": ["customer_id"]}]},
    "shipments": {"columns": {"id": {"type": "int", "nullable": False}, "order_id": {"type": "int", "nullable": False}}, "primary_key": ["id"], "foreign_keys": [
        {"name": "fk_shipments_order", "columns": ["order_id"], "ref_table": "orders", "ref_columns": ["id"]}
    ], "indexes": [{"name": "idx_shipments_order_id", "columns": ["order_id"]}]},
}}
ops = plan_migration(current, target, {"zero_downtime": True})
types = [op["type"] for op in ops]
assert types.count("add_table") == 1 and any(op["table"] == "shipments" for op in ops if op["type"] == "add_table"), ops
status_ops = [op["type"] for op in ops if op["table"] == "orders" and op.get("column") == "status"]
assert status_ops == ["add_column", "backfill_column", "alter_nullable"], status_ops
assert types.index("add_index") < types.index("validate_foreign_key"), types
drop_ops = [op for op in ops if op["type"] == "drop_column"]
assert drop_ops and drop_ops[0]["risk"] == "medium" and drop_ops[0].get("deferred") is True, drop_ops
for op in ops:
    assert {"id", "type", "table", "risk", "rollback"} <= set(op), op
assert plan_migration(current, target, {"zero_downtime": True}) == ops
print("ALL_TESTS_PASSED")
''',
    },
]
