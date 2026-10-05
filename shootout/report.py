"""Turn per-round samples into the comparison table."""

from __future__ import annotations

from collections import defaultdict

from shootout.catalog import ROUTE_TITLES


def median(values: list[float]) -> float:
    if not values:
        raise ValueError("median of an empty sample")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def format_rps(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{round(value):,}"
    return f"{value:,.1f}"


def format_ratio(part: float, whole: float) -> str:
    if whole <= 0:
        return ""
    return f"{part / whole:.2f}×"


def _samples(rows: list[dict], route: str, concurrency: int) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("error"):
            continue
        for sample in row.get("http") or []:
            if sample.get("route") == route and int(sample.get("conc", -1)) == concurrency and sample.get("ok_sample"):
                grouped[row["app"]].append(sample)
    return grouped


def summarize(rows: list[dict], apps: list[str], routes: list[str], concurrencies: list[int]) -> dict:
    """Median requests/sec per app, route and concurrency, plus body differences."""
    table = []
    for concurrency in concurrencies:
        for route in routes:
            grouped = _samples(rows, route, concurrency)
            cells = {}
            for app in apps:
                samples = grouped.get(app, [])
                rates = [float(sample["rps"]) for sample in samples]
                latencies = [
                    float(sample["latency"]["p50_ms"])
                    for sample in samples
                    if isinstance(sample.get("latency"), dict) and "p50_ms" in sample["latency"]
                ]
                cells[app] = {
                    "rounds": len(rates),
                    "rps": median(rates) if rates else None,
                    "rps_min": min(rates) if rates else None,
                    "rps_max": max(rates) if rates else None,
                    "p50_ms": median(latencies) if latencies else None,
                }
            table.append({"route": route, "concurrency": concurrency, "cells": cells})

    bodies = _bodies(rows, apps, routes)
    failures = [
        {"app": row["app"], "round": row.get("round"), "error": row["error"]}
        for row in rows
        if row.get("error")
    ]
    for row in rows:
        for route, body in (row.get("preflight") or {}).items():
            if body.get("error"):
                failures.append({
                    "app": row["app"],
                    "round": row.get("round"),
                    "error": f"{route}: {body['error']}",
                })
    return {"apps": apps, "table": table, "bodies": bodies, "failures": failures}


def _bodies(rows: list[dict], apps: list[str], routes: list[str]) -> list[dict]:
    first: dict[tuple[str, str], dict] = {}
    for row in rows:
        preflight = row.get("preflight") or {}
        for route, body in preflight.items():
            first.setdefault((row["app"], route), body)
    rails = {route: first.get(("rails", route)) for route in routes}
    differences = []
    for route in routes:
        baseline = rails.get(route)
        for app in apps:
            body = first.get((app, route))
            if not body:
                continue
            same_hash = baseline is not None and body.get("body_sha256") == baseline.get("body_sha256")
            same_window = baseline is not None and body.get("message_ids") == baseline.get("message_ids")
            if app == "rails":
                continue
            if baseline is None or not same_hash or not same_window or body.get("decoded_bytes") != baseline.get("decoded_bytes"):
                differences.append({
                    "route": route,
                    "app": app,
                    "decoded_bytes": body.get("decoded_bytes"),
                    "rails_decoded_bytes": None if baseline is None else baseline.get("decoded_bytes"),
                    "same_body": bool(same_hash),
                    "same_message_window": bool(same_window) if body.get("message_ids") is not None else None,
                    "encoding": body.get("encoding"),
                })
    return differences


def markdown(summary: dict, metadata: dict | None = None) -> str:
    apps: list[str] = summary["apps"]
    lines = ["# Campfire shootout", ""]
    if metadata:
        lines.extend(_metadata_lines(metadata))
        lines.append("")
    concurrencies = []
    for row in summary["table"]:
        if row["concurrency"] not in concurrencies:
            concurrencies.append(row["concurrency"])
    for concurrency in concurrencies:
        lines.append(f"## Requests/sec, {concurrency} clients")
        lines.append("")
        lines.append("| Workload | " + " | ".join(_title(app) for app in apps) + " |")
        lines.append("|---|" + "|".join("---:" for _ in apps) + "|")
        for row in summary["table"]:
            if row["concurrency"] != concurrency:
                continue
            cells = []
            baseline = row["cells"].get("rails", {}).get("rps")
            for app in apps:
                cell = row["cells"][app]
                if cell["rps"] is None:
                    cells.append("—")
                    continue
                text = format_rps(cell["rps"])
                if app != "rails" and baseline:
                    text += f" ({format_ratio(cell['rps'], baseline)})"
                if cell["rounds"] and (cell["rps_min"] != cell["rps_max"]):
                    text += f" [{format_rps(cell['rps_min'])}–{format_rps(cell['rps_max'])}]"
                cells.append(text)
            lines.append(f"| {ROUTE_TITLES.get(row['route'], row['route'])} | " + " | ".join(cells) + " |")
        lines.append("")

    lines.extend(_latency_section(summary, concurrencies))
    if summary["bodies"]:
        lines.append("## Response differences")
        lines.append("")
        lines.append("Same route and seed. A different body or message window means the throughput columns are not serving identical HTML.")
        lines.append("")
        lines.append("| Workload | App | Decoded bytes | Rails bytes | Same body | Same messages |")
        lines.append("|---|---|---:|---:|---|---|")
        for item in summary["bodies"]:
            window = "—" if item["same_message_window"] is None else ("yes" if item["same_message_window"] else "no")
            lines.append(
                f"| {ROUTE_TITLES.get(item['route'], item['route'])} | {_title(item['app'])} "
                f"| {item['decoded_bytes']} | {item['rails_decoded_bytes']} "
                f"| {'yes' if item['same_body'] else 'no'} | {window} |"
            )
        lines.append("")
    if summary["failures"]:
        lines.append("## Rounds that did not finish")
        lines.append("")
        for failure in summary["failures"]:
            lines.append(f"- {_title(failure['app'])} round {failure['round']}: {failure['error']}")
        lines.append("")
    lines.append(
        "Rates count keep-alive HTTP 200 responses only. "
        "The ratio is that app's median divided by Rails. "
        "A range appears when the rounds disagreed."
    )
    lines.append("")
    return "\n".join(lines)


def _latency_section(summary: dict, concurrencies: list[int]) -> list[str]:
    apps = summary["apps"]
    lines = ["## Median of per-round p50 latency (ms)", ""]
    for concurrency in concurrencies:
        lines.append(f"### {concurrency} clients")
        lines.append("")
        lines.append("| Workload | " + " | ".join(_title(app) for app in apps) + " |")
        lines.append("|---|" + "|".join("---:" for _ in apps) + "|")
        for row in summary["table"]:
            if row["concurrency"] != concurrency:
                continue
            cells = []
            for app in apps:
                value = row["cells"][app]["p50_ms"]
                cells.append("—" if value is None else f"{value:.2f}")
            lines.append(f"| {ROUTE_TITLES.get(row['route'], row['route'])} | " + " | ".join(cells) + " |")
        lines.append("")
    return lines


def _metadata_lines(metadata: dict) -> list[str]:
    lines = []
    if metadata.get("host"):
        lines.append(f"Host: {metadata['host']}")
    lines.append(
        f"Server CPUs `{metadata.get('server_cpus', '')}`, "
        f"client CPUs `{metadata.get('client_cpus', '')}`, "
        f"gzip {'on' if metadata.get('gzip', True) else 'off'}."
    )
    lines.append(
        f"Rounds: {metadata.get('rounds')}. "
        f"Sample: {metadata.get('duration')}s after a short warmup. "
        f"Order alternates each round."
    )
    if metadata.get("seed_sha256"):
        lines.append(f"Seed sha256: `{metadata['seed_sha256']}`")
    revisions = metadata.get("revisions") or {}
    if revisions:
        rendered = ", ".join(f"{name} `{rev[:12]}`" for name, rev in revisions.items())
        lines.append(f"Revisions: {rendered}.")
    lines.append(
        "Each app runs alone, from its production image, on a fresh copy of the Rust port's parity seed. "
        "Loopback measurements skip the NIC and TLS."
    )
    return lines


def _title(name: str) -> str:
    return {"rails": "Rails", "django": "Django", "laravel": "Laravel", "express": "Express",
            "elixir": "Elixir", "go": "Go", "rust": "Rust"}.get(name, name)
