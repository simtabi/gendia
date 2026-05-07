"""Packagist (the PHP/Composer registry).

Webhook-style: we POST a repo URL to `/api/update-package` with our
username + API token, and Packagist pulls the latest tags.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from gendia.registries.base import RegistryError, WebhookCapable


class PackagistRegistry(WebhookCapable):
    _ENDPOINT = "https://packagist.org/api/update-package"

    @property
    def kind(self) -> str:
        return "packagist"

    def notify(self, repo_url: str) -> None:
        if not self._token or not self._config.username:
            raise RegistryError(
                "packagist: registry config requires both `username` and `credential_ref`"
            )

        params = urllib.parse.urlencode(
            {
                "username": self._config.username,
                "apiToken": self._token,
            }
        )
        url = f"{self._ENDPOINT}?{params}"
        body = json.dumps({"repository": {"url": repo_url}}).encode("utf-8")

        # The URL is built from the constant `_ENDPOINT` (https://packagist.org/...);
        # scheme is fixed and not user-controlled. The S310 audit warning is
        # a false positive in this context.
        req = urllib.request.Request(  # noqa: S310 — scheme fixed at module top
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "gendia/0.1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:  # noqa: S310 — scheme fixed
                if resp.status not in (200, 202):
                    raise RegistryError(
                        f"packagist: unexpected status {resp.status} for {repo_url}"
                    )
                self._log.info(
                    "packagist notified",
                    extra={"repo": repo_url, "status": resp.status},
                )
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            raise RegistryError(f"packagist: HTTP {exc.code} for {repo_url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RegistryError(f"packagist: network error for {repo_url}: {exc}") from exc
