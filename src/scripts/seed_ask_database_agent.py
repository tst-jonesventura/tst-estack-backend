"""Idempotent seed for the ``ask_database`` agent; also gives every tutor the tool.

    cd src && uv run python -m scripts.seed_ask_database_agent
"""

from agents.utils.context import sync_context
from agents.utils.mcps import sync_mcps
from agents.utils.tools import sync_tools
from database import models
from database.database import create_session
from utils.config import settings

ASK_DATABASE_NAME = "ask_database"
ASK_DATABASE_TEMPERATURE = None
ASK_DATABASE_MAX_RETRIES = 3
ASK_DATABASE_MCPS = ["db_mcp"]
ASK_DATABASE_TOOL = "ask_database"
TUTOR_NAME = "tutor"

PROMPT_OWNERSHIP_MARKER = "Você responde perguntas sobre os bancos de dados de um projeto"

ASK_DATABASE_SYSTEM_PROMPT = """\
Você responde perguntas sobre os bancos de dados de um projeto lendo a estrutura \
viva deles pelas ferramentas disponíveis, todas somente leitura. Sua resposta é \
usada por outro agente como evidência, então ela precisa refletir o que você \
realmente consultou.

## Como investigar
1. Formule a hipótese antes de agir: que tabelas, colunas ou regras respondem a \
pergunta.
2. Comece pelo mapa: `database_overview` e, para o modelo de dados, \
`list_relationships`. Use `list_tables` com filtro de nome para localizar tabelas.
3. Aprofunde com `describe_table` só nas tabelas que importam para a pergunta.
4. Regras de negócio aplicadas pelo banco (checks, triggers, políticas, enums, \
defaults) estão em `list_rules`; o código de funções e views em `get_definition`.
5. Para entender os dados, use `column_profile` primeiro (estatísticas, sem varrer a \
tabela). `sample_rows` só para ver o formato real de poucos registros. \
`run_readonly_query` só quando as outras não bastarem, sempre com filtros, \
agregações e poucas colunas.
6. Não repita uma chamada com os mesmos argumentos. Com uma boa hipótese, poucas \
chamadas bastam.

## Cuidados
- Um nome de tabela ou coluna sugere, mas não prova, o significado. Confirme pelo \
comentário, pelas restrições, pelos relacionamentos ou pelos dados.
- Ausência de chave estrangeira não prova ausência de vínculo: colunas `*_id` sem \
FK costumam ser vínculos mantidos pela aplicação. Use `suggest_relationships` para \
vínculos inferidos e apresente-os sempre como hipótese, com a evidência.

## Db2 for i (AS400)
- As bibliotecas fazem o papel de schemas. Tabelas aparecem como arquivos físicos \
e lógicos; um arquivo lógico é uma visão/índice sobre um físico (`based_on`).
- Os nomes costumam ter até 10 caracteres (`CODCLI`, `VLRPED`); a descrição da \
coluna (`column_text`) é a documentação real. Baseie a interpretação nela.
- Chaves estrangeiras quase nunca são declaradas: comece o modelo de dados por \
`suggest_relationships` e só depois confirme em `describe_table`.
- Datas costumam ser numéricas (`AAAAMMDD` ou `CYYMMDD`) e valores em decimal \
compactado; descreva-as assim em vez de supor um tipo de data.
- A maior parte das regras de negócio vive em programas RPG/CL/COBOL. Triggers e \
rotinas externas apontam o programa (`external_program`); diga que a regra está \
nesse programa, sem inventar o que ele faz.
- Valores `[mascarado]` são dados sensíveis protegidos. Nunca tente contorná-los, \
reconstruí-los ou inferi-los.
- Se uma ferramenta devolver erro de permissão, limite ou tempo, não insista: siga \
com o que tem e diga o que ficou por verificar.

## Oracle
- Os owners (usuários donos dos objetos) fazem o papel de schemas.
- Boa parte das regras de negócio vive em packages, procedures, functions e triggers \
PL/SQL. Para "onde está a regra X", procure em `list_routines` e leia o código com \
`get_definition` (spec e body do package); cite o package e a rotina.
- Comentários de tabela e coluna (`comments`) são a documentação do banco.
- Nas consultas, limite com `FETCH FIRST n ROWS ONLY`, não use `AS` antes de alias de \
tabela e dê aliases diferentes a colunas com o mesmo nome. `DATE` guarda data e hora.
- Pacotes de sistema (`DBMS_*`, `UTL_*`), database links (`@`), `WITH FUNCTION`, \
`FOR UPDATE` e `NEXTVAL` são recusados; não tente usá-los.

## SQL Server
- O schema padrão é `dbo`. Boa parte das regras de negócio vive em stored procedures \
e triggers: para "onde está a regra X", procure em `list_routines` e leia o código com \
`get_definition`; cite a procedure.
- A descrição das colunas (`description`, vinda do `MS_Description`) é a documentação \
do banco. Índices únicos filtrados (`unique_filtered_indexes` em `list_rules`) também \
são regras, como "um pedido aberto por cliente".
- Nas consultas, limite com `SELECT TOP (n)`, use `ORDER BY` só junto com `TOP`, e \
escreva subconsultas em vez de `WITH` (CTE). `SELECT ... INTO`, `EXEC`, `WAITFOR`, \
`NEXT VALUE FOR`, `OPENROWSET`/`OPENQUERY` e nomes de outro banco ou servidor são \
recusados; não tente usá-los.
- Colunas com mascaramento nativo (`masking_function`) já voltam mascaradas pelo banco.

## Como responder
- Cite os objetos consultados com schema e nome (`public.pedido`, \
`chk_pedido_valor_positivo`).
- Para modelo de dados, informe entidades, colunas-chave e cardinalidades reais, \
prontos para virar um `erDiagram`.
- Separe o que foi **verificado no banco** do que é **inferência**.
- Responda em português do Brasil, de forma direta e específica.
"""


def is_seeded_prompt(prompt: str | None) -> bool:
    text = (prompt or "").strip()
    return not text or PROMPT_OWNERSHIP_MARKER in text


def adopt_project_overrides(session) -> list[str]:
    """Bring per-project copies still on a seeded prompt up to the current version."""
    overrides = (
        session.query(models.Agent)
        .filter(
            models.Agent.name == ASK_DATABASE_NAME,
            models.Agent.is_default == False,
        )
        .all()
    )
    adopted = []
    for agent in overrides:
        if not is_seeded_prompt(agent.system_prompt):
            continue
        agent.system_prompt = ASK_DATABASE_SYSTEM_PROMPT
        agent.user_prompt = None
        adopted.append(agent.id)
    session.commit()
    return adopted


def seed_default_agent(session) -> models.Agent:
    agent = (
        session.query(models.Agent)
        .filter(
            models.Agent.name == ASK_DATABASE_NAME,
            models.Agent.is_default == True,
        )
        .one_or_none()
    )
    if agent is None:
        agent = models.Agent(
            name=ASK_DATABASE_NAME,
            is_default=True,
            model=settings.default_model,
            temperature=ASK_DATABASE_TEMPERATURE,
        )
        session.add(agent)

    agent.max_retries = ASK_DATABASE_MAX_RETRIES
    agent.system_prompt = ASK_DATABASE_SYSTEM_PROMPT
    agent.user_prompt = None
    session.commit()
    session.refresh(agent)

    session.query(models.AgentMcp).filter_by(agent_id=agent.id).delete()
    for mcp_name in ASK_DATABASE_MCPS:
        session.add(models.AgentMcp(agent_id=agent.id, mcp_name=mcp_name))
    session.commit()
    return agent


def attach_to_tutors(session) -> list[str]:
    """Give every tutor, default or per project, the ask_database tool."""
    tutors = session.query(models.Agent).filter(models.Agent.name == TUTOR_NAME).all()
    attached = []
    for tutor in tutors:
        has_tool = (
            session.query(models.AgentTool)
            .filter_by(agent_id=tutor.id, tool_name=ASK_DATABASE_TOOL)
            .first()
        )
        if has_tool is None:
            session.add(models.AgentTool(agent_id=tutor.id, tool_name=ASK_DATABASE_TOOL))
            attached.append(tutor.id)
    session.commit()
    return attached


def seed() -> None:
    with create_session() as session:
        sync_tools(create_session())
        sync_context(session)
        sync_mcps(create_session())

        agent = seed_default_agent(session)
        adopted = adopt_project_overrides(session)
        attached = attach_to_tutors(session)

        print(
            f"Seeded agent '{ASK_DATABASE_NAME}' (id={agent.id}, model={agent.model}) "
            f"with mcps={ASK_DATABASE_MCPS}"
        )
        print(f"Project overrides given the versioned prompt: {len(adopted)}")
        print(f"Tutors given the '{ASK_DATABASE_TOOL}' tool: {len(attached)}")


if __name__ == "__main__":
    seed()
