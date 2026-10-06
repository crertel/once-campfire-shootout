"""Command line for the Campfire shootout."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from shootout.catalog import (
    APPS,
    EXTRA_ROUTES,
    OPTIONAL_APPS,
    ROUTES,
    cpu_count,
    default_cpu_sets,
    parse_apps,
    render_step,
    required_ports,
    worker_count,
)
from shootout.discover import find_root, inspect, reference_env, seed_dir
from shootout.docker_shim import docker_is_rootless, install_rootless_shim
from shootout.report import markdown, summarize
from shootout.runner import (
    HarnessError,
    build_app,
    build_loadgen,
    build_seed,
    known_routes,
    launch_env,
    loadgen_binary,
    port_is_free,
    run_comparison,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="campfire-shootout",
        description="Build the Basecamp Campfire ports and compare them on one machine.",
    )
    parser.add_argument("--root", help="directory that contains the once-campfire* checkouts")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show each port, its revision, and its image")
    plan = sub.add_parser("plan", help="print the build and launch plan")
    plan.add_argument("--apps", default="all")

    build = sub.add_parser("build", help="docker-build the selected production images")
    build.add_argument("--apps", default="all")

    sub.add_parser("seed", help="build the shared Rust parity seed (also builds that repo's Rails reference image)")
    sub.add_parser("loadgen", help="compile the shared Rust load generator")

    check = sub.add_parser("check", help="report whether a bench can start")
    _add_bench_args(check)

    bench = sub.add_parser("bench", help="run the comparison and write a report")
    _add_bench_args(bench)

    report = sub.add_parser("report", help="render a report from a previous results directory")
    report.add_argument("directory")

    args = parser.parse_args(argv)
    try:
        if args.command in {"seed", "build", "check", "bench"}:
            install_rootless_shim()
        root = find_root(args.root)
        if args.command == "list":
            return _list(root)
        if args.command == "plan":
            return _plan(root, args.apps)
        if args.command == "build":
            return _build(root, args.apps)
        if args.command == "seed":
            print(build_seed(root))
            return 0
        if args.command == "loadgen":
            print(build_loadgen(root))
            return 0
        if args.command == "check":
            return _check(root, args)
        if args.command == "bench":
            return _bench(root, args)
        if args.command == "report":
            return _report(Path(args.directory))
    except (HarnessError, ValueError) as error:
        print(f"campfire-shootout: {error}", file=sys.stderr)
        return 2
    parser.error(f"unknown command {args.command}")
    return 2


def _add_bench_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--apps", default="all")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--duration", type=float, default=8)
    parser.add_argument("--concurrencies", default="16")
    parser.add_argument("--routes", default=",".join(ROUTES))
    parser.add_argument("--port", type=int, default=25130)
    parser.add_argument("--cpus", default="")
    parser.add_argument("--client-cpus", default="")
    parser.add_argument("--gzip", choices=("0", "1"), default="1")
    parser.add_argument("--load-max", type=float, default=0)
    parser.add_argument("--load-wait", type=int, default=120)
    parser.add_argument("--settle", type=float, default=10, help="seconds to wait after /up before measuring")
    parser.add_argument("--output", default="")


def _selected(root: Path, text: str):
    apps = parse_apps(text)
    return [inspect(root, app) for app in apps]


def _list(root: Path) -> int:
    print(f"workspace  {root}")
    seed = seed_dir(root)
    print(f"seed       {seed}  {'ready' if (seed / 'labels.json').is_file() else 'not built'}")
    binary = loadgen_binary(root)
    print(f"loadgen    {binary}  {'ready' if binary.is_file() else 'not built'}")
    print(f"env        {reference_env(root)}")
    print()
    for checkout in (inspect(root, app) for app in (*APPS, *OPTIONAL_APPS)):
        state = checkout.revision[:12] if checkout.present else "missing"
        dirty = " dirty" if checkout.dirty else ""
        reference = f"  reference {checkout.reference_revision[:12]}" if checkout.reference_revision else ""
        print(f"{checkout.app.name:<8} {state}{dirty}{reference}")
        print(f"         {checkout.path}")
        print(f"         image {checkout.app.image}")
        print(f"         {checkout.app.note}")
    return 0


def _plan(root: Path, apps: str) -> int:
    for checkout in _selected(root, apps):
        print(f"# {checkout.app.title}")
        if not checkout.present:
            print(f"# missing checkout {checkout.path}")
            continue
        print(f"# cwd {checkout.path}")
        for step in checkout.app.build:
            argv = render_step(step, checkout.revision or "HEAD", checkout.reference_revision, checkout.app.image)
            print(" ".join(argv))
        env = launch_env("DISABLE_SSL=true\n", checkout.app, 25130, "8-11")
        interesting = {key: env[key] for key in ("HTTP_PORT", "TARGET_PORT", "WEB_CONCURRENCY", "JOB_CONCURRENCY", "WEB_WORKERS", "REDIS_URL", "PORT") if key in env}
        print(f"# launch {interesting} ports={list(required_ports(checkout.app, 25130))} user={checkout.app.run_user or 'image'}")
        print()
    return 0


def _build(root: Path, apps: str) -> int:
    for checkout in _selected(root, apps):
        print(f"==> {checkout.app.title}", flush=True)
        build_app(checkout)
    return 0


def _check(root: Path, args: argparse.Namespace) -> int:
    problems = []
    checkouts = _selected(root, args.apps)
    for checkout in checkouts:
        if checkout.present:
            print(f"ok    checkout {checkout.app.name} {checkout.revision[:12]}")
        else:
            problems.append(f"missing checkout {checkout.path}")
    seed = seed_dir(root)
    if (seed / "db" / "production.sqlite3").is_file() and (seed / "labels.json").is_file():
        print(f"ok    seed {seed}")
    else:
        problems.append(f"seed not built at {seed}")
    if reference_env(root).is_file():
        print(f"ok    env {reference_env(root)}")
    else:
        problems.append(f"missing {reference_env(root)}")
    binary = loadgen_binary(root)
    if binary.is_file():
        print(f"ok    loadgen {binary}")
    else:
        problems.append("loadgen not built (campfire-shootout loadgen)")
    if shutil.which("taskset") is None:
        problems.append("taskset is not on PATH")
    else:
        print("ok    taskset")
    if shutil.which("docker") is None:
        problems.append("docker is not on PATH")
    else:
        info = subprocess.run(["docker", "info"], check=False, capture_output=True)
        if info.returncode != 0:
            problems.append("docker info failed")
        elif docker_is_rootless():
            print("ok    docker (rootless; containers run as uid 0)")
        else:
            print("ok    docker")
    server, clients = _cpus(args)
    try:
        width = cpu_count(server)
        print(f"ok    cpus server {server} ({width}, workers {worker_count(width)}) client {clients}")
    except ValueError as error:
        problems.append(str(error))
        width = 0
    for checkout in checkouts:
        if not checkout.present:
            continue
        inspected = subprocess.run(
            ["docker", "image", "inspect", checkout.app.image],
            check=False, capture_output=True,
        )
        if inspected.returncode == 0:
            print(f"ok    image {checkout.app.image}")
        else:
            problems.append(f"image not built: {checkout.app.image}")
        busy = [port for port in required_ports(checkout.app, args.port) if not port_is_free(port)]
        if busy:
            problems.append(f"{checkout.app.name} needs free ports {busy}")
    routes = _routes(args.routes)
    print(f"ok    routes {', '.join(routes)}")
    for problem in problems:
        print(f"miss  {problem}")
    return 1 if problems else 0


def _bench(root: Path, args: argparse.Namespace) -> int:
    server, clients = _cpus(args)
    routes = _routes(args.routes)
    concurrencies = [int(part) for part in args.concurrencies.split(",") if part.strip()]
    if not concurrencies or any(item < 1 for item in concurrencies):
        raise HarnessError("concurrencies must be positive integers")
    if args.duration <= 0:
        raise HarnessError("duration must be positive")
    output = Path(args.output) if args.output else _default_output(root)
    result = run_comparison(
        root,
        _selected(root, args.apps),
        output,
        rounds=args.rounds,
        duration=args.duration,
        concurrencies=concurrencies,
        routes=routes,
        http_port=args.port,
        server_cpus=server,
        client_cpus=clients,
        gzip_enabled=args.gzip == "1",
        load_max=args.load_max,
        load_wait=args.load_wait,
        settle=args.settle,
    )
    summary = summarize(result["rows"], [item.app.name for item in _selected(root, args.apps)], routes, concurrencies)
    text = markdown(summary, result["metadata"])
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (output / "report.md").write_text(text)
    print(text)
    print(f"wrote {output}")
    return 1 if summary["failures"] else 0


def _report(directory: Path) -> int:
    summary = json.loads((directory / "summary.json").read_text())
    metadata_path = directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else None
    sys.stdout.write(markdown(summary, metadata))
    return 0


def _routes(text: str) -> list[str]:
    names = [part.strip() for part in text.split(",") if part.strip()]
    allowed = set(known_routes())
    unknown = [name for name in names if name not in allowed]
    if unknown or not names:
        raise HarnessError(f"routes must be chosen from {', '.join([*ROUTES, *EXTRA_ROUTES])}")
    return names


def _cpus(args: argparse.Namespace) -> tuple[str, str]:
    if args.cpus or args.client_cpus:
        if not args.cpus or not args.client_cpus:
            raise HarnessError("pass both --cpus and --client-cpus")
        return args.cpus, args.client_cpus
    return default_cpu_sets(os.cpu_count() or 1)


def _default_output(root: Path) -> Path:
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return root / "once-campfire-shootout" / "tmp" / "results" / stamp
