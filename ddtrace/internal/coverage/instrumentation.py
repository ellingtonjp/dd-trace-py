import sys


# Import are noqa'd otherwise some formatters will helpfully remove them
if sys.version_info >= (3, 13):
    from ddtrace.internal.coverage.instrumentation_py3_13 import instrument_all_lines  # noqa
elif sys.version_info >= (3, 12):
    from ddtrace.internal.coverage.instrumentation_py3_12 import instrument_all_lines  # noqa
elif sys.version_info >= (3, 11):
    from ddtrace.internal.coverage.instrumentation_py3_11 import instrument_all_lines  # noqa
elif sys.version_info >= (3, 10):
    from ddtrace.internal.coverage.instrumentation_py3_10 import instrument_all_lines  # noqa
else:
    # Python 3.8 and 3.9 use the same instrumentation
    from ddtrace.internal.coverage.instrumentation_py3_8 import instrument_all_lines  # noqa
