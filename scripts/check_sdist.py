#!/usr/bin/env python3
"""Verify an sdist tarball only contains files we intend to publish.

Usage: check_sdist.py DIST.tar.gz [DIST2.tar.gz ...]

Every member path, after stripping the leading `<name>-<version>/` directory
that sdists are built with, must either live under `src/iota_hub/` or be one
of a small set of root-level files (README.md, LICENSE, pyproject.toml,
PKG-INFO). Anything else (tests, fixtures, .github, dev config, ...) means
the build picked up something it shouldn't have, so this fails loudly rather
than letting an oversized or leaky sdist go out to PyPI.
"""

from __future__ import annotations

import argparse
import sys
import tarfile

# hatchling force-includes the VCS ignore file in every sdist; it is harmless.
ALLOWED_ROOT_FILES = {
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "PKG-INFO",
    ".gitignore",
}
ALLOWED_PREFIX = "src/iota_hub/"


def offending_members(tar_path: str) -> list[str]:
    offenders: list[str] = []
    with tarfile.open(tar_path, "r:*") as tar:
        for member in tar.getmembers():
            if member.isdir():
                continue
            parts = member.name.split("/", 1)
            if len(parts) != 2:
                # A bare top-level entry (e.g. just the directory itself).
                continue
            rest = parts[1]
            if rest == "":
                continue
            if rest in ALLOWED_ROOT_FILES:
                continue
            if rest.startswith(ALLOWED_PREFIX):
                continue
            offenders.append(member.name)
    return offenders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("dist", nargs="+", help="Path(s) to a built sdist (.tar.gz)")
    args = parser.parse_args(argv)

    exit_code = 0
    for path in args.dist:
        offenders = offending_members(path)
        if offenders:
            exit_code = 1
            print(
                f"{path}: sdist contains files outside the allowed set:",
                file=sys.stderr,
            )
            for offender in offenders:
                print(f"  {offender}", file=sys.stderr)
        else:
            print(f"{path}: OK")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
