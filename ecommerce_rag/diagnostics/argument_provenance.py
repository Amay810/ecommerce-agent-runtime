"""Diagnostic-only Retail argument provenance auditing.

This module intentionally has no dependency on the GRPO reward adapter, the
agent loop, Tau2 runtime, torch, or VERL.  It consumes normalized offline
messages/tool calls and reports evidence about schema validity, value
provenance, and entity binding.  It never returns a training reward and cannot
participate in a policy update.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence


_ID_KEYS = {
    "user_id",
    "order_id",
    "item_id",
    "payment_method_id",
    "product_id",
    "variant_id",
}
_ENTITY_ARGUMENTS = {"order_id", "item_id", "payment_method_id", "product_id", "variant_id"}
_ID_LIKE_ARGUMENTS = _ENTITY_ARGUMENTS | {"user_id"}
_ID_PATTERN = re.compile(r"(?:#W\w+|(?:paypal|credit_card|card|gift_card)[-_][\w-]+|\d{7,})")
_TOOL_BLOCK_PATTERN = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


@dataclass(frozen=True)
class ArgumentAudit:
    """One deterministic audit result for one argument in one tool call."""

    task_id: str
    trial: int | str | None
    turn_index: int
    tool_name: str
    argument_name: str
    argument_value: Any
    schema_status: str
    provenance_status: str
    binding_status: str
    source_turn: int | None
    source_path: str | None
    confidence: str
    notes: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Occurrence:
    value_key: str
    value: Any
    role: str
    turn_index: int
    path: str
    parent_ids: tuple[tuple[str, str], ...] = ()


def _value_key(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{type(value).__name__}:{value!s}"


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return _value_key(left) == _value_key(right) or str(left) == str(right)


def _parse_json_content(content: Any) -> Any | None:
    if isinstance(content, (dict, list)):
        return content
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    candidates = [stripped]
    for match in _TOOL_BLOCK_PATTERN.finditer(stripped):
        candidates.append(match.group(1))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
    return None


def _message_content(message: Mapping[str, Any]) -> Any:
    if "content" in message:
        return message["content"]
    if "observation" in message:
        return message["observation"]
    if "output" in message:
        return message["output"]
    return None


def _walk_values(
    value: Any,
    *,
    role: str,
    turn_index: int,
    path: str,
    parent_ids: tuple[tuple[str, str], ...] = (),
) -> Iterator[_Occurrence]:
    """Yield structured values and preserve nearest entity ancestry."""
    if isinstance(value, Mapping):
        local_ids = list(parent_ids)
        for key, child in value.items():
            if key in _ID_KEYS and isinstance(child, (str, int)):
                occurrence = _Occurrence(
                    value_key=_value_key(child),
                    value=child,
                    role=role,
                    turn_index=turn_index,
                    path=f"{path}.{key}",
                    parent_ids=tuple(local_ids),
                )
                yield occurrence
                local_ids.append((key, str(child)))
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if isinstance(child, (Mapping, list)):
                yield from _walk_values(
                    child,
                    role=role,
                    turn_index=turn_index,
                    path=child_path,
                    parent_ids=tuple(local_ids),
                )
            elif key not in _ID_KEYS:
                yield _Occurrence(
                    value_key=_value_key(child),
                    value=child,
                    role=role,
                    turn_index=turn_index,
                    path=child_path,
                    parent_ids=tuple(local_ids),
                )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_values(
                child,
                role=role,
                turn_index=turn_index,
                path=f"{path}[{index}]",
                parent_ids=parent_ids,
            )
    elif value is not None:
        yield _Occurrence(
            value_key=_value_key(value),
            value=value,
            role=role,
            turn_index=turn_index,
            path=path,
            parent_ids=parent_ids,
        )


def _structured_messages(messages: Sequence[Mapping[str, Any]]) -> Iterator[tuple[int, Mapping[str, Any], Any]]:
    for index, message in enumerate(messages):
        role = str(message.get("role") or message.get("source") or "unknown")
        content = _message_content(message)
        structured = _parse_json_content(content)
        if structured is not None:
            yield index, {"role": role}, structured
        elif content is not None:
            yield index, {"role": role}, content


def _schema_parts(tool_schema: Mapping[str, Any] | None) -> tuple[dict[str, Any], set[str]] | None:
    if not isinstance(tool_schema, Mapping):
        return None
    function = tool_schema.get("function")
    if isinstance(function, Mapping):
        tool_schema = function
    parameters = tool_schema.get("parameters") or tool_schema.get("input_schema")
    if not isinstance(parameters, Mapping):
        return None
    properties = parameters.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}
    required = parameters.get("required")
    if not isinstance(required, list):
        required = []
    return dict(properties), {str(key) for key in required}


def _type_matches(value: Any, expected: Any) -> bool:
    if not expected:
        return True
    expected_types = expected if isinstance(expected, list) else [expected]
    for kind in expected_types:
        if kind == "string" and isinstance(value, str):
            return True
        if kind == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if kind == "boolean" and isinstance(value, bool):
            return True
        if kind == "array" and isinstance(value, list):
            return True
        if kind == "object" and isinstance(value, Mapping):
            return True
        if kind == "null" and value is None:
            return True
    return False


def _schema_status(
    argument_name: str,
    argument_value: Any,
    schema: Mapping[str, Any] | None,
) -> str:
    parts = _schema_parts(schema)
    if parts is None:
        return "schema_unknown"
    properties, _ = parts
    if argument_name not in properties:
        return "extra_invalid_argument"
    definition = properties[argument_name]
    if not isinstance(definition, Mapping):
        return "schema_unknown"
    enum = definition.get("enum")
    if isinstance(enum, list) and argument_value not in enum:
        return "invalid_enum"
    if not _type_matches(argument_value, definition.get("type")):
        return "wrong_type"
    return "schema_valid"


def _is_id_like(value: Any) -> bool:
    return isinstance(value, (str, int)) and bool(_ID_PATTERN.search(str(value)))


def _index_context(messages: Sequence[Mapping[str, Any]]) -> tuple[list[_Occurrence], dict[str, list[_Occurrence]]]:
    occurrences: list[_Occurrence] = []
    for turn_index, meta, content in _structured_messages(messages):
        occurrences.extend(
            _walk_values(
                content,
                role=str(meta.get("role") or "unknown"),
                turn_index=turn_index,
                path="$",
            )
        )
        if isinstance(content, str):
            for match in _ID_PATTERN.finditer(content):
                occurrences.append(
                    _Occurrence(
                        value_key=_value_key(match.group(0)),
                        value=match.group(0),
                        role=str(meta.get("role") or "unknown"),
                        turn_index=turn_index,
                        path="$.__text__",
                    )
                )
    by_value: dict[str, list[_Occurrence]] = {}
    for occurrence in occurrences:
        by_value.setdefault(occurrence.value_key, []).append(occurrence)
    return occurrences, by_value


def _latest_occurrence(
    value: Any,
    by_value: Mapping[str, Sequence[_Occurrence]],
    *,
    preferred_roles: set[str] | None = None,
) -> _Occurrence | None:
    matches = list(by_value.get(_value_key(value), ()))
    if not matches:
        matches = [item for items in by_value.values() for item in items if _same_value(item.value, value)]
    if preferred_roles:
        preferred = [item for item in matches if item.role in preferred_roles]
        if not preferred:
            return None
        matches = preferred
    return max(matches, key=lambda item: item.turn_index) if matches else None


def _entity_maps(occurrences: Iterable[_Occurrence]) -> dict[str, dict[str, set[str]]]:
    maps: dict[str, dict[str, set[str]]] = {
        "order_user": {},
        "item_order": {},
        "payment_user": {},
        "product_item": {},
    }
    for occurrence in occurrences:
        ancestry = dict(occurrence.parent_ids)
        key = occurrence.path.rsplit(".", 1)[-1]
        value = str(occurrence.value)
        if key == "order_id":
            user_id = ancestry.get("user_id")
            if user_id:
                maps["order_user"].setdefault(value, set()).add(user_id)
        elif key == "item_id":
            order_id = ancestry.get("order_id")
            product_id = ancestry.get("product_id")
            if order_id:
                maps["item_order"].setdefault(value, set()).add(order_id)
            if product_id:
                maps["product_item"].setdefault(product_id, set()).add(value)
        elif key == "payment_method_id":
            user_id = ancestry.get("user_id")
            if user_id:
                maps["payment_user"].setdefault(value, set()).add(user_id)
    return maps


def _known_user_ids(occurrences: Iterable[_Occurrence]) -> set[str]:
    return {
        str(item.value)
        for item in occurrences
        if item.path.rsplit(".", 1)[-1] == "user_id"
    }


def _current_order_id(arguments: Mapping[str, Any], occurrences: Iterable[_Occurrence]) -> str | None:
    if arguments.get("order_id") is not None:
        return str(arguments["order_id"])
    order_occurrences = [
        item for item in occurrences if item.path.rsplit(".", 1)[-1] == "order_id"
    ]
    return str(max(order_occurrences, key=lambda item: item.turn_index).value) if order_occurrences else None


def _binding_result(
    argument_name: str,
    value: Any,
    *,
    arguments: Mapping[str, Any],
    occurrences: Sequence[_Occurrence],
    by_value: Mapping[str, Sequence[_Occurrence]],
) -> tuple[str, str]:
    """Return (binding_status, note) conservatively."""
    maps = _entity_maps(occurrences)
    known_users = _known_user_ids(occurrences)
    value_text = str(value)
    user_occurrences = [
        item
        for item in by_value.get(_value_key(value), ())
        if item.role in {"user", "customer"}
    ]
    if argument_name == "order_id":
        owners = maps["order_user"].get(value_text, set())
        if not owners:
            if _is_id_like(value):
                return "binding_uncertain", "order was not structurally linked to a user in available context"
            return "binding_uncertain", "order-like argument is not structurally identifiable"
        if known_users and owners.isdisjoint(known_users):
            return "wrong_user_entity", "order is linked to a different user than the observed user"
        return "binding_valid", "order is structurally linked to the observed user"
    if argument_name == "item_id":
        parent_orders = maps["item_order"].get(value_text, set())
        current_order = _current_order_id(arguments, occurrences)
        if not parent_orders:
            return "binding_uncertain", "item was not structurally linked to an order in available context"
        if current_order and current_order not in parent_orders:
            return "wrong_parent_entity", f"item belongs to {sorted(parent_orders)}, current order is {current_order}"
        return "binding_valid", "item is structurally linked to the current order"
    if argument_name == "payment_method_id":
        owners = maps["payment_user"].get(value_text, set())
        if known_users and owners and owners.isdisjoint(known_users):
            return "wrong_user_entity", "payment method is linked to another user"
        explicit_payment_values = {
            str(item.value)
            for items in by_value.values()
            for item in items
            if item.role in {"user", "customer"} and _is_id_like(item.value)
        }
        if explicit_payment_values and value_text not in explicit_payment_values:
            return "wrong_selected_option", "user explicitly supplied a different payment method"
        if user_occurrences:
            return "binding_valid", "payment method was explicitly selected by the user"
        if owners:
            return "binding_uncertain", "payment method belongs to the user but selection intent is absent"
        return "binding_uncertain", "payment method relation is incomplete in available context"
    if argument_name in {"product_id", "variant_id"}:
        if _latest_occurrence(value, by_value, preferred_roles={"tool", "environment", "observation"}):
            return "binding_uncertain", "product/variant was observed but parent option relation is not proven"
        return "binding_uncertain", "product/variant relation is not covered by the high-precision rules"
    if user_occurrences:
        return "binding_uncertain", "value provenance is known but semantic relation is not modeled"
    return "binding_uncertain", "argument relation is outside v1 high-precision rules"


def _provenance_result(
    argument_name: str,
    value: Any,
    by_value: Mapping[str, Sequence[_Occurrence]],
) -> tuple[str, _Occurrence | None, str]:
    user = _latest_occurrence(value, by_value, preferred_roles={"user", "customer"})
    if user is not None:
        return "user_provided_value", user, "exact value was present in a user/customer message"
    observed = _latest_occurrence(value, by_value, preferred_roles={"tool", "environment", "observation"})
    if observed is not None:
        return "observed_value", observed, "exact value was present in a tool/environment observation"
    if argument_name in _ID_LIKE_ARGUMENTS and _is_id_like(value):
        return "hallucinated_value", None, "identifier was not found in available prior user/observation context"
    return "unknown_provenance", None, "no exact prior source was found by the deterministic v1 matcher"


def audit_tool_call(
    *,
    task_id: str,
    trial: int | str | None,
    turn_index: int,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    messages: Sequence[Mapping[str, Any]],
    tool_schema: Mapping[str, Any] | None = None,
) -> list[ArgumentAudit]:
    """Audit one call against messages preceding it.

    The function is intentionally conservative.  It reports `binding_uncertain`
    rather than guessing when free text or multiple candidates make the
    relation ambiguous.
    """
    arguments = dict(arguments or {})
    occurrences, by_value = _index_context(messages)
    result: list[ArgumentAudit] = []
    parts = _schema_parts(tool_schema)
    properties, required = parts if parts is not None else ({}, set())

    def add(
        name: str,
        value: Any,
        schema_status: str,
        provenance_status: str,
        binding_status: str,
        source: _Occurrence | None,
        confidence: str,
        notes: str,
    ) -> None:
        result.append(
            ArgumentAudit(
                task_id=str(task_id),
                trial=trial,
                turn_index=int(turn_index),
                tool_name=str(tool_name),
                argument_name=str(name),
                argument_value=value,
                schema_status=schema_status,
                provenance_status=provenance_status,
                binding_status=binding_status,
                source_turn=source.turn_index if source else None,
                source_path=source.path if source else None,
                confidence=confidence,
                notes=notes,
            )
        )

    for name, value in arguments.items():
        schema_status = _schema_status(str(name), value, tool_schema)
        if schema_status != "schema_valid":
            add(
                str(name), value, schema_status, "unknown_provenance", "binding_uncertain", None, "high", "schema rule fired before semantic checks",
            )
            continue
        # Retail mutation tools commonly carry collections such as
        # ``item_ids``.  Audit each element against the singular entity key so
        # parent-order binding is not hidden by the surrounding list.
        if isinstance(value, list) and str(name).endswith("_ids"):
            singular_name = str(name)[:-1]
            if not value:
                add(
                    str(name), value, schema_status, "unknown_provenance", "binding_uncertain", None, "medium",
                    "empty identifier collection; no element-level provenance to audit",
                )
            for index, element in enumerate(value):
                element_schema_status = schema_status
                parts = _schema_parts(tool_schema)
                if parts is not None:
                    properties, _ = parts
                    definition = properties.get(str(name))
                    item_definition = definition.get("items") if isinstance(definition, Mapping) else None
                    if isinstance(item_definition, Mapping) and not _type_matches(element, item_definition.get("type")):
                        element_schema_status = "wrong_type"
                if element_schema_status != "schema_valid":
                    add(
                        f"{name}[{index}]", element, element_schema_status, "unknown_provenance", "binding_uncertain", None, "high",
                        "collection element failed the declared item schema",
                    )
                    continue
                provenance_status, source, provenance_note = _provenance_result(singular_name, element, by_value)
                binding_status, binding_note = _binding_result(
                    singular_name, element, arguments=arguments, occurrences=occurrences, by_value=by_value
                )
                confidence = "high" if binding_status in {"binding_valid", "wrong_parent_entity", "wrong_user_entity", "wrong_selected_option"} else "medium"
                if provenance_status in {"hallucinated_value", "unknown_provenance"}:
                    confidence = "high" if singular_name in _ID_LIKE_ARGUMENTS and provenance_status == "hallucinated_value" else "medium"
                add(
                    f"{name}[{index}]", element, element_schema_status, provenance_status, binding_status, source, confidence,
                    f"{provenance_note}; {binding_note}",
                )
            continue
        provenance_status, source, provenance_note = _provenance_result(str(name), value, by_value)
        binding_status, binding_note = _binding_result(
            str(name), value, arguments=arguments, occurrences=occurrences, by_value=by_value
        )
        confidence = "high" if binding_status in {"binding_valid", "wrong_parent_entity", "wrong_user_entity", "wrong_selected_option"} else "medium"
        if provenance_status in {"hallucinated_value", "unknown_provenance"}:
            confidence = "high" if str(name) in _ID_LIKE_ARGUMENTS and provenance_status == "hallucinated_value" else "medium"
        add(
            str(name), value, schema_status, provenance_status, binding_status, source, confidence,
            f"{provenance_note}; {binding_note}",
        )

    for name in sorted(required - set(arguments)):
        if name in properties:
            add(
                name, None, "missing_required_argument", "unknown_provenance", "binding_uncertain", None, "high",
                "required schema argument was absent from the call",
            )
    return result


def _call_from_message(message: Mapping[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function") if isinstance(call.get("function"), Mapping) else call
            name = function.get("name")
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if name:
                yield str(name), dict(arguments) if isinstance(arguments, Mapping) else {}
    direct_name = message.get("tool_name") or message.get("name")
    if direct_name and message.get("arguments") is not None:
        arguments = message.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        yield str(direct_name), dict(arguments) if isinstance(arguments, Mapping) else {}
    content = message.get("content")
    if isinstance(content, str):
        for match in _TOOL_BLOCK_PATTERN.finditer(content):
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping) and payload.get("name"):
                args = payload.get("arguments") or {}
                yield str(payload["name"]), dict(args) if isinstance(args, Mapping) else {}


def _messages_from_record(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("messages", "trajectory", "events"):
        value = record.get(key)
        if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
            return list(value)
    return []


def audit_trajectory(
    record: Mapping[str, Any],
    *,
    tool_schemas: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[ArgumentAudit]:
    """Audit all parseable tool calls in one offline trajectory record."""
    messages = _messages_from_record(record)
    task_id = str(record.get("task_id", "unknown"))
    trial = record.get("trial", record.get("seed"))
    audits: list[ArgumentAudit] = []
    for index, message in enumerate(messages):
        for tool_name, arguments in _call_from_message(message):
            schema = tool_schemas.get(tool_name) if tool_schemas else None
            audits.extend(
                audit_tool_call(
                    task_id=task_id,
                    trial=trial,
                    turn_index=index,
                    tool_name=tool_name,
                    arguments=arguments,
                    messages=messages[:index],
                    tool_schema=schema,
                )
            )
    return audits


def first_causal_error(
    record: Mapping[str, Any],
    audits: Sequence[ArgumentAudit] | None = None,
) -> dict[str, Any] | None:
    """Return a conservative first-causal candidate for a failed record.

    This is only a deterministic candidate generator.  When no high-confidence
    argument/schema event exists, it returns `manual_review_required` rather
    than attributing the terminal grader label to a guessed cause.
    """
    success = record.get("success")
    if success is None:
        reward = record.get("reward")
        if reward is None and isinstance(record.get("reward_info"), Mapping):
            reward = record["reward_info"].get("reward")
        success = reward == 1 or record.get("passed") is True
    if bool(success):
        return None
    task_id = str(record.get("task_id", "unknown"))
    trial = record.get("trial", record.get("seed"))
    audits = list(audits or audit_trajectory(record))
    priority = {
        "extra_invalid_argument": 1,
        "missing_required_argument": 1,
        "wrong_type": 1,
        "invalid_enum": 1,
        "hallucinated_value": 2,
        "stale_value": 2,
        "wrong_parent_entity": 2,
        "wrong_user_entity": 2,
        "wrong_selected_option": 2,
        "observed_but_wrong_entity": 2,
    }
    candidates = [item for item in audits if item.schema_status != "schema_valid" or item.provenance_status in {"hallucinated_value", "stale_value"} or item.binding_status in {"wrong_parent_entity", "wrong_user_entity", "wrong_selected_option", "observed_but_wrong_entity"}]
    if candidates:
        item = min(candidates, key=lambda value: (value.turn_index, priority.get(value.binding_status, priority.get(value.provenance_status, priority.get(value.schema_status, 9)))))
        if item.schema_status != "schema_valid":
            category, subcategory = "argument_schema", item.schema_status
        elif item.binding_status != "binding_uncertain":
            category, subcategory = "argument_grounding", item.binding_status
        else:
            category, subcategory = "argument_grounding", item.provenance_status
        return {
            "task_id": item.task_id,
            "trial": item.trial,
            "first_causal_turn": item.turn_index,
            "category": category,
            "subcategory": subcategory,
            "tool_name": item.tool_name,
            "argument_name": item.argument_name,
            "tool_correct": True,
            "argument_error": category.startswith("argument_"),
            "confidence": item.confidence,
            "notes": item.notes,
        }
    termination = str(record.get("termination_reason") or "").lower()
    if termination and termination not in {"user_stop", "agent_stop"}:
        return {
            "task_id": task_id,
            "trial": trial,
            "first_causal_turn": None,
            "category": "termination_or_max_steps",
            "subcategory": termination,
            "tool_correct": None,
            "argument_error": False,
            "confidence": "medium",
            "notes": "no earlier high-confidence schema/provenance/binding event was found",
        }
    return {
        "task_id": task_id,
        "trial": trial,
        "first_causal_turn": None,
        "category": "other",
        "subcategory": "manual_review_required",
        "tool_correct": None,
        "argument_error": None,
        "confidence": "low",
        "notes": "trajectory evidence is insufficient for deterministic first-cause attribution",
    }
