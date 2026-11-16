# estack-backend

## Execução com Docker (Recomendado)

O projeto possui suporte completo para orquestração via **Docker Compose**, executando o Backend (FastAPI + OpenHands/Agentes), o Banco de Dados (Microsoft SQL Server) e o Frontend em containers sincronizados.

### Como Executar

1. **Configuração de Ambiente**:
   Copie o template de variáveis de ambiente:
   ```bash
   cp .env.example .env
   ```
   Abra o arquivo `.env` e configure as variáveis principais:
   - **`SEED_ADMIN_EMAIL`**: **(Obrigatório para login)** Informe um e-mail real e acessível. Este e-mail será criado na inicialização com privilégios de Administrador e dono do projeto inicial.
   - **`EMAIL_USER` / `EMAIL_PASSWORD` / `EMAIL_HOST`**: Configurações de SMTP do servidor de e-mail utilizado pelo sistema para enviar o código de verificação de login.
   - **`TARGET_PROJECT_DIR`**: Caminho da pasta local no seu computador onde estão os códigos-fonte que os agentes de IA irão analisar/modificar (ex: `C:/Users/seu_usuario/meu-projeto`).
   - **Chaves de API das LLMs**: Insira suas chaves (`ESTACK_OPENAI_API_KEY`, `ESTACK_ANTHROPIC_API_KEY`, etc.).

2. **Iniciar os Serviços**:
   Execute o Docker Compose na raiz do `estack-backend`:
   ```bash
   docker compose up --build
   ```
   *(ou adicione `-d` para executar em segundo plano)*.

3. **Primeiro Acesso e Login**:
   - Acesse o Frontend em [http://localhost:3000](http://localhost:3000).
   - Na tela de login, informe o mesmo e-mail configurado em `SEED_ADMIN_EMAIL` e solicite o código de acesso.
   - Verifique sua caixa de entrada, insira o código de verificação recebido por e-mail e entre na plataforma.

4. **Acesso aos Serviços**:
   - **Frontend**: [http://localhost:3000](http://localhost:3000)
   - **Backend API & Swagger**: [http://localhost:8000/docs](http://localhost:8000/docs)
   - **SQL Server**: `localhost:1433` (Database: `estack_db`, Usuário: `sa`)

---

## Ambiente Local (Sem Docker)
- Instale o uv (gerenciador de pacotes python) utilizando o pip: ```pip install uv``` 
- Instale o ambiente virtual utilizando o comando ```uv sync```
- Caso não ative automaticamente, ative o ambiente virtual com ```.venv\Scripts\activate``` (Windows)
- Para rodar a API: ```uvicorn src.main:app --reload --host 0.0.0.0 --port 8000```
- Testes: ```pytest```
## Resumo

```
📦src
 ┣ 📂agents
 ┃ ┣ 📜agent_deps.py - objetos que podem ser acessados pelos agentes, contextos e ferramentas
 ┃ ┣ 📜agent_factory.py - Factory para criação de agentes. Obtém dados do agente a partir do banco de dados, exceto o formato de saída. Isso é explicitamente definido via código
 ┃ ┗ 📂utils - funções auxiliares para os agentes
 ┣ 📂database
 ┃ ┣ 📜models.py - classes do sqlalchemy para ORM
 ┃ ┗ 📜database.py - handler de conexões com o banco de dados
 ┣ 📂schemas
 ┃ ┣ 📜base.py - modelos base para objetos (pydantic)
 ┃ ┣ 📜..._schemas.py - modelos de dados específicos (ex: agent_schemas, project_schemas)
 ┣ 📂routers
 ┃ ┣ 📂agents - endpoints que utilizam agentes para processamento
 ┃ ┣ 📂documents - endpoints para artefatos - transcrições, requisitos... 
 ┃ ┗ 📂crud - endpoints para operações CRUD em objetos do banco de dados
 ┣ 📂services
 ┃ ┗ 📜... - lógica de negócio e serviços auxiliares
 ┣ 📂tests
 ┃ ┗ 📜...
 ┣ 📂utils
 ┃ ┗ 📜... - utilitários gerais do sistema (auth, config, etc)
 ┗ 📜main.py
```

## Criação de um novo Agente
- Caso necessário, crie os objetos sqlalchemy em [src/database/models.py](src/database/models.py) e os modelos pydantic em [src/schemas](src/schemas).
- Caso o agente precise de funções específicas para injeção de contexto ou ferramentas, verifique [src/agents/agent_deps.py](src/agents/agent_deps.py) ou crie novos agentes em [src/agents](src/agents).
- Usando o frontend, crie o novo agente (rota `/agents`). Nessa interface, é possível selecionar o modelo, escrever o prompt do agente e conectar os contextos e ferramentas.
- Utilizando os arquivos da pasta [src/routers/agents](src/routers/agents) como exemplo, crie uma função de processamento e um endpoint para chamada do agente.

## Debug de Agentes (logfire)

Siga as instruções em https://logfire.pydantic.dev/docs/ para configurar localmente o logfire. Crie um token e armazene na variável LOGFIRE_TOKEN, no .env

### Exportação para o New Relic (OTLP)

Os traces do logfire (incluindo os do pydantic-ai) também podem ser enviados ao New Relic via OTLP/gRPC:

- `NEW_RELIC_LICENSE_KEY`: license key de ingestão. Se ausente, nada é enviado ao New Relic.
- `NEW_RELIC_OTLP_ENDPOINT` (opcional): padrão `https://otlp.nr-data.net:4317`; use `https://otlp.eu01.nr-data.net:4317` para contas na região EU.
- `NEW_RELIC_APP_NAME` (opcional): usado como `service.name` dos traces exportados; padrão `estack-backend`.

## Versionamento do Banco de Dados (Alembic)

O Alembic é uma ferramenta de migração de banco de dados para projetos Python que utilizam SQLAlchemy. Ele permite controlar versões do esquema do banco de dados, facilitando a criação, atualização e reversão de alterações estruturais.

### Funcionamento Básico
- O Alembic mantém um diretório de versões (`alembic/versions`) onde cada arquivo representa uma migração.
- Cada migração possui um identificador único e registra as operações realizadas (criação, alteração ou remoção de tabelas, colunas, etc).
- O histórico de migrações garante que o banco de dados possa ser atualizado ou revertido para qualquer estado anterior.
- A conexão com o banco de dados utiliza a variável de ambiente ESTACK_DB_CONNECTION_STRING.
- Todos os bancos de dados (dev, pre prod e prod) foram sincronizados na versão 2ac862400fc8.


### Principais Comandos

#### 1. `alembic revision --autogenerate -m "mensagem"`
Gera um novo arquivo de migração automaticamente, detectando alterações no modelo do SQLAlchemy em relação ao banco de dados atual. O parâmetro `-m` permite adicionar uma mensagem descritiva à migração.

Exemplo:
```
alembic revision --autogenerate -m "Adiciona tabela de tags"
```
IMPORTANTE: Sempre revise o arquivo gerado por `--autogenerate` antes de aplicar ao banco de dados. Existem alterações que não são detectadas pelo alembic, que são explicadas na documentação -  https://alembic.sqlalchemy.org/en/latest/autogenerate.html

#### 2. `alembic upgrade <versão>`
Aplica as migrações pendentes até a versão especificada (ou até a última, se usar `head`). Atualiza o banco de dados para o novo estado.

Exemplo:
```
alembic upgrade head
```

#### 3. `alembic downgrade <versão>`
Reverte migrações, retornando o banco de dados para uma versão anterior. Útil para desfazer alterações ou testar diferentes estados do banco.

Exemplo:
```
alembic downgrade -1
```

