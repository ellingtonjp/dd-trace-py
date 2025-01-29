from ddtrace.ext.test_visibility._test_visibility_base import TestVisibilityItemId
from ddtrace.internal import core
from ddtrace.internal.logger import get_logger
from ddtrace.trace import Span


log = get_logger(__name__)


def _get_item_span(item_id: TestVisibilityItemId) -> Span:
    log.debug("Getting span for item %s", item_id)
    span: Span = core.dispatch_with_results("test_visibility.item.get_span", (item_id,)).span.value
    return span
