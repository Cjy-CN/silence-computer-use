"""Per-cell GAC budgets selected by a public toolset facade."""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
from dataclasses import asdict, dataclass, fields
from typing import Any, Callable, Iterable, Iterator, Mapping

_PROFILE_NAME_MAX = 64


@dataclass(frozen=True)
class GacProfile:
    """Capture and model-rendering policy for one Browser Use backend."""

    name: str
    max_stdout_chars: int
    max_stderr_chars: int
    max_actions: int
    max_observations: int
    max_observation_chars: int
    max_images: int
    stdout_render_limit: int
    stderr_render_limit: int
    error_render_limit: int
    observation_render_limit: int
    progress_ops_max: int
    operation_label_limit: int

    def __post_init__(self) -> None:
        name = str(self.name or "")
        if not name or len(name) > _PROFILE_NAME_MAX:
            raise ValueError("GAC profile name must be 1 to 64 characters")
        # Budget values belong to the selected profile, not a second limit table.
        for field in fields(self):
            if field.name == "name":
                continue
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field.name} must be an integer")
            if value < 1:
                raise ValueError(f"{field.name} must be positive, got {value}")

    def to_envelope(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_envelope(cls, value: Any) -> GacProfile | None:
        if not isinstance(value, Mapping):
            return None
        expected = {field.name for field in fields(cls)}
        if set(value) != expected:
            return None
        try:
            return cls(**{name: value[name] for name in expected})
        except (TypeError, ValueError):
            return None


# Preserve the pre-expansion mainline runtime behavior for cells which do not
# call a Browser Use facade (for example seed_computer_use-only cells).
DEFAULT_GAC_PROFILE = GacProfile(
    name="shared-default",
    max_stdout_chars=200_000,
    max_stderr_chars=40_000,
    max_actions=400,
    max_observations=64,
    max_observation_chars=40_000,
    max_images=8,
    stdout_render_limit=4_000,
    stderr_render_limit=2_000,
    error_render_limit=4_800,
    observation_render_limit=3_200,
    progress_ops_max=5,
    operation_label_limit=24,
)


class GacProfileConflictError(RuntimeError):
    code = "BU_GAC_PROFILE_CONFLICT"


class GacProfileState:
    """Mutable selection owned by one cell; profiles themselves are immutable."""

    def __init__(self) -> None:
        self._selected: GacProfile | None = None

    @property
    def profile(self) -> GacProfile:
        return self._selected or DEFAULT_GAC_PROFILE

    def select(self, profile: GacProfile) -> GacProfile:
        if self._selected is None:
            self._selected = profile
        elif self._selected != profile:
            raise GacProfileConflictError(
                "one Python cell cannot mix Browser Use GAC profiles: "
                f"{self._selected.name!r} is already active, requested "
                f"{profile.name!r}"
            )
        return self._selected


_CURRENT_STATE: contextvars.ContextVar[GacProfileState | None] = (
    contextvars.ContextVar("gac_runtime_gac_profile_state", default=None)
)


@contextlib.contextmanager
def gac_profile_scope() -> Iterator[GacProfileState]:
    """Install a fresh profile selection for one runner cell."""

    state = GacProfileState()
    token = _CURRENT_STATE.set(state)
    try:
        yield state
    finally:
        _CURRENT_STATE.reset(token)


def select_gac_profile(profile: GacProfile) -> GacProfile:
    """Select ``profile`` for the active cell, or no-op outside a cell."""

    if not isinstance(profile, GacProfile):
        raise TypeError("profile must be a GacProfile")
    state = _CURRENT_STATE.get()
    return state.select(profile) if state is not None else profile


def bind_gac_profile(profile: GacProfile, function: Callable) -> Callable:
    """Bind a public facade function to a backend GAC profile."""

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        select_gac_profile(profile)
        return function(*args, **kwargs)

    wrapped.__gac_profile__ = profile.name
    return wrapped


def bind_gac_profile_exports(
    profile: GacProfile,
    namespace: dict[str, Any],
    names: Iterable[str],
) -> None:
    """Bind every public routine in a facade namespace with one mechanism."""

    for name in names:
        value = namespace.get(name)
        if inspect.isroutine(value):
            namespace[name] = bind_gac_profile(profile, value)


def gac_profile_from_envelope(envelope: Mapping[str, Any]) -> GacProfile:
    profile = GacProfile.from_envelope(envelope.get("gac_profile"))
    return profile or DEFAULT_GAC_PROFILE


__all__ = [
    "DEFAULT_GAC_PROFILE",
    "GacProfile",
    "GacProfileConflictError",
    "GacProfileState",
    "bind_gac_profile",
    "bind_gac_profile_exports",
    "gac_profile_from_envelope",
    "gac_profile_scope",
    "select_gac_profile",
]
