"""A failing source must not take the other branches down with it."""

import sys
import threading
import time
from collections.abc import Callable
from typing import Never

import pytest

from src.pipeline.errors import (
    PipelineIncompleteError,
    SourceUnavailableError,
    unavailable_if_unreachable,
)
from src.pipeline.runner import Pipeline, Step, main, run


def _recorder(ran: list[str]) -> Callable[[str], Step]:
    """A step that records its name when it runs."""

    def step(name: str) -> Step:
        def run_it() -> None:
            ran.append(name)

        return run_it

    return step


def _pipeline(ran: list[str], osm_download: Step) -> Pipeline:
    step = _recorder(ran)
    return {
        "start": (None, ["osm-download", "atp-download"]),
        "osm-download": (osm_download, ["osm-import"]),
        "osm-import": (step("osm-import"), ["mv-brand"]),
        "atp-download": (step("atp-download"), ["atp-import"]),
        "atp-import": (step("atp-import"), ["mv-brand"]),
        "mv-brand": (step("mv-brand"), []),
    }


def _run(pipeline: Pipeline) -> None:
    run(pipeline, set(pipeline))


def test_unavailable_source_lets_its_own_branch_finish() -> None:
    ran: list[str] = []

    def down() -> Never:
        raise SourceUnavailableError("Geofabrik")

    with pytest.raises(PipelineIncompleteError):
        _run(_pipeline(ran, down))
    # osm-import still runs: it finds no new PBF and leaves the tables alone.
    assert set(ran) == {"osm-import", "atp-download", "atp-import", "mv-brand"}


def test_real_failure_kills_only_its_descendants() -> None:
    ran: list[str] = []

    def boom() -> Never:
        raise RuntimeError("half-written tables")

    with pytest.raises(RuntimeError):
        _run(_pipeline(ran, boom))
    assert "osm-import" not in ran  # descendant, never run
    assert "mv-brand" not in ran  # joins the dead branch
    assert "atp-import" in ran  # unrelated branch completed


def test_clean_run_raises_nothing() -> None:
    ran: list[str] = []
    _run(_pipeline(ran, _recorder(ran)("osm-download")))
    assert len(ran) == 5


def test_disk_errors_are_not_mistaken_for_an_outage() -> None:
    """A full disk must fail loudly, not be recorded as a skipped source."""
    with pytest.raises(OSError, match="No space left"), unavailable_if_unreachable("ATP"):
        raise OSError(28, "No space left on device")


# --- Locks and the command line ------------------------------------------------------


def test_steps_sharing_a_lock_never_overlap() -> None:
    """Two downloads on the network lock: the second starts when the first
    is done. Two on different locks run together.
    """
    running = {"network": 0, "cpu": 0}
    overlap = {"network": 0, "cpu": 0}
    guard = threading.Lock()

    def step(lock: str) -> Step:
        def run_it() -> None:
            with guard:
                running[lock] += 1
                overlap[lock] = max(overlap[lock], running[lock])
            time.sleep(0.05)
            with guard:
                running[lock] -= 1

        return run_it

    pipeline: Pipeline = {
        "start": (None, ["a", "b", "c", "d"]),
        "a": (step("network"), [], {"lock": "network"}),
        "b": (step("network"), [], {"lock": "network"}),
        "c": (step("cpu"), [], {"lock": "cpu"}),
        "d": (step("cpu"), [], {"lock": "cpu"}),
    }
    run(pipeline, set(pipeline))

    assert overlap["network"] == 1
    assert overlap["cpu"] == 1


def test_a_lock_is_released_by_a_failing_step() -> None:
    ran: list[str] = []

    def boom() -> Never:
        raise RuntimeError("half-written")

    pipeline: Pipeline = {
        "start": (None, ["a", "b"]),
        "a": (boom, [], {"lock": "network"}),
        "b": (_recorder(ran)("b"), [], {"lock": "network"}),
    }
    with pytest.raises(RuntimeError, match="half-written"):
        run(pipeline, set(pipeline))
    assert ran == ["b"]


def test_a_pipeline_without_a_root_is_refused() -> None:
    pipeline: Pipeline = {"a": (lambda: None, ["b"]), "b": (lambda: None, ["a"])}
    with pytest.raises(RuntimeError, match="cycle"):
        run(pipeline, set(pipeline))


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[str]]:
    """`python -m src.pipeline <args>` on a small pipeline; what ran."""
    ran: list[str] = []
    step = _recorder(ran)
    pipeline: Pipeline = {
        "start": (None, ["osm-download"]),
        "osm-download": (step("osm-download"), ["osm-import"], {"lock": "network"}),
        "osm-import": (step("osm-import"), ["mv-brand"]),
        "mv-brand": (step("mv-brand"), []),
    }

    def invoke(*args: str) -> list[str]:
        monkeypatch.setattr(sys, "argv", ["pipeline", *args])
        main(pipeline)
        return ran

    return invoke


def test_the_default_command_runs_everything_from_start(cli: Callable[..., list[str]]) -> None:
    assert cli() == ["osm-download", "osm-import", "mv-brand"]


def test_from_runs_a_step_and_what_follows(cli: Callable[..., list[str]]) -> None:
    assert cli("from", "osm-import") == ["osm-import", "mv-brand"]


def test_step_runs_one_step_alone(cli: Callable[..., list[str]]) -> None:
    assert cli("step", "osm-import") == ["osm-import"]


@pytest.mark.parametrize(
    "args", [("step", "no-such-step"), ("from", "no-such-step"), ("frobnicate",), ("step",)]
)
def test_a_wrong_command_exits_non_zero_without_running_anything(
    cli: Callable[..., list[str]], args: tuple[str, ...]
) -> None:
    with pytest.raises(SystemExit) as exited:
        cli(*args)
    assert exited.value.code == 1


def test_list_prints_the_steps_in_order(
    cli: Callable[..., list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    cli("list")
    out = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in out] == ["start", "osm-download", "osm-import", "mv-brand"]
    assert "[lock=network]" in out[1]
