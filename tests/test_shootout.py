import gzip
import http.server
import threading

import pytest

from shootout.catalog import (
    APPS,
    cpu_count,
    default_cpu_sets,
    parse_apps,
    required_ports,
    worker_count,
)
from shootout.cli import main
from shootout.discover import find_root
from shootout.report import format_ratio, markdown, median, summarize
from shootout.runner import _fetch, _preflight_error, launch_env, parse_env_file


def test_blessed_ports_match_the_readme_order():
    assert [app.name for app in APPS] == [
        "rails", "django", "laravel", "express", "elixir", "go", "rust",
    ]


def test_worker_count_matches_the_elixir_driver():
    assert worker_count(4) == 3
    assert worker_count(1) == 1
    assert worker_count(8) == 6


def test_default_cpus_use_the_published_sets_on_a_16_thread_machine():
    assert default_cpu_sets(16) == ("8-11", "12-15")
    assert default_cpu_sets(8) == ("0-3", "4-7")
    assert cpu_count("8-11") == 4
    assert cpu_count("0,2,4-5") == 4


def test_parse_apps_rejects_unknown_and_duplicate_names():
    assert [app.name for app in parse_apps("rust,go")] == ["rust", "go"]
    with pytest.raises(ValueError):
        parse_apps("dotnet")
    with pytest.raises(ValueError):
        parse_apps("rails,rails")


def test_launch_env_uses_three_workers_and_the_shared_secrets():
    text = "DISABLE_SSL=true\nSECRET_KEY_BASE=test\nWEB_CONCURRENCY=1\n# comment\n"
    rails = next(app for app in APPS if app.name == "rails")
    django = next(app for app in APPS if app.name == "django")
    elixir = next(app for app in APPS if app.name == "elixir")
    rails_env = launch_env(text, rails, 25130, "8-11")
    assert rails_env["WEB_CONCURRENCY"] == "3"
    assert rails_env["JOB_CONCURRENCY"] == "3"
    assert rails_env["DISABLE_SSL"] == "true"
    assert rails_env["TARGET_PORT"] == "25131"
    django_env = launch_env(text, django, 25130, "8-11")
    assert django_env["REDIS_URL"] == "redis://127.0.0.1:25132/0"
    assert django_env["WEB_WORKERS"] == "3"
    assert launch_env(text, elixir, 25130, "8-11")["PORT"] == "25130"
    assert parse_env_file(text)["SECRET_KEY_BASE"] == "test"


def test_required_ports_cover_redis_and_php():
    rails = next(app for app in APPS if app.name == "rails")
    laravel = next(app for app in APPS if app.name == "laravel")
    assert 6379 in required_ports(rails, 25130)
    assert required_ports(laravel, 25130) == (25130, 26130, 27130)


def test_median_and_report_show_the_ratio_to_rails():
    assert median([10, 30]) == 20
    assert median([10, 20, 30]) == 20
    rows = [
        _row("rails", 1, 200, "aaa", [1, 2]),
        _row("rails", 2, 240, "aaa", [1, 2]),
        _row("rust", 1, 36000, "bbb", [1, 2]),
        _row("rust", 2, 36520, "bbb", [1, 2]),
    ]
    summary = summarize(rows, ["rails", "rust"], ["room_show"], [16])
    text = markdown(summary, {"server_cpus": "8-11", "client_cpus": "12-15", "gzip": True,
                              "rounds": 2, "duration": 8, "revisions": {"rails": "abc", "rust": "def"}})
    assert "Room page" in text
    assert "220" in text
    assert "36,260" in text
    assert "(164.82×)" in text or "164.82×" in text
    assert summary["bodies"][0]["app"] == "rust"
    assert summary["bodies"][0]["same_body"] is False
    assert summary["bodies"][0]["same_message_window"] is True
    assert format_ratio(170, 241).startswith("0.71")


def test_preflight_failures_stay_out_of_the_throughput_table():
    rows = [{
        "app": "django",
        "round": 1,
        "error": None,
        "preflight": {"sidebar": {"error": "sidebar is missing the seeded room"}},
        "http": [],
    }]
    summary = summarize(rows, ["rails", "django"], ["sidebar"], [16])
    assert summary["table"][0]["cells"]["django"]["rps"] is None
    assert any("sidebar" in failure["error"] for failure in summary["failures"])


def test_preflight_accepts_a_populated_room_and_rejects_an_empty_one():
    assert _preflight_error("room_show", 200, 'data-message-id="4"', [4], "text/html", 1, 20) is None
    assert _preflight_error("room_show", 200, "hello", [], "text/html", 1, 5) == "response has no messages"
    assert _preflight_error("avatar", 200, "", None, "image/png", 1, 120) is None
    assert _preflight_error("up", 500, "no", None, "text/html", 1, 2) == "HTTP 500"


def test_fetch_reads_gzip_and_message_ids():
    body = b'<article data-message-id="9"></article>'
    wire = gzip.compress(body)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(wire)

        def log_message(self, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        fetched = _fetch(f"http://127.0.0.1:{port}", "/rooms/1", "session_token=x", True, 1, "room_show")
    finally:
        server.shutdown()
    assert fetched["error"] is None
    assert fetched["message_ids"] == [9]
    assert fetched["decoded_bytes"] == len(body)
    assert fetched["wire_bytes"] == len(wire)


def test_find_root_walks_up_to_the_checkouts(tmp_path):
    workspace = tmp_path / "campfire"
    nested = workspace / "once-campfire-shootout"
    (workspace / "once-campfire").mkdir(parents=True)
    (workspace / "once-campfire-rust").mkdir()
    nested.mkdir()
    assert find_root(None, nested) == workspace
    assert main(["--root", str(workspace), "list"]) == 0


def _row(app, round_number, rps, digest, ids):
    return {
        "app": app,
        "round": round_number,
        "error": None,
        "preflight": {
            "room_show": {
                "decoded_bytes": 100 if app == "rails" else 90,
                "body_sha256": digest,
                "message_ids": ids,
                "encoding": "gzip",
            },
        },
        "http": [{
            "route": "room_show",
            "conc": 16,
            "rps": rps,
            "ok_sample": True,
            "latency": {"p50_ms": 1.5},
        }],
    }
