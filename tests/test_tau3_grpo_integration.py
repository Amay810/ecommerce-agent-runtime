from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.train_tau3_grpo as grpo_launcher
from ecommerce_rag.grpo.config import FROZEN_CONFIG
from ecommerce_rag.grpo.metrics import GroupArtifact
from ecommerce_rag.grpo.reward_adapter import (
    OfficialTerminalRewardAdapter,
    RewardAdapterError,
)
from ecommerce_rag.grpo.tau3_env_adapter import Tau3EnvironmentError, Tau3RetailEpisode
from ecommerce_rag.grpo.trajectory_schema import TokenSegment, assistant_only_loss_mask
from scripts.train_tau3_grpo import _verl_command


def test_frozen_pilot_shape_and_values():
    FROZEN_CONFIG.validate()
    assert FROZEN_CONFIG.train_tasks == 74
    assert FROZEN_CONFIG.group_size_k == 8
    assert FROZEN_CONFIG.groups_per_step_p == 2
    assert FROZEN_CONFIG.rollouts_per_step == 16
    assert FROZEN_CONFIG.total_steps == 9


def test_group_artifact_keeps_all_eight_rollouts_and_population_variance():
    artifact = GroupArtifact.create("g", "17", [0, 1, 0, 1, 0, 1, 0, 1])
    assert artifact.rollout_indices == tuple(range(8))
    assert artifact.reward_class == "mixed"
    assert artifact.mean_reward == 0.5
    assert artifact.variance == 0.25


def _valid_official_zero_info():
    return {
        "tau2_simulation_run_present": True,
        "tau2_simulation_run_complete": True,
        "tau2_official_evaluator_succeeded": True,
    }


def test_max_steps_official_zero_is_a_valid_grpo_negative():
    adapter = OfficialTerminalRewardAdapter()
    result = adapter.from_step(
        terminated=True, reward=0.0, info=_valid_official_zero_info()
    )
    assert result.value == 0.0


def test_agent_failure_official_zero_is_a_valid_grpo_negative():
    adapter = OfficialTerminalRewardAdapter()
    result = adapter.from_step(
        terminated=True,
        reward=0.0,
        info={**_valid_official_zero_info(), "termination_reason": "agent_error"},
    )
    assert result.value == 0.0


def test_simulation_run_missing_rejects_raw_gym_zero():
    episode = Tau3RetailEpisode.__new__(Tau3RetailEpisode)
    episode.task_id = "missing-simulation"
    episode._env = SimpleNamespace(
        _simulation_run=None,
        step=lambda _action: ("", 0.0, True, False, {}),
    )
    result = episode.step("{}")[1:]
    with pytest.raises(RewardAdapterError, match="interaction failed"):
        OfficialTerminalRewardAdapter().from_episode(("", *result))


def test_evaluator_exception_is_rejected_and_not_converted_to_zero():
    episode = Tau3RetailEpisode.__new__(Tau3RetailEpisode)
    episode.task_id = "evaluator-failure"

    def fail(_action):
        raise RuntimeError("frozen evaluator failed")

    episode._env = SimpleNamespace(step=fail)
    with pytest.raises(Tau3EnvironmentError, match="frozen evaluator failed"):
        episode.step("{}")


def test_incomplete_k8_group_from_infrastructure_failure_fails_fast():
    with pytest.raises(ValueError, match="K=8"):
        GroupArtifact.create("incomplete", "17", [0.0] * 7)


def test_reward_adapter_rejects_nonterminal_and_unproven_zero():
    adapter = OfficialTerminalRewardAdapter()
    with pytest.raises(RewardAdapterError):
        adapter.from_step(terminated=False, reward=0.0, info={})
    with pytest.raises(RewardAdapterError):
        adapter.from_step(terminated=True, reward=0.0, info={})


def test_assistant_only_loss_mask_excludes_environment_tokens():
    segments = [
        TokenSegment("system", (1, 2)),
        TokenSegment("user", (3,)),
        TokenSegment("assistant", (4, 5)),
        TokenSegment("tool", (6,)),
    ]
    assert assistant_only_loss_mask(segments) == [0, 0, 0, 1, 1, 0]


def test_nscc_launcher_is_synchronous_and_supports_both_step_modes(tmp_path):
    command = _verl_command(
        tmp_path / "train.parquet",
        tmp_path,
        optimizer_steps=1,
    )
    assert "verl.trainer.main_ppo_sync" in command
    assert "verl.experimental.one_step_off_policy.main_ppo" not in command
    assert "actor_rollout_ref.hybrid_engine=True" in command
    assert "actor_rollout_ref.rollout.mode=sync" not in command
    assert "trainer.n_gpus_per_node=2" in command
    assert "trainer.total_training_steps=1" in command

    formal = _verl_command(
        tmp_path / "train.parquet",
        tmp_path,
        optimizer_steps=FROZEN_CONFIG.total_steps,
    )
    assert f"trainer.total_training_steps={FROZEN_CONFIG.total_steps}" in formal


def test_nscc_launcher_persists_all_process_streams():
    job = (Path(__file__).parents[1] / "nscc" / "train_tau3_grpo_v1.pbs").read_text(
        encoding="utf-8"
    )

    assert 'GRPO_LOG_DIR="${GRPO_LOG_DIR:-$GRPO_OUTPUT_DIR/logs}"' in job
    assert 'tee -a "$GRPO_LOG_DIR/stdout.log"' in job
    assert 'tee -a "$GRPO_LOG_DIR/stderr.log"' in job
    assert '"$GRPO_LOG_DIR/combined.log"' in job
    assert "export PYTHONFAULTHANDLER=1" in job


def test_preflight_only_short_circuits_before_verl(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GRPO_PREFLIGHT_ONLY", "1")
    monkeypatch.setattr(sys, "argv", [
        "train_tau3_grpo",
        "--tau-root",
        str(tmp_path / "tau2"),
        "--output-dir",
        str(tmp_path / "preflight"),
        "--launch",
    ])
    monkeypatch.setattr(grpo_launcher, "validate_snapshot", lambda root: root)
    monkeypatch.setattr(
        grpo_launcher,
        "retail_train_task_ids",
        lambda _root: [str(index) for index in range(FROZEN_CONFIG.train_tasks)],
    )

    generated = tmp_path / "preflight" / "tau3_retail_train.parquet"

    def fake_build_dataset(_tau_root, output_dir):
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(b"parquet-preflight")
        return generated

    monkeypatch.setattr(grpo_launcher, "_build_dataset", fake_build_dataset)
    monkeypatch.setattr(
        grpo_launcher.subprocess,
        "call",
        lambda *_args, **_kwargs: pytest.fail("preflight launched VERL"),
    )

    assert grpo_launcher.main() == 0
    output = capsys.readouterr().out
    assert "PREFLIGHT_ONLY_PASS" in output
    assert "Launching VERL" not in output


def test_check_only_short_circuits_before_verl(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GRPO_PREFLIGHT_ONLY", raising=False)
    monkeypatch.setattr(sys, "argv", [
        "train_tau3_grpo",
        "--output-dir",
        str(tmp_path / "checks"),
        "--check-only",
    ])
    monkeypatch.setattr(
        grpo_launcher,
        "_check_artifact_chain",
        lambda _output_dir, *, steps: {"status": "PASS", "steps": steps},
    )
    monkeypatch.setattr(
        grpo_launcher.subprocess,
        "call",
        lambda *_args, **_kwargs: pytest.fail("check-only launched VERL"),
    )

    assert grpo_launcher.main() == 0
    assert "Launching VERL" not in capsys.readouterr().out


def test_verl_seed_override_uses_hydra_append_syntax(tmp_path):
    command = _verl_command(tmp_path / "train.parquet", tmp_path, optimizer_steps=1)

    assert "seed=300" not in command
    assert "+seed=300" in command


def test_frozen_config_records_the_track_a_nl_judge():
    assert FROZEN_CONFIG.nl_assertions_model == "deepseek/deepseek-chat"
