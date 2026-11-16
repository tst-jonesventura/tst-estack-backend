# Guia de Configuração da Integração com o Jira

---

## 1. Pré-requisitos e Coleta de Dados

Antes de iniciar, colete os seguintes dados:

1. **Jira**:
   - `jira_base_url`: URL da instância (ex: `https://sua-empresa.atlassian.net`).
   - `jira_email`: E-mail da conta Atlassian com permissão de integração.
   - `jira_api_token`: Token gerado em [Atlassian API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens).
   - `jira_project_key`: Chave do projeto no Jira (ex: `ESTACK`).
   - `jira_project_id`: ID numérico do projeto no Jira (visível via API Jira `/rest/api/3/project/{key}` ou URL de configurações do projeto).
   - `default_issue_type`: Nome do tipo de issue padrão (ex: `Story` ou `Task`).
   - `jira_status_id` e `jira_status_name`: IDs e nomes dos status de transição no Jira (ex: `10001` - `To Do`, `10002` - `In Progress`, `10003` - `Done`).
2. **E-Stack Backend**:
   - `project_id`: UUID do projeto no banco de dados.
   - `project_kanban_column_id`: UUIDs das colunas do Kanban do projeto na tabela `project_kanban_column`.
   - Usuário de sistema: O backend requer um usuário com o e-mail `e-stack@elumini-it.com` cadastrado na tabela `user`.

---

## 2. Script SQL de Inserção no Banco de Dados

Substitua os placeholders `<...>` pelos valores reais e execute no PostgreSQL:

```sql
-- 1. Garante que o usuário de sistema existe
INSERT INTO "user" (id, email, name, is_active, created_at, updated_at)
VALUES (
    gen_random_uuid(),
    'e-stack@elumini-it.com',
    'E-Stack',
    true,
    NOW(),
    NOW()
)
ON CONFLICT (email) DO NOTHING;

-- 2. Configuração principal da integração do projeto
INSERT INTO jira_integration_config (
    id,
    project_id,
    jira_base_url,
    jira_email,
    jira_api_token,
    jira_project_id,
    jira_project_key,
    default_issue_type,
    created_at
) VALUES (
    'config-uuid-0001', -- ou gen_random_uuid()
    '<PROJECT_UUID>',
    'https://<SUA_EMPRESA>.atlassian.net',
    '<SEU_EMAIL_ATLASSIAN>',
    '<SEU_API_TOKEN>',
    '<JIRA_PROJECT_ID>',
    '<JIRA_PROJECT_KEY>',
    'Story',
    NOW()
);

-- 3. Mapeamento de Status do Jira para Colunas do Kanban
INSERT INTO jira_integration_columns_mapping (
    id,
    jira_integration_config_id,
    project_kanban_column_id,
    jira_status_id,
    jira_status_name
) VALUES 
(
    gen_random_uuid(),
    'config-uuid-0001',
    '<KANBAN_COLUMN_TODO_UUID>',
    '10001',
    'To Do'
),
(
    gen_random_uuid(),
    'config-uuid-0001',
    '<KANBAN_COLUMN_IN_PROGRESS_UUID>',
    '10002',
    'In Progress'
),
(
    gen_random_uuid(),
    'config-uuid-0001',
    '<KANBAN_COLUMN_DONE_UUID>',
    '10003',
    'Done'
);
```

---

## 3. Configuração do Webhook no Jira

1. Acesse o Jira como Administrador.
2. Vá em **Configurações (ícone de engrenagem)** > **Sistema (System)**.
3. No menu lateral esquerdo, na seção **Avançado (Advanced)**, clique em **WebHooks**.
4. Clique no botão **Criar um WebHook (+ Create a WebHook)**.
5. Preencha os campos:
   - **Nome**: `E-Stack Integration - <Nome do Projeto>`
   - **Status**: `Habilitado (Enabled)`
   - **URL**: `https://<SEU_DOMINIO_BACKEND>/webhook/jira/`
   - **JQL Filter** (recomendado para restringir ao projeto):
     ```text
     project = "<JIRA_PROJECT_KEY>"
     ```
   - **Eventos de Issue (Issue related events)**:
     - Marque: `Criado (created)`
     - Marque: `Atualizado (updated)`
     - Marque: `Excluído (deleted)`
6. Role até o final da página e clique em **Criar (Save)**.

---

## 4. Fluxo e Validação da Integração

- **Jira -> E-Stack (Webhook)**:
  - Criação de issue no Jira cria uma `UserStory` associada ao projeto e ao usuário `e-stack@elumini-it.com`.
  - Atualizações de status no Jira movem a `UserStory` para a coluna mapeada correspondente.
  - Exclusão no Jira remove a `UserStory` sincronizada.
- **E-Stack -> Jira (SyncService / JiraIntegrationProvider)**:
  - Criação/atualização de `UserStory` no backend sincroniza diretamente via API REST v3 do Jira (`/rest/api/3/issue`).
