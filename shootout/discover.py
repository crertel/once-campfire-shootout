"""Find the sibling Basecamp checkouts and read their revisions."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from shootout.catalog import SEED_CHECKOUT, SEED_RELATIVE, App


@dataclass(frozen=True)
class Checkout:
    app: App
    path: Path
    present: bool
    revision: str
    dirty: bool
    reference_revision: str


def find_root(explicit: str | None, cwd: Path | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("CAMPFIRE_ROOT")
    if env:
        return Path(env).resolve()
    start = (cwd or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / "once-campfire").is_dir() and (candidate / SEED_CHECKOUT).is_dir():
            return candidate
    raise SystemExit(
        "could not find the Campfire checkouts. Run this from the workspace "
        "that contains them, or pass --root."
    )


def git_output(path: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def inspect(root: Path, app: App) -> Checkout:
    path = root / app.checkout
    present = (path / ".git").exists()
    revision = git_output(path, "rev-parse", "HEAD") or "" if present else ""
    dirty = bool(git_output(path, "status", "--porcelain")) if present else False
    reference = path / "reference"
    reference_revision = ""
    # Several ports pin Rails in a submodule named reference. Go's submodule
    # of that name is the Rust port, so require the Rails entry points.
    if (reference / "Gemfile").is_file() and (reference / "config.ru").is_file():
        reference_revision = git_output(reference, "rev-parse", "HEAD") or ""
    return Checkout(app, path, present, revision, dirty, reference_revision)


def seed_dir(root: Path) -> Path:
    return root.joinpath(SEED_CHECKOUT, *SEED_RELATIVE)


def reference_env(root: Path) -> Path:
    return root / "once-campfire-elixir" / "parity" / "reference.env"
