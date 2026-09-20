# Typed tools and transactional safety

## Tool surface

| Tool | Capability | State effect |
|---|---|---|
| `search_catalog` | Retrieve product candidates | Read |
| `get_product` | Read one canonical product | Read |
| `compare_products` | Compare canonical products | Read |
| `get_policy` | Retrieve one policy category | Read |
| `get_order` | Read an authenticated order | Read |
| `check_return_eligibility` | Evaluate return rules | Read |
| `create_return_request` | Create an idempotent return request | Write |
| `escalate_to_human` | Record a handoff | Controlled action |

## Guardrail chain

High-risk return operations require all of the following:

1. the session identity and six-digit verification code match;
2. the requested order belongs to that user;
3. the order satisfies the return policy;
4. the user has explicitly confirmed the write;
5. the requested write is valid and idempotent.

The harness injects session identity independently of model-provided arguments, so the policy cannot operate on behalf of another user by changing `user_id`.

## Trusted confirmation boundary

Transactional writes require two independent facts: the legacy typed argument
`confirmed=true` and a trusted `ConfirmationLedger` authorization. The latter
is created only by the host interaction path after it presents a concrete
operation to the user and records the user's response. It binds the session,
authenticated user, operation name, and canonical business parameters (for
example, order id and verification code); it is not exposed in the model tool
schema. A bare affirmative without a pending request, a refusal, a target
change, a parameter change, or a cross-session/user replay has no authorization.

`RetailTools.call` and every transactional method fail closed without the
record. MCP writes use the same path and therefore reject `confirmed=true`
unless a trusted host callback has first issued and confirmed the matching
request. SQLite conditional updates remain the idempotency boundary, so a
legitimate retry can return `idempotent_replay=true` without incrementing the
business version a second time.

`RetailTools.call` is also the final typed dispatch boundary: it revalidates the
JSON Schema before invoking a method. This matters for Direct callers and
diagnostics that do not pass through a model parser; in particular, a string
such as `"false"` is not accepted as boolean `false`. The MCP confirmation
callback binds the configured server-side user after merging callback
arguments, so an input argument cannot replace that identity.

The return-window date is frozen by `ERAG_SIMULATED_TODAY` (default
`2026-07-20`) and is read when `RetailTools` is constructed. Freeze manifests
and tool execution therefore use the same date configuration.

## Safety interpretation

Agent v2 recorded a 5% forbidden-tool attempt rate and a 0% illegal-state-change rate. This means the execution layer protected the database even when the policy selected an invalid action; it does not mean policy compliance was perfect.
