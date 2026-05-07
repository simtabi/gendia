"""Optional OS-keychain backend.

Uses the `keyring` package when installed. On macOS it talks to the Keychain;
on Linux to libsecret / kwallet; on Windows to the Credential Manager. The
import is deferred so that gendia's required dependency footprint stays at
zero.

A secret stored via `keyring set gendia <KEY>` is retrieved by `get(KEY)`.
"""

from __future__ import annotations

from gendia.auth.base import CredentialStore


class KeyringCredentialStore(CredentialStore):
    _SERVICE = "gendia"

    def __init__(self) -> None:
        # Lazy probe: importing here keeps `keyring` an optional install. The
        # real `import keyring` happens inside `get()` so this constructor stays
        # cheap when called speculatively.
        try:
            import keyring  # type: ignore[import-not-found]  # noqa: F401, PLC0415 — deferred optional dep
        except ImportError as exc:
            raise RuntimeError(
                "keyring backend requires the optional `keyring` package. "
                "Install with `pip install gendia[keyring]`."
            ) from exc

    @property
    def name(self) -> str:
        return "keyring"

    def get(self, key: str) -> str | None:
        import keyring  # noqa: PLC0415 — deferred optional dep

        try:
            value = keyring.get_password(self._SERVICE, key)
        except Exception:  # noqa: BLE001 — keyring backends throw a wide variety of errors
            return None
        return str(value) if value else None
