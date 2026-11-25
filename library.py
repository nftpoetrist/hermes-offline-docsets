"""Read Dash-format docsets (the offline API documentation sets Dash and Zeal use).

A docset is a folder ``Name.docset`` holding ``Contents/Info.plist``, a SQLite index at
``Contents/Resources/docSet.dsidx`` and the HTML pages under ``Contents/Resources/Documents``. The
index is either Dash's ``searchIndex(name, type, path)`` table or Apple's Core Data token tables.
Everything here is local: the index is opened per call without writing anything and closed again,
pages are read from inside ``Documents`` only, and nothing goes over the network. Nothing is
cached, so one profile's ``paths`` never serve another's.
"""
from __future__ import annotations

import plistlib
import posixpath
import re
import sqlite3
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

DEFAULT_MAX_CHARS = 12_000
MAX_CHARS_LIMIT = 100_000
MAX_LINKS = 50
MAX_PAGE_BYTES = 20 * 1024 * 1024
_HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
_TEXT_SUFFIXES = {".txt", ".md"}
_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_DASH_ENTRY_RE = re.compile(r"^(?:<dash_entry_[^>]*>)+")  # Dash metadata some docsets put before a path
_ANCHOR = "\x00"  # marks where the requested #fragment sits in the extracted text


class SettingsError(ValueError):
    """This plugin's settings cannot be used as written."""


class LookupFailed(ValueError):
    """A docset or page the model asked for does not exist or is not readable text."""


def configured_paths(value: Any) -> list[Path]:
    """Validate ``settings.paths``: ``.docset`` folders and/or folders holding them."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(p, str) and p.strip() for p in value):
        raise SettingsError("settings.paths must be a list of .docset folders or folders holding them")
    return [Path(p.strip()).expanduser() for p in value]


def max_chars(value: Any) -> int:
    if value in (None, ""):
        return DEFAULT_MAX_CHARS
    if not isinstance(value, int) or isinstance(value, bool) or not 1_000 <= value <= MAX_CHARS_LIMIT:
        raise SettingsError(f"settings.max_chars must be an integer from 1000 to {MAX_CHARS_LIMIT}")
    return value


def _is_docset(path: Path) -> bool:
    return path.suffix.lower() == ".docset" and path.is_dir()


def discover(paths: list[Path]) -> tuple[dict[str, Path], list[str]]:
    """Map docset id (the folder name without ``.docset``) to its folder, plus problems found.

    A folder that is not itself a docset is scanned one level deep for ``*.docset`` folders. Ids
    must be unique: a second docset with the same name elsewhere is skipped and reported.
    """
    docsets: dict[str, Path] = {}
    problems: list[str] = []
    for path in paths:
        if _is_docset(path):
            found = [path]
        elif path.is_dir():
            found = sorted(p for p in path.iterdir() if _is_docset(p))
            if not found:
                problems.append(f"{path}: no .docset folders in this folder")
        else:
            problems.append(f"{path}: not a .docset folder or a folder holding them")
            continue
        for folder in found:
            docset = _ID_RE.sub("_", folder.stem)
            if docset in docsets:
                problems.append(f"{folder}: skipped, docset id {docset!r} is already {docsets[docset]}")
                continue
            docsets[docset] = folder
    return docsets, problems


def _resources(folder: Path) -> Path:
    return folder / "Contents" / "Resources"


# Dash's own layout: one table of (name, type, path#anchor) rows.
_DASH_ENTRIES = "SELECT name, type, path FROM searchIndex"
# Apple's Core Data layout (also used by some Kapeli docsets, Python's among them): each token links
# to its type and to meta information holding the page and the anchor.
_CORE_DATA_TABLES = {"ZTOKEN", "ZTOKENTYPE", "ZTOKENMETAINFORMATION", "ZFILEPATH"}
_CORE_DATA_ENTRIES = (
    "SELECT t.ZTOKENNAME AS name, ty.ZTYPENAME AS type, "
    "f.ZPATH || CASE WHEN m.ZANCHOR IS NULL OR m.ZANCHOR = '' THEN '' ELSE '#' || m.ZANCHOR END AS path "
    "FROM ZTOKEN t "
    "JOIN ZTOKENMETAINFORMATION m ON m.Z_PK = t.ZMETAINFORMATION "
    "JOIN ZFILEPATH f ON f.Z_PK = m.ZFILE "
    "LEFT JOIN ZTOKENTYPE ty ON ty.Z_PK = t.ZTOKENTYPE "
    "WHERE t.ZTOKENNAME IS NOT NULL"
)


class _Index:
    """An open docset index and the query that lists its entries as (name, type, path) rows."""

    def __init__(self, conn: sqlite3.Connection, entries: str):
        self.conn, self.entries = conn, entries

    def close(self) -> None:
        self.conn.close()


def _connect(folder: Path) -> _Index:
    """The docset's index, opened without writing anything; LookupFailed for what cannot be read.

    ``immutable=1`` makes SQLite read the file as it is, with no locking and no ``-shm``/``-wal``
    files written or created (plain ``mode=ro`` writes them for a WAL-mode index, which some
    published docsets are). It would also skip changes still waiting in a write-ahead log or a
    rollback journal, so an index with a non-empty ``-wal`` or ``-journal`` file is refused instead
    of read incompletely.
    """
    resources = _resources(folder)
    index = resources / "docSet.dsidx"
    if not index.is_file():
        raise LookupFailed(f"{folder}: no Contents/Resources/docSet.dsidx index")
    if not (resources / "Documents").is_dir():
        raise LookupFailed(f"{folder}: no Contents/Resources/Documents folder (compressed docsets are not supported)")
    for suffix in ("-wal", "-journal"):
        pending = resources / f"docSet.dsidx{suffix}"
        if pending.is_file() and pending.stat().st_size > 0:
            raise LookupFailed(f"{folder}: the index has unsaved changes in {pending.name} "
                               "(is another program writing it?); try again once it is closed")
    conn = sqlite3.connect(f"{index.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        if "searchIndex" in tables:
            return _Index(conn, _DASH_ENTRIES)
        if _CORE_DATA_TABLES <= tables:
            return _Index(conn, _CORE_DATA_ENTRIES)
        missing = "Core Data tables " + ", ".join(sorted(_CORE_DATA_TABLES - tables)) if "ZTOKEN" in tables else "a searchIndex table"
        raise LookupFailed(f"{folder}: unsupported index (no {missing})")
    except BaseException:
        conn.close()
        raise


def _info(folder: Path) -> dict[str, Any]:
    try:
        with open(folder / "Contents" / "Info.plist", "rb") as fh:
            info = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def list_docsets(docsets: dict[str, Path], problems: list[str]) -> dict[str, Any]:
    out, unavailable = [], list(problems)
    for docset, folder in docsets.items():
        try:
            with closing(_connect(folder)) as index:
                entries = index.conn.execute(f"SELECT COUNT(*) FROM ({index.entries})").fetchone()[0]
                types = [row[0] for row in index.conn.execute(
                    f"SELECT type FROM ({index.entries}) WHERE type IS NOT NULL "
                    "GROUP BY type ORDER BY COUNT(*) DESC, type LIMIT 12")]
        except (LookupFailed, sqlite3.Error) as exc:  # a damaged docset must not hide the others
            unavailable.append(str(exc) if isinstance(exc, LookupFailed) else f"{folder}: {exc}")
            continue
        info = _info(folder)
        entry = {"docset": docset, "title": _text(info.get("CFBundleName")) or docset,
                 "family": _text(info.get("DocSetPlatformFamily")), "entries": entries, "types": types,
                 "index_page": _text(info.get("dashIndexFilePath"))}
        out.append({k: v for k, v in entry.items() if v is not None})
    result: dict[str, Any] = {"docsets": out}
    if unavailable:
        result["unavailable"] = unavailable
    return result


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _search_one(index: _Index, query: str, kind: str | None, limit: int) -> tuple[list[tuple], int]:
    """Entries whose name holds every word of ``query``: names starting with it first, then the rest.

    Within each group shorter names come first, so the exact name (the shortest possible match) leads.
    """
    words = query.split()
    where = " AND ".join("name LIKE ? ESCAPE '\\'" for _ in words)
    params: list[Any] = [_like(w) for w in words]
    if kind is not None:
        where += " AND type = ? COLLATE NOCASE"
        params.append(kind)
    total = index.conn.execute(f"SELECT COUNT(*) FROM ({index.entries}) WHERE {where}", params).fetchone()[0]
    rows = index.conn.execute(
        f"SELECT name, type, path FROM ({index.entries}) WHERE {where} "
        "ORDER BY CASE WHEN name LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, length(name), name LIMIT ?",
        [*params, _like(query)[1:], limit]).fetchall()
    return rows, total


def search(docsets: dict[str, Path], problems: list[str], query: str, docset: str | None,
           kind: str | None, limit: int) -> dict[str, Any]:
    if docset is not None and docset not in docsets:
        raise LookupFailed(f"no docset {docset!r}; docset_list lists the configured ones")
    targets = {docset: docsets[docset]} if docset is not None else docsets
    per_docset, unavailable, counts = [], list(problems), {}
    for name, folder in targets.items():
        try:
            with closing(_connect(folder)) as index:
                rows, counts[name] = _search_one(index, query, kind, limit)
        except (LookupFailed, sqlite3.Error) as exc:
            unavailable.append(str(exc) if isinstance(exc, LookupFailed) else f"{folder}: {exc}")
            continue
        per_docset.append([{"docset": name, "name": n, "type": t, "path": _DASH_ENTRY_RE.sub("", p)} for n, t, p in rows])
    results = []  # interleave so one big docset does not crowd out the rest
    for rank in range(limit):
        for hits in per_docset:
            if rank < len(hits) and len(results) < limit:
                results.append(hits[rank])
    payload: dict[str, Any] = {"results": results, "matches": counts}
    if unavailable:
        payload["unavailable"] = unavailable
    return payload


class _TextExtractor(HTMLParser):
    """HTML to readable text, the links to other pages of the docset, and where an anchor sits."""

    _SKIP = {"script", "style", "noscript", "head", "template", "svg"}
    _BLOCK = {"p", "div", "section", "article", "br", "tr", "table", "blockquote", "dd", "dt",
              "figcaption", "caption", "ul", "ol", "dl", "hr", "pre", "details", "summary", "header",
              "footer", "aside", "main", "nav"}

    def __init__(self, page: str, anchor: str | None):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._seen_links: set[str] = set()
        self._skip = 0
        self._pre = 0
        self._link: tuple[str, list[str]] | None = None
        self._page = page
        self._anchor = anchor
        self.anchor_found = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self._anchor and not self.anchor_found and self._anchor in (attrs.get("name"), attrs.get("id")):
            self.anchor_found = True
            self.parts.append(_ANCHOR)
        if tag in self._SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "pre":
            self._pre += 1
        if tag in self._BLOCK:
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag == "a":
            target = self._target(attrs.get("href"))
            self._link = (target, []) if target else None

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(self._skip - 1, 0)
            return
        if self._skip:
            return
        if tag == "pre":
            self._pre = max(self._pre - 1, 0)
        if tag in self._BLOCK or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")
        elif tag == "a" and self._link:
            target, words = self._link
            self._link = None
            text = _WS_RE.sub(" ", "".join(words)).strip()
            if text and target not in self._seen_links and len(self.links) < MAX_LINKS:
                self._seen_links.add(target)
                self.links.append({"path": target, "text": text})

    def handle_data(self, data):
        if self._skip:
            return
        if self._link:
            self._link[1].append(data)
        self.parts.append(data if self._pre else _WS_RE.sub(" ", data.replace("\n", " ")))

    def _target(self, href: str | None) -> str | None:
        """The docset path a link points at (fragment kept), or None for outside links and same-page anchors."""
        if not href:
            return None
        try:
            parts = urlsplit(href)
        except ValueError:  # e.g. "http://[user@]host/path" written as an example in the docs
            return None
        if parts.scheme or parts.netloc or not parts.path:
            return None
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(self._page), parts.path))
        if parts.path.startswith("/") or joined.startswith("..") or joined in (".", ""):
            return None
        return joined + (f"#{parts.fragment}" if parts.fragment else "")

    def text(self) -> tuple[str, int | None]:
        """The cleaned text and where the first line at or after the anchor starts (None if not found).

        The text is the same whether or not an anchor was asked for; only the offset differs.
        """
        out: list[str] = []
        blank, pending, anchor_line = False, False, None
        for line in "".join(self.parts).split("\n"):
            if _ANCHOR in line:
                pending = True
                line = line.replace(_ANCHOR, "")
            line = line.strip()
            if line:
                if pending and anchor_line is None:
                    anchor_line = len(out)
                out.append(line)
                blank = False
            elif not blank and out:
                out.append("")
                blank = True
        while out and not out[-1]:
            out.pop()
        if anchor_line is None or anchor_line >= len(out):
            return "\n".join(out), None
        return "\n".join(out), sum(len(line) + 1 for line in out[:anchor_line])


def html_to_text(html_text: str, page: str, anchor: str | None = None) -> tuple[str, list[dict[str, str]], int | None]:
    parser = _TextExtractor(page, anchor)
    parser.feed(html_text)
    parser.close()
    text, at = parser.text()
    return text, parser.links, at


def _page_file(folder: Path, page: str) -> Path:
    """The file for ``page`` inside the docset's Documents folder; nothing outside it is opened."""
    documents = (_resources(folder) / "Documents").resolve()
    candidate = (documents / page).resolve()
    if candidate != documents and not candidate.is_relative_to(documents):
        raise LookupFailed(f"{page!r} points outside the docset")
    if not candidate.is_file():
        raise LookupFailed(f"no page {page!r} in this docset; use docset_search to find its path")
    return candidate


def read(docsets: dict[str, Path], docset: str, path: str, offset: int | None, limit_chars: int) -> dict[str, Any]:
    """A documentation page as plain text, ``limit_chars`` at a time.

    Without ``offset`` the text starts at the line holding the path's ``#fragment`` anchor (the
    entry docset_search found), or at the top of the page when there is no fragment or it is not
    in the page.
    """
    if docset not in docsets:
        raise LookupFailed(f"no docset {docset!r}; docset_list lists the configured ones")
    folder = docsets[docset]
    with closing(_connect(folder)):  # same layout checks as list and search
        pass
    try:
        parts = urlsplit(_DASH_ENTRY_RE.sub("", path.strip()))
    except ValueError:
        raise LookupFailed(f"{path!r} is not a page path; use docset_search to find one") from None
    if parts.scheme or parts.netloc:
        raise LookupFailed(f"{path!r} is an outside link, not a page of this docset")
    page = unquote(parts.path).lstrip("/")
    if not page:
        raise LookupFailed("path names no page")
    file = _page_file(folder, page)
    if file.stat().st_size > MAX_PAGE_BYTES:
        raise LookupFailed(f"{page} is larger than {MAX_PAGE_BYTES // (1024 * 1024)} MB")
    suffix = file.suffix.lower()
    if suffix not in _HTML_SUFFIXES | _TEXT_SUFFIXES:
        raise LookupFailed(f"{page} is a {suffix or 'suffix-less'} file, not a text page")
    raw = file.read_bytes().decode("utf-8", errors="replace")
    anchor = parts.fragment or None
    if suffix in _HTML_SUFFIXES:
        text, links, at = html_to_text(raw, page, anchor)
    else:
        text, links, at = raw.strip(), [], None
    start = offset if offset is not None else (at or 0)
    chunk = text[start:start + limit_chars]
    end = start + len(chunk)
    payload: dict[str, Any] = {"docset": docset, "path": page, "text": chunk, "offset": start,
                               "total_chars": len(text), "next_offset": end if end < len(text) else None}
    if anchor:
        payload["anchor"] = anchor
        payload["anchor_found"] = at is not None
    if offset is None and links:
        payload["links"] = links
    return payload
