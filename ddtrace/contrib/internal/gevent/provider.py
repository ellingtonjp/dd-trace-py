import gevent

from ddtrace._trace.provider import BaseContextProvider
from ddtrace._trace.provider import DatadogContextMixin
from ddtrace.trace import Span
from ddtrace.vendor.debtcollector import deprecate


class GeventContextProvider(BaseContextProvider, DatadogContextMixin):
    """Manages the active context for gevent execution.

    This provider depends on corresponding monkey patches to copy the active
    context from one greenlet to another.
    """

    # Greenlet attribute used to set/get the context
    _CONTEXT_ATTR = "__datadog_context"

    def __init__(self) -> None:
        deprecate("GeventContextProvider is deprecated and will be removed in a future version.", "3.0.0")
        super().__init__()

    def _get_current_context(self):
        """Helper to get the active context from the current greenlet."""
        current_g = gevent.getcurrent()
        if current_g is not None:
            return getattr(current_g, self._CONTEXT_ATTR, None)
        return None

    def _has_active_context(self):
        """Helper to determine if there is an active context."""
        return self._get_current_context() is not None

    def activate(self, context):
        """Sets the active context for the current running ``Greenlet``."""
        current_g = gevent.getcurrent()
        if current_g is not None:
            setattr(current_g, self._CONTEXT_ATTR, context)
            super(GeventContextProvider, self).activate(context)
            return context

    def active(self):
        """Returns the active context for this execution flow."""
        ctx = self._get_current_context()
        if isinstance(ctx, Span):
            return self._update_active(ctx)
        return ctx
