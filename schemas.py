"""Tool schemas the model reads."""

DOCSET_LIST = {
    "name": "docset_list",
    "description": (
        "List the offline documentation sets (Dash/Zeal docsets, for example Python, Node.js, Bash "
        "or SQLite) the user has on this machine. Returns each docset's id, title, family keyword, "
        "number of index entries, its most common entry types (Function, Class, Guide, ...) and "
        "its start page. Works without internet access."
    ),
    "parameters": {"type": "object", "properties": {}},
}

DOCSET_SEARCH = {
    "name": "docset_search",
    "description": (
        "Search the index of the user's offline documentation sets by entry name (functions, "
        "classes, methods, guides, ...). Every word of the query must appear in the name; exact "
        "names come first, then names that start with the query. Returns the docset id, name, "
        "type and path of each match; pass docset and path to docset_read to read the entry."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Name or words of the name to look for, e.g. 'readFile' or 'printf'."},
            "docset": {
                "type": "string",
                "description": "Search only this docset id (from docset_list). Default: every configured docset.",
            },
            "type": {
                "type": "string",
                "description": "Only entries of this type, e.g. Function, Class, Method, Guide (docset_list shows each docset's types).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results (1-50, default 10).",
                "minimum": 1,
                "maximum": 50,
            },
        },
        "required": ["query"],
    },
}

DOCSET_READ = {
    "name": "docset_read",
    "description": (
        "Read a documentation page from an offline docset as plain text (headings marked with #, "
        "list items with -). Given a path from docset_search, the text starts at that entry's "
        "section of the page. Long pages come back in parts: pass the returned next_offset as "
        "offset to read on. The first part also lists the page's links to other pages of the "
        "same docset, as paths docset_read accepts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "docset": {"type": "string", "description": "Docset id from docset_list or docset_search."},
            "path": {"type": "string", "description": "Page path from docset_search or a link, including any #anchor."},
            "offset": {
                "type": "integer",
                "description": "Character offset to read from. Omit it to start at the path's anchor.",
                "minimum": 0,
            },
        },
        "required": ["docset", "path"],
    },
}
