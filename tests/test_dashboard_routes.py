"""Every path the dashboard calls must exist, with the verb it uses.

This exists because `Save changes` in the Settings tab returned 405 for the whole
life of the feature: the dashboard sent POST /config and only PUT was registered.
Nothing caught it, because the API tests call the API directly and the dashboard is
a static file no test had ever read.

The mismatch is mechanical, so checking it is mechanical.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from asm.services.api.main import app

STATIC = Path(__file__).resolve().parents[1] / \
    "src/asm/services/api/static/index.html"

# api('/x') -> GET, post('/x') -> POST, del_('/x') -> DELETE
HELPERS = {"api": "GET", "post": "POST", "put": "PUT",
           "del_": "DELETE", "delete": "DELETE"}


def dashboard_calls() -> set[tuple[str, str]]:
    html = STATIC.read_text()
    found = set()
    for m in re.finditer(r"""\b(api|post|put|del_|delete)\(\s*['"`]([^'"`?)]*)""", html):
        path = m.group(2)
        if path.startswith("/"):
            found.add((HELPERS[m.group(1)], path))
    return found


def registered() -> list[tuple[str, str]]:
    return [(method.upper(), path)
            for path, ops in app.openapi()["paths"].items() for method in ops]


def as_regex(route_path: str) -> str:
    """A route's path with {params} standing in for one segment."""
    return "^" + re.sub(r"\{[^}]+\}", "[^/]+", route_path).rstrip("/") + "/?$"


def probes(call_path: str) -> list[str]:
    """A call ending in '/' is built as '/base/' + something; try it filled in."""
    base = call_path.rstrip("/") or "/"
    return [base, base + "/x"] if call_path.endswith("/") else [base]


CALLS = sorted(dashboard_calls())


def test_the_dashboard_actually_calls_something():
    """A regex that silently matches nothing would make every case below pass."""
    assert len(CALLS) > 20, CALLS


@pytest.mark.parametrize("verb,path", CALLS, ids=[f"{v} {p}" for v, p in CALLS])
def test_dashboard_call_has_a_route(verb: str, path: str):
    routes = registered()
    if any(re.match(as_regex(rp), pr)
           for meth, rp in routes if meth == verb for pr in probes(path)):
        return

    # Distinguish "wrong verb" from "no such path" - one is a typo, the other a 405
    # that looks like a broken feature.
    others = sorted({meth for meth, rp in routes
                     if any(re.match(as_regex(rp), pr) for pr in probes(path))})
    pytest.fail(
        f"the dashboard calls {verb} {path} but " +
        (f"it is only registered as {', '.join(others)} - a 405"
         if others else "no such route exists - a 404")
    )
