from __future__ import annotations

from ecommerce_rag.context_compaction import context_compaction_enabled


def test_canonical_compaction_setting_has_priority(monkeypatch):
    monkeypatch.setenv("ARAG_CONTEXT_COMPACTION", "1")
    monkeypatch.setenv("ERAG_CONTEXT_COMPACTION", "0")
    assert context_compaction_enabled(default=True) is False


def test_legacy_compaction_setting_is_backward_compatible(monkeypatch):
    monkeypatch.delenv("ERAG_CONTEXT_COMPACTION", raising=False)
    monkeypatch.setenv("ARAG_CONTEXT_COMPACTION", "on")
    assert context_compaction_enabled(default=False) is True


def test_compaction_setting_uses_caller_default_when_unset(monkeypatch):
    monkeypatch.delenv("ERAG_CONTEXT_COMPACTION", raising=False)
    monkeypatch.delenv("ARAG_CONTEXT_COMPACTION", raising=False)
    assert context_compaction_enabled(default=False) is False
    assert context_compaction_enabled(default=True) is True


def test_compaction_keeps_the_opt_in_attribute_view_and_nothing_new_otherwise():
    from ecommerce_rag.context_compaction import compact_tool_result

    item = {"product_id": "P00001", "title": "Acme", "doc_id": "product:P00001", "score": 0.1}
    plain = compact_tool_result("search_catalog", {"ok": True, "items": [item]})
    assert plain["items"] == [{"product_id": "P00001", "title": "Acme"}]
    shown = compact_tool_result("search_catalog", {"ok": True, "items": [{**item, "attributes": {"Color": "Blue"}}]})
    assert shown["items"][0]["attributes"] == {"Color": "Blue"}
    product = {"ok": True, "product": {"product_id": "P00001"}, "evidence": ["a"], "attributes": {"Size": "Twin"}}
    assert compact_tool_result("get_product", product)["attributes"] == {"Size": "Twin"}
    compared = compact_tool_result("compare_products", {"ok": True, "products": [product]})
    assert compared["products"][0]["attributes"] == {"Size": "Twin"}
    assert "attributes" not in compact_tool_result("get_product", {**product, "attributes": None})


def test_compacted_order_keeps_what_order_item_writes_need():
    from ecommerce_rag.context_compaction import compact_tool_result

    order = {"order_id": "O000001", "user_id": "U0001", "status": "pending", "product_id": "P00001",
             "item_ids": '["P00001"]', "payment_method_id": "credit_card_U0001", "shipping_address": "{}",
             "exchange_status": None}
    compact = compact_tool_result("get_order", {"ok": True, "order": order, "error": None})["order"]
    assert compact == {"order_id": "O000001", "user_id": "U0001", "status": "pending", "product_id": "P00001",
                       "item_ids": '["P00001"]', "payment_method_id": "credit_card_U0001"}
