"""Runtime grounding for search filters the user never stated.

Models sometimes add ``max_price`` or ``category`` filters on their own. Because
``search_catalog`` drops every product without a known price when ``max_price``
is set, an invented budget can silently hide the right product. The runtime
keeps only filters supported by the user's own messages and reports the rest in
the tool result, so the policy sees what was not applied.
"""

from __future__ import annotations

import re
from typing import Any

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def ground_search_filters(
    arguments: dict[str, Any], user_messages: list[str],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return applied arguments and a grounding note, or ``None`` if unchanged."""
    text = _normalize(" ".join(user_messages))
    applied = dict(arguments)
    ignored: dict[str, Any] = {}
    max_price = arguments.get("max_price")
    if max_price is not None:
        stated = [float(x) for x in _NUMBER.findall(text.replace(",", ""))]
        try:
            budget = float(max_price)
        except (TypeError, ValueError):
            budget = None
        if budget is None or not any(abs(value - budget) < 1e-6 for value in stated):
            ignored["max_price"] = applied.pop("max_price")
    category = arguments.get("category")
    if category and _normalize(str(category)) not in text:
        ignored["category"] = applied.pop("category")
    if not ignored:
        return arguments, None
    return applied, {"ignored_arguments": ignored, "reason": "not_stated_by_user"}
