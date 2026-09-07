from __future__ import annotations

import json
from pathlib import Path

from ecommerce_rag.diagnostics.transaction_audit import (
    CanonicalAction,
    ReplayRunner,
    artifact_capability_audit,
    artifact_project_boundary,
    database_state,
    differential_replay,
    failure_semantics_audit,
    frozen_contract_spec,
    normalize_money,
    normalize_observation,
    run_adversarial_suite,
    state_diff,
)
from ecommerce_rag.orders import seed_database


def _account(db: Path, *, status: str = "pending") -> tuple[dict, str]:
    state = database_state(db)
    order = next(row for row in state["tables"]["orders"]["rows"] if row["status"] == status)
    user = next(row for row in state["tables"]["users"]["rows"] if row["user_id"] == order["user_id"])
    return order, str(user["verification_code"])


def test_money_and_observation_normalization_are_frozen():
    assert normalize_money(0.1 + 0.2) == "0.30"
    assert normalize_observation({"optional": None, "items": [2, 1]}) == {"items": [2, 1]}
    assert frozen_contract_spec()["money"]["available"] is False


def test_state_diff_is_field_level_and_json_columns_are_structured(tmp_path):
    db = tmp_path / "retail.db"
    seed_database(db, users=4, orders=12)
    before = database_state(db)
    order, _code = _account(db)
    assert isinstance(order["item_ids"], list)
    assert isinstance(order["shipping_address"], dict)

    after = json.loads(json.dumps(before))
    after["tables"]["orders"]["rows"][0]["version"] = 1
    diff = state_diff(before, after)
    assert list(diff) == [f"orders.{order['order_id']}.version"]


def test_guarded_cross_user_mutation_is_blocked_without_state_change(tmp_path):
    db = tmp_path / "retail.db"
    seed_database(db, users=40, orders=200)
    order, _code = _account(db)
    foreign = "U0001" if order["user_id"] != "U0001" else "U0002"
    foreign_user = next(
        row for row in database_state(db)["tables"]["users"]["rows"] if row["user_id"] == foreign
    )
    action = CanonicalAction(
        "cancel_pending_order",
        {
            "order_id": order["order_id"],
            "user_id": foreign,
            "verification_code": foreign_user["verification_code"],
            "reason": "no longer needed",
            "confirmed": True,
        },
    )
    step = ReplayRunner(db).run([action])[0]
    assert step.attempted_violation is True
    assert step.blocked_violation is True
    assert step.committed_violation is False
    assert step.state_diff == {}


def test_guardrail_off_commits_the_same_cross_user_action(tmp_path):
    guarded = tmp_path / "guarded.db"
    unsafe = tmp_path / "unsafe.db"
    seed_database(guarded, users=40, orders=200)
    seed_database(unsafe, users=40, orders=200)
    order, _code = _account(guarded)
    foreign = "U0001" if order["user_id"] != "U0001" else "U0002"
    foreign_user = next(
        row for row in database_state(guarded)["tables"]["users"]["rows"] if row["user_id"] == foreign
    )
    action = CanonicalAction(
        "cancel_pending_order",
        {
            "order_id": order["order_id"],
            "user_id": foreign,
            "verification_code": foreign_user["verification_code"],
            "reason": "no longer needed",
            "confirmed": True,
        },
    )
    step = ReplayRunner(unsafe, guardrails=False).run([action])[0]
    assert step.attempted_violation is True
    assert step.committed_violation is True
    assert step.state_diff[f"orders.{order['order_id']}.status"]["after"] == "cancelled"


def test_idempotent_replay_keeps_version_stable(tmp_path):
    db = tmp_path / "retail.db"
    seed_database(db, users=40, orders=200)
    order, code = _account(db)
    action = CanonicalAction(
        "cancel_pending_order",
        {
            "order_id": order["order_id"],
            "user_id": order["user_id"],
            "verification_code": code,
            "reason": "no longer needed",
            "confirmed": True,
        },
    )
    steps = ReplayRunner(db).run([action, action])
    assert steps[0].state_diff[f"orders.{order['order_id']}.version"]["after"] == 1
    assert steps[1].blocked_violation is True
    assert steps[1].committed_violation is False
    assert steps[1].state_diff == {}


def test_adversarial_suite_is_deterministic_and_has_required_volume():
    report = run_adversarial_suite(repetitions=2)
    assert report["execution_classification"] == {
        "total_executions": 20,
        "frozen_contract_applicable_violations": 14,
        "valid_setup_or_control_executions": 4,
        "unresolved_confirmation_binding_probes": 2,
        "equation": "20 = 14 + 4 + 2",
    }
    assert report["on"]["committed"] == 0
    assert report["off"]["committed"] > 0
    assert report["known_unresolved_gap"]["status"] == "UNRESOLVED"
    assert report["known_unresolved_gap"]["on_state_commits_without_binding"] == 2


def test_direct_mcp_differential_reports_each_surface_layer():
    report = differential_replay()
    assert report["tools_tested"] == 15
    assert report["executions"] == 42
    assert report["coverage_cases"]["missing_confirmation"] == 6
    assert report["coverage_cases"]["terminal_state_rejection"] == 3
    assert report["coverage_cases"]["invalid_item_identity"] == 3
    assert report["coverage_cases"]["idempotent_noop"] == 1
    assert report["state_mismatches"] == 0
    assert report["observation_mismatches"] == 0
    assert report["error_mismatches"] == 0


def test_failure_semantics_reconciles_historical_denominators():
    report = failure_semantics_audit("docs/harness_v2_llm_360_regraded_v2.json")
    assert report["reconciled_counts"]["operational_success"] == {
        "count": 303,
        "total": 360,
        "rate": 0.8416666666666667,
    }
    assert report["reconciled_counts"]["terminal_state_match"]["count"] == 360
    assert "write_attempt_count" in report["unavailable_denominators"]


def test_artifact_audit_does_not_promote_messages_to_state(tmp_path):
    artifact = tmp_path / "messages.json"
    artifact.write_text(
        json.dumps(
            {
                "simulations": [
                    {
                        "id": "t1",
                        "task_id": "1",
                        "messages": [
                            {"role": "assistant", "tool_calls": [{"function": {"name": "get_order", "arguments": "{}"}}]},
                            {"role": "tool", "content": "{\"status\": \"delivered\"}"},
                        ],
                        "reward_info": {"reward": 1},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report = artifact_capability_audit([artifact])
    item = report["artifacts"][0]
    assert item["tool_calls"] == 1
    assert item["tool_observations"] == 1
    assert item["classification"] == "CASE_C_MESSAGE_LEVEL_ONLY_STATE_RECONSTRUCTION_UNAVAILABLE"


def test_post_training_artifact_boundary_is_explicit():
    assert artifact_project_boundary(
        "data/simulations/recorded_results.json"
    ) == "POST_TRAINING_OUT_OF_SCOPE"
    assert artifact_project_boundary(
        "reports/provenance_inputs/base_results.json"
    ) == "POST_TRAINING_OUT_OF_SCOPE"
    assert artifact_project_boundary("docs/harness_v2_llm_360_regraded_v2.json") == "AGENT_RUNTIME_SUMMARY_ONLY"
