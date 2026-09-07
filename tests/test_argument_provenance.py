import unittest

from ecommerce_rag.diagnostics.argument_provenance import audit_tool_call, first_causal_error


ORDER_SCHEMA = {
    "name": "modify_pending_order_items",
    "parameters": {
        "type": "object",
        "required": ["order_id", "item_ids"],
        "properties": {
            "order_id": {"type": "string"},
            "item_ids": {"type": "array"},
        },
    },
}

PAYMENT_SCHEMA = {
    "name": "modify_pending_order_payment",
    "parameters": {
        "type": "object",
        "required": ["order_id", "payment_method_id"],
        "properties": {
            "order_id": {"type": "string"},
            "payment_method_id": {"type": "string"},
        },
    },
}


def _rows(arguments, messages, schema=ORDER_SCHEMA, tool_name="modify_pending_order_items"):
    return audit_tool_call(
        task_id="t1",
        trial=0,
        turn_index=len(messages),
        tool_name=tool_name,
        arguments=arguments,
        messages=messages,
        tool_schema=schema,
    )


class ArgumentProvenanceTests(unittest.TestCase):
    def test_valid_order_id_is_observed_and_bound(self):
        rows = _rows(
            {"order_id": "#W1", "item_ids": ["i1"]},
            [{"role": "tool", "content": {"user_id": "u1", "orders": [{"order_id": "#W1", "items": [{"item_id": "i1"}]}]}}],
        )
        order = next(row for row in rows if row.argument_name == "order_id")
        self.assertEqual(order.provenance_status, "observed_value")
        self.assertEqual(order.binding_status, "binding_valid")

    def test_hallucinated_order_id(self):
        row = next(iter(_rows({"order_id": "#W999", "item_ids": []}, [])))
        self.assertEqual(row.provenance_status, "hallucinated_value")
        self.assertEqual(row.binding_status, "binding_uncertain")

    def test_item_from_correct_order(self):
        rows = _rows(
            {"order_id": "#W1", "item_ids": ["i1"]},
            [{"role": "tool", "content": {"order_id": "#W1", "items": [{"item_id": "i1"}]}}],
        )
        item = next(row for row in rows if row.argument_name == "item_ids[0]")
        self.assertEqual(item.schema_status, "schema_valid")
        self.assertEqual(item.binding_status, "binding_valid")

    def test_item_from_wrong_order_is_not_promoted_to_db_mismatch(self):
        rows = _rows(
            {"order_id": "#W1", "item_ids": ["i2"]},
            [{"role": "tool", "content": {"orders": [
                {"order_id": "#W1", "items": [{"item_id": "i1"}]},
                {"order_id": "#W2", "items": [{"item_id": "i2"}]},
            ]}}],
        )
        item = next(row for row in rows if row.argument_name == "item_ids[0]")
        self.assertEqual(item.binding_status, "wrong_parent_entity")

    def test_valid_payment_method_is_user_selected(self):
        rows = _rows(
            {"order_id": "#W1", "payment_method_id": "card-1"},
            [{"role": "user", "content": "Please use card-1."}, {"role": "tool", "content": {"user_id": "u1", "payment_methods": [{"payment_method_id": "card-1"}]}}],
            schema=PAYMENT_SCHEMA,
            tool_name="modify_pending_order_payment",
        )
        payment = next(row for row in rows if row.argument_name == "payment_method_id")
        self.assertEqual(payment.provenance_status, "user_provided_value")
        self.assertEqual(payment.binding_status, "binding_valid")

    def test_wrong_observed_payment_method_is_flagged_when_user_selected_another(self):
        rows = _rows(
            {"order_id": "#W1", "payment_method_id": "card-1"},
            [{"role": "user", "content": "Use card-2."}, {"role": "tool", "content": {"user_id": "u1", "payment_methods": [{"payment_method_id": "card-1"}, {"payment_method_id": "card-2"}]}}],
            schema=PAYMENT_SCHEMA,
            tool_name="modify_pending_order_payment",
        )
        payment = next(row for row in rows if row.argument_name == "payment_method_id")
        self.assertEqual(payment.provenance_status, "observed_value")
        self.assertEqual(payment.binding_status, "wrong_selected_option")

    def test_missing_required_argument(self):
        rows = _rows({"order_id": "#W1"}, [])
        item = next(row for row in rows if row.argument_name == "item_ids")
        self.assertEqual(item.schema_status, "missing_required_argument")

    def test_schema_invalid_extra_and_wrong_type(self):
        schema = {"name": "tool", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}}}
        rows = _rows({"order_id": 7, "not_allowed": "x"}, [], schema=schema, tool_name="tool")
        by_name = {row.argument_name: row for row in rows}
        self.assertEqual(by_name["order_id"].schema_status, "wrong_type")
        self.assertEqual(by_name["not_allowed"].schema_status, "extra_invalid_argument")

    def test_ambiguous_binding_stays_uncertain(self):
        rows = _rows(
            {"product_id": "p1"},
            [{"role": "tool", "content": {"product_id": "p1", "variants": [{"item_id": "i1"}, {"item_id": "i2"}]}}],
            schema={"name": "lookup", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}}},
            tool_name="lookup",
        )
        product = next(row for row in rows if row.argument_name == "product_id")
        self.assertEqual(product.binding_status, "binding_uncertain")

    def test_failed_record_without_evidence_requires_manual_review(self):
        cause = first_causal_error({"task_id": "t1", "trial": 0, "success": False, "termination_reason": "user_stop"}, [])
        self.assertEqual(cause["subcategory"], "manual_review_required")
        self.assertEqual(cause["confidence"], "low")

    def test_reward_info_marks_canonical_success_as_not_a_failure(self):
        cause = first_causal_error(
            {"task_id": "t1", "trial": 0, "reward_info": {"reward": 1}, "termination_reason": "user_stop"},
            [],
        )
        self.assertIsNone(cause)
