#!/usr/bin/env python3
"""Check the vendored public OpenAPI document against the deployed one.

Fetches the live public-only OpenAPI document (the dev deployment by
default) and compares it against the vendored copy at spec/openapi.json,
after dropping the `servers` block (which just names the environment that
served the document and is expected to differ). If `paths` or
`components.schemas` differ, this fails so a public-surface change on the
server is noticed here instead of silently drifting from the client.

Usage:
    check_openapi_drift.py            # compare, exit 1 on drift, 2 on fetch failure
    check_openapi_drift.py --update   # overwrite spec/openapi.json with the fetched doc

Environment:
    IOTA_HUB_OPENAPI_URL   overrides the URL to fetch (default: the dev deployment)
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://djf414kfl6u07.cloudfront.net/developers/openapi.json"
SPEC_PATH = pathlib.Path(__file__).resolve().parent.parent / "spec" / "openapi.json"
COMPARE_KEYS = ("paths", "components")


def fetch(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            body = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"could not fetch OpenAPI document from {url}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        print(
            f"could not parse OpenAPI document fetched from {url}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


def load_vendored(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        print(f"vendored spec not found at {path}", file=sys.stderr)
        raise SystemExit(2) from exc
    except json.JSONDecodeError as exc:
        print(f"could not parse vendored spec at {path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def normalize(doc: dict) -> dict:
    """Keep only the parts of the document that describe the actual API surface."""
    return {key: doc.get(key, {}) for key in COMPARE_KEYS}


def pretty(doc: dict) -> list[str]:
    return json.dumps(doc, sort_keys=True, indent=2).splitlines(keepends=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--update",
        action="store_true",
        help="overwrite spec/openapi.json with the fetched document (re-vendor)",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="URL to fetch the deployed OpenAPI document from "
        "(default: $IOTA_HUB_OPENAPI_URL or the dev deployment)",
    )
    args = parser.parse_args(argv)

    url = args.url or os.environ.get("IOTA_HUB_OPENAPI_URL", DEFAULT_URL)
    fetched = fetch(url)

    if args.update:
        SPEC_PATH.write_text(json.dumps(fetched, sort_keys=True, indent=2) + "\n")
        print(f"wrote {SPEC_PATH} from {url}")
        return 0

    vendored = load_vendored(SPEC_PATH)

    fetched_norm = normalize(fetched)
    vendored_norm = normalize(vendored)

    if fetched_norm == vendored_norm:
        print("spec/openapi.json matches the deployed public API document")
        return 0

    diff = difflib.unified_diff(
        pretty(vendored_norm),
        pretty(fetched_norm),
        fromfile="spec/openapi.json (vendored)",
        tofile=f"{url} (deployed)",
    )
    sys.stdout.writelines(diff)
    print(
        "\nspec/openapi.json differs from the deployed public API document"
        " — re-vendor it and review the client models",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
