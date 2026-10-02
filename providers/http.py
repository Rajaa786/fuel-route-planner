"""Shared plumbing for the httpx-based provider clients."""

from collections.abc import Mapping
from typing import Any

import httpx

from providers.base import ProviderRateLimitedError, ProviderTimeoutError, ProviderUnavailableError


def build_client(*, user_agent: str, connect_timeout: float, read_timeout: float) -> httpx.Client:
    """Create a pooled client.

    One long-lived client per process keeps TCP/TLS connections alive, which
    removes a handshake (often 100-300 ms) from every request after the first.
    """
    return httpx.Client(
        timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=5.0, pool=2.0),
        headers={"User-Agent": user_agent, "Accept": "application/json"},
        transport=httpx.HTTPTransport(retries=1),  # retries connection failures only
        follow_redirects=False,
    )


def get_json(
    client: httpx.Client, url: str, params: Mapping[str, Any], *, provider: str
) -> tuple[int, Any]:
    """GET ``url`` and return ``(status_code, parsed_json)``.

    Transport problems, throttling and server errors are raised as provider
    errors. 4xx responses are returned so callers can interpret the provider's
    own error vocabulary.
    """
    try:
        response = client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise ProviderTimeoutError(f"{provider} timed out.") from exc
    except httpx.HTTPError as exc:
        raise ProviderUnavailableError(f"{provider} is unreachable: {exc!r}") from exc

    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        retry_after = response.headers.get("Retry-After", "")
        raise ProviderRateLimitedError(
            f"{provider} rate limit exceeded.", int(retry_after) if retry_after.isdigit() else None
        )
    if response.status_code >= 500:
        raise ProviderUnavailableError(f"{provider} returned HTTP {response.status_code}.")

    try:
        return response.status_code, response.json()
    except ValueError as exc:
        raise ProviderUnavailableError(f"{provider} returned a non-JSON response.") from exc
