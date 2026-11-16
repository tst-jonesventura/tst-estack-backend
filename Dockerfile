FROM python:3.12-slim

# Evita geração de .pyc e força stdout imediato
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on

# Instala dependências do sistema necessárias para git, compilação e agentes (ordenadas alfanumericamente)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    freetds-bin \
    freetds-dev \
    git \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Instala o uv (gerenciador de pacotes do projeto)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# Copia arquivos de dependência primeiro para aproveitar cache de camadas
COPY pyproject.toml uv.lock ./

# Instala dependências do projeto respeitando o lockfile exato e sem execução de scripts de build
RUN uv sync --frozen --no-dev --no-build

# Adiciona o venv ao PATH e configura PYTHONPATH
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app:/app/src"

# Copia o código-fonte, scripts de migração, configurações e entrypoint
COPY alembic alembic
COPY alembic.ini .
COPY newrelic.ini ./
COPY docker-entrypoint.sh /docker-entrypoint.sh
COPY src src

# Cria usuário não-root, diretórios necessários e ajusta permissões
RUN chmod +x /docker-entrypoint.sh \
    && groupadd -g 10001 appuser \
    && useradd -u 10001 -g appuser -d /home/appuser -m -s /bin/bash appuser \
    && mkdir -p /home/appuser/.estack/openhands /workspace /app \
    && chown -R appuser:appuser /home/appuser /workspace /app /docker-entrypoint.sh

# Executa o container com usuário não-root por segurança
USER appuser

EXPOSE 8000

# Executa alembic upgrade head através do entrypoint antes de iniciar a aplicação
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
