"""Answer a request from inside a dependency with a ready-made response (replays, 409/429)."""

from fastapi import Request
from starlette.responses import Response


class ShortCircuit(Exception):
    """Raised by a dependency: the route handler is skipped and ``response`` is sent as is."""

    def __init__(self, response: Response) -> None:
        super().__init__(response.status_code)
        self.response = response


async def short_circuit_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, ShortCircuit):  # pragma: no cover - registered for ShortCircuit only
        raise exc
    return exc.response
