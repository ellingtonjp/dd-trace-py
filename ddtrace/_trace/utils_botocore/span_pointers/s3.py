from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import NamedTuple
from typing import Optional

from ddtrace._trace._span_pointer import _SpanPointerDescription
from ddtrace._trace._span_pointer import _SpanPointerDirection
from ddtrace._trace._span_pointer import _standard_hashing_function
from ddtrace._trace.utils_botocore.span_pointers.telemetry import record_span_pointer_calculation_issue
from ddtrace.internal.logger import get_logger


log = get_logger(__name__)


def _extract_span_pointers_for_s3_response(
    operation_name: str,
    request_parameters: Dict[str, Any],
    response: Dict[str, Any],
) -> List[_SpanPointerDescription]:
    if operation_name in ("PutObject", "CompleteMultipartUpload"):
        return _extract_span_pointers_for_s3_response_with_helper(
            operation_name,
            _AWSS3ObjectHashingProperties.for_put_object_or_complete_multipart_upload,
            request_parameters,
            response,
        )

    if operation_name == "CopyObject":
        return _extract_span_pointers_for_s3_response_with_helper(
            operation_name,
            _AWSS3ObjectHashingProperties.for_copy_object,
            request_parameters,
            response,
        )

    return []


class _AWSS3ObjectHashingProperties(NamedTuple):
    bucket: str
    key: str
    etag: str

    @staticmethod
    def for_put_object_or_complete_multipart_upload(
        request_parameters: Dict[str, Any], response: Dict[str, Any]
    ) -> "_AWSS3ObjectHashingProperties":
        # Endpoint References:
        # https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html
        # https://docs.aws.amazon.com/AmazonS3/latest/API/API_CompleteMultipartUpload.html
        return _AWSS3ObjectHashingProperties(
            bucket=request_parameters["Bucket"],
            key=request_parameters["Key"],
            etag=response["ETag"],
        )

    @staticmethod
    def for_copy_object(
        request_parameters: Dict[str, Any], response: Dict[str, Any]
    ) -> "_AWSS3ObjectHashingProperties":
        # Endpoint References:
        # https://docs.aws.amazon.com/AmazonS3/latest/API/API_CopyObject.html
        return _AWSS3ObjectHashingProperties(
            bucket=request_parameters["Bucket"],
            key=request_parameters["Key"],
            etag=response["CopyObjectResult"]["ETag"],
        )


def _extract_span_pointers_for_s3_response_with_helper(
    operation_name: str,
    extractor: Callable[[Dict[str, Any], Dict[str, Any]], _AWSS3ObjectHashingProperties],
    request_parameters: Dict[str, Any],
    response: Dict[str, Any],
) -> List[_SpanPointerDescription]:
    operation = f"S3.{operation_name}"

    try:
        hashing_properties = extractor(request_parameters, response)
        bucket = hashing_properties.bucket
        key = hashing_properties.key
        etag = hashing_properties.etag

        # The ETag is surrounded by double quotes for some reason sometimes.
        if etag.startswith('"') and etag.endswith('"'):
            etag = etag[1:-1]

    except Exception as e:
        log.debug(
            "problem with parameters for %s span pointer: %s",
            operation,
            e,
        )
        record_span_pointer_calculation_issue(operation=operation, issue_tag="request_parameters")
        return []

    span_pointer_description = _aws_s3_object_span_pointer_description(
        operation=operation,
        pointer_direction=_SpanPointerDirection.DOWNSTREAM,
        bucket=bucket,
        key=key,
        etag=etag,
    )
    if span_pointer_description is None:
        return []

    return [span_pointer_description]


def _aws_s3_object_span_pointer_description(
    operation: str,
    pointer_direction: _SpanPointerDirection,
    bucket: str,
    key: str,
    etag: str,
) -> Optional[_SpanPointerDescription]:
    pointer_hash = _aws_s3_object_span_pointer_hash(operation, bucket, key, etag)
    if pointer_hash is None:
        return None

    return _SpanPointerDescription(
        pointer_kind="aws.s3.object",
        pointer_direction=pointer_direction,
        pointer_hash=pointer_hash,
        extra_attributes={},
    )


def _aws_s3_object_span_pointer_hash(operation: str, bucket: str, key: str, etag: str) -> Optional[str]:
    if '"' in etag:
        # Some AWS API endpoints put the ETag in double quotes. We expect the
        # calling code to have correctly fixed this already.
        log.debug(
            "ETag should not have double quotes: %s",
            etag,
        )
        record_span_pointer_calculation_issue(operation=operation, issue_tag="etag_quotes")
        return None

    try:
        return _standard_hashing_function(
            bucket.encode("ascii"),
            key.encode("utf-8"),
            etag.encode("ascii"),
        )

    except Exception as e:
        log.debug(
            "failed to hash S3 object span pointer: %s",
            e,
        )
        record_span_pointer_calculation_issue(operation=operation, issue_tag="hashing")
        return None
