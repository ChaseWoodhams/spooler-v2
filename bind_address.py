"""Where Spooler listens: the BIND_HOST setting and which values count as loopback or wildcard.

Dependency-free so the supervisor can import it without pulling in the server.
"""
import os

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
WILDCARD_HOSTS = ("", "0.0.0.0", "::")
# Hosts a local health probe can reach as 127.0.0.1.
LOCAL_PROBE_HOSTS = WILDCARD_HOSTS + ("localhost", "::1")


def configured_bind_host(raw: str | None) -> str:
    """The address to bind for a raw BIND_HOST value: this machine only unless it opts in to another."""
    return (raw or "").strip() or "127.0.0.1"


def bind_host() -> str:
    return configured_bind_host(os.getenv("BIND_HOST"))


def probe_host(raw: str | None) -> str:
    """Host a local health probe should use for a raw BIND_HOST value."""
    host = (raw or "").strip()
    return "127.0.0.1" if host in LOCAL_PROBE_HOSTS else host
