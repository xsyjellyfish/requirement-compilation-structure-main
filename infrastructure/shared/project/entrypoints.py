"""Implementation source paths, stable exports and portable imports."""

import posixpath
from pathlib import Path, PurePosixPath
import json

from infrastructure.utils.paths import check_path_case
from infrastructure.utils.files import save_json


FILE_ROOTS = {"api": "backend/src", "page": "frontend/src", "trigger": "frontend/src",
    "backend_test": "backend/tests", "e2e_test": "frontend/tests/e2e"}

INDEX = ".arc/implementation_paths.json"
# Reading the index is a deterministic parse of one small file. Repair loops ask for it
# many times per round, so the parsed value is reused until the file changes on disk.
_CACHE: dict[tuple[str, int, int], dict] = {}


def _index(output: Path) -> dict:
    """Return the parsed path index, re-reading only when the file on disk changed."""
    path = output / INDEX
    try:
        status = path.stat()
    except OSError:
        return {}
    key = (str(path), status.st_mtime_ns, status.st_size)
    if key not in _CACHE:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = value
    return _CACHE[key]


def selected_path(output: Path, kind: str, identity: str) -> str | None:
    """Read the implementation path selected for a model ID."""
    paths = _index(output)
    return paths.get(kind, {}).get(identity)


def remember_path(output: Path, kind: str, identity: str, relative: str) -> None:
    """Persist the implementation path for a target without model response metadata."""
    path = output / INDEX
    paths = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    paths.setdefault(kind, {})[identity] = relative
    save_json(path, paths)
    _CACHE.clear()


def implementation_entrypoint(output: Path, kind: str, identity: str) -> dict | None:
    """Resolve the selected path; TypeScript checks the stable named export."""
    relative = selected_path(output, kind, identity)
    if relative is None:
        return None
    check_path_case(output, relative)
    current = output
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Implementation path must not contain symlinks: {relative}")
    if not current.is_file():
        return None
    return {"path": relative, "export": "Page" if kind == "page" else "handle"}


def import_path(source: str, target: str, extension: str = ".js") -> str:
    """Construct a portable import directly to the selected implementation file."""
    imported = posixpath.relpath(PurePosixPath(target).with_suffix(extension).as_posix(), PurePosixPath(source).parent.as_posix())
    return imported if imported.startswith(".") else "./" + imported
