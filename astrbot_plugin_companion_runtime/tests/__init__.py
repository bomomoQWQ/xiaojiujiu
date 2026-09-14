"""Tests for the companion Runtime adapter.

These tests never import AstrBot: they exercise the pure protocol, settings,
retry queue, context bridge, and outbox consumer against in-memory fakes.

Run them from the plugin directory, either way works:

    python -m pytest tests -q
    python -m unittest discover -s tests -t .
"""
