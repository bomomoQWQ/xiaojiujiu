"""Tests for the controllable clock and the process-wide rebinding."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from cf.clock import (
    ControllableClock,
    install_process_clock,
    parse_duration,
    parse_when,
)


class TestParsing:
    """Command-line parsing is the difference between a usable CLI and a puzzle."""

    @pytest.mark.parametrize(
        ("text", "seconds"),
        [
            ("90", 90.0),
            ("90s", 90.0),
            ("15m", 900.0),
            ("8h", 28800.0),
            ("3d", 259200.0),
            ("1w", 604800.0),
            ("1h30m", 5400.0),
            ("2d12h", 216000.0),
            ("1H", 3600.0),
            (" 8 h ", 28800.0),
        ],
    )
    def test_durations(self, text: str, seconds: float) -> None:
        """Every documented duration spelling parses to the right number of seconds."""
        assert parse_duration(text).total_seconds() == seconds

    @pytest.mark.parametrize("text", ["", "h", "8x", "abc", "-5h", "1h30", "8hh"])
    def test_bad_durations_are_rejected(self, text: str) -> None:
        """A duration that cannot be understood is an error, never a silent zero.

        Silently reading ``8x`` as zero would make a test that meant to skip eight
        hours skip none, and the test would still pass.
        """
        with pytest.raises(ValueError):
            parse_duration(text)

    def test_absolute_times(self) -> None:
        """ISO-8601 in several spellings, including the bare date form."""
        assert parse_when("2026-09-15T08:00:00Z") == datetime(2026, 9, 15, 8, tzinfo=timezone.utc)
        assert parse_when("2026-09-15") == datetime(2026, 9, 15, tzinfo=timezone.utc)
        assert parse_when("2026-09-15T08:00") == datetime(2026, 9, 15, 8, tzinfo=timezone.utc)
        assert parse_when("2026-09-15 08:00:00") == datetime(2026, 9, 15, 8, tzinfo=timezone.utc)

    def test_offset_is_converted_to_utc(self) -> None:
        """An instant with an offset is normalised, not reinterpreted as UTC."""
        parsed = parse_when("2026-09-15T16:00:00+08:00")
        assert parsed == datetime(2026, 9, 15, 8, tzinfo=timezone.utc)

    def test_bad_time_is_rejected(self) -> None:
        """Nonsense is an error."""
        with pytest.raises(ValueError):
            parse_when("tomorrow-ish")


class TestClock:
    """The clock's own arithmetic."""

    def test_frozen_clock_does_not_move(self) -> None:
        """A frozen clock returns the same instant no matter how long you wait."""
        clock = ControllableClock(datetime(2026, 1, 1, tzinfo=timezone.utc), frozen=True)
        first = clock.now()
        time.sleep(0.05)
        assert clock.now() == first

    def test_advance_moves_exactly(self) -> None:
        """``advance`` is a precise jump, not an approximation."""
        clock = ControllableClock(datetime(2026, 1, 1, tzinfo=timezone.utc), scale=0.0)
        clock.advance(timedelta(hours=8))
        assert clock.now() == datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
        clock.advance(timedelta(hours=-2))
        assert clock.now() == datetime(2026, 1, 1, 6, tzinfo=timezone.utc)

    def test_set_is_absolute(self) -> None:
        """``set`` jumps to an instant regardless of where the clock was."""
        # Frozen, because a *running* clock keeps moving between ``set`` and the
        # read -- asserting equality against a live clock would be asserting that
        # no time passed, which is not what "absolute" means.
        clock = ControllableClock(datetime(2026, 1, 1, tzinfo=timezone.utc), frozen=True)
        clock.set(datetime(2030, 6, 1, 12, tzinfo=timezone.utc))
        assert clock.now() == datetime(2030, 6, 1, 12, tzinfo=timezone.utc)

    def test_scale_multiplies_elapsed_real_time(self) -> None:
        """At scale N, virtual time advances about N times faster than real time."""
        clock = ControllableClock(datetime(2026, 1, 1, tzinfo=timezone.utc), scale=60.0)
        time.sleep(0.1)
        elapsed = (clock.now() - datetime(2026, 1, 1, tzinfo=timezone.utc)).total_seconds()
        assert 4.0 < elapsed < 12.0, elapsed

    def test_changing_scale_does_not_jump(self) -> None:
        """Re-scaling anchors first, so the instant in progress is preserved.

        The tolerance has to scale with the new factor: at scale 5, the few
        microseconds spent inside :meth:`set_scale` are worth tens of
        microseconds of virtual time, and at scale 1000 the same microseconds are
        worth milliseconds. That amplification is the feature, not a defect, so
        the assertion allows for it explicitly instead of pretending re-scaling
        is instantaneous.
        """
        clock = ControllableClock(datetime(2026, 1, 1, tzinfo=timezone.utc), scale=1.0)
        time.sleep(0.05)
        before = clock.now()
        started = time.monotonic()
        clock.set_scale(5.0)
        after = clock.now()
        real_elapsed = time.monotonic() - started
        assert 0 <= (after - before).total_seconds() <= real_elapsed * 5.0 + 0.02

    def test_freeze_and_unfreeze_round_trip(self) -> None:
        """Freezing pins; unfreezing resumes from the pinned instant."""
        clock = ControllableClock(datetime(2026, 1, 1, tzinfo=timezone.utc), scale=0.0)
        pinned = clock.freeze()
        time.sleep(0.05)
        assert clock.now() == pinned
        clock.advance(timedelta(hours=1))
        assert clock.now() == pinned + timedelta(hours=1)
        clock.unfreeze()
        assert clock.frozen is False

    def test_negative_scale_is_refused(self) -> None:
        """Time running backwards on its own is not a supported mode."""
        clock = ControllableClock()
        with pytest.raises(ValueError):
            clock.set_scale(-1.0)

    def test_journal_records_every_mutation(self) -> None:
        """The journal is the audit trail for what the CLI did to time."""
        clock = ControllableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        clock.advance(timedelta(hours=1))
        clock.set_scale(10.0)
        clock.freeze()
        assert [entry["action"] for entry in clock.journal] == ["init", "advance", "scale", "freeze"]

    def test_iso_matches_now(self) -> None:
        """``iso`` is just ``now`` rendered, for the ``utc_now_iso`` binding."""
        clock = ControllableClock(datetime(2026, 1, 1, tzinfo=timezone.utc), scale=0.0)
        assert clock.iso() == clock.now().isoformat()


class TestInstallProcessClock:
    """The rebinding is what makes a *program* run on virtual time."""

    def test_rebinds_direct_and_attribute_imports(self, tmp_path) -> None:
        """Both import styles are reached.

        This is the whole reason the installer walks ``sys.modules`` instead of
        patching one module: a module that did ``from .utility import utcnow``
        holds its own reference and would otherwise stay on real time.
        """
        import sys
        import types

        package = types.ModuleType("cf_probe_pkg")
        package.__path__ = []  # type: ignore[attr-defined]
        direct = types.ModuleType("cf_probe_pkg.direct")
        attribute = types.ModuleType("cf_probe_pkg.attribute")
        real_now = lambda: datetime(1999, 1, 1, tzinfo=timezone.utc)  # noqa: E731
        direct.utcnow = real_now  # type: ignore[attr-defined]
        attribute.utcnow = real_now  # type: ignore[attr-defined]
        sys.modules["cf_probe_pkg"] = package
        sys.modules["cf_probe_pkg.direct"] = direct
        sys.modules["cf_probe_pkg.attribute"] = attribute
        try:
            clock = ControllableClock(datetime(2026, 5, 5, tzinfo=timezone.utc), scale=0.0)
            rebound = install_process_clock(clock, prefixes=("cf_probe_pkg",))
            assert rebound == 2
            assert direct.utcnow() == datetime(2026, 5, 5, tzinfo=timezone.utc)
            assert attribute.utcnow() == datetime(2026, 5, 5, tzinfo=timezone.utc)
        finally:
            for name in ("cf_probe_pkg", "cf_probe_pkg.direct", "cf_probe_pkg.attribute"):
                sys.modules.pop(name, None)

    def test_ignores_unrelated_modules(self) -> None:
        """A module outside the prefix is left alone."""
        import json as unrelated

        clock = ControllableClock()
        install_process_clock(clock, prefixes=("cf_definitely_not_a_package",))
        assert unrelated.dumps({"a": 1}) == '{"a": 1}'

    def test_is_idempotent(self) -> None:
        """Running it twice reports the same bindings and changes nothing else."""
        clock = ControllableClock()
        first = install_process_clock(clock, prefixes=("cf",))
        second = install_process_clock(clock, prefixes=("cf",))
        assert first == second
