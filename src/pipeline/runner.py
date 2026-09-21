"""
Pipeline runner — event-driven DAG executor.

Pipeline format
---------------
A pipeline is a plain ``dict`` mapping step names to entries::

    PIPELINE = {
        "step-name": (function, ["successor-a", "successor-b"]),
        "step-name": (function, ["successor-a", "successor-b"], {options}),
        ...
    }

Each entry is a tuple of:

* **function** — callable with no arguments, or ``None`` for a virtual/anchor
  node (useful as a named starting point with no work of its own).
* **successors** — list of step names that become eligible to run once this
  step completes.
* **options** *(optional)* — a ``dict`` of execution hints (see below).

Execution model
---------------
Steps start as soon as **all their direct predecessors** have completed.
Unrelated branches run fully in parallel and never wait for each other — there
is no synchronisation barrier between branches.

Step options
------------
Options are passed as the third element of a step's entry tuple.

``lock`` : str
    Serialise steps that share the same lock name. Only one step holding a
    given lock name runs at a time; any other step that reaches that lock will
    queue and wait for it to be released before starting.

    Typical use: bandwidth-heavy operations where true concurrency would
    saturate the network or a shared resource::

        "osm-download": (download_pbf, ["osm-import"], {"lock": "network"}),
        "atp-download": (download_atp, ["atp-extract"], {"lock": "network"}),

    Both downloads become eligible at the same time, but only one executes;
    the other starts the moment the first finishes. Their downstream steps
    (``osm-import``, ``atp-extract``) then proceed independently.

CLI commands
------------
Run from the project root with ``python -m src.pipeline [command]``:

``start`` (default)
    Run the full pipeline starting from the ``"start"`` node.

``from <step>``
    Run ``<step>`` and all steps reachable from it.

``step <step>``
    Run a single step in isolation (no predecessors, no successors).

``list``
    Print all steps in topological order, showing successors and any
    ``lock`` annotation.

``setup``
    Schedule ``start`` at ``app.refresh_schedule`` in the country's timezone
    and never return: the entry point of the ``refresh`` container.
"""

import logging
import sys
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

from src.pipeline.errors import PipelineIncompleteError, SourceUnavailableError

logger = logging.getLogger(__name__)

Step = Callable[[], None]
Options = dict[str, str]
Entry = tuple[Step | None, list[str]] | tuple[Step | None, list[str], Options]
Pipeline = dict[str, Entry]
FailureHook = Callable[[str, BaseException], None]


def _noop_failure(step_name: str, exc: BaseException) -> None:
    """Default failure hook, does nothing."""


_step_ctx = threading.local()


class StepFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        step = getattr(_step_ctx, "name", None)
        return f"[{step}] {base}" if step else base


def _fn(entry: Entry) -> Step | None:
    return entry[0]


def _succs(entry: Entry) -> list[str]:
    return entry[1]


def _opts(entry: Entry) -> Options:
    return entry[2] if len(entry) == 3 else {}  # noqa: PLR2004 — the optional third element


def _get_lock_name(entry: Entry) -> str | None:
    """Return the lock name for this step, or None."""
    return _opts(entry).get("lock")


def _reachable(pipeline: Pipeline, start: str) -> set[str]:
    visited: set[str] = set()
    queue = [start]
    while queue:
        node = queue.pop(0)
        if node in visited:
            continue
        visited.add(node)
        queue.extend(_succs(pipeline[node]))
    return visited


def _topo_levels(pipeline: Pipeline, nodes: Iterable[str]) -> list[list[str]]:
    """Group nodes by topological level (used for display only)."""
    subset = set(nodes)
    nexts = {n: [s for s in _succs(pipeline[n]) if s in subset] for n in subset}
    in_degree = dict.fromkeys(subset, 0)
    for succs in nexts.values():
        for s in succs:
            in_degree[s] += 1

    levels: list[list[str]] = []
    while in_degree:
        ready = sorted(n for n, d in in_degree.items() if d == 0)
        levels.append(ready)
        for node in ready:
            del in_degree[node]
            for s in nexts[node]:
                in_degree[s] -= 1
    return levels


def _run_step(pipeline: Pipeline, name: str, on_failure: FailureHook = _noop_failure) -> None:
    fn = _fn(pipeline[name])
    if fn is None:
        return
    _step_ctx.name = name
    try:
        logger.info("▶  start")
        fn()
        logger.info("✓  done")
    except Exception as exc:
        on_failure(name, exc)
        raise
    finally:
        _step_ctx.name = None


class _Run:
    """One execution of a subset of the pipeline — see run()."""

    def __init__(self, pipeline: Pipeline, nodes: Iterable[str], on_failure: FailureHook) -> None:
        self.pipeline = pipeline
        self.on_failure = on_failure
        self.subset = set(nodes)

        predecessors: dict[str, set[str]] = {n: set() for n in self.subset}
        for n in self.subset:
            for s in _succs(pipeline[n]):
                if s in self.subset:
                    predecessors[s].add(n)
        self.initial = [n for n in self.subset if not predecessors[n]]

        # Per-name mutex registry (for lock= option)
        self._mutexes: dict[str, threading.Lock] = {}
        self._mutexes_guard = threading.Lock()

        # Shared state, under state_lock. active_count tracks nodes that are
        # either running or scheduled-but-not-started: incremented for all
        # initial nodes up front, then atomically decremented (self) /
        # incremented (successors) so it never hits 0 prematurely.
        self.state_lock = threading.Lock()
        self.remaining = {n: len(predecessors[n]) for n in self.subset}
        self.active_count = len(self.initial)
        self.done_event = threading.Event()
        self.errors: list[BaseException] = []
        self.unavailable: list[SourceUnavailableError] = []
        self.dead: set[str] = set()
        self.executor = ThreadPoolExecutor(max_workers=max(1, len(self.subset)))

    def _get_mutex(self, lock_name: str) -> threading.Lock:
        with self._mutexes_guard:
            if lock_name not in self._mutexes:
                self._mutexes[lock_name] = threading.Lock()
            return self._mutexes[lock_name]

    def _execute(self, name: str) -> None:
        lock_name = _get_lock_name(self.pipeline[name])
        mutex = self._get_mutex(lock_name) if lock_name else None
        try:
            if mutex:
                mutex.acquire()
            try:
                _run_step(self.pipeline, name, self.on_failure)
            finally:
                if mutex:
                    mutex.release()
        except SourceUnavailableError as exc:
            # The source is down, not the branch: downstream steps find no
            # new input and no-op, leaving the existing tables alone.
            logger.warning("[%s] %s — branch continues on existing data", name, exc)
            with self.state_lock:
                self.unavailable.append(exc)
        except Exception as exc:  # noqa: BLE001 — recorded, and raised again by run()
            with self.state_lock:
                self.errors.append(exc)
                self.dead.add(name)

    def _run_node(self, name: str) -> None:
        with self.state_lock:
            is_dead = name in self.dead
        if is_dead:
            logger.warning("[%s] not run: a step it depends on failed", name)
        else:
            self._execute(name)

        # A dead node still walks the graph, marking its descendants dead, so
        # the completion counting stays exact without a second traversal.
        with self.state_lock:
            self.active_count -= 1
            newly_ready: list[str] = []
            for s in _succs(self.pipeline[name]):
                if s in self.subset:
                    if name in self.dead:
                        self.dead.add(s)
                    self.remaining[s] -= 1
                    if self.remaining[s] == 0:
                        newly_ready.append(s)
                        self.active_count += 1  # count before submit
            if self.active_count == 0:
                self.done_event.set()

        for s in newly_ready:
            self.executor.submit(self._run_node, s)

    def wait(self) -> None:
        for name in self.initial:
            self.executor.submit(self._run_node, name)
        self.done_event.wait()
        self.executor.shutdown(wait=True)


def run(pipeline: Pipeline, nodes: Iterable[str], on_failure: FailureHook = _noop_failure) -> None:
    """Run pipeline steps as soon as their predecessors complete.

    Each branch is fully independent: a step starts the moment all its
    direct predecessors are done, regardless of other in-flight branches.

    Steps that share the same ``lock`` name are serialized via a per-name
    mutex — only one such step runs at a time; others queue behind it.

    Failures are contained to their own branch: a step that raises marks its
    descendants dead (they are never executed) while unrelated branches run to
    completion. A step raising SourceUnavailableError does not even do that — its
    branch continues, since the downstream steps no-op when their input did
    not change, which is what leaves the existing tables in place.
    """
    execution = _Run(pipeline, nodes, on_failure)
    if not execution.subset:
        return
    if not execution.initial:
        raise RuntimeError("Pipeline has no root nodes (cycle?)")

    execution.wait()

    if execution.errors:
        raise execution.errors[0]
    if execution.unavailable:
        raise PipelineIncompleteError("; ".join(str(e) for e in execution.unavailable))


def main(pipeline: Pipeline, on_failure: FailureHook = _noop_failure) -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "start"

    if cmd == "start":
        run(pipeline, _reachable(pipeline, "start"), on_failure)

    elif cmd == "from":
        start = _step_argument(pipeline, args)
        run(pipeline, _reachable(pipeline, start), on_failure)

    elif cmd == "step":
        name = _step_argument(pipeline, args)
        _run_step(pipeline, name, on_failure)

    elif cmd == "list":
        for level in _topo_levels(pipeline, set(pipeline)):
            for name in level:
                succs = _succs(pipeline[name])
                lock = _get_lock_name(pipeline[name])
                arrow = f"  →  {', '.join(succs)}" if succs else ""
                lock_tag = f" [lock={lock}]" if lock else ""
                print(f"  {name}{arrow}{lock_tag}")  # noqa: T201 — the command's output
        return

    else:
        _usage()


def _step_argument(pipeline: Pipeline, args: list[str]) -> str:
    """The step named after the command, checked against the pipeline."""
    if len(args) != 2:  # noqa: PLR2004 — the command and its one argument
        _usage()
    name = args[1]
    if name not in pipeline:
        print(f"Unknown step '{name}'. Available: {', '.join(pipeline)}", file=sys.stderr)  # noqa: T201
        sys.exit(1)
    return name


def _usage() -> None:
    print(  # noqa: T201 — the command's output
        "Usage:\n"
        "  python -m src.pipeline                 — full pipeline (from start)\n"
        "  python -m src.pipeline start           — same\n"
        "  python -m src.pipeline from <step>     — step + all downstream\n"
        "  python -m src.pipeline step <step>     — single step only\n"
        "  python -m src.pipeline list            — print pipeline order",
        file=sys.stderr,
    )
    sys.exit(1)
