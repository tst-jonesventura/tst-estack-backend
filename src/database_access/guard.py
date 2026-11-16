"""Engine-independent guards for handing a project database to a model."""
import datetime
import decimal
import json
import re
import uuid
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from utils.ttl_cache import TTLCache

DEFAULT_QUERY_ROWS = 50
MAX_QUERY_ROWS = 200
DEFAULT_SAMPLE_ROWS = 10
MAX_SAMPLE_ROWS = 20
MAX_CELL_CHARS = 500
MAX_RESPONSE_BYTES = 64_000
MAX_SQL_CHARS = 20_000

MASK = "[mascarado]"

POSTURE_TTL_SECONDS = 10 * 60
_posture_cache: TTLCache[Any] = TTLCache(POSTURE_TTL_SECONDS)

SCHEMA_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")

SENSITIVE_NAME_FRAGMENTS = (
    "password",
    "passwd",
    "senha",
    "secret",
    "segredo",
    "token",
    "apikey",
    "api_key",
    "private_key",
    "chave_privada",
    "credential",
    "credencial",
    "email",
    "e_mail",
    "telefone",
    "phone",
    "celular",
    "whatsapp",
    "cpf",
    "cnpj",
    "cartao",
    "credit_card",
    "card_number",
    "cvv",
    "nascimento",
    "birth",
    "endereco",
    "address",
    "logradouro",
    "passaporte",
    "passport",
)
SENSITIVE_NAME_TOKENS = frozenset(
    {"rg", "pix", "pwd", "cep", "ssn", "card", "hash", "salt", "iban", "dob", "otp"}
)

WHITESPACE_PATTERN = re.compile(r"(\s+)")
CPF_PATTERN = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
CNPJ_PATTERN = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")

DOLLAR_QUOTE_TAG = re.compile(r"\$([A-Za-z_]\w*)?\$")
Q_QUOTE_START = re.compile(r"[nN]?[qQ]'")
Q_QUOTE_CLOSERS = {"[": "]", "{": "}", "(": ")", "<": ">"}
LITERAL_FILLER = "#"
CALL_PREFIX = r'(?<![\w$])["\[]?('
IDENTIFIER_NEUTRAL = str.maketrans({";": LITERAL_FILLER, "(": LITERAL_FILLER, ")": LITERAL_FILLER})
QUOTED_IDENTIFIER = re.compile(r'"[^"]*(?:""[^"]*)*"')
BRACKETED_IDENTIFIER = re.compile(r"\[[^\]]*(?:\]\][^\]]*)*\]")
PLAIN_QUALIFIER = re.compile(r'(?<![\w$"\]#])([A-Za-z_][\w$]*)\s*\.')
FOLLOWED_BY_DOT = re.compile(r"\s*\.")


class DatabaseAccessError(Exception):
    """A refusal or failure whose message is safe to show the model."""


@dataclass(frozen=True)
class Dialect:
    """How one engine's SQL is lexed, bounded and screened."""

    name: str
    blocked_functions: tuple[str, ...]
    system_schemas: tuple[str, ...]
    blocked_schemas: tuple[str, ...] = ()
    blocked_prefixes: tuple[str, ...] = ()
    blocked_relations: tuple[str, ...] = ()
    query_keywords: tuple[str, ...] = ("select", "with", "values")
    dollar_quotes: bool = False
    escape_strings: bool = False
    unicode_escapes: bool = False
    q_quotes: bool = False
    bracket_identifiers: bool = False
    national_strings: bool = False
    folds_to_upper: bool = False
    row_limit: str = "LIMIT {rows}"
    limit_position: str = "suffix"
    subquery_alias: str = "AS estack_q"
    refused_patterns: tuple[tuple[str, str], ...] = ()

    def fold(self, name: str) -> str:
        return name.upper() if self.folds_to_upper else name.lower()


@dataclass(frozen=True)
class DatabaseTarget:
    """Where one project database lives, plus the schemas the agent may read."""

    alias: str
    host: str
    port: int
    database: str
    username: str
    password: str = field(repr=False)
    ssl_mode: str = "require"
    schemas: tuple[str, ...] = ()
    engine: str = "postgres"
    options: tuple[tuple[str, str], ...] = ()

    @property
    def cache_key(self) -> tuple:
        return (
            self.engine,
            self.host.lower(),
            self.port,
            self.database,
            self.username,
            self.schemas,
            self.options,
        )

    @property
    def schema_list(self) -> list[str] | None:
        return list(self.schemas) or None

    def option(self, key: str, default: str | None = None) -> str | None:
        return dict(self.options).get(key, default)


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool = False
    source_columns: list[str | None] = field(default_factory=list)

    def records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


# region Masking and shaping
def _name_tokens(column: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", column.lower()) if token]


def is_sensitive_column(column: str | None) -> bool:
    if not column:
        return False
    lowered = column.lower()
    compact = re.sub(r"[^a-z0-9_]", "_", lowered)
    if any(fragment in compact for fragment in SENSITIVE_NAME_FRAGMENTS):
        return True
    return any(token in SENSITIVE_NAME_TOKENS for token in _name_tokens(lowered))


def _is_email(candidate: str) -> bool:
    local, at, domain = candidate.partition("@")
    labels = domain.split(".")
    return bool(at and local) and "@" not in domain and len(labels) > 1 and all(labels)


def _mask_email_token(token: str) -> str:
    if "@" not in token:
        return token
    start, end = 0, len(token)
    while start < end and not token[start].isalnum():
        start += 1
    while end > start and not token[end - 1].isalnum():
        end -= 1
    if not _is_email(token[start:end]):
        return token
    return f"{token[:start]}{MASK}{token[end:]}"


def redact_emails(value: str) -> str:
    return "".join(_mask_email_token(part) for part in WHITESPACE_PATTERN.split(value))


def redact_text(value: str) -> str:
    value = redact_emails(value)
    value = CPF_PATTERN.sub(MASK, value)
    return CNPJ_PATTERN.sub(MASK, value)


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, (datetime.timedelta, uuid.UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binário, {len(bytes(value))} bytes>"
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return str(value)


def shape_cell(column: str, value: Any, source_column: str | None = None) -> Any:
    """Mask, redact and trim one value before it can reach the model."""
    if value is None:
        return None
    if is_sensitive_column(column) or is_sensitive_column(source_column):
        return MASK
    value = to_jsonable(value)
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, str):
        value = redact_text(value)
        if len(value) > MAX_CELL_CHARS:
            value = f"{value[:MAX_CELL_CHARS]}… (+{len(value) - MAX_CELL_CHARS} caracteres)"
    return value


def shape_rows(result: QueryResult, sensitive: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Masked rows, cut at the response budget; ``sensitive`` adds columns to mask."""
    rows: list[list[Any]] = []
    size = len(json.dumps(result.columns, ensure_ascii=False))
    truncated = result.truncated
    sources = result.source_columns or [None] * len(result.columns)
    for row in result.rows:
        shaped = [
            MASK if column in sensitive and cell is not None else shape_cell(column, cell, source)
            for column, cell, source in zip(result.columns, row, sources)
        ]
        size += len(json.dumps(shaped, ensure_ascii=False, default=str))
        if size > MAX_RESPONSE_BYTES:
            truncated = True
            break
        rows.append(shaped)
    payload: dict[str, Any] = {"columns": result.columns, "rows": rows, "row_count": len(rows)}
    if truncated:
        payload["truncated"] = True
        payload["note"] = (
            "Resultado cortado pelo limite de linhas ou de tamanho. Refine com "
            "filtros, agregações ou menos colunas em vez de pedir tudo."
        )
    return payload


def shape_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """JSON-safe catalog rows. Catalog text is schema, not data, so it is not masked."""
    return [{key: to_jsonable(value) for key, value in record.items()} for record in records]


# endregion


# region SQL validation
def _skip_quoted(sql: str, index: int, quote: str) -> int:
    index += 1
    while index < len(sql):
        if sql[index] == quote:
            if index + 1 < len(sql) and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    raise DatabaseAccessError("A consulta tem uma string ou identificador sem fechamento.")


def _skip_escape_string(sql: str, index: int) -> int:
    index += 2
    while index < len(sql):
        char = sql[index]
        if char == "\\":
            index += 2
            continue
        if char == "'":
            if index + 1 < len(sql) and sql[index + 1] == "'":
                index += 2
                continue
            return index + 1
        index += 1
    raise DatabaseAccessError("A consulta tem uma string sem fechamento.")


def _skip_block_comment(sql: str, index: int) -> int:
    depth = 0
    while index < len(sql):
        if sql.startswith("/*", index):
            depth += 1
            index += 2
        elif sql.startswith("*/", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index
        else:
            index += 1
    raise DatabaseAccessError("A consulta tem um comentário sem fechamento.")


def _skip_dollar_quoted(sql: str, index: int) -> int | None:
    match = DOLLAR_QUOTE_TAG.match(sql, index)
    if match is None:
        return None
    tag = match.group(0)
    end = sql.find(tag, match.end())
    if end < 0:
        raise DatabaseAccessError("A consulta tem um bloco $$ sem fechamento.")
    return end + len(tag)


def _skip_q_quoted(sql: str, index: int) -> int | None:
    match = Q_QUOTE_START.match(sql, index)
    if match is None or match.end() >= len(sql):
        return None
    opener = sql[match.end()]
    if opener.isspace():
        raise DatabaseAccessError("A consulta tem um literal q'...' com delimitador inválido.")
    closer = Q_QUOTE_CLOSERS.get(opener, opener) + "'"
    end = sql.find(closer, match.end() + 1)
    if end < 0:
        raise DatabaseAccessError("A consulta tem um literal q'...' sem fechamento.")
    return end + len(closer)


def _skip_bracketed(sql: str, index: int) -> int:
    index += 1
    while index < len(sql):
        if sql[index] == "]":
            if sql.startswith("]]", index):
                index += 2
                continue
            return index + 1
        index += 1
    raise DatabaseAccessError("A consulta tem um identificador [ sem fechamento.")


def _follows_identifier(sql: str, index: int) -> bool:
    return index > 0 and (sql[index - 1].isalnum() or sql[index - 1] in "_$")


def _is_national_string_start(sql: str, index: int) -> bool:
    if sql[index] not in "nN" or not sql.startswith("'", index + 1):
        return False
    return not _follows_identifier(sql, index)


def _is_escape_string_start(sql: str, index: int) -> bool:
    if sql[index] not in "eE" or not sql.startswith("'", index + 1):
        return False
    return not _follows_identifier(sql, index)


def _comment_end(sql: str, index: int, dialect: Dialect) -> int | None:
    if sql.startswith("--", index):
        newline = sql.find("\n", index)
        return len(sql) if newline < 0 else newline
    if sql.startswith("/*", index):
        return _skip_block_comment(sql, index)
    return None


def _prefixed_string_end(sql: str, index: int, dialect: Dialect) -> int | None:
    if dialect.escape_strings and _is_escape_string_start(sql, index):
        return _skip_escape_string(sql, index)
    if dialect.q_quotes and sql[index] in "nNqQ" and not _follows_identifier(sql, index):
        end = _skip_q_quoted(sql, index)
        if end is not None:
            return end
    if dialect.national_strings and _is_national_string_start(sql, index):
        return _skip_quoted(sql, index + 1, "'")
    return None


def _quoted_end(sql: str, index: int, dialect: Dialect) -> int | None:
    char = sql[index]
    if dialect.bracket_identifiers and char == "[":
        return _skip_bracketed(sql, index)
    if char in "'\"":
        return _skip_quoted(sql, index, char)
    if dialect.dollar_quotes and char == "$" and not _follows_identifier(sql, index):
        return _skip_dollar_quoted(sql, index)
    return None


LITERAL_SCANNERS = (_comment_end, _prefixed_string_end, _quoted_end)


def _skip_literal(sql: str, index: int, dialect: Dialect) -> int | None:
    """End of the literal or comment starting at ``index``, or None if there is none."""
    for scanner in LITERAL_SCANNERS:
        end = scanner(sql, index, dialect)
        if end is not None:
            return end
    return None


def _blank(sql: str, start: int, end: int) -> str:
    if sql[start] in '"[':
        return sql[start:end].translate(IDENTIFIER_NEUTRAL)
    if sql.startswith(("--", "/*"), start):
        return " " * (end - start)
    return LITERAL_FILLER * (end - start)


def code_skeleton(sql: str, dialect: Dialect) -> str:
    """The query with literals and comments blanked out, same length as the input."""
    parts: list[str] = []
    index = 0
    while index < len(sql):
        end = _skip_literal(sql, index, dialect)
        if end is None:
            parts.append(sql[index])
            index += 1
            continue
        parts.append(_blank(sql, index, end))
        index = end
    return "".join(parts)


def _check_parentheses(skeleton: str) -> None:
    depth = 0
    for char in skeleton:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise DatabaseAccessError(
                    "A consulta fecha mais parênteses do que abre."
                )
    if depth != 0:
        raise DatabaseAccessError("A consulta tem parênteses desbalanceados.")


@cache
def _blocked_call_pattern(dialect: Dialect) -> re.Pattern[str]:
    names = [re.escape(name) for name in dialect.blocked_functions]
    names += [re.escape(prefix) + r"[\w$]*" for prefix in dialect.blocked_prefixes]
    return re.compile(CALL_PREFIX + "|".join(names) + r')["\]]?\s*\(', re.IGNORECASE)


@cache
def _blocked_schema_pattern(dialect: Dialect) -> re.Pattern[str] | None:
    if not dialect.blocked_schemas:
        return None
    schemas = "|".join(re.escape(schema) for schema in dialect.blocked_schemas)
    return re.compile(
        CALL_PREFIX + schemas + r')["\]]?\s*\.\s*["\[]?[\w$]+["\]]?\s*\(', re.IGNORECASE
    )


def _check_blocked_calls(skeleton: str, dialect: Dialect) -> None:
    blocked = _blocked_call_pattern(dialect).search(skeleton)
    if blocked:
        raise DatabaseAccessError(
            f"A função '{blocked.group(1)}' não é permitida em consultas de leitura."
        )
    schema_pattern = _blocked_schema_pattern(dialect)
    blocked = schema_pattern.search(skeleton) if schema_pattern else None
    if blocked:
        raise DatabaseAccessError(
            f"Funções do schema '{blocked.group(1)}' não são permitidas em consultas de leitura."
        )


@cache
def _blocked_relation_pattern(dialect: Dialect) -> re.Pattern[str] | None:
    if not dialect.blocked_relations:
        return None
    names = "|".join(re.escape(name) for name in dialect.blocked_relations)
    return re.compile(CALL_PREFIX + names + r')["\]]?(?![\w$])', re.IGNORECASE)


def _check_blocked_relations(skeleton: str, dialect: Dialect) -> None:
    pattern = _blocked_relation_pattern(dialect)
    blocked = pattern.search(skeleton) if pattern else None
    if blocked:
        raise DatabaseAccessError(
            f"O objeto '{blocked.group(1)}' guarda credenciais e não pode ser consultado."
        )


UNICODE_ESCAPE = re.compile(r"(?i)(?<![\w$])u&[#\"]")


def _check_unicode_escapes(skeleton: str, dialect: Dialect) -> None:
    if dialect.unicode_escapes and UNICODE_ESCAPE.search(skeleton):
        raise DatabaseAccessError(
            "Literais e identificadores com escape Unicode (U&'...') não são aceitos."
        )


@cache
def _refused_patterns(dialect: Dialect) -> tuple[tuple[re.Pattern[str], str], ...]:
    return tuple((re.compile(pattern, re.IGNORECASE), message) for pattern, message in dialect.refused_patterns)


def _check_refused_patterns(skeleton: str, dialect: Dialect) -> None:
    for pattern, message in _refused_patterns(dialect):
        found = pattern.search(skeleton)
        if found:
            raise DatabaseAccessError(message.format(match=found.group(0).strip()))


def _check_starts_with_query(skeleton: str, dialect: Dialect) -> None:
    first = skeleton.lstrip(" (\t\r\n").split(None, 1)
    keyword = first[0].lower() if first else ""
    if keyword not in dialect.query_keywords:
        allowed = " ou ".join(word.upper() for word in dialect.query_keywords)
        raise DatabaseAccessError(
            f"Só são aceitas consultas de leitura que começam com {allowed}; "
            f"'{keyword.upper() or '?'}' não é permitido."
        )


def _unquote_identifier(name: str, dialect: Dialect) -> str:
    if name.startswith('"'):
        return name[1:-1].replace('""', '"')
    if name.startswith("["):
        return name[1:-1].replace("]]", "]")
    return dialect.fold(name)


def _qualified_names(skeleton: str) -> list[str]:
    """Raw qualifier tokens; quoted ones are blanked first so names inside them are not misread."""
    names: list[str] = []
    remaining = skeleton
    for pattern in (QUOTED_IDENTIFIER, BRACKETED_IDENTIFIER):
        names += [
            match.group(0)
            for match in pattern.finditer(remaining)
            if FOLLOWED_BY_DOT.match(remaining, match.end())
        ]
        remaining = pattern.sub(lambda match: LITERAL_FILLER * len(match.group(0)), remaining)
    return names + [match.group(1) for match in PLAIN_QUALIFIER.finditer(remaining)]


def schema_qualifiers(sql: str, dialect: Dialect) -> set[str]:
    """Every name used as a qualifier in the query, outside literals and comments."""
    try:
        skeleton = code_skeleton(sql or "", dialect)
    except DatabaseAccessError:
        return set()
    return {_unquote_identifier(name, dialect) for name in _qualified_names(skeleton)}


def _check_schema_scope(
    skeleton: str, allowed: tuple[str, ...], known_schemas: set[str], dialect: Dialect
) -> None:
    if not allowed:
        return
    permitted = {
        variant
        for name in (*allowed, *dialect.system_schemas)
        for variant in (name, dialect.fold(name))
    }
    for raw in _qualified_names(skeleton):
        name = _unquote_identifier(raw, dialect)
        if name in known_schemas and name not in permitted:
            raise DatabaseAccessError(
                f"O schema '{name}' está fora dos schemas liberados para este "
                f"projeto ({', '.join(allowed)})."
            )


def prepare_readonly_query(
    sql: str,
    limit: int,
    *,
    dialect: Dialect,
    allowed_schemas: tuple[str, ...] = (),
    known_schemas: set[str] | None = None,
) -> str:
    """Wrap a model-written query so it can only ever be one bounded SELECT."""
    text = (sql or "").strip()
    if not text:
        raise DatabaseAccessError("Consulta vazia.")
    if len(text) > MAX_SQL_CHARS:
        raise DatabaseAccessError(
            f"Consulta longa demais (máximo de {MAX_SQL_CHARS} caracteres)."
        )
    skeleton = code_skeleton(text, dialect).rstrip()
    while skeleton.endswith(";"):
        skeleton = skeleton[:-1].rstrip()
    text = text[: len(skeleton)]
    if ";" in skeleton:
        raise DatabaseAccessError(
            "Envie uma única consulta SELECT, sem ';' separando instruções."
        )
    _check_starts_with_query(skeleton, dialect)
    _check_unicode_escapes(skeleton, dialect)
    _check_parentheses(skeleton)
    _check_blocked_calls(skeleton, dialect)
    _check_blocked_relations(skeleton, dialect)
    _check_refused_patterns(skeleton, dialect)
    _check_schema_scope(skeleton, allowed_schemas, known_schemas or set(), dialect)
    bounded = max(1, min(int(limit), MAX_QUERY_ROWS))
    row_limit = dialect.row_limit.format(rows=bounded + 1)
    if dialect.limit_position == "prefix":
        return f"SELECT {row_limit} * FROM (\n{text}\n) {dialect.subquery_alias}"
    return f"SELECT * FROM (\n{text}\n) {dialect.subquery_alias} {row_limit}"


# endregion
