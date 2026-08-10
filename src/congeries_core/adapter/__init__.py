"""Replaceable integrations for external infrastructure."""

from .sqlite_event import SqliteEventLedger

__all__ = ["SqliteEventLedger"]
