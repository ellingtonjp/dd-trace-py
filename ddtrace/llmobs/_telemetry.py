import time
from typing import Optional

from ddtrace.internal.telemetry import telemetry_writer
from ddtrace.internal.telemetry.constants import TELEMETRY_NAMESPACE
from ddtrace.llmobs._constants import DECORATOR
from ddtrace.llmobs._constants import INTEGRATION
from ddtrace.llmobs._constants import PARENT_ID_KEY
from ddtrace.llmobs._constants import ROOT_PARENT_ID
from ddtrace.llmobs._constants import SESSION_ID
from ddtrace.llmobs._constants import SPAN_KIND
from ddtrace.llmobs._writer import LLMObsSpanEvent
from ddtrace.trace import Span


class LLMObsTelemetryMetrics:
    INIT_TIME = "init_time"
    ENABLED = "product_enabled"
    RAW_SPAN_SIZE = "span.raw_size"
    SPAN_SIZE = "span.size"
    SPAN_STARTED = "span.start"
    SPAN_FINISHED = "span.finished"


def _find_integration_from_tags(tags):
    integration_tag = next((tag for tag in tags if tag.startswith("integration:")), None)
    if not integration_tag:
        return None
    return integration_tag.split("integration:")[-1]


def _get_tags_from_span_event(event: LLMObsSpanEvent):
    span_kind = event.get("meta", {}).get("span.kind", "")
    integration = _find_integration_from_tags(event.get("tags", []))
    autoinstrumented = integration is not None
    error = event.get("status") == "error"
    return [
        ("span_kind", span_kind),
        ("autoinstrumented", str(int(autoinstrumented))),
        ("error", str(int(error))),
        ("integration", integration if integration else "N/A"),
    ]


def record_llmobs_enabled(error: Optional[str], agentless_enabled: bool, site: str, start_ns: int, auto: bool):
    tags = [
        ("agentless", str(int(agentless_enabled))),
        ("site", site),
        ("auto", str(int(auto))),
        ("error", "1" if error else "0"),
    ]
    if error:
        tags.append(("error_type", error))
    init_time_ms = (time.time_ns() - start_ns) / 1e6
    telemetry_writer.add_distribution_metric(
        namespace=TELEMETRY_NAMESPACE.MLOBS, name=LLMObsTelemetryMetrics.INIT_TIME, value=init_time_ms, tags=tuple(tags)
    )
    telemetry_writer.add_count_metric(
        namespace=TELEMETRY_NAMESPACE.MLOBS, name=LLMObsTelemetryMetrics.ENABLED, value=1, tags=tuple(tags)
    )


def record_span_started():
    telemetry_writer.add_count_metric(
        namespace=TELEMETRY_NAMESPACE.MLOBS, name=LLMObsTelemetryMetrics.SPAN_STARTED, value=1
    )


def record_span_created(span: Span):
    is_root_span = span._get_ctx_item(PARENT_ID_KEY) != ROOT_PARENT_ID
    has_session_id = span._get_ctx_item(SESSION_ID) is not None
    integration = span._get_ctx_item(INTEGRATION)
    autoinstrumented = integration is not None
    decorator = span._get_ctx_item(DECORATOR) is True
    span_kind = span._get_ctx_item(SPAN_KIND)
    model_provider = span._get_ctx_item("model_provider")

    tags = [
        ("autoinstrumented", str(int(autoinstrumented))),
        ("has_session_id", str(int(has_session_id))),
        ("is_root_span", str(int(is_root_span))),
        ("span_kind", span_kind or "N/A"),
        ("integration", integration or "N/A"),
        ("error", str(span.error)),
    ]
    if not autoinstrumented:
        tags.append(("decorator", str(int(decorator))))
    if model_provider:
        tags.append(("model_provider", model_provider))
    telemetry_writer.add_count_metric(
        namespace=TELEMETRY_NAMESPACE.MLOBS, name=LLMObsTelemetryMetrics.SPAN_FINISHED, value=1, tags=tuple(tags)
    )


def record_span_event_raw_size(event: LLMObsSpanEvent, raw_event_size: int):
    telemetry_writer.add_distribution_metric(
        namespace=TELEMETRY_NAMESPACE.MLOBS,
        name=LLMObsTelemetryMetrics.RAW_SPAN_SIZE,
        value=raw_event_size,
        tags=tuple(_get_tags_from_span_event(event)),
    )


def record_span_event_size(event: LLMObsSpanEvent, event_size: int, truncated: bool):
    tags = _get_tags_from_span_event(event)
    tags.append(("truncated", str(int(truncated))))
    telemetry_writer.add_distribution_metric(
        namespace=TELEMETRY_NAMESPACE.MLOBS, name=LLMObsTelemetryMetrics.SPAN_SIZE, value=event_size, tags=tuple(tags)
    )
