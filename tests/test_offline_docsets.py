"""End-to-end tests against docsets built in the real Dash layout.

The fixtures write ``Info.plist``, a ``docSet.dsidx`` SQLite index and HTML pages the way published
docsets have them, in both index layouts: Dash's ``searchIndex`` table, and Apple's Core Data token
tables in WAL mode with ``<dash_entry_...>`` path prefixes, as Kapeli's Python docset ships. Docsets
the plugin must refuse are there too: Core Data tables with one missing, no ``Documents`` folder, an
index that is not SQLite, an index with changes still waiting in its ``-wal`` file, and an index
entry and a symlink pointing outside the docset. Set ``OFFLINE_DOCSETS_REAL`` to downloaded docsets
to also run the test against real content.
"""
import hashlib
import importlib
import json
import os
import plistlib
import socket
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

LONG_PARAGRAPHS = [f"Paragraph {i}: " + " ".join(f"word{i}-{j}" for j in range(40)) for i in range(60)]

BUILTINS_HTML = """<html><head><title>ignored</title><style>.x{color:red}</style></head>
<body><div class="nav"><a href="index.html">Up</a> <a href="../outside.html">escape</a></div>
<h1>Shell builtins</h1>
<p>Commands built into the shell. See <a href="guide/redirection.html#to-files">redirection</a>,
<a href="guide/two%20words.html">two words</a>, <a href="https://example.com/x">an outside page</a>, <a href="http://[user@]host/path">an example URL</a> and <a href="#echo-section">echo below</a>.</p>
<script>stealSecrets()</script>
<a class="dashAnchor" name="//apple_ref/Builtin/%3A"></a>
<dt><code>:</code> (a colon)</dt><dd><p>Do nothing beyond expanding arguments.</p></dd>
<a class="dashAnchor" name="//apple_ref/Builtin/echo"></a>
<h2 id="echo-section">echo</h2>
<p>Output the args, separated by spaces. Caf&eacute; &amp; more.</p>
<ul><li>-n: no trailing newline</li><li>-e: interpret escapes</li></ul>
<a class="dashAnchor" name="//apple_ref/Builtin/read"></a>
<h2>read</h2><p>Read one line from the standard input.</p>
<table><tr><th>Option</th><th>Meaning</th></tr><tr><td>-r</td><td>raw</td></tr></table>
</body></html>"""

GUIDE_HTML = """<html><body><h1>Redirection</h1><p>Intro.</p>
<h2 id="to-files">Redirecting to files</h2><p>Use &gt; to write a file.</p>
<p>Back to <a href="../builtins.html#//apple_ref/Builtin/echo">echo</a>.</p></body></html>"""

ENTRIES = [
    (":", "Builtin", "builtins.html#//apple_ref/Builtin/%3A"),
    ("echo", "Builtin", "builtins.html#//apple_ref/Builtin/echo"),
    ("read", "Builtin", "builtins.html#//apple_ref/Builtin/read"),
    ("readarray", "Builtin", "builtins.html#//apple_ref/Builtin/readarray"),
    ("readonly", "Builtin", "builtins.html#//apple_ref/Builtin/readonly"),
    ("thread", "Function", "builtins.html"),
    ("Readline Interaction", "Guide", "guide/redirection.html"),
    ("Redirecting to files", "Guide", "guide/redirection.html#to-files"),
    ("__init__", "Function", "builtins.html"),
    ("100%_done", "Variable", "builtins.html"),
    ("Long page", "Guide", "long.html"),
    ("Notes", "Guide", "notes.txt"),
    ("Logo", "Guide", "logo.png"),
    ("Missing", "Guide", "gone.html"),
    ("Escape", "Guide", "../../secret.txt"),
    ("Link out", "Guide", "linked.html"),
]


CORE_DATA_SCHEMA = [  # the columns Kapeli's Python docset has, trimmed to the ones joined here
    "CREATE TABLE ZTOKEN (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZMETAINFORMATION INTEGER, "
    "ZTOKENTYPE INTEGER, ZTOKENNAME VARCHAR)",
    "CREATE TABLE ZTOKENTYPE (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZTYPENAME VARCHAR)",
    "CREATE TABLE ZTOKENMETAINFORMATION (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZFILE INTEGER, "
    "ZTOKEN INTEGER, ZANCHOR VARCHAR)",
    "CREATE TABLE ZFILEPATH (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZPATH VARCHAR)",
]

PY_PREFIX = ("<dash_entry_menuDescription=library/json><dash_entry_originalName=json%20module>"
             "<dash_entry_name=json%20module>")

PY_JSON_HTML = """<html><body><h1>json — JSON encoder and decoder</h1><p>Intro.</p>
<dl><dt><a name="//apple_ref/Function/json.dump" class="dashAnchor"></a>json.dump(obj, fp)</dt>
<dd><p>Serialize obj to fp.</p></dd>
<dt><a name="//apple_ref/Function/json.dumps" class="dashAnchor"></a>json.dumps(obj)</dt>
<dd><p>Serialize obj to a str.</p></dd></dl></body></html>"""


def _core_data_index(path: Path, tokens, wal=True):
    """A Core Data index like Python's: tokens -> meta information (anchor) -> file path."""
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    for statement in CORE_DATA_SCHEMA:
        conn.execute(statement)
    types, files = {}, {}
    for pk, (name, kind, file, anchor) in enumerate(tokens, 1):
        types.setdefault(kind, len(types) + 1)
        files.setdefault(file, len(files) + 1)
        conn.execute("INSERT OR IGNORE INTO ZTOKENTYPE VALUES (?, 17, 1, ?)", (types[kind], kind))
        conn.execute("INSERT OR IGNORE INTO ZFILEPATH VALUES (?, 6, 1, ?)", (files[file], file))
        conn.execute("INSERT INTO ZTOKENMETAINFORMATION VALUES (?, 16, 1, ?, ?, ?)", (pk, files[file], pk, anchor))
        conn.execute("INSERT INTO ZTOKEN VALUES (?, 14, 1, ?, ?, ?)", (pk, pk, types[kind], name))
    conn.commit()
    conn.close()
    if wal:  # Kapeli ships the index with an empty -wal and a -shm beside it
        (path.parent / f"{path.name}-wal").write_bytes(b"")
        (path.parent / f"{path.name}-shm").write_bytes(bytes(32768))


def _docset(root: Path, name: str, entries=None, pages=None, info=None, documents=True, index="searchIndex"):
    folder = root / f"{name}.docset"
    res = folder / "Contents" / "Resources"
    res.mkdir(parents=True)
    with open(folder / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump(info or {"CFBundleName": name, "DocSetPlatformFamily": name.lower(), "isDashDocset": True}, fh)
    if index == "core_data":
        _core_data_index(res / "docSet.dsidx", entries or [])
    else:
        conn = sqlite3.connect(res / "docSet.dsidx")
        if index == "searchIndex":
            conn.execute("CREATE TABLE searchIndex(id INTEGER PRIMARY KEY, name TEXT, type TEXT, path TEXT)")
            conn.execute("CREATE UNIQUE INDEX anchor ON searchIndex (name, type, path)")
            conn.executemany("INSERT INTO searchIndex(name, type, path) VALUES (?, ?, ?)", entries or [])
        else:  # Core Data with its file table missing
            conn.execute(CORE_DATA_SCHEMA[0])
        conn.commit()
        conn.close()
    if documents:
        docs = res / "Documents"
        docs.mkdir()
        for rel, content in (pages or {}).items():
            (docs / rel).parent.mkdir(parents=True, exist_ok=True)
            (docs / rel).write_bytes(content if isinstance(content, bytes) else content.encode())
    return folder


def _pending_wal(index: Path, scratch: Path):
    """Give ``index`` a -wal file holding a committed change not yet written back to the index."""
    work = scratch / "docSet.dsidx"
    work.write_bytes(index.read_bytes())
    writer = sqlite3.connect(work)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO searchIndex(name, type, path) VALUES ('pending', 'Guide', 'x.html')")
    writer.commit()
    for suffix in ("", "-wal", "-shm"):  # copied while the writer still holds the log open
        (index.parent / f"docSet.dsidx{suffix}").write_bytes((scratch / f"docSet.dsidx{suffix}").read_bytes())
    writer.close()
    assert (index.parent / "docSet.dsidx-wal").stat().st_size > 0


def _resource_files(folder: Path):
    res = folder / "Contents/Resources"
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(res.iterdir()) if p.is_file()}


@pytest.fixture(scope="session")
def library_dir(tmp_path_factory):
    root = tmp_path_factory.mktemp("docsets")
    (root / "secret.txt").write_text("TOP SECRET outside the docset")
    shell = _docset(root, "Shell", ENTRIES, {
        "builtins.html": BUILTINS_HTML,
        "guide/redirection.html": GUIDE_HTML,
        "guide/two words.html": "<html><body><h1>Two words</h1><p>A page whose file name has a space.</p></body></html>",
        "index.html": "<html><body><h1>Shell</h1></body></html>",
        "long.html": "<html><body><h1>Long</h1>" + "".join(f"<p>{p}</p>" for p in LONG_PARAGRAPHS) + "</body></html>",
        "notes.txt": "Plain text notes.\nSecond line.",
        "logo.png": b"\x89PNG",
    }, info={"CFBundleName": "Shell Reference", "DocSetPlatformFamily": "sh", "isDashDocset": True,
             "dashIndexFilePath": "index.html"})
    (shell / "Contents/Resources/Documents/linked.html").symlink_to(root / "secret.txt")
    _docset(root, "Lua", [("print", "Function", "manual.html#pdf-print"), ("printf", "Function", "manual.html")],
            {"manual.html": "<html><body><h3 id='pdf-print'>print (...)</h3><p>Prints values.</p></body></html>"})
    _docset(root, "Py", [
        ("json.dumps", "Function", PY_PREFIX + "library/json.html", "//apple_ref/Function/json.dumps"),
        ("json.dump", "Function", PY_PREFIX + "library/json.html", "//apple_ref/Function/json.dump"),
        ("json module", "Module", PY_PREFIX + "library/json.html", None),
    ], {"library/json.html": PY_JSON_HTML}, index="core_data")
    _docset(root, "Apple", index="ZTOKEN only")
    busy = _docset(root, "Busy", [("x", "Guide", "x.html")], {"x.html": "<p>x</p>"})
    _pending_wal(busy / "Contents/Resources/docSet.dsidx", tmp_path_factory.mktemp("busy"))
    _docset(root, "Packed", [("x", "Guide", "x.html")], documents=False)
    broken = root / "Broken.docset/Contents/Resources"
    broken.mkdir(parents=True)
    (broken / "docSet.dsidx").write_bytes(b"this is not a sqlite database at all, just bytes" * 4)
    (broken / "Documents").mkdir()
    (root / "readme.txt").write_text("ignored: not a docset")
    return root


@pytest.fixture(scope="module")
def plugin():
    sys.path.insert(0, str(ROOT.parent))
    try:
        return importlib.import_module(ROOT.name)
    finally:
        sys.path.remove(str(ROOT.parent))


class _Ctx:
    """The slice of Hermes's PluginContext this plugin uses."""

    def __init__(self, settings):
        self.settings, self.tools = settings, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kw):
        self.tools[kw["name"]] = kw


def _tools(plugin, settings):
    ctx = _Ctx(settings)
    plugin.register(ctx)
    return ctx.tools


def _run(tool, args):
    return json.loads(tool["handler"](args))


@pytest.fixture
def tools(plugin, library_dir):
    return _tools(plugin, {"paths": [str(library_dir)]})


def test_registers_exactly_the_declared_tools(plugin):
    manifest = (ROOT / "plugin.yaml").read_text()
    registered = _tools(plugin, {})
    assert sorted(registered) == ["docset_list", "docset_read", "docset_search"]
    for name, tool in registered.items():
        assert f"- {name}" in manifest and not tool.get("is_async")


def test_tools_are_offered_only_while_paths_are_configured(plugin, library_dir):
    assert _tools(plugin, {})["docset_search"]["check_fn"]() is False
    assert _tools(plugin, {"paths": []})["docset_search"]["check_fn"]() is False
    assert _tools(plugin, {"paths": [str(library_dir)]})["docset_read"]["check_fn"]() is True


def test_list_reads_each_docsets_info_and_index(tools, library_dir):
    listed = _run(tools["docset_list"], {})
    by_id = {d["docset"]: d for d in listed["docsets"]}
    assert sorted(by_id) == ["Lua", "Py", "Shell"]
    shell = by_id["Shell"]
    assert (shell["title"], shell["family"], shell["index_page"]) == ("Shell Reference", "sh", "index.html")
    assert shell["entries"] == len(ENTRIES) and shell["types"][0] == "Guide" and "Builtin" in shell["types"]
    assert (by_id["Py"]["entries"], by_id["Py"]["types"]) == (3, ["Function", "Module"])
    problems = "\n".join(listed["unavailable"])
    assert "unsupported index (no Core Data tables ZFILEPATH, ZTOKENMETAINFORMATION, ZTOKENTYPE)" in problems
    assert "no Contents/Resources/Documents folder" in problems
    assert "unsaved changes in docSet.dsidx-wal" in problems
    assert str(library_dir / "Broken.docset") in problems
    assert len(listed["unavailable"]) == 4


def test_search_ranks_prefixes_first_then_shorter_names(tools):
    found = _run(tools["docset_search"], {"query": "read", "docset": "Shell"})
    assert [r["name"] for r in found["results"]] == ["read", "readonly", "readarray", "Readline Interaction", "thread"]
    assert found["results"][0] == {"docset": "Shell", "name": "read", "type": "Builtin",
                                   "path": "builtins.html#//apple_ref/Builtin/read"}
    assert found["matches"] == {"Shell": 5}


def test_search_needs_every_word_and_can_filter_by_type(tools):
    found = _run(tools["docset_search"], {"query": "files redirecting"})
    assert [r["name"] for r in found["results"]] == ["Redirecting to files"]
    typed = _run(tools["docset_search"], {"query": "read", "type": "guide"})
    assert [r["name"] for r in typed["results"]] == ["Readline Interaction"]


def test_like_wildcards_in_the_query_are_literal(tools):
    assert [r["name"] for r in _run(tools["docset_search"], {"query": "%"})["results"]] == ["100%_done"]
    assert [r["name"] for r in _run(tools["docset_search"], {"query": "__"})["results"]] == ["__init__"]


def test_searching_every_docset_interleaves_and_reports_unusable_ones(tools):
    found = _run(tools["docset_search"], {"query": "print"})
    assert [(r["docset"], r["name"]) for r in found["results"]] == [("Lua", "print"), ("Lua", "printf")]
    assert len(found["unavailable"]) == 4
    mixed = _run(tools["docset_search"], {"query": "r", "limit": 4})
    assert [r["docset"] for r in mixed["results"]] == ["Lua", "Shell", "Lua", "Shell"]


def test_read_starts_at_the_entrys_anchor(tools):
    page = _run(tools["docset_read"], {"docset": "Shell", "path": "builtins.html#//apple_ref/Builtin/echo"})
    assert page["anchor_found"] is True and page["offset"] > 0
    assert page["text"].startswith("## echo\n\nOutput the args, separated by spaces. Café & more.")
    assert "- -n: no trailing newline" in page["text"] and "| -r | raw" in page["text"]
    assert "Do nothing" not in page["text"]
    whole = _run(tools["docset_read"], {"docset": "Shell", "path": "builtins.html", "offset": 0})
    assert whole["text"][page["offset"]:].startswith("## echo") and page["total_chars"] == whole["total_chars"]


def test_read_matches_percent_encoded_and_id_anchors(tools):
    colon = _run(tools["docset_read"], {"docset": "Shell", "path": "builtins.html#//apple_ref/Builtin/%3A"})
    assert colon["anchor_found"] is True and colon["text"].startswith(": (a colon)")
    guide = _run(tools["docset_read"], {"docset": "Shell", "path": "guide/redirection.html#to-files"})
    assert guide["text"].startswith("## Redirecting to files")
    lua = _run(tools["docset_read"], {"docset": "Lua", "path": "manual.html#pdf-print"})
    assert lua["text"].startswith("### print (...)")


def test_read_without_a_found_anchor_starts_at_the_top(tools):
    page = _run(tools["docset_read"], {"docset": "Shell", "path": "builtins.html#nowhere"})
    assert (page["anchor_found"], page["offset"]) == (False, 0)
    assert page["text"].startswith("Up escape\n\n# Shell builtins")
    assert "stealSecrets" not in page["text"] and "color:red" not in page["text"] and "ignored" not in page["text"]
    assert "anchor" not in _run(tools["docset_read"], {"docset": "Shell", "path": "builtins.html"})


def test_read_lists_in_docset_links_only(tools):
    page = _run(tools["docset_read"], {"docset": "Shell", "path": "builtins.html"})
    assert page["links"] == [{"path": "index.html", "text": "Up"},
                             {"path": "guide/redirection.html#to-files", "text": "redirection"},
                             {"path": "guide/two%20words.html", "text": "two words"}]
    back = _run(tools["docset_read"], {"docset": "Shell", "path": "guide/redirection.html"})
    assert back["links"] == [{"path": "builtins.html#//apple_ref/Builtin/echo", "text": "echo"}]
    followed = _run(tools["docset_read"], {"docset": "Shell", "path": back["links"][0]["path"]})
    assert followed["text"].startswith("## echo")
    spaced = _run(tools["docset_read"], {"docset": "Shell", "path": page["links"][2]["path"]})
    assert (spaced["path"], spaced["text"]) == ("guide/two words.html", "# Two words\n\nA page whose file name has a space.")


def test_long_pages_are_read_in_parts_that_join_up(plugin, library_dir):
    read = _tools(plugin, {"paths": [str(library_dir / "Shell.docset")], "max_chars": 1000})["docset_read"]
    parts, offset = [], None
    while True:
        part = _run(read, {"docset": "Shell", "path": "long.html", **({} if offset is None else {"offset": offset})})
        assert len(part["text"]) <= 1000
        assert ("links" in part) is False
        parts.append(part["text"])
        offset = part["next_offset"]
        if offset is None:
            break
    whole = "".join(parts)
    assert len(parts) > 2 and len(whole) == part["total_chars"]
    assert all(p in whole for p in LONG_PARAGRAPHS)


def test_plain_text_pages_are_returned_as_is(tools):
    assert _run(tools["docset_read"], {"docset": "Shell", "path": "notes.txt"})["text"] == "Plain text notes.\nSecond line."


def test_nothing_outside_the_docset_is_read(tools, library_dir):
    for path in ("../../secret.txt", "../../../secret.txt", "linked.html", "/../../secret.txt",
                 "%2e%2e/%2e%2e/secret.txt", "guide/..%2f..%2f..%2fsecret.txt"):
        result = _run(tools["docset_read"], {"docset": "Shell", "path": path})
        assert "points outside the docset" in result["error"], (path, result)
    assert "outside link" in _run(tools["docset_read"], {"docset": "Shell", "path": "file:///etc/passwd"})["error"]


@pytest.mark.parametrize("args, message", [
    ({"docset": "Shell", "path": "logo.png"}, "not a text page"),
    ({"docset": "Shell", "path": "gone.html"}, "use docset_search"),
    ({"docset": "nope", "path": "index.html"}, "docset_list"),
    ({"docset": "Shell", "path": "#only-a-fragment"}, "names no page"),
    ({"docset": "Shell", "path": "http://[user@]host/path"}, "not a page path"),
    ({"docset": "Shell"}, "path is required"),
    ({"path": "index.html"}, "docset is required"),
    ({"docset": "Shell", "path": "index.html", "offset": -1}, "offset must be"),
])
def test_read_errors_are_reported_not_raised(tools, args, message):
    assert message in _run(tools["docset_read"], args)["error"]


def test_core_data_docsets_are_searched_and_read_like_dash_ones(tools):
    found = _run(tools["docset_search"], {"query": "json.dump", "docset": "Py"})
    assert found["results"] == [
        {"docset": "Py", "name": "json.dump", "type": "Function", "path": "library/json.html#//apple_ref/Function/json.dump"},
        {"docset": "Py", "name": "json.dumps", "type": "Function", "path": "library/json.html#//apple_ref/Function/json.dumps"},
    ]
    page = _run(tools["docset_read"], {"docset": "Py", "path": found["results"][1]["path"]})
    assert page["anchor_found"] is True and page["text"].startswith("json.dumps(obj)\n\nSerialize obj to a str.")
    module = _run(tools["docset_search"], {"query": "json module", "type": "Module"})["results"]
    assert module == [{"docset": "Py", "name": "json module", "type": "Module", "path": "library/json.html"}]


def test_a_path_with_its_dash_entry_prefix_still_reads(tools):
    page = _run(tools["docset_read"], {"docset": "Py", "path": PY_PREFIX + "library/json.html#//apple_ref/Function/json.dump"})
    assert (page["path"], page["anchor_found"]) == ("library/json.html", True)
    assert page["text"].startswith("json.dump(obj, fp)")


def test_an_index_with_unsaved_changes_is_refused_and_left_alone(plugin, library_dir):
    before = _resource_files(library_dir / "Busy.docset")
    tools = _tools(plugin, {"paths": [str(library_dir / "Busy.docset")]})
    assert "unsaved changes in docSet.dsidx-wal" in _run(tools["docset_list"], {})["unavailable"][0]
    assert "unsaved changes" in _run(tools["docset_read"], {"docset": "Busy", "path": "x.html"})["error"]
    assert _resource_files(library_dir / "Busy.docset") == before


def test_a_refused_layout_is_reported_when_read_directly(plugin, library_dir):
    read = _tools(plugin, {"paths": [str(library_dir / "Apple.docset")]})["docset_read"]
    assert "unsupported index" in _run(read, {"docset": "Apple", "path": "x.html"})["error"]


def test_search_errors_are_reported_not_raised(tools):
    assert "query is required" in _run(tools["docset_search"], {"query": " "})["error"]
    assert "limit must be" in _run(tools["docset_search"], {"query": "x", "limit": 51})["error"]
    assert "docset_list" in _run(tools["docset_search"], {"query": "x", "docset": "nope"})["error"]
    assert "type must be" in _run(tools["docset_search"], {"query": "x", "type": ""})["error"]


@pytest.mark.parametrize("settings, message", [
    ({}, "no docset paths are configured"),
    ({"paths": [1]}, "must be a list"),
    ({"paths": ["/tmp"], "max_chars": 10}, "max_chars"),
])
def test_invalid_settings_are_reported(plugin, settings, message):
    assert message in _run(_tools(plugin, settings)["docset_list"], {})["error"]


def test_missing_paths_empty_folders_and_duplicate_ids_are_reported(plugin, library_dir, tmp_path):
    _docset(tmp_path / "twin", "Lua", [("x", "Guide", "x.html")], {"x.html": "<p>x</p>"})
    (tmp_path / "empty").mkdir()
    listed = _run(_tools(plugin, {"paths": [str(library_dir), str(tmp_path / "twin"), str(tmp_path / "empty"),
                                            str(tmp_path / "gone")]})["docset_list"], {})
    assert sorted(d["docset"] for d in listed["docsets"]) == ["Lua", "Py", "Shell"]
    problems = "\n".join(listed["unavailable"])
    assert "already" in problems and str(tmp_path / "twin" / "Lua.docset") in problems
    assert "no .docset folders in this folder" in problems
    assert "not a .docset folder or a folder holding them" in problems


def test_every_index_connection_is_read_only_and_closed(plugin, tools, library_dir, monkeypatch):
    library = importlib.import_module(f"{ROOT.name}.library")
    opened, real_connect = [], sqlite3.connect

    class Tracked(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def connect(database, *args, **kwargs):
        conn = real_connect(database, *args, factory=Tracked, **kwargs)
        opened.append((database, kwargs.get("uri"), conn))
        return conn

    monkeypatch.setattr(library.sqlite3, "connect", connect)
    before = {name: _resource_files(library_dir / f"{name}.docset") for name in ("Shell", "Py", "Lua")}
    _run(tools["docset_list"], {})
    _run(tools["docset_search"], {"query": "json"})
    _run(tools["docset_read"], {"docset": "Shell", "path": "builtins.html#//apple_ref/Builtin/echo"})
    _run(tools["docset_read"], {"docset": "Py", "path": "library/json.html#//apple_ref/Function/json.dumps"})
    _run(tools["docset_read"], {"docset": "Apple", "path": "x.html"})
    assert len(opened) >= 11
    for database, uri, conn in opened:
        assert uri is True and database.startswith("file://") and database.endswith("?mode=ro&immutable=1"), database
        assert conn.closed, database
    # No file is written or created, the WAL-mode Core Data index's -wal and -shm included.
    assert {name: _resource_files(library_dir / f"{name}.docset") for name in before} == before
    assert sorted(before["Py"]) == ["docSet.dsidx", "docSet.dsidx-shm", "docSet.dsidx-wal"]
    assert sorted(before["Shell"]) == ["docSet.dsidx"]


def test_nothing_goes_over_the_network(tools, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the plugin opened a network socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert _run(tools["docset_list"], {})["docsets"]
    assert _run(tools["docset_search"], {"query": "echo"})["results"]
    assert _run(tools["docset_read"], {"docset": "Shell", "path": "builtins.html#//apple_ref/Builtin/echo"})["text"]


def _sample(folder, count):
    """Up to ``count`` index entries spread evenly over the whole index, in either index layout."""
    library = importlib.import_module(f"{ROOT.name}.library")
    index = library._connect(Path(folder))
    try:
        rows = index.conn.execute(f"SELECT name, type, path FROM ({index.entries}) ORDER BY path, name").fetchall()
    finally:
        index.close()
    step = max(len(rows) // count, 1)
    return rows[::step][:count]


def _snapshot(folder):
    return {p: (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(Path(folder).rglob("*")) if p.is_file() and "Documents" not in p.parts}


@pytest.mark.skipif(not os.environ.get("OFFLINE_DOCSETS_REAL"), reason="set OFFLINE_DOCSETS_REAL to downloaded docsets")
def test_real_world_docsets(plugin):
    """Every sampled entry is found by its exact name first and reads from its own anchor.

    ``OFFLINE_DOCSETS_REAL`` holds one or more paths (separated like PATH) to docsets or folders
    of them. Their files outside ``Documents`` (index, ``-wal``/``-shm``, plist) must be unchanged.
    """
    paths = [p for p in os.environ["OFFLINE_DOCSETS_REAL"].split(os.pathsep) if p]
    tools = _tools(plugin, {"paths": paths})
    listed = _run(tools["docset_list"], {})
    assert listed["docsets"] and "unavailable" not in listed, listed
    folders, _ = importlib.import_module(f"{ROOT.name}.library").discover([Path(p) for p in paths])
    before = {name: _snapshot(folder) for name, folder in folders.items()}
    report = []
    for docset in listed["docsets"]:
        name = docset["docset"]
        stats = {"docset": name, "entries": docset["entries"], "sampled": 0, "exact_first": 0,
                 "with_anchor": 0, "anchor_found": 0}
        for entry, kind, path in _sample(folders[name], 200):
            stats["sampled"] += 1
            hits = _run(tools["docset_search"], {"query": entry, "docset": name, "limit": 50})["results"]
            stats["exact_first"] += bool(hits) and hits[0]["name"].lower() == entry.lower()
            page = _run(tools["docset_read"], {"docset": name, "path": path})
            assert "error" not in page, (entry, path, page)
            if "anchor" in page:
                stats["with_anchor"] += 1
                stats["anchor_found"] += page["anchor_found"]
        report.append(stats)
    print(json.dumps(report, indent=1))
    for stats in report:
        assert stats["exact_first"] == stats["sampled"], stats
        assert stats["anchor_found"] == stats["with_anchor"], stats
    assert {name: _snapshot(folder) for name, folder in folders.items()} == before
