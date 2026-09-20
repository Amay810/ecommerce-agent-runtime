"""MCP exposure for the guarded retail tool surface.

MCP is an integration boundary, not an alternate execution path. Every call is
delegated to :class:`RetailTools`, including identity and transactional guards.
The authenticated user id is server-side configuration and is never accepted
from an MCP tool argument.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

from .tools import RetailTools


MCP_TOOL_NAMES = (
    "search_catalog",
    "get_product",
    "compare_products",
    "get_policy",
    "get_order",
    "check_return_eligibility",
    "create_return_request",
    "cancel_pending_order",
    "modify_pending_order_address",
    "modify_pending_order_items",
    "modify_pending_order_payment",
    "modify_user_address",
    "return_delivered_order_items",
    "exchange_delivered_order_items",
    "escalate_to_human",
)


class MCPRetailFacade:
    def __init__(self, tools: RetailTools, user_id: str, session_id: str | None = None):
        self.tools = tools
        self.user_id = user_id
        self.session_id = session_id or f"mcp_{uuid.uuid4().hex[:12]}"

    def _user_call(self, name: str, **arguments: Any) -> dict[str, Any]:
        if not self.user_id:
            return {"ok": False, "changed": False, "error": "mcp_user_not_configured"}
        return self.tools.call(name, _session_id=self.session_id, user_id=self.user_id, **arguments)

    def issue_confirmation(self, operation: str, arguments: dict[str, Any], request_text: str) -> str:
        """Trusted host callback used before presenting a confirmation prompt."""

        return self.tools.issue_confirmation(
            session_id=self.session_id,
            user_id=self.user_id,
            operation=operation,
            # The authenticated facade identity must win over untrusted callback
            # arguments, just as it does for normal MCP tool calls.
            arguments={**arguments, "user_id": self.user_id},
            request_text=request_text,
        )

    def record_user_confirmation(self, response_text: str) -> dict[str, Any]:
        """Trusted host callback used with the actual user response."""

        return self.tools.record_user_confirmation(
            session_id=self.session_id,
            response_text=response_text,
        )

    def search_catalog(self, query: str, top_k: int = 5,
                       category: str | None = None, max_price: float | None = None) -> dict[str, Any]:
        """Search products with optional category and budget constraints."""
        return self.tools.call("search_catalog", query=query, top_k=top_k,
                               category=category, max_price=max_price)

    def get_product(self, product_id: str) -> dict[str, Any]:
        """Read one product card using an internal product id from search."""
        return self.tools.call("get_product", product_id=product_id)

    def compare_products(self, product_ids: list[str]) -> dict[str, Any]:
        """Compare two or more products by internal id."""
        return self.tools.call("compare_products", product_ids=product_ids)

    def get_policy(self, policy_type: str) -> dict[str, Any]:
        """Read return, warranty, shipping, invoice, or refund policy."""
        return self.tools.call("get_policy", policy_type=policy_type)

    def get_order(self, order_id: str, verification_code: str) -> dict[str, Any]:
        """Read an authenticated user's order after six-digit verification."""
        return self._user_call("get_order", order_id=order_id, verification_code=verification_code)

    def check_return_eligibility(self, order_id: str, verification_code: str) -> dict[str, Any]:
        """Check return eligibility without changing order state."""
        return self._user_call("check_return_eligibility", order_id=order_id,
                               verification_code=verification_code)

    def create_return_request(self, order_id: str, verification_code: str,
                              confirmed: bool) -> dict[str, Any]:
        """Create a return request only after eligibility and explicit confirmation."""
        return self._user_call("create_return_request", order_id=order_id,
                               verification_code=verification_code, confirmed=confirmed)

    def cancel_pending_order(self, order_id: str, verification_code: str,
                             reason: str, confirmed: bool) -> dict[str, Any]:
        """Cancel a pending order after explicit confirmation."""
        return self._user_call(
            "cancel_pending_order",
            order_id=order_id,
            verification_code=verification_code,
            reason=reason,
            confirmed=confirmed,
        )

    def modify_pending_order_address(
        self,
        order_id: str,
        verification_code: str,
        address1: str,
        address2: str,
        city: str,
        state: str,
        country: str,
        zip: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        """Update shipping address on a pending order after confirmation."""
        return self._user_call(
            "modify_pending_order_address",
            order_id=order_id,
            verification_code=verification_code,
            address1=address1,
            address2=address2,
            city=city,
            state=state,
            country=country,
            zip=zip,
            confirmed=confirmed,
        )

    def modify_pending_order_items(
        self,
        order_id: str,
        verification_code: str,
        item_ids: list[str],
        new_item_ids: list[str],
        payment_method_id: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        """Replace items on a pending order after confirmation."""
        return self._user_call(
            "modify_pending_order_items",
            order_id=order_id,
            verification_code=verification_code,
            item_ids=item_ids,
            new_item_ids=new_item_ids,
            payment_method_id=payment_method_id,
            confirmed=confirmed,
        )

    def modify_pending_order_payment(
        self,
        order_id: str,
        verification_code: str,
        payment_method_id: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        """Change payment method on a pending order after confirmation."""
        return self._user_call(
            "modify_pending_order_payment",
            order_id=order_id,
            verification_code=verification_code,
            payment_method_id=payment_method_id,
            confirmed=confirmed,
        )

    def modify_user_address(
        self,
        verification_code: str,
        address1: str,
        address2: str,
        city: str,
        state: str,
        country: str,
        zip: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        """Update the authenticated user's default address after confirmation."""
        return self._user_call(
            "modify_user_address",
            verification_code=verification_code,
            address1=address1,
            address2=address2,
            city=city,
            state=state,
            country=country,
            zip=zip,
            confirmed=confirmed,
        )

    def return_delivered_order_items(
        self,
        order_id: str,
        verification_code: str,
        item_ids: list[str],
        payment_method_id: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        """Return delivered order items after eligibility and confirmation."""
        return self._user_call(
            "return_delivered_order_items",
            order_id=order_id,
            verification_code=verification_code,
            item_ids=item_ids,
            payment_method_id=payment_method_id,
            confirmed=confirmed,
        )

    def exchange_delivered_order_items(
        self,
        order_id: str,
        verification_code: str,
        item_ids: list[str],
        new_item_ids: list[str],
        payment_method_id: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        """Exchange delivered order items after confirmation."""
        return self._user_call(
            "exchange_delivered_order_items",
            order_id=order_id,
            verification_code=verification_code,
            item_ids=item_ids,
            new_item_ids=new_item_ids,
            payment_method_id=payment_method_id,
            confirmed=confirmed,
        )

    def escalate_to_human(self, reason: str, order_id: str | None = None) -> dict[str, Any]:
        """Hand the authenticated user's conversation to a human agent."""
        return self._user_call("escalate_to_human", reason=reason, order_id=order_id)


def build_server(tools: RetailTools, user_id: str) -> "FastMCP":
    from mcp.server.fastmcp import FastMCP

    facade = MCPRetailFacade(tools, user_id)
    server = FastMCP(
        "Trusted E-commerce Tools",
        instructions=(
            "Retail tools backed by the same identity, eligibility, confirmation, "
            "and transactional guardrails as the customer-support agent."
        ),
        stateless_http=True,
        json_response=True,
    )
    for name in MCP_TOOL_NAMES:
        server.tool()(getattr(facade, name))
    return server


def _runtime_tools() -> RetailTools:
    db_path = Path(os.getenv("ERAG_MCP_DB", "logs/demo_agent.db"))
    index_path = os.getenv("ERAG_MCP_INDEX", "").strip()
    retriever = None
    if index_path:
        from .hybrid_retriever import HybridRetriever
        retriever = HybridRetriever(Path(index_path))
    return RetailTools(db_path, retriever)


def _runtime_user_id() -> str:
    user_id = os.getenv("ERAG_MCP_USER_ID", "").strip()
    if not user_id:
        raise RuntimeError(
            "ERAG_MCP_USER_ID is required: MCP identity is server-side configuration"
        )
    return user_id


def main() -> None:
    server = build_server(_runtime_tools(), _runtime_user_id())
    transport = os.getenv("ERAG_MCP_TRANSPORT", "stdio")
    if transport not in {"stdio", "streamable-http", "sse"}:
        raise ValueError("ERAG_MCP_TRANSPORT must be stdio, streamable-http, or sse")
    server.run(transport=transport)


if __name__ == "__main__":
    main()
