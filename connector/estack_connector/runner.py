"""The connector loop: report status, take jobs from e-Stack, run them, send results back."""
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from database_access.guard import DatabaseAccessError, DatabaseTarget
from database_access.operations import run_connector_job
from estack_connector.client import EstackClient, UnauthorizedError
from estack_connector.config import ConnectorConfig

VERSION = "1.0.0"
STATUS_REFRESH_SECONDS = 600
MAX_BACKOFF_SECONDS = 60
UNKNOWN_ALIAS = "O banco '{alias}' não está configurado neste conector."
UNEXPECTED_FAILURE = "Falha inesperada no conector ao executar '{operation}'."

logger = logging.getLogger("estack_connector")

JobRunner = Callable[[DatabaseTarget, str, dict[str, Any]], dict[str, Any]]


class Connector:
    def __init__(
        self,
        config: ConnectorConfig,
        client: EstackClient,
        run_job: JobRunner = run_connector_job,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.client = client
        self.run_job = run_job
        self.clock = clock
        self.stopped = threading.Event()
        self._statuses: dict[str, tuple[float, dict[str, Any]]] = {}
        self._pool = ThreadPoolExecutor(max_workers=config.max_parallel_jobs)

    # region Status
    def _check(self, target: DatabaseTarget) -> dict[str, Any]:
        try:
            return self.run_job(target, "check_connection", {})
        except DatabaseAccessError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            logger.exception("Connection check failed for '%s'", target.alias)
            return {"ok": False, "error": "Falha inesperada ao testar a conexão."}

    def _status(self, target: DatabaseTarget) -> dict[str, Any]:
        cached = self._statuses.get(target.alias)
        if cached is not None and self.clock() - cached[0] < STATUS_REFRESH_SECONDS:
            return cached[1]
        status = self._check(target)
        self._statuses[target.alias] = (self.clock(), status)
        return status

    def database_report(self) -> list[dict[str, Any]]:
        report = []
        for target in self.config.targets:
            status = self._status(target)
            report.append(
                {
                    "alias": target.alias,
                    "engine": target.engine,
                    "database": target.database or None,
                    "schemas": list(target.schemas) or list(status.get("schemas") or [])[:500],
                    "ok": status.get("ok"),
                    "server_version": status.get("server_version"),
                    "role": status.get("role"),
                    "warnings": list(status.get("warnings") or [])[:20],
                    "error": status.get("error"),
                }
            )
        return report

    # endregion

    # region Jobs
    def handle(self, job: dict[str, Any]) -> None:
        job_id, operation = job["id"], job["operation"]
        target = self.config.target(job["database_alias"])
        result, error = None, None
        if target is None:
            error = UNKNOWN_ALIAS.format(alias=job["database_alias"])
        else:
            try:
                result = self.run_job(target, operation, job.get("arguments") or {})
            except DatabaseAccessError as exc:
                error = str(exc)
            except Exception:
                logger.exception("Job %s (%s) failed on '%s'", job_id, operation, target.alias)
                error = UNEXPECTED_FAILURE.format(operation=operation)
        try:
            self.client.submit(job_id, result, error)
        except UnauthorizedError:
            logger.error("Token refused while submitting job %s; stopping.", job_id)
            self.stop()
        except httpx.HTTPError as exc:
            logger.warning("Could not submit job %s: %s", job_id, type(exc).__name__)

    # endregion

    # region Loops
    def send_heartbeat(self) -> None:
        self.client.heartbeat(VERSION, self.database_report())

    def heartbeat_loop(self) -> None:
        while not self.stopped.is_set():
            try:
                self.send_heartbeat()
            except UnauthorizedError:
                logger.error("Token refused by e-Stack; stopping. Generate a new token.")
                self.stop()
                return
            except httpx.HTTPError as exc:
                logger.warning("Heartbeat failed: %s", type(exc).__name__)
            self.stopped.wait(self.config.heartbeat_seconds)

    def poll_once(self) -> int:
        jobs = self.client.poll_jobs()
        for job in jobs:
            self._pool.submit(self.handle, job)
        return len(jobs)

    def poll_loop(self) -> None:
        backoff = 1
        while not self.stopped.is_set():
            try:
                self.poll_once()
                backoff = 1
            except UnauthorizedError:
                logger.error("Token refused by e-Stack; stopping. Generate a new token.")
                self.stop()
                return
            except httpx.HTTPError as exc:
                logger.warning("Polling failed (%s); retrying in %ss", type(exc).__name__, backoff)
                self.stopped.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def run(self) -> None:
        logger.info(
            "estack-db-connector %s serving %s",
            VERSION,
            ", ".join(f"{t.alias} ({t.engine})" for t in self.config.targets),
        )
        heartbeat = threading.Thread(target=self.heartbeat_loop, name="heartbeat", daemon=True)
        heartbeat.start()
        try:
            self.poll_loop()
        finally:
            self.stop()

    def stop(self) -> None:
        self.stopped.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # endregion
