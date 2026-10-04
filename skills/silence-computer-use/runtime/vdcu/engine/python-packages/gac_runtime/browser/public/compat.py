"""Compatibility wrappers for the original monolithic runtime API."""

import itertools
import json

from .. import runtime as _runtime
from ..services.actions import hover as _hover
from ..domain.core import _read_selector
from ..services.pages import navigate
from ..services.pages import page_info
from ..services.reads import find


def dialog(accept: bool, input: str = ""):
    return _runtime.cdp(
        "Page.handleJavaScriptDialog",
        accept=bool(accept),
        promptText=str(input),
    )


def go_back():
    return navigate("back")


def go_forward():
    return navigate("forward")


def find_iframe(pattern: str) -> str:
    return _runtime.iframe_target(pattern) or ""


def viewport() -> tuple[int, int]:
    info = page_info()
    return int(info.get("w") or 0), int(info.get("h") or 0)


def screen(*coords: int):
    return (
        value * size / 1000.0
        for value, size in zip(coords, itertools.cycle(viewport()))
    )


def locate(ref: str):
    selector = _read_selector(ref)
    raw = _runtime.js(
        "(()=>{const e=document.querySelector(%s);if(!e)return null;"
        "e.scrollIntoView({block:'center',inline:'center'});"
        "const b=e.getBoundingClientRect();"
        "const cx=b.left+b.width/2,cy=b.top+b.height/2;"
        "let occluded=false;"
        "try{const top=document.elementFromPoint(cx,cy);"
        "occluded=!!top&&top!==e&&!top.contains(e)&&!e.contains(top);}catch(_){}"
        "return JSON.stringify([b.left,b.top,b.width,b.height,occluded]);})()"
        % json.dumps(selector)
    )
    if not raw or raw == "null":
        return None
    x, y, width, height, occluded = json.loads(raw)
    return ref, x, y, width, height, occluded


def locate_all(query: str):
    out = []
    for ref in find(query):
        value = locate(ref)
        if value is not None:
            out.append(value)
    return out


def hover(ref: str):
    position = locate(ref)
    _hover(ref)
    if position is None:
        return None
    _, x, y, width, height, occluded = position
    return (
        round(x + width / 2),
        round(y + height / 2),
        x,
        y,
        width,
        height,
        occluded,
    )


__all__ = [
    "dialog",
    "find_iframe",
    "go_back",
    "go_forward",
    "hover",
    "locate",
    "locate_all",
    "screen",
    "viewport",
]
