"""HTTP calls from the connector to e-Stack. Every call is outbound HTTPS."""
from typing import Any

import httpx

LONG_POLL_SECONDS = 25
REQUEST_TIMEOUT = httpx.Timeout(10.0, read=LONG_POLL_SECONDS + 10.0)


class UnauthorizedError(RuntimeError):
    """The token was refused: revoked, rotated or mistyped. Retrying will not help."""


class EstackClient:
    def __init__(self, base_url: str, token: str, version: str, http: httpx.Client | None = None):
        self._http = http or httpx.Client(
            base_url=base_url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": f"estack-db-connector/{version}"},
        )
        self._headers = {"Authorization": f"Bearer {token}"}

    def _check(self, response: httpx.Response) -> httpx.Response:
        if response.status_code == 401:
            raise UnauthorizedError("Token do conector recusado pelo e-Stack.")
        response.raise_for_status()
        return response

    def heartbeat(self, version: str, databases: list[dict[str, Any]]) -> None:
        self._check(
            self._http.post(
                "connector/heartbeat",
                json={"version": version, "databases": databases},
                headers=self._headers,
            )
        )

    def poll_jobs(self, wait: int = LONG_POLL_SECONDS) -> list[dict[str, Any]]:
        response = self._check(
            self._http.get("connector/jobs", params={"wait": wait}, headers=self._headers)
        )
        return response.json()

    def submit(self, job_id: str, result: dict[str, Any] | None, error: str | None) -> None:
        response = self._http.post(
            f"connector/jobs/{job_id}/result",
            json={"result": result, "error": error},
            headers=self._headers,
        )
        if response.status_code == 404:
            return
        self._check(response)

    def close(self) -> None:
        self._http.close()
