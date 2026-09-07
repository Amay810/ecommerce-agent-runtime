"""Offline diagnostics that are deliberately outside the training path."""

from .argument_provenance import (
    ArgumentAudit,
    audit_tool_call,
    audit_trajectory,
    first_causal_error,
)

__all__ = [
    "ArgumentAudit",
    "audit_tool_call",
    "audit_trajectory",
    "first_causal_error",
]
