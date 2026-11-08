"""Express path rendering shared by Express registration and frontend API clients.

The API IR keeps the spelling produced by the design stage, so one path may arrive as
``/api/books/{title}`` and another already as ``/api/books/:title``. Both are accepted
and normalized at the emission points: Express routes always use ``:name`` parameters,
and generated clients substitute the same names from ``request.params`` before a
request is sent, so a placeholder can never be requested as a literal URL. The
parameter syntax itself is defined once in ``infrastructure.shared.parameters`` and is
reused by generated page routes.
"""

from infrastructure.shared.parameters import (BRACES, DANGLING, PARAMETER, PARAMETER_NAME,
    PARAMETER_PATTERN, WHOLE_PARAMETER)

__all__ = ["BRACES", "DANGLING", "PARAMETER", "PARAMETER_NAME", "PARAMETER_PATTERN",
    "WHOLE_PARAMETER", "express_path"]


def express_path(path: str) -> str:
    """Render an API IR path in Express syntax so parameters never match as literals."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"API path must be an absolute route beginning with '/': {path!r}")
    route = PARAMETER.sub(lambda match: ":" + (match.group(1) or match.group(2)), path)
    if BRACES.search(route) or DANGLING.search(route):
        raise ValueError(f"Invalid route parameter in API path: {path}")
    return route

