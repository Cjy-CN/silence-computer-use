"""GAC profile for the Legacy Browser Use facade."""

from dataclasses import replace

from gac_runtime.gac_profile import DEFAULT_GAC_PROFILE


SEED_GAC_PROFILE = replace(
    DEFAULT_GAC_PROFILE,
    name="seed_browser_use",
)


__all__ = ["SEED_GAC_PROFILE"]
