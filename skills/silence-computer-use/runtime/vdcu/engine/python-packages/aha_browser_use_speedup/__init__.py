"""The complete Aha Browser Use API with the speedup GAC budget profile."""

from gac_runtime.gac_profile import bind_gac_profile_exports

from aha_browser_use import __getattr__
# Import shared handlers, not the already profile-bound normal facade.
from aha_browser_use._api import *
from aha_browser_use._api import __all__
from aha_browser_use._profile import bind_snapshot_defaults

from ._profile import AHA_SPEEDUP_GAC_PROFILE, AHA_SPEEDUP_SNAPSHOT_DEFAULTS


snapshot = bind_snapshot_defaults(snapshot, AHA_SPEEDUP_SNAPSHOT_DEFAULTS)
bind_gac_profile_exports(AHA_SPEEDUP_GAC_PROFILE, globals(), __all__)
