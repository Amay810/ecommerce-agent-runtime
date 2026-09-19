"""Trusted, session-local confirmation records for transactional tools.

The model may propose a write and may include the legacy ``confirmed`` field,
but it cannot create an authorization record.  Only the trusted interaction
path can issue a pending request and record the user's response.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any


_ORDER_ID = re.compile(r"\bO[0-9]{6}\b", re.IGNORECASE)
_NEGATIVE = re.compile(
    r"(?:不确认|不\s*同意|不同意|不要|拒绝|别执行|不执行|取消|撤回|先不|不用|不想|"
    r"cannot|can't|do not|don't|not now|decline|拒绝提供)",
    re.IGNORECASE,
)
_POSITIVE = re.compile(
    r"^(?:yes|y|yep|yeah|ok|okay|correct|confirmed?|confirm|go ahead|do it|proceed|"
    r"好的?|可以|是的|确认(?:提交退货|退货申请|执行)?|同意(?:提交退货|执行)?|请执行|执行吧|提交吧)[，。！!,.\s]*$",
    re.IGNORECASE,
)


def canonical_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Return stable business arguments, excluding model-only confirmation."""

    return {
        key: parameters[key]
        for key in sorted(parameters)
        if key not in {"confirmed", "confirmation_id"}
    }


def parameters_hash(parameters: dict[str, Any]) -> str:
    payload = json.dumps(canonical_parameters(parameters), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def confirmation_decision(text: str) -> bool | None:
    """Parse only an explicit response; negative language always wins."""

    normalized = " ".join(str(text or "").strip().split())
    if not normalized or _NEGATIVE.search(normalized):
        return False if normalized else None
    return True if _POSITIVE.fullmatch(normalized) else None


@dataclass
class ConfirmationRecord:
    request_id: str
    session_id: str
    user_id: str
    operation: str
    parameter_hash: str
    parameters: dict[str, Any]
    request_text: str
    status: str = "pending"  # pending | authorized | rejected | revoked
    response_text: str = ""
    authorization_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "operation": self.operation,
            "parameter_hash": self.parameter_hash,
            "parameters": self.parameters,
            "request_text": self.request_text,
            "status": self.status,
            "response_text": self.response_text,
            "authorization_id": self.authorization_id,
        }


@dataclass
class ConfirmationLedger:
    """In-memory trusted ledger scoped to one runtime/session boundary."""

    records: dict[str, ConfirmationRecord] = field(default_factory=dict)

    def issue(
        self,
        *,
        session_id: str,
        user_id: str,
        operation: str,
        parameters: dict[str, Any],
        request_text: str,
    ) -> str:
        self.revoke_session(session_id, "superseded_by_new_request")
        record = ConfirmationRecord(
            request_id=f"cnf_req_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            user_id=user_id,
            operation=operation,
            parameter_hash=parameters_hash(parameters),
            parameters=canonical_parameters(parameters),
            request_text=request_text,
        )
        self.records[record.request_id] = record
        return record.request_id

    def respond(self, *, session_id: str, response_text: str) -> dict[str, Any]:
        record = self._current(session_id)
        if record is None:
            return {"ok": False, "decision": None, "reason": "no_pending_confirmation"}
        decision = confirmation_decision(response_text)
        order_ids = {value.upper() for value in _ORDER_ID.findall(response_text or "")}
        expected_order = {
            str(record.parameters.get("order_id", "")).upper()
        } - {""}
        changed_target = bool(order_ids and order_ids != expected_order)
        changed_request = any(
            marker in (response_text or "").lower()
            for marker in ("改为", "换成", "instead", "change it", "different order")
        )
        if decision is not True or changed_target or changed_request:
            record.status = "rejected" if decision is False else "revoked"
            record.response_text = response_text
            record.authorization_id = None
            return {
                "ok": True,
                "decision": False if decision is False else None,
                "reason": "confirmation_rejected" if decision is False else "confirmation_not_bound",
                "request_id": record.request_id,
            }
        record.status = "authorized"
        record.response_text = response_text
        record.authorization_id = f"cnf_auth_{uuid.uuid4().hex[:12]}"
        return {
            "ok": True,
            "decision": True,
            "request_id": record.request_id,
            "authorization_id": record.authorization_id,
        }

    def authorization_for(
        self,
        *,
        session_id: str,
        user_id: str,
        operation: str,
        parameters: dict[str, Any],
    ) -> str | None:
        expected_hash = parameters_hash(parameters)
        for record in reversed(list(self.records.values())):
            if (
                record.session_id == session_id
                and record.user_id == user_id
                and record.operation == operation
                and record.parameter_hash == expected_hash
                and record.status == "authorized"
            ):
                return record.authorization_id
        return None

    def validate_authorization(
        self,
        *,
        authorization_id: str | None,
        session_id: str | None,
        user_id: str | None,
        operation: str,
        parameters: dict[str, Any],
    ) -> bool:
        if not authorization_id or not session_id or not user_id:
            return False
        expected_hash = parameters_hash(parameters)
        return any(
            record.authorization_id == authorization_id
            and record.session_id == session_id
            and record.user_id == user_id
            and record.operation == operation
            and record.parameter_hash == expected_hash
            and record.status == "authorized"
            for record in self.records.values()
        )

    def revoke_session(self, session_id: str, reason: str = "revoked") -> None:
        for record in self.records.values():
            if record.session_id == session_id and record.status in {"pending", "authorized"}:
                record.status = "revoked"
                record.response_text = reason
                record.authorization_id = None

    def _current(self, session_id: str) -> ConfirmationRecord | None:
        active = [
            record
            for record in self.records.values()
            if record.session_id == session_id and record.status == "pending"
        ]
        return active[-1] if active else None
