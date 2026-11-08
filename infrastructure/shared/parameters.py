"""Route parameter syntax shared by generated API paths and page routes.

The design stages may spell one parameter as ``{name}`` or as ``:name``. Both spellings
are accepted wherever the compiler emits a route: Express registration, the generated
API client and the generated UI router. Keeping one definition here stops the Python
emitter and the emitted TypeScript from accepting different syntax.

The pattern is also embedded in generated TypeScript as a RegExp literal, so it must
stay free of anchors and flags: group 1 is a braced parameter, group 2 is an
Express-style parameter.
"""

import re

PARAMETER_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
PARAMETER_PATTERN = r"\{(" + PARAMETER_NAME + r")\}|:(" + PARAMETER_NAME + ")"

PARAMETER = re.compile(PARAMETER_PATTERN)
WHOLE_PARAMETER = re.compile(r"^(?:" + PARAMETER_PATTERN + r")$")
BRACES = re.compile(r"[{}]")
DANGLING = re.compile(r":(?!" + PARAMETER_NAME + ")")
