"""Speedup runtime budgets and defaults for the shared Aha implementation."""

from dataclasses import replace

from aha_browser_use._profile import AHA_GAC_PROFILE, AHA_SNAPSHOT_DEFAULTS


AHA_SPEEDUP_GAC_PROFILE = replace(
    AHA_GAC_PROFILE,
    name="aha_browser_use_speedup",
    stdout_render_limit=140_000,
)


AHA_SPEEDUP_SNAPSHOT_DEFAULTS = replace(
    AHA_SNAPSHOT_DEFAULTS,
    within="full",
    view="all",
    max_chars=140_000,
    max_chars_limit=140_000,
    diff="auto",
)


__all__ = ["AHA_SPEEDUP_GAC_PROFILE", "AHA_SPEEDUP_SNAPSHOT_DEFAULTS"]
