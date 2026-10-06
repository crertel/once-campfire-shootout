"""Make Docker bind mounts writable under rootless Docker.

Rootless Docker maps container uid 0 to the host user. A mount the host user
created therefore looks like root:root inside the container, and the port
scripts' ``--user $(id -u):$(id -g)`` cannot write it. Running those
containers as uid 0 writes files the host user owns.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def endpoint_is_rootless(endpoint: str) -> bool:
    return "/run/user/" in endpoint


def docker_is_rootless() -> bool:
    endpoint = os.environ.get("DOCKER_HOST", "")
    if not endpoint:
        try:
            completed = subprocess.run(
                ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                check=False, capture_output=True, text=True,
            )
        except OSError:
            return False
        endpoint = completed.stdout.strip()
    return endpoint_is_rootless(endpoint)


def rewrite_docker_args(argv: list[str], host_user: str) -> list[str]:
    """Force container-root on ``docker run`` when the caller asked for the host uid.

    A run that already names some other user is left alone. ``docker build``
    and every other subcommand pass through.
    """
    if not argv or argv[0] != "run":
        return list(argv)
    args = list(argv)
    for index, arg in enumerate(args):
        if arg == "--user" and index + 1 < len(args):
            if args[index + 1] == host_user:
                args[index + 1] = "0:0"
            return args
        if arg.startswith("--user="):
            if arg.split("=", 1)[1] == host_user:
                args[index] = "--user=0:0"
            return args
    return ["run", "--user", "0:0", *args[1:]]


def install_rootless_shim() -> Path | None:
    if not docker_is_rootless():
        return None
    real = _real_docker()
    directory = Path(tempfile.mkdtemp(prefix="campfire-shootout-docker-"))
    host_user = f"{os.getuid()}:{os.getgid()}"
    script = directory / "docker"
    module = Path(__file__).resolve()
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"sys.path.insert(0, {str(module.parent.parent)!r})\n"
        "from shootout.docker_shim import rewrite_docker_args\n"
        f"REAL = {real!r}\n"
        f"HOST = {host_user!r}\n"
        "os.execv(REAL, ['docker', *rewrite_docker_args(sys.argv[1:], HOST)])\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
    print(
        "rootless Docker: containers run as uid 0, which is this user, so the seed and images can write their mounts",
        file=sys.stderr,
    )
    return directory


def _real_docker() -> str:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "docker"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise SystemExit("docker is not on PATH")
