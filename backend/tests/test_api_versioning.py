"""The public API is versioned: everything lives under /api/v1 and nothing is left unversioned."""

import httpx
from fastapi import FastAPI

from tests.conftest import VALID_PAYLOAD


async def test_openapi_paths_are_all_versioned(app: FastAPI) -> None:
    paths = set(app.openapi()["paths"])

    assert paths == {
        "/api/v1/health",
        "/api/v1/submissions",
        "/api/v1/submissions/{tracking_id}/status",
    }


async def test_unversioned_health_path_is_gone(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health")

    assert response.status_code == 404


async def test_unversioned_submissions_path_is_gone(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/submissions", json=VALID_PAYLOAD)

    assert response.status_code == 404
