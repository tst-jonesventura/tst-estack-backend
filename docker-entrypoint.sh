#!/bin/sh
set -e

echo "==> Aguardando conexao com o banco de dados..."
python -c "
import os, time, sys
from sqlalchemy.engine.url import make_url

db_url = os.getenv('ESTACK_DB_CONNECTION_STRING')
if db_url and 'mssql' in db_url:
    import pymssql
    url = make_url(db_url)
    db_name = url.database or 'estack_db'
    max_retries = 30
    for i in range(max_retries):
        try:
            conn = pymssql.connect(
                server=url.host,
                port=url.port or 1433,
                user=url.username,
                password=url.password,
                database='master',
                autocommit=True,
                login_timeout=5
            )
            cursor = conn.cursor()
            cursor.execute(f\"IF NOT EXISTS (SELECT * FROM sys.databases WHERE name = N'{db_name}') CREATE DATABASE [{db_name}]\")
            conn.close()
            print(f'==> Banco de dados SQL Server [{db_name}] verificado/criado com sucesso.')
            break
        except Exception as e:
            print(f'Aguardando SQL Server ({i+1}/{max_retries}): {e}')
            time.sleep(2)
    else:
        print('Erro: Nao foi possivel conectar ao SQL Server.')
        sys.exit(1)
"

echo "==> Executando migrações do banco de dados (alembic upgrade head)..."
alembic upgrade head

echo "==> Populando dados iniciais do banco (seed_initial_data)..."
python -m scripts.seed_initial_data

echo "==> Iniciando o servidor backend..."
exec "$@"
