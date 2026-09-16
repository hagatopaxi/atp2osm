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
from concurrent.futures import ThreadPoolExecutor

from src.pipeline.errors import PipelineIncomplete, SourceUnavailable

logger = logging.getLogger(__name__)


def _noop_failure(step_name, exc):
    """Default failure hook, does nothing."""

_step_ctx = threading.local()


class StepFormatter(logging.Formatter):
    def format(self, record):
        base = super().format(record)
        step = getattr(_step_ctx, "name", None)
        return f"[{step}] {base}" if step else base


def _fn(entry):
    return entry[0]


def _succs(entry):
    return entry[1]


def _opts(entry):
    return entry[2] if len(entry) > 2 else {}


def _get_lock_name(entry):
    """Return the lock name for this step, or None."""
    return _opts(entry).get("lock")


def _reachable(pipeline, start):
    visited, queue = set(), [start]
    while queue:
        node = queue.pop(0)
        if node in visited:
            continue
        visited.add(node)
        queue.extend(_succs(pipeline[node]))
    return visited


def _topo_levels(pipeline, nodes):
    """Group nodes by topological level (used for display only)."""
    subset = set(nodes)
    nexts = {n: [s for s in _succs(pipeline[n]) if s in subset] for n in subset}
    in_degree = {n: 0 for n in subset}
    for succs in nexts.values():
        for s in succs:
            in_degree[s] += 1

    levels = []
    while in_degree:
        ready = sorted(n for n, d in in_degree.items() if d == 0)
        levels.append(ready)
        for node in ready:
            del in_degree[node]
            for s in nexts[node]:
                in_degree[s] -= 1
    return levels


def _run_step(pipeline, name, on_failure=_noop_failure):
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


def run(pipeline, nodes, on_failure=_noop_failure):
    """Run pipeline steps as soon as their predecessors complete.

    Each branch is fully independent: a step starts the moment all its
    direct predecessors are done, regardless of other in-flight branches.

    Steps that share the same ``lock`` name are serialized via a per-name
    mutex — only one such step runs at a time; others queue behind it.

    Failures are contained to their own branch: a step that raises marks its
    descendants dead (they are never executed) while unrelated branches run to
    completion. A step raising SourceUnavailable does not even do that — its
    branch continues, since the downstream steps no-op when their input did
    not change, which is what leaves the existing tables in place.
    """
    subset = set(nodes)
    if not subset:
        return

    # Build predecessors map from the subset
    predecessors: dict[str, set[str]] = {n: set() for n in subset}
    for n in subset:
        for s in _succs(pipeline[n]):
            if s in subset:
                predecessors[s].add(n)

    initial = [n for n in subset if not predecessors[n]]
    if not initial:
        raise RuntimeError("Pipeline has no root nodes (cycle?)")

    # Per-name mutex registry (for lock= option)
    _mutexes: dict[str, threading.Lock] = {}
    _mutexes_guard = threading.Lock()

    def _get_mutex(lock_name: str) -> threading.Lock:
        with _mutexes_guard:
            if lock_name not in _mutexes:
                _mutexes[lock_name] = threading.Lock()
            return _mutexes[lock_name]

    # Shared state
    state_lock = threading.Lock()
    remaining = {n: len(predecessors[n]) for n in subset}
    # active_count tracks nodes that are either running or scheduled-but-not-started.
    # It is incremented for all initial nodes up front, then atomically
    # decremented (self) / incremented (successors) inside state_lock so it
    # never hits 0 prematurely.
    active_count = [len(initial)]
    done_event = threading.Event()
    errors: list[BaseException] = []
    unavailable: list[SourceUnavailable] = []
    dead: set[str] = set()

    executor = ThreadPoolExecutor(max_workers=len(subset))

    def _run_node(name: str) -> None:
        with state_lock:
            is_dead = name in dead
        if is_dead:
            logger.warning("[%s] not run: a step it depends on failed", name)
        else:
            lock_name = _get_lock_name(pipeline[name])
            mutex = _get_mutex(lock_name) if lock_name else None
            try:
                if mutex:
                    mutex.acquire()
                try:
                    _run_step(pipeline, name, on_failure)
                finally:
                    if mutex:
                        mutex.release()
            except SourceUnavailable as exc:
                # The source is down, not the branch: downstream steps find no
                # new input and no-op, leaving the existing tables alone.
                logger.warning("[%s] %s — branch continues on existing data", name, exc)
                with state_lock:
                    unavailable.append(exc)
            except Exception as exc:
                with state_lock:
                    errors.append(exc)
                    dead.add(name)

        # A dead node still walks the graph, marking its descendants dead, so
        # the completion counting stays exact without a second traversal.
        with state_lock:
            active_count[0] -= 1
            newly_ready: list[str] = []
            for s in _succs(pipeline[name]):
                if s in subset:
                    if name in dead:
                        dead.add(s)
                    remaining[s] -= 1
                    if remaining[s] == 0:
                        newly_ready.append(s)
                        active_count[0] += 1  # count before submit
            if active_count[0] == 0:
                done_event.set()

        for s in newly_ready:
            executor.submit(_run_node, s)

    for name in initial:
        executor.submit(_run_node, name)

    done_event.wait()
    executor.shutdown(wait=True)

    if errors:
        raise errors[0]
    if unavailable:
        raise PipelineIncomplete("; ".join(str(e) for e in unavailable))


def main(pipeline, on_failure=_noop_failure):
    args = sys.argv[1:]
    cmd = args[0] if args else "start"

    if cmd == "start":
        run(pipeline, _reachable(pipeline, "start"), on_failure)

    elif cmd == "from":
        if len(args) < 2:
            _usage()
        start = args[1]
        _check(pipeline, start)
        run(pipeline, _reachable(pipeline, start), on_failure)

    elif cmd == "step":
        if len(args) < 2:
            _usage()
        name = args[1]
        _check(pipeline, name)
        _run_step(pipeline, name, on_failure)

    elif cmd == "list":
        for level in _topo_levels(pipeline, set(pipeline)):
            for name in level:
                succs = _succs(pipeline[name])
                lock = _get_lock_name(pipeline[name])
                arrow = f"  →  {', '.join(succs)}" if succs else ""
                lock_tag = f" [lock={lock}]" if lock else ""
                print(f"  {name}{arrow}{lock_tag}")
        return

    else:
        _usage()


def _check(pipeline, name):
    if name not in pipeline:
        print(f"Unknown step '{name}'. Available: {', '.join(pipeline)}", file=sys.stderr)
        sys.exit(1)


def _usage():
    print(
        "Usage:\n"
        "  python -m src.pipeline                 — full pipeline (from start)\n"
        "  python -m src.pipeline start           — same\n"
        "  python -m src.pipeline from <step>     — step + all downstream\n"
        "  python -m src.pipeline step <step>     — single step only\n"
        "  python -m src.pipeline list            — print pipeline order",
        file=sys.stderr,
    )
    sys.exit(1)
