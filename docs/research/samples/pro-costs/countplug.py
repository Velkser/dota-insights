"""Pytest plugin that counts source requests per tool and endpoint (p5-05 measurement).

Wraps ``metrics.record_source_call`` and ``metrics.record_cache``, finds the calling tool on the
stack and writes ``{tool: {test: counts}}`` to ``$COUNT_OUT``. Requests made inside
``asyncio.gather`` tasks have no tool frame and land under ``<no-tool>``. Run:
``PYTHONPATH=docs/research/samples/pro-costs COUNT_OUT=counts.json uv run pytest -q -p countplug``
"""

import collections
import json
import os
import re
import sys

import pytest

from dota_insights import metrics

TOOLS_DIR = os.sep + os.path.join("dota_insights", "mcp", "tools") + os.sep
counts = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
current_test = ["?"]
_orig_src = metrics.record_source_call
_orig_cache = metrics.record_cache


def _tool_and_endpoint():
    f = sys._getframe(2)
    tool, endpoint = None, None
    while f is not None:
        code = f.f_code
        if endpoint is None:
            loc = f.f_locals
            if code.co_name == "_request" and "path" in loc:
                p = re.sub(r"/\d+", "/{id}", str(loc["path"]))
                endpoint = "od " + p
            elif code.co_name == "query" and "document" in loc:
                m = re.search(r"(query|mutation)\s+(\w+)", loc["document"])
                endpoint = "st " + (m.group(2) if m else "anon")
        if (
            TOOLS_DIR in code.co_filename
            and code.co_name not in ("<module>",)
            and not code.co_name.startswith("_")
        ):
            tool = code.co_name
        f = f.f_back
    return tool, endpoint


def rec_src(source):
    tool, ep = _tool_and_endpoint()
    c = counts[tool or "<no-tool>"][current_test[0]]
    c["src:" + source] += 1
    if ep:
        c["ep:" + ep] += 1
    return _orig_src(source)


def rec_cache(outcome):
    tool, _ = _tool_and_endpoint()
    counts[tool or "<no-tool>"][current_test[0]]["cache:" + outcome] += 1
    return _orig_cache(outcome)


metrics.record_source_call = rec_src
metrics.record_cache = rec_cache


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    current_test[0] = item.nodeid
    yield


def pytest_sessionfinish(session):
    out = os.environ.get("COUNT_OUT", "counts.json")
    with open(out, "w") as fh:
        json.dump({t: {k: dict(v) for k, v in d.items()} for t, d in counts.items()}, fh)
