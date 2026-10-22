"""Extra pages on the Locust web UI: LoadGenerator.addWebPage() pages, and handlers via route().

The entrypoint loads this file next to the locustfile (``-f locustfile.py,cre_pages.py``). On
``events.init`` it registers, on the controller's (or standalone task's) web UI:

  - every page ``LoadGenerator.addWebPage(path, html)`` baked into ``cre_pages/`` (GET, HTML, with
    ``{{BASE_PATH}}`` replaced by the UI's base path, e.g. ``/locust``, so links and ``fetch()``
    calls work behind the CloudFront Signer edge);
  - every handler a locustfile declares with ``@route``::

        from cre_pages import route

        @route("/api/flush", methods=["POST"])
        def flush():
            ...
            return {"ok": True}

Paths are relative to the base path: Locust's ``--web-base-path`` does NOT prefix routes added to
``web_ui.app``, so this module adds it. Workers have no web UI and register nothing.

Locust imports each ``-f`` file as its own module object, so a locustfile that also does
``import cre_pages`` gets a second copy of this module. The state lives in one ``sys.modules``
entry shared by both copies, and only the first copy listens, so nothing registers twice (Flask
rejects a duplicate endpoint).
"""

import json
import os
import sys
import types

from locust import events

_STATE = sys.modules.setdefault("_cre_pages_state", types.ModuleType("_cre_pages_state"))
if not hasattr(_STATE, "routes"):
    _STATE.routes = []
    _STATE.listening = False

PAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cre_pages")
HTML = {"Content-Type": "text/html; charset=utf-8"}


def route(path, methods=("GET",)):
    """Register ``fn`` on the web UI at ``<base path><path>`` (Flask view: return str/dict/tuple)."""
    if not path.startswith("/"):
        raise ValueError(f"cre_pages.route: path must start with '/': {path!r}")

    def deco(fn):
        _STATE.routes.append((path, fn, list(methods)))
        return fn

    return deco


def _page_view(html):
    return lambda: (html, 200, HTML)


def _register(environment, **_kwargs):
    web_ui = environment.web_ui
    if web_ui is None:
        return
    base = (getattr(environment.parsed_options, "web_base_path", None) or "").rstrip("/")
    manifest = os.path.join(PAGES_DIR, "pages.json")
    if os.path.exists(manifest):
        with open(manifest, encoding="utf-8") as f:
            pages = json.load(f)
        for i, (path, filename) in enumerate(sorted(pages.items())):
            with open(os.path.join(PAGES_DIR, filename), encoding="utf-8") as f:
                html = f.read().replace("{{BASE_PATH}}", base)
            web_ui.app.add_url_rule(base + path, f"cre_page_{i}", _page_view(html))
    for i, (path, fn, methods) in enumerate(_STATE.routes):
        web_ui.app.add_url_rule(base + path, f"cre_route_{i}_{fn.__name__}", fn, methods=methods)


if not _STATE.listening:
    _STATE.listening = True
    events.init.add_listener(_register)
