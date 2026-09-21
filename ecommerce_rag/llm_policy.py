"""OpenAI-compatible and local next-action policy.

The policy receives only ``AgentObservation``. It never loads task files or gold
labels, which makes it safe to evaluate on the locked split.

Action protocol: **one JSON object per turn**, validated against the JSON Schema
in :mod:`ecommerce_rag.tool_schema`. The same schemas can later be handed to a
native tool-calling API or a constrained decoder without changing this contract.

Every generation is recorded in :attr:`LLMPolicy.last_trace` — raw output,
finish reason, token counts, and the exact stage at which parsing failed. The
first 360-trajectory run could not be diagnosed because none of that was kept.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .domain import AgentAction, AgentObservation
from .tool_schema import (
    IDENTITY_TOOLS,
    ToolArgumentError,
    has_valid_verification_code,
    prompt_block,
    validate_arguments,
)

SYSTEM_PROMPT = """You are a retail support next-action policy.

Return EXACTLY ONE JSON object and nothing else. No prose, no markdown fence, no explanation.

Always emit all five fields:
{"action_type": "tool_call" | "final_answer" | "handoff",
 "tool_name": string or null,
 "arguments": object,
 "content": string,
 "requires_user_response": boolean}

What each action type requires:

- tool_call:    tool_name = one listed tool; arguments = exactly its declared
                arguments; content = ""; requires_user_response = false.
                A tool_call runs immediately, so it can never be used to ask the
                user for something. If an argument is missing, use final_answer.
- final_answer: tool_name = null; arguments = {}; content = your message.
                Set requires_user_response = true when you are asking the user
                for an order id, a six-digit verification code, or confirmation.
                This is the ONLY action type that may set it to true.
- handoff:      tool_name = null; content = your message; requires_user_response
                = false; arguments = {"reason": "<short reason>"} and optionally
                "order_id". reason is REQUIRED. Do not set user_id — the system
                supplies it.

Available tools:
{tools}

Rules:
- Use only a listed tool, with exactly its declared arguments and types.
- Product IDs accepted by get_product are internal IDs such as P00042. A model
  number, title, SKU, or external code must first be sent unchanged to
  search_catalog; take the returned Pxxxxx and then call get_product before
  answering a product-specification question.
- get_policy accepts only: return, warranty, shipping, invoice, refund. Map
  return_policy/退货 to return and 保修 to warranty; never invent another key.
- Never call an order tool until the user has given you a six-digit verification
  code in this conversation. If you do not have it, do not call the tool with an
  empty or invented code — return final_answer with requires_user_response=true
  and ask for it. Only once the user has replied with the digits may you call the
  tool.
- Never invent tool results, identity data, or policy facts.
- After a successful read tool, answer from its result.
- On identity or ownership failure, hand off with a reason.
"""

#: Tool results can be large; keep the rendered history inside a budget so the
#: request does not crowd out the response window on a small local model.
MAX_HISTORY_CHARS = 6000
MAX_TOOL_RESULT_CHARS = 900


@dataclass
class Generation:
    """A single model call, with whatever the backend can tell us about it."""

    text: str
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    truncated: bool | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ActionParseError(ValueError):
    """Parse failure carrying the stage at which it happened."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


#: The protocol requires exactly these envelope fields and nothing else.
ACTION_FIELDS = ("action_type", "tool_name", "arguments", "content", "requires_user_response")

#: Markdown wrappers a model commonly adds. Recognised so they can be reported,
#: not silently discarded: during smoke we want the real output format visible.
_FENCE_MARKERS = ("```", "~~~")


def _extract_json_object(raw: str) -> tuple[str, str]:
    """Return the first balanced ``{...}`` block and any content outside it.

    A greedy ``\\{.*\\}`` spans from the first brace to the last one, so a model
    that emits two objects, or prose containing a brace, yields something that
    is not valid JSON and the real cause is hidden behind a decode error.

    The protocol says "exactly one JSON object and nothing else", so surrounding
    material is returned verbatim rather than cleaned up — the caller classifies
    it instead of counting the output as compliant.
    """
    start = raw.find("{")
    if start < 0:
        raise ActionParseError("no_json_object", "no '{' in model output")
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw[start:index + 1], (raw[:start] + raw[index + 1:])
    raise ActionParseError("unbalanced_json", "'{' is never closed — output was likely truncated")


def _render_history(history: list[dict[str, Any]]) -> str:
    lines = []
    for entry in history:
        role = entry.get("role", "?")
        if role == "tool":
            content = str(entry.get("content", ""))
            if len(content) > MAX_TOOL_RESULT_CHARS:
                content = content[:MAX_TOOL_RESULT_CHARS] + f"…[+{len(content) - MAX_TOOL_RESULT_CHARS} chars]"
            lines.append(f"[tool:{entry.get('name')}] {content}")
        else:
            lines.append(f"[{role}] {entry.get('content', '')}")
    rendered = "\n".join(lines)
    if len(rendered) > MAX_HISTORY_CHARS:  # keep the most recent turns
        rendered = "…[earlier turns omitted]\n" + rendered[-MAX_HISTORY_CHARS:]
    return rendered


class LLMPolicy:
    privileged = False

    def __init__(self, generate: Callable[[str, str], "str | Generation"], *, max_parse_retries: int = 1,
                 generator_meta: dict[str, Any] | None = None):
        self.generate = generate
        self.max_parse_retries = max_parse_retries
        self.generator_meta = generator_meta or {}
        self.retry_count = 0
        self.last_trace: dict[str, Any] = {}

    # ------------------------------------------------------------------ setup

    @classmethod
    def from_env(cls) -> "LLMPolicy":
        backend = os.getenv("ARAG_AGENT_BACKEND", "openai").lower()
        if backend == "local":
            model = os.getenv("ARAG_LOCAL_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
            raw_max_new_tokens = os.getenv("ARAG_LOCAL_MAX_NEW_TOKENS", "512").strip()
            if not re.fullmatch(r"[1-9][0-9]*", raw_max_new_tokens):
                raise ValueError("ARAG_LOCAL_MAX_NEW_TOKENS must be a positive integer")
            max_new_tokens = int(raw_max_new_tokens)
            generate, meta = cls._local_generator(model, max_new_tokens=max_new_tokens)
            return cls(generate, generator_meta=meta)
        base_url = os.getenv("ARAG_LLM_BASE_URL", "https://api.openai.com/v1")
        model = os.getenv("ARAG_LLM_MODEL", "gpt-4o-mini")
        return cls(cls._openai_generator(base_url, os.getenv("ARAG_LLM_API_KEY", ""), model),
                   generator_meta={"backend": "openai", "model": model, "base_url": base_url})

    @classmethod
    def probe_backend(cls) -> dict[str, Any]:
        """Try to build the backend and report the outcome without raising.

        Model and tokenizer loading, and the chat-template probe, all happen while
        the generator is constructed — before any policy exists and therefore
        before :meth:`act`'s guard can see them. A bad model path, a failed weight
        load, an OOM at load time or a template probe raising something other than
        TypeError would otherwise kill the job with nothing recorded. Run this
        first so the failure is attributable.
        """
        try:
            policy = cls.from_env()
        except Exception as exc:  # noqa: BLE001 - reporting is the point
            return {"ok": False, "stage": "backend_init",
                    "error_type": type(exc).__name__, "error": str(exc)}
        result: dict[str, Any] = {"ok": True, "stage": "backend_init", "generator": policy.generator_meta}
        try:
            generation = policy.generate("Reply with one JSON object.", 'Return {"action_type":"final_answer"}.')
        except Exception as exc:  # noqa: BLE001
            result.update(ok=False, stage="first_generation",
                          error_type=type(exc).__name__, error=str(exc))
            return result
        if isinstance(generation, str):
            generation = Generation(text=generation)
        result["first_generation"] = {
            "raw_output": generation.text, "finish_reason": generation.finish_reason,
            "prompt_tokens": generation.prompt_tokens, "completion_tokens": generation.completion_tokens,
            "truncated": generation.truncated,
        }
        return result

    @staticmethod
    def _openai_generator(base_url: str, api_key: str, model: str) -> Callable[[str, str], Generation]:
        if not api_key:
            raise RuntimeError("ARAG_LLM_API_KEY is required for the openai backend")
        endpoint = base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"

        def generate(system: str, user: str) -> Generation:
            body = json.dumps({"model": model, "temperature": 0, "messages": [
                {"role": "system", "content": system}, {"role": "user", "content": user}
            ]}).encode("utf-8")
            request = urllib.request.Request(
                endpoint, data=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            choice = payload["choices"][0]
            usage = payload.get("usage") or {}
            finish = choice.get("finish_reason")
            return Generation(
                text=choice["message"]["content"] or "",
                finish_reason=finish,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                truncated=finish == "length",
            )
        return generate

    @staticmethod
    def _local_generator(model_name: str, max_new_tokens: int = 512):
        from transformers import AutoModelForCausalLM, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True, local_files_only=True)
        model = AutoModelForCausalLM.from_pretrained(
            model_name, device_map="auto", torch_dtype="auto",
            trust_remote_code=True, local_files_only=True,
        )
        model.eval()
        # Record which template path was taken; a mismatched chat template is one
        # of the few things that can silently break every single generation.
        thinking_supported = True
        try:
            tokenizer.apply_chat_template([{"role": "user", "content": "x"}], tokenize=False,
                                          add_generation_prompt=True, enable_thinking=False)
        except TypeError:
            thinking_supported = False

        meta = {"backend": "local", "model": model_name, "max_new_tokens": max_new_tokens,
                "enable_thinking_supported": thinking_supported,
                "chat_template_present": bool(getattr(tokenizer, "chat_template", None))}

        def generate(system: str, user: str) -> Generation:
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            kwargs = {"tokenize": False, "add_generation_prompt": True}
            if thinking_supported:
                kwargs["enable_thinking"] = False
            text = tokenizer.apply_chat_template(messages, **kwargs)
            inputs = tokenizer(text, return_tensors="pt").to(model.device)
            output = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
            generated = output[0][inputs.input_ids.shape[1]:]
            completion = int(generated.shape[0])
            return Generation(
                text=tokenizer.decode(generated, skip_special_tokens=True),
                finish_reason="length" if completion >= max_new_tokens else "stop",
                prompt_tokens=int(inputs.input_ids.shape[1]),
                completion_tokens=completion,
                truncated=completion >= max_new_tokens,
                meta={"rendered_prompt_chars": len(text)},
            )
        return generate, meta

    # ------------------------------------------------------------------ parse

    @staticmethod
    def _parse(raw: str, allowed_tools: set[str]) -> tuple[AgentAction, list[str]]:
        """Parse one action. Returns the action and any envelope violations.

        Types are checked, never coerced: ``bool("false")`` is ``True`` and
        ``str(7)`` is ``"7"``, so coercion turns a malformed action into a
        plausible-looking one and the protocol violation disappears from the
        statistics. Deviations that are recoverable (extra keys, trailing text)
        are reported instead of silently accepted, so the diagnosis can separate
        strictly compliant output from output we merely rescued.
        """
        if not raw or not raw.strip():
            raise ActionParseError("empty_output", "model returned no text")
        block, trailing = _extract_json_object(raw)
        try:
            value = json.loads(block)
        except json.JSONDecodeError as exc:
            raise ActionParseError("json_decode_error", str(exc)) from exc
        # `value` is necessarily a dict: the extractor returns a balanced {...}
        # block, so a successful decode cannot yield any other type.

        # "Exactly these fields and nothing else" is taken literally: a fence, a
        # missing field defaulted for us, or an extra key are all deviations from
        # the stated contract. They stay recoverable, but they are never counted
        # as compliant — during smoke the real output format must be visible.
        violations: list[str] = []
        fenced = any(marker in trailing for marker in _FENCE_MARKERS)
        residue = trailing
        if fenced:
            violations.append("markdown_fence")
            for marker in _FENCE_MARKERS:
                residue = residue.replace(marker, "")
            # A fence may carry a language tag right after the opening marker.
            # Only discount it when a fence was actually present — a bare "json"
            # prefix is stray prose, not fence syntax, and must stay a violation.
            residue = residue.strip()
            if residue.lower().startswith("json"):
                residue = residue[4:]
        if residue.strip():
            violations.append("content_outside_json_object")
        extra = sorted(set(value) - set(ACTION_FIELDS))
        if extra:
            violations.append(f"unknown_envelope_field:{','.join(extra)}")
        missing = [field for field in ACTION_FIELDS if field not in value]
        if missing:
            violations.append(f"missing_envelope_field:{','.join(missing)}")

        action_type = value.get("action_type")
        if action_type not in {"tool_call", "final_answer", "handoff"}:
            raise ActionParseError("bad_action_type", f"unknown action_type: {action_type!r}")

        if "arguments" in value and value["arguments"] is not None and not isinstance(value["arguments"], dict):
            raise ActionParseError("arguments_not_object",
                                   f"arguments must be an object, got {type(value['arguments']).__name__}")
        arguments = value.get("arguments") or {}

        content = value.get("content", "")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ActionParseError("bad_content_type", f"content must be a string, got {type(content).__name__}")

        requires_response = value.get("requires_user_response", False)
        if requires_response is None:
            requires_response = False
        if not isinstance(requires_response, bool):
            raise ActionParseError("bad_requires_user_response_type",
                                   f"requires_user_response must be a boolean, got {type(requires_response).__name__}")

        tool_name = value.get("tool_name")
        if action_type == "tool_call":
            if tool_name is None or tool_name == "":
                raise ActionParseError("missing_tool_name", "tool_call without tool_name")
            if not isinstance(tool_name, str):
                raise ActionParseError("bad_tool_name_type", f"tool_name must be a string, got {type(tool_name).__name__}")
            if tool_name not in allowed_tools:
                raise ActionParseError("unknown_tool", f"tool not offered: {tool_name!r}")
            # Checked before anything else about this call, because there is only
            # one retry: reporting the flag or the pattern first spends it on a
            # symptom, and the model then "fixes" exactly what was reported while
            # the empty code — the real problem — survives into the second attempt.
            if tool_name in IDENTITY_TOOLS and not has_valid_verification_code(arguments):
                raise ActionParseError(
                    "missing_verification_code",
                    "The user has not supplied a six-digit verification code. "
                    "Do not call the tool. Return final_answer with "
                    "requires_user_response=true and ask the user for it.")
            if tool_name == "get_product":
                product_id = arguments.get("product_id")
                if isinstance(product_id, str) and re.fullmatch(r"P[0-9]{3,5}", product_id) is None:
                    raise ActionParseError(
                        "external_product_identifier",
                        f"{product_id!r} is not an internal Pxxxxx product_id. Call search_catalog "
                        "with that original identifier, read the returned Pxxxxx, then call get_product.")
            if tool_name == "get_policy":
                policy_type = arguments.get("policy_type")
                canonical = {"return", "warranty", "shipping", "invoice", "refund"}
                if isinstance(policy_type, str) and policy_type not in canonical:
                    raise ActionParseError(
                        "noncanonical_policy_type",
                        f"{policy_type!r} is not canonical. Use exactly one of return, warranty, "
                        "shipping, invoice, refund (return_policy/退货 -> return; 保修 -> warranty).")
            # A tool_call is executed on the spot, so the harness never sees this
            # flag. A policy that means "ask the user" has to say final_answer, or
            # the tool runs with whatever placeholder it left in the arguments.
            if requires_response:
                raise ActionParseError(
                    "tool_call_requires_user_response",
                    "tool_call runs immediately and cannot request a user response; "
                    "use final_answer with requires_user_response=true to ask")
            try:
                validate_arguments(tool_name, arguments)
            except ToolArgumentError as exc:
                raise ActionParseError("schema_violation", str(exc)) from exc
        else:
            if tool_name is not None:
                raise ActionParseError("tool_name_on_non_tool_action",
                                       f"{action_type} must not carry tool_name={tool_name!r}")
            if action_type == "handoff":
                # Handing off ends the turn, so asking the user something in the
                # same action is self-contradictory: the harness escalates while
                # the message still requests a code. Only final_answer may wait.
                if requires_response:
                    raise ActionParseError(
                        "handoff_requires_user_response",
                        "handoff ends the conversation and cannot request a user response; "
                        "use final_answer with requires_user_response=true to ask")
                # escalate_to_human requires a reason; the previous contract told
                # the model to send {} for non-tool actions, so every handoff the
                # model produced failed at the tool layer through no fault of its own.
                reason = arguments.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise ActionParseError("handoff_missing_reason",
                                           "handoff requires a non-empty arguments.reason")
                if "user_id" in arguments:
                    raise ActionParseError("handoff_sets_user_id",
                                           "user_id is supplied by the system, not the policy")
                unknown = sorted(set(arguments) - {"reason", "order_id"})
                if unknown:
                    raise ActionParseError("handoff_unknown_argument",
                                           f"handoff arguments may only be reason and order_id; got {unknown}")
            elif arguments:
                violations.append("arguments_on_final_answer")

        return AgentAction(action_type, tool_name, arguments, content, requires_response), violations

    # -------------------------------------------------------------------- act

    def _build_prompt(self, observation: AgentObservation) -> str:
        return (
            f"CONVERSATION SO FAR (step {observation.step}):\n{_render_history(observation.history)}\n\n"
            f"CURRENT USER MESSAGE:\n{observation.current_message}\n\n"
            f"SESSION: {json.dumps(observation.session, ensure_ascii=False)}\n\n"
            "Return the next action as one JSON object."
        )

    def act(self, observation: AgentObservation) -> AgentAction:
        system = SYSTEM_PROMPT.replace("{tools}", prompt_block(observation.tool_schemas))
        user = self._build_prompt(observation)
        allowed = {schema["name"] for schema in observation.tool_schemas}
        attempts: list[dict[str, Any]] = []
        error = ""

        for attempt in range(self.max_parse_retries + 1):
            request = user if not error else (
                f"{user}\n\nYour previous output could not be parsed: {error}\n"
                "Return corrected JSON only.")
            record: dict[str, Any] = {"attempt": attempt, "system_chars": len(system), "user_chars": len(request)}

            # A backend that raises — incompatible chat template, tokenizer or
            # model load failure, OOM, runtime error — is the one path that would
            # otherwise bypass every bit of this instrumentation and abort the run.
            try:
                result = self.generate(system, request)
            except Exception as exc:  # noqa: BLE001 - the whole point is to attribute it
                record.update(parse_ok=False, parse_stage="generation_error",
                              parse_error=f"{type(exc).__name__}: {exc}",
                              generation_error_type=type(exc).__name__, raw_output=None)
                attempts.append(record)
                error = f"[generation_error] {type(exc).__name__}"
                if attempt < self.max_parse_retries:
                    self.retry_count += 1
                continue

            if isinstance(result, str):
                result = Generation(text=result)
            record.update({
                "prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
                "finish_reason": result.finish_reason, "truncated": result.truncated,
                "raw_output": result.text, "raw_output_chars": len(result.text or ""),
                **({"generation_meta": result.meta} if result.meta else {}),
            })
            try:
                action, violations = self._parse(result.text, allowed)
            except ActionParseError as exc:
                record.update(parse_ok=False, parse_stage=exc.stage, parse_error=str(exc))
                attempts.append(record)
                error = f"[{exc.stage}] {exc}"
                if result.truncated or exc.stage == "unbalanced_json":
                    error += " Your output was truncated; make content significantly shorter and do not repeat it."
                if attempt < self.max_parse_retries:
                    self.retry_count += 1
                continue
            record.update(parse_ok=True, parse_stage=None, parse_error=None,
                          action_type=action.action_type, tool_name=action.tool_name,
                          envelope_violations=violations, strict_envelope=not violations)
            attempts.append(record)
            self.last_trace = {"resolution": "parsed" if not violations else "parsed_with_violations",
                               "attempts": attempts, "generator": self.generator_meta,
                               "envelope_violations": violations}
            return action

        final_stage = attempts[-1].get("parse_stage") if attempts else None
        # Distinguish "the model produced unusable text" from "we could not call it".
        reason = "model_generation_error" if final_stage == "generation_error" else "model_action_parse_failure"
        self.last_trace = {"resolution": "fallback_handoff", "attempts": attempts,
                           "generator": self.generator_meta, "final_stage": final_stage,
                           "fallback_reason": reason}
        return AgentAction.handoff(reason)
