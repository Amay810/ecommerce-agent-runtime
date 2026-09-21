# -*- coding: utf-8 -*-
"""Typed tool contracts — the single source of truth for the action protocol.

The parameter schemas are plain JSON Schema, so one definition is shown to the
policy inside ``AgentObservation``, validates arguments before a tool executes,
and can back a native tool-calling API or a constrained decoder. Providers wrap
these differently (``function.parameters``, ``input_schema``, …), so the outer
envelope still needs a small adapter — the parameter schema itself is reusable
as is.

``tests/test_tool_schema.py`` asserts every schema agrees with the corresponding
``RetailTools`` method: parameter names, the required set, Python type
annotations and declared defaults, so the contract cannot silently drift.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_catalog",
        "evidence_bearing": True,
        "description": "Search the product catalogue. Use for product questions and recommendations.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language product query."},
                "top_k": {"type": "integer", "default": 5, "description": "Number of results."},
                "category": {"type": ["string", "null"], "default": None, "description": "Optional category filter."},
                "max_price": {"type": ["number", "null"], "default": None, "description": "Optional budget ceiling."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_product",
        "evidence_bearing": True,
        "description": "Fetch one product card by an internal P-prefixed ID returned by search_catalog. Product names, model numbers and SKUs must be searched first.",
        "parameters": {
            "type": "object",
            # The checked-in small corpus uses P001..P040 while the seeded
            # business environment uses five digits. Accept both internal
            # forms; external SKU-like identifiers still fail closed.
            "properties": {"product_id": {"type": "string", "pattern": r"P[0-9]{3,5}"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "compare_products",
        "evidence_bearing": True,
        "description": "Compare two or more products by id.",
        "parameters": {
            "type": "object",
            "properties": {"product_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["product_ids"],
        },
    },
    {
        "name": "get_policy",
        "evidence_bearing": True,
        "description": "Fetch one policy category using its canonical key.",
        "parameters": {
            "type": "object",
            "properties": {"policy_type": {
                "type": "string",
                "enum": ["return", "warranty", "shipping", "invoice", "refund"],
                "description": "Canonical policy key: return, warranty, shipping, invoice, or refund.",
            }},
            "required": ["policy_type"],
        },
    },
    {
        "name": "get_order",
        "evidence_bearing": True,
        "description": "Read one order. Requires the caller's six-digit verification code.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "verification_code": {"type": "string", "pattern": r"[0-9]{6}",
                                      "description": "Exactly six digits, supplied by the user in conversation."},
            },
            "required": ["order_id", "user_id", "verification_code"],
        },
    },
    {
        "name": "check_return_eligibility",
        "evidence_bearing": True,
        "description": "Check whether an order may be returned. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "verification_code": {"type": "string", "pattern": r"[0-9]{6}"},
            },
            "required": ["order_id", "user_id", "verification_code"],
        },
    },
    {
        "name": "create_return_request",
        "evidence_bearing": True,
        "description": "WRITE. Only after eligibility passes and the user explicitly confirms. If an active return request already exists, returns ok=true, changed=false, idempotent_replay=true with the existing request_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "verification_code": {"type": "string", "pattern": r"[0-9]{6}"},
                "confirmed": {"type": "boolean", "description": "True only if the user said so in this conversation."},
            },
            "required": ["order_id", "user_id", "verification_code", "confirmed"],
        },
    },
    {
        "name": "cancel_pending_order",
        "evidence_bearing": True,
        "description": "WRITE. Cancel a pending order after explicit confirmation. reason must be 'no longer needed' or 'ordered by mistake'.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "verification_code": {"type": "string", "pattern": r"[0-9]{6}"},
                "reason": {"type": "string", "enum": ["no longer needed", "ordered by mistake"]},
                "confirmed": {"type": "boolean"},
            },
            "required": ["order_id", "user_id", "verification_code", "reason", "confirmed"],
        },
    },
    {
        "name": "modify_pending_order_address",
        "evidence_bearing": True,
        "description": "WRITE. Update shipping address on a pending order after explicit confirmation.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "verification_code": {"type": "string", "pattern": r"[0-9]{6}"},
                "address1": {"type": "string"},
                "address2": {"type": "string"},
                "city": {"type": "string"},
                "state": {"type": "string"},
                "country": {"type": "string"},
                "zip": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": [
                "order_id", "user_id", "verification_code",
                "address1", "address2", "city", "state", "country", "zip", "confirmed",
            ],
        },
    },
    {
        "name": "modify_pending_order_items",
        "evidence_bearing": True,
        "description": "WRITE. Replace items on a pending order after explicit confirmation.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "verification_code": {"type": "string", "pattern": r"[0-9]{6}"},
                "item_ids": {"type": "array", "items": {"type": "string"}},
                "new_item_ids": {"type": "array", "items": {"type": "string"}},
                "payment_method_id": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": [
                "order_id", "user_id", "verification_code",
                "item_ids", "new_item_ids", "payment_method_id", "confirmed",
            ],
        },
    },
    {
        "name": "modify_pending_order_payment",
        "evidence_bearing": True,
        "description": "WRITE. Change payment method on a pending order after explicit confirmation.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "verification_code": {"type": "string", "pattern": r"[0-9]{6}"},
                "payment_method_id": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": ["order_id", "user_id", "verification_code", "payment_method_id", "confirmed"],
        },
    },
    {
        "name": "modify_user_address",
        "evidence_bearing": True,
        "description": "WRITE. Update the authenticated user's default address after explicit confirmation.",
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "verification_code": {"type": "string", "pattern": r"[0-9]{6}"},
                "address1": {"type": "string"},
                "address2": {"type": "string"},
                "city": {"type": "string"},
                "state": {"type": "string"},
                "country": {"type": "string"},
                "zip": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": [
                "user_id", "verification_code",
                "address1", "address2", "city", "state", "country", "zip", "confirmed",
            ],
        },
    },
    {
        "name": "return_delivered_order_items",
        "evidence_bearing": True,
        "description": "WRITE. Return items from a delivered eligible order after explicit confirmation.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "verification_code": {"type": "string", "pattern": r"[0-9]{6}"},
                "item_ids": {"type": "array", "items": {"type": "string"}},
                "payment_method_id": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": [
                "order_id", "user_id", "verification_code",
                "item_ids", "payment_method_id", "confirmed",
            ],
        },
    },
    {
        "name": "exchange_delivered_order_items",
        "evidence_bearing": True,
        "description": "WRITE. Exchange items on a delivered order after explicit confirmation. Only once per order.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "verification_code": {"type": "string", "pattern": r"[0-9]{6}"},
                "item_ids": {"type": "array", "items": {"type": "string"}},
                "new_item_ids": {"type": "array", "items": {"type": "string"}},
                "payment_method_id": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": [
                "order_id", "user_id", "verification_code",
                "item_ids", "new_item_ids", "payment_method_id", "confirmed",
            ],
        },
    },
    {
        "name": "escalate_to_human",
        "evidence_bearing": False,
        "description": "Hand the conversation to a human agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "reason": {"type": "string"},
                "order_id": {"type": ["string", "null"], "default": None},
            },
            "required": ["user_id", "reason"],
        },
    },
]

SCHEMA_BY_NAME: dict[str, dict[str, Any]] = {schema["name"]: schema for schema in TOOL_SCHEMAS}

#: Tools that cannot run without the user's code, derived from the contract so
#: the set cannot drift from it. ``tools.IDENTITY_GUARDED_TOOLS`` is asserted
#: equal to this in tests.
IDENTITY_TOOLS: frozenset[str] = frozenset(
    name for name, schema in SCHEMA_BY_NAME.items()
    if "verification_code" in schema["parameters"]["properties"]
)

VERIFICATION_CODE_PATTERN = r"[0-9]{6}"


def has_valid_verification_code(arguments: dict[str, Any]) -> bool:
    code = arguments.get("verification_code")
    return isinstance(code, str) and re.fullmatch(VERIFICATION_CODE_PATTERN, code) is not None

_PYTHON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


class ToolArgumentError(ValueError):
    """Raised when arguments do not satisfy a tool's schema."""


def _type_ok(value: Any, spec: Any) -> bool:
    names = spec if isinstance(spec, list) else [spec]
    for name in names:
        if name == "null":
            if value is None:
                return True
            continue
        expected = _PYTHON_TYPES.get(name)
        if not expected:
            continue
        # bool is a subclass of int; keep integer/number strict so True is not a count
        if bool not in expected and isinstance(value, bool):
            continue
        if isinstance(value, expected):
            return True
    return False


def validate_arguments(tool_name: str, arguments: dict[str, Any]) -> None:
    """Raise :class:`ToolArgumentError` unless ``arguments`` satisfies the schema."""
    schema = SCHEMA_BY_NAME.get(tool_name)
    if schema is None:
        raise ToolArgumentError(f"unknown tool: {tool_name}")
    if not isinstance(arguments, dict):
        raise ToolArgumentError("arguments must be an object")

    parameters = schema["parameters"]
    properties: dict[str, Any] = parameters["properties"]

    missing = [name for name in parameters.get("required", []) if name not in arguments]
    if missing:
        raise ToolArgumentError(f"{tool_name}: missing required argument(s): {', '.join(missing)}")

    unknown = [name for name in arguments if name not in properties]
    if unknown:
        raise ToolArgumentError(f"{tool_name}: unknown argument(s): {', '.join(sorted(unknown))}")

    for name, value in arguments.items():
        spec = properties[name]
        if not _type_ok(value, spec.get("type")):
            raise ToolArgumentError(
                f"{tool_name}.{name}: expected {spec.get('type')}, got {type(value).__name__}"
            )
        pattern = spec.get("pattern")
        if pattern and not (isinstance(value, str) and re.fullmatch(pattern, value)):
            raise ToolArgumentError(
                f"{tool_name}.{name}: {value!r} does not match required pattern {pattern}")
        choices = spec.get("enum")
        if choices is not None and value not in choices:
            raise ToolArgumentError(
                f"{tool_name}.{name}: {value!r} is not one of {', '.join(map(str, choices))}")
        if spec.get("type") == "array":
            item_type = (spec.get("items") or {}).get("type")
            bad = [x for x in value if item_type and not _type_ok(x, item_type)]
            if bad:
                raise ToolArgumentError(f"{tool_name}.{name}: items must be {item_type}")


def prompt_block(tool_schemas: Iterable[dict[str, Any]] | None = None) -> str:
    """Compact, deterministic rendering of the offered tool contract.

    ``None`` preserves the full-contract convenience used by static callers,
    while policies that operate on an observation must pass the observation's
    tool schemas explicitly.  This keeps the prompt-facing action space aligned
    with the set that the policy will accept.
    """
    lines = []
    for schema in TOOL_SCHEMAS if tool_schemas is None else tool_schemas:
        parameters = schema["parameters"]
        required = set(parameters.get("required", []))
        fields = ", ".join(
            f"{name}: {spec.get('type')}{'' if name in required else ' (optional)'}"
            for name, spec in parameters["properties"].items()
        )
        lines.append(f"- {schema['name']}({fields}) — {schema['description']}")
    return "\n".join(lines)
