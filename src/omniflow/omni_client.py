from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .exceptions import OmniAPIError, OmniAuthError
from .security import redact

LOG = logging.getLogger(__name__)


class OmniClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: int = 60,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def get_model_yaml(
        self,
        model_id: str,
        branch_id: str | None = None,
        mode: str = "combined",
        include_checksums: bool = True,
        fully_resolved: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "mode": mode,
            "includeChecksums": str(include_checksums).lower(),
            "fullyResolved": str(fully_resolved).lower(),
        }
        if branch_id:
            params["branchId"] = branch_id
        return self._request("GET", f"/api/v1/models/{model_id}/yaml", params=params)

    def validate_model(self, model_id: str, branch_id: str | None = None) -> list[dict[str, Any]]:
        params = {"branchId": branch_id} if branch_id else None
        payload = self._request("GET", f"/api/v1/models/{model_id}/validate", params=params)
        if not isinstance(payload, list):
            raise OmniAPIError("Model validation returned an unexpected response shape")
        return [item for item in payload if isinstance(item, dict)]

    def validate_content(
        self,
        model_id: str,
        branch_id: str | None = None,
        user_id: str | None = None,
        include_personal_folders: bool = False,
        find: str | None = None,
        find_type: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if user_id:
            params["userId"] = user_id
        if branch_id:
            params["branch_id"] = branch_id
        if include_personal_folders:
            params["include_personal_folders"] = "true"
        if find:
            params["find"] = find
        if find_type:
            params["find_type"] = _content_validator_find_type(find_type)
        return self._request("GET", f"/api/v1/models/{model_id}/content-validator", params=params)

    def search_content_references(
        self,
        model_id: str,
        *,
        find: str,
        find_type: str,
        branch_id: str | None = None,
        user_id: str | None = None,
        include_personal_folders: bool = False,
    ) -> Any:
        return self.validate_content(
            model_id,
            branch_id=branch_id,
            user_id=user_id,
            include_personal_folders=include_personal_folders,
            find=find,
            find_type=find_type,
        )

    def get_git_configuration(self, model_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/api/v1/models/{model_id}/git")
        if not isinstance(payload, dict):
            raise OmniAPIError("Git configuration returned an unexpected response shape")
        return payload

    def list_models(
        self,
        model_kind: str | None = None,
        base_model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if model_kind:
            params["modelKind"] = model_kind
        if base_model_id:
            params["baseModelId"] = base_model_id
        return self._paginate("/api/v1/models", params=params)

    def resolve_branch_id(self, model_id: str, branch_name: str | None) -> str | None:
        if not branch_name:
            return None
        for record in self.list_models():
            if record.get("modelKind") != "BRANCH":
                continue
            if record.get("baseModelId") != model_id:
                continue
            if record.get("name") == branch_name:
                return record.get("id")
        return None

    def list_content(
        self,
        labels: list[str] | None = None,
        branch_id: str | None = None,
        include_personal_folders: bool = False,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if labels:
            params["include"] = "labels"
            params["labels"] = ",".join(labels)
        return self._paginate("/api/v1/content", params=params)

    def _paginate(self, path: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        cursor = None
        while True:
            page_params = dict(params or {})
            if cursor:
                page_params["cursor"] = cursor
            payload = self._request("GET", path, params=page_params)
            page_records = payload.get("records", []) if isinstance(payload, dict) else []
            records.extend([item for item in page_records if isinstance(item, dict)])
            cursor = payload.get("pageInfo", {}).get("nextCursor") if isinstance(payload, dict) else None
            if not cursor:
                return records

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_payload,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 3:
                    raise OmniAPIError(f"Omni API request failed: {redact(str(exc))}") from exc
                time.sleep(2**attempt)
                continue

            if response.status_code in {401, 403}:
                raise OmniAuthError(f"Omni authorization failed: {response.status_code}")
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < 3:
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                    time.sleep(retry_after if retry_after is not None else 2**attempt)
                    continue
            if not response.ok:
                raise OmniAPIError(
                    f"Omni API request failed: {response.status_code} {redact(response.text[:500])}"
                )
            try:
                return response.json()
            except ValueError as exc:
                raise OmniAPIError(f"Omni API did not return JSON: {exc}") from exc

        LOG.debug("Final Omni request error: %s", redact(str(last_error)))
        raise OmniAPIError("Omni API request failed after retries")


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, min(60, int(value)))
    except ValueError:
        return None


def _content_validator_find_type(value: str) -> str:
    normalized = value.strip().upper()
    aliases = {
        "FIELD": "FIELD",
        "VIEW": "VIEW",
        "TOPIC": "TOPIC",
    }
    return aliases.get(normalized, normalized)
