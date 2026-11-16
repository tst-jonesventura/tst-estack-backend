"""The tutor is told which knowledge sources the project has and to consult them all."""


from agents import tutor_agent
from database.models import ProjectDatabase


def _database(db_session, project, alias, **overrides):
    values = {
        "project_id": project.id,
        "alias": alias,
        "engine": "postgres",
        "host": "db.example.com",
        "port": 5432,
        "database_name": "app",
        "username": "reader",
        "password": "secret",
        "description": None,
        "is_active": True,
    }
    values.update(overrides)
    db_session.add(ProjectDatabase(**values))
    db_session.commit()


class TestSourcesInstructions:
    def test_lists_repositories_and_active_databases(self, db_session, sample_project, sample_project_repository):
        _database(db_session, sample_project, "loja", description="Banco de vendas")
        _database(db_session, sample_project, "erp", engine="db2i")
        _database(db_session, sample_project, "antigo", is_active=False)

        text = tutor_agent.tutor_sources_instructions(db_session, sample_project.id)

        assert "example/test-repo (consulte com `ask_git`)" in text
        assert "`erp` (Db2 for i (IBM i/AS400)); `loja` (PostgreSQL): Banco de vendas" in text
        assert "antigo" not in text

    def test_says_when_a_source_is_not_configured(self, db_session, sample_project):
        text = tutor_agent.tutor_sources_instructions(db_session, sample_project.id)

        assert "Código: nenhum repositório conectado" in text
        assert "Bancos de dados: nenhum banco cadastrado" in text

    def test_other_projects_sources_are_not_listed(self, db_session, sample_project, sample_second_project):
        _database(db_session, sample_second_project, "outro")

        assert "outro" not in tutor_agent.tutor_sources_instructions(db_session, sample_project.id)

    def test_asks_to_consult_every_source_in_parallel_unless_restricted(self):
        rules = tutor_agent.TUTOR_SOURCES_INSTRUCTIONS

        assert "consulte **todas** as fontes configuradas" in rules
        assert "na mesma" in rules
        assert "paralelo" in rules
        assert "só quando a pessoa pedir" in rules
        assert "Nunca descarte o que uma fonte encontrou" in rules


class _SqlServerSession:
    """Compiles each query with the SQL Server dialect instead of running it."""

    def __init__(self):
        self.statements = []

    def query(self, *entities):
        from sqlalchemy.dialects import mssql
        from sqlalchemy.orm import Query

        session = self

        class _Compiled:
            def __init__(self, query):
                self.query = query

            def filter(self, *criteria):
                return _Compiled(self.query.filter(*criteria))

            def order_by(self, *clauses):
                return _Compiled(self.query.order_by(*clauses))

            def all(self):
                compiled = self.query.statement.compile(
                    dialect=mssql.dialect(), compile_kwargs={"literal_binds": True}
                )
                session.statements.append(str(compiled))
                return []

        return _Compiled(Query(entities))


class TestSqlServerCompatibility:
    def test_queries_use_sql_server_boolean_syntax(self):
        session = _SqlServerSession()

        tutor_agent.tutor_sources_instructions(session, "p-1")

        databases = next(sql for sql in session.statements if "project_database" in sql)
        assert "IS 1" not in databases
        assert "is_active = 1" in databases
