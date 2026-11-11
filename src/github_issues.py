"""The GitHub issue or pull request a missing brand is tracked by.

A contributor links a todo entry to the issue asking for its spider, or to the
pull request writing it — usually on alltheplaces/alltheplaces. Its state tells
the others how far the work got, without opening it. Only the All The Places
repository is accepted: that is where a missing brand gets its spider.
"""

import logging
import os
import re
from typing import Final, Literal

import requests

logger = logging.getLogger(__name__)

IssueState = Literal["open", "closed", "merged"]

_URL: Final = re.compile(
    r"^https://github\.com/(?P<owner>alltheplaces)/(?P<repo>alltheplaces)/(?:issues|pull)/(?P<number>\d+)/?$",
    re.IGNORECASE,
)
_API: Final = "https://api.github.com/repos/{owner}/{repo}/issues/{number}"


def parse_issue_url(url: str) -> tuple[str, str, int] | None:
    """Owner, repository and number of an All The Places issue or pull request URL."""
    match = _URL.match(url.split("#", 1)[0].split("?", 1)[0])
    if match is None:
        return None
    return match["owner"], match["repo"], int(match["number"])


def issue_number(url: str) -> int | None:
    """The number shown for a link, None when it is not an All The Places one."""
    parsed = parse_issue_url(url)
    return parsed[2] if parsed else None


def fetch_issue_state(url: str) -> IssueState | None:
    """Where the issue stands, or None when GitHub did not say.

    The issues endpoint answers for pull requests too, with a `pull_request`
    member whose `merged_at` tells a merged one from a closed one.
    `GITHUB_TOKEN`, when set, lifts the anonymous limit of 60 requests an hour.
    """
    parsed = parse_issue_url(url)
    if parsed is None:
        return None
    owner, repo, number = parsed
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(
            _API.format(owner=owner, repo=repo, number=number), headers=headers, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        logger.warning("Could not read the state of %s", url, exc_info=True)
        return None
    pull = data.get("pull_request")
    if isinstance(pull, dict) and pull.get("merged_at"):  # pyright: ignore[reportUnknownMemberType] — JSON
        return "merged"
    return "closed" if data.get("state") == "closed" else "open"
