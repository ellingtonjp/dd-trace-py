"""
Trace the standard library ``webbrowser`` library to trace
HTTP requests and detect SSRF vulnerabilities. It is enabled by default
if ``DD_IAST_ENABLED`` is set to ``True`` (for detecting sink points) and/or
``DD_ASM_ENABLED`` is set to ``True`` (for exploit prevention).
"""
from ...internal.utils.importlib import require_modules


required_modules = ["webbrowser"]

with require_modules(required_modules) as missing_modules:
    if not missing_modules:
        # Required to allow users to import from `ddtrace.contrib.webbrowser.patch` directly
        from . import patch as _  # noqa: F401, I001

        # Expose public methods
        from ..internal.webbrowser.patch import get_version
        from ..internal.webbrowser.patch import patch
        from ..internal.webbrowser.patch import unpatch

        __all__ = ["patch", "unpatch", "get_version"]
