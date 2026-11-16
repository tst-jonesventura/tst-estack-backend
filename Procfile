release: alembic upgrade head
web: gunicorn -w 2 -k uvicorn.workers.UvicornWorker src.main:app --preload -b 0.0.0.0:$PORT --access-logfile -
