"""Rolling text log plus a structured JSONL trace.

Two views of one run, deliberately kept in step:

* **``framework.log``** -- a rolling, human-readable file (``RotatingFileHandler``,
  so a long soak test cannot fill the disk). One line per event, with the
  variables that matter rendered inline.
* **``trace.jsonl``** -- one JSON object per line, never rotated away mid-run,
  carrying the *full* variable snapshot. This is the file to grep, diff between
  runs, or feed to a plotter.

The split exists because the two audiences disagree: a human debugging "why did
it speak at 3am" wants prose and the current mood in one line, while a script
asking "did pressure ever exceed 0.8" wants every number, unmangled, on its own
line. Writing only one of them always makes the other question painful.

Nothing here raises on a logging failure. A test harness that dies because its
log directory filled up is worse than one with a gap in its log.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

LOGGER = logging.getLogger("cf.logbook")

#: Rotate at 8 MiB, keep 5 files -- ~40 MiB per run directory by default.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_BACKUPS = 5

TRACE_NAME = "trace.jsonl"
LOG_NAME = "framework.log"


@dataclass(slots=True)
class TraceRecord:
    """One structured event in the trace."""

    kind: str
    virtual_now: str
    wall_now: str
    seq: int
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the flat JSON rendering written to ``trace.jsonl``.

        The record's own routing fields are spread **last**, so a payload that
        happens to carry a ``kind`` (or ``seq``, or ``virtual_now``) cannot
        rename the record and make it invisible to ``cf tail --kind``. That is
        not hypothetical: the mock endpoint's payload carries the prompt kind,
        and an earlier version of this method let it overwrite ``kind``.

        Colliding payload keys are reported in ``shadowed_keys`` when -- and only
        when -- the record's own value actually differs, so the information is
        recoverable rather than dropped in silence. ``virtual_now`` is normally
        *lifted* from the payload by :meth:`Logbook.event`, so an identical value
        is not a collision.
        """
        record = {
            **self.data,
            "seq": self.seq,
            "kind": self.kind,
            "virtual_now": self.virtual_now,
            "wall_now": self.wall_now,
        }
        shadowed = sorted(
            key
            for key in ("seq", "kind", "virtual_now", "wall_now")
            if key in self.data and self.data[key] != record[key]
        )
        if shadowed:
            record["shadowed_keys"] = shadowed
        return record


class Logbook:
    """Owns the rolling text log and the JSONL trace for one run directory.

    Args:
        run_dir: Directory to write into; created if missing.
        max_bytes: Rotation threshold for the text log.
        backups: How many rotated text logs to keep.
        echo: Also write a compact line to stderr, for interactive runs.
        level: Minimum level for the text log.
    """

    def __init__(
        self,
        run_dir: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backups: int = DEFAULT_BACKUPS,
        echo: bool = True,
        level: int = logging.INFO,
    ) -> None:
        """Create the log directory and open both sinks."""
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.run_dir / TRACE_NAME
        self.log_path = self.run_dir / LOG_NAME
        self._lock = threading.RLock()
        self._seq = 0
        self._closed = False

        self.logger = logging.getLogger(f"cf.run.{self.run_dir.name}")
        self.logger.setLevel(level)
        self.logger.propagate = False
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
            handler.close()

        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        rolling = logging.handlers.RotatingFileHandler(
            self.log_path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        rolling.setFormatter(formatter)
        self.logger.addHandler(rolling)
        self._rolling = rolling

        if echo:
            stream = logging.StreamHandler()
            stream.setFormatter(logging.Formatter(fmt="%(message)s"))
            self.logger.addHandler(stream)
            self._stream = stream
        else:
            self._stream = None

        self._trace = self.trace_path.open("a", encoding="utf-8")
        self.event("run", {"run_dir": str(self.run_dir), "log": str(self.log_path)})

    # ------------------------------------------------------------------ writing

    def event(self, kind: str, data: Mapping[str, Any] | None = None, *, message: str = "") -> TraceRecord:
        """Record one structured event in both sinks.

        Args:
            kind: Event kind, e.g. ``heartbeat`` / ``tick`` / ``llm_request``.
            data: The full variable payload; must be JSON-serialisable.
            message: Optional prose line for the rolling log. When omitted, a
                compact rendering of ``data``'s scalar fields is used.

        Returns:
            The record that was written.

        Raises:
            TypeError: When ``data`` is not JSON-serialisable. This one *does*
                propagate: a trace you cannot parse later is not worth writing.
        """
        with self._lock:
            self._seq += 1
            moment = datetime.now(timezone.utc)
            virtual = str(data.get("virtual_now", "")) if data else ""
            record = TraceRecord(
                kind=kind,
                virtual_now=virtual,
                wall_now=moment.isoformat(),
                seq=self._seq,
                data=dict(data or {}),
            )
            payload = record.to_dict()
            # Fail loudly here rather than writing a half-line later.
            line = json.dumps(payload, ensure_ascii=False, default=str)
            if not self._closed:
                self._trace.write(line + "\n")
                self._trace.flush()
            text = message or _summarise(kind, payload)
            self.logger.info(text)
            return record

    def debug(self, kind: str, data: Mapping[str, Any] | None = None, *, message: str = "") -> None:
        """Record a low-priority event; still traced, only logged at DEBUG."""
        with self._lock:
            self._seq += 1
            record = TraceRecord(
                kind=kind,
                virtual_now=str((data or {}).get("virtual_now", "")),
                wall_now=datetime.now(timezone.utc).isoformat(),
                seq=self._seq,
                data=dict(data or {}),
            )
            if not self._closed:
                self._trace.write(json.dumps(record.to_dict(), ensure_ascii=False, default=str) + "\n")
                self._trace.flush()
            self.logger.debug(message or _summarise(kind, record.to_dict()))

    def warn(self, kind: str, data: Mapping[str, Any] | None = None, *, message: str = "") -> None:
        """Record an event that deserves a WARNING line in the rolling log."""
        with self._lock:
            self._seq += 1
            record = TraceRecord(
                kind=kind,
                virtual_now=str((data or {}).get("virtual_now", "")),
                wall_now=datetime.now(timezone.utc).isoformat(),
                seq=self._seq,
                data=dict(data or {}),
            )
            if not self._closed:
                self._trace.write(json.dumps(record.to_dict(), ensure_ascii=False, default=str) + "\n")
                self._trace.flush()
            self.logger.warning(message or _summarise(kind, record.to_dict()))

    # ------------------------------------------------------------------ reading

    def read_trace(self, *, kinds: tuple[str, ...] | None = None, limit: int = 0) -> list[dict[str, Any]]:
        """Read the trace back as a list of records.

        Args:
            kinds: Only return these event kinds.
            limit: Keep only the last N records after filtering; 0 means all.

        Returns:
            The decoded records, oldest first. Malformed lines are skipped
            rather than raising: a truncated final line is normal after a kill.
        """
        records: list[dict[str, Any]] = []
        if not self.trace_path.exists():
            return records
        with self.trace_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kinds and record.get("kind") not in kinds:
                    continue
                records.append(record)
        if limit > 0:
            return records[-limit:]
        return records

    @property
    def seq(self) -> int:
        """How many records have been written."""
        with self._lock:
            return self._seq

    # ------------------------------------------------------------------ cleanup

    def close(self) -> None:
        """Flush and close both sinks. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._trace.flush()
                self._trace.close()
            except OSError:  # pragma: no cover - closing must not raise
                LOGGER.debug("trace close failed", exc_info=True)
            for handler in list(self.logger.handlers):
                self.logger.removeHandler(handler)
                try:
                    handler.close()
                except OSError:  # pragma: no cover
                    LOGGER.debug("handler close failed", exc_info=True)

    def __enter__(self) -> Logbook:
        """Enter a context manager."""
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """Close on exit."""
        self.close()


def _summarise(kind: str, payload: Mapping[str, Any]) -> str:
    """Render a compact one-line summary of a trace record.

    Scalars come through verbatim; nested structures are collapsed so one event
    cannot wrap across a dozen lines in the rolling log.
    """
    parts: list[str] = []
    for key, value in payload.items():
        if key in {"seq", "kind", "wall_now"}:
            continue
        if key == "virtual_now":
            parts.append(f"t={value}")
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            parts.append(f"{key}={value}")
        elif isinstance(value, (list, tuple, dict)):
            parts.append(f"{key}[{len(value)}]")
    body = " ".join(parts)
    return f"[{kind}] {body}" if body else f"[{kind}]"


class LogBridge(logging.Handler):
    """Forward the program's own log records into this run's logbook.

    Without this, everything the program says about itself -- a clamped tick, a
    rejected candidate operation, a degraded provider call -- goes to stderr and
    is gone. Those lines are often the only explanation for a number that moved
    the wrong way, so they belong in the same file as the variables.

    Records are traced with ``kind="program_log"`` at their original level, so
    ``cf tail --kind program_log`` shows exactly what the program thought while
    the framework was driving it.

    Args:
        logbook: Destination.
        logger_names: Loggers to attach to. The default covers the program and
            the AstrBot adapter; ``uvicorn`` is included at WARNING so a server
            problem is visible without drowning the log in access lines.
        minimum_level: Records below this level are ignored entirely.
    """

    def __init__(
        self,
        logbook: Logbook,
        *,
        logger_names: tuple[str, ...] = ("companion_runtime", "astrbot", "uvicorn"),
        minimum_level: int = logging.INFO,
        per_logger_levels: Mapping[str, int] | None = None,
    ) -> None:
        """Attach to every named logger.

        Args:
            logbook: Destination.
            logger_names: Loggers to attach to.
            minimum_level: Default floor; records below it are ignored.
            per_logger_levels: Overrides by logger name. The AstrBot adapter needs
                DEBUG: it is *required* to swallow observation failures and report
                them at that level, so an INFO floor hides exactly the errors this
                bridge exists to surface.
        """
        # The handler's own level is the lowest floor any logger needs, so a
        # DEBUG-level logger is not filtered out by the handler itself.
        overrides = {str(k): int(v) for k, v in (per_logger_levels or {}).items()}
        super().__init__(level=min([minimum_level, *overrides.values()]))
        self.logbook = logbook
        self.logger_names = logger_names
        self._attached: list[logging.Logger] = []
        for name in logger_names:
            target = logging.getLogger(name)
            level = overrides.get(name, minimum_level)
            # A third-party logger defaults to WARNING and does not propagate to
            # a handler it never reaches; lowering the level and attaching here
            # is the only way to see the program's own diagnostics.
            if target.level == logging.NOTSET or target.level > level:
                target.setLevel(level)
            target.addHandler(self)
            self._attached.append(target)

    def emit(self, record: logging.LogRecord) -> None:
        """Trace one program log record.

        Exception info is carried through. The adapter is *required* to swallow its
        failures and reports them with ``exc_info=True`` at DEBUG, so a bridge that
        recorded only ``getMessage()`` would turn "observation failed" into a dead
        end. That is precisely what happened the first time this framework drove
        the plugin, and it cost an hour of guessing at a line that had already
        printed the answer.
        """
        try:
            detail = logging.Formatter().formatException(record.exc_info) if record.exc_info else ""
            message = record.getMessage()
            self.logbook.debug(
                "program_log",
                {
                    "logger": record.name,
                    "level": record.levelname,
                    "program_message": message,
                    "traceback": detail,
                },
                message=(
                    f"[{record.levelname.lower()}] {record.name}: {message}"
                    + (f"\n{detail}" if detail else "")
                ),
            )
        except Exception:  # noqa: BLE001 - a logging failure must never break the run
            self.handleError(record)

    def detach(self) -> None:
        """Detach from every logger. Idempotent."""
        for target in self._attached:
            try:
                target.removeHandler(self)
            except Exception:  # noqa: BLE001
                pass
        self._attached.clear()
