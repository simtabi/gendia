"""npm registry (push model).

Implementation strategy: shell out to `npm publish` with `NPM_TOKEN` set in
the environment. We do not re-implement the npm publish protocol; the CLI is
authoritative and well-maintained.

This keeps the dependency surface tiny but does require `npm` on the host /
in the Docker image. Add it to the image only if you actually publish to npm.
"""

from __future__ import annotations

import os
from pathlib import Path

from gendia.git.shell import run as shell_run
from gendia.registries.base import PublishCapable, RegistryError


class NpmRegistry(PublishCapable):
    @property
    def kind(self) -> str:
        return "npm"

    def publish(self, package_path: Path, *, version: str) -> None:
        if not (package_path / "package.json").is_file():
            raise RegistryError(f"npm: {package_path} has no package.json")
        if not self._token:
            raise RegistryError("npm: no NPM_TOKEN configured")

        env = os.environ.copy()
        env["NPM_TOKEN"] = self._token
        # The standard token-based auth pattern: write a one-line .npmrc.
        rc = package_path / ".npmrc"
        rc_existed = rc.exists()
        rc.write_text(
            "//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n",
            encoding="utf-8",
        )
        try:
            shell_run(
                ["npm", "publish", "--access", "public"],
                cwd=package_path,
                env=env,
                timeout=180.0,
            )
            self._log.info("npm published", extra={"path": str(package_path), "version": version})
        finally:
            if not rc_existed:
                rc.unlink(missing_ok=True)
