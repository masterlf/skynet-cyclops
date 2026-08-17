"""Safe public exceptions for Skynet-Cyclops."""

from __future__ import annotations


class CyclopsError(Exception):
    """Base exception whose message is safe to show to an operator."""


class ValidationError(CyclopsError):
    """Input failed a bounded schema or graph contract."""


class AdapterError(CyclopsError):
    """A supported Hermes CLI operation failed."""


class LedgerError(CyclopsError):
    """The private supervisor ledger is unavailable or unsafe."""


class ProjectionError(CyclopsError):
    """The public status projection is unavailable or unsafe."""
