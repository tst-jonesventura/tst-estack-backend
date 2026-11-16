"""Idempotent seed for the ``ask_git`` sub-agent that reads the source code.

This agent's prompt used to live only as a database row, so it could not be
reviewed or changed safely. That gap produced a real defect: asked about the
"Tutor" feature, it matched the literal string in ``AgentRun(title="Tutor")``
and described the agent-run machinery instead, treating a name collision as
proof of implementation.

Run from the ``src`` directory:

    cd src && uv run python -m scripts.seed_ask_git_agent
"""

from agents.utils.context import sync_context
from agents.utils.mcps import sync_mcps
from agents.utils.tools import sync_tools
from database import models
from database.database import create_session

ASK_GIT_NAME = "ask_git"
ASK_GIT_MODEL = "openai:gpt-5.6-luna"
ASK_GIT_TEMPERATURE = None
ASK_GIT_MAX_RETRIES = 3

ASK_GIT_MCPS = ["git_mcp"]

PROMPT_OWNERSHIP_MARKER = (
    "Você responde perguntas sobre o código-fonte de um projeto"
)

ASK_GIT_SYSTEM_PROMPT = """\
Você responde perguntas sobre o código-fonte de um projeto lendo os repositórios \
dele através das ferramentas de leitura disponíveis. Sua resposta é usada por outro \
agente como se fosse evidência do código, então ela precisa refletir o que você \
realmente leu.

## Regra central: parecer não é ser
Encontrar o nome de uma funcionalidade em um trecho de código NÃO prova que aquele \
trecho a implementa. Um título, um rótulo, uma constante ou um comentário com o mesmo \
nome é uma **pista fraca**.

- Antes de afirmar que um arquivo implementa algo, **abra o arquivo e confirme**.
- Desconfie de colisão de nomes: o mesmo termo costuma aparecer no código de outra \
funcionalidade que apenas menciona aquela.
- Se a evidência ficar ambígua entre duas funcionalidades, **relate a ambiguidade** e \
diga o que encontrou de cada lado. Nunca escolha uma e apresente como certa.

## Busque pela entidade, não pelo nome da funcionalidade
O nome de uma funcionalidade se espalha por código que apenas a menciona. A entidade \
que a implementa tem nome próprio e quase não colide. Para a funcionalidade "Tutor", \
procure `TutorMessage` ou `TutorConversation`, não `Tutor`.

## Como investigar
Você tem duas ferramentas: `search_code` para localizar e `get_file_contents` para ler.

1. Formule a hipótese antes de agir: qual entidade responde a pergunta, e em que tipo \
de arquivo ela costuma morar (modelo, schema, tipo, rota, componente).
2. Para perguntas sobre estados, campos ou dados, vá direto aos arquivos de modelo, \
schema ou tipos. É onde a resposta está, e costuma bastar um arquivo.
3. Leia com `get_file_contents`. É a fonte confiável: o que você leu, você pode afirmar.
4. Use `search_code` para localizar candidatos, nunca como resposta. Ela é frágil \
nestes repositórios: uma busca vazia não prova ausência. Se voltar vazia, troque os \
termos pelo nome da entidade em vez de repetir a mesma busca.
5. Siga as importações e referências reais entre arquivos para confirmar que um fluxo \
existe de fato, em vez de deduzir pelo nome.
6. Abrir arquivos por tentativa é caro e raramente aproxima da resposta. Com uma boa \
hipótese, três ou quatro leituras bastam.

## Como responder
- Cite os caminhos dos arquivos que você efetivamente leu.
- Separe claramente o que você **verificou no código** do que é **suposição**.
- Diga explicitamente o que não conseguiu verificar. Uma resposta parcial e honesta é \
mais útil que uma completa e inventada.
- Responda em português do Brasil, de forma direta e específica.
"""


def is_seeded_prompt(prompt: str | None) -> bool:
    """Whether this prompt is unset or an earlier version of the one seeded here.

    Without the marker check, a project override would keep whichever version of
    this prompt it received first, and later corrections would never reach it.
    """
    text = (prompt or "").strip()
    return not text or PROMPT_OWNERSHIP_MARKER in text


def adopt_project_overrides(session) -> list[str]:
    """Bring per-project copies up to the versioned prompt.

    Agents resolve to a project override before the default, so seeding only the
    default leaves every project that has an override behind. A project that wrote
    its own prompt keeps it; only the model choice is preserved for the rest.
    """
    overrides = (
        session.query(models.Agent)
        .filter(
            models.Agent.name == ASK_GIT_NAME,
            models.Agent.is_default == False,
        )
        .all()
    )
    adopted = []
    for agent in overrides:
        if not is_seeded_prompt(agent.system_prompt):
            continue
        agent.system_prompt = ASK_GIT_SYSTEM_PROMPT
        agent.user_prompt = None
        agent.temperature = ASK_GIT_TEMPERATURE
        adopted.append(agent.id)
    session.commit()
    return adopted


def seed() -> None:
    with create_session() as session:
        sync_tools(create_session())
        sync_context(session)
        sync_mcps(create_session())

        agent = (
            session.query(models.Agent)
            .filter(
                models.Agent.name == ASK_GIT_NAME,
                models.Agent.is_default == True,
            )
            .one_or_none()
        )
        if agent is None:
            agent = models.Agent(name=ASK_GIT_NAME, is_default=True)
            session.add(agent)

        agent.model = ASK_GIT_MODEL
        agent.temperature = ASK_GIT_TEMPERATURE
        agent.max_retries = ASK_GIT_MAX_RETRIES
        agent.system_prompt = ASK_GIT_SYSTEM_PROMPT
        agent.user_prompt = None
        session.commit()
        session.refresh(agent)

        session.query(models.AgentMcp).filter_by(agent_id=agent.id).delete()
        for mcp_name in ASK_GIT_MCPS:
            session.add(models.AgentMcp(agent_id=agent.id, mcp_name=mcp_name))
        session.commit()

        adopted = adopt_project_overrides(session)

        print(f"Seeded agent '{ASK_GIT_NAME}' (id={agent.id}) with mcps={ASK_GIT_MCPS}")
        print(f"Project overrides given the versioned prompt: {len(adopted)}")


if __name__ == "__main__":
    seed()
