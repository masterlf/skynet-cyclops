#!/usr/bin/env python3
"""Compatibility wrapper for the installed disposable Hermes seam verifier."""

from __future__ import annotations

from skynet_cyclops.hermes_cron_seams import main

if __name__ == "__main__":
    raise SystemExit(main())
