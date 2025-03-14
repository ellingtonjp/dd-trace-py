"""
The Flask__ integration will add tracing to all requests to your Flask application.

This integration will track the entire Flask lifecycle including user-defined endpoints, hooks,
signals, and template rendering.

To configure tracing manually::

    import ddtrace.auto

    from flask import Flask

    app = Flask(__name__)


    @app.route('/')
    def index():
        return 'hello world'


    if __name__ == '__main__':
        app.run()


You may also enable Flask tracing automatically via ddtrace-run::

    ddtrace-run python app.py

Note that if you are using Runtime Code Analysis to detect vulnerabilities (`DD_IAST_ENABLED=1`)
and your main `app.py` file contains code outside the `app.run()` call (e.g. routes or
utility functions) you will need to import and call `ddtrace_iast_flask_patch()` before
the `app.run()` to ensure the code inside the main module is patched to propagation works::

    from flask import Flask
    from ddtrace.appsec._iast import ddtrace_iast_flask_patch

    app = Flask(__name__)

    if __name__ == '__main__':
        ddtrace_iast_flask_patch()
        app.run()


Configuration
~~~~~~~~~~~~~

.. py:data:: ddtrace.config.flask['distributed_tracing_enabled']

   Whether to parse distributed tracing headers from requests received by your Flask app.

   Default: ``True``

.. py:data:: ddtrace.config.flask['service_name']

   The service name reported for your Flask app.

   Can also be configured via the ``DD_SERVICE`` environment variable.

   Default: ``'flask'``

.. py:data:: ddtrace.config.flask['collect_view_args']

   Whether to add request tags for view function argument values.

   Default: ``True``

.. py:data:: ddtrace.config.flask['template_default_name']

   The default template name to use when one does not exist.

   Default: ``<memory>``

.. py:data:: ddtrace.config.flask['trace_signals']

   Whether to trace Flask signals (``before_request``, ``after_request``, etc).

   Default: ``True``


Example::

    from ddtrace import config

    # Enable distributed tracing
    config.flask['distributed_tracing_enabled'] = True

    # Override service name
    config.flask['service_name'] = 'custom-service-name'

.. __: http://flask.pocoo.org/

:ref:`All HTTP tags <http-tagging>` are supported for this integration.

"""
