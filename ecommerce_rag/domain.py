"""Public, serialisable contracts shared by tools and the agent harness."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CatalogRecord:
    record_id: str
    source_type: str
    title: str
    text: str
    category: str = ""
    product_id: str | None = None
    price: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    category: str
    user_id: str
    user_goal: str
    seed: int
    gold_doc_ids: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    expected_state: dict[str, Any] = field(default_factory=dict)
    initial_state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    split: str = "smoke"
    # Hidden evaluation-only requirements. They must never be copied into an
    # AgentObservation or prompt.
    answer_expectations: dict[str, Any] = field(default_factory=dict)
    expected_tool_sequence: list[str] = field(default_factory=list)
    # Versioned scoring contract.  The default preserves historical task/report
    # semantics; return-closure-v2 is opt-in for the new experiment set.
    scoring_version: str = "harness-v1"
    # Optional read-only research resource limit. It is not an answer hint.
    research_budget: int | None = None
    # Human/evaluator-facing contract kept on TaskSpec and never copied into
    # AgentObservation or a provider prompt.
    evaluation_contract: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentObservation:
    """The complete policy-visible state.

    Gold documents, expected tools, terminal state and task category deliberately
    live only on :class:`TaskSpec` and must never be copied here.
    """

    current_message: str
    session: dict[str, Any]
    history: list[dict[str, Any]] = field(default_factory=list)
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    # Derived only from tool results already visible in ``history``. This is not
    # gold data; evidence-aware policies receive the normalized form so answer
    # citations remain stable across heterogeneous tools.
    evidence_ledger: list[dict[str, Any]] = field(default_factory=list)
    # Derived from history/evidence_ledger; this is not a second business-state
    # store and is empty for the legacy/baseline observation path.
    research_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentAction:
    action_type: str  # tool_call | final_answer | handoff
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    content: str = ""
    requires_user_response: bool = False
    # Optional typed request metadata from a control action.  The harness keeps
    # its legacy content classifier as a fallback for older policies, but native
    # ``request_user_input`` calls must not lose their declared input type.
    requested_input_type: str | None = None

    @classmethod
    def tool_call(cls, name: str, **arguments: Any) -> "AgentAction":
        return cls("tool_call", tool_name=name, arguments=arguments)

    @classmethod
    def answer(
        cls,
        content: str,
        *,
        requires_user_response: bool = False,
        requested_input_type: str | None = None,
    ) -> "AgentAction":
        return cls(
            "final_answer",
            content=content,
            requires_user_response=requires_user_response,
            requested_input_type=requested_input_type,
        )

    @classmethod
    def handoff(cls, reason: str, **arguments: Any) -> "AgentAction":
        return cls("handoff", arguments={"reason": reason, **arguments})


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str
    result: dict[str, Any]
    started_at: str
    latency_ms: float = 0.0
    error: str | None = None


@dataclass
class Trajectory:
    trajectory_id: str
    task_id: str
    seed: int
    messages: list[dict[str, str]] = field(default_factory=list)
    retrievals: list[dict[str, Any]] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    guardrail_spans: list[dict[str, Any]] = field(default_factory=list)
    handoff_spans: list[dict[str, Any]] = field(default_factory=list)
    final_answer: str = ""
    final_state: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    estimated_cost: float = 0.0
    policy_name: str = "unknown"
    observations: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    retry_spans: list[dict[str, Any]] = field(default_factory=list)
    user_simulator_spans: list[dict[str, Any]] = field(default_factory=list)
    confirmation_spans: list[dict[str, Any]] = field(default_factory=list)
    evidence_ledger: list[dict[str, Any]] = field(default_factory=list)
    verification_spans: list[dict[str, Any]] = field(default_factory=list)
    repair_spans: list[dict[str, Any]] = field(default_factory=list)
    evidence_conversion_spans: list[dict[str, Any]] = field(default_factory=list)
    progress_spans: list[dict[str, Any]] = field(default_factory=list)
    correction_spans: list[dict[str, Any]] = field(default_factory=list)
    constraint_spans: list[dict[str, Any]] = field(default_factory=list)
    memory_spans: list[dict[str, Any]] = field(default_factory=list)
    decision_spans: list[dict[str, Any]] = field(default_factory=list)
    failed_closed: bool = False
    rejected_tool_dispatch_attempts: int = 0
    semantic_fact_spans: list[dict[str, Any]] = field(default_factory=list)
    # Auditable before/after views for multi-step evidence decisions. The
    # authoritative facts remain tool_calls and evidence_ledger.
    research_spans: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GradeResult:
    task_id: str
    success: bool
    policy_compliant: bool
    tool_precision: float
    tool_recall: float
    tool_f1: float
    handoff_expected: bool
    handoff_observed: bool
    terminal_state_match: bool
    state_diff: dict[str, Any]
    turns: int
    latency_ms: float
    reward: float
    failure_type: str | None = None
    split: str = "smoke"
    abstention_expected: bool = False
    abstention_observed: bool = False
    recovered: bool = False
    leakage_checked: bool = False
    forbidden_tool_attempt: bool = False
    illegal_state_change: bool = False
    answer_fact_applicable: bool = False
    answer_fact_pass: bool | None = None
    citation_binding_pass: bool | None = None
    required_evidence_coverage: float | None = None
    unsupported_high_risk_claims: list[dict[str, Any]] = field(default_factory=list)
    contradicted_claims: list[dict[str, Any]] = field(default_factory=list)
    omitted_required_facts: list[str] = field(default_factory=list)
    repair_attempted: bool = False
    repair_succeeded: bool = False
    joint_success: bool = False
    tool_sequence_match: bool | None = None
    hard_verification_pass: bool = True
    operational_success: bool = False
    citation_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    repair_hard_recovery: bool = False
    repair_diagnostic_improvement: bool = False
    raw_observed_tool_sequence: list[str] = field(default_factory=list)
    successful_tool_sequence: list[str] = field(default_factory=list)
    failed_or_empty_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    scoring_version: str = "harness-v1"
    required_facts_pass: bool = True
    confirmation_protocol_pass: bool = True
    unexpected_tool_attempt: bool = False
    interaction_protocol_failure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
