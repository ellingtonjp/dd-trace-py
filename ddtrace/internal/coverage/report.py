import ast
import json
import linecache
import os
from pathlib import Path
import re
import typing as t

from ddtrace.internal.coverage.util import collapse_ranges


try:
    w, _ = os.get_terminal_size()
except OSError:
    w = 80

NOCOVER_PRAGMA_RE = re.compile(r"^\s*(?P<command>.*)\s*#.*\s+pragma\s*:\s*no\s?cover.*$")

ast_cache: t.Dict[str, t.Any] = {}


def _get_relative_path_strings(executable_lines, workspace_path: Path) -> t.Dict[str, str]:
    relative_path_strs: t.Dict[str, str] = {}

    for path in executable_lines:
        path_obj = Path(path)
        path_str = str(path_obj.relative_to(workspace_path) if path_obj.is_relative_to(workspace_path) else path_obj)
        relative_path_strs[path] = path_str

    return relative_path_strs


def _get_ast_for_path(path: str):
    if path not in ast_cache:
        with open(path, "r") as f:
            file_src = f.read()
        ast_cache[path] = ast.parse(file_src)
    return ast_cache[path]


def find_statement_for_line(node, line):
    if hasattr(node, "body"):
        for child_node in node.body:
            found_node = find_statement_for_line(child_node, line)
            if found_node is not None:
                return found_node

    if not hasattr(node, "end_lineno"):
        return None

    # If the start and end line numbers are the same, we're (almost certainly) dealing with some kind of
    # statement instead of the sort of block statements we're looking for.
    if node.lineno == node.end_lineno:
        return None

    if node.lineno <= line <= node.end_lineno:
        return node

    return None


def no_cover(path, src_line) -> t.Optional[t.Tuple[int, int]]:
    """Returns the start and end lines of statements to ignore the line includes pragma nocover.

    If the line ends with a :, parse the AST and return the block the line belongs to.
    """
    text = linecache.getline(path, src_line).strip()
    matches = NOCOVER_PRAGMA_RE.match(text)
    if matches:
        if matches["command"].strip().endswith(":"):
            parsed = _get_ast_for_path(path)
            statement = find_statement_for_line(parsed, src_line)
            if statement is not None:
                return statement.lineno, statement.end_lineno
            # We shouldn't get here, in theory, but if we do, let's not consider anything uncovered.
            return None
        # If our line does not end in ':', assume it's just one line that needs to be removed
        return src_line, src_line
    return None


def print_coverage_report(executable_lines, covered_lines, workspace_path: Path, ignore_nocover=False):
    total_executable_lines = 0
    total_covered_lines = 0
    total_missed_lines = 0

    if len(executable_lines) == 0:
        print("No Datadog line coverage recorded.")
        return

    relative_path_strs: t.Dict[str, str] = _get_relative_path_strings(executable_lines, workspace_path)

    n = max(len(path_str) for path_str in relative_path_strs.values()) + 4

    covered_lines = covered_lines

    # Title
    print(" DATADOG LINE COVERAGE REPORT ".center(w, "="))

    # Header
    print(f"{'PATH':<{n}}{'LINES':>8}{'MISSED':>8} {'COVERED':>8}  MISSED LINES")
    print("-" * (w))

    for path, orig_lines in sorted(executable_lines.items()):
        path_lines = orig_lines.copy()
        path_covered = covered_lines[path].copy()
        if not ignore_nocover:
            for line in orig_lines:
                # We may have already deleted this line due to no_cover
                if line not in path_lines and line not in path_covered:
                    continue
                no_cover_lines = no_cover(path, line)
                if no_cover_lines:
                    for no_cover_line in range(no_cover_lines[0], no_cover_lines[1] + 1):
                        path_lines.discard(no_cover_line)
                        path_covered.discard(no_cover_line)

        n_lines = len(path_lines)
        n_covered = len(path_covered)
        n_missed = n_lines - n_covered
        total_executable_lines += n_lines
        total_covered_lines += n_covered
        total_missed_lines += n_missed
        if n_covered == 0:
            continue
        missed_ranges = collapse_ranges(sorted(path_lines - path_covered))
        missed = ",".join([f"{start}-{end}" if start != end else str(start) for start, end in missed_ranges])
        missed_str = f"  [{missed}]" if missed else ""
        print(
            f"{relative_path_strs[path]:{n}s}{n_lines:>8}{n_missed:>8}{int(n_covered / n_lines * 100):>8}%{missed_str}"
        )
    print("-" * (w))
    total_covered_percent = int((total_covered_lines / total_executable_lines) * 100)
    print(f"{'TOTAL':<{n}}{total_executable_lines:>8}{total_missed_lines:>8}{total_covered_percent:>8}%")
    print()


def gen_json_report(
    executable_lines, covered_lines, workspace_path: t.Optional[Path] = None, ignore_nocover=False
) -> str:
    """Writes a JSON-formatted coverage report similar in structure to coverage.py 's JSON report, but only
    containing a subset (namely file-level executed and missing lines).

    {
      "files": {
        "path/to/file.py": {
          "executed_lines": [1, 2, 3, 4, 11, 12, 13, ...],
          "missing_lines": [5, 6, 7, 15, 16, 17, ...]
        },
        ...
      }
    }

    Paths are relative to workspace_path if provided, and are absolute otherwise.
    """
    output: t.Dict[str, t.Dict[str, t.Dict[str, t.List[int]]]] = {"files": {}}

    relative_path_strs: t.Dict[str, str] = {}
    if workspace_path is not None:
        relative_path_strs.update(_get_relative_path_strings(executable_lines, workspace_path))

    for path, orig_lines in sorted(executable_lines.items()):
        path_lines = orig_lines.copy()
        path_covered = covered_lines[path].copy()
        if not ignore_nocover:
            for line in orig_lines:
                # We may have already deleted this line due to no_cover
                if line not in path_lines and line not in path_covered:
                    continue
                no_cover_lines = no_cover(path, line)
                if no_cover_lines:
                    for no_cover_line in range(no_cover_lines[0], no_cover_lines[1] + 1):
                        path_lines.discard(no_cover_line)
                        path_covered.discard(no_cover_line)

        path_str = relative_path_strs[path] if workspace_path is not None else path

        output["files"][path_str] = {
            "executed_lines": sorted(list(path_covered)),
            "missing_lines": sorted(list(path_lines - path_covered)),
        }

    return json.dumps(output)


def compare_coverage_reports(coverage_py_filename: str, dd_coverage_filename: str) -> t.Dict[str, t.Any]:
    """Compare two JSON-formatted coverage reports and return a dictionary of the differences."""
    with open(coverage_py_filename, "r") as coverage_py_f:
        coverage_py_data = json.load(coverage_py_f)

    with open(dd_coverage_filename, "r") as dd_coverage_f:
        dd_coverage_data = json.load(dd_coverage_f)

    compared_data: t.Dict[str, t.Any] = {
        "coverage_py_missed_files": [f for f in dd_coverage_data["files"] if f not in coverage_py_data["files"]],
        "coverage_py_missed_executed_lines": {},
        "coverage_py_missed_missing_lines": {},
        "dd_coverage_missed_files": [
            f
            for f in coverage_py_data["files"]
            if len(coverage_py_data["files"][f]["executed_lines"]) > 0 and f not in dd_coverage_data["files"]
        ],
        "dd_coverage_missed_executed_lines": {},
        "dd_coverage_missed_missing_lines": {},
    }

    # Treat coverage.py as "source of truth" when comparing lines
    for path in coverage_py_data["files"].keys() & dd_coverage_data["files"].keys():
        dd_coverage_missed_executed_lines = sorted(
            set(coverage_py_data["files"][path]["executed_lines"])
            - set(coverage_py_data["files"][path]["excluded_lines"])  # Lines not covered because of pragma nocover
            - set(dd_coverage_data["files"][path]["executed_lines"])
        )
        dd_coverage_missed_missing_lines = sorted(
            set(coverage_py_data["files"][path]["missing_lines"])
            - set(dd_coverage_data["files"][path]["missing_lines"])
        )

        coverage_py_missed_executed_lines = sorted(
            set(dd_coverage_data["files"][path]["executed_lines"])
            - set(coverage_py_data["files"][path]["executed_lines"])
        )
        coverage_py_missed_missing_lines = sorted(
            set(dd_coverage_data["files"][path]["missing_lines"])
            - set(coverage_py_data["files"][path]["missing_lines"])
        )

        if dd_coverage_missed_executed_lines:
            compared_data["dd_coverage_missed_executed_lines"][path] = collapse_ranges(
                dd_coverage_missed_executed_lines
            )
        if dd_coverage_missed_missing_lines:
            compared_data["dd_coverage_missed_missing_lines"][path] = collapse_ranges(dd_coverage_missed_missing_lines)
        if coverage_py_missed_executed_lines:
            compared_data["coverage_py_missed_executed_lines"][path] = collapse_ranges(
                coverage_py_missed_executed_lines
            )
        if coverage_py_missed_missing_lines:
            compared_data["coverage_py_missed_missing_lines"][path] = collapse_ranges(coverage_py_missed_missing_lines)

    return compared_data
