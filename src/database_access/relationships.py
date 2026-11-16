"""Infers links between tables that the database never declared."""
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

MAX_SUGGESTIONS = 200
HIGH, MEDIUM, LOW = "alta", "média", "baixa"
MAX_TABLES_PER_KEY_NAME = 3

NUMBER_TYPES = ("int", "smallint", "bigint", "integer", "decimal", "numeric", "dec", "real", "double", "float")
TEXT_TYPES = ("char", "varchar", "character", "text", "graphic", "vargraphic", "nchar", "nvarchar")
TIME_TYPES = ("date", "time", "timestamp")


@dataclass(frozen=True)
class ColumnInfo:
    schema: str
    table: str
    column: str
    data_type: str
    length: int | None = None
    text: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.schema, self.table, self.column)


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", plain.lower()).strip()


def type_family(data_type: str) -> str:
    base = re.split(r"[\s(]", data_type.strip().lower(), maxsplit=1)[0]
    if base.startswith(NUMBER_TYPES):
        return "number"
    if base.startswith(TEXT_TYPES):
        return "text"
    if base.startswith(TIME_TYPES):
        return base
    return base


def _compatible(column: ColumnInfo, key: ColumnInfo) -> bool:
    if type_family(column.data_type) != type_family(key.data_type):
        return False
    if type_family(column.data_type) == "text" and column.length and key.length:
        return column.length == key.length
    return True


def _qualified(info: ColumnInfo) -> str:
    return f"{info.schema}.{info.table}"


def _suggestion(column: ColumnInfo, key: ColumnInfo, confidence: str, evidence: str) -> dict[str, Any]:
    return {
        "from_table": _qualified(column),
        "from_column": column.column,
        "to_table": _qualified(key),
        "to_column": key.column,
        "confidence": confidence,
        "evidence": evidence,
    }


def _match(column: ColumnInfo, key: ColumnInfo) -> tuple[str, str] | None:
    same_name = column.column.upper() == key.column.upper()
    text = _normalize(column.text)
    same_text = bool(text) and text == _normalize(key.text)
    if same_name and same_text:
        return HIGH, f"mesmo nome e mesma descrição ('{column.text}')"
    if same_name:
        return MEDIUM, "mesmo nome de coluna"
    if column.column.lower() == f"{key.table}_{key.column}".lower():
        return MEDIUM, f"nome segue o padrão {key.table}_{key.column}"
    if same_text:
        return LOW, f"mesma descrição ('{column.text}')"
    return None


def suggest_relationships(
    columns: list[ColumnInfo],
    keys: list[ColumnInfo],
    declared: set[tuple[str, str, str]],
) -> dict[str, Any]:
    """Columns that likely reference another table's single-column key."""
    tables_per_name: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for key in keys:
        tables_per_name[key.column.upper()].add((key.schema, key.table))
    usable_keys = [
        key for key in keys if len(tables_per_name[key.column.upper()]) <= MAX_TABLES_PER_KEY_NAME
    ]
    key_columns = {key.key for key in keys}

    found: list[dict[str, Any]] = []
    for column in columns:
        if column.key in declared or column.key in key_columns:
            continue
        for key in usable_keys:
            if (key.schema, key.table) == (column.schema, column.table):
                continue
            matched = _match(column, key)
            if matched is None or not _compatible(column, key):
                continue
            found.append(_suggestion(column, key, *matched))

    order = {HIGH: 0, MEDIUM: 1, LOW: 2}
    found.sort(key=lambda item: (order[item["confidence"]], item["from_table"], item["from_column"]))
    payload: dict[str, Any] = {
        "inferred": found[:MAX_SUGGESTIONS],
        "count": min(len(found), MAX_SUGGESTIONS),
        "note": (
            "Vínculos INFERIDOS por nome ou descrição de coluna, sem chave estrangeira "
            "declarada no banco. Apresente-os como hipóteses e confirme no código da "
            "aplicação antes de afirmar."
        ),
    }
    if len(found) > MAX_SUGGESTIONS:
        payload["truncated"] = True
    return payload
