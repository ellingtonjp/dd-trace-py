# -*- coding: utf-8 -*-
import os
from typing import Any
from typing import Dict
from typing import Mapping
from typing import Optional

from ddtrace.appsec._capabilities import _asm_feature_is_required
from ddtrace.appsec._capabilities import _rc_capabilities
from ddtrace.appsec._constants import PRODUCTS
from ddtrace.internal.logger import get_logger
from ddtrace.internal.remoteconfig._connectors import PublisherSubscriberConnector
from ddtrace.internal.remoteconfig._publishers import RemoteConfigPublisherMergeDicts
from ddtrace.internal.remoteconfig._pubsub import PubSub
from ddtrace.internal.remoteconfig._subscribers import RemoteConfigSubscriber
from ddtrace.internal.remoteconfig.worker import remoteconfig_poller
from ddtrace.internal.telemetry import telemetry_writer
from ddtrace.internal.telemetry.constants import TELEMETRY_APM_PRODUCT
from ddtrace.settings.asm import config as asm_config
from ddtrace.trace import Tracer


log = get_logger(__name__)

APPSEC_PRODUCTS = [PRODUCTS.ASM_FEATURES, PRODUCTS.ASM, PRODUCTS.ASM_DATA, PRODUCTS.ASM_DD]


class AppSecRC(PubSub):
    __subscriber_class__ = RemoteConfigSubscriber
    __publisher_class__ = RemoteConfigPublisherMergeDicts
    __shared_data__ = PublisherSubscriberConnector()

    def __init__(self, _preprocess_results, callback):
        self._publisher = self.__publisher_class__(self.__shared_data__, _preprocess_results)
        self._subscriber = self.__subscriber_class__(self.__shared_data__, callback, "ASM")


def _forksafe_appsec_rc():
    remoteconfig_poller.start_subscribers_by_product(APPSEC_PRODUCTS)


def enable_appsec_rc(test_tracer: Optional[Tracer] = None) -> None:
    """Remote config will be used by ASM libraries to receive four different updates from the backend.
    Each update has it’s own product:
    - ASM_FEATURES product - To allow users enable or disable ASM remotely
    - ASM product - To allow clients to activate or deactivate rules
    - ASM_DD product - To allow the library to receive rules updates
    - ASM_DATA product - To allow the library to receive list of blocked IPs and users

    If environment variable `DD_APPSEC_ENABLED` is not set, registering ASM_FEATURE can enable ASM remotely.
    If it's set to true, we will register the rest of the products.

    Parameters `test_tracer` and `start_subscribers` are needed for testing purposes
    """
    log.debug("[%s][P: %s] Register ASM Remote Config Callback", os.getpid(), os.getppid())
    asm_callback = (
        remoteconfig_poller.get_registered(PRODUCTS.ASM_FEATURES)
        or remoteconfig_poller.get_registered(PRODUCTS.ASM)
        or AppSecRC(_preprocess_results_appsec_1click_activation, _appsec_callback)
    )

    if _asm_feature_is_required():
        remoteconfig_poller.register(PRODUCTS.ASM_FEATURES, asm_callback, capabilities=[_rc_capabilities()])

    if asm_config._asm_enabled and asm_config._asm_static_rule_file is None:
        remoteconfig_poller.register(PRODUCTS.ASM_DATA, asm_callback)  # IP Blocking
        remoteconfig_poller.register(PRODUCTS.ASM, asm_callback)  # Exclusion Filters & Custom Rules
        remoteconfig_poller.register(PRODUCTS.ASM_DD, asm_callback)  # DD Rules
    # ensure exploit prevention patches are loaded by one-click activation
    if asm_config._asm_enabled:
        from ddtrace.appsec import load_common_appsec_modules

        load_common_appsec_modules()

    telemetry_writer.product_activated(TELEMETRY_APM_PRODUCT.APPSEC, True)
    asm_config._rc_client_id = remoteconfig_poller._client.id


def disable_appsec_rc():
    # only used to avoid data leaks between tests
    for product_name in APPSEC_PRODUCTS:
        remoteconfig_poller.unregister(product_name)

    telemetry_writer.product_activated(TELEMETRY_APM_PRODUCT.APPSEC, False)


def _add_rules_to_list(features: Mapping[str, Any], feature: str, message: str, ruleset: Dict[str, Any]) -> None:
    rules = features.get(feature, None)
    if rules is not None:
        if ruleset.get(feature) is None:
            ruleset[feature] = rules
        else:
            current_rules = ruleset[feature]
            if isinstance(rules, list) and isinstance(current_rules, list):
                ruleset[feature] = current_rules + rules
            elif isinstance(rules, dict) and isinstance(current_rules, dict):
                ruleset[feature] = {**current_rules, **rules}
            else:
                log.debug("Invalid type for %s: %s with %s", message, str(type(current_rules)), str(type(rules)))
        log.debug("Reloading Appsec %s: %s", message, str(rules)[:20])


def _appsec_callback(features: Mapping[str, Any], test_tracer: Optional[Tracer] = None) -> None:
    config = features.get("config", {})
    _appsec_1click_activation(config, test_tracer)
    _appsec_auto_user_mode(config, test_tracer)
    _appsec_rules_data(config, test_tracer)


def _appsec_rules_data(features: Mapping[str, Any], test_tracer: Optional[Tracer]) -> bool:
    # Tracer is a parameter for testing propose
    # Import tracer here to avoid a circular import
    if test_tracer is None:
        from ddtrace.trace import tracer
    else:
        tracer = test_tracer

    if features and tracer._appsec_processor:
        ruleset = {}  # type: dict[str, Optional[list[Any]]]
        if features.get("rules", None) == []:
            # if rules is empty, we need to switch back to the default rules
            ruleset = tracer._appsec_processor._rules.copy() or {}
        _add_rules_to_list(features, "actions", "actions", ruleset)
        _add_rules_to_list(features, "custom_rules", "custom rules", ruleset)
        _add_rules_to_list(features, "exclusions", "exclusion filters", ruleset)
        _add_rules_to_list(features, "exclusion_data", "exclusion data", ruleset)
        _add_rules_to_list(features, "processors", "processors", ruleset)
        _add_rules_to_list(features, "rules", "Datadog rules", ruleset)
        _add_rules_to_list(features, "rules_data", "rules data", ruleset)
        _add_rules_to_list(features, "rules_override", "rules override", ruleset)
        _add_rules_to_list(features, "scanners", "scanners", ruleset)
        _add_rules_to_list(features, "metadata", "metadata", ruleset)

        if ruleset:
            return tracer._appsec_processor._update_rules({k: v for k, v in ruleset.items() if v is not None})

    return False


def _preprocess_results_appsec_1click_activation(
    features: Dict[str, Any], pubsub_instance: Optional[PubSub] = None
) -> Dict[str, Any]:
    """The main process has the responsibility to enable or disable the ASM products. The child processes don't
    care about that, the children only need to know about payload content.
    """
    if asm_config._asm_can_be_enabled:
        log.debug(
            "[%s][P: %s] Receiving ASM Remote Configuration ASM_FEATURES: %s",
            os.getpid(),
            os.getppid(),
            features.get("asm", {}),
        )

        rc_asm_enabled = None
        if features and "asm" in features:
            rc_asm_enabled = features["asm"].get("enabled", False)
            log.debug(
                "[%s][P: %s] ASM Remote Configuration ASM_FEATURES. Appsec enabled: %s",
                os.getpid(),
                os.getppid(),
                rc_asm_enabled,
            )
            from ddtrace.appsec._constants import PRODUCTS

            if pubsub_instance is None:
                pubsub_instance = (
                    remoteconfig_poller.get_registered(PRODUCTS.ASM_FEATURES)
                    or remoteconfig_poller.get_registered(PRODUCTS.ASM)
                    or AppSecRC(_preprocess_results_appsec_1click_activation, _appsec_callback)
                )

            if rc_asm_enabled and asm_config._asm_static_rule_file is None:
                remoteconfig_poller.register(PRODUCTS.ASM_DATA, pubsub_instance)  # IP Blocking
                remoteconfig_poller.register(PRODUCTS.ASM, pubsub_instance)  # Exclusion Filters & Custom Rules
                remoteconfig_poller.register(PRODUCTS.ASM_DD, pubsub_instance)  # DD Rules
            else:
                remoteconfig_poller.unregister(PRODUCTS.ASM_DATA)
                remoteconfig_poller.unregister(PRODUCTS.ASM)
                remoteconfig_poller.unregister(PRODUCTS.ASM_DD)

            features["asm"]["enabled"] = rc_asm_enabled
    return features


def _appsec_1click_activation(features: Mapping[str, Any], test_tracer: Optional[Tracer] = None) -> None:
    """This callback updates appsec enabled in tracer and config instances following this logic:
    ```
    | DD_APPSEC_ENABLED | RC Enabled | Result   |
    |-------------------|------------|----------|
    | <not set>         | <not set>  | Disabled |
    | <not set>         | false      | Disabled |
    | <not set>         | true       | Enabled  |
    | false             | <not set>  | Disabled |
    | true              | <not set>  | Enabled  |
    | false             | true       | Disabled |
    | true              | true       | Enabled  |
    ```
    """
    if asm_config._asm_can_be_enabled and "asm" in features:
        # Tracer is a parameter for testing propose
        # Import tracer here to avoid a circular import
        if test_tracer is None:
            from ddtrace.trace import tracer
        else:
            tracer = test_tracer

        log.debug("[%s][P: %s] ASM_FEATURES: %s", os.getpid(), os.getppid(), str(features)[:100])
        if features is False:
            rc_asm_enabled = False
        else:
            rc_asm_enabled = features.get("asm", {}).get("enabled", False)

        log.debug("APPSEC_ENABLED: %s", rc_asm_enabled)
        if rc_asm_enabled is not None:
            log.debug(
                "[%s][P: %s] Updating ASM Remote Configuration ASM_FEATURES: %s",
                os.getpid(),
                os.getppid(),
                rc_asm_enabled,
            )

            if rc_asm_enabled:
                if not asm_config._asm_enabled:
                    tracer._configure(appsec_enabled=True)
                else:
                    asm_config._asm_enabled = True
            else:
                if asm_config._asm_enabled:
                    tracer._configure(appsec_enabled=False)
                else:
                    asm_config._asm_enabled = False


def _appsec_auto_user_mode(features: Mapping[str, Any], test_tracer: Optional[Tracer] = None) -> None:
    """
    Update Auto User settings from remote config
    """
    asm_config._auto_user_instrumentation_rc_mode = features.get("auto_user_instrum", {}).get("mode", None)
