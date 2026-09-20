"""OpenAI-compatible native function-calling policy.

The provider wire format uses ``tools`` and ``message.tool_calls``. Provider
responses are converted to the existing :class:`AgentAction`, so the harness,
action constraint, transactional guardrails, and trajectory attribution remain
the execution authority.
"""

from __future__ import annotations

import copy
import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent_runtime import AgentRuntime, RuntimeConfig
from .context_compaction import context_compaction_enabled
from .domain import AgentAction, AgentObservation
from .skill_loader import Skill, load_skill
from .tool_schema import IDENTITY_TOOLS, ToolArgumentError, has_valid_verification_code, validate_arguments


NATIVE_SYSTEM_PROMPT = """You are a retail customer-support tool agent.

Use the provided tools when business data or an action is required. Tool results
are the only authority for products, orders, prices, eligibility, and state.
Never use a write tool as a probe. Read and bind the exact target order and item,
then obtain explicit user confirmation before a write. Never invent identifiers,
tool results, refund completion, balances, or delivery guarantees.

Use request_user_input when an order id, six-digit verification code, reason,
clarification, or confirmation is missing. Use handoff_to_human when the request
cannot be completed safely. Otherwise answer the user directly.
"""


CONTROL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "request_user_input",
            "description": "Ask the user for one missing value or explicit confirmation instead of guessing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "input_type": {
                        "type": "string",
                        "enum": ["order_id", "verification_code", "confirmation", "reason", "clarification", "other"],
                    },
                },
                "required": ["message", "input_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff_to_human",
            "description": "Safely hand the conversation to a human agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "order_id": {"type": ["string", "null"]},
                    "message": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
]

_CONTROL_INPUT_TYPES = frozenset(
    {"order_id", "verification_code", "confirmation", "reason", "clarification", "other"}
)
_HARNESS_INPUT_TYPES = {"reason": "return_reason"}


@dataclass
class NativeGeneration:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw_message: dict[str, Any] = field(default_factory=dict)


class NativeActionError(ValueError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def native_tool_schemas(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wrap internal JSON Schemas for OpenAI-compatible APIs.

    ``user_id`` is system-owned. It is hidden from the model and injected from
    ``AgentObservation.session`` after generation.
    """
    wrapped: list[dict[str, Any]] = []
    for schema in schemas:
        if schema.get("name") == "escalate_to_human":
            continue
        parameters = copy.deepcopy(schema["parameters"])
        parameters.get("properties", {}).pop("user_id", None)
        parameters["required"] = [x for x in parameters.get("required", []) if x != "user_id"]
        wrapped.append({
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema.get("description", ""),
                "parameters": parameters,
            },
        })
    return wrapped + copy.deepcopy(CONTROL_TOOLS)


def _history_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    pending_call_id: str | None = None
    for index, entry in enumerate(history):
        role = entry.get("role")
        content = str(entry.get("content", ""))
        if role == "assistant" and entry.get("action") == "tool_call" and entry.get("tool_name"):
            pending_call_id = f"call_history_{index}"
            messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": [{
                    "id": pending_call_id,
                    "type": "function",
                    "function": {
                        "name": entry["tool_name"],
                        "arguments": json.dumps(entry.get("arguments") or {}, ensure_ascii=False),
                    },
                }],
            })
        elif role == "tool":
            messages.append({
                "role": "tool",
                "tool_call_id": pending_call_id or f"call_history_{index}",
                "name": entry.get("name"),
                "content": content,
            })
            pending_call_id = None
        elif role in {"user", "assistant", "system"}:
            messages.append({"role": role, "content": content})
    return messages


class NativeToolPolicy:
    """One native tool call or one direct answer per policy step."""

    privileged = False

    def __init__(
        self,
        generate: Callable[[list[dict[str, Any]], list[dict[str, Any]]], NativeGeneration],
        *,
        max_parse_retries: int = 1,
        generator_meta: dict[str, Any] | None = None,
        compact_context: bool = True,
        skill_path: str | os.PathLike[str] | None = None,
        skill_enabled: bool = False,
    ):
        self.generate = generate
        self.max_parse_retries = max_parse_retries
        self.generator_meta = generator_meta or {}
        self.compact_context = compact_context
        self.skill: Skill | None = load_skill(skill_path) if skill_enabled and skill_path else None
        self.retry_count = 0
        self.last_trace: dict[str, Any] = {}
        self.runtime = AgentRuntime(
            RuntimeConfig(
                runtime_version="system-v1",
                prompt_version="ecommerce-native-v1",
                compact_context=compact_context,
                max_generation_retries=max_parse_retries,
                instruction=NATIVE_SYSTEM_PROMPT,
                skill_id=self.skill.skill_id if self.skill else None,
                skill_version=self.skill.version if self.skill else None,
                skill_content_hash=self.skill.content_hash if self.skill else None,
                skill_instructions=self.skill.content if self.skill else "",
            )
        )

    @classmethod
    def from_env(
        cls,
        *,
        skill_path: str | os.PathLike[str] | None = None,
        skill_enabled: bool = False,
    ) -> "NativeToolPolicy":
        base_url = os.getenv("ARAG_LLM_BASE_URL", os.getenv("ERAG_LLM_BASE_URL", "")).strip()
        if not base_url:
            raise RuntimeError(
                "ARAG_LLM_BASE_URL is required for the native tool backend; "
                "the endpoint must expose an OpenAI-compatible chat-completions API"
            )
        model = os.getenv(
            "ARAG_LLM_MODEL",
            os.getenv("ERAG_LLM_MODEL", "Qwen/Qwen3-4B-Instruct-2507"),
        )
        api_key = os.getenv("ARAG_LLM_API_KEY", os.getenv("ERAG_LLM_API_KEY", ""))
        compact = context_compaction_enabled(default=True)
        return cls(
            cls._openai_generator(base_url, api_key, model),
            generator_meta={"backend": "openai_compatible_native_tools", "model": model, "base_url": base_url},
            compact_context=compact,
            skill_path=skill_path,
            skill_enabled=skill_enabled,
        )

    @staticmethod
    def _openai_generator(base_url: str, api_key: str, model: str):
        if not api_key:
            raise RuntimeError("ARAG_LLM_API_KEY is required for the native tool backend")
        endpoint = base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"

        def generate(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> NativeGeneration:
            body = json.dumps({
                "model": model,
                "temperature": 0,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            }, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            choice = payload["choices"][0]
            message = choice["message"]
            usage = payload.get("usage") or {}
            return NativeGeneration(
                content=message.get("content") or "",
                tool_calls=list(message.get("tool_calls") or []),
                finish_reason=choice.get("finish_reason"),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                raw_message=message,
            )

        return generate

    @staticmethod
    def _arguments(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        function = call.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise NativeActionError("missing_tool_name", "native tool call has no function name")
        raw = function.get("arguments") or "{}"
        if isinstance(raw, dict):
            arguments = raw
        else:
            try:
                arguments = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as exc:
                raise NativeActionError("tool_arguments_json", str(exc)) from exc
        if not isinstance(arguments, dict):
            raise NativeActionError("tool_arguments_not_object", "tool arguments must be an object")
        return name, arguments

    def _to_action(self, generation: NativeGeneration, observation: AgentObservation) -> AgentAction:
        if len(generation.tool_calls) > 1:
            raise NativeActionError("parallel_tool_calls", "the harness executes one action per step")
        if generation.tool_calls:
            name, arguments = self._arguments(generation.tool_calls[0])
            if name == "request_user_input":
                message = arguments.get("message")
                if not isinstance(message, str) or not message.strip():
                    raise NativeActionError("request_message_missing", "request_user_input requires message")
                input_type = arguments.get("input_type")
                if not isinstance(input_type, str) or input_type not in _CONTROL_INPUT_TYPES:
                    raise NativeActionError(
                        "request_input_type_invalid",
                        "request_user_input requires one of the declared input_type values",
                    )
                unknown = sorted(set(arguments) - {"message", "input_type"})
                if unknown:
                    raise NativeActionError(
                        "request_input_argument_invalid",
                        f"request_user_input received unknown argument(s): {unknown}",
                    )
                return AgentAction.answer(
                    message,
                    requires_user_response=True,
                    requested_input_type=_HARNESS_INPUT_TYPES.get(input_type, input_type),
                )
            if name == "handoff_to_human":
                reason = arguments.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise NativeActionError("handoff_reason_missing", "handoff_to_human requires reason")
                extra = {"order_id": arguments["order_id"]} if arguments.get("order_id") else {}
                return AgentAction.handoff(reason, **extra)

            allowed = {schema["name"] for schema in observation.tool_schemas}
            if name not in allowed or name == "escalate_to_human":
                raise NativeActionError("unknown_tool", f"tool not offered: {name!r}")
            original = next(schema for schema in observation.tool_schemas if schema["name"] == name)
            if "user_id" in original["parameters"].get("properties", {}):
                arguments["user_id"] = observation.session.get("user_id")
            if name in IDENTITY_TOOLS and not has_valid_verification_code(arguments):
                raise NativeActionError("missing_verification_code", "ask the user for the six-digit code")
            try:
                validate_arguments(name, arguments)
            except ToolArgumentError as exc:
                raise NativeActionError("schema_violation", str(exc)) from exc
            return AgentAction.tool_call(name, **arguments)

        if not generation.content.strip():
            raise NativeActionError("empty_output", "model returned neither content nor a tool call")
        return AgentAction.answer(generation.content.strip())

    def act(self, observation: AgentObservation) -> AgentAction:
        history_messages = _history_messages(observation.history)
        messages, stats = self.runtime.prepare_messages(history_messages, "")
        # ``history`` is the source of truth for message provenance.  The
        # harness sets ``current_message`` to the latest history entry for all
        # policy steps, including tool results.  Never infer provenance from
        # text equality because a tool result can repeat user text (and vice
        # versa).  In the normal harness path the latest event is already
        # represented by ``history_messages``; only an empty history needs the
        # compatibility fallback of treating ``current_message`` as a user
        # message.
        if not observation.history:
            messages.append({"role": "user", "content": observation.current_message})
        tools = native_tool_schemas(observation.tool_schemas)
        attempts: list[dict[str, Any]] = []
        error = ""
        for attempt in range(self.max_parse_retries + 1):
            request_messages = copy.deepcopy(messages)
            if error:
                request_messages.append({
                    "role": "system",
                    "content": f"Previous native action was invalid: {error}. Return one corrected tool call or answer.",
                })
            record: dict[str, Any] = {
                "attempt": attempt,
                "protocol": "native_tool_calls",
                "message_count": len(request_messages),
                "tool_count": len(tools),
                "context_compaction": stats.to_dict() if stats else {"enabled": False},
            }
            try:
                generation = self.generate(request_messages, tools)
                action = self._to_action(generation, observation)
            except NativeActionError as exc:
                record.update(parse_ok=False, parse_stage=exc.stage, parse_error=str(exc))
                attempts.append(record)
                error = f"[{exc.stage}] {exc}"
                if attempt < self.max_parse_retries:
                    self.retry_count += 1
                continue
            except Exception as exc:  # provider/runtime attribution
                record.update(parse_ok=False, parse_stage="generation_error",
                              parse_error=f"{type(exc).__name__}: {exc}")
                attempts.append(record)
                error = f"[generation_error] {type(exc).__name__}: {exc}"
                if attempt < self.max_parse_retries:
                    self.retry_count += 1
                continue
            record.update(
                parse_ok=True,
                finish_reason=generation.finish_reason,
                prompt_tokens=generation.prompt_tokens,
                completion_tokens=generation.completion_tokens,
                raw_message=generation.raw_message or {
                    "content": generation.content, "tool_calls": generation.tool_calls},
                action_type=action.action_type,
                tool_name=action.tool_name,
            )
            attempts.append(record)
            self.last_trace = {
                "resolution": "native_action",
                "attempts": attempts,
                "generator": self.generator_meta,
                "protocol": "native_tool_calls",
                "runtime": self.runtime.config.to_dict(),
            }
            return action

        final_stage = attempts[-1].get("parse_stage") if attempts else None
        reason = "model_generation_error" if final_stage == "generation_error" else "model_action_parse_failure"
        self.last_trace = {
            "resolution": "fallback_handoff", "attempts": attempts,
            "generator": self.generator_meta, "protocol": "native_tool_calls",
            "final_stage": final_stage, "fallback_reason": reason,
            "runtime": self.runtime.config.to_dict(),
        }
        return AgentAction.handoff(reason)
