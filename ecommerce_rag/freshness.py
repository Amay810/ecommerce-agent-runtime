# -*- coding: utf-8 -*-
"""Freshness guardrail — the e-commerce analogue of point-in-time grounding.

A customer-support answer must not assert price / inventory / policy facts that may be
stale. When the answer makes such a claim, we check the backing snapshot item's
`default_updated_at`: if missing (unverified) or older than MAX_AGE_DAYS (stale), the
agent hedges (downgrade to caution + advise verifying live info / human handoff).

Pure-stdlib and model-free, so it is fully unit-testable without the retriever.
"""

import re
from datetime import datetime, timezone

from . import config

PRICE_RE = re.compile(r"\d+(?:\.\d+)?\s*元|价格|售价|多少钱|优惠|折扣")
INVENTORY_TERMS = ("现货", "预售", "缺货", "售罄", "库存", "补货", "有货", "无货", "发货")
POLICY_TERMS = ("退货", "换货", "退款", "保修", "维修", "质保", "发票", "运费", "配送", "七天")


def detect_claims(answer: str) -> set[str]:
    """Which transactional claim types appear in the answer text."""
    if not answer:
        return set()
    claims = set()
    if PRICE_RE.search(answer):
        claims.add("price")
    if any(t in answer for t in INVENTORY_TERMS):
        claims.add("inventory")
    if any(t in answer for t in POLICY_TERMS):
        claims.add("policy")
    return claims


def _parse_date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        try:
            dt = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _item_status(item: dict, now: datetime, max_age_days: int) -> str:
    dt = _parse_date(item.get("default_updated_at"))
    if dt is None:
        return "unverified"
    return "stale" if (now - dt).days > max_age_days else "fresh"


def assess(snapshot: dict, intent: str, answer: str, now: datetime | None = None,
           max_age_days: int | None = None) -> dict:
    """Return a freshness verdict for an answer given its product/policy snapshot.

    status: "n/a" (no transactional claim) | "fresh" | "unverified" | "stale".
    Aggregation is worst-case: any stale -> stale; else any unverified -> unverified; else fresh.
    """
    max_age_days = config.FRESHNESS_MAX_AGE_DAYS if max_age_days is None else max_age_days
    now = now or datetime.now(timezone.utc)
    claims = detect_claims(answer)
    if not claims:
        return {"triggered": False, "status": "n/a", "claims": [], "reasons": []}

    items: list[dict] = []
    if claims & {"price", "inventory"}:
        items += snapshot.get("products", []) or []
    if "policy" in claims:
        items += snapshot.get("policies", []) or []

    statuses = [_item_status(it, now, max_age_days) for it in items] or ["unverified"]
    if "stale" in statuses:
        status = "stale"
    elif "unverified" in statuses:
        status = "unverified"
    else:
        status = "fresh"

    reasons = []
    if status == "stale":
        reasons.append(f"引用的{'/'.join(sorted(claims))}信息超过{max_age_days}天未更新，可能已变动。")
    elif status == "unverified":
        reasons.append(f"引用的{'/'.join(sorted(claims))}信息缺少更新时间，无法确认时效。")
    return {"triggered": True, "status": status, "claims": sorted(claims),
            "reasons": reasons, "max_age_days": max_age_days}
