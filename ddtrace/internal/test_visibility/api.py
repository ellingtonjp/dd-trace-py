from pathlib import Path
import typing as t
from typing import NamedTuple

from ddtrace import Span
from ddtrace.ext.test_visibility import api as ext_api
from ddtrace.ext.test_visibility._test_visibility_base import TestSessionId
from ddtrace.ext.test_visibility._utils import _catch_and_log_exceptions
from ddtrace.ext.test_visibility._utils import _is_item_finished
from ddtrace.ext.test_visibility.api import TestExcInfo
from ddtrace.ext.test_visibility.api import TestStatus
from ddtrace.internal import core
from ddtrace.internal.codeowners import Codeowners as _Codeowners
from ddtrace.internal.logger import get_logger
from ddtrace.internal.test_visibility._efd_mixins import EFDSessionMixin
from ddtrace.internal.test_visibility._efd_mixins import EFDTestMixin
from ddtrace.internal.test_visibility._internal_item_ids import InternalTestId
from ddtrace.internal.test_visibility._itr_mixins import ITRMixin
from ddtrace.internal.test_visibility._utils import _get_item_span


log = get_logger(__name__)


class InternalTestBase(ext_api.TestBase):
    @staticmethod
    @_catch_and_log_exceptions
    def get_span(item_id: t.Union[ext_api.TestVisibilityItemId, InternalTestId]) -> Span:
        return _get_item_span(item_id)


class InternalTestSession(ext_api.TestSession, EFDSessionMixin):
    @staticmethod
    def get_span() -> Span:
        return _get_item_span(TestSessionId())

    @staticmethod
    def is_finished() -> bool:
        return _is_item_finished(TestSessionId())

    @staticmethod
    @_catch_and_log_exceptions
    def get_codeowners() -> t.Optional[_Codeowners]:
        log.debug("Getting codeowners object")

        codeowners: t.Optional[_Codeowners] = core.dispatch_with_results(
            "test_visibility.session.get_codeowners",
        ).codeowners.value
        return codeowners

    @staticmethod
    @_catch_and_log_exceptions
    def get_workspace_path() -> Path:
        log.debug("Getting session workspace path")

        workspace_path: Path = core.dispatch_with_results(
            "test_visibility.session.get_workspace_path"
        ).workspace_path.value
        return workspace_path

    @staticmethod
    @_catch_and_log_exceptions
    def should_collect_coverage() -> bool:
        log.debug("Checking if coverage should be collected for session")

        _should_collect_coverage = bool(
            core.dispatch_with_results("test_visibility.session.should_collect_coverage").should_collect_coverage.value
        )
        log.debug("Coverage should be collected: %s", _should_collect_coverage)

        return _should_collect_coverage

    @staticmethod
    @_catch_and_log_exceptions
    def is_test_skipping_enabled() -> bool:
        log.debug("Checking if test skipping is enabled")

        _is_test_skipping_enabled = bool(
            core.dispatch_with_results(
                "test_visibility.session.is_test_skipping_enabled"
            ).is_test_skipping_enabled.value
        )
        log.debug("Test skipping is enabled: %s", _is_test_skipping_enabled)

        return _is_test_skipping_enabled

    @staticmethod
    @_catch_and_log_exceptions
    def set_covered_lines_pct(coverage_pct: float):
        log.debug("Setting covered lines percentage for session to %s", coverage_pct)

        core.dispatch("test_visibility.session.set_covered_lines_pct", (coverage_pct,))

    @staticmethod
    @_catch_and_log_exceptions
    def get_path_codeowners(path: Path) -> t.Optional[t.List[str]]:
        log.debug("Getting codeowners object for path %s", path)

        path_codeowners: t.Optional[t.List[str]] = core.dispatch_with_results(
            "test_visibility.session.get_path_codeowners", (path,)
        ).path_codeowners.value
        return path_codeowners


class InternalTestModule(ext_api.TestModule, InternalTestBase):
    pass


class InternalTestSuite(ext_api.TestSuite, InternalTestBase, ITRMixin):
    pass


class InternalTest(ext_api.Test, InternalTestBase, ITRMixin, EFDTestMixin):
    class FinishArgs(NamedTuple):
        """InternalTest allows finishing with an overridden finish time (for EFD and other retry purposes)"""

        test_id: InternalTestId
        status: TestStatus
        skip_reason: t.Optional[str] = None
        exc_info: t.Optional[TestExcInfo] = None
        override_finish_time: t.Optional[float] = None

    @staticmethod
    @_catch_and_log_exceptions
    def finish(
        item_id: InternalTestId,
        status: ext_api.TestStatus,
        reason: t.Optional[str] = None,
        exc_info: t.Optional[ext_api.TestExcInfo] = None,
        override_finish_time: t.Optional[float] = None,
    ):
        log.debug("Finishing test with status: %s, reason: %s", status, reason)
        core.dispatch(
            "test_visibility.test.finish",
            (InternalTest.FinishArgs(item_id, status, reason, exc_info, override_finish_time),),
        )

    @staticmethod
    @_catch_and_log_exceptions
    def is_new_test(item_id: InternalTestId) -> bool:
        log.debug("Checking if test %s is new", item_id)
        is_new = bool(core.dispatch_with_results("test_visibility.test.is_new", (item_id,)).is_new.value)
        log.debug("Test %s is new: %s", item_id, is_new)
        return is_new