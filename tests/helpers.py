from __future__ import annotations

import uuid

import httpx


async def register_and_login(client: httpx.AsyncClient) -> dict[str, str]:
    email = f"user-{uuid.uuid4().hex}@example.com"
    password = "password12345"
    register = await client.post(
        "/api/v1/users",
        json={"email": email, "password": password},
    )
    register.raise_for_status()
    response = await client.post(
        "/api/v1/sessions",
        json={"email": email, "password": password},
    )
    response.raise_for_status()
    return response.json()
