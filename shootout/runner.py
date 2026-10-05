"""Build images, seed the shared fixture, and run the comparison."""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.parse
from pathlib import Path

from shootout.catalog import (
    LOADGEN_CHECKOUT,
    LOADGEN_RELATIVE,
    REDIS_IMAGE,
    ROUTES,
    App,
    required_ports,
    worker_count,
)
from shootout.discover import Checkout, reference_env, seed_dir

MESSAGE_ID = re.compile(r'data-message-id="(\d+)"')
REDIS_PORT_OFFSET = 2


class HarnessError(RuntimeError):
    pass


def parse_env_file(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def launch_env(base_text: str, app: App, http_port: int, server_cpus: str) -> dict[str, str]:
    values = parse_env_file(base_text)
    workers = str(worker_count(_spec_width(server_cpus)))
    for name in app.worker_env:
        values[name] = workers
    values["RAILS_MAX_THREADS"] = "5"
    values["RAILS_LOG_LEVEL"] = "warn"
    values["HTTP_PORT"] = str(http_port)
    values["TARGET_PORT"] = str(http_port + 1)
    if app.name == "elixir":
        values["PORT"] = str(http_port)
    if app.redis_sidecar:
        values["REDIS_URL"] = f"redis://127.0.0.1:{http_port + REDIS_PORT_OFFSET}/0"
    return values


def _spec_width(spec: str) -> int:
    from shootout.catalog import cpu_count
    return cpu_count(spec)


def port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def assert_ports_free(ports: list[int]) -> None:
    busy = [port for port in sorted(set(ports)) if not port_is_free(port)]
    if busy:
        raise HarnessError(f"ports already in use: {', '.join(map(str, busy))}")


def _shootout_tmp(root: Path) -> Path:
    return root / "once-campfire-shootout" / "tmp"


def loadgen_binary(root: Path) -> Path:
    return _shootout_tmp(root) / "loadgen" / "release" / "loadgen"


def build_loadgen(root: Path) -> Path:
    source = root.joinpath(LOADGEN_CHECKOUT, *LOADGEN_RELATIVE)
    if not (source / "Cargo.toml").is_file():
        raise HarnessError(f"load generator source is missing at {source}")
    target = _shootout_tmp(root) / "loadgen"
    target.mkdir(parents=True, exist_ok=True)
    _run([
        "cargo", "build", "--release", "--locked",
        "--manifest-path", str(source / "Cargo.toml"),
        "--target-dir", str(target),
    ], cwd=source)
    binary = target / "release" / "loadgen"
    if not binary.is_file():
        raise HarnessError(f"cargo did not produce {binary}")
    return binary


def build_app(checkout: Checkout) -> None:
    if not checkout.present:
        raise HarnessError(f"{checkout.app.checkout} is not cloned")
    from shootout.catalog import render_step
    for step in checkout.app.build:
        argv = render_step(step, checkout.revision, checkout.reference_revision, checkout.app.image)
        _run(list(argv), cwd=checkout.path)


def build_seed(root: Path) -> Path:
    rust = root / "once-campfire-rust"
    reference = rust / "parity" / "bin" / "reference"
    seed = rust / "parity" / "bin" / "seed"
    if not reference.is_file() or not seed.is_file():
        raise HarnessError(f"Rust parity scripts are missing under {rust}")
    _run([str(reference), "build"], cwd=rust)
    _run([str(seed), "build", "default"], cwd=rust)
    destination = seed_dir(root)
    if not (destination / "db" / "production.sqlite3").is_file():
        raise HarnessError(f"seed build did not write {destination}")
    return destination


def run_comparison(
    root: Path,
    checkouts: list[Checkout],
    output: Path,
    *,
    rounds: int,
    duration: float,
    concurrencies: list[int],
    routes: list[str],
    http_port: int,
    server_cpus: str,
    client_cpus: str,
    gzip_enabled: bool,
    load_max: float,
    load_wait: int,
    settle: float,
) -> dict:
    if rounds < 2 or rounds % 2:
        raise HarnessError("rounds must be even and at least 2 so each app runs in both orders")
    missing = [item.app.name for item in checkouts if not item.present]
    if missing:
        raise HarnessError(f"missing checkouts: {', '.join(missing)}")
    seed = seed_dir(root)
    database = seed / "db" / "production.sqlite3"
    if not database.is_file() or not (seed / "labels.json").is_file():
        raise HarnessError(
            f"parity seed is missing at {seed}. Run `campfire-shootout seed` first."
        )
    env_path = reference_env(root)
    if not env_path.is_file():
        raise HarnessError(f"missing shared environment file {env_path}")
    binary = loadgen_binary(root)
    if not binary.is_file():
        binary = build_loadgen(root)
    labels = json.loads((seed / "labels.json").read_text())
    _require_images(checkouts)
    ports = [port for item in checkouts for port in required_ports(item.app, http_port)]
    assert_ports_free(ports)

    output.mkdir(parents=True, exist_ok=True)
    metadata = _metadata(
        root, checkouts, database, rounds, duration, concurrencies, routes,
        http_port, server_cpus, client_cpus, gzip_enabled, binary,
    )
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    rows: list[dict] = []
    base_env = env_path.read_text()
    try:
        for iteration in range(rounds):
            order = checkouts if iteration % 2 == 0 else list(reversed(checkouts))
            for checkout in order:
                _wait_for_load(load_max, load_wait)
                row = _run_one(
                    root, checkout, seed, labels, base_env, binary, output,
                    round_number=iteration + 1,
                    base_url=f"http://127.0.0.1:{http_port}",
                    http_port=http_port,
                    duration=duration,
                    concurrencies=concurrencies,
                    routes=routes,
                    server_cpus=server_cpus,
                    client_cpus=client_cpus,
                    gzip_enabled=gzip_enabled,
                    settle=settle,
                )
                rows.append(row)
                _write_json(output / f"{checkout.app.name}-{iteration + 1}.json", row)
                print(
                    f"{checkout.app.name} round {iteration + 1}: "
                    + (row["error"] or _brief(row)),
                    flush=True,
                )
    finally:
        _remove_container(_container_name(http_port))
        _remove_container(_redis_name(http_port))
    return {"metadata": metadata, "rows": rows}


def _run_one(
    root: Path,
    checkout: Checkout,
    seed: Path,
    labels: dict,
    base_env: str,
    loadgen: Path,
    output: Path,
    *,
    round_number: int,
    base_url: str,
    http_port: int,
    duration: float,
    concurrencies: list[int],
    routes: list[str],
    server_cpus: str,
    client_cpus: str,
    gzip_enabled: bool,
    settle: float,
) -> dict:
    app = checkout.app
    runtime = output / "runtime" / f"{app.name}-{round_number}"
    row: dict = {"app": app.name, "round": round_number, "preflight": {}, "http": [], "error": None}
    container = _container_name(http_port)
    redis = _redis_name(http_port)
    try:
        _prepare_runtime(seed, runtime)
        env = launch_env(base_env, app, http_port, server_cpus)
        if app.redis_sidecar:
            _start_redis(redis, server_cpus, http_port + REDIS_PORT_OFFSET)
        _start_app(checkout, runtime, env, container, server_cpus)
        _wait_until_up(base_url, app.name, container)
        if settle:
            time.sleep(settle)
        cookie = _loadgen(loadgen, client_cpus, [
            "login", "--base", base_url,
            "--email", str(_label(labels, "emails.david")),
            "--password", str(_label(labels, "passwords.all")),
        ])["cookie"]
        room = int(_label(labels, "rooms.watercooler"))
        write_room = int(_label(labels, "rooms.hq"))
        before = _label(labels, "messages.busy_060")
        scrape = _loadgen(loadgen, client_cpus, [
            "scrape", "--base", base_url, "--cookie", cookie, "--room", str(room),
        ])
        paths = _route_paths(routes, labels, room, before, scrape)
        row["preflight"] = _preflight(base_url, cookie, paths, room, gzip_enabled)
        measurable = [
            name for name in routes
            if name == "post_message" or not row["preflight"].get(name, {}).get("error")
        ]
        if not measurable:
            failed = ", ".join(name for name, body in row["preflight"].items() if body.get("error"))
            raise HarnessError(f"every route failed preflight: {failed}")
        database = runtime / "db" / "production.sqlite3"
        before_count = (
            _message_count(database, write_room, app.name, container)
            if "post_message" in measurable else 0
        )
        acknowledged = 0
        for name in measurable:
            path = paths[name]
            args = ["--path", path] if path else ["--post-room", str(write_room), "--csrf", scrape.get("csrf") or ""]
            if not gzip_enabled:
                args.extend(["--gzip", "0"])
            warmup = _loadgen(loadgen, client_cpus, [
                "http", "--base", base_url, "--cookie", cookie, *args, "--conc", "4", "--duration", "2",
            ])
            _require_sample(name, warmup)
            acknowledged += int(warmup["ok"]) if path is None else 0
            for concurrency in concurrencies:
                sample = _loadgen(loadgen, client_cpus, [
                    "http", "--base", base_url, "--cookie", cookie, *args,
                    "--conc", str(concurrency), "--duration", str(duration),
                ])
                _require_sample(name, sample)
                acknowledged += int(sample["ok"]) if path is None else 0
                sample["route"] = name
                sample["ok_sample"] = True
                row["http"].append(sample)
                print(
                    f"  {app.name} {name} c={concurrency} {sample['rps']} req/s",
                    flush=True,
                )
        if "post_message" in routes:
            after = _message_count(database, write_room, app.name, container)
            if after - before_count < acknowledged:
                raise HarnessError(
                    f"persisted {after - before_count} messages after acknowledging {acknowledged}"
                )
            row["persisted_writes"] = after - before_count
    except Exception as error:
        row["error"] = str(error)
        log_path = output / f"{app.name}-{round_number}.log"
        log_path.write_text(_container_logs(container))
        row["log"] = str(log_path)
    finally:
        _remove_container(container)
        _remove_container(redis)
        if row["error"] is None:
            _remove_tree(runtime)
    return row


def _route_paths(routes: list[str], labels: dict, room: int, before: object, scrape: dict) -> dict[str, str | None]:
    avatar = _label(labels, "avatar_tokens.jason")
    catalog = {
        "room_show": f"/rooms/{room}",
        "messages_page": f"/rooms/{room}/messages?before={before}",
        "sidebar": "/users/me/sidebar",
        "search": "/searches?q=coffee",
        "avatar": f"/users/{avatar}/avatar",
        "static_css": scrape.get("css") or "",
        "up": "/up",
        "post_message": None,
    }
    selected = {}
    for name in routes:
        if name not in catalog:
            raise HarnessError(f"unknown route {name}")
        selected[name] = catalog[name]
    return selected


def _preflight(base: str, cookie: str, paths: dict[str, str | None], room: int, gzip_enabled: bool) -> dict:
    results = {}
    for name, path in paths.items():
        if path is None:
            continue
        results[name] = _fetch(base, path, cookie, gzip_enabled, room, name)
    return results


def _fetch(base: str, path: str, cookie: str, gzip_enabled: bool, room: int, name: str) -> dict:
    encoding = "gzip" if gzip_enabled else "identity"
    if not path.startswith("/"):
        return {"error": f"route is not a path: {path}", "status": 0}
    try:
        status, wire, content_encoding, content_type = _http_get(base, path, cookie, encoding)
    except OSError as error:
        return {"error": str(error), "status": 0}
    body = wire
    if "gzip" in content_encoding:
        try:
            body = gzip.decompress(wire)
        except OSError as error:
            return {"error": f"gzip decode failed: {error}", "status": status}
    text = body.decode("utf-8", "replace")
    ids = [int(value) for value in MESSAGE_ID.findall(text)] if name in {"room_show", "messages_page", "search"} else None
    error = _preflight_error(name, status, text, ids, content_type, room, len(body))
    return {
        "status": status,
        "error": error,
        "decoded_bytes": len(body),
        "wire_bytes": len(wire),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "encoding": content_encoding or encoding,
        "content_type": content_type,
        "message_ids": ids,
    }


def _preflight_error(
    name: str, status: int, text: str, ids: list[int] | None,
    content_type: str, room: int, size: int,
) -> str | None:
    if status != 200:
        return f"HTTP {status}"
    if size == 0:
        return "empty body"
    if name in {"room_show", "messages_page", "search"} and not ids:
        return "response has no messages"
    if name == "sidebar" and ("shared_rooms" not in text or str(room) not in text):
        return "sidebar is missing the seeded room"
    if name == "avatar" and (not content_type.startswith("image/") or size < 100):
        return "response is not an image"
    if name == "static_css" and (not content_type.startswith("text/css") or "{" not in text):
        return "response is not a stylesheet"
    if name == "up" and "background-color: green" not in text and text.strip() not in {"ok", "OK"}:
        return "unexpected health body"
    return None


def _http_get(base: str, path: str, cookie: str, encoding: str) -> tuple[int, bytes, str, str]:
    parsed = urllib.parse.urlparse(base)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=60)
    try:
        connection.request("GET", path, headers={
            "Cookie": cookie,
            "Accept-Encoding": encoding,
            "Host": parsed.netloc,
        })
        response = connection.getresponse()
        wire = response.read()
        return (
            response.status,
            wire,
            response.getheader("Content-Encoding") or "",
            response.getheader("Content-Type") or "",
        )
    finally:
        connection.close()


def _prepare_runtime(seed: Path, runtime: Path) -> None:
    if runtime.exists():
        _remove_tree(runtime)
    runtime.mkdir(parents=True)
    _copy_tree(seed / "db", runtime / "db")
    _copy_tree(seed / "storage", runtime / "files")
    (runtime / "logs").mkdir()
    database = runtime / "db" / "production.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE push_subscriptions SET endpoint = 'https://127.0.0.1:9/push/' || id")
        connection.execute("UPDATE webhooks SET url = 'http://127.0.0.1:9/hook/' || id")


def _copy_tree(source: Path, destination: Path) -> None:
    subprocess.run(
        ["cp", "-a", "--reflink=auto", str(source), str(destination)],
        check=True,
    )


def _start_redis(name: str, cpus: str, port: int) -> None:
    _remove_container(name)
    _run([
        "docker", "run", "-d", "--name", name, "--network", "host",
        "--cpuset-cpus", cpus, REDIS_IMAGE,
        "redis-server", "--bind", "127.0.0.1", "--port", str(port),
        "--save", "", "--appendonly", "no",
    ])


def _start_app(checkout: Checkout, runtime: Path, env: dict[str, str], name: str, cpus: str) -> None:
    _remove_container(name)
    command = [
        "docker", "run", "-d", "--name", name, "--network", "host",
        "--cpuset-cpus", cpus,
    ]
    if checkout.app.run_user == "host":
        command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    for key, value in env.items():
        command.extend(["-e", f"{key}={value}"])
    mounts = {
        runtime / "db": "/rails/storage/db",
        runtime / "files": "/rails/storage/files",
        runtime / "logs": "/rails/storage/logs",
    }
    for host, target in mounts.items():
        command.extend(["-v", f"{host}:{target}"])
    command.append(checkout.app.image)
    _run(command)


def _wait_until_up(base: str, app: str, container: str, timeout: float = 120) -> None:
    del container
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        try:
            status, _wire, _encoding, _kind = _http_get(base, "/up", "", "identity")
            if status == 200:
                return
            last = f"HTTP {status}"
        except OSError as error:
            last = str(error)
        time.sleep(0.2)
    raise HarnessError(f"{app} did not become ready ({last})")


def _loadgen(binary: Path, cpus: str, args: list[str]) -> dict:
    command = ["taskset", "-c", cpus, str(binary), *args]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise HarnessError(f"loadgen {' '.join(args[:1])} failed: {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise HarnessError(f"loadgen returned non-JSON: {completed.stdout[:400]}") from error


def _require_sample(name: str, sample: dict) -> None:
    statuses = sample.get("statuses") or {}
    if sample.get("errors") or sample.get("invalid_responses") or list(statuses) != ["200"]:
        raise HarnessError(f"{name}: unsuccessful sample {sample.get('statuses')} errors={sample.get('errors')}")


def _message_count(database: Path, room: int, app: str, container: str) -> int:
    query = f"SELECT COUNT(*) AS n FROM messages WHERE room_id={int(room)}"
    try:
        return _sqlite(database, query)
    except sqlite3.OperationalError:
        if app != "laravel":
            raise
        completed = subprocess.run(
            [
                "docker", "exec", "--user", "www-data", container, "php", "-r",
                '$pdo = new PDO("sqlite:/rails/storage/db/production.sqlite3");'
                '$pdo->exec("PRAGMA busy_timeout = 10000");'
                'echo $pdo->query($argv[1])->fetchColumn();',
                query,
            ],
            check=False, capture_output=True, text=True,
        )
        if completed.returncode != 0:
            raise HarnessError(completed.stderr.strip() or "could not read the Laravel database")
        return int(completed.stdout.strip())


def _sqlite(database: Path, query: str) -> int:
    uri = f"file:{database}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as connection:
        row = connection.execute(query).fetchone()
    return int(row[0])


def _label(labels: dict, name: str) -> object:
    if name not in labels:
        raise HarnessError(f"labels.json is missing {name}")
    return labels[name]


def _require_images(checkouts: list[Checkout]) -> None:
    missing = []
    for checkout in checkouts:
        completed = subprocess.run(
            ["docker", "image", "inspect", "-f", "{{.Id}}", checkout.app.image],
            check=False, capture_output=True, text=True,
        )
        if completed.returncode != 0:
            missing.append(f"{checkout.app.name} ({checkout.app.image})")
    if missing:
        raise HarnessError("images are not built: " + ", ".join(missing) + ". Run `campfire-shootout build` first.")


def _metadata(root, checkouts, database, rounds, duration, concurrencies, routes, http_port, server_cpus, client_cpus, gzip_enabled, binary) -> dict:
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    revisions = {}
    images = {}
    for checkout in checkouts:
        revisions[checkout.app.name] = checkout.revision
        completed = subprocess.run(
            ["docker", "image", "inspect", "-f", "{{.Id}}", checkout.app.image],
            check=False, capture_output=True, text=True,
        )
        images[checkout.app.name] = completed.stdout.strip()
    return {
        "host": _host(),
        "rounds": rounds,
        "duration": duration,
        "concurrencies": concurrencies,
        "routes": routes,
        "http_port": http_port,
        "server_cpus": server_cpus,
        "client_cpus": client_cpus,
        "gzip": gzip_enabled,
        "seed_sha256": digest,
        "loadgen_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "revisions": revisions,
        "images": images,
        "root": str(root),
    }


def _host() -> str:
    cpu = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return f"{cpu} ({os.cpu_count()} CPUs)".strip()


def _container_name(port: int) -> str:
    return f"campfire-shootout-{port}"


def _redis_name(port: int) -> str:
    return f"campfire-shootout-redis-{port}"


def _remove_container(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


def _container_logs(name: str) -> str:
    completed = subprocess.run(["docker", "logs", "--tail", "80", name], check=False, capture_output=True, text=True)
    return completed.stdout + completed.stderr


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        return
    except OSError:
        pass
    subprocess.run(
        ["docker", "run", "--rm", "--user", "0", "--entrypoint", "rm",
         "-v", f"{path}:/wipe", REDIS_IMAGE, "-rf", "/wipe"],
        check=False, capture_output=True,
    )


def _wait_for_load(limit: float, timeout: int) -> None:
    if limit <= 0:
        return
    waited = 0
    while waited < timeout:
        try:
            current = float(Path("/proc/loadavg").read_text().split()[0])
        except (OSError, ValueError):
            return
        if current < limit:
            return
        time.sleep(10)
        waited += 10


def _brief(row: dict) -> str:
    parts = []
    for sample in row["http"]:
        parts.append(f"{sample['route']}@{sample['conc']}={sample['rps']}")
    return ", ".join(parts) or "ok"


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def _run(argv: list[str], cwd: Path | None = None) -> None:
    print("+ " + _shown(argv), flush=True)
    completed = subprocess.run(argv, cwd=cwd)
    if completed.returncode != 0:
        raise HarnessError(f"command failed ({completed.returncode}): {' '.join(argv[:6])}")


def known_routes() -> tuple[str, ...]:
    return (*ROUTES, "avatar", "static_css", "up")


def _shown(argv: list[str]) -> str:
    shown = []
    for part in argv:
        key, separator, _value = part.partition("=")
        if separator and key.startswith(("SECRET", "VAPID")):
            shown.append(f"{key}=<set>")
        else:
            shown.append(part)
    return " ".join(shown)
