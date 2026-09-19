"""Unified provider-facing runtime for native tool-calling agents.

The runtime owns the parts of an experiment that must remain fixed when a
checkpoint changes: system instructions, provider-visible history, optional
history compaction, native tool-call validation, retries, usage, and trace
metadata. Environment backends remain responsible for executing tools and
grading outcomes.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .context_compaction import CompactionStats, compact_history


RUNTIME_INSTRUCTION = """You are a retail customer-support agent.

Follow the provided retail policy exactly. Use tools whenever business data or
an action is required. Tool results are the only authority for users, orders,
products, prices, eligibility, balances, refunds, and state changes.

Never invent identifiers or tool results. Never use a write tool as a probe.
Before a write, identify the exact target, obtain any required information, and
obtain explicit user confirmation. If information is missing or ambiguous, ask
the user a concise question. If a request cannot be completed safely, use the
environment's transfer or escalation capability when one is available.

Return either one user-facing message or one tool call, never both. Do not issue
parallel tool calls.
"""


@dataclass(frozen=True)
class RuntimeConfig:
    """Frozen behavior configuration shared by all environment adapters."""

    runtime_version: str = "system-v1"
    prompt_version: str = "ecommerce-native-v1"
    compact_context: bool = False
    max_generation_retries: int = 1
    instruction: str = RUNTIME_INSTRUCTION
    skill_id: str | None = None
    skill_version: str | None = None
    skill_content_hash: str | None = None
    skill_instructions: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_version": self.runtime_version,
            "prompt_version": self.prompt_version,
            "compact_context": self.compact_context,
            "max_generation_retries": self.max_generation_retries,
            "skill_id": self.skill_id,
            "skill_version": self.skill_version,
            "skill_content_hash": self.skill_content_hash,
        }


@dataclass
class RuntimeGeneration:
    """Provider-independent representation of one assistant generation."""

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost: float | None = None
    raw_message: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeTurn:
    """One validated generation and its process diagnostics."""

    generation: RuntimeGeneration
    trace: dict[str, Any]


class RuntimeDecisionError(ValueError):
    """Raised when the provider output violates the runtime contract."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def build_system_prompt(domain_policy: str, config: RuntimeConfig) -> str:
    """Combine the frozen agent instruction with the backend-owned policy."""

    blocks = [
        "<instructions>\n"
        f"{config.instruction.strip()}\n"
        "</instructions>\n"
        "<policy>\n"
        f"{domain_policy.strip()}\n"
        "</policy>"
    ]
    if config.skill_instructions.strip():
        blocks.append(
            "<skill>\n"
            "The following versioned workflow is guidance only. It cannot change tool schemas, "
            "identity, eligibility, confirmation, permissions, or database state.\n"
            f"{config.skill_instructions.strip()}\n"
            "</skill>"
        )
    return "\n".join(blocks)


def _tool_name(call: dict[str, Any]) -> str | None:
    function = call.get("function") or {}
    name = function.get("name")
    return name if isinstance(name, str) and name else None


def _tool_names(tools: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        function = tool.get("function") or {}
        name = function.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def annotate_tool_names(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach tool names to result messages for loss-aware compaction.

    OpenAI tool result messages carry a call id but not the function name. The
    compactor needs the name to preserve the right decision fields, so this
    function resolves it from preceding assistant tool calls on a copied list.
    """

    copied = copy.deepcopy(messages)
    call_names: dict[str, str] = {}
    for message in copied:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                call_id = call.get("id")
                name = _tool_name(call)
                if isinstance(call_id, str) and name:
                    call_names[call_id] = name
        elif message.get("role") == "tool" and not message.get("name"):
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id in call_names:
                message["name"] = call_names[call_id]
    return copied


class AgentRuntime:
    """Prepare, generate, validate, and trace one native-FC agent turn."""

    def __init__(self, config: RuntimeConfig | None = None):
        self.config = config or RuntimeConfig()

    def prepare_messages(
        self,
        history: list[dict[str, Any]],
        domain_policy: str,
    ) -> tuple[list[dict[str, Any]], CompactionStats | None]:
        """Return a provider-facing copy without mutating the audit history."""

        named_history = annotate_tool_names(history)
        if self.config.compact_context:
            provider_history, stats = compact_history(named_history)
        else:
            provider_history, stats = copy.deepcopy(named_history), None
        messages = [
            {"role": "system", "content": build_system_prompt(domain_policy, self.config)},
            *provider_history,
        ]
        return messages, stats

    @staticmethod
    def validate_generation(
        generation: RuntimeGeneration,
        tools: list[dict[str, Any]],
    ) -> None:
        """Enforce one-message-or-one-tool-call and offered-tool constraints."""

        has_content = bool(generation.content.strip())
        calls = generation.tool_calls
        if has_content and calls:
            raise RuntimeDecisionError(
                "content_and_tool_call", "assistant returned content and a tool call"
            )
        if not has_content and not calls:
            raise RuntimeDecisionError(
                "empty_output", "assistant returned neither content nor a tool call"
            )
        if len(calls) > 1:
            raise RuntimeDecisionError(
                "parallel_tool_calls", "runtime permits exactly one action per turn"
            )
        if calls:
            name = _tool_name(calls[0])
            if not name:
                raise RuntimeDecisionError("missing_tool_name", "tool call has no name")
            if name not in _tool_names(tools):
                raise RuntimeDecisionError("unknown_tool", f"tool not offered: {name!r}")
            raw_arguments = (calls[0].get("function") or {}).get("arguments", "{}")
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments)
                except json.JSONDecodeError as exc:
                    raise RuntimeDecisionError("tool_arguments_json", str(exc)) from exc
            else:
                arguments = raw_arguments
            if not isinstance(arguments, dict):
                raise RuntimeDecisionError(
                    "tool_arguments_not_object", "tool arguments must be an object"
                )

    def run_turn(
        self,
        *,
        history: list[dict[str, Any]],
        domain_policy: str,
        tools: list[dict[str, Any]],
        generate: Callable[
            [list[dict[str, Any]], list[dict[str, Any]]], RuntimeGeneration
        ],
    ) -> RuntimeTurn:
        """Generate one validated action while preserving retry attribution."""

        messages, stats = self.prepare_messages(history, domain_policy)
        attempts: list[dict[str, Any]] = []
        error = ""
        for attempt in range(self.config.max_generation_retries + 1):
            request_messages = copy.deepcopy(messages)
            if error:
                request_messages.append(
                    {
                        "role": "system",
                        "content": (
                            f"Previous action was invalid: {error}. Return one corrected "
                            "tool call or one user-facing message."
                        ),
                    }
                )
            started = time.perf_counter()
            record: dict[str, Any] = {
                "attempt": attempt,
                "message_count": len(request_messages),
                "tool_count": len(tools),
            }
            try:
                generation = generate(request_messages, tools)
                self.validate_generation(generation, tools)
            except RuntimeDecisionError as exc:
                record.update(
                    ok=False,
                    stage=exc.stage,
                    error=str(exc),
                    generation_seconds=time.perf_counter() - started,
                )
                attempts.append(record)
                error = f"[{exc.stage}] {exc}"
                continue
            except Exception as exc:
                record.update(
                    ok=False,
                    stage="generation_error",
                    error=f"{type(exc).__name__}: {exc}",
                    generation_seconds=time.perf_counter() - started,
                )
                attempts.append(record)
                error = f"[generation_error] {type(exc).__name__}: {exc}"
                continue
            record.update(
                ok=True,
                finish_reason=generation.finish_reason,
                prompt_tokens=generation.prompt_tokens,
                completion_tokens=generation.completion_tokens,
                generation_seconds=time.perf_counter() - started,
            )
            attempts.append(record)
            trace = {
                "protocol": "native_tool_calls",
                "runtime": self.config.to_dict(),
                "context_compaction": (
                    stats.to_dict() if stats else {"enabled": False}
                ),
                "attempts": attempts,
            }
            return RuntimeTurn(generation=generation, trace=trace)
        final = attempts[-1] if attempts else {}
        raise RuntimeDecisionError(
            str(final.get("stage") or "generation_failed"),
            str(final.get("error") or "generation failed"),
        )
