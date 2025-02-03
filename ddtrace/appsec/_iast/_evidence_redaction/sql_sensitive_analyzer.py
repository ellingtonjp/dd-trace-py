import re

from ddtrace.appsec._iast.constants import DBAPI_MARIADB
from ddtrace.appsec._iast.constants import DBAPI_MYSQL
from ddtrace.appsec._iast.constants import DBAPI_MYSQLDB
from ddtrace.appsec._iast.constants import DBAPI_PSYCOPG
from ddtrace.appsec._iast.constants import DBAPI_PYMYSQL
from ddtrace.appsec._iast.constants import DBAPI_SQLITE
from ddtrace.internal.logger import get_logger


log = get_logger(__name__)


STRING_LITERAL = r"'(?:''|[^'])*'"
POSTGRESQL_ESCAPED_LITERAL = r"\$([^$]*)\$.*?\$\1\$"
MYSQL_STRING_LITERAL = r'"(?:\\\\"|[^"])*"|\'(?:\\\\\'|[^\'])*\''
LINE_COMMENT = r"--.*$"
BLOCK_COMMENT = r"/\*[\s\S]*?\*/"
EXPONENT = r"(?:E[-+]?\\d+[fd]?)?"
INTEGER_NUMBER = r"(?<!\w)\d+"
DECIMAL_NUMBER = r"\d*\.\d+"
HEX_NUMBER = r"x'[0-9a-f]+'|0x[0-9a-f]+"
BIN_NUMBER = r"b'[0-9a-f]+'|0b[0-9a-f]+"
NUMERIC_LITERAL = (
    r"[-+]?(?:" + "|".join([HEX_NUMBER, BIN_NUMBER, DECIMAL_NUMBER + EXPONENT, INTEGER_NUMBER + EXPONENT]) + r")"
)

patterns = {
    DBAPI_MYSQL: re.compile(
        f"({NUMERIC_LITERAL})|({MYSQL_STRING_LITERAL})|({LINE_COMMENT})|({BLOCK_COMMENT})", re.IGNORECASE | re.MULTILINE
    ),
    DBAPI_PSYCOPG: re.compile(
        f"({NUMERIC_LITERAL})|({POSTGRESQL_ESCAPED_LITERAL})|({STRING_LITERAL})|({LINE_COMMENT})|({BLOCK_COMMENT})",
        re.IGNORECASE | re.MULTILINE,
    ),
}
patterns[DBAPI_SQLITE] = patterns[DBAPI_MYSQL]
patterns[DBAPI_MARIADB] = patterns[DBAPI_MYSQL]
patterns[DBAPI_PYMYSQL] = patterns[DBAPI_MYSQL]
patterns[DBAPI_MYSQLDB] = patterns[DBAPI_MYSQL]


def sql_sensitive_analyzer(evidence, name_pattern, value_pattern):
    pattern = patterns.get(evidence.dialect, patterns[DBAPI_MYSQL])
    tokens = []

    regex_result = pattern.search(evidence.value)
    while regex_result is not None:
        start = regex_result.start()
        end = regex_result.end()
        start_char = evidence.value[start]
        if start_char == "'" or start_char == '"':
            start += 1
            end -= 1
        elif end > start + 1:
            next_char = evidence.value[start + 1]
            if start_char == "/" and next_char == "*":
                start += 2
                end -= 2
            elif start_char == "-" and start_char == next_char:
                start += 2
            elif start_char.lower() == "q" and next_char == "'":
                start += 3
                end -= 2
            elif start_char == "$":
                match = regex_result.group(0)
                size = match.find("$", 1) + 1
                if size > 1:
                    start += size
                    end -= size
        tokens.append({"start": start, "end": end})
        regex_result = pattern.search(evidence.value, regex_result.end())
    return tokens
