"""The installed package version, for the User-Agent and ``--version``."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("iota-hub")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0.dev0"
