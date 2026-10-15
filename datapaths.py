"""Where Spooler keeps its files, and the read-only door to its database (pure: no jobs, printer or network code).

`projects.configure`, `slicer.configure` and `splits.configure` point this module at another data directory (tests), so the
read core finds the same files the app does. A reader that is not the app (the MCP server) calls `configure_all(data_dir)`.
"""

import json
import sqlite3
from pathlib import Path

from persistence import DATA_DIR

_roots = {"projects": Path(DATA_DIR), "profiles": Path(DATA_DIR)}


def configure_projects(data_dir: Path | None = None) -> None:
    _roots["projects"] = Path(data_dir or DATA_DIR)


def configure_profiles(data_dir: Path | None = None) -> None:
    _roots["profiles"] = Path(data_dir or DATA_DIR)


def configure_all(data_dir: Path | None = None) -> None:
    configure_projects(data_dir)
    configure_profiles(data_dir)


def projects_dir() -> Path:
    return _roots["projects"] / "projects"


def project_dir(project_id: int) -> Path:
    return projects_dir() / str(int(project_id))


def model_dir(project_id: int, model_id: int) -> Path:
    return project_dir(project_id) / "models" / str(int(model_id))


def plate_dir(project_id: int, plate_id: int) -> Path:
    return project_dir(project_id) / "plates" / str(int(plate_id))


def profiles_dir() -> Path:
    return _roots["profiles"] / "profiles"


def database_file(data_dir: Path | None = None) -> Path:
    """The SQLite file inside a data directory (`database.DB_FILE` for the app's own)."""
    return Path(data_dir or DATA_DIR) / "spooler.db"


def open_readonly(path: Path) -> sqlite3.Connection:
    """A connection that can only read. `database.transaction()` takes a write lock, so a reader that must never change or
    block the app opens its own. Rows are `sqlite3.Row`. Raises sqlite3.OperationalError when the file is missing."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def read_json(folder: Path, name: str):
    """The JSON in `folder/name`, or None when it is missing or damaged."""
    try:
        return json.loads((Path(folder) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
