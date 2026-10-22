"""Centralized factories for agents, sandboxes, and security checks.

These factories eliminate duplication across graph.py, async_runner.py,
and server/app.py — providing a single place to change provider routing,
sandbox selection, and security pipeline logic.
"""
