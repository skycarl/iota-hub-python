# CLAUDE.md

Guidance for working in this repository.

## What this is

`iota-hub` on PyPI: a Python client library (`from iota_hub import Client`) and CLI
(`iota-hub`) for the IOTA Hub public API v1. The audience, in priority order: the CLI is
the first-choice interface (for observers and for their agents), the library second (for
observers scripting something bespoke), the raw HTTP API last (everyone else, or anyone
the first two don't fit). Optimize for that order — a feature that only the library can do
is a smell.

This repo does not own the API. It is a client of `skycarl/iota-hub`, built and versioned
independently.

## The contract

Two documents govern behavior here and must stay current with any change:

- **`spec/openapi.json`** — vendored from the deployed public API. It is the schema; the
  transport layer (below) is generated from it in shape, if not in code. `scripts/
  check_openapi_drift.py` compares it against the live document in CI and fails on drift;
  re-vendor with `--update` and review what changed before committing.
- **`docs/client-conventions.md`** — the language-neutral spec for everything the API
  itself doesn't define: config precedence, env var names, retry/polling policy, exit
  codes, the user-agent format. **Read it before changing CLI or client behavior, and
  update it in the same change.** It's what makes a future language client (`iota-hub-
  <lang>`) a port instead of a second design.

If this repo and a spec in `skycarl/iota-hub` disagree, the API repo's spec wins — file an
issue there (see below) rather than guessing.

## Two layers — keep them separate

1. **Transport** (`src/iota_hub/client.py` or similar): one method per public
   `operation_id`. Hand-written pydantic models with `extra="ignore"` so additive API
   changes never break an installed CLI. No workflow logic here.
2. **Workflow** (hand-written, not generated): `submit_folder`/`submit_files`,
   `wait_for_checks`, `download_files`, retry/idempotency handling, folder-to-slot file
   mapping, and problem-details-to-exception mapping. This is the part every language
   client has to reimplement, which is exactly why `docs/client-conventions.md` exists —
   write the behavior down there, not just in code.

Don't let workflow logic leak into the transport layer or vice versa.

## Layout

```
src/iota_hub/     library + CLI
tests/            pytest (unit/contract; smoke tests excluded by default)
tests/smoke/      live tests against dev — needs IOTA_HUB_API_KEY, IOTA_HUB_BASE_URL
spec/openapi.json vendored public API document
docs/             client-conventions.md and friends
```

## Commands

```bash
uv sync                          # install deps
uv run pytest -q                 # unit/contract tests (smoke excluded via -m "not smoke")
uv run ruff check .               # lint
uv run ruff format .              # format
uv build                          # sdist + wheel

# Smoke test (needs a live dev account key; not run by default, and never on fork PRs)
IOTA_HUB_API_KEY=... IOTA_HUB_BASE_URL=... uv run pytest -m smoke tests/smoke -q

# Re-vendor the OpenAPI document after a public API change
uv run python scripts/check_openapi_drift.py --update
```

## Rules

- **Additive-only.** The API only grows fields; models must ignore unknown ones
  (`extra="ignore"`). Don't add strict validation that would break on a future field.
- **Branch on `code`, never on `message`.** Error messages are for humans; `code` is the
  stable contract. Same for CLI exit codes — don't parse output to figure out what
  happened, use the documented codes.
- **A key is never a CLI argument.** It leaks into shell history and `ps`. Accept it via
  `IOTA_HUB_API_KEY`, stdin, or the stored profile — never a flag.
- **Never log or echo a key**, including in `--json` output or error details.
- **Windows is a first-class target**, not an afterthought — tests run on
  `windows-latest`. No POSIX-only path assumptions, no shell-specific syntax in anything
  users run, no decorative Unicode in default output (a non-UTF-8 Windows console has to
  survive it).

## Release process

Tag `vX.Y.Z` matching the version in `pyproject.toml` and push it; `release.yml` builds
and publishes to PyPI via Trusted Publishing (no stored token). The tag/version check
fails loudly on a mismatch rather than publishing the wrong thing.

**Nothing publishes before the production release of the IOTA Hub public API** (phase 7
of `features/public-api/design.md` in the main repo) — see the warning at the top of
`release.yml`.

Semver, `0.x` until then. A **major** bump is: removing a command or flag, or changing the
shape of a `--json` output. Anything additive (new command, new optional flag, new field
in a `--json` shape) is not.

## Coding philosophy

Same as the main repo: YAGNI, KISS, don't abstract on first duplication (wait for the Rule
of Three). This is a thin client — resist the pull to build a framework. When in doubt,
write the straightforward thing.

## Issues

File GitHub issues for anything out-of-scope you notice while working — don't fix it
inline and don't just mention it in a summary. Issues that touch the IOTA Hub API itself
(not just this client) go in `skycarl/iota-hub`, labeled `drafts-and-api`. Issues about
this repo's own code go here.
