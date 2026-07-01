# CIO Guide To The Frontier Benchmark

This benchmark is designed to answer a practical buying question:

**When is a more powerful AI model worth paying for?**

The tests are not generic coding puzzles. They represent enterprise IT work where
the cost of a bad answer is not just an extra API call. It can mean a delayed
migration, a missed incident, an unsafe access-control decision, or more senior
engineering review.

## How To Read The Weights

Each test has a weight. Higher weight means the task is more representative of
high-value enterprise work.

| Weight | Meaning |
|---|---|
| `w1` | Calibration: confirms the model can follow a small deterministic spec. |
| `w3` | Advanced operational reasoning with noisy real-world inputs. |
| `w5` | Frontier enterprise task where failure has meaningful business or security risk. |

The benchmark score is weighted so that frontier tasks matter more than simple
tasks. This prevents a model from looking strong just because it solves easy work
cheaply.

## Test 1: `calibration_acl_merge` `w1`

**Plain-English version:**  
Can the model correctly combine allow and deny access-control entries?

**Business scenario:**  
An IT team has a list of permissions. Some entries grant access, and some revoke
it. The model must calculate the final effective access for each team.

**What it tests:**

- Basic instruction following
- Correct handling of deny-overrides-allow
- Ignoring malformed records
- Producing deterministic, simple code

**Why CIOs should care:**  
This is a baseline sanity check. Every serious model should pass it. It should
not be the reason to buy a premium model, but failing it is a warning sign.

**What a failure means:**  
The model may struggle with even simple access-control logic or edge cases.

## Test 2: `enterprise_incident_correlator` `w3`

**Plain-English version:**  
Can the model turn noisy monitoring events into clean incident timelines?

**Business scenario:**  
Your monitoring tools produce repeated alerts from multiple hosts and services.
The model must group related events, split unrelated ones, normalize timestamps,
track severity, and produce a stable incident summary.

**What it tests:**

- Handling messy operational data
- Grouping related alerts without over-merging unrelated ones
- Timestamp normalization
- Severity ranking
- Stable, repeatable incident IDs
- Deterministic output regardless of input order

**Why CIOs should care:**  
Incident response depends on signal quality. A model that incorrectly groups
alerts can hide a real outage or create noise that wastes staff time.

**What a failure means:**  
The model may not be reliable enough for operations, SRE, SOC, or ITSM workflows
where correctness and repeatability matter.

## Test 3: `frontier_policy_engine` `w5`

**Plain-English version:**  
Can the model implement enterprise authorization logic with a complete audit
trail?

**Business scenario:**  
The model must decide whether a user can perform an action on a resource. It must
respect roles, departments, resource tags, IP ranges, MFA, risk scores, ownership,
time windows, and deny-overrides-allow behavior.

**What it tests:**

- Security-sensitive reasoning
- Deny precedence
- Role, department, resource, and tag matching
- Conditional access policies
- Complete audit explanations
- Default-deny behavior

**Why CIOs should care:**  
Authorization is a high-risk domain. The model must not only make the right
decision, it must explain which policies matched so security teams can audit the
decision.

**What a failure means:**  
The model may produce access-control logic that is unsafe, incomplete, or hard to
defend in an audit. Even when the final allow/deny decision is correct, missing
audit details can be unacceptable in regulated environments.

## Test 4: `frontier_migration_planner` `w5`

**Plain-English version:**  
Can the model plan a safe production database migration?

**Business scenario:**  
The current and target database schemas differ. The model must produce an ordered
migration plan that respects dependencies, avoids unsafe downtime, stages
backfills, validates foreign keys, and includes rollback guidance.

**What it tests:**

- Long-range planning across many constraints
- Dependency ordering
- Zero-downtime migration safety
- Risk classification
- Rollback awareness
- Avoiding destructive operations too early
- Completing the answer inside the token budget

**Why CIOs should care:**  
Production migrations are expensive and risky. A model that produces an
incomplete or unsafe plan can delay projects or create operational incidents.

**What a failure means:**  
Failures here are especially important. If a model runs out of tokens, that is
not just a formatting issue. It means the model could not complete a complex
enterprise task within the agreed budget. For a CIO, that translates into higher
latency, unpredictable cost, and lower workflow reliability.

## How To Interpret Model Differences

Use the lower-cost model when it achieves the same weighted frontier score on the
tasks that matter to your organization.

Choose the more powerful model when it materially improves:

- completion reliability
- auditability
- safety under complex constraints
- output discipline under token limits
- correctness on high-risk workflows

The key enterprise question is not:

**"Which model is cheapest per token?"**

It is:

**"Which model completes high-risk work correctly, consistently, and within the
operational budget?"**

## Why Token Exhaustion Counts As A Real Failure

If a model hits the output-token limit and stops mid-answer, the benchmark records
`token_budget_exhausted:max_tokens`.

That is counted as a real failure because incomplete work is not usable work.
For enterprise buyers, token exhaustion indicates:

- unpredictable operating cost
- longer runtime
- more manual review
- higher chance of broken automation
- lower confidence in unattended workflows

This is one of the clearest reasons a CIO might choose a more capable model even
when a cheaper model passes simpler tests.
