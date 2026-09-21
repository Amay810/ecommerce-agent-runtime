"""Deterministic CPU policy used to exercise the research-state wiring.

This is not a model-quality baseline.  It makes one evidence decision at a
time from the same visible state a NativeToolPolicy receives, which provides a
cheap contract test for continuation, source coverage, and budget stopping.
"""

from __future__ import annotations

import re
from typing import Any

from .domain import AgentAction, AgentObservation
from .research_state import ResearchState, budget_exhausted_answer


class EvidenceStateRulePolicy:
    """Read-only rule policy that follows derived evidence gaps."""

    privileged = False
    uses_evidence = True
    uses_research_state = True

    @staticmethod
    def _user_text(observation: AgentObservation) -> str:
        return "\n".join(str(row.get("content", "")) for row in observation.history if row.get("role") == "user")

    @staticmethod
    def _tool_events(observation: AgentObservation, name: str | None = None) -> list[dict[str, Any]]:
        return [
            row for row in observation.history
            if row.get("role") == "tool" and (name is None or row.get("name") == name)
        ]

    @staticmethod
    def _candidate_ids(observation: AgentObservation) -> list[str]:
        ids: list[str] = []
        for event in reversed(EvidenceStateRulePolicy._tool_events(observation)):
            result = event.get("result") or {}
            for item in result.get("items") or []:
                if isinstance(item, dict) and item.get("product_id"):
                    ids.append(str(item["product_id"]).upper())
            for wrapped in result.get("products") or []:
                product = wrapped.get("product") if isinstance(wrapped, dict) else None
                if isinstance(product, dict) and product.get("product_id"):
                    ids.append(str(product["product_id"]).upper())
        return list(dict.fromkeys(ids))

    @staticmethod
    def _policy_type(text: str) -> str:
        for token, value in (
            ("退货", "return"), ("退换", "return"), ("保修", "warranty"),
            ("物流", "shipping"), ("发货", "shipping"), ("发票", "invoice"),
            ("退款", "refund"),
        ):
            if token in text:
                return value
        return "return"

    def act(self, observation: AgentObservation) -> AgentAction:
        state = ResearchState(**(observation.research_state or {}))
        if state.remaining_budget == 0 and state.missing_evidence:
            return AgentAction.answer(budget_exhausted_answer(state))

        text = self._user_text(observation)
        gaps = state.missing_evidence
        called = self._tool_events(observation)
        if not called and any(gap.get("kind") == "product_candidates" for gap in gaps):
            return AgentAction.tool_call("search_catalog", query=text, top_k=5)

        for gap in gaps:
            kind = gap.get("kind")
            if kind == "comparison_coverage":
                candidates = self._candidate_ids(observation)
                if len(candidates) >= 2:
                    return AgentAction.tool_call("compare_products", product_ids=candidates[:2])
                return AgentAction.tool_call("search_catalog", query=text, top_k=5)
            if kind == "product_details":
                candidates = self._candidate_ids(observation)
                if candidates:
                    return AgentAction.tool_call("get_product", product_id=candidates[0])
            if kind == "policy_clause":
                return AgentAction.tool_call("get_policy", policy_type=self._policy_type(text))
            if kind == "order_record":
                order = re.search(r"\bO\d{6}\b", text, flags=re.I)
                code = re.search(r"(?<![A-Za-z0-9])\d{6}(?!\d)", text)
                if order and code:
                    return AgentAction.tool_call(
                        "get_order",
                        order_id=order.group(0).upper(),
                        user_id=str(observation.session.get("user_id", "")),
                        verification_code=code.group(0),
                    )
                return AgentAction.answer("请提供订单号和六位身份验证码。", requires_user_response=True)

        if gaps and state.remaining_budget != 0:
            return AgentAction.answer(
                "我已取得部分资料，但仍有信息无法由当前资料确认："
                + "；".join(str(gap.get("description")) for gap in gaps)
                + "。"
            )
        return AgentAction.answer("已根据当前取得且可追溯的资料整理结果；没有证据支持的部分不会猜测。")
