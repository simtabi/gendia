"""PyPI registry (push model).

Shells out to `python -m twine upload`, the canonical publishing tool. The
host / image must have `twine` installed; gendia does not depend on it.
"""

from __future__ import annotations

import os
from pathlib import Path

from gendia.git.shell import run as shell_run
from gendia.registries.base import PublishCapable, RegistryError


class PyPIRegistry(PublishCapable):
    @property
    def kind(self) -> str:
        return "pypi"

    def publish(self, package_path: Path, *, version: str) -> None:
        # Build first if no dist/ exists.
        dist = package_path / "dist"
        if not dist.is_dir() or not any(dist.iterdir()):
            shell_run(
                ["python", "-m", "build"],
                cwd=package_path,
                timeout=180.0,
            )

        if not self._token:
            raise RegistryError("pypi: no PYPI_API_TOKEN configured")

        env = os.environ.copy()
        env.update(
            {
                "TWINE_USERNAME": "__token__",
                "TWINE_PASSWORD": self._token,
            }
        )

        artifacts = sorted(str(p) for p in dist.glob("*"))
        if not artifacts:
            raise RegistryError(f"pypi: no build artifacts under {dist}")

        shell_run(
            ["python", "-m", "twine", "upload", *artifacts],
            cwd=package_path,
            env=env,
            timeout=180.0,
        )
        self._log.info("pypi published", extra={"path": str(package_path), "version": version})
