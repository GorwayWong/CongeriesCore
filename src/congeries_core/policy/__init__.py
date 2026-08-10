"""Scope-based, deny-by-default authorization boundary."""

from congeries_core.runtime.scope import CoreScopeKind, ScopeRef

from .authorization import (
    AccessRequest,
    ActionRef,
    ActionRegistry,
    AuthorizationPolicy,
    AuthorizedCall,
    AuthorizedDispatcher,
    CorePrincipalKind,
    DenyAllPolicy,
    Grant,
    PolicyDecision,
    PolicyEffect,
    ResourceRef,
    RuntimePrincipal,
)
from .integration import RunAuditFailureHandler

__all__ = [
    "AccessRequest",
    "ActionRef",
    "ActionRegistry",
    "AuthorizationPolicy",
    "AuthorizedCall",
    "AuthorizedDispatcher",
    "CorePrincipalKind",
    "CoreScopeKind",
    "DenyAllPolicy",
    "Grant",
    "PolicyDecision",
    "PolicyEffect",
    "ResourceRef",
    "RunAuditFailureHandler",
    "RuntimePrincipal",
    "ScopeRef",
]
