from __future__ import division

from dataclasses import dataclass
from dataclasses import field
import random
import threading
import time
from typing import Any  # noqa:F401
from typing import Callable  # noqa:F401
from typing import Optional  # noqa:F401


class RateLimiter(object):
    """
    A token bucket rate limiter implementation
    """

    __slots__ = (
        "_lock",
        "current_window_ns",
        "time_window",
        "last_update_ns",
        "max_tokens",
        "prev_window_rate",
        "rate_limit",
        "tokens",
        "tokens_allowed",
        "tokens_total",
    )

    def __init__(self, rate_limit: int, time_window: float = 1e9):
        """
        Constructor for RateLimiter

        :param rate_limit: The rate limit to apply for number of requests per second.
            rate limit > 0 max number of requests to allow per second,
            rate limit == 0 to disallow all requests,
            rate limit < 0 to allow all requests
        :type rate_limit: :obj:`int`
        :param time_window: The time window where the rate limit applies in nanoseconds. default value is 1 second.
        :type time_window: :obj:`float`
        """
        self.rate_limit = rate_limit
        self.time_window = time_window
        self.tokens = rate_limit  # type: float
        self.max_tokens = rate_limit

        self.last_update_ns = time.monotonic_ns()

        self.current_window_ns = 0  # type: float
        self.tokens_allowed = 0
        self.tokens_total = 0
        self.prev_window_rate = None  # type: Optional[float]

        self._lock = threading.Lock()

    def is_allowed(self) -> bool:
        """
        Check whether the current request is allowed or not

        This method will also reduce the number of available tokens by 1

        :returns: Whether the current request is allowed or not
        :rtype: :obj:`bool`
        """
        # rate limits are tested and mocked in pytest so we need to compute the timestamp here
        # (or move the unit tests to rust)
        timestamp_ns = time.monotonic_ns()
        allowed = self._is_allowed(timestamp_ns)
        # Update counts used to determine effective rate
        self._update_rate_counts(allowed, timestamp_ns)
        return allowed

    def _update_rate_counts(self, allowed: bool, timestamp_ns: int) -> None:
        # No tokens have been seen yet, start a new window
        if not self.current_window_ns:
            self.current_window_ns = timestamp_ns

        # If more time than the configured time window
        # has past since last window, reset
        # DEV: We are comparing nanoseconds, so 1e9 is 1 second
        elif timestamp_ns - self.current_window_ns >= self.time_window:
            # Store previous window's rate to average with current for `.effective_rate`
            self.prev_window_rate = self._current_window_rate()
            self.tokens_allowed = 0
            self.tokens_total = 0
            self.current_window_ns = timestamp_ns

        # Keep track of total tokens seen vs allowed
        if allowed:
            self.tokens_allowed += 1
        self.tokens_total += 1

    def _is_allowed(self, timestamp_ns: int) -> bool:
        # Rate limit of 0 blocks everything
        if self.rate_limit == 0:
            return False

        # Negative rate limit disables rate limiting
        elif self.rate_limit < 0:
            return True

        # Lock, we need this to be thread safe, it should be shared by all threads
        with self._lock:
            self._replenish(timestamp_ns)

            if self.tokens >= 1:
                self.tokens -= 1
                return True

            return False

    def _replenish(self, timestamp_ns: int) -> None:
        try:
            # If we are at the max, we do not need to add any more
            if self.tokens == self.max_tokens:
                return

            # Add more available tokens based on how much time has passed
            # DEV: We store as nanoseconds, convert to seconds
            elapsed = (timestamp_ns - self.last_update_ns) / self.time_window
        finally:
            # always update the timestamp
            # we can't update at the beginning of the function, since if we did, our calculation for
            # elapsed would be incorrect
            self.last_update_ns = timestamp_ns

        # Update the number of available tokens, but ensure we do not exceed the max
        self.tokens = min(
            self.max_tokens,
            self.tokens + (elapsed * self.rate_limit),
        )

    def _current_window_rate(self) -> float:
        # No tokens have been seen, effectively 100% sample rate
        # DEV: This is to avoid division by zero error
        if not self.tokens_total:
            return 1.0

        # Get rate of tokens allowed
        return self.tokens_allowed / self.tokens_total

    @property
    def effective_rate(self) -> float:
        """
        Return the effective sample rate of this rate limiter

        :returns: Effective sample rate value 0.0 <= rate <= 1.0
        :rtype: :obj:`float``
        """
        # If we have not had a previous window yet, return current rate
        if self.prev_window_rate is None:
            return self._current_window_rate()

        return (self._current_window_rate() + self.prev_window_rate) / 2.0

    def __repr__(self):
        return "{}(rate_limit={!r}, tokens={!r}, last_update_ns={!r}, effective_rate={!r})".format(
            self.__class__.__name__,
            self.rate_limit,
            self.tokens,
            self.last_update_ns,
            self.effective_rate,
        )

    __str__ = __repr__


class RateLimitExceeded(Exception):
    pass


@dataclass
class BudgetRateLimiterWithJitter:
    """A budget rate limiter with jitter.

    The jitter is induced by a uniform distribution. The rate limit can be
    specified with ``limit_rate`` and the time scale can be controlled with the
    ``tau`` parameter (which defaults to 1 second). The initial budget is the
    product between ``limit_rate`` and the time-scale parameter ``tau``, which
    is also taken as the maximum budget. By default, the ``RateLimitExceeded``
    exception is raised when the rate limit is exceeded. This can be changed by
    setting ``raise_on_exceed`` to ``False``. The ``on_exceed`` argument can be
    used to pass a callback that is to be called whenever the rate limit is
    exceeded. The ``call_once`` argument controls whether the callback should be
    called only once for every rate limit excess or every time the rate limiter
    is invoked.

    Instances of this class can also be used as decorators.

    Since the initial and maximum budget are set to ``limit_rate * tau``, the
    rate limiter could have an initial burst phase. When this is not desired,
    ``tau`` should be set to ``1 / limit_rate`` to ensure an initial and maximum
    budget of ``1``.
    """

    limit_rate: float
    tau: float = 1.0
    raise_on_exceed: bool = True
    on_exceed: Optional[Callable] = None
    call_once: bool = False
    budget: float = field(init=False)
    max_budget: float = field(init=False)
    last_time: float = field(init=False, default_factory=time.monotonic)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self):
        if self.limit_rate == float("inf"):
            self.budget = self.max_budget = float("inf")
        elif self.limit_rate:
            self.budget = self.max_budget = self.limit_rate * self.tau
        else:
            self.budget = self.max_budget = 1.0
        self._on_exceed_called = False

    def limit(self, f: Optional[Callable[..., Any]] = None, *args: Any, **kwargs: Any) -> Any:
        """Make rate-limited calls to a function with the given arguments."""
        should_call = False
        with self._lock:
            now = time.monotonic()
            self.budget += self.limit_rate * (now - self.last_time) * (0.5 + random.random())  # jitter
            should_call = self.budget >= 1.0
            if self.budget > self.max_budget:
                self.budget = self.max_budget
            self.last_time = now

        if should_call:
            self._on_exceed_called = False
            self.budget -= 1.0
            return f(*args, **kwargs) if f is not None else None

        if self.on_exceed is not None:
            if not self.call_once:
                self.on_exceed()
            elif not self._on_exceed_called:
                self.on_exceed()
                self._on_exceed_called = True

        if self.raise_on_exceed:
            raise RateLimitExceeded()
        else:
            return RateLimitExceeded

    def __call__(self, f: Callable[..., Any]) -> Callable[..., Any]:
        def limited_f(*args, **kwargs):
            return self.limit(f, *args, **kwargs)

        return limited_f
