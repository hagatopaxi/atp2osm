import psycopg
import pytest
from flask.testing import FlaskClient

from src import github_issues
from src.config import Database
from src.pipeline import github as pipeline_github
from src.routes import todo
from tests.conftest import Connection, one


def _github_says(state: github_issues.IssueState | None) -> object:
    def fetch(_url: str) -> github_issues.IssueState | None:
        return state

    return fetch


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/alltheplaces/alltheplaces/issues/1234",
            ("alltheplaces", "alltheplaces", 1234),
        ),
        (
            "https://github.com/alltheplaces/alltheplaces/pull/99#issuecomment-1",
            ("alltheplaces", "alltheplaces", 99),
        ),
        ("https://github.com/alltheplaces/alltheplaces", None),
        ("https://gitlab.com/a/b/issues/1", None),
        ("https://github.com/someone/alltheplaces/issues/1", None),
        ("https://github.com/alltheplaces/other/pull/1", None),
        ("http://github.com/a/b/issues/1", None),
    ],
)
def test_only_an_issue_or_pull_request_url_is_one(
    url: str, expected: tuple[str, str, int] | None
) -> None:
    assert github_issues.parse_issue_url(url) == expected


def test_the_issue_is_stored_with_the_state_github_gives(
    contributor: FlaskClient, migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(todo, "fetch_issue_state", _github_says("open"))
    url = "https://github.com/alltheplaces/alltheplaces/issues/1234"
    res = contributor.post("/todo", json={"brand_name": "Acme", "github_issue": url})
    assert res.status_code == 201
    row = one(
        migrated_conn.execute(
            "SELECT github_issue, issue_state, issue_checked_at IS NOT NULL FROM todo_brands"
        ).fetchone()
    )
    assert row == (url, "open", True)


def test_github_silent_leaves_the_state_unknown(
    contributor: FlaskClient, migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(todo, "fetch_issue_state", _github_says(None))
    url = "https://github.com/alltheplaces/alltheplaces/pull/7"
    assert (
        contributor.post("/todo", json={"brand_name": "Acme", "github_issue": url}).status_code
        == 201
    )
    row = one(
        migrated_conn.execute("SELECT issue_state, issue_checked_at FROM todo_brands").fetchone()
    )
    assert row == (None, None)


@pytest.mark.parametrize(
    ("body", "cause"),
    [
        ({"brand_name": "Acme", "brand_wikidata": "Carrefour"}, "Carrefour"),
        ({"brand_name": "Acme", "estimation": -3}, "negative"),
        ({"brand_name": "Acme", "github_issue": "https://example.org/1"}, "https://example.org/1"),
        (
            {"brand_name": "Acme", "github_issue": "https://github.com/osm/osm/issues/1"},
            "All The Places",
        ),
        ({"brand_name": "  "}, "name"),
    ],
)
def test_a_refused_entry_says_why(
    contributor: FlaskClient, migrated_conn: Connection, body: dict[str, object], cause: str
) -> None:
    res = contributor.post("/todo", json=body)
    assert res.status_code == 400
    assert cause in one(res.json)["error"]
    assert one(migrated_conn.execute("SELECT COUNT(*) FROM todo_brands").fetchone())[0] == 0


def test_a_duplicate_names_the_entry_already_there(
    contributor: FlaskClient, migrated_conn: Connection
) -> None:
    migrated_conn.execute(
        "INSERT INTO todo_brands (brand_wikidata, brand_name, osm_user_id) VALUES ('Q42', 'Original', 7)"
    )
    migrated_conn.commit()
    res = contributor.post("/todo", json={"brand_name": "Copy", "brand_wikidata": "q42"})
    assert res.status_code == 409
    assert "Original" in one(res.json)["error"]


def test_the_refresh_keeps_a_merged_issue_and_a_state_github_withholds(
    migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch, test_db: Database
) -> None:
    migrated_conn.execute("""
        INSERT INTO todo_brands (brand_name, osm_user_id, github_issue, issue_state) VALUES
            ('Moving', 7, 'https://github.com/alltheplaces/alltheplaces/issues/1', 'open'),
            ('Silent', 7, 'https://github.com/alltheplaces/alltheplaces/issues/2', 'open'),
            ('Done', 7, 'https://github.com/alltheplaces/alltheplaces/pull/3', 'merged')
    """)
    migrated_conn.commit()
    asked: list[str] = []

    def state(url: str) -> github_issues.IssueState | None:
        asked.append(url)
        return "closed" if url.endswith("/1") else None

    monkeypatch.setattr(pipeline_github, "fetch_issue_state", state)
    monkeypatch.setattr(pipeline_github, "connect", lambda: psycopg.connect(test_db.conninfo))
    pipeline_github.refresh_github_issues()
    rows = migrated_conn.execute(
        "SELECT brand_name, issue_state FROM todo_brands ORDER BY brand_name"
    ).fetchall()
    assert rows == [("Done", "merged"), ("Moving", "closed"), ("Silent", "open")]
    assert not any(url.endswith("/3") for url in asked)
