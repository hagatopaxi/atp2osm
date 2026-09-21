"""The two probe endpoints: health reaches the database, version names the build."""

from flask import Flask

from tests.conftest import Connection


def test_health_and_version(web_app: Flask, migrated_conn: Connection) -> None:
    client = web_app.test_client()
    assert client.get("/health").get_json() == {"status": "ok"}
    assert client.get("/version").get_json()["version"].startswith("Gamma")


def test_every_route_takes_the_parts_of_its_url_by_name(web_app: Flask) -> None:
    """Flask passes `<brand_wikidata>` as a keyword: a view renaming the
    parameter, to mark it unused say, answers 500 on every call.
    """
    import inspect
    from typing import cast

    for rule in web_app.url_map.iter_rules():
        if rule.endpoint == "static":  # Flask's own, wired with **kwargs
            continue
        view = web_app.view_functions[rule.endpoint]
        parameters = inspect.signature(inspect.unwrap(view)).parameters
        # werkzeug types `arguments` loosely: it is the set of the rule's names.
        arguments = cast("set[str]", rule.arguments)  # pyright: ignore[reportUnknownMemberType]
        missing = arguments - set(parameters)
        assert not missing, f"{rule.rule}: {view.__name__} does not take {missing}"
