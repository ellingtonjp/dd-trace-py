import os
import os.path
from platform import machine
from platform import system
import sys
from typing import List
from typing import Optional

from envier import Env

from ddtrace import config as tracer_config
from ddtrace.appsec._constants import API_SECURITY
from ddtrace.appsec._constants import APPSEC
from ddtrace.appsec._constants import DEFAULT
from ddtrace.appsec._constants import EXPLOIT_PREVENTION
from ddtrace.appsec._constants import IAST
from ddtrace.appsec._constants import LOGIN_EVENTS_MODE
from ddtrace.appsec._constants import TELEMETRY_INFORMATION_NAME
from ddtrace.constants import APPSEC_ENV
from ddtrace.internal import core
from ddtrace.internal.serverless import in_aws_lambda
from ddtrace.settings._core import report_telemetry as _report_telemetry


def _validate_non_negative_int(r: int) -> None:
    if r < 0:
        raise ValueError("value must be non negative")


def _validate_percentage(r: float) -> None:
    if r < 0 or r > 100:
        raise ValueError("percentage value must be between 0 and 100")


def _parse_options(options: List[str]):
    def parse(str_in: str) -> str:
        for o in options:
            if o.startswith(str_in.lower()):
                return o
        return options[0]

    return parse


def build_libddwaf_filename() -> str:
    """
    Build the filename of the libddwaf library to load.
    """
    _DIRNAME = os.path.dirname(os.path.dirname(__file__))
    FILE_EXTENSION = {"Linux": "so", "Darwin": "dylib", "Windows": "dll"}[system()]
    ARCHI = machine().lower()
    # 32-bit-Python on 64-bit-Windows
    if system() == "Windows" and ARCHI == "amd64":
        from sys import maxsize

        if maxsize <= (1 << 32):
            ARCHI = "x86"
    TRANSLATE_ARCH = {"amd64": "x64", "i686": "x86_64", "x86": "win32"}
    ARCHITECTURE = TRANSLATE_ARCH.get(ARCHI, ARCHI)
    return os.path.join(_DIRNAME, "appsec", "_ddwaf", "libddwaf", ARCHITECTURE, "lib", "libddwaf." + FILE_EXTENSION)


class ASMConfig(Env):
    _asm_enabled = Env.var(bool, APPSEC_ENV, default=False)
    _asm_static_rule_file = Env.var(Optional[str], APPSEC.RULE_FILE, default=None)
    # prevent empty string
    if _asm_static_rule_file == "":
        _asm_static_rule_file = None
    _iast_enabled = tracer_config._from_endpoint.get(  # type: ignore
        "iast_enabled", Env.var(bool, IAST.ENV, default=False)
    )
    _iast_request_sampling = Env.var(float, IAST.ENV_REQUEST_SAMPLING, default=30.0)
    _iast_debug = Env.var(bool, IAST.ENV_DEBUG, default=False, private=True)
    _iast_propagation_debug = Env.var(bool, IAST.ENV_PROPAGATION_DEBUG, default=False, private=True)
    _iast_telemetry_report_lvl = Env.var(str, IAST.ENV_TELEMETRY_REPORT_LVL, default=TELEMETRY_INFORMATION_NAME)
    _appsec_standalone_enabled = Env.var(bool, APPSEC.STANDALONE_ENV, default=False)
    _use_metastruct_for_triggers = True

    _auto_user_instrumentation_local_mode = Env.var(
        str,
        APPSEC.AUTO_USER_INSTRUMENTATION_MODE,
        default=LOGIN_EVENTS_MODE.IDENT,
        parser=_parse_options([LOGIN_EVENTS_MODE.DISABLED, LOGIN_EVENTS_MODE.IDENT, LOGIN_EVENTS_MODE.ANON]),
    )
    _auto_user_instrumentation_rc_mode: Optional[str] = None
    _auto_user_instrumentation_enabled = Env.var(bool, APPSEC.AUTO_USER_INSTRUMENTATION_MODE_ENABLED, default=True)

    _user_model_login_field = Env.var(str, APPSEC.USER_MODEL_LOGIN_FIELD, default="")
    _user_model_email_field = Env.var(str, APPSEC.USER_MODEL_EMAIL_FIELD, default="")
    _user_model_name_field = Env.var(str, APPSEC.USER_MODEL_NAME_FIELD, default="")
    _api_security_enabled = Env.var(bool, API_SECURITY.ENV_VAR_ENABLED, default=True)
    _api_security_sample_delay = Env.var(float, API_SECURITY.SAMPLE_DELAY, default=30.0)
    _api_security_parse_response_body = Env.var(bool, API_SECURITY.PARSE_RESPONSE_BODY, default=True)

    # internal state of the API security Manager service.
    # updated in API Manager enable/disable
    _api_security_active = False
    _asm_libddwaf = build_libddwaf_filename()
    _asm_libddwaf_available = os.path.exists(_asm_libddwaf)

    _waf_timeout = Env.var(
        float,
        "DD_APPSEC_WAF_TIMEOUT",
        default=DEFAULT.WAF_TIMEOUT,
        help_type=float,
        help="Timeout in milliseconds for WAF computations",
    )
    _asm_deduplication_enabled = Env.var(bool, "_DD_APPSEC_DEDUPLICATION_ENABLED", default=True)
    _asm_obfuscation_parameter_key_regexp = Env.var(
        str, APPSEC.OBFUSCATION_PARAMETER_KEY_REGEXP, default=DEFAULT.APPSEC_OBFUSCATION_PARAMETER_KEY_REGEXP
    )
    _asm_obfuscation_parameter_value_regexp = Env.var(
        str, APPSEC.OBFUSCATION_PARAMETER_VALUE_REGEXP, default=DEFAULT.APPSEC_OBFUSCATION_PARAMETER_VALUE_REGEXP
    )

    _iast_redaction_enabled = Env.var(bool, IAST.REDACTION_ENABLED, default=True)
    _iast_redaction_name_pattern = Env.var(
        str,
        IAST.REDACTION_NAME_PATTERN,
        default=r"(?i)^.*(?:p(?:ass)?w(?:or)?d|pass(?:_?phrase)?|secret|(?:api_?|private_?|"
        + r"public_?|access_?|secret_?)key(?:_?id)?|password|token|username|user_id|last.name|"
        + r"consumer_?(?:id|key|secret)|"
        + r"sign(?:ed|ature)?|auth(?:entication|orization)?)",
    )
    _iast_redaction_value_pattern = Env.var(
        str,
        IAST.REDACTION_VALUE_PATTERN,
        default=r"(?i)bearer\s+[a-z0-9\._\-]+|token:[a-z0-9]{13}|password|gh[opsu]_[0-9a-zA-Z]{36}|"
        + r"ey[I-L][\w=-]+\.ey[I-L][\w=-]+(\.[\w.+\/=-]+)?|[\-]{5}BEGIN[a-z\s]+PRIVATE\sKEY"
        + r"[\-]{5}[^\-]+[\-]{5}END[a-z\s]+PRIVATE\sKEY|ssh-rsa\s*[a-z0-9\/\.+]{100,}",
    )
    _iast_max_concurrent_requests = Env.var(
        int,
        IAST.DD_IAST_MAX_CONCURRENT_REQUESTS,
        default=2,
    )
    _iast_max_vulnerabilities_per_requests = Env.var(
        int,
        IAST.DD_IAST_VULNERABILITIES_PER_REQUEST,
        default=2,
    )
    _iast_lazy_taint = Env.var(bool, IAST.LAZY_TAINT, default=False)
    _iast_deduplication_enabled = Env.var(bool, "DD_IAST_DEDUPLICATION_ENABLED", default=True)

    # default will be set to True once the feature is GA. For now it's always False
    _ep_enabled = Env.var(bool, EXPLOIT_PREVENTION.EP_ENABLED, default=True)
    _ep_stack_trace_enabled = Env.var(bool, EXPLOIT_PREVENTION.STACK_TRACE_ENABLED, default=True)
    # for max_stack_traces, 0 == unlimited
    _ep_max_stack_traces = Env.var(
        int, EXPLOIT_PREVENTION.MAX_STACK_TRACES, default=2, validator=_validate_non_negative_int
    )
    # for max_stack_trace_depth, 0 == unlimited
    _ep_max_stack_trace_depth = Env.var(
        int, EXPLOIT_PREVENTION.MAX_STACK_TRACE_DEPTH, default=32, validator=_validate_non_negative_int
    )

    # percentage of stack trace reported on top, in case depth is larger than max_stack_trace_depth
    _ep_stack_top_percent = Env.var(
        float, EXPLOIT_PREVENTION.STACK_TOP_PERCENT, default=75.0, validator=_validate_percentage
    )

    _iast_stack_trace_enabled = Env.var(bool, IAST.STACK_TRACE_ENABLED, default=True)

    # Django ATO
    _django_include_user_name = Env.var(bool, "DD_DJANGO_INCLUDE_USER_NAME", default=True)
    _django_include_user_email = Env.var(bool, "DD_DJANGO_INCLUDE_USER_EMAIL", default=False)
    _django_include_user_login = Env.var(bool, "DD_DJANGO_INCLUDE_USER_LOGIN", default=True)
    _django_include_user_realname = Env.var(bool, "DD_DJANGO_INCLUDE_USER_REALNAME", default=False)

    # for tests purposes
    _asm_config_keys = [
        "_asm_enabled",
        "_asm_can_be_enabled",
        "_asm_static_rule_file",
        "_asm_obfuscation_parameter_key_regexp",
        "_asm_obfuscation_parameter_value_regexp",
        "_appsec_standalone_enabled",
        "_iast_enabled",
        "_iast_request_sampling",
        "_iast_debug",
        "_iast_propagation_debug",
        "_iast_telemetry_report_lvl",
        "_ep_enabled",
        "_use_metastruct_for_triggers",
        "_auto_user_instrumentation_local_mode",
        "_auto_user_instrumentation_rc_mode",
        "_auto_user_instrumentation_enabled",
        "_user_model_login_field",
        "_user_model_email_field",
        "_user_model_name_field",
        "_api_security_enabled",
        "_api_security_sample_delay",
        "_api_security_parse_response_body",
        "_waf_timeout",
        "_iast_redaction_enabled",
        "_iast_redaction_name_pattern",
        "_iast_redaction_value_pattern",
        "_iast_max_concurrent_requests",
        "_iast_max_vulnerabilities_per_requests",
        "_iast_lazy_taint",
        "_iast_deduplication_enabled",
        "_ep_stack_trace_enabled",
        "_ep_max_stack_traces",
        "_ep_max_stack_trace_depth",
        "_ep_stack_top_percent",
        "_iast_stack_trace_enabled",
        "_asm_config_keys",
        "_asm_deduplication_enabled",
        "_django_include_user_name",
        "_django_include_user_email",
        "_django_include_user_login",
        "_django_include_user_realname",
    ]
    _iast_redaction_numeral_pattern = Env.var(
        str,
        IAST.REDACTION_VALUE_NUMERAL,
        default=r"^[+-]?((0b[01]+)|(0x[0-9A-Fa-f]+)|(\d+\.?\d*(?:[Ee][+-]?\d+)?|\.\d+(?:[Ee][+-]"
        + r"?\d+)?)|(X\'[0-9A-Fa-f]+\')|(B\'[01]+\'))$",
    )
    _bypass_instrumentation_for_waf = False

    # IAST supported on python 3.6 to 3.13 and never on windows
    _iast_supported: bool = ((3, 6, 0) <= sys.version_info < (3, 14, 0)) and not (
        sys.platform.startswith("win") or sys.platform.startswith("cygwin")
    )

    def __init__(self):
        super().__init__()
        if not self._iast_supported:
            self._iast_enabled = False
        if not self._asm_libddwaf_available or in_aws_lambda():
            self._asm_enabled = False
            self._asm_can_be_enabled = False
            self._iast_enabled = False
            self._api_security_enabled = False
            self._ep_enabled = False
            self._auto_user_instrumentation_enabled = False
            self._auto_user_instrumentation_local_mode = LOGIN_EVENTS_MODE.DISABLED
            self._load_modules = False
            self._asm_rc_enabled = False
        else:
            # Is one click available?
            self._eval_asm_can_be_enabled()

    def reset(self):
        """For testing purposes, reset the configuration to its default values given current environment variables."""
        self.__init__()

    def _eval_asm_can_be_enabled(self):
        self._asm_can_be_enabled = APPSEC_ENV not in os.environ and tracer_config._remote_config_enabled
        self._load_modules: bool = bool(
            self._iast_enabled or (self._ep_enabled and (self._asm_enabled or self._asm_can_be_enabled))
        )
        self._asm_rc_enabled = (self._asm_enabled and tracer_config._remote_config_enabled) or self._asm_can_be_enabled

    @property
    def _api_security_feature_active(self) -> bool:
        return self._asm_libddwaf_available and self._asm_enabled and self._api_security_enabled

    @property
    def _apm_opt_out(self) -> bool:
        return (
            self._asm_enabled or self._iast_enabled or tracer_config._sca_enabled is True
        ) and self._appsec_standalone_enabled

    @property
    def _user_event_mode(self) -> str:
        if self._asm_enabled and self._auto_user_instrumentation_enabled:
            if self._auto_user_instrumentation_rc_mode is not None:
                return self._auto_user_instrumentation_rc_mode
            return self._auto_user_instrumentation_local_mode
        return LOGIN_EVENTS_MODE.DISABLED

    @property
    def is_iast_request_enabled(self) -> bool:
        env = core.get_item(IAST.REQUEST_CONTEXT_KEY)
        if env:
            return env.request_enabled
        return False


config = ASMConfig()
_report_telemetry(config)
