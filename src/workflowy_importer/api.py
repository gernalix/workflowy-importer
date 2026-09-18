from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx


class WorkflowyAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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

    def _request(
        self,
        method: str,
        path: str,
        *,
        retry_safe: bool = False,
        **kwargs,
    ) -> httpx.Response:
        attempts = self.max_retries + 1 if retry_safe else 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                time.sleep(min(2**attempt, 8))
                continue

            retryable_status = response.status_code in {429, 500, 502, 503, 504}
            if retryable_status and retry_safe and attempt + 1 < attempts:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(2**attempt, 8)
                except ValueError:
                    delay = min(2**attempt, 8)
                time.sleep(max(0.0, min(delay, 60.0)))
                continue

            if response.is_error:
                raise WorkflowyAPIError(
                    f"{method} {path} failed: HTTP {response.status_code}: "
                    f"{response.text[:500]}",
                    status_code=response.status_code,
                )
            return response

        raise WorkflowyAPIError(
            f"{method} {path} failed: {last_error or 'request failed before a response'}"
        )

    def create_node(
        self,
        parent_id: str,
        name: str,
        layout_mode: str = "bullets",
        position: str = "bottom",
    ) -> str:
        # Node creation is not idempotent. Never retry automatically: if the
        # server created the node but the response was lost, a retry could
        # silently create a duplicate.
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

    def get_node(self, node_id: str) -> dict:
        response = self._request("GET", f"/nodes/{node_id}", retry_safe=True)
        node = response.json().get("node")
        if not isinstance(node, dict):
            raise WorkflowyAPIError("Retrieve node response did not contain a node object")
        return node

    def list_nodes(self, parent_id: str) -> list[dict]:
        response = self._request(
            "GET",
            "/nodes",
            params={"parent_id": parent_id},
            retry_safe=True,
        )
        nodes = response.json().get("nodes")
        if not isinstance(nodes, list):
            raise WorkflowyAPIError("List nodes response did not contain a nodes array")
        return [node for node in nodes if isinstance(node, dict)]

    def node_exists(self, node_id: str) -> bool:
        try:
            self.get_node(node_id)
            return True
        except WorkflowyAPIError as exc:
            if exc.status_code == 404:
                return False
            raise
