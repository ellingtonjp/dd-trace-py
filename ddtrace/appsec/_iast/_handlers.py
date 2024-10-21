from collections.abc import MutableMapping
import functools

from wrapt import when_imported
from wrapt import wrap_function_wrapper as _w

from ddtrace.appsec._iast import _is_iast_enabled
from ddtrace.appsec._iast._metrics import _set_metric_iast_instrumented_source
from ddtrace.appsec._iast._patch import _iast_instrument_starlette_request
from ddtrace.appsec._iast._patch import _iast_instrument_starlette_request_body
from ddtrace.appsec._iast._patch import _iast_instrument_starlette_url
from ddtrace.appsec._iast._patch import _patched_dictionary
from ddtrace.appsec._iast._patch import try_wrap_function_wrapper
from ddtrace.appsec._iast._taint_utils import taint_structure
from ddtrace.internal.logger import get_logger


MessageMapContainer = None
try:
    from google._upb._message import MessageMapContainer  # type: ignore[no-redef]
except ImportError:
    pass


log = get_logger(__name__)


def _on_set_http_meta_iast(
    span,
    request_ip,
    raw_uri,
    route,
    method,
    request_headers,
    request_cookies,
    parsed_query,
    request_path_params,
    request_body,
    status_code,
    response_headers,
    response_cookies,
):
    if _is_iast_enabled():
        from ddtrace.appsec._iast.taint_sinks.insecure_cookie import asm_check_cookies

        if response_cookies:
            asm_check_cookies(response_cookies)


def _on_request_init(wrapped, instance, args, kwargs):
    from ddtrace.appsec._iast._iast_request_context import in_iast_context

    wrapped(*args, **kwargs)
    if _is_iast_enabled() and in_iast_context():
        try:
            from ddtrace.appsec._iast._taint_tracking import OriginType
            from ddtrace.appsec._iast._taint_tracking import origin_to_str
            from ddtrace.appsec._iast._taint_tracking import taint_pyobject

            instance.query_string = taint_pyobject(
                pyobject=instance.query_string,
                source_name=origin_to_str(OriginType.QUERY),
                source_value=instance.query_string,
                source_origin=OriginType.QUERY,
            )
            instance.path = taint_pyobject(
                pyobject=instance.path,
                source_name=origin_to_str(OriginType.PATH),
                source_value=instance.path,
                source_origin=OriginType.PATH,
            )
        except Exception:
            log.debug("Unexpected exception while tainting pyobject", exc_info=True)


def _on_flask_patch(flask_version):
    if _is_iast_enabled():
        from ddtrace.appsec._iast._taint_tracking import OriginType

        try_wrap_function_wrapper(
            "werkzeug.datastructures",
            "Headers.items",
            functools.partial(if_iast_taint_yield_tuple_for, (OriginType.HEADER_NAME, OriginType.HEADER)),
        )
        _set_metric_iast_instrumented_source(OriginType.HEADER_NAME)
        _set_metric_iast_instrumented_source(OriginType.HEADER)

        try_wrap_function_wrapper(
            "werkzeug.datastructures",
            "ImmutableMultiDict.__getitem__",
            functools.partial(if_iast_taint_returned_object_for, OriginType.PARAMETER),
        )
        _set_metric_iast_instrumented_source(OriginType.PARAMETER)

        try_wrap_function_wrapper(
            "werkzeug.datastructures",
            "EnvironHeaders.__getitem__",
            functools.partial(if_iast_taint_returned_object_for, OriginType.HEADER),
        )
        _set_metric_iast_instrumented_source(OriginType.HEADER)

        if flask_version >= (2, 0, 0):
            # instance.query_string: raising an error on werkzeug/_internal.py "AttributeError: read only property"
            try_wrap_function_wrapper("werkzeug.wrappers.request", "Request.__init__", _on_request_init)

        _set_metric_iast_instrumented_source(OriginType.PATH)
        _set_metric_iast_instrumented_source(OriginType.QUERY)

        # Instrumented on _ddtrace.appsec._asm_request_context._on_wrapped_view
        _set_metric_iast_instrumented_source(OriginType.PATH_PARAMETER)

        try_wrap_function_wrapper(
            "werkzeug.wrappers.request",
            "Request.get_data",
            functools.partial(_patched_dictionary, OriginType.BODY, OriginType.BODY),
        )
        try_wrap_function_wrapper(
            "werkzeug.wrappers.request",
            "Request.get_json",
            functools.partial(_patched_dictionary, OriginType.BODY, OriginType.BODY),
        )

        _set_metric_iast_instrumented_source(OriginType.BODY)

        if flask_version < (2, 0, 0):
            _w(
                "werkzeug._internal",
                "_DictAccessorProperty.__get__",
                functools.partial(if_iast_taint_returned_object_for, OriginType.QUERY),
            )
            _set_metric_iast_instrumented_source(OriginType.QUERY)


def _on_wsgi_environ(wrapped, _instance, args, kwargs):
    from ddtrace.appsec._iast._iast_request_context import in_iast_context

    if _is_iast_enabled() and args and in_iast_context():
        from ddtrace.appsec._iast._taint_tracking import OriginType

        return wrapped(*((taint_structure(args[0], OriginType.HEADER_NAME, OriginType.HEADER),) + args[1:]), **kwargs)

    return wrapped(*args, **kwargs)


def _on_django_patch():
    if _is_iast_enabled():
        try:
            from ddtrace.appsec._iast._taint_tracking import OriginType

            # we instrument those sources on _on_django_func_wrapped
            _set_metric_iast_instrumented_source(OriginType.HEADER_NAME)
            _set_metric_iast_instrumented_source(OriginType.HEADER)
            _set_metric_iast_instrumented_source(OriginType.PATH_PARAMETER)
            _set_metric_iast_instrumented_source(OriginType.PATH)
            _set_metric_iast_instrumented_source(OriginType.COOKIE)
            _set_metric_iast_instrumented_source(OriginType.COOKIE_NAME)
            _set_metric_iast_instrumented_source(OriginType.PARAMETER)
            _set_metric_iast_instrumented_source(OriginType.PARAMETER_NAME)
            _set_metric_iast_instrumented_source(OriginType.BODY)
            when_imported("django.http.request")(
                lambda m: try_wrap_function_wrapper(
                    m,
                    "QueryDict.__getitem__",
                    functools.partial(if_iast_taint_returned_object_for, OriginType.PARAMETER),
                )
            )
        except Exception:
            log.debug("Unexpected exception while patch IAST functions", exc_info=True)


def _on_django_func_wrapped(fn_args, fn_kwargs, first_arg_expected_type, *_):
    # If IAST is enabled and we're wrapping a Django view call, taint the kwargs (view's
    # path parameters)
    if _is_iast_enabled() and fn_args and isinstance(fn_args[0], first_arg_expected_type):
        from ddtrace.appsec._iast._iast_request_context import in_iast_context
        from ddtrace.appsec._iast._taint_tracking import OriginType  # noqa: F401
        from ddtrace.appsec._iast._taint_tracking import is_pyobject_tainted
        from ddtrace.appsec._iast._taint_tracking import origin_to_str
        from ddtrace.appsec._iast._taint_tracking import taint_pyobject

        if not in_iast_context():
            return

        http_req = fn_args[0]

        http_req.COOKIES = taint_structure(http_req.COOKIES, OriginType.COOKIE_NAME, OriginType.COOKIE)
        http_req.GET = taint_structure(http_req.GET, OriginType.PARAMETER_NAME, OriginType.PARAMETER)
        http_req.POST = taint_structure(http_req.POST, OriginType.BODY, OriginType.BODY)
        if (
            getattr(http_req, "_body", None) is not None
            and len(getattr(http_req, "_body", None)) > 0
            and not is_pyobject_tainted(getattr(http_req, "_body", None))
        ):
            try:
                http_req._body = taint_pyobject(
                    http_req._body,
                    source_name=origin_to_str(OriginType.BODY),
                    source_value=http_req._body,
                    source_origin=OriginType.BODY,
                )
            except AttributeError:
                log.debug("IAST can't set attribute http_req._body", exc_info=True)
        elif (
            getattr(http_req, "body", None) is not None
            and len(getattr(http_req, "body", None)) > 0
            and not is_pyobject_tainted(getattr(http_req, "body", None))
        ):
            try:
                http_req.body = taint_pyobject(
                    http_req.body,
                    source_name=origin_to_str(OriginType.BODY),
                    source_value=http_req.body,
                    source_origin=OriginType.BODY,
                )
            except AttributeError:
                log.debug("IAST can't set attribute http_req.body", exc_info=True)

        http_req.headers = taint_structure(http_req.headers, OriginType.HEADER_NAME, OriginType.HEADER)
        http_req.path = taint_pyobject(
            http_req.path, source_name="path", source_value=http_req.path, source_origin=OriginType.PATH
        )
        http_req.path_info = taint_pyobject(
            http_req.path_info,
            source_name=origin_to_str(OriginType.PATH),
            source_value=http_req.path,
            source_origin=OriginType.PATH,
        )
        http_req.environ["PATH_INFO"] = taint_pyobject(
            http_req.environ["PATH_INFO"],
            source_name=origin_to_str(OriginType.PATH),
            source_value=http_req.path,
            source_origin=OriginType.PATH,
        )
        http_req.META = taint_structure(http_req.META, OriginType.HEADER_NAME, OriginType.HEADER)
        if fn_kwargs:
            try:
                for k, v in fn_kwargs.items():
                    fn_kwargs[k] = taint_pyobject(
                        v, source_name=k, source_value=v, source_origin=OriginType.PATH_PARAMETER
                    )
            except Exception:
                log.debug("IAST: Unexpected exception while tainting path parameters", exc_info=True)


def _custom_protobuf_getattribute(self, name):
    from ddtrace.appsec._iast._taint_tracking import OriginType
    from ddtrace.appsec._iast._taint_tracking import taint_pyobject

    ret = type(self).__saved_getattr(self, name)
    if isinstance(ret, (str, bytes, bytearray)):
        ret = taint_pyobject(
            pyobject=ret,
            source_name=OriginType.GRPC_BODY,
            source_value=ret,
            source_origin=OriginType.GRPC_BODY,
        )
    elif MessageMapContainer is not None and isinstance(ret, MutableMapping):
        if isinstance(ret, MessageMapContainer) and len(ret):
            # Patch the message-values class
            first_key = next(iter(ret))
            value_type = type(ret[first_key])
            _patch_protobuf_class(value_type)
        else:
            ret = taint_structure(ret, OriginType.GRPC_BODY, OriginType.GRPC_BODY)

    return ret


_custom_protobuf_getattribute.__datadog_custom = True  # type: ignore[attr-defined]


# Used to replace the Protobuf message class "getattribute" with a custom one that taints the return
# of the original __getattribute__ method
def _patch_protobuf_class(cls):
    getattr_method = getattr(cls, "__getattribute__")
    if not getattr_method:
        return

    if not hasattr(getattr_method, "__datadog_custom"):
        try:
            # Replace the class __getattribute__ method with our custom one
            # (replacement is done at the class level because it would incur on a recursive loop with the instance)
            cls.__saved_getattr = getattr_method
            cls.__getattribute__ = _custom_protobuf_getattribute
        except TypeError:
            # Avoid failing on Python 3.12 while patching immutable types
            pass


def _on_grpc_response(message):
    if _is_iast_enabled():
        msg_cls = type(message)
        _patch_protobuf_class(msg_cls)


def if_iast_taint_yield_tuple_for(origins, wrapped, instance, args, kwargs):
    if _is_iast_enabled():
        from ._iast_request_context import is_iast_request_enabled
        from ._taint_tracking import taint_pyobject

        if not is_iast_request_enabled():
            for key, value in wrapped(*args, **kwargs):
                yield key, value
        else:
            for key, value in wrapped(*args, **kwargs):
                new_key = taint_pyobject(pyobject=key, source_name=key, source_value=key, source_origin=origins[0])
                new_value = taint_pyobject(
                    pyobject=value, source_name=key, source_value=value, source_origin=origins[1]
                )
                yield new_key, new_value

    else:
        for key, value in wrapped(*args, **kwargs):
            yield key, value


def if_iast_taint_returned_object_for(origin, wrapped, instance, args, kwargs):
    value = wrapped(*args, **kwargs)
    from ._iast_request_context import is_iast_request_enabled

    if _is_iast_enabled() and is_iast_request_enabled():
        try:
            from ._taint_tracking import is_pyobject_tainted
            from ._taint_tracking import taint_pyobject

            if not is_pyobject_tainted(value):
                name = str(args[0]) if len(args) else "http.request.body"
                from ddtrace.appsec._iast._taint_tracking import OriginType

                if origin == OriginType.HEADER and name.lower() in ["cookie", "cookies"]:
                    origin = OriginType.COOKIE
                return taint_pyobject(pyobject=value, source_name=name, source_value=value, source_origin=origin)
        except Exception:
            log.debug("Unexpected exception while tainting pyobject", exc_info=True)
    return value


def _on_iast_fastapi_patch():
    from ddtrace.appsec._iast._taint_tracking import OriginType

    # Cookies sources
    try_wrap_function_wrapper(
        "starlette.requests",
        "cookie_parser",
        functools.partial(_patched_dictionary, OriginType.COOKIE_NAME, OriginType.COOKIE),
    )
    _set_metric_iast_instrumented_source(OriginType.COOKIE)
    _set_metric_iast_instrumented_source(OriginType.COOKIE_NAME)

    # Parameter sources
    try_wrap_function_wrapper(
        "starlette.datastructures",
        "QueryParams.__getitem__",
        functools.partial(if_iast_taint_returned_object_for, OriginType.PARAMETER),
    )
    try_wrap_function_wrapper(
        "starlette.datastructures",
        "QueryParams.get",
        functools.partial(if_iast_taint_returned_object_for, OriginType.PARAMETER),
    )
    _set_metric_iast_instrumented_source(OriginType.PARAMETER)

    # Header sources
    try_wrap_function_wrapper(
        "starlette.datastructures",
        "Headers.__getitem__",
        functools.partial(if_iast_taint_returned_object_for, OriginType.HEADER),
    )
    try_wrap_function_wrapper(
        "starlette.datastructures",
        "Headers.get",
        functools.partial(if_iast_taint_returned_object_for, OriginType.HEADER),
    )
    _set_metric_iast_instrumented_source(OriginType.HEADER)

    # Path source
    try_wrap_function_wrapper("starlette.datastructures", "URL.__init__", _iast_instrument_starlette_url)
    _set_metric_iast_instrumented_source(OriginType.PATH)

    # Body source
    try_wrap_function_wrapper("starlette.requests", "Request.__init__", _iast_instrument_starlette_request)
    try_wrap_function_wrapper("starlette.requests", "Request.body", _iast_instrument_starlette_request_body)
    try_wrap_function_wrapper(
        "starlette.datastructures",
        "FormData.__getitem__",
        functools.partial(if_iast_taint_returned_object_for, OriginType.BODY),
    )
    try_wrap_function_wrapper(
        "starlette.datastructures",
        "FormData.get",
        functools.partial(if_iast_taint_returned_object_for, OriginType.BODY),
    )
    _set_metric_iast_instrumented_source(OriginType.BODY)

    # Instrumented on _iast_starlette_scope_taint
    _set_metric_iast_instrumented_source(OriginType.PATH_PARAMETER)
