"""Every tool against a real Postgres; set ESTACK_TEST_POSTGRES_DSN to enable."""

import os

import pytest
from psycopg2.extensions import parse_dsn

from database_access.engines import postgres as catalog
from database_access.engines.postgres import readonly_session
from database_access.guard import MASK, DatabaseTarget
from database_access.operations import DirectExecutor

DSN = os.environ.get("ESTACK_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="ESTACK_TEST_POSTGRES_DSN not set")


@pytest.fixture(scope="module")
def target() -> DatabaseTarget:
    parts = parse_dsn(DSN)
    return DatabaseTarget(
        alias="live",
        host=parts.get("host", "localhost"),
        port=int(parts.get("port", 5432)),
        database=parts["dbname"],
        username=parts["user"],
        password=parts.get("password", ""),
        ssl_mode=parts.get("sslmode", "prefer"),
    )


def _run(target, name, **args):
    return DirectExecutor((target,)).run(None, name, args)


def _query(target, sql, limit=5):
    return _run(target, "run_readonly_query", sql=sql, limit=limit)


@pytest.fixture(scope="module")
def first_table(target):
    with readonly_session(target) as session:
        listed = catalog.list_tables(session, None, None, None)["items"]
    tables = [item for item in listed if item["kind"] in ("table", "partitioned table")]
    if not tables:
        pytest.skip("database has no tables")
    return tables[0]


class TestCatalogAgainstPostgres:
    def test_overview(self, target):
        overview = _run(target, "database_overview")

        assert "error" not in overview, overview
        assert overview["schemas"]

    def test_relationships(self, target):
        assert "error" not in _run(target, "list_relationships")

    def test_rules(self, target):
        assert "error" not in _run(target, "list_rules")

    def test_routines(self, target):
        assert "error" not in _run(target, "list_routines")

    def test_describe_profile_and_sample(self, target, first_table):
        name, schema = first_table["name"], first_table["schema"]

        described = _run(target, "describe_table", table=name, schema=schema)
        profile = _run(target, "column_profile", table=name, schema=schema)
        sample = _run(target, "sample_rows", table=name, schema=schema, limit=3)

        assert "error" not in described, described
        assert described["columns"]
        assert "error" not in profile, profile
        assert "error" not in sample, sample
        assert len(sample["rows"]) <= 3


class TestGuardsAgainstPostgres:
    def test_select_works_and_masks(self, target):
        result = _query(target, "select 'a@b.com' as email, 1 as n")

        assert result["rows"] == [[MASK, 1]]

    @pytest.mark.parametrize(
        "sql",
        [
            "with d as (delete from pg_catalog.pg_class returning 1) select * from d",
            "insert into estack_nope values (1)",
            "update pg_catalog.pg_class set relname = relname",
            "create table estack_nope (id int)",
            "select 1; select 2",
            "select pg_sleep(30)",
        ],
    )
    def test_writes_and_escapes_are_refused(self, target, sql):
        result = _query(target, sql)

        assert "error" in result, result

    def test_slow_query_hits_the_statement_timeout(self, target):
        result = _query(target, "select count(*) from generate_series(1, 1000000000) a", 1)

        assert "limite" in result.get("error", ""), result

    def test_session_is_read_only(self, target):
        with readonly_session(target) as session:
            row = session.one("select current_setting('transaction_read_only') as ro")

        assert row["ro"] == "on"
