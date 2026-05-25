# Single source of truth for the app version.
# GitHub Actions injects the real tag (e.g. "1.2.0") before building.
# Local / dev builds stay as "dev" — no spurious update prompts.
__version__ = "dev"
