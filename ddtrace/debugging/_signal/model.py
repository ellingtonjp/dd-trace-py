import abc
from collections import ChainMap
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from functools import singledispatch
import threading
from threading import Thread
import time
from types import FrameType
from typing import Any
from typing import ClassVar
from typing import Dict
from typing import List
from typing import Mapping
from typing import Optional
from typing import Union
from typing import cast
from uuid import uuid4

from ddtrace.debugging._expressions import DDExpressionEvaluationError
from ddtrace.debugging._probe.model import Probe
from ddtrace.debugging._probe.model import ProbeConditionMixin
from ddtrace.debugging._probe.model import ProbeEvalTiming
from ddtrace.debugging._probe.model import RateLimitMixin
from ddtrace.debugging._probe.model import TimingMixin
from ddtrace.debugging._safety import get_args
from ddtrace.internal.compat import ExcInfoType
from ddtrace.internal.metrics import Metrics
from ddtrace.internal.rate_limiter import BudgetRateLimiterWithJitter as RateLimiter
from ddtrace.internal.rate_limiter import RateLimitExceeded
from ddtrace.trace import Context
from ddtrace.trace import Span


@dataclass
class EvaluationError:
    expr: str
    message: str


class SignalState(str, Enum):
    NONE = "NONE"
    SKIP_COND = "SKIP_COND"
    SKIP_COND_ERROR = "SKIP_COND_ERROR"
    SKIP_RATE = "SKIP_RATE"
    COND_ERROR = "COND_ERROR"
    DONE = "DONE"


@dataclass
class Signal(abc.ABC):
    """Debugger signal base class.

    Used to model the data carried by the signal emitted by a probe when it is
    triggered.

    Probes with an evaluation timing will trigger the following logic:

    - If the timing is on ENTRY, the probe will trigger on entry and on exit,
      but the condition and rate limiter will only be evaluated on entry.

    - If the timing is on EXIT, the probe will trigger on exit only, and the
      condition and rate limiter will be evaluated on exit.

    - IF the timing is DEFAULT, the probe will trigger on the default timing for
      the signal.

    - If the probe has no timing, it defaults to the ENTRY timing.
    """

    __default_timing__: ClassVar[ProbeEvalTiming] = ProbeEvalTiming.EXIT

    probe: Probe
    frame: FrameType
    thread: Thread
    trace_context: Optional[Union[Span, Context]] = None
    state: str = SignalState.NONE
    errors: List[EvaluationError] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    uuid: str = field(default_factory=lambda: str(uuid4()), init=False)

    def __post_init__(self):
        probe = self.probe
        if isinstance(probe, TimingMixin):
            eval_at = probe.evaluate_at
            self._timing = self.__default_timing__ if eval_at is ProbeEvalTiming.DEFAULT else eval_at
        else:
            self._timing = ProbeEvalTiming.ENTRY

    def _eval_condition(self, scope: Mapping[str, Any]) -> bool:
        """Evaluate the probe condition against the collected frame."""
        probe = self.probe
        if not isinstance(probe, ProbeConditionMixin):
            # The probe has no condition, so it should always trigger.
            return True

        condition = probe.condition
        if condition is None:
            return True

        try:
            if bool(condition.eval(scope)):
                return True
        except DDExpressionEvaluationError as e:
            self.errors.append(EvaluationError(expr=e.dsl, message=e.error))
            self.state = (
                SignalState.SKIP_COND_ERROR
                if probe.condition_error_limiter.limit() is RateLimitExceeded
                else SignalState.COND_ERROR
            )
        else:
            self.state = SignalState.SKIP_COND

        return False

    def _rate_limit_exceeded(self) -> bool:
        """Evaluate the probe rate limiter."""
        probe = self.probe
        if not isinstance(probe, RateLimitMixin):
            # We don't have a rate limiter, so no rate was exceeded.
            return False

        exceeded = probe.limiter.limit() is RateLimitExceeded
        if exceeded:
            self.state = SignalState.SKIP_RATE

        return exceeded

    @property
    def args(self):
        return dict(get_args(self.frame))

    @abc.abstractmethod
    def enter(self, scope: Mapping[str, Any]) -> None:
        pass

    @abc.abstractmethod
    def exit(self, retval: Any, exc_info: ExcInfoType, duration: int, scope: Mapping[str, Any]) -> None:
        pass

    @abc.abstractmethod
    def line(self, scope: Mapping[str, Any]) -> None:
        pass

    def do_enter(self) -> None:
        if self._timing is not ProbeEvalTiming.ENTRY:
            return

        scope = ChainMap(self.args, self.frame.f_globals)
        if not self._eval_condition(scope):
            return

        if self._rate_limit_exceeded():
            return

        self.enter(scope)

    def do_exit(self, retval: Any, exc_info: ExcInfoType, duration: int) -> None:
        if self.state is not SignalState.NONE:
            # The signal has already been handled and move to a final state
            return

        frame = self.frame
        extra: Dict[str, Any] = {"@duration": duration / 1e6}  # milliseconds

        exc = exc_info[1]
        if exc is not None:
            extra["@exception"] = exc
        else:
            extra["@return"] = retval

        scope = ChainMap(extra, frame.f_locals, frame.f_globals)

        if self._timing is ProbeEvalTiming.EXIT:
            # We only evaluate the condition and the rate limiter on exit if it
            # is a probe with timing on exit
            if not self._eval_condition(scope):
                return

            if self._rate_limit_exceeded():
                return

        self.exit(retval, cast(ExcInfoType, exc_info), duration or 0, scope)

        self.state = SignalState.DONE

    def do_line(self, global_limiter: Optional[RateLimiter] = None) -> None:
        frame = self.frame
        scope = ChainMap(frame.f_locals, frame.f_globals)

        if not self._eval_condition(scope):
            return

        if global_limiter is not None and global_limiter.limit() is RateLimitExceeded:
            self.state = SignalState.SKIP_RATE
            return

        if self._rate_limit_exceeded():
            return

        self.line(scope)

        self.state = SignalState.DONE

    @staticmethod
    def from_probe(
        probe: Probe, frame: FrameType, thread: Thread, trace_context: Optional[Any], meter: Metrics.Meter
    ) -> "Signal":
        return probe_to_signal(probe, frame, thread, trace_context, meter)


@singledispatch
def probe_to_signal(
    probe: Probe,
    frame: FrameType,
    thread: threading.Thread,
    trace_context: Optional[Any],
    meter: Metrics.Meter,
) -> Signal:
    raise TypeError(f"Unsupported probe type: {type(probe)}")
