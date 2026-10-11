import httpx

from financial_hub.main import create_app

BASELINE = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "content-security-policy": "frame-ancestors 'none'",
    "x-frame-options": "DENY",
}


async def get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


async def test_every_response_carries_the_baseline_headers() -> None:
    for path in ("/health/live", "/v1/auth/me", "/does-not-exist"):
        response = await get(path)
        for name, value in BASELINE.items():
            assert response.headers[name] == value, (path, name)


async def test_auth_responses_are_never_cached() -> None:
    auth = await get("/v1/auth/me")  # 401 without a session: no database needed
    health = await get("/health/live")

    assert auth.status_code == 401
    assert auth.headers["cache-control"] == "no-store"
    assert auth.headers["pragma"] == "no-cache"
    assert "cache-control" not in health.headers
