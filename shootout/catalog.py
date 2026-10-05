"""The blessed ports and how this harness builds and launches each one.

The set is the implementations named from the Basecamp Rails README: Rails
itself, Django, Laravel, Express, Elixir, Go and Rust. Each build follows
that repository's own production Dockerfile. Launch matches the two official
comparison drivers (the Express ``bench/compare.rb`` driver for the
script-language ports, and the Elixir ``bench/run`` driver for Elixir, Go
and Rust): production image, host networking, the shared parity seed, gzip
through the public listener, four server CPUs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class App:
    name: str
    title: str
    checkout: str
    image: str
    # argv templates. {rev} is this checkout's HEAD, {reference_rev} is its
    # Rails submodule when it has one, {image} is the tag above.
    build: tuple[tuple[str, ...], ...]
    # "host" runs the container as the invoking uid:gid, which is what the
    # Elixir driver does for images that drop to uid 1000. Empty keeps the
    # image default. Laravel's entrypoint chowns storage as root and then
    # drops to www-data, so it must stay root.
    run_user: str
    # Environment variables set to the Puma-style worker count.
    worker_env: tuple[str, ...]
    port_offsets: tuple[int, ...]
    binds_redis: bool
    redis_sidecar: bool
    note: str
    # Kept out of the default set. `parse_apps` still accepts the name.
    optional: bool = False


REDIS_IMAGE = "redis:7.2-alpine"
REFERENCE_ENV = ("once-campfire-elixir", "parity", "reference.env")
SEED_CHECKOUT = "once-campfire-rust"
SEED_RELATIVE = ("parity", ".seed", "default")
LOADGEN_CHECKOUT = "once-campfire-elixir"
LOADGEN_RELATIVE = ("bench", "loadgen")

# Published comparison order, which is also the README column order.
APPS: tuple[App, ...] = (
    App(
        name="rails",
        title="Rails",
        checkout="once-campfire",
        image="campfire-rails:shootout",
        build=(
            (
                "docker", "build",
                "--build-arg", "GIT_REVISION={rev}",
                "--build-arg", "APP_VERSION=shootout",
                "-t", "{image}",
                ".",
            ),
        ),
        run_user="",
        worker_env=("WEB_CONCURRENCY", "JOB_CONCURRENCY"),
        port_offsets=(0, 1),
        binds_redis=True,
        redis_sidecar=False,
        note="Current basecamp/once-campfire main, Thruster in front of Puma, Redis and Resque inside the image.",
    ),
    App(
        name="django",
        title="Django",
        checkout="once-campfire-django",
        image="once-campfire-django:shootout",
        build=(
            ("docker", "pull", REDIS_IMAGE),
            (
                "docker", "build",
                "--build-arg", "REVISION={rev}",
                "-t", "{image}",
                ".",
            ),
        ),
        run_user="",
        worker_env=("WEB_WORKERS",),
        port_offsets=(0, 2),
        binds_redis=False,
        redis_sidecar=True,
        note="Uvicorn. More than one worker needs the Redis sidecar on the HTTP port plus 2.",
    ),
    App(
        name="laravel",
        title="Laravel",
        checkout="once-campfire-laravel",
        image="once-campfire-laravel:shootout",
        build=(
            ("docker", "build", "-t", "{image}", "."),
        ),
        run_user="",
        worker_env=(),
        port_offsets=(0, 1000, 2000),
        binds_redis=False,
        redis_sidecar=False,
        note="nginx and eight PHP-FPM workers, as the image ships. Cable is HTTP+1000 and FPM is HTTP+2000.",
    ),
    App(
        name="express",
        title="Express",
        checkout="once-campfire-express",
        image="once-campfire-express:shootout",
        build=(
            (
                "docker", "build",
                "--build-arg", "REVISION={rev}",
                "-t", "{image}",
                ".",
            ),
        ),
        run_user="",
        worker_env=("WEB_WORKERS",),
        port_offsets=(0,),
        binds_redis=False,
        redis_sidecar=False,
        note="Node cluster. Jobs stay in the process and use the mounted SQLite database.",
    ),
    App(
        name="elixir",
        title="Elixir",
        checkout="once-campfire-elixir",
        image="campfire-elixir:shootout",
        build=(
            (
                "docker", "build",
                "--build-arg", "GIT_REVISION={reference_rev}",
                "--build-arg", "APP_VERSION=shootout",
                "-t", "campfire-reference:app",
                "reference",
            ),
            ("docker", "build", "-f", "Dockerfile.dev", "-t", "campfire-elixir:toolchain", "."),
            ("bin/mix", "local.hex", "--force"),
            ("bin/mix", "local.rebar", "--force"),
            ("bin/mix", "deps.get"),
            ("bin/export-assets",),
            (
                "docker", "build",
                "--build-arg", "GIT_REVISION={rev}",
                "--build-arg", "APP_VERSION=shootout",
                "-t", "{image}",
                ".",
            ),
        ),
        run_user="host",
        worker_env=("WEB_CONCURRENCY", "JOB_CONCURRENCY"),
        port_offsets=(0, 1),
        binds_redis=True,
        redis_sidecar=False,
        note="Bandit behind the Rails Thruster binary, Redis inside the container. The documented build writes Hex, deps and exported assets into the checkout, and retags campfire-reference:app from this repo's Rails submodule.",
    ),
    App(
        name="go",
        title="Go",
        checkout="once-campfire-go",
        image="once-campfire-go:shootout",
        build=(
            (
                "docker", "build",
                "--build-arg", "GIT_REVISION={rev}",
                "--build-arg", "APP_VERSION=shootout",
                "-t", "{image}",
                ".",
            ),
        ),
        run_user="host",
        worker_env=("JOB_CONCURRENCY",),
        port_offsets=(0, 1),
        binds_redis=False,
        redis_sidecar=False,
        note="One process. Its own listener replaces Thruster. The image still honors HTTP_PORT and TARGET_PORT.",
    ),
    App(
        name="rust",
        title="Rust",
        checkout="once-campfire-rust",
        image="campfire-rust:shootout",
        build=(
            (
                "docker", "build",
                "--build-arg", "GIT_REVISION={rev}",
                "--build-arg", "APP_VERSION=shootout",
                "-t", "{image}",
                ".",
            ),
        ),
        run_user="host",
        worker_env=("JOB_CONCURRENCY",),
        port_offsets=(0, 1),
        binds_redis=False,
        redis_sidecar=False,
        note="One process, its own HTTP listener. This measures the checkout's Dockerfile, not a previously published image id.",
    ),
)

# Local ports that use the same Docker launch contract but are not part of the
# default comparison. Select them by name: --apps dotnet
OPTIONAL_APPS: tuple[App, ...] = (
    App(
        name="dotnet",
        title="ASP.NET",
        checkout="once-campfire-dotnet",
        image="once-campfire-dotnet:shootout",
        build=(
            (
                "docker", "build",
                "--build-arg", "GIT_REVISION={rev}",
                "--build-arg", "APP_VERSION=shootout",
                "-t", "{image}",
                ".",
            ),
        ),
        run_user="",
        worker_env=(),
        port_offsets=(0, 1),
        binds_redis=False,
        redis_sidecar=False,
        optional=True,
        note="Kestrel behind the Rails Thruster binary. Same HTTP_PORT, TARGET_PORT, and /rails/storage mounts. No Redis. The server opens production.sqlite3 with its own schema, so the Rails parity seed is left unchanged and is not served.",
    ),
)

BY_NAME = {app.name: app for app in (*APPS, *OPTIONAL_APPS)}

ROUTES = (
    "room_show",
    "messages_page",
    "sidebar",
    "search",
    "post_message",
)

ROUTE_TITLES = {
    "room_show": "Room page",
    "messages_page": "Messages page",
    "sidebar": "Sidebar",
    "search": "Search",
    "post_message": "Post a message",
    "avatar": "Avatar",
    "static_css": "Stylesheet",
    "up": "Health",
}

EXTRA_ROUTES = ("avatar", "static_css", "up")


def parse_apps(text: str) -> tuple[App, ...]:
    if text.strip() in {"", "all"}:
        return APPS
    names = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [name for name in names if name not in BY_NAME]
    if unknown or not names or len(names) != len(set(names)):
        known = ", ".join(app.name for app in (*APPS, *OPTIONAL_APPS))
        raise ValueError(f"apps must be a unique subset of {known}")
    return tuple(BY_NAME[name] for name in names)


def worker_count(server_cpus: int) -> int:
    """ceil(ncpu * 0.666), the same count Puma and the Elixir driver use."""
    if server_cpus < 1:
        raise ValueError("server cpu count must be positive")
    return (server_cpus * 666 + 999) // 1000


def cpu_count(spec: str) -> int:
    """Count CPUs in a taskset spec such as ``8-11`` or ``0,2,4-5``."""
    total = 0
    seen: set[int] = set()
    for part in spec.split(","):
        piece = part.strip()
        if not piece:
            raise ValueError(f"empty cpu spec in {spec!r}")
        if "-" in piece:
            start_text, end_text = piece.split("-", 1)
            start, end = int(start_text), int(end_text)
            if end < start:
                raise ValueError(f"cpu range {piece} is reversed")
            ids = range(start, end + 1)
        else:
            ids = (int(piece),)
        for cpu in ids:
            if cpu < 0 or cpu in seen:
                raise ValueError(f"cpu spec {spec!r} repeats or is negative")
            seen.add(cpu)
            total += 1
    if total == 0:
        raise ValueError("cpu spec is empty")
    return total


def default_cpu_sets(available: int) -> tuple[str, str]:
    """Server on 8-11 and clients on 12-15 when the machine has those CPUs.

    That is the placement recorded with the published tables. Smaller machines
    split the available CPUs in half, server first.
    """
    if available >= 16:
        return "8-11", "12-15"
    if available < 2:
        raise ValueError("need at least two CPUs")
    server = available // 2
    if server == 1:
        return "0", "1"
    return f"0-{server - 1}", f"{server}-{available - 1}"


def render_step(argv: tuple[str, ...], rev: str, reference_rev: str, image: str) -> tuple[str, ...]:
    values = {"rev": rev, "reference_rev": reference_rev or rev, "image": image}
    return tuple(part.format(**values) for part in argv)


def required_ports(app: App, http_port: int) -> tuple[int, ...]:
    ports = [http_port + offset for offset in app.port_offsets]
    if app.binds_redis:
        ports.append(6379)
    return tuple(ports)
