import inspect
import sys


class BaseTracedLangChainStreamResponse:
    def __init__(self, generator, integration, span, on_span_finish):
        self._generator = generator
        self._dd_integration = integration
        self._dd_span = span
        self._on_span_finish = on_span_finish
        self._chunks = []


class TracedLangchainStreamResponse(BaseTracedLangChainStreamResponse):
    def __enter__(self):
        self._generator.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._generator.__exit__(exc_type, exc_val, exc_tb)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = self._generator.__next__()
            self._chunks.append(chunk)
            return chunk
        except StopIteration:
            self._on_span_finish(self._dd_span, self._chunks)
            self._dd_span.finish()
            raise
        except Exception:
            self._dd_span.set_exc_info(*sys.exc_info())
            self._dd_integration.metric(self._dd_span, "incr", "request.error", 1)
            self._dd_span.finish()
            raise


class TracedLangchainAsyncStreamResponse(BaseTracedLangChainStreamResponse):
    async def __aenter__(self):
        await self._generator.__enter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._generator.__exit__(exc_type, exc_val, exc_tb)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._generator.__anext__()
            self._chunks.append(chunk)
            return chunk
        except StopAsyncIteration:
            self._on_span_finish(self._dd_span, self._chunks)
            self._dd_span.finish()
            raise
        except Exception:
            self._dd_span.set_exc_info(*sys.exc_info())
            self._dd_integration.metric(self._dd_span, "incr", "request.error", 1)
            self._dd_span.finish()
            raise


def shared_stream(
    integration,
    pin,
    func,
    instance,
    args,
    kwargs,
    interface_type,
    on_span_started,
    on_span_finished,
    **extra_options,
):
    options = {
        "pin": pin,
        "operation_id": f"{instance.__module__}.{instance.__class__.__name__}",
        "interface_type": interface_type,
        "submit_to_llmobs": True,
    }

    options.update(extra_options)

    span = integration.trace(**options)
    span.set_tag("langchain.request.stream", True)
    on_span_started(span)

    try:
        resp = func(*args, **kwargs)
        cls = TracedLangchainAsyncStreamResponse if inspect.isasyncgen(resp) else TracedLangchainStreamResponse

        return cls(resp, integration, span, on_span_finished)
    except Exception:
        # error with the method call itself
        span.set_exc_info(*sys.exc_info())
        span.finish()
        integration.metric(span, "incr", "request.error", 1)
        integration.metric(span, "dist", "request.duration", span.duration_ns)
        raise


def tag_general_message_input(span, inputs, integration, langchain_core):
    if langchain_core and isinstance(inputs, langchain_core.prompt_values.PromptValue):
        inputs = inputs.to_messages()
    elif not isinstance(inputs, list):
        inputs = [inputs]
    for input_idx, inp in enumerate(inputs):
        if isinstance(inp, dict):
            span.set_tag_str(
                "langchain.request.messages.%d.content" % (input_idx),
                integration.trunc(str(inp.get("content", ""))),
            )
            role = inp.get("role")
            if role is not None:
                span.set_tag_str(
                    "langchain.request.messages.%d.message_type" % (input_idx),
                    str(inp.get("role", "")),
                )
        elif langchain_core and isinstance(inp, langchain_core.messages.BaseMessage):
            content = inp.content
            role = inp.__class__.__name__
            span.set_tag_str("langchain.request.messages.%d.content" % (input_idx), integration.trunc(str(content)))
            span.set_tag_str("langchain.request.messages.%d.message_type" % (input_idx), str(role))
        else:
            span.set_tag_str("langchain.request.messages.%d.content" % (input_idx), integration.trunc(str(inp)))