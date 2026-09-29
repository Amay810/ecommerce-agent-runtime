# -*- coding: utf-8 -*-
"""The tool contract must never drift from the implementation."""

import inspect
import types
import typing
import unittest

from ecommerce_rag.tool_schema import (
    SCHEMA_BY_NAME,
    TOOL_SCHEMAS,
    ToolArgumentError,
    prompt_block,
    validate_arguments,
)
from ecommerce_rag.evidence import CONVERTER_TOOLS, EVIDENCE_BEARING_TOOLS
from ecommerce_rag.tools import IDENTITY_GUARDED_TOOLS, READ_TOOLS, WRITE_TOOLS, RetailTools


def _signature(name: str) -> inspect.Signature:
    return inspect.signature(getattr(RetailTools, name))


_SCALARS = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _json_type(annotation) -> object:
    """Map a Python annotation onto the JSON Schema ``type`` it should declare."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        parts = [_json_type(arg) for arg in typing.get_args(annotation) if arg is not type(None)]
        if type(None) in typing.get_args(annotation):
            return parts[0] if len(parts) != 1 else [parts[0], "null"]
        return parts[0]
    if origin in (list, typing.List):
        return "array"
    if annotation in _SCALARS:
        return _SCALARS[annotation]
    raise AssertionError(f"unmapped annotation: {annotation!r}")


class SchemaMatchesImplementationTests(unittest.TestCase):
    def test_every_schema_has_a_method(self):
        for schema in TOOL_SCHEMAS:
            self.assertTrue(callable(getattr(RetailTools, schema["name"], None)),
                            f"{schema['name']} has a schema but no implementation")

    def test_every_declared_tool_has_a_schema(self):
        for name in READ_TOOLS | WRITE_TOOLS:
            self.assertIn(name, SCHEMA_BY_NAME, f"{name} is a declared tool with no schema")

    def test_schema_properties_match_the_signature(self):
        for schema in TOOL_SCHEMAS:
            params = [p for p in _signature(schema["name"]).parameters if p != "self"]
            self.assertEqual(sorted(schema["parameters"]["properties"]), sorted(params),
                             f"{schema['name']}: schema properties differ from the signature")

    def test_required_matches_parameters_without_defaults(self):
        for schema in TOOL_SCHEMAS:
            signature = _signature(schema["name"])
            mandatory = sorted(name for name, p in signature.parameters.items()
                               if name != "self" and p.default is inspect.Parameter.empty)
            self.assertEqual(sorted(schema["parameters"].get("required", [])), mandatory,
                             f"{schema['name']}: required set differs from the signature")

    def test_declared_types_match_the_python_annotations(self):
        # Names and required sets agreeing is not enough: a schema saying "string"
        # for a bool parameter would still let a wrong-typed call through.
        for schema in TOOL_SCHEMAS:
            hints = typing.get_type_hints(getattr(RetailTools, schema["name"]))
            for name, spec in schema["parameters"]["properties"].items():
                self.assertIn(name, hints, f"{schema['name']}.{name} has no annotation to compare against")
                self.assertEqual(spec.get("type"), _json_type(hints[name]),
                                 f"{schema['name']}.{name}: declared type differs from the annotation")

    def test_declared_defaults_match_the_signature(self):
        for schema in TOOL_SCHEMAS:
            parameters = _signature(schema["name"]).parameters
            for name, spec in schema["parameters"]["properties"].items():
                if "default" in spec:
                    self.assertEqual(spec["default"], parameters[name].default,
                                     f"{schema['name']}.{name}: declared default differs from the signature")

    def test_every_optional_parameter_declares_its_default(self):
        # Checking only already-declared defaults would let a parameter omit one
        # entirely and still pass, so the contract would be silently incomplete.
        for schema in TOOL_SCHEMAS:
            parameters = _signature(schema["name"]).parameters
            required = set(schema["parameters"].get("required", []))
            for name, spec in schema["parameters"]["properties"].items():
                if name in required:
                    self.assertNotIn("default", spec, f"{schema['name']}.{name} is required but declares a default")
                    continue
                self.assertIn("default", spec,
                              f"{schema['name']}.{name} is optional but declares no default "
                              f"(signature default is {parameters[name].default!r})")

    def test_array_parameters_declare_their_item_type(self):
        for schema in TOOL_SCHEMAS:
            hints = typing.get_type_hints(getattr(RetailTools, schema["name"]))
            for name, spec in schema["parameters"]["properties"].items():
                if spec.get("type") != "array":
                    continue
                (item_annotation,) = typing.get_args(hints[name])
                self.assertEqual((spec.get("items") or {}).get("type"), _json_type(item_annotation),
                                 f"{schema['name']}.{name}: item type differs from the annotation")


class IdentityGuardCoverageTests(unittest.TestCase):
    def test_every_evidence_bearing_tool_has_a_converter(self):
        self.assertEqual(EVIDENCE_BEARING_TOOLS, CONVERTER_TOOLS)
    """The central guard promises no order tool can be added without protection.

    That only holds if membership of IDENTITY_GUARDED_TOOLS is derived from the
    contract rather than maintained by hand, so assert the two agree.
    """

    def test_every_tool_taking_a_verification_code_is_identity_guarded(self):
        takes_code = {schema["name"] for schema in TOOL_SCHEMAS
                      if "verification_code" in schema["parameters"]["properties"]}
        missing = sorted(takes_code - IDENTITY_GUARDED_TOOLS)
        self.assertEqual(missing, [], f"unguarded tools accepting a verification code: {missing}")

    def test_no_guarded_tool_lacks_a_verification_code(self):
        for name in IDENTITY_GUARDED_TOOLS:
            self.assertIn("verification_code", SCHEMA_BY_NAME[name]["parameters"]["properties"],
                          f"{name} is identity-guarded but declares no verification_code")

    def test_verification_code_uses_ascii_digits_only(self):
        # `\d` matches Unicode decimals, so it would accept full-width digits
        for name in IDENTITY_GUARDED_TOOLS:
            spec = SCHEMA_BY_NAME[name]["parameters"]["properties"]["verification_code"]
            self.assertEqual(spec.get("pattern"), r"[0-9]{6}", f"{name} uses a non-ASCII digit class")


class ValidateArgumentsTests(unittest.TestCase):
    def test_accepts_a_well_formed_call(self):
        validate_arguments("get_order", {"order_id": "O000001", "user_id": "U0001", "verification_code": "123456"})
        validate_arguments("search_catalog", {"query": "耳机", "top_k": 3, "max_price": 199.5, "category": None})

    def test_missing_required_argument_is_reported_by_name(self):
        with self.assertRaises(ToolArgumentError) as ctx:
            validate_arguments("get_order", {"order_id": "O000001"})
        self.assertIn("user_id", str(ctx.exception))
        self.assertIn("verification_code", str(ctx.exception))

    def test_unknown_argument_is_rejected(self):
        with self.assertRaises(ToolArgumentError) as ctx:
            validate_arguments("get_policy", {"policy_type": "return", "locale": "zh"})
        self.assertIn("locale", str(ctx.exception))

    def test_wrong_type_is_rejected(self):
        # the failure mode that matters: a model emitting "true" instead of true
        with self.assertRaises(ToolArgumentError):
            validate_arguments("create_return_request", {"order_id": "O1", "user_id": "U1",
                                                         "verification_code": "123456", "confirmed": "true"})

    def test_boolean_is_not_accepted_as_a_number(self):
        with self.assertRaises(ToolArgumentError):
            validate_arguments("search_catalog", {"query": "x", "top_k": True})

    def test_nullable_field_accepts_none_but_not_a_wrong_type(self):
        validate_arguments("escalate_to_human", {"user_id": "U1", "reason": "r", "order_id": None})
        with self.assertRaises(ToolArgumentError):
            validate_arguments("escalate_to_human", {"user_id": "U1", "reason": "r", "order_id": 12})

    def test_array_item_type_is_enforced(self):
        validate_arguments("compare_products", {"product_ids": ["P00001", "P00002"]})
        with self.assertRaises(ToolArgumentError):
            validate_arguments("compare_products", {"product_ids": [1, 2]})

    def test_compare_products_requires_at_least_two_ids(self):
        for product_ids in ([], ["P00001"]):
            with self.subTest(product_ids=product_ids), self.assertRaises(ToolArgumentError) as ctx:
                validate_arguments("compare_products", {"product_ids": product_ids})
            self.assertIn("at least 2", str(ctx.exception))

    def test_compare_products_runtime_rejects_short_list(self):
        tools = RetailTools(":memory:")
        self.assertEqual(
            tools.compare_products(["P00001"]),
            {"ok": False, "error": "at_least_two_products_required", "products": []},
        )
        result = tools.call("compare_products", product_ids=["P00001"])
        self.assertFalse(result["ok"])
        self.assertIn("at least 2", result["error"])

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(ToolArgumentError):
            validate_arguments("drop_database", {})

    def test_product_detail_accepts_only_internal_ids(self):
        validate_arguments("get_product", {"product_id": "P00001"})
        for external_id in ("Ce-H22B12-S1", "QGHXO", "PSAMGLXTLC26"):
            with self.subTest(external_id=external_id), self.assertRaises(ToolArgumentError):
                validate_arguments("get_product", {"product_id": external_id})

    def test_policy_type_is_a_canonical_enum(self):
        for policy_type in ("return", "warranty", "shipping", "invoice", "refund"):
            validate_arguments("get_policy", {"policy_type": policy_type})
        with self.assertRaises(ToolArgumentError):
            validate_arguments("get_policy", {"policy_type": "保修"})


class PromptBlockTests(unittest.TestCase):
    def test_prompt_block_lists_every_tool_and_is_deterministic(self):
        block = prompt_block()
        for schema in TOOL_SCHEMAS:
            self.assertIn(schema["name"], block)
        self.assertEqual(block, prompt_block())

    def test_prompt_block_can_render_an_offered_subset(self):
        offered = [SCHEMA_BY_NAME[name] for name in ("search_catalog", "get_policy", "get_order")]
        block = prompt_block(offered)

        for name in ("search_catalog", "get_policy", "get_order"):
            self.assertIn(name, block)
        for schema in TOOL_SCHEMAS:
            if schema["name"] not in {item["name"] for item in offered}:
                self.assertNotIn(f"- {schema['name']}(", block)


if __name__ == "__main__":
    unittest.main()
