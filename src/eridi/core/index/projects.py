"""Projects in the index: which folders belong to which project, and SQL to scope by project.

`projects` and `project_roots` are derived from `project` entries in the record (see
`records.py`), so they are rebuilt with everything else.
"""

import sqlite3

from eridi.core.models import Project, ProjectFolder

_PREFIX_MATCH = (
    "(pr.path_prefix = '' OR {path} = pr.path_prefix "
    "OR substr({path}, 1, length(pr.path_prefix) + 1) = pr.path_prefix || '/')"
)


# Aliases are constants from our own code; project IDs are always passed as parameters.


def file_in_project_sql(alias: str = "f") -> str:
    """A condition true when file `alias` is in the project given by one `?` parameter."""
    match = _PREFIX_MATCH.format(path=f"{alias}.path")
    return (
        "EXISTS (SELECT 1 FROM project_roots pr WHERE pr.project_id = ? "  # noqa: S608
        f"AND pr.root_id = {alias}.root_id AND {match})"
    )


def entry_in_project_sql(alias: str = "e") -> str:
    """A condition true when entry `alias` belongs to the project given by two `?` parameters:
    it names the project, or it is about a file in one of the project's folders."""
    match = _PREFIX_MATCH.format(path="s.path")
    return (
        f"({alias}.project_id = ? OR EXISTS (SELECT 1 FROM entry_subjects s "  # noqa: S608
        "JOIN project_roots pr ON pr.root_id = s.root_id "
        f"WHERE s.entry_id = {alias}.id AND pr.project_id = ? AND {match}))"
    )


def list_projects(conn: sqlite3.Connection) -> list[Project]:
    folders: dict[str, list[ProjectFolder]] = {}
    for r in conn.execute(
        "SELECT project_id, root_id, path_prefix FROM project_roots "
        "ORDER BY project_id, root_id, path_prefix"
    ):
        folders.setdefault(r[0], []).append(ProjectFolder(r[1], r[2]))
    rows = conn.execute("SELECT id, name, home_root_id FROM projects").fetchall()
    projects = [Project(r[0], r[1], r[2], tuple(folders.get(r[0], ()))) for r in rows]
    return sorted(projects, key=lambda p: (p.name.casefold(), p.id))


def projects_for_file(projects: list[Project], root_id: str, path: str) -> list[Project]:
    """Projects that contain the file, most specific folder first."""
    scored = []
    for p in projects:
        depth = max((len(f.path) for f in p.folders if f.contains(root_id, path)), default=None)
        if depth is not None:
            scored.append((-depth, p.name.casefold(), p))
    return [p for *_, p in sorted(scored, key=lambda t: (t[0], t[1]))]
