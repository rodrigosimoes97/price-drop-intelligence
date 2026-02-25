from __future__ import annotations

import json
import logging
import os
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)


def to_affiliate_url(canonical_url: str, cfg: dict | None = None) -> str:
    cfg = cfg or {}
    mode = str(cfg.get("mode", "template")).lower()

    if mode == "template":
        template = cfg.get("template")
        if template:
            return str(template).replace("{url}", quote_plus(canonical_url))
        return canonical_url

    if mode == "api":
        base = os.getenv("SHOPEE_AFFILIATE_API_BASE", "")
        token = os.getenv("SHOPEE_AFFILIATE_TOKEN", "")
        if not base or not token:
            return canonical_url
        try:
            req = Request(
                f"{base.rstrip('/')}/deeplink",
                data=json.dumps({"url": canonical_url}).encode("utf-8"),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return str(payload.get("affiliate_url") or canonical_url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("affiliate api fallback due to %s", type(exc).__name__)
            return canonical_url

    return canonical_url
