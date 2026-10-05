# Campfire shootout

One harness for the Basecamp Campfire ports. It builds each production image, starts them one at a time on the same seed, and writes a table of the differences.

The ports live next to this directory, cloned from the Basecamp repositories:

| App | Checkout | Remote |
|---|---|---|
| Rails | `../once-campfire` | [basecamp/once-campfire](https://github.com/basecamp/once-campfire) |
| Django | `../once-campfire-django` | [basecamp/once-campfire-django](https://github.com/basecamp/once-campfire-django) |
| Laravel | `../once-campfire-laravel` | [basecamp/once-campfire-laravel](https://github.com/basecamp/once-campfire-laravel) |
| Express | `../once-campfire-express` | [basecamp/once-campfire-express](https://github.com/basecamp/once-campfire-express) |
| Elixir | `../once-campfire-elixir` | [basecamp/once-campfire-elixir](https://github.com/basecamp/once-campfire-elixir) |
| Go | `../once-campfire-go` | [basecamp/once-campfire-go](https://github.com/basecamp/once-campfire-go) |
| Rust | `../once-campfire-rust` | [basecamp/once-campfire-rust](https://github.com/basecamp/once-campfire-rust) |

DHH has no separate Campfire repositories. These Basecamp `main` branches are the authoritative trees. The local ASP.NET port in `../once-campfire-dotnet` is optional: `campfire-shootout build --apps dotnet` and `campfire-shootout bench --apps rails,dotnet` launch it with the same contract as Rails (Thruster on `HTTP_PORT`, the app on `TARGET_PORT`, mounts under `/rails/storage`). It is not in the default set. Its image opens `production.sqlite3` with the ASP.NET schema and leaves a Rails parity seed unchanged, so those rounds do not serve the seeded rooms.

## What a run measures

This follows the official production comparison, not the direct-to-Puma bench in the Rails tree:

- Each app runs from its own Dockerfile, alone, on CPUs `8-11` when the machine has at least 16 CPUs. The load generator uses `12-15`.
- The fixture is the Rust port's `parity` seed (`rooms.watercooler`, `messages.busy_060`, David signed in).
- The client is the Elixir port's `bench/loadgen`: keep-alive HTTP/1.1, gzip asked for by default, whole body read.
- Workloads are the README rows: room page, messages page, sidebar, search, and posting a message.
- Rounds are even and the app order flips each round. A cell is the median, with the ratio to Rails.
- The report also records decoded size, body hash, and message id window. Ports do not always emit the same HTML, and a faster column serving a different document is called out.

Loopback runs skip the NIC and TLS. Rails is current `basecamp/once-campfire`, which is a migration ahead of the Rails commit pinned inside some ports (an index on room and creation time). Numbers from this machine are not the published Ryzen AI MAX+ 395 table.

Elixir's documented build writes Hex, dependencies, and exported assets into that checkout, and it retags `campfire-reference:app`. `seed` retags that same name from the Rust checkout's Rails submodule. Build those whenever you like; the Rails column uses `campfire-rails:shootout` and does not depend on the tag they share.

## Commands

```sh
nix develop
campfire-shootout list
campfire-shootout plan
campfire-shootout seed          # Rust parity reference image, then parity/.seed/default
campfire-shootout loadgen       # compiles bench/loadgen with the Nix Rust toolchain
campfire-shootout build         # every production image; Rust, Go and Elixir take a long time
campfire-shootout check
campfire-shootout bench --apps rails,django,laravel,express,elixir,go,rust
campfire-shootout report tmp/results/<stamp>
```

`build --apps django,express` and `bench --apps django,express` limit the set. Useful overrides:

```sh
campfire-shootout bench --apps rails,rust --routes room_show,sidebar --concurrencies 1,16 --duration 4 --rounds 2
campfire-shootout bench --gzip 0          # Accept-Encoding: identity, still one mode for the whole run
campfire-shootout bench --load-max 1.5    # wait until the one-minute load average drops
```

Results land in `tmp/results/<utc-stamp>/` (`report.md`, `summary.json`, per-round JSON, container logs when a round fails). That directory is gitignored.

`check` is the readiness list: checkouts, seed, load generator, images, Docker, and free ports. Django with more than one worker also needs `redis:7.2-alpine`, which its build pulls. Laravel keeps the image's eight PHP-FPM workers and needs the HTTP port, that port plus 1000, and that port plus 2000. Rails and Elixir need host port 6379 for the Redis they start themselves.
