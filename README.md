# hermes-offline-docsets

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets the agent look up
API documentation in **docsets** on your own machine, without internet access. A docset is the
offline documentation format used by [Dash](https://kapeli.com/dash) and
[Zeal](https://zealdocs.org/): a `Name.docset` folder holding a SQLite index of every function,
class, method and guide, plus the documentation's HTML pages. Kapeli's public feeds alone list
about 180 of them, among them Python, Node.js, Bash, SQLite, Vim, Go, Ruby and PostgreSQL.

Useful when the agent runs somewhere with no or poor connectivity, when you want answers from the
exact documentation version you have installed, or when you would rather not send every lookup to
a web search provider.

This plugin is not affiliated with Kapeli (Dash) or the Zeal project. It reads the docset format
with Python's standard library only. Both index layouts docsets use are read: Dash's `searchIndex`
table and Apple's Core Data token tables (which Kapeli's Python docset uses, for example).

## Tools

| Tool | What it does |
|---|---|
| `docset_list` | Lists the configured docsets: id, title, family keyword, number of index entries, the most common entry types (Function, Class, Guide, ...) and the start page. |
| `docset_search` | Searches the index of one docset or all of them by entry name. Every word of the query must appear in the name. Names that start with the query come first, then shorter names, so the exact name leads. Can be limited to one entry type. |
| `docset_read` | Reads a page as plain text, with headings marked `#` and list items `-`. Given a path from `docset_search` (for example `nodejs/api/fs.html#fsreadfilepath-options`), the text starts at that entry's section rather than at the top of the page. Long pages come back in parts (`next_offset`). The first part also lists the page's links to other pages of the same docset. |

The tools are offered to the model only while at least one path is configured.

## Install

```bash
hermes plugins install offline-docsets --enable
```

No dependencies: the plugin uses only the Python standard library (`sqlite3`, `plistlib`,
`html.parser`).

## Get some docsets

If you already use Dash or Zeal, point `paths` at the folder where it keeps its docsets. Docsets
whose pages are not in an unpacked `Contents/Resources/Documents` folder are reported as
unavailable (see Limits).

You can also download one yourself. Kapeli publishes a feed per docset at
`https://kapeli.com/feeds/<Name>.xml`, for example
[`Bash.xml`](https://kapeli.com/feeds/Bash.xml), listing mirror URLs of a `.tgz` archive:

```bash
mkdir -p ~/docsets && cd ~/docsets
curl -LO http://sanfrancisco.kapeli.com/feeds/Bash.tgz   # a URL from Bash.xml
tar -xzf Bash.tgz                                        # creates Bash.docset/
```

## Configure

```yaml
plugins:
  entries:
    offline-docsets:
      settings:
        paths:                 # .docset folders and/or folders holding them
          - ~/docsets
          - /data/docs/NodeJS.docset
        max_chars: 12000       # optional; characters per docset_read call (1000-100000)
```

A folder that is not itself a docset is scanned one level deep for `*.docset` folders. Each docset's
id is its folder name without `.docset`, for example `NodeJS`. If two folders hold docsets with the
same name, the first one wins and the second is reported under `unavailable`. Missing paths and
unreadable docsets are reported the same way and do not hide the others.

## What it does on your machine (disclosure)

- **Reads only the docsets you configure.** For each one it opens `Contents/Info.plist`, the
  `Contents/Resources/docSet.dsidx` index and pages inside `Contents/Resources/Documents`. A page
  path that resolves outside `Documents` (through `..`, an encoded `%2e%2e`, an absolute path or a
  symlink) is refused. Folders are listed only to find `*.docset` folders.
- **No network.** Search and reading are local. The test suite checks this by refusing to open any
  socket while all three tools run. Links to outside sites are left out of the link list.
- **Writes nothing.** The index is opened for each call with SQLite's `mode=ro&immutable=1` and
  closed again; nothing is kept between calls. `immutable=1` matters: some docsets ship their
  index in WAL mode, and plain `mode=ro` then writes the `-shm` file, or creates `-wal` and `-shm`
  files when they are missing. The tests check that every connection is opened this way and
  closed, and that no file of a WAL-mode index changes.
- **No credentials, no shell commands, no background work.**
- **Content is third-party text.** Page text is returned to the model as the documentation's
  authors wrote it. Scripts and styles are stripped and HTML is reduced to plain text, but the words
  are not filtered. Use docsets from sources you trust.

## Limits

- A docset with no unpacked `Documents` folder (for example one whose pages are kept in a
  compressed archive) is reported as unavailable, and so is an index that has neither a
  `searchIndex` table nor the Core Data tables `ZTOKEN`, `ZTOKENTYPE`, `ZTOKENMETAINFORMATION` and
  `ZFILEPATH`.
- An index whose `docSet.dsidx-wal` or `docSet.dsidx-journal` file is not empty has changes another
  program has not finished writing. It is reported as unavailable rather than read incompletely;
  it can be read once that program closes it.
- Search matches entry names, not the full text of pages.
- Text only: images and other files inside a docset are not returned.
- Pages larger than 20 MB are refused. Up to 50 in-docset links are listed per page.

## Development

```bash
python -m pip install pytest
python -m pytest -c tests/pytest.ini tests
OFFLINE_DOCSETS_REAL=~/docsets python -m pytest -c tests/pytest.ini tests -k real_world -s
hermes plugins validate .
```

The tests build docsets in the real layout (an `Info.plist`, a `docSet.dsidx` SQLite index and
HTML pages) in both index layouts, the Core Data one in WAL mode with `<dash_entry_...>` path
prefixes as Kapeli's Python docset has it. They also build layouts the plugin must refuse: Core
Data tables with some missing, no `Documents` folder, an index that is not SQLite, an index with
changes waiting in its `-wal` file, and an index entry and a symlink pointing outside the docset.

`OFFLINE_DOCSETS_REAL` (one or more paths, separated like `PATH`) adds a test against downloaded
docsets. It samples up to 200 entries per docset and checks that searching for each entry's exact
name returns it first, that reading it starts at its anchor, and that no file of the index
changes.

## License

MIT
