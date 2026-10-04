"""Runtime budgets and API defaults owned by the Aha Browser Use facade."""

from dataclasses import dataclass, replace
from functools import wraps
from typing import Callable, Literal

from gac_runtime.gac_profile import DEFAULT_GAC_PROFILE

# DEFAULT_GAC_PROFILE
#    name="shared-default",
#    max_stdout_chars=200_000,
#    max_stderr_chars=40_000,
#    max_actions=400,
#    max_observations=64,
#    max_observation_chars=40_000,
#    max_images=8,
#    stdout_render_limit=4_000,
#    stderr_render_limit=2_000,
#    error_render_limit=4_800,
#    observation_render_limit=3_200,
#    progress_ops_max=5,
#    operation_label_limit=24,

AHA_GAC_PROFILE = replace(
    DEFAULT_GAC_PROFILE,
    name="aha_browser_use",
    # observation_render_limit=5_000,
)


@dataclass(frozen=True)
class SnapshotDefaults:
    within: Literal["viewport", "full"]
    view: Literal["outline", "interactive", "all"]
    max_chars: int
    max_chars_limit: int
    diff: bool | Literal["auto"]


AHA_SNAPSHOT_DEFAULTS = SnapshotDefaults(
    within="viewport",
    view="outline",
    max_chars=3200,
    max_chars_limit=4000,
    diff=False,
)


def bind_snapshot_defaults(function: Callable, defaults: SnapshotDefaults) -> Callable:
    """Resolve defaults and clamp the budget before the shared implementation runs."""

    @wraps(function)
    def snapshot(*, view=None, within=None, max_chars=None, diff=None):
        if max_chars is None:
            max_chars = defaults.max_chars
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            raise TypeError(
                f"max_chars must be an integer in [1000, {defaults.max_chars_limit}]"
            )
        max_chars = max(1000, min(max_chars, defaults.max_chars_limit))
        return function(
            view=defaults.view if view is None else view,
            within=defaults.within if within is None else within,
            max_chars=max_chars,
            diff=defaults.diff if diff is None else diff,
        )

    return snapshot


__all__ = [
    "AHA_GAC_PROFILE",
    "AHA_SNAPSHOT_DEFAULTS",
    "SnapshotDefaults",
    "bind_snapshot_defaults",
]
