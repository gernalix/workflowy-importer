from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx


class WorkflowyAPIError(RuntimeError):
    pass


@dataclass(slots=True)
class WorkflowyClient:
    api_key: str
    base_url: str = "https://workflowy.com/api/v1"
    max_retries: int = 5
    timeout: float = 30.0
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            base_url=self.base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "workflowy-importer/0.1",
            },
            timeout=self.timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "WorkflowyClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2**attempt, 8))
                continue

            if response.status_code not in {429, 500, 502, 503, 504}:
                if response.is_error:
                    raise WorkflowyAPIError(
                        f"{method} {path} failed: HTTP {response.status_code}: {response.text[:500]}"
                    )
                return response

            if attempt >= self.max_retries:
                last_error = WorkflowyAPIError(
                    f"{method} {path} failed after retries: HTTP {response.status_code}"
                )
                break

            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else min(2**attempt, 8)
            except ValueError:
                delay = min(2**attempt, 8)
            time.sleep(max(0.0, min(delay, 60.0)))

        raise WorkflowyAPIError(str(last_error or "Workflowy API request failed"))

    def create_node(
        self,
        parent_id: str,
        name: str,
        layout_mode: str = "bullets",
        position: str = "bottom",
    ) -> str:
        response = self._request(
            "POST",
            "/nodes",
            json={
                "parent_id": parent_id,
                "name": name,
                "layoutMode": layout_mode,
                "position": position,
            },
        )
        item_id = response.json().get("item_id")
        if not item_id:
            raise WorkflowyAPIError("Create node response did not contain item_id")
        return str(item_id)

    def update_node(self, node_id: str, name: str) -> None:
        self._request("POST", f"/nodes/{node_id}", json={"name": name})

    def complete_node(self, node_id: str) -> None:
        self._request("POST", f"/nodes/{node_id}/complete")

    def delete_node(self, node_id: str) -> None:
        self._request("DELETE", f"/nodes/{node_id}")

    def node_exists(self, node_id: str) -> bool:
        try:
            self._request("GET", f"/nodes/{node_id}")
            return True
        except WorkflowyAPIError as exc:
            if "HTTP 404" in str(exc):
                return False
            raise
