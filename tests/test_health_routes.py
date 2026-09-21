"""The two probe endpoints: health reaches the database, version names the build."""

from flask import Flask

from tests.conftest import Connection


def test_health_and_version(web_app: Flask, migrated_conn: Connection) -> None:
    client = web_app.test_client()
    assert client.get("/health").get_json() == {"status": "ok"}
    assert client.get("/version").get_json()["version"].startswith("Gamma")
