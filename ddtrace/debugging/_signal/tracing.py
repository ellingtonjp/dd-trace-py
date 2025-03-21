from dataclasses import dataclass
from dataclasses import field
import typing as t

import ddtrace
from ddtrace.constants import _ORIGIN_KEY
from ddtrace.debugging._expressions import DDExpressionEvaluationError
from ddtrace.debugging._probe.model import Probe
from ddtrace.debugging._probe.model import SpanDecorationFunctionProbe
from ddtrace.debugging._probe.model import SpanDecorationLineProbe
from ddtrace.debugging._probe.model import SpanDecorationMixin
from ddtrace.debugging._probe.model import SpanDecorationTargetSpan
from ddtrace.debugging._probe.model import SpanFunctionProbe
from ddtrace.debugging._signal.log import LogSignal
from ddtrace.debugging._signal.model import EvaluationError
from ddtrace.debugging._signal.model import Signal
from ddtrace.debugging._signal.model import probe_to_signal
from ddtrace.debugging._signal.utils import serialize
from ddtrace.internal.compat import ExcInfoType
from ddtrace.internal.logger import get_logger
from ddtrace.internal.safety import _isinstance
from ddtrace.trace import Span


log = get_logger(__name__)

SPAN_NAME = "dd.dynamic.span"
PROBE_ID_TAG_NAME = "debugger.probeid"


@dataclass
class DynamicSpan(Signal):
    """Dynamically created span"""

    _span_cm: t.Optional[Span] = field(init=False, default=None)

    def __post_init__(self) -> None:
        super().__post_init__()

        self._span_cm = None

    def enter(self, scope: t.Mapping[str, t.Any]) -> None:
        probe = t.cast(SpanFunctionProbe, self.probe)

        self._span_cm = ddtrace.tracer.trace(
            SPAN_NAME,
            service=None,  # Currently unused
            resource=probe.func_qname,
            span_type=None,  # Currently unused
        )
        span = self._span_cm.__enter__()

        span.set_tags(probe.tags)  # type: ignore[arg-type]
        span.set_tag_str(PROBE_ID_TAG_NAME, probe.probe_id)
        span.set_tag_str(_ORIGIN_KEY, "di")

    def exit(self, retval: t.Any, exc_info: ExcInfoType, duration: float, scope: t.Mapping[str, t.Any]) -> None:
        if self._span_cm is not None:
            # Condition evaluated to true so we created a span. Finish it.
            self._span_cm.__exit__(*exc_info)

    def line(self, scope):
        raise NotImplementedError("Dynamic line spans are not supported in Python")


@dataclass
class SpanDecoration(LogSignal):
    """Decorate a span."""

    def _decorate_span(self, scope: t.Mapping[str, t.Any]) -> None:
        probe = t.cast(SpanDecorationMixin, self.probe)

        if probe.target_span == SpanDecorationTargetSpan.ACTIVE:
            span = ddtrace.tracer.current_span()
        elif probe.target_span == SpanDecorationTargetSpan.ROOT:
            span = ddtrace.tracer.current_root_span()
        else:
            log.error("Invalid target span for span decoration: %s", probe.target_span)
            return

        if span is not None:
            log.debug("Decorating span %r according to span decoration probe %r", span, probe)
            for d in probe.decorations:
                try:
                    if not (d.when is None or d.when(scope)):
                        continue
                except DDExpressionEvaluationError as e:
                    self.errors.append(
                        EvaluationError(expr=e.dsl, message="Failed to evaluate condition: %s" % e.error)
                    )
                    continue
                for tag in d.tags:
                    try:
                        tag_value = tag.value.render(scope, serialize)
                    except DDExpressionEvaluationError as e:
                        span.set_tag_str(
                            "_dd.di.%s.evaluation_error" % tag.name, ", ".join([serialize(v) for v in e.args])
                        )
                    else:
                        span.set_tag_str(tag.name, tag_value if _isinstance(tag_value, str) else serialize(tag_value))
                        span.set_tag_str("_dd.di.%s.probe_id" % tag.name, t.cast(Probe, probe).probe_id)

    def enter(self, scope: t.Mapping[str, t.Any]) -> None:
        self._decorate_span(scope)

    def exit(self, retval: t.Any, exc_info: ExcInfoType, duration: float, scope: t.Mapping[str, t.Any]) -> None:
        self._decorate_span(scope)

    def line(self, scope: t.Mapping[str, t.Any]):
        self._decorate_span(scope)

    @property
    def message(self):
        return f"Condition evaluation errors for probe {self.probe.probe_id}" if self.errors else None

    def has_message(self) -> bool:
        return bool(self.errors)


@probe_to_signal.register
def _(probe: SpanFunctionProbe, frame, thread, trace_context, meter):
    return DynamicSpan(probe=probe, frame=frame, thread=thread, trace_context=trace_context)


@probe_to_signal.register
def _(probe: SpanDecorationFunctionProbe, frame, thread, trace_context, meter):
    return SpanDecoration(probe=probe, frame=frame, thread=thread)


@probe_to_signal.register
def _(probe: SpanDecorationLineProbe, frame, thread, trace_context, meter):
    return SpanDecoration(probe=probe, frame=frame, thread=thread)
