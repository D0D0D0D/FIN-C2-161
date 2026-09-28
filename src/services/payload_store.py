"""FIN-C2-161 payload-store construction seam."""

from __future__ import annotations

from collections.abc import Callable

from src.payload_store import PayloadStore


def build_payload_store(*, ttl_seconds: int, clock: Callable[[], float] | None = None) -> PayloadStore:
    kwargs = {"scope_keys": ("institution_id",), "ttl_seconds": ttl_seconds}
    if clock is not None:
        kwargs["clock"] = clock
    return PayloadStore(**kwargs)
