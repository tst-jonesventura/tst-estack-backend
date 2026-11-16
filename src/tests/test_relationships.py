"""Inference of links a legacy database never declared."""

import pytest

from database_access.relationships import (
    MAX_SUGGESTIONS,
    ColumnInfo,
    suggest_relationships,
    type_family,
)


def col(table, column, data_type="INTEGER", length=None, text=None, schema="LIB"):
    return ColumnInfo(schema=schema, table=table, column=column, data_type=data_type, length=length, text=text)


ORDER_KEY = col("PEDIDO", "NUMPED", text="Numero do pedido")
CUSTOMER_KEY = col("CLIENTE", "CODCLI", text="Codigo do cliente")


def run(columns, keys=(ORDER_KEY, CUSTOMER_KEY), declared=()):
    return suggest_relationships(list(columns), list(keys), set(declared))


class TestMatching:
    def test_same_name_and_description_is_high_confidence(self):
        result = run([col("ITEMPED", "NUMPED", text="Número do pedido")])

        assert result["inferred"] == [
            {
                "from_table": "LIB.ITEMPED",
                "from_column": "NUMPED",
                "to_table": "LIB.PEDIDO",
                "to_column": "NUMPED",
                "confidence": "alta",
                "evidence": "mesmo nome e mesma descrição ('Número do pedido')",
            }
        ]

    def test_same_name_only_is_medium(self):
        result = run([col("ITEMPED", "NUMPED", text="Pedido")])

        assert result["inferred"][0]["confidence"] == "média"

    def test_table_underscore_column_convention_is_medium(self):
        result = run([col("item", "pedido_id")], [col("pedido", "id")])

        assert result["inferred"][0]["confidence"] == "média"
        assert "pedido_id" in result["inferred"][0]["evidence"]

    def test_same_description_only_is_low(self):
        result = run([col("NOTA", "PEDNUM", text="numero do PEDIDO")])

        assert result["inferred"][0]["confidence"] == "baixa"
        assert result["inferred"][0]["to_column"] == "NUMPED"

    def test_incompatible_types_are_ignored(self):
        assert run([col("ITEMPED", "NUMPED", data_type="DATE")])["inferred"] == []

    def test_text_keys_need_the_same_length(self):
        keys = [col("PRODUTO", "CODPRO", data_type="CHAR", length=10)]

        same = run([col("ITEM", "CODPRO", data_type="CHAR", length=10)], keys)
        different = run([col("ITEM", "CODPRO", data_type="CHAR", length=15)], keys)

        assert len(same["inferred"]) == 1
        assert different["inferred"] == []

    def test_declared_foreign_keys_need_no_guess(self):
        result = run([col("PEDIDO", "CODCLI")], declared=[("LIB", "PEDIDO", "CODCLI")])

        assert result["inferred"] == []

    def test_a_key_does_not_point_at_itself_or_its_own_table(self):
        result = run([ORDER_KEY, col("PEDIDO", "NUMPED")])

        assert result["inferred"] == []

    def test_names_keyed_in_many_tables_are_too_ambiguous(self):
        keys = [col(f"T{i}", "SEQ") for i in range(5)]

        assert run([col("ITEM", "SEQ")], keys)["inferred"] == []

    def test_results_are_capped_and_ordered_by_confidence(self):
        columns = [col(f"T{i}", "NUMPED", text="Numero do pedido" if i % 2 else None) for i in range(MAX_SUGGESTIONS + 50)]

        result = run(columns)

        assert result["count"] == MAX_SUGGESTIONS
        assert result["truncated"] is True
        assert result["inferred"][0]["confidence"] == "alta"

    def test_every_answer_says_it_is_an_inference(self):
        assert "INFERIDOS" in run([])["note"]


@pytest.mark.parametrize(
    ("data_type", "family"),
    [
        ("INTEGER", "number"),
        ("numeric(8,0)", "number"),
        ("character varying(60)", "text"),
        ("VARGRAPHIC", "text"),
        ("timestamp without time zone", "timestamp"),
        ("DATE", "date"),
        ("uuid", "uuid"),
    ],
)
def test_type_families(data_type, family):
    assert type_family(data_type) == family
