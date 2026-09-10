"""Failure kinds the runner tells apart.

A remote datasource being down is not a pipeline failure: the SQL tables we
already hold stay valid and stay in place. Only a step that fails *while
rebuilding* leaves the database half-done and must stop its branch.
"""
from contextlib import contextmanager

import requests


class SourceUnavailable(Exception):
    """A remote datasource could not be reached.

    The branch keeps running — its downstream steps no-op on unchanged inputs
    — and the datasource is recorded 'skipped' in data_imports rather than
    left 'pending', so the home page shows it checked, not syncing. The run
    still exits non-zero so the systemd timer retries it 4 hours later (3
    times: 12h after the first run at the latest).
    """


class PipelineIncomplete(Exception):
    """At least one datasource was skipped; everything else ran."""


@contextmanager
def unavailable_if_unreachable(source: str):
    """Turn *network* failures inside the block into SourceUnavailable.

    Only requests exceptions: it already covers DNS, connect, read timeout and
    HTTP status. Deliberately not OSError — the downloads write to disk inside
    these blocks, and a full disk relabelled "source unreachable" would be
    recorded 'skipped' and pass in silence, which is the one failure this
    codebase least wants to hide (see osm._require_free_space).
    """
    try:
        yield
    except SourceUnavailable:
        raise
    except requests.RequestException as exc:
        raise SourceUnavailable(f"{source} unreachable: {exc}") from exc
