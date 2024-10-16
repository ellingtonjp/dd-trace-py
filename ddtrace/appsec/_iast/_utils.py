from functools import lru_cache
import os
import sys
from typing import List
from typing import Text

from ddtrace.appsec._constants import IAST
from ddtrace.internal.utils.formats import asbool


@lru_cache(maxsize=1)
def _is_python_version_supported() -> bool:
    # IAST supports Python versions 3.6 to 3.12
    return (3, 6, 0) <= sys.version_info < (3, 13, 0)


def _get_source_index(sources: List, source) -> int:
    i = 0
    for source_ in sources:
        if hash(source_) == hash(source):
            return i
        i += 1
    return -1


def _get_patched_code(module_path: Text, module_name: Text) -> str:
    """
    Print the patched code to stdout, for debugging purposes.
    """
    import astunparse

    from ddtrace.appsec._iast._ast.ast_patching import get_encoding
    from ddtrace.appsec._iast._ast.ast_patching import visit_ast

    with open(module_path, "r", encoding=get_encoding(module_path)) as source_file:
        source_text = source_file.read()

        new_source = visit_ast(
            source_text,
            module_path,
            module_name=module_name,
        )

        # If no modifications are done,
        # visit_ast returns None
        if not new_source:
            return ""

        new_code = astunparse.unparse(new_source)
        return new_code


if __name__ == "__main__":
    MODULE_PATH = sys.argv[1]
    MODULE_NAME = sys.argv[2]
    print(_get_patched_code(MODULE_PATH, MODULE_NAME))


def _is_iast_debug_enabled():
    return asbool(os.environ.get(IAST.ENV_DEBUG, "false"))
