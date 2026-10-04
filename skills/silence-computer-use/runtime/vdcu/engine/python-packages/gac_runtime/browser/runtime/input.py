"""Mouse, text-input, screenshot, and file-upload primitives."""

import base64
import json
import os

from pathlib import Path

from .. import backend as transport
from . import transport as runtime_transport
from .cdp import js
from .transport import cdp


_debug_click_counter = 0


def click_at_xy(x, y, button="left", clicks=1):
    if os.environ.get("CUA_GAC_DEBUG_CLICKS"):
        global _debug_click_counter
        try:
            from PIL import Image, ImageDraw
            dpr = js("window.devicePixelRatio") or 1
            path = capture_screenshot(str(transport.TMP_DIR / f"debug_click_{_debug_click_counter}.png"))
            img = Image.open(path)
            draw = ImageDraw.Draw(img)
            px, py = int(x * dpr), int(y * dpr)
            r = int(15 * dpr)
            draw.ellipse([px - r, py - r, px + r, py + r], outline="red", width=int(3 * dpr))
            draw.line([px - r - int(5 * dpr), py, px + r + int(5 * dpr), py], fill="red", width=int(2 * dpr))
            draw.line([px, py - r - int(5 * dpr), px, py + r + int(5 * dpr)], fill="red", width=int(2 * dpr))
            img.save(path)
            print(f"[debug_click] saved {path} (x={x}, y={y}, dpr={dpr})")
        except Exception as e:
            print(f"[debug_click] overlay failed: {e}")
        _debug_click_counter += 1
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button=button, clickCount=clicks)
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button=button, clickCount=clicks)

def type_text(text):
    cdp("Input.insertText", text=text)

def _fill_input_expression(selector, text, clear_first):
    return (
        "(()=>{"
        f"const selector={json.dumps(selector)};"
        f"const text={json.dumps(text)};"
        f"const clearFirst={json.dumps(bool(clear_first))};"
        "const e=document.querySelector(selector);"
        "if(!e)return {ok:false,reason:'not_found'};"
        "const dispatch=()=>{"
        "try{e.dispatchEvent(new Event('input',{bubbles:true}));"
        "e.dispatchEvent(new Event('change',{bubbles:true}));}catch(err){}"
        "};"
        "try{e.focus({preventScroll:true});}catch(err){try{e.focus();}catch(err2){}}"
        "if(e instanceof HTMLInputElement||e instanceof HTMLTextAreaElement){"
        "const proto=(e instanceof HTMLTextAreaElement)?HTMLTextAreaElement.prototype:"
        "HTMLInputElement.prototype;"
        "const desc=Object.getOwnPropertyDescriptor(proto,'value');"
        "const next=clearFirst?text:String(e.value||'')+text;"
        "try{if(desc&&desc.set){desc.set.call(e,next);}else{e.value=next;}}"
        "catch(err){return {ok:false,reason:'setter_failed',message:String(err&&err.message||err)}}"
        "dispatch();"
        "return {ok:true,kind:'input'};"
        "}"
        "const ceAttrRaw=e.getAttribute('contenteditable');"
        "const ceAttr=ceAttrRaw===null?null:String(ceAttrRaw).toLowerCase();"
        "const editable=!!(e.isContentEditable||ceAttr===''||ceAttr==='true'||"
        "ceAttr==='plaintext-only'||"
        "e.getAttribute('role')==='textbox');"
        "if(!editable){"
        "return {ok:false,reason:'unsupported',tag:String(e.tagName||''),"
        "type:String(e.type||''),role:String(e.getAttribute('role')||''),"
        "contentEditable:String(e.getAttribute('contenteditable')||'')};"
        "}"
        "const sel=window.getSelection&&window.getSelection();"
        "let range=null;"
        "if(sel){"
        "range=document.createRange();"
        "range.selectNodeContents(e);"
        "if(!clearFirst)range.collapse(false);"
        "sel.removeAllRanges();"
        "sel.addRange(range);"
        "}"
        "let inserted=false;"
        "try{inserted=!!(document.execCommand&&document.execCommand('insertText',false,text));}"
        "catch(err){}"
        "if(!inserted){"
        "try{"
        "if(clearFirst)e.textContent='';"
        "const node=document.createTextNode(text);"
        "if(!range){range=document.createRange();range.selectNodeContents(e);range.collapse(false);}"
        "range.deleteContents();"
        "range.insertNode(node);"
        "range.setStartAfter(node);range.setEndAfter(node);"
        "if(sel){sel.removeAllRanges();sel.addRange(range);}"
        "}catch(err){return {ok:false,reason:'contenteditable_failed',"
        "message:String(err&&err.message||err)}}"
        "}"
        "dispatch();"
        "return {ok:true,kind:'contenteditable'};"
        "})()"
    )

def fill_input(selector, text, clear_first=True, timeout=0.0):
    """Fill a framework-managed input (React controlled, Vue v-model, Ember tracked).

    CDP Input.* depends on the browser-side focused WebContents and can leak into the
    embedder when a BrowserUse page is shown in a <webview>. This helper writes inside
    the target page's JS context, then fires input+change so frameworks see the update.

    Raises RuntimeError if the element is not found. Pass timeout>0 to wait for
    late-rendered elements (e.g. after a route change) before typing.
    """
    if text is None:
        raise TypeError("fill_input: text must not be None")
    value = str(text)
    if timeout > 0:
        from .navigation import wait_for_element

        if not wait_for_element(selector, timeout=timeout):
            raise RuntimeError(f"fill_input: element not found: {selector!r}")
    result = js(_fill_input_expression(selector, value, clear_first))
    if not isinstance(result, dict):
        raise RuntimeError(f"fill_input: unexpected result for {selector!r}")
    if result.get("ok"):
        return
    reason = result.get("reason")
    if reason == "not_found":
        raise RuntimeError(f"fill_input: element not found: {selector!r}")
    if reason == "unsupported":
        tag = result.get("tag") or "unknown"
        role = result.get("role") or ""
        raise RuntimeError(
            f"fill_input: unsupported element type for {selector!r}: "
            f"tag={tag!r}, role={role!r}"
        )
    message = result.get("message") or reason or "unknown error"
    raise RuntimeError(f"fill_input: failed for {selector!r}: {message}")

def scroll(x, y, dy=-300, dx=0):
    cdp("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=dx, deltaY=dy)


# --- visual ---
def capture_screenshot(path=None, full=False, max_dim=None, clip=None):
    """Save a PNG of the viewport or one page-coordinate clip."""
    path = path or str(transport.TMP_DIR / "shot.png")
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    params = {"format": "png", "captureBeyondViewport": bool(full or clip)}
    if clip:
        params["clip"] = {**dict(clip), "scale": float(clip.get("scale", 1))}
    max_dimension = 0 if max_dim is None else int(max_dim)
    if max_dimension < 0:
        raise ValueError("capture_screenshot: max_dim must be non-negative")
    r = runtime_transport._capture_screenshot(
        params,
        max_dimension=max_dimension,
    )
    Path(path).write_bytes(base64.b64decode(r["data"]))
    return path


_KC = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, " ": 32, "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40}


def dispatch_key(selector, key="Enter", event="keypress"):
    """Dispatch a DOM KeyboardEvent on the matched element.

    Use this when a site reacts to synthetic DOM key events on an element more reliably
    than to raw CDP input events.
    """
    kc = _KC.get(key, ord(key) if len(key) == 1 else 0)
    js(
        f"(()=>{{const e=document.querySelector({json.dumps(selector)});if(e){{e.focus();e.dispatchEvent(new KeyboardEvent({json.dumps(event)},{{key:{json.dumps(key)},code:{json.dumps(key)},keyCode:{kc},which:{kc},bubbles:true}}));}}}})()"
    )

def upload_file(selector, path):
    """Set files on a file input via CDP DOM.setFileInputFiles. `path` is an absolute filepath (use tempfile.mkstemp if needed)."""
    doc = cdp("DOM.getDocument", depth=-1)
    nid = cdp("DOM.querySelector", nodeId=doc["root"]["nodeId"], selector=selector)["nodeId"]
    if not nid: raise RuntimeError(f"no element for {selector}")
    cdp("DOM.setFileInputFiles", files=[path] if isinstance(path, str) else list(path), nodeId=nid)
