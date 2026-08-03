"""Fail-closed endpoint checks for commands that mutate a DataHub graph."""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

from dotenv import load_dotenv


DEFAULT_DATAHUB_GMS_URL = "http://localhost:8080"
NON_LOOPBACK_APPROVAL_TOKEN = (
    "RIPPLE_PROOF_APPROVE_NON_LOOPBACK_DATAHUB_MUTATION"
)


class MutationSafetyError(ValueError):
    """A graph mutation was not authorized for the configured endpoint."""


def configured_datahub_url() -> str:
    """Return the explicit mutation endpoint, defaulting only to local DataHub."""
    load_dotenv()
    endpoint = os.environ.get("DATAHUB_GMS_URL", DEFAULT_DATAHUB_GMS_URL).strip()
    if not endpoint:
        raise MutationSafetyError("DATAHUB_GMS_URL cannot be empty")
    return endpoint


def is_loopback_datahub_url(endpoint: str) -> bool:
    """Return whether an HTTP(S) endpoint names a loopback-only host."""
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise MutationSafetyError(
            "DATAHUB_GMS_URL must be a valid HTTP(S) URL"
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise MutationSafetyError(
            "DATAHUB_GMS_URL must be an HTTP(S) URL without embedded credentials"
        )
    normalized_host = hostname.rstrip(".").lower()
    if normalized_host == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized_host).is_loopback
    except ValueError:
        return False


def require_datahub_mutation_permission(
    *,
    endpoint: str | None = None,
    allow_non_loopback: bool = False,
    approval_token: str | None = None,
) -> str:
    """Authorize a mutation endpoint and return its validated URL."""
    selected = configured_datahub_url() if endpoint is None else endpoint.strip()
    if not selected:
        raise MutationSafetyError("DATAHUB_GMS_URL cannot be empty")
    if is_loopback_datahub_url(selected):
        return selected
    if not allow_non_loopback:
        raise MutationSafetyError(
            "Refusing to mutate non-loopback DataHub endpoint "
            f"{selected!r}. Only localhost/loopback is allowed by default. "
            "A deliberate exception requires "
            "--allow-non-loopback-datahub-mutation and the exact approval token."
        )
    if approval_token != NON_LOOPBACK_APPROVAL_TOKEN:
        raise MutationSafetyError(
            "Non-loopback DataHub mutation approval was not exact. Pass "
            "--non-loopback-approval-token "
            f"{NON_LOOPBACK_APPROVAL_TOKEN}"
        )
    return selected
