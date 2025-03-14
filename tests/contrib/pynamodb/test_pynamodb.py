from moto import mock_dynamodb
from moto.dynamodb import dynamodb_backend
import pynamodb.connection.base
from pynamodb.connection.base import Connection
import pytest

from ddtrace.contrib.internal.pynamodb.patch import patch
from ddtrace.contrib.internal.pynamodb.patch import unpatch
from ddtrace.internal.schema import DEFAULT_SPAN_SERVICE_NAME
from ddtrace.trace import Pin
from tests.utils import TracerTestCase
from tests.utils import assert_is_measured


class PynamodbTest(TracerTestCase):
    TEST_SERVICE = "pynamodb"

    def setUp(self):
        patch()

        self.conn = Connection(region="us-east-1")
        self.conn.session.set_credentials("aws-access-key", "aws-secret-access-key", "session-token")

        super(PynamodbTest, self).setUp()
        Pin._override(self.conn, tracer=self.tracer)

    def tearDown(self):
        super(PynamodbTest, self).tearDown()
        unpatch()

    @mock_dynamodb
    def test_list_tables(self):
        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        list_result = self.conn.list_tables()

        spans = self.get_spans()

        assert spans
        span = spans[0]

        assert span.name == "pynamodb.command"
        assert span.service == "pynamodb"
        assert span.resource == "ListTables"
        assert len(spans) == 1
        assert_is_measured(span)
        assert span.span_type == "http"
        assert span.get_tag("aws.operation") == "ListTables"
        assert span.get_tag("aws.region") == "us-east-1"
        assert span.get_tag("region") == "us-east-1"
        assert span.get_tag("aws.agent") == "pynamodb"
        assert span.get_tag("component") == "pynamodb"
        assert span.get_tag("span.kind") == "client"
        assert span.get_tag("db.system") == "dynamodb"
        assert span.duration >= 0
        assert span.error == 0

        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"

    @mock_dynamodb
    def test_delete_table(self):
        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        delete_result = self.conn.delete_table("Test")
        spans = self.get_spans()

        assert spans
        span = spans[0]

        assert span.name == "pynamodb.command"
        assert span.service == "pynamodb"
        assert span.resource == "DeleteTable Test"
        assert len(spans) == 1
        assert_is_measured(span)
        assert span.span_type == "http"
        assert span.get_tag("aws.operation") == "DeleteTable"
        assert span.get_tag("aws.region") == "us-east-1"
        assert span.get_tag("region") == "us-east-1"
        assert span.get_tag("aws.agent") == "pynamodb"
        assert span.get_tag("component") == "pynamodb"
        assert span.get_tag("span.kind") == "client"
        assert span.get_tag("db.system") == "dynamodb"
        assert span.duration >= 0
        assert span.error == 0

        assert delete_result["Table"]["TableName"] == "Test"
        assert len(self.conn.list_tables()["TableNames"]) == 0

    @mock_dynamodb
    def test_scan(self):
        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        scan_result = self.conn.scan("Test")
        spans = self.get_spans()

        assert spans
        span = spans[0]

        assert span.name == "pynamodb.command"
        assert span.service == "pynamodb"
        assert span.resource == "Scan Test"
        assert len(spans) == 1
        assert_is_measured(span)
        assert span.span_type == "http"
        assert span.get_tag("aws.operation") == "Scan"
        assert span.get_tag("aws.region") == "us-east-1"
        assert span.get_tag("region") == "us-east-1"
        assert span.get_tag("aws.agent") == "pynamodb"
        assert span.get_tag("component") == "pynamodb"
        assert span.get_tag("span.kind") == "client"
        assert span.get_tag("db.system") == "dynamodb"
        assert span.duration >= 0
        assert span.error == 0

        assert scan_result["ScannedCount"] == 0
        assert len(scan_result["Items"]) == 0

    @mock_dynamodb
    def test_scan_on_error(self):
        with pytest.raises(pynamodb.exceptions.ScanError):
            self.conn.scan("OtherTable")

        spans = self.get_spans()
        assert spans
        span = spans[0]
        assert span.name == "pynamodb.command"
        assert span.service == "pynamodb"
        assert span.resource == "Scan OtherTable"
        assert len(spans) == 1
        assert_is_measured(span)
        assert span.span_type == "http"
        assert span.get_tag("aws.operation") == "Scan"
        assert span.get_tag("aws.region") == "us-east-1"
        assert span.get_tag("region") == "us-east-1"
        assert span.get_tag("aws.agent") == "pynamodb"
        assert span.get_tag("component") == "pynamodb"
        assert span.get_tag("span.kind") == "client"
        assert span.get_tag("db.system") == "dynamodb"
        assert span.duration >= 0
        assert span.error == 1
        assert span.get_tag("error.type") != ""

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc"))
    @mock_dynamodb
    def test_schematized_service_default(self):
        from ddtrace import config

        assert config.service == "mysvc"

        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        list_result = self.conn.list_tables()

        span = self.get_spans()[0]
        assert span.service == "pynamodb", "Expected 'pynamodb', got %s" % span.service
        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    @mock_dynamodb
    def test_schematized_service_v0(self):
        from ddtrace import config

        assert config.service == "mysvc"

        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        list_result = self.conn.list_tables()

        span = self.get_spans()[0]
        assert span.service == "pynamodb", "Expected 'pynamodb', got %s" % span.service
        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    @mock_dynamodb
    def test_schematized_service_v1(self):
        from ddtrace import config

        assert config.service == "mysvc"

        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        list_result = self.conn.list_tables()

        span = self.get_spans()[0]
        assert span.service == "mysvc", "Expected 'mysvc', got %s" % span.service
        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"

    @TracerTestCase.run_in_subprocess(env_overrides=dict())
    @mock_dynamodb
    def test_schematized_unspecified_service_default(self):
        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        list_result = self.conn.list_tables()

        span = self.get_spans()[0]
        assert span.service == "pynamodb", "Expected 'pynamodb', got %s" % span.service
        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    @mock_dynamodb
    def test_schematized_unspecified_service_v0(self):
        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        list_result = self.conn.list_tables()

        span = self.get_spans()[0]
        assert span.service == "pynamodb", "Expected 'pynamodb', got %s" % span.service
        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    @mock_dynamodb
    def test_schematized_unspecified_service_v1(self):
        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        list_result = self.conn.list_tables()

        span = self.get_spans()[0]
        assert span.service == DEFAULT_SPAN_SERVICE_NAME, (
            "Expected 'internal.schema.DEFAULT_SEVICE_NAME', got %s" % span.service
        )
        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    @mock_dynamodb
    def test_schematized_operation_v0(self):
        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        list_result = self.conn.list_tables()

        span = self.get_spans()[0]
        assert span.name == "pynamodb.command", "Expected 'pynamodb.command', got %s" % span.name
        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    @mock_dynamodb
    def test_schematized_operation_v1(self):
        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        list_result = self.conn.list_tables()

        span = self.get_spans()[0]
        assert span.name == "aws.dynamodb.request", "Expected 'aws.dynamodb.request', got %s" % span.name
        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_PYNAMODB_SERVICE="mypynamodb"))
    @mock_dynamodb
    def test_env_user_specified_pynamodb_service(self):
        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        list_result = self.conn.list_tables()

        span = self.get_spans()[0]

        assert span.service == "mypynamodb", span.service
        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"

        self.reset()

        # Global config
        with self.override_config("pynamodb", dict(service="cfg-pynamodb")):
            dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
            list_result = self.conn.list_tables()
            span = self.get_spans()[0]

            assert span.service == "cfg-pynamodb", span.service
            assert len(list_result["TableNames"]) == 1
            assert list_result["TableNames"][0] == "Test"

        self.reset()

        # Manual override
        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        Pin._override(self.conn, service="mypynamodb", tracer=self.tracer)
        list_result = self.conn.list_tables()
        span = self.get_spans()[0]
        assert span.service == "mypynamodb", span.service
        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="app-svc", DD_PYNAMODB_SERVICE="env-pynamodb"))
    @mock_dynamodb
    def test_service_precedence(self):
        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        list_result = self.conn.list_tables()
        span = self.get_spans()[0]
        assert span.service == "env-pynamodb", span.service
        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"

        self.reset()

        # Manual override
        dynamodb_backend.create_table("Test", hash_key_attr="content", hash_key_type="S")
        Pin._override(self.conn, service="override-pynamodb", tracer=self.tracer)
        list_result = self.conn.list_tables()
        span = self.get_spans()[0]
        assert span.service == "override-pynamodb", span.service
        assert len(list_result["TableNames"]) == 1
        assert list_result["TableNames"][0] == "Test"
