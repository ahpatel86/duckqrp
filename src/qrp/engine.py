"""
Execution engine: a DuckDB connection plus an explicit stage boundary.

Why a stage runner at all
-------------------------
The problem in the Spark port was not that it lacked materialisation
boundaries — it was that the boundaries were implicit, hand-placed as 50
scattered `localCheckpoint(eager=True)` calls tuned by trial and error to
one dataset size.

Here a stage boundary is a first-class object. `Engine.script_stage()` runs one
named SQL script, materialises the result as a real table, records rows
and wall time, and (optionally) writes a parity dump. There is exactly
one place that decides what "materialise" means, so changing the policy
is a one-line change rather than an archaeology exercise.

DuckDB specifics worth knowing
------------------------------
* One process, one connection. No driver/executor split, no serialisation
  boundary, no plan shipping. A "job" costs microseconds, so the ~240
  control-flow actions that dominated the Spark run are simply not a cost
  centre here.
* `CREATE OR REPLACE TABLE ... AS SELECT` is the materialisation
  primitive. It is eager and bounded, which is what SAS gave for free and
  Spark did not.
* Larger-than-memory operation is handled by spilling to `temp_directory`,
  so a table that does not fit is slow rather than fatal.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
import shutil as _shutil
from pathlib import Path

from .sysinfo import suggest_memory_limit as _suggest_memory_limit
from typing import Any, Mapping, Sequence

import duckdb

from .events import (
    Event,
    EventSink,
    Level,
    LogMessage,
    MemoryStatus,
    StageFinished,
    StageProgress,
    StageStarted,
    StatementFinished,
    console_sink,
)
from .sqlsplit import split_statements

SQL_DIR = Path(__file__).parent / "sql"


class CancelledRun(Exception):
    """Raised at a stage boundary when cancellation has been requested.

    Distinct from DuckDB's InterruptException, which is what surfaces
    when a query was actually in flight.
    """


@dataclass
class StageResult:
    name: str
    table: str | None
    rows: int
    seconds: float




def sql_str(value: object) -> str:
    """A path or other value as a SQL single-quoted literal.

    Paths are interpolated into SET, COPY and read_parquet() because
    those take a literal, not a parameter. A perfectly legal path —
    `/data/O'Brien/scdm` — otherwise terminates the string early and
    produces a parser error. Verified: DuckDB rejects it outright, so
    this is a correctness fix rather than an injection concern in a
    controlled environment.

    Doubling the quote is the SQL standard escape and what DuckDB
    expects.
    """
    return "'" + str(value).replace("'", "''") + "'"


@dataclass
class Engine:
    """Owns the DuckDB connection and the stage log."""

    database: str = ":memory:"
    threads: int | None = None
    memory_limit: str | None = None
    temp_directory: str | None = None
    verbose: bool = True
    on_event: EventSink | None = None
    progress_interval: float = 0.25   # seconds between progress polls
    # Per-statement timing and row/column counts. Costs a few percent of
    # wall time; on by default because a log you cannot diagnose from is
    # a false economy. Turn off for benchmarking.
    statement_detail: bool = True
    # DuckDB ships with autoinstall_known_extensions and
    # autoload_known_extensions ON by default, meaning a query that
    # touches an unsupported path type will try to DOWNLOAD a binary
    # extension from extensions.duckdb.org at runtime. At a Data Partner
    # site that is both a security-review red flag (runtime code
    # download) and a confusing failure mode (no egress -> cryptic
    # error). Off by default here; parquet/json/icu are statically
    # linked into the wheel and need no download.
    allow_extension_download: bool = False

    con: duckdb.DuckDBPyConnection = field(init=False)
    log: list[StageResult] = field(default_factory=list, init=False)
    _stage_index: int = field(default=0, init=False)
    _stage_total: int = field(default=0, init=False)
    _cancelled: bool = field(default=False, init=False)
    _monitor: duckdb.DuckDBPyConnection | None = field(default=None, init=False)
    _limit_bytes: int = field(default=0, init=False)
    peak_memory_bytes: int = field(default=0, init=False)
    _stmt_peak: int = field(default=0, init=False)
    peak_spill_bytes: int = field(default=0, init=False)
    _shapes: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.con = duckdb.connect(self.database)
        if self.threads:
            self.con.execute(f"SET threads = {int(self.threads)}")
        # Always set an explicit limit. Leaving it unset means DuckDB's
        # default of 80% of physical RAM, which is unpredictable across
        # machines and antisocial on shared hardware. `memory_limit=None`
        # therefore means "the package default", not "no limit".
        limit = self.memory_limit or _suggest_memory_limit()
        if limit and limit != "auto":
            self.con.execute(f"SET memory_limit = {sql_str(limit)}")
        if self.temp_directory:
            Path(self.temp_directory).mkdir(parents=True, exist_ok=True)
            self.con.execute(f"SET temp_directory = {sql_str(self.temp_directory)}")
        # Preserve insertion order only where we ask for it; letting DuckDB
        # drop the guarantee lets it parallelise scans more aggressively.
        self.con.execute("SET preserve_insertion_order = false")
        if not self.allow_extension_download:
            for stmt in ("SET autoinstall_known_extensions = false",
                         "SET autoload_known_extensions = false"):
                try:
                    self.con.execute(stmt)
                except Exception:
                    pass
        # Required for query_progress() to report anything. Verified live:
        # a polling thread sees 0->100 during parquet scans, joins and
        # aggregations. Operators that cannot estimate cardinality report
        # 0, so progress is advisory, never a completion signal.
        # Silence DuckDB's own stdout progress bar while keeping the
        # query_progress() API alive. Without this, the bar's carriage
        # returns corrupt any TUI or captured log.
        self.con.execute("SET enable_progress_bar_print = false")
        self.con.execute("SET enable_progress_bar = true")
        self.con.execute("SET progress_bar_time = 1")
        if self.on_event is None:
            self.on_event = console_sink(self.verbose)
        # A separate cursor for telemetry. The executing connection is
        # blocked inside execute(), so polling it for memory stats
        # returns one sample per query; a cursor shares the database and
        # answers while the main query runs.
        self._monitor = self.con.cursor()
        # A cursor does NOT inherit connection settings, so it prints its
        # own progress bar to stdout — which corrupts the TUI and leaked
        # into captured output. Silence it explicitly.
        for stmt in ("SET enable_progress_bar_print = false",
                     "SET enable_progress_bar = false"):
            try:
                self._monitor.execute(stmt)
            except Exception:
                pass
        self._limit_bytes = self._parse_limit()
        self.execute_script("00_macros.sql")

    @property
    def effective_memory_limit(self) -> str:
        """What DuckDB is ACTUALLY allowed to use, as a human string.

        `memory_limit=None` does not mean "unlimited" or "modest" — it
        means DuckDB's own default, which is **80% of physical RAM**. On
        the 4 GB benchmark box that is 3.1 GiB; on a 128 GB DP server it
        is ~102 GB. A run log that records the REQUESTED value shows
        "None" in both cases, which tells an operator nothing about what
        the job actually took on a shared machine.
        """
        try:
            row = self.con.execute(
                "SELECT current_setting('memory_limit')").fetchone()
            return str(row[0]).strip() if row else "unknown"
        except Exception:
            return "unknown"

    @property
    def effective_threads(self) -> int:
        """Threads DuckDB will actually use (defaults to core count)."""
        try:
            row = self.con.execute(
                "SELECT current_setting('threads')").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _parse_limit(self) -> int:
        """Resolve the effective memory limit to bytes."""
        try:
            raw = str(self.con.execute(
                "SELECT current_setting('memory_limit')"
            ).fetchone()[0]).strip()
        except Exception:
            return 0
        units = {"KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4,
                 "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}
        for suffix, mult in units.items():
            if raw.upper().endswith(suffix):
                try:
                    return int(float(raw[: -len(suffix)].strip()) * mult)
                except ValueError:
                    return 0
        try:
            return int(float(raw))
        except ValueError:
            return 0

    def _spill_total(self) -> int:
        """Bytes written to the temp directory so far, cumulative.

        Read on the MONITOR cursor: the executing connection is blocked
        while a statement runs, which is the whole reason spilling was
        invisible per statement.
        """
        if self._monitor is None:
            return 0
        try:
            row = self._monitor.execute(
                "SELECT coalesce(sum(temporary_storage_bytes), 0) "
                "FROM duckdb_memory()").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def memory_status(self) -> MemoryStatus:
        """Current RAM use and bytes spilled to the temp directory."""
        used = spilled = 0
        if self._monitor is not None:
            try:
                used, spilled = self._monitor.execute(
                    "SELECT coalesce(sum(memory_usage_bytes), 0), "
                    "       coalesce(sum(temporary_storage_bytes), 0) "
                    "FROM duckdb_memory()"
                ).fetchone()
            except Exception:
                pass
        self.peak_memory_bytes = max(self.peak_memory_bytes, int(used))
        # per-statement high-water mark, reset by the statement loop
        self._stmt_peak = max(getattr(self, "_stmt_peak", 0), int(used))
        self.peak_spill_bytes = max(self.peak_spill_bytes, int(spilled))
        return MemoryStatus(used_bytes=int(used), spilled_bytes=int(spilled),
                            limit_bytes=self._limit_bytes)

    # ---------------- events & cancellation -------------------------

    def emit(self, event: Event) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:
            pass          # a broken sink must never fail a run

    def cancel(self) -> None:
        """Interrupt whatever query is running.

        DuckDB's `interrupt()` raises InterruptException in the thread
        blocked on execute(), so a run stops within roughly one operator
        boundary rather than at the next stage.

        `interrupt()` alone is not enough, though: called while no query
        is running — during engine construction, or between stages — it
        is a silent no-op. So the flag is also checked at every stage
        boundary by `raise_if_cancelled()`. Without that, cancelling
        early set the flag and the run then completed all ten stages.
        """
        self._cancelled = True
        try:
            self.con.interrupt()
        except Exception:
            pass

    def raise_if_cancelled(self) -> None:
        """Stop at a stage boundary if cancellation was requested."""
        if self._cancelled:
            raise CancelledRun("run cancelled")

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def plan(self, stage_names: Sequence[str]) -> None:
        """Declare the stage list so a UI can size a progress bar."""
        self._stage_total = len(stage_names)
        self._stage_index = 0

    def _run_with_progress(self, name: str, fn) -> None:
        """Execute `fn` while polling DuckDB for intra-query progress."""
        stop = threading.Event()
        t0 = time.perf_counter()

        def poll() -> None:
            while not stop.wait(self.progress_interval):
                try:
                    pct = self.con.query_progress()
                except Exception:
                    continue
                if isinstance(pct, (int, float)) and pct >= 0:
                    self.emit(StageProgress(
                        name=name,
                        percent=float(pct),
                        elapsed=time.perf_counter() - t0,
                    ))
                self.emit(self.memory_status())

        watcher = threading.Thread(target=poll, daemon=True)
        watcher.start()
        try:
            fn()
        finally:
            stop.set()
            watcher.join(timeout=1.0)

    # ---------------- low-level -------------------------------------

    def sql(self, query: str, params: Sequence[Any] | None = None):
        return self.con.execute(query, params) if params else self.con.execute(query)

    def read_sql(self, name: str) -> str:
        return (SQL_DIR / name).read_text()

    def execute_script(self, name: str) -> None:
        """Run a multi-statement .sql file."""
        self.con.execute(self.read_sql(name))

    def count(self, table: str) -> int:
        return int(self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    def shape(self, name: str) -> tuple[int, int]:
        """(rows, columns) for a table or view.

        One catalog query rather than three round trips. `estimated_size`
        is exact for a materialised table and needs no scan; views report
        -1 rows because counting one would execute it and double the
        work. Measured 10.2ms -> 0.8ms per call, and this runs once per
        statement.
        """
        try:
            row = self.con.execute(
                "SELECT t.estimated_size, t.column_count, 0 AS is_view "
                "FROM duckdb_tables() t WHERE t.table_name = ? "
                "UNION ALL "
                "SELECT NULL, v.column_count, 1 "
                "FROM duckdb_views() v WHERE v.view_name = ? "
                "LIMIT 1",
                [name, name],
            ).fetchone()
        except Exception:
            return -1, -1
        if row is None:
            return -1, -1
        size, cols, is_view = row
        if is_view or size is None:
            return -1, int(cols or -1)
        return int(size), int(cols or -1)

    def register(self, name: str, rows: Sequence[Mapping[str, Any]],
                 schema: str) -> None:
        """Materialise a small Python-side config list as a DuckDB table.

        This is how resolved config reaches SQL. It replaces macro-variable
        string interpolation: the SQL is static and joins to these tables,
        which is what allows every cohort to be evaluated in one pass.
        """
        self.con.execute(f"CREATE OR REPLACE TABLE {name} ({schema})")
        if not rows:
            return
        cols = [c.split()[0] for c in schema.split(",")]
        placeholders = ", ".join("?" for _ in cols)
        self.con.executemany(
            f"INSERT INTO {name} VALUES ({placeholders})",
            [[r.get(c) for c in cols] for r in rows],
        )

    # ---------------- stage boundary --------------------------------

    def script_stage(self, name: str, script: str, **params: Any) -> None:
        """Run a .sql file, one statement at a time.

        Executing the file as a single call was simpler but opaque: the
        log said "script" with no row counts, and a 30-second stage gave
        no clue which of its statements was slow. Splitting is cheap
        (DuckDB parses either way) and turns the log into something you
        can actually diagnose from.

        Parameters are substituted with `str.format`, but only scalars
        that came from validated config ever reach here — never user
        data. See docs/SECURITY.md.
        """
        self.raise_if_cancelled()
        self._stage_index += 1
        self.emit(StageStarted(name=name, index=self._stage_index,
                               total=self._stage_total))
        t0 = time.perf_counter()
        sql = self.read_sql(script).format(**params)

        # One progress poller for the whole stage, not one per statement.
        # Spawning a thread per statement cost ~7% at 2m patients (47
        # statements vs 11 stages) — the counting itself is negligible,
        # since a count(*) on a materialised table reads catalog stats
        # in ~5ms.
        if not self.statement_detail:
            self._run_with_progress(name, lambda: self.con.execute(sql))
            dt = time.perf_counter() - t0
            self.log.append(StageResult(name, None, -1, dt))
            self.emit(StageFinished(name=name, index=self._stage_index,
                                    total=self._stage_total, rows=-1,
                                    seconds=dt))
            return

        def run_all() -> None:
            for stmt in split_statements(sql):
                self.raise_if_cancelled()
                # Reset the per-statement high-water mark so the peak
                # is attributed to this statement, not the run.
                before = self._spill_total()
                self._stmt_peak = 0
                s0 = time.perf_counter()
                self.con.execute(stmt.sql)
                sdt = time.perf_counter() - s0
                spilled = max(0, self._spill_total() - before)
                rows = cols = -1
                if stmt.kind in ("TABLE", "VIEW") and stmt.target:
                    rows, cols = self.shape(stmt.target)
                self.emit(StatementFinished(
                    stage=name, target=stmt.target or "",
                    kind=stmt.kind or "", rows=rows, columns=cols,
                    seconds=sdt,
                    peak_bytes=self._stmt_peak,
                    spilled_bytes=spilled,
                ))
                if stmt.target:
                    self._shapes[stmt.target] = rows

        self._run_with_progress(name, run_all)

        dt = time.perf_counter() - t0
        self.log.append(StageResult(name, None, -1, dt))
        self.emit(StageFinished(name=name, index=self._stage_index,
                                total=self._stage_total, rows=-1, seconds=dt))

    # ---------------- output ----------------------------------------

    def write_parquet(self, table: str, path: str | Path,
                      partition_by: str | None = None) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        opts = "FORMAT PARQUET, COMPRESSION ZSTD"
        if partition_by:
            # Remove the target tree first. Neither OVERWRITE_OR_IGNORE
            # nor OVERWRITE deletes partition directories that the new
            # data does not produce — verified on DuckDB 1.5.5, where
            # rewriting a single-cohort table with a DIFFERENT cohort
            # left both partitions in place. So rerunning a study with
            # fewer cohorts, or a renamed one, left the old cohort's
            # partition sitting in the output with nothing marking it
            # stale. Reported in review; the reported cause was right
            # and the obvious one-word fix was not sufficient.
            if path.exists():
                _shutil.rmtree(path)
            opts += f", PARTITION_BY ({partition_by}), OVERWRITE_OR_IGNORE"
        self.con.execute(f"COPY {table} TO {sql_str(path)} ({opts})")

    def write_csv(self, table: str, path: str | Path) -> None:
        """Export a table as CSV, for opening in Excel.

        Parquet is the right storage format but the wrong handover
        format for an analyst who wants to look at a number. Large
        tables are still parquet-only; this is aimed at the summary
        outputs people actually read.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con.execute(f"COPY {table} TO {sql_str(path)} (FORMAT CSV, HEADER)")

    def summary(self) -> str:
        total = sum(s.seconds for s in self.log)
        lines = [
            "",
            f"{'stage':<36}{'rows':>14}{'seconds':>10}{'%':>7}",
            "-" * 67,
        ]
        for s in self.log:
            rows = f"{s.rows:,}" if s.rows >= 0 else "-"
            pct = 100 * s.seconds / total if total else 0
            lines.append(f"{s.name:<36}{rows:>14}{s.seconds:>10.3f}{pct:>7.1f}")
        lines += ["-" * 67, f"{'TOTAL':<36}{'':>14}{total:>10.3f}{100.0:>7.1f}"]
        return "\n".join(lines)

    def warn(self, message: str, detail: str = "") -> None:
        self.emit(LogMessage(level=Level.WARNING, message=message,
                             detail=detail))

    def close(self) -> None:
        self._monitor = None
        self.con.close()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
