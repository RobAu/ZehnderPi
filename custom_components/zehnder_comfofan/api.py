"""Client for the HTTP API served by zehnder.py on the Raspberry Pi."""

import asyncio

import aiohttp

# A command is retried over the radio for up to 10 seconds before it fails.
TIMEOUT = 20


class ZehnderError(Exception):
    """The API could not be reached or the fan did not answer."""


class ZehnderApi:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"

    async def _request(self, method: str, path: str) -> dict:
        try:
            async with asyncio.timeout(TIMEOUT):
                response = await self._session.request(method, self._base + path)
                body = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise ZehnderError(f"Cannot reach the Zehnder API: {error}") from error
        if response.status != 200:
            raise ZehnderError(body.get("error", f"HTTP {response.status}"))
        return body

    async def info(self) -> dict:
        return await self._request("GET", "/")

    async def status(self) -> dict:
        return await self._request("GET", "/status")

    async def set_speed(self, speed: str) -> dict:
        return await self._request("POST", f"/speed/{speed}")

    async def set_timer(self, speed: str, minutes: int) -> dict:
        return await self._request("POST", f"/timer/{speed}/{minutes}")
