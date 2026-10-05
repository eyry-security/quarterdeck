"""Persistent Docker sandbox for Quarterdeck agents.

Two problems solved vs pinnace's throwaway DockerSandbox:

1. /work is a HOST directory (~/.quarterdeck/agents/<name>/work) mounted
   into the container. Files survive container restarts, daemon restarts,
   and droplet reboots.
2. reset() gives the agent (or seed) a clean slate on demand: stops the
   container, wipes the host workdir, starts fresh from the image.

The image (qd-agent:latest, see quarterdeck/docker/Dockerfile) ships the
recon toolchain baked in so agents don't apt-get on every boot.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pinnace.sandbox import DockerSandbox, SandboxError

from .agent_registry import home_dir

IMAGE = "qd-agent:latest"
WORKDIR = "/work"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip().lower())


class PersistentDockerSandbox(DockerSandbox):
    """DockerSandbox with host-persistent /work and reset()."""

    def __init__(
        self,
        agent_name: str,
        home: Path | None = None,
        image: str = IMAGE,
        workdir: str = WORKDIR,
        network: str | None = None,
        mem_limit: str = "1g",
    ) -> None:
        # DockerSandbox.__init__ starts the container without volumes, so we
        # replicate its setup here with a host volume mounted at /work.
        try:
            import docker
        except ImportError as e:
            raise SandboxError("the 'docker' package isn't installed (pip install docker)") from e
        try:
            client = docker.from_env()
            client.ping()
        except Exception as e:
            raise SandboxError(f"docker daemon not reachable: {e}") from e

        self.agent_name = agent_name.strip()
        self.home = Path(home) if home else home_dir()
        self.host_workdir = self.home / "agents" / _safe(self.agent_name) / "work"
        self.host_workdir.mkdir(parents=True, exist_ok=True)
        self.workdir = workdir
        self._image = image
        self._network = network
        self._mem_limit = mem_limit
        self._client = client
        self._start_container()

    def _start_container(self) -> None:
        run_kwargs: dict = {
            "image": self._image,
            "command": "sleep infinity",
            "detach": True,
            "working_dir": self.workdir,
            "mem_limit": self._mem_limit,
            "stdin_open": True,
            "volumes": {str(self.host_workdir): {"bind": self.workdir, "mode": "rw"}},
        }
        if self._network is not None:
            run_kwargs["network_mode"] = self._network
        try:
            self._container = self._client.containers.run(**run_kwargs)
        except Exception as e:
            raise SandboxError(f"couldn't start {self._image}: {e}") from e

    def reset(self) -> str:
        """Wipe /work and restart from a clean image. Returns a summary."""
        # Stop + remove the old container.
        try:
            self._container.remove(force=True)
        except Exception:
            pass
        # Wipe the host workdir (files are the persistent part).
        wiped = 0
        for child in self.host_workdir.iterdir():
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                wiped += 1
            except OSError:
                pass
        # Fresh container from the clean image.
        self._start_container()
        return (
            f"environment reset: container rebuilt from {self._image}, "
            f"{wiped} entr{'y' if wiped == 1 else 'ies'} wiped from /work"
        )

    def workdir_size(self) -> str:
        """Human-readable size of the persistent workdir."""
        total = sum(
            f.stat().st_size for f in self.host_workdir.rglob("*") if f.is_file()
        )
        for unit in ("B", "KB", "MB", "GB"):
            if total < 1024 or unit == "GB":
                return f"{total:.1f} {unit}"
            total /= 1024
        return f"{total:.1f} GB"
