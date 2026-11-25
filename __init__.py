"""Offline docsets for Hermes: list, search and read Dash-format documentation sets on this machine.

Configure ``plugins.entries.offline-docsets.settings.paths`` with ``.docset`` folders or folders
holding them (see README.md). The plugin registers three fixed tools, ``docset_list``,
``docset_search`` and ``docset_read``. They are offered to the model only while at least one path
is configured. Everything is read locally with the standard library; nothing goes over the network.
"""
from __future__ import annotations

import json
import logging

from . import library, schemas

logger = logging.getLogger(__name__)

TOOLSET = "offline_docsets"


def _json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(message: str) -> str:
    return _json({"error": message})


def _optional_name(args: dict, key: str, hint: str):
    value = args.get(key)
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, _error(f"{key} must be {hint}")
    return value.strip(), None


def register(ctx):
    def settings():
        paths = library.configured_paths(ctx.get_config("paths", default=[]))
        if not paths:
            raise library.SettingsError(
                "no docset paths are configured (plugins.entries.offline-docsets.settings.paths)")
        return paths, library.max_chars(ctx.get_config("max_chars", default=None))

    def docsets():
        paths, limit_chars = settings()
        found, problems = library.discover(paths)
        return found, problems, limit_chars

    def run(name, work):
        try:
            return _json(work())
        except (library.SettingsError, library.LookupFailed) as exc:
            return _error(str(exc))
        except Exception as exc:
            logger.warning("%s failed: %s", name, exc)
            return _error(f"{name} failed: {exc}")

    def list_docsets(args: dict, **kwargs) -> str:
        def work():
            found, problems, _ = docsets()
            return library.list_docsets(found, problems)
        return run("docset_list", work)

    def search(args: dict, **kwargs) -> str:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _error("query is required")
        limit = args.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            return _error("limit must be an integer from 1 to 50")
        docset, problem = _optional_name(args, "docset", "a docset id from docset_list")
        if problem:
            return problem
        kind, problem = _optional_name(args, "type", "an entry type such as Function or Class")
        if problem:
            return problem

        def work():
            found, problems, _ = docsets()
            return library.search(found, problems, query.strip(), docset, kind, limit)
        return run("docset_search", work)

    def read(args: dict, **kwargs) -> str:
        docset, path = args.get("docset"), args.get("path")
        if not isinstance(docset, str) or not docset.strip():
            return _error("docset is required (a docset id from docset_list)")
        if not isinstance(path, str) or not path.strip():
            return _error("path is required (a page path from docset_search)")
        offset = args.get("offset")
        if offset is not None and (not isinstance(offset, int) or isinstance(offset, bool) or offset < 0):
            return _error("offset must be a non-negative integer")

        def work():
            found, _problems, limit_chars = docsets()
            return library.read(found, docset.strip(), path, offset, limit_chars)
        return run("docset_read", work)

    def available() -> bool:
        try:
            return bool(settings()[0])
        except library.SettingsError:
            return False

    for name, schema, handler, description in (
        ("docset_list", schemas.DOCSET_LIST, list_docsets, "List offline documentation sets"),
        ("docset_search", schemas.DOCSET_SEARCH, search, "Search offline documentation sets"),
        ("docset_read", schemas.DOCSET_READ, read, "Read a page from an offline documentation set"),
    ):
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          check_fn=available, description=description)
