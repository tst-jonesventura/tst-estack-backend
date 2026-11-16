# Conector de banco do e-Stack

Roda na rede do cliente e permite que o Tutor do e-Stack leia, **somente leitura**,
bancos que a nuvem não alcança: Db2 for i (AS400), Oracle, SQL Server e PostgreSQL internos.

- Só abre conexões de **saída**: HTTPS até o e-Stack e a porta de cada banco.
- As **senhas ficam aqui**; o e-Stack guarda apenas o hash do token do conector.
- Dados sensíveis são **mascarados aqui**, antes de sair da rede do cliente.

## 1. Criar o conector no e-Stack

Painel → **Conectores de banco** → empresa → **Novo conector**. Guarde o token:
ele aparece uma única vez.

## 2. Preparar o banco

### Db2 for i (AS400), IBM i 7.3 ou superior

1. Instale e inicie o Mapepire no IBM i:
   ```
   yum install mapepire-server
   sc start mapepire
   ```
   Ele escuta na porta **8076** (TLS). Libere essa porta do host do conector até o IBM i.
2. Crie um perfil dedicado, por exemplo `ESTACKRO`:
   - **sem autoridades especiais** (`SPCAUT(*NONE)`); perfis com `*ALLOBJ` ou
     `*SECADM`, inclusive herdados de grupo, são recusados;
   - `LMTCPB(*YES)`;
   - `*USE` nas bibliotecas de dados que o Tutor pode ler.
3. A conexão é aberta em modo somente leitura e o conector bloqueia funções com
   efeito colateral (`QCMDEXC`, `SYSTOOLS`, `IFS_*`, `HTTP_*`). Como defesa
   adicional, revogue do perfil o uso dessas funções.

### Oracle (12c R2 ou superior)

Crie um usuário só de leitura, sem papéis administrativos:

```sql
CREATE USER estack_ro IDENTIFIED BY "...";
GRANT CREATE SESSION TO estack_ro;
-- para cada tabela ou view que o Tutor pode ler:
GRANT SELECT ON erp.pedido TO estack_ro;
-- para o Tutor ler o código PL/SQL (packages, procedures, triggers) sem poder executá-lo:
GRANT DEBUG ON erp.pkg_pedido TO estack_ro;
```

- **Não dê `EXECUTE`** em rotinas da aplicação. Uma rotina com `PRAGMA AUTONOMOUS_TRANSACTION`
  grava mesmo numa transação somente leitura, e o conector recusa usuários que possam
  executá-las.
- São recusados: os papéis `DBA`, `IMP_FULL_DATABASE`/`EXP_FULL_DATABASE` (e os `DATAPUMP_*`),
  privilégios `ANY` (exceto `SELECT/READ ANY TABLE`, que só geram aviso), `ALTER SYSTEM`,
  `BECOME USER` e `EXEMPT ACCESS POLICY`.
- O campo `database` é o **service name** (ex.: `ORCLPDB1`). Porta padrão: 1521. Com
  `ssl_mode` `require`/`verify-*` a conexão usa TCPS (normalmente a porta 2484) e **sempre
  verifica o certificado e o nome do servidor**. Se o certificado for de uma CA interna ou
  autoassinado, informe a CA em `options: {ca_file: ...}`.
- O driver roda em modo *thin* (sem Oracle Client). Ele **não conecta** em servidores que
  exigem *Native Network Encryption* (`SQLNET.ENCRYPTION_SERVER = REQUIRED`). Nesse caso, peça
  ao DBA para habilitar TCPS ou deixar a NNE como `ACCEPTED`/`REQUESTED`.

### SQL Server (2017 ou superior, Azure SQL, RDS)

Crie um usuário só de leitura no banco que o Tutor vai ler:

```sql
CREATE LOGIN estack_ro WITH PASSWORD = '...';
USE <banco>;
CREATE USER estack_ro FOR LOGIN estack_ro;
-- só os schemas liberados (ex.: vendas); não use db_datareader se houver schemas que o Tutor não deve ler
GRANT SELECT ON SCHEMA::vendas TO estack_ro;
-- para o Tutor ler o código de procedures, functions, triggers e views
GRANT VIEW DEFINITION ON SCHEMA::vendas TO estack_ro;
```

- O SQL Server não tem sessão somente leitura: cada consulta roda numa transação que sempre é
  desfeita, e o conector recusa `SELECT ... INTO`, `EXEC`, `OPENROWSET`/`OPENQUERY`, linked
  servers, `WAITFOR` e nomes de outros bancos.
- São recusados: `sysadmin`, `securityadmin`, `serveradmin`, `db_owner`, `db_ddladmin`,
  `db_securityadmin`, `db_accessadmin`, `CONTROL`, `ALTER`, `IMPERSONATE` e **`UNMASK`** (veria o
  que o mascaramento nativo esconde). Permissão de escrita e `EXECUTE` geram aviso.
- O campo `database` é o nome do banco. Porta padrão: 1433 (instância nomeada só com porta fixa).
- Com `ssl_mode` `require`/`verify-*` a conexão usa TLS e **sempre valida o certificado**
  (`verify-full` também valida o nome do servidor). Azure SQL e RDS usam CAs públicas. Para
  certificado de CA interna ou autoassinado, informe a CA em `options: {ca_file: ...}`.

### PostgreSQL

```sql
CREATE ROLE estack_reader LOGIN PASSWORD '...';
GRANT pg_read_all_data TO estack_reader;  -- PG 14+
```

## 3. Configurar e subir

Copie `databases.example.yaml` para `databases.yaml` e ajuste os bancos. Depois:

```bash
docker run -d --restart=always --name estack-db-connector \
  -e ESTACK_URL=https://<backend-do-estack>/ \
  -e ESTACK_CONNECTOR_TOKEN=<token> \
  -e ERP_PASSWORD=<senha do perfil> \
  -v ./databases.yaml:/config/databases.yaml:ro \
  estack-db-connector:1.0.0
```

Em até um minuto o conector aparece **online** no Painel, com os bancos que
consegue acessar. No projeto, cadastre o banco escolhendo **Via conector**.

### Sem arquivo: um banco descrito por variáveis

Use esta forma onde não é possível montar arquivo, como no Heroku, no Kubernetes ou no Portainer.
Se o `databases.yaml` não existir, o conector lê um único banco das variáveis abaixo:

| Variável | Obrigatória | Padrão | Descrição |
|---|---|---|---|
| `DB_HOST` | sim | — | Host do banco |
| `DB_USER` | sim | — | Usuário de leitura |
| `DB_PASSWORD` | sim | — | Senha |
| `DB_SCHEMAS` | não | lista de bibliotecas do perfil | Bibliotecas/schemas liberados, separados por vírgula |
| `DB_ALIAS` | não | `erp` | Nome do banco no e-Stack |
| `DB_ENGINE` | não | `db2i` | `db2i` (AS400) ou `postgres` |
| `DB_PORT` | não | `8076` (db2i) / `5432` (postgres) | Porta |
| `DB_NAME` | não | — | Nome do banco (obrigatório para `postgres`) |
| `DB_SSL_MODE` | não | `require` | Modo TLS |
| `MAX_PARALLEL_JOBS` | não | `4` | Consultas simultâneas |

Se o arquivo existir, ele tem prioridade e as variáveis `DB_*` são ignoradas. Para mais de um banco,
use o arquivo.

Se o banco for acessível pela internet, o conector não é necessário: cadastre-o no projeto em modo
**Direto** (PostgreSQL ou Db2 for i).

## Construir a imagem

As dependências ficam travadas, com hash, em `requirements.lock`. Para atualizar:

```bash
uv pip compile connector/requirements.txt --generate-hashes --python-version 3.12 \
  --python-platform x86_64-manylinux_2_28 --no-header -o connector/requirements.lock
```

Na raiz do repositório:

```bash
docker build -f connector/Dockerfile -t estack-db-connector:1.0.0 .
```
