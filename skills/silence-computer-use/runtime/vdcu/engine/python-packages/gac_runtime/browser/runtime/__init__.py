"""Low-level browser runtime assembled from focused helper modules."""

from . import transport as _transport
from .cdp import *
from .config import INTERNAL
from .config import MARK_TABS
from .http import *
from .input import *
from .keyboard import *
from .navigation import *
from .pages import *
from .tabs import *
from .transport import *


transport = _transport
_send = _transport.send
SOCK = _transport.SOCK
