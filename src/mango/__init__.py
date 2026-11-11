"""mango: declare reproducible learning environments in Nix, build them
offline, and run them as Harbor tasks."""

from pathlib import Path

#: The harness checkout: Nix library, builders and this package.
HARNESS = Path(__file__).resolve().parents[2]
