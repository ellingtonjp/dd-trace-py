from pathlib import Path
import re
import typing as t

import pytest

from ddtrace import DDTraceDeprecationWarning
from ddtrace import config as dd_config
from ddtrace._monkey import patch
from ddtrace.contrib.internal.coverage.constants import PCT_COVERED_KEY
from ddtrace.contrib.internal.coverage.data import _coverage_data
from ddtrace.contrib.internal.coverage.patch import patch as patch_coverage
from ddtrace.contrib.internal.coverage.patch import run_coverage_report
from ddtrace.contrib.internal.coverage.utils import _is_coverage_invoked_by_coverage_run
from ddtrace.contrib.internal.coverage.utils import _is_coverage_patched
from ddtrace.contrib.internal.pytest._benchmark_utils import _set_benchmark_data_from_item
from ddtrace.contrib.internal.pytest._plugin_v1 import _extract_reason
from ddtrace.contrib.internal.pytest._plugin_v1 import _is_pytest_cov_enabled
from ddtrace.contrib.internal.pytest._types import _pytest_report_teststatus_return_type
from ddtrace.contrib.internal.pytest._types import pytest_CallInfo
from ddtrace.contrib.internal.pytest._types import pytest_Config
from ddtrace.contrib.internal.pytest._types import pytest_TestReport
from ddtrace.contrib.internal.pytest._utils import PYTEST_STATUS
from ddtrace.contrib.internal.pytest._utils import _get_module_path_from_item
from ddtrace.contrib.internal.pytest._utils import _get_names_from_item
from ddtrace.contrib.internal.pytest._utils import _get_session_command
from ddtrace.contrib.internal.pytest._utils import _get_source_file_info
from ddtrace.contrib.internal.pytest._utils import _get_test_id_from_item
from ddtrace.contrib.internal.pytest._utils import _get_test_parameters_json
from ddtrace.contrib.internal.pytest._utils import _is_enabled_early
from ddtrace.contrib.internal.pytest._utils import _is_test_unskippable
from ddtrace.contrib.internal.pytest._utils import _pytest_marked_to_skip
from ddtrace.contrib.internal.pytest._utils import _pytest_version_supports_atr
from ddtrace.contrib.internal.pytest._utils import _pytest_version_supports_efd
from ddtrace.contrib.internal.pytest._utils import _pytest_version_supports_retries
from ddtrace.contrib.internal.pytest._utils import _TestOutcome
from ddtrace.contrib.internal.pytest.constants import FRAMEWORK
from ddtrace.contrib.internal.pytest.constants import XFAIL_REASON
from ddtrace.contrib.internal.pytest.plugin import is_enabled
from ddtrace.contrib.internal.unittest.patch import unpatch as unpatch_unittest
from ddtrace.ext import test
from ddtrace.ext.test_visibility import ITR_SKIPPING_LEVEL
from ddtrace.ext.test_visibility.api import TestExcInfo
from ddtrace.ext.test_visibility.api import TestStatus
from ddtrace.ext.test_visibility.api import disable_test_visibility
from ddtrace.ext.test_visibility.api import enable_test_visibility
from ddtrace.ext.test_visibility.api import is_test_visibility_enabled
from ddtrace.internal.ci_visibility.constants import SKIPPED_BY_ITR_REASON
from ddtrace.internal.ci_visibility.telemetry.coverage import COVERAGE_LIBRARY
from ddtrace.internal.ci_visibility.telemetry.coverage import record_code_coverage_empty
from ddtrace.internal.ci_visibility.telemetry.coverage import record_code_coverage_finished
from ddtrace.internal.ci_visibility.telemetry.coverage import record_code_coverage_started
from ddtrace.internal.ci_visibility.utils import take_over_logger_stream_handler
from ddtrace.internal.coverage.code import ModuleCodeCollector
from ddtrace.internal.coverage.installer import install as install_coverage
from ddtrace.internal.logger import get_logger
from ddtrace.internal.test_visibility.api import InternalTest
from ddtrace.internal.test_visibility.api import InternalTestModule
from ddtrace.internal.test_visibility.api import InternalTestSession
from ddtrace.internal.test_visibility.api import InternalTestSuite
from ddtrace.internal.test_visibility.coverage_lines import CoverageLines
from ddtrace.vendor.debtcollector import deprecate


if _pytest_version_supports_retries():
    from ddtrace.contrib.internal.pytest._retry_utils import get_retry_num

if _pytest_version_supports_efd():
    from ddtrace.contrib.internal.pytest._efd_utils import efd_get_failed_reports
    from ddtrace.contrib.internal.pytest._efd_utils import efd_get_teststatus
    from ddtrace.contrib.internal.pytest._efd_utils import efd_handle_retries
    from ddtrace.contrib.internal.pytest._efd_utils import efd_pytest_terminal_summary_post_yield

if _pytest_version_supports_atr():
    from ddtrace.contrib.internal.pytest._atr_utils import atr_get_failed_reports
    from ddtrace.contrib.internal.pytest._atr_utils import atr_get_teststatus
    from ddtrace.contrib.internal.pytest._atr_utils import atr_handle_retries
    from ddtrace.contrib.internal.pytest._atr_utils import atr_pytest_terminal_summary_post_yield
    from ddtrace.contrib.internal.pytest._atr_utils import quarantine_atr_get_teststatus
    from ddtrace.contrib.internal.pytest._atr_utils import quarantine_pytest_terminal_summary_post_yield

log = get_logger(__name__)


_NODEID_REGEX = re.compile("^((?P<module>.*)/(?P<suite>[^/]*?))::(?P<name>.*?)$")
USER_PROPERTY_QUARANTINED = "dd_quarantined"
OUTCOME_QUARANTINED = "quarantined"
SKIPPED_BY_QUARANTINE_REASON = "Skipped by Datadog Quarantine"


def _handle_itr_should_skip(item, test_id) -> bool:
    """Checks whether a test should be skipped

    This function has the side effect of marking the test as skipped immediately if it should be skipped.
    """
    if not InternalTestSession.is_test_skipping_enabled():
        return False

    suite_id = test_id.parent_id

    item_is_unskippable = InternalTestSuite.is_itr_unskippable(suite_id)

    if InternalTestSuite.is_itr_skippable(suite_id):
        if item_is_unskippable:
            # Marking the test as forced run also applies to its hierarchy
            InternalTest.mark_itr_forced_run(test_id)
            return False

        InternalTest.mark_itr_skipped(test_id)
        # Marking the test as skipped by ITR so that it appears in pytest's output
        item.add_marker(pytest.mark.skip(reason=SKIPPED_BY_ITR_REASON))  # TODO don't rely on internal for reason
        return True

    return False


def _handle_quarantine(item, test_id):
    """Add a user property to identify quarantined tests, and mark them for skipping if quarantine is enabled in
    skipping mode.
    """
    is_quarantined = InternalTest.is_quarantined_test(test_id)
    if is_quarantined:
        # We add this information to user_properties to have it available in pytest_runtest_makereport().
        item.user_properties += [(USER_PROPERTY_QUARANTINED, True)]

        if InternalTestSession.should_skip_quarantined_tests():
            item.add_marker(pytest.mark.skip(reason=SKIPPED_BY_QUARANTINE_REASON))


def _start_collecting_coverage() -> ModuleCodeCollector.CollectInContext:
    coverage_collector = ModuleCodeCollector.CollectInContext()
    # TODO: don't depend on internal for telemetry
    record_code_coverage_started(COVERAGE_LIBRARY.COVERAGEPY, FRAMEWORK)

    coverage_collector.__enter__()

    return coverage_collector


def _handle_collected_coverage(test_id, coverage_collector) -> None:
    # TODO: clean up internal coverage API usage
    test_covered_lines = coverage_collector.get_covered_lines()
    coverage_collector.__exit__()

    record_code_coverage_finished(COVERAGE_LIBRARY.COVERAGEPY, FRAMEWORK)

    if not test_covered_lines:
        log.debug("No covered lines found for test %s", test_id)
        record_code_coverage_empty()
        return

    coverage_data: t.Dict[Path, CoverageLines] = {}

    for path_str, covered_lines in test_covered_lines.items():
        coverage_data[Path(path_str).absolute()] = covered_lines

    InternalTestSuite.add_coverage_data(test_id.parent_id, coverage_data)


def _handle_coverage_dependencies(suite_id) -> None:
    coverage_data = InternalTestSuite.get_coverage_data(suite_id)
    coverage_paths = coverage_data.keys()
    import_coverage = ModuleCodeCollector.get_import_coverage_for_paths(coverage_paths)
    InternalTestSuite.add_coverage_data(suite_id, import_coverage)


def _disable_ci_visibility():
    try:
        disable_test_visibility()
    except Exception:  # noqa: E722
        log.debug("encountered error during disable_ci_visibility", exc_info=True)


def pytest_load_initial_conftests(early_config, parser, args):
    """Performs the bare-minimum to determine whether or ModuleCodeCollector should be enabled

    ModuleCodeCollector has a tangible impact on the time it takes to load modules, so it should only be installed if
    coverage collection is requested by the backend.
    """
    if not _is_enabled_early(early_config):
        return

    try:
        take_over_logger_stream_handler()
        log.warning("This version of the ddtrace pytest plugin is currently in beta.")
        # Freezegun is proactively patched to avoid it interfering with internal timing
        patch(freezegun=True)
        dd_config.test_visibility.itr_skipping_level = ITR_SKIPPING_LEVEL.SUITE
        enable_test_visibility(config=dd_config.pytest)
        if InternalTestSession.should_collect_coverage():
            workspace_path = InternalTestSession.get_workspace_path()
            if workspace_path is None:
                workspace_path = Path.cwd().absolute()
            log.warning("Installing ModuleCodeCollector with include_paths=%s", [workspace_path])
            install_coverage(include_paths=[workspace_path], collect_import_time_coverage=True)
    except Exception:  # noqa: E722
        log.warning("encountered error during configure, disabling Datadog CI Visibility", exc_info=True)
        _disable_ci_visibility()


def pytest_configure(config: pytest_Config) -> None:
    # The only way we end up in pytest_configure is if the environment variable is being used, and logging the warning
    # now ensures it shows up in output regardless of the use of the -s flag
    deprecate(
        "the DD_PYTEST_USE_NEW_PLUGIN_BETA environment variable is deprecated",
        message="this preview version of the pytest ddtrace plugin will become the only version.",
        removal_version="3.0.0",
        category=DDTraceDeprecationWarning,
    )

    try:
        if is_enabled(config):
            unpatch_unittest()
            enable_test_visibility(config=dd_config.pytest)
            if _is_pytest_cov_enabled(config):
                patch_coverage()

            # pytest-bdd plugin support
            if config.pluginmanager.hasplugin("pytest-bdd"):
                from ddtrace.contrib.internal.pytest._pytest_bdd_subplugin import _PytestBddSubPlugin

                config.pluginmanager.register(_PytestBddSubPlugin(), "_datadog-pytest-bdd")
        else:
            # If the pytest ddtrace plugin is not enabled, we should disable CI Visibility, as it was enabled during
            # pytest_load_initial_conftests
            _disable_ci_visibility()
    except Exception:  # noqa: E722
        log.warning("encountered error during configure, disabling Datadog CI Visibility", exc_info=True)
        _disable_ci_visibility()


def pytest_unconfigure(config: pytest_Config) -> None:
    if not is_test_visibility_enabled():
        return

    _disable_ci_visibility()


def pytest_sessionstart(session: pytest.Session) -> None:
    if not is_test_visibility_enabled():
        return

    log.debug("CI Visibility enabled - starting test session")

    try:
        command = _get_session_command(session)

        InternalTestSession.discover(
            test_command=command,
            test_framework=FRAMEWORK,
            test_framework_version=pytest.__version__,
            session_operation_name="pytest.test_session",
            module_operation_name="pytest.test_module",
            suite_operation_name="pytest.test_suite",
            test_operation_name=dd_config.pytest.operation_name,
            reject_duplicates=False,
        )

        InternalTestSession.start()
        if InternalTestSession.efd_enabled() and not _pytest_version_supports_efd():
            log.warning("Early Flake Detection disabled: pytest version is not supported")

    except Exception:  # noqa: E722
        log.debug("encountered error during session start, disabling Datadog CI Visibility", exc_info=True)
        _disable_ci_visibility()


def _pytest_collection_finish(session) -> None:
    """Discover modules, suites, and tests that have been selected by pytest

    NOTE: Using pytest_collection_finish instead of pytest_collection_modifyitems allows us to capture only the
    tests that pytest has selection for run (eg: with the use of -k as an argument).
    """
    for item in session.items:
        test_id = _get_test_id_from_item(item)
        suite_id = test_id.parent_id
        module_id = suite_id.parent_id

        # TODO: don't rediscover modules and suites if already discovered
        InternalTestModule.discover(module_id, _get_module_path_from_item(item))
        InternalTestSuite.discover(suite_id)

        item_path = Path(item.path if hasattr(item, "path") else item.fspath).absolute()
        workspace_path = InternalTestSession.get_workspace_path()
        if workspace_path:
            try:
                repo_relative_path = item_path.relative_to(workspace_path)
            except ValueError:
                repo_relative_path = item_path
        else:
            repo_relative_path = item_path

        item_codeowners = InternalTestSession.get_path_codeowners(repo_relative_path) if repo_relative_path else None

        source_file_info = _get_source_file_info(item, item_path)

        InternalTest.discover(test_id, codeowners=item_codeowners, source_file_info=source_file_info)

        markers = [marker.kwargs for marker in item.iter_markers(name="dd_tags")]
        for tags in markers:
            InternalTest.set_tags(test_id, tags)

        # Pytest markers do not allow us to determine if the test or the suite was marked as unskippable, but any
        # test marked unskippable in a suite makes the entire suite unskippable (since we are in suite skipping
        # mode)
        if InternalTestSession.is_test_skipping_enabled() and _is_test_unskippable(item):
            InternalTest.mark_itr_unskippable(test_id)
            InternalTestSuite.mark_itr_unskippable(suite_id)

    # NOTE: EFD enablement status is already specified during service enablement
    if InternalTestSession.efd_enabled() and InternalTestSession.efd_is_faulty_session():
        log.warning("Early Flake Detection disabled: too many new tests detected")


def pytest_collection_finish(session) -> None:
    if not is_test_visibility_enabled():
        return

    try:
        return _pytest_collection_finish(session)
    except Exception:  # noqa: E722
        log.debug("encountered error during collection finish, disabling Datadog CI Visibility", exc_info=True)
        _disable_ci_visibility()


def _pytest_runtest_protocol_pre_yield(item) -> t.Optional[ModuleCodeCollector.CollectInContext]:
    test_id = _get_test_id_from_item(item)
    suite_id = test_id.parent_id
    module_id = suite_id.parent_id

    # TODO: don't re-start modules if already started
    InternalTestModule.start(module_id)
    InternalTestSuite.start(suite_id)

    # DEV: pytest's fixtures resolution may change parameters between collection finish and test run
    parameters = _get_test_parameters_json(item)
    if parameters is not None:
        InternalTest.set_parameters(test_id, parameters)

    InternalTest.start(test_id)

    _handle_quarantine(item, test_id)
    _handle_itr_should_skip(item, test_id)

    item_will_skip = _pytest_marked_to_skip(item) or InternalTest.was_skipped_by_itr(test_id)

    collect_test_coverage = InternalTestSession.should_collect_coverage() and not item_will_skip

    if collect_test_coverage:
        return _start_collecting_coverage()

    return None


def _pytest_runtest_protocol_post_yield(item, nextitem, coverage_collector):
    test_id = _get_test_id_from_item(item)
    suite_id = test_id.parent_id
    module_id = suite_id.parent_id

    if coverage_collector is not None:
        _handle_collected_coverage(test_id, coverage_collector)

    # We rely on the CI Visibility service to prevent finishing items that have been discovered and have unfinished
    # children, but as an optimization:
    # - we know we don't need to finish the suite if the next item is in the same suite
    # - we know we don't need to finish the module if the next item is in the same module
    # - we trust that the next item is in the same module if it is in the same suite
    next_test_id = _get_test_id_from_item(nextitem) if nextitem else None
    if next_test_id is None or next_test_id.parent_id != suite_id:
        if InternalTestSuite.is_itr_skippable(suite_id) and not InternalTestSuite.was_forced_run(suite_id):
            InternalTestSuite.mark_itr_skipped(suite_id)
        else:
            _handle_coverage_dependencies(suite_id)
            InternalTestSuite.finish(suite_id)
        if nextitem is None or (next_test_id is not None and next_test_id.parent_id.parent_id != module_id):
            InternalTestModule.finish(module_id)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_protocol(item, nextitem) -> None:
    """Discovers tests, and starts tests, suites, and modules, then handles coverage data collection"""
    if not is_test_visibility_enabled():
        yield
        return

    try:
        coverage_collector = _pytest_runtest_protocol_pre_yield(item)
    except Exception:  # noqa: E722
        log.debug("encountered error during pre-test", exc_info=True)

    # Yield control back to pytest to run the test
    yield

    try:
        return _pytest_runtest_protocol_post_yield(item, nextitem, coverage_collector)
    except Exception:  # noqa: E722
        log.debug("encountered error during post-test", exc_info=True)
        return


def _process_result(item, call, result) -> _TestOutcome:
    test_id = _get_test_id_from_item(item)

    has_exception = call.excinfo is not None

    # In cases where a test was marked as XFAIL, the reason is only available during when call.when == "call", so we
    # add it as a tag immediately:
    if getattr(result, "wasxfail", None):
        InternalTest.set_tag(test_id, XFAIL_REASON, result.wasxfail)
    elif "xfail" in getattr(result, "keywords", []) and getattr(result, "longrepr", None):
        InternalTest.set_tag(test_id, XFAIL_REASON, result.longrepr)

    # Only capture result if:
    # - there is an exception
    # - the test failed
    # - the test passed with xfail
    # - we are tearing down the test
    # DEV NOTE: some skip scenarios (eg: skipif) have an exception during setup
    if call.when != "teardown" and not (has_exception or result.failed):
        return _TestOutcome()

    xfail = hasattr(result, "wasxfail") or "xfail" in result.keywords
    xfail_reason_tag = InternalTest.get_tag(test_id, XFAIL_REASON) if xfail else None
    has_skip_keyword = any(x in result.keywords for x in ["skip", "skipif", "skipped"])

    # If run with --runxfail flag, tests behave as if they were not marked with xfail,
    # that's why no XFAIL_REASON or test.RESULT tags will be added.
    if result.skipped:
        if InternalTest.was_skipped_by_itr(test_id):
            # Items that were skipped by ITR already have their status and reason set
            return _TestOutcome()

        if xfail and not has_skip_keyword:
            # XFail tests that fail are recorded skipped by pytest, should be passed instead
            if not item.config.option.runxfail:
                InternalTest.set_tag(test_id, test.RESULT, test.Status.XFAIL.value)
                if xfail_reason_tag is None:
                    InternalTest.set_tag(test_id, XFAIL_REASON, getattr(result, "wasxfail", "XFail"))
                return _TestOutcome(TestStatus.PASS)

        return _TestOutcome(TestStatus.SKIP, _extract_reason(call))

    if result.passed:
        if xfail and not has_skip_keyword and not item.config.option.runxfail:
            # XPass (strict=False) are recorded passed by pytest
            if xfail_reason_tag is None:
                InternalTest.set_tag(test_id, XFAIL_REASON, "XFail")
            InternalTest.set_tag(test_id, test.RESULT, test.Status.XPASS.value)

        return _TestOutcome(TestStatus.PASS)

    if xfail and not has_skip_keyword and not item.config.option.runxfail:
        # XPass (strict=True) are recorded failed by pytest, longrepr contains reason
        if xfail_reason_tag is None:
            InternalTest.set_tag(test_id, XFAIL_REASON, getattr(result, "longrepr", "XFail"))
        InternalTest.set_tag(test_id, test.RESULT, test.Status.XPASS.value)
        return _TestOutcome(TestStatus.FAIL)

    # NOTE: for ATR and EFD purposes, we need to know if the test failed during setup or teardown.
    if call.when == "setup" and result.failed:
        InternalTest.stash_set(test_id, "setup_failed", True)
    elif call.when == "teardown" and result.failed:
        InternalTest.stash_set(test_id, "teardown_failed", True)

    exc_info = TestExcInfo(call.excinfo.type, call.excinfo.value, call.excinfo.tb) if call.excinfo else None

    return _TestOutcome(status=TestStatus.FAIL, exc_info=exc_info)


def _pytest_runtest_makereport(item: pytest.Item, call: pytest_CallInfo, outcome: pytest_TestReport) -> None:
    # When ATR or EFD retries are active, we do not want makereport to generate results
    if _pytest_version_supports_retries() and get_retry_num(item.nodeid) is not None:
        return

    original_result = outcome.get_result()

    test_id = _get_test_id_from_item(item)

    is_quarantined = InternalTest.is_quarantined_test(test_id)

    test_outcome = _process_result(item, call, original_result)

    # A None value for test_outcome.status implies the test has not finished yet
    # Only continue to finishing the test if the test has finished, or if tearing down the test
    if test_outcome.status is None and call.when != "teardown":
        return

    # Support for pytest-benchmark plugin
    if item.config.pluginmanager.hasplugin("benchmark"):
        _set_benchmark_data_from_item(item)

    # Record a result if we haven't already recorded it:
    if not InternalTest.is_finished(test_id):
        InternalTest.finish(test_id, test_outcome.status, test_outcome.skip_reason, test_outcome.exc_info)

    if original_result.failed and is_quarantined:
        # Ensure test doesn't count as failed for pytest's exit status logic
        # (see <https://github.com/pytest-dev/pytest/blob/8.3.x/src/_pytest/main.py#L654>).
        original_result.outcome = OUTCOME_QUARANTINED

    # ATR and EFD retry tests only if their teardown succeeded to ensure the best chance the retry will succeed
    # NOTE: this mutates the original result's outcome
    if InternalTest.stash_get(test_id, "setup_failed") or InternalTest.stash_get(test_id, "teardown_failed"):
        log.debug("Test %s failed during setup or teardown, skipping retries", test_id)
        return
    if InternalTestSession.efd_enabled() and InternalTest.efd_should_retry(test_id):
        return efd_handle_retries(test_id, item, call.when, original_result, test_outcome)
    if InternalTestSession.atr_is_enabled() and InternalTest.atr_should_retry(test_id):
        return atr_handle_retries(test_id, item, call.when, original_result, test_outcome, is_quarantined)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest_CallInfo) -> None:
    """Store outcome for tracing."""
    outcome: pytest_TestReport
    outcome = yield

    if not is_test_visibility_enabled():
        return

    try:
        return _pytest_runtest_makereport(item, call, outcome)
    except Exception:  # noqa: E722
        log.debug("encountered error during makereport", exc_info=True)


def _pytest_terminal_summary_pre_yield(terminalreporter) -> int:
    # Before yield gives us a chance to show failure reports, but they have to be in terminalreporter.stats["failed"] to
    # be shown. That, however, would make them count towards the final summary, so we add them temporarily, then restore
    # terminalreporter.stats["failed"] to its original size after the yield.
    failed_reports_initial_size = len(terminalreporter.stats.get(PYTEST_STATUS.FAILED, []))

    if _pytest_version_supports_efd() and InternalTestSession.efd_enabled():
        for failed_report in efd_get_failed_reports(terminalreporter):
            failed_report.outcome = PYTEST_STATUS.FAILED
            terminalreporter.stats.setdefault("failed", []).append(failed_report)

    if _pytest_version_supports_atr() and InternalTestSession.atr_is_enabled():
        for failed_report in atr_get_failed_reports(terminalreporter):
            failed_report.outcome = PYTEST_STATUS.FAILED
            terminalreporter.stats.setdefault("failed", []).append(failed_report)

    return failed_reports_initial_size


def _pytest_terminal_summary_post_yield(terminalreporter, failed_reports_initial_size: t.Optional[int] = None):
    # After yield gives us a chance to:
    # - print our flaky test status summary
    # - modify the total counts

    # Restore terminalreporter.stats["failed"] to its original size so the final summary remains correct
    if failed_reports_initial_size is None:
        log.debug("Could not get initial failed report size, not restoring failed reports")
    elif failed_reports_initial_size == 0:
        terminalreporter.stats.pop("failed", None)
    else:
        terminalreporter.stats[PYTEST_STATUS.FAILED] = terminalreporter.stats[PYTEST_STATUS.FAILED][
            :failed_reports_initial_size
        ]

    # IMPORTANT: terminal summary functions mutate terminalreporter.stats
    if _pytest_version_supports_efd() and InternalTestSession.efd_enabled():
        efd_pytest_terminal_summary_post_yield(terminalreporter)

    if _pytest_version_supports_atr() and InternalTestSession.atr_is_enabled():
        atr_pytest_terminal_summary_post_yield(terminalreporter)

    quarantine_pytest_terminal_summary_post_yield(terminalreporter)

    return


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Report flaky or failed tests"""
    try:
        from ddtrace.appsec._iast._pytest_plugin import print_iast_report

        print_iast_report(terminalreporter)
    except Exception:  # noqa: E722
        log.debug("Encountered error during code security summary", exc_info=True)

    if not is_test_visibility_enabled():
        yield
        return

    failed_reports_initial_size = None
    try:
        failed_reports_initial_size = _pytest_terminal_summary_pre_yield(terminalreporter)
    except Exception:  # noqa: E722
        log.debug("Encountered error during terminal summary pre-yield", exc_info=True)

    yield

    try:
        _pytest_terminal_summary_post_yield(terminalreporter, failed_reports_initial_size)
    except Exception:  # noqa: E722
        log.debug("Encountered error during terminal summary post-yield", exc_info=True)

    return


def _pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not is_test_visibility_enabled():
        return

    if InternalTestSession.efd_enabled() and InternalTestSession.efd_has_failed_tests():
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if InternalTestSession.atr_is_enabled() and InternalTestSession.atr_has_failed_tests():
        session.exitstatus = pytest.ExitCode.TESTS_FAILED

    invoked_by_coverage_run_status = _is_coverage_invoked_by_coverage_run()
    pytest_cov_status = _is_pytest_cov_enabled(session.config)
    if _is_coverage_patched() and (pytest_cov_status or invoked_by_coverage_run_status):
        if invoked_by_coverage_run_status and not pytest_cov_status:
            run_coverage_report()

        lines_pct_value = _coverage_data.get(PCT_COVERED_KEY, None)
        if not isinstance(lines_pct_value, float):
            log.warning("Tried to add total covered percentage to session span but the format was unexpected")
        else:
            InternalTestSession.set_covered_lines_pct(lines_pct_value)

    if ModuleCodeCollector.is_installed():
        ModuleCodeCollector.uninstall()

    InternalTestSession.finish(force_finish_children=True)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not is_test_visibility_enabled():
        return

    try:
        _pytest_sessionfinish(session, exitstatus)
    except Exception:  # noqa: E722
        log.debug("encountered error during session finish", exc_info=True)


def pytest_report_teststatus(
    report: pytest_TestReport,
) -> _pytest_report_teststatus_return_type:
    if not is_test_visibility_enabled():
        return

    if _pytest_version_supports_atr() and InternalTestSession.atr_is_enabled():
        test_status = atr_get_teststatus(report) or quarantine_atr_get_teststatus(report)
        if test_status is not None:
            return test_status

    if _pytest_version_supports_efd() and InternalTestSession.efd_enabled():
        test_status = efd_get_teststatus(report)
        if test_status is not None:
            return test_status

    user_properties = getattr(report, "user_properties", [])
    is_quarantined = (USER_PROPERTY_QUARANTINED, True) in user_properties
    if is_quarantined:
        if report.when == "teardown":
            return (OUTCOME_QUARANTINED, "q", ("QUARANTINED", {"blue": True}))
        else:
            # Don't show anything for setup and call of quarantined tests, regardless of
            # whether there were errors or not.
            return ("", "", "")


@pytest.hookimpl(trylast=True)
def pytest_ddtrace_get_item_module_name(item):
    names = _get_names_from_item(item)
    return names.module


@pytest.hookimpl(trylast=True)
def pytest_ddtrace_get_item_suite_name(item):
    """
    Extract suite name from a `pytest.Item` instance.
    If the module path doesn't exist, the suite path will be reported in full.
    """
    names = _get_names_from_item(item)
    return names.suite


@pytest.hookimpl(trylast=True)
def pytest_ddtrace_get_item_test_name(item):
    """Extract name from item, prepending class if desired"""
    names = _get_names_from_item(item)
    return names.test
