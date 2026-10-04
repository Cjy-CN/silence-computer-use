"""Ref, coordinate, mouse, typing, and selection actions."""

from ..domain.core import *


# --------------------------------------------------------------------------- #
# ref actions (the default path) — act on a snapshot ref                        #
# --------------------------------------------------------------------------- #
def _box_center_css(ref):
    # Scroll the target into view FIRST, then read its box. A ref whose element is
    # scrolled out of the viewport has a center at negative / off-screen coords, so the
    # CDP mouse event lands on nothing (or whatever is at that pixel) — a silent mis-click
    # that feeds the "click didn't work" retry loop. scrollIntoView({block:'center'}) is a
    # standard DOM call; we read the rect AFTER it so coords are viewport-correct.
    # We also report occlusion: if document.elementFromPoint(center) is not the target
    # (nor a descendant), a sticky header / modal / overlay covers it — the caller can
    # surface that instead of clicking the wrong element. Generic, no site rules.
    r = _bh.js(
        f"(()=>{{const e=document.querySelector({_json.dumps(_read_selector(ref))});if(!e)return null;"
        "e.scrollIntoView({block:'center',inline:'center'});"
        "const b=e.getBoundingClientRect();"
        "const cx=b.left+b.width/2, cy=b.top+b.height/2;"
        "let occluded=false;"
        "try{const top=document.elementFromPoint(cx,cy);"
        "occluded=!!top && top!==e && !e.contains(top) && !top.contains(e);}catch(_){}"
        "return JSON.stringify([cx, cy, occluded]);})()"
    )
    if not r or r == "null":
        # Stale/gone ref. Do NOT raise — a raw RuntimeError aborts the whole GUI-code
        # cell and pushes the model into brittle coordinate escapes (trace audits: this
        # was the #1 primitive error, ~41% of runs). Return
        # None; each caller prints an actionable "re-snapshot" note and no-ops the action,
        # so the model recovers on the next step in the bu plane instead of bailing.
        return None
    cx, cy, occluded = _json.loads(r)
    if occluded:
        # Not fatal — the mouse event still dispatches at these coords and often the
        # overlay is a transparent wrapper that forwards the click. But warn so a genuine
        # "covered by a modal" case is visible rather than a mystery no-op.
        _emit_notice(
            f"⚠️ click: ref {ref!r} appears covered by another element at its center; "
            "if the click has no effect, dismiss the overlay/modal first or re-snapshot."
        )
    return float(cx), float(cy)


def _element_clip_css(ref):
    """Page-coordinate screenshot clip for one verified snapshot ref."""
    raw = _bh.js(
        "(()=>{const e=document.querySelector(%s);if(!e)return null;"
        "e.scrollIntoView({block:'center',inline:'center'});"
        "const b=e.getBoundingClientRect();"
        "if(!(b.width>0&&b.height>0))return null;"
        "return JSON.stringify({x:scrollX+b.left,y:scrollY+b.top,"
        "width:b.width,height:b.height,scale:1});})()"
        % _json.dumps(_sel(ref))
    )
    if not raw or raw == "null":
        raise RuntimeError(
            "gac_runtime: screenshot ref %r is stale/gone or has no rendered "
            "box — re-run bu.snapshot() and use a visible fresh ref" % ref
        )
    value = _json.loads(raw)
    return {
        "x": max(0.0, float(value["x"])),
        "y": max(0.0, float(value["y"])),
        "width": float(value["width"]),
        "height": float(value["height"]),
        "scale": 1.0,
    }


def click(ref, *, button="left", clicks=1):
    """Click the element identified by a snapshot ref, e.g. click("d7:e2").

    Dispatches a real CDP mouse event (isTrusted) at the element's box center.
    For a double / right click use clicks=2 / button="right". For a raw viewport
    coordinate (canvas / no DOM) use click_xy(x, y).
    """
    cx, cy = _box_center_css(ref) or (None, None)
    if cx is None:
        raise RuntimeError(
            f"gac_runtime: click ref {ref!r} is stale/gone — re-run "
            "bu.snapshot() and click the fresh ref"
        )
    with _dispatch("click_at", [_audit_value(ref)]):
        _bh.click_at_xy(cx, cy, button=button, clicks=clicks)


def hover(ref):
    """Move the mouse over the element identified by a snapshot ref (real CDP mouseMoved).

    Many UIs only reveal their target on hover — a message's "…"/Edit action bar, a
    submenu that expands on mouseover, a tooltip. click() alone does NOT dispatch a
    mouseMoved, so the hover-only element never appears. Call hover(ref) first, then
    re-snapshot to pick up the newly-revealed refs, then click.
    """
    cx, cy = _box_center_css(ref) or (None, None)
    if cx is None:
        raise RuntimeError(
            f"gac_runtime: hover ref {ref!r} is stale/gone — re-run "
            "bu.snapshot() and use the fresh ref"
        )
    with _dispatch("hover", [_audit_value(ref)]):
        _bh.cdp("Input.dispatchMouseEvent", type="mouseMoved", x=cx, y=cy)


def handle_dialog(*, accept=True, prompt_text=None, timeout=5.0):
    """Wait for and handle a JavaScript dialog without event-mirror deadlock."""
    with _dispatch("handle_dialog", ["accept=%s" % bool(accept)]):
        info = _bh.handle_javascript_dialog(
            accept=accept,
            prompt_text=prompt_text,
            timeout=timeout,
        )
        if info is None:
            raise RuntimeError(
                "gac_runtime.handle_dialog: no JavaScript dialog appeared within "
                f"{timeout}s"
            )
    return info


def click_and_handle_dialog(ref, *, accept=True, prompt_text=None, timeout=5.0):
    """Click a ref asynchronously, then handle the JS dialog it opens.

    A normal CDP click waits for its command response, but an alert freezes the
    page thread before that response arrives. Scheduling the DOM click lets this
    call return to Python so the dialog can be accepted through browser-level CDP.
    """
    selector = _read_selector(ref)
    with _dispatch("click_scheduled", [_audit_value(ref)]):
        _bh.js(
            "(()=>{const e=document.querySelector(%s);if(!e)return false;"
            "setTimeout(()=>e.click(),0);return true})()" % _json.dumps(selector)
        )
    return handle_dialog(
        accept=accept,
        prompt_text=prompt_text,
        timeout=timeout,
    )


def type(ref, text=_UNSET, *, submit=None):  # noqa: A001 — 1:1 name with the discrete action
    """Focus the ref'd element and type. Trailing '\\n' (or submit=True) presses Enter.

    Native <input>/<textarea> go through fill_input. A contenteditable / rich-text
    block editor (Notion-style body, mail compose div) is NOT a native input —
    fill_input can't drive it — so we focus it, clear it, insert via CDP Input.insertText,
    then fire a bubbling ``input`` event the framework listens for. After writing we read
    the field back and raise if nothing landed, so a silent no-op surfaces immediately
    instead of being mistaken for success.
    """
    # Signature guard — the model very often calls bu.type("some text") with the ref
    # omitted (it's the #2 primitive error in trace audits, ~26% of runs). Python's raw
    # "missing required positional argument: 'text'" is a TypeError that aborts the WHOLE
    # GUI-code cell and pushes the model into brittle coordinate escapes. Catch the
    # one-arg misuse here and print an actionable message (NOT raise), naming the correct
    # signature, so the model fixes the call on the next step instead of changing paths.
    if text is _UNSET:
        # Only a single positional arg was passed. If it looks like a ref, the caller
        # meant an empty type (rare) — treat as ""; otherwise it's almost certainly the
        # text with the ref forgotten.
        looks_like_ref = _split_ref(ref)[1] is not None
        if not looks_like_ref:
            _emit_notice(
                f"⚠️ ACTION FAILED: bu.type({ref!r}) is missing the ref. The signature is "
                f"bu.type(ref, text) — first arg is a snapshot ref like 'd7:e12' "
                f"(from bu.snapshot()), second is the text. You passed only the text. "
                f"Re-call as bu.type('<ref>', {ref!r}) after picking the ref from "
                f"bu.snapshot()."
            )
            return
        text = ""  # a bare ref with no text → clear/no-op, keep going
    s = "" if text is None else str(text)
    enter = submit if submit is not None else (s.endswith("\n") or s.endswith("\\n"))
    body = s[:-1] if s.endswith("\n") else (s[:-2] if s.endswith("\\n") else s)
    sel = _read_selector(ref)
    kind = _bh.js(
        f"(()=>{{const e=document.querySelector({_json.dumps(sel)});if(!e)return 'NONE';"
        "const t=e.tagName.toLowerCase();"
        "if(t==='input'||t==='textarea')return 'INPUT';"
        # isContentEditable covers inherited editability; also accept an explicit
        # [contenteditable] attr and role=textbox (custom editors) — all generic signals.
        "if(e.isContentEditable||e.getAttribute('contenteditable')!==null||e.getAttribute('role')==='textbox')return 'CE';"
        # OTHER = a non-editable node (a wrapper div/span, a nav item). The model very
        # often picks the WRAPPER whose descendant is the real field (root cause of the
        # empty/nav-label readbacks). If exactly ONE editable descendant exists, retarget
        # to it and report the retarget; if none or many, we cannot guess — surface a clear
        # error naming the [edit] signal so the model re-picks, instead of silently no-op'ing
        # a fill_input on a <span>. Generic DOM traversal, no site-specific rules.
        "const eds=[...e.querySelectorAll('input,textarea,[contenteditable],[role=textbox]')]"
        ".filter(x=>{const b=x.getBoundingClientRect();return b.width>0&&b.height>0;});"
        "if(eds.length===1){const c=eds[0];const t2=c.tagName.toLowerCase();"
        "return (t2==='input'||t2==='textarea')?'RETARGET_INPUT':'RETARGET_CE';}"
        "return eds.length>1?'MANY':'OTHER';})()"
    )
    if kind == "NONE":
        raise RuntimeError(f"gac_runtime: type ref {ref!r} not found — re-run bu.snapshot().")
    retarget_token = None
    if kind in ("RETARGET_INPUT", "RETARGET_CE"):
        # Point sel at the sole editable descendant. The marker gets its OWN
        # attribute and its own per-call token: writing it into _REF_ATTR would
        # erase the child's real ref, and a fixed token would make querySelector
        # match the FIRST element still carrying a marker from an earlier
        # retarget in this document — i.e. type into a stale input.
        retarget_token = "r" + _uuid.uuid4().hex[:12]
        _bh.js(f"(()=>{{const e=document.querySelector({_json.dumps(sel)});if(!e)return;"
               "const c=[...e.querySelectorAll('input,textarea,[contenteditable],[role=textbox]')]"
               ".filter(x=>{const b=x.getBoundingClientRect();return b.width>0&&b.height>0;})[0];"
               f"if(c)c.setAttribute({_json.dumps(_RETARGET_ATTR)},{_json.dumps(retarget_token)});}})()")
        _emit_notice(
            f"ℹ️ type: ref {ref!r} was a wrapper; retargeting to its editable child."
        )
        sel = f'[{_RETARGET_ATTR}="{retarget_token}"]'
        kind = "INPUT" if kind == "RETARGET_INPUT" else "CE"
    elif kind in ("OTHER", "MANY"):
        hint = ("has no editable field inside it" if kind == "OTHER"
                else "wraps several inputs — ambiguous")
        raise RuntimeError(
            f"gac_runtime: type ref {ref!r} is not editable ({hint}). "
            "Re-run bu.snapshot() and pick the ref tagged [edit] (a real input / "
            "textarea / contenteditable), not a wrapper or label."
        )
    try:
        _type_body(ref, sel, kind, body, enter)
    finally:
        if retarget_token is not None:
            # Best effort: the token is unique per call, so a leftover marker can
            # never mis-target anything — this only keeps the DOM tidy.
            try:
                _bh.js(
                    "(()=>{const e=document.querySelector(%s);"
                    "if(e)e.removeAttribute(%s);})()"
                    % (_json.dumps(f'[{_RETARGET_ATTR}="{retarget_token}"]'),
                       _json.dumps(_RETARGET_ATTR))
                )
            except Exception:  # noqa: BLE001 — cleanup must not mask a real failure
                pass


def _split_input(text):
    return _re.findall(r"[\x00-\x7f]+|[^\x00-\x7f]+", text)


def _type_body(ref, sel, kind, body, enter):
    """Write ``body`` into the already-resolved selector, then optionally submit."""
    if kind == "CE":
        # Focus + select-all + Delete to clear, then type each char as a REAL key event
        # (keyDown+keyUp carrying text). insertText alone does NOT fire keydown, so
        # slash-command menus / rich-text editors (Notion-style) that listen on keydown
        # never trigger — e.g. typing "/" must fire the handler, not just insert "/".
        with _dispatch("type_ce", [_audit_value(ref), "chars=%d" % len(body)]):
            _bh.js(f"(()=>{{const e=document.querySelector({_json.dumps(sel)});e.focus();"
                   "const r=document.createRange();r.selectNodeContents(e);"
                   "const s=getSelection();s.removeAllRanges();s.addRange(r);})()")
            _bh.cdp("Input.dispatchKeyEvent", type="keyDown", key="Delete",
                    code="Delete", windowsVirtualKeyCode=46, nativeVirtualKeyCode=46)
            _bh.cdp("Input.dispatchKeyEvent", type="keyUp", key="Delete",
                    code="Delete", windowsVirtualKeyCode=46, nativeVirtualKeyCode=46)
            for piece in _split_input(body):
                if not piece.isascii():
                    _bh.cdp("Input.insertText", text=piece)
                    continue
                for ch in piece:
                    if ch == "\n":
                        # Interior newline in the content uses Shift+Enter so editors
                        # that submit on Enter retain the intended multiline value.
                        _bh.press_key("Enter", modifiers=8)
                        continue
                    # ASCII keyDown with text triggers app key handlers and inserts the
                    # character. Non-ASCII input uses Input.insertText above.
                    _bh.cdp(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        text=ch,
                        key=ch,
                        windowsVirtualKeyCode=ord(ch.upper()) if ch.isalnum() else 0,
                    )
                    _bh.cdp(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key=ch,
                        windowsVirtualKeyCode=ord(ch.upper()) if ch.isalnum() else 0,
                    )
            _bh.js(f"(()=>{{const e=document.querySelector({_json.dumps(sel)});"
                   "e.dispatchEvent(new Event('change',{bubbles:true}));})()")
    else:
        with _dispatch("fill_input", [_audit_value(ref), "chars=%d" % len(body)]):
            _bh.fill_input(sel, body)  # native input/textarea: focuses, types, fires input/change
    if body.strip():
        # Read the value back to catch a genuine no-op — but tolerantly. A React/Vue
        # controlled input commits asynchronously, so read too early and you see the
        # OLD/empty value (a false alarm). We retry a few times with a short settle,
        # read ONLY the element's own value/textContent (never a parent's aggregated
        # innerText — that reads sibling labels/placeholders and false-alarms), and if
        # it still looks unset we only WARN (print), never raise: raising aborts the
        # whole program and pushes the model into brittle js/coordinate fallbacks.
        def _readback():
            return _bh.js(
                f"(()=>{{const e=document.querySelector({_json.dumps(sel)});if(!e)return '__SCU_NONE__';"
                "if(e.type==='password')return '__SCU_PW__';"  # masked — unreadable
                # value browser-normalizes for these types (or reads empty mid-commit)
                "if(['date','time','datetime-local','month','week','color','range','number','file'].includes(e.type))return '__SCU_SKIP__';"
                # ONLY the element's own value (input/textarea) or its own textContent
                # (contenteditable). Never fall back to innerText of a parent.
                "if(e.value!==undefined)return (e.value||'').trim();"
                "return (e.textContent||'').trim();})()"
            )
        def _an(s): return "".join(c for c in (s or "").lower() if c.isalnum())
        def _landed(v): return body.strip() in (v or "") or _an(body) in _an(v)
        got = _readback()
        # Retry: a controlled input may not have committed yet on the first read.
        for _ in range(3):
            if got in ("__SCU_PW__", "__SCU_SKIP__", "__SCU_NONE__") or _landed(got):
                break
            _time.sleep(0.25)
            got = _readback()
        # Still not landed → the library itself retries once via a native value setter
        # (React/Vue controlled inputs ignore plain typing but honor the native setter +
        # input event). This pulls the "re-try a different way" loop into the tool instead
        # of making the model burn steps on it.
        if got not in ("__SCU_PW__", "__SCU_SKIP__", "__SCU_NONE__") and not _landed(got):
            try:
                _bh.js(
                    f"(()=>{{const e=document.querySelector({_json.dumps(sel)});if(!e)return;"
                    "const proto=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
                    "const set=Object.getOwnPropertyDescriptor(proto,'value');"
                    "if(set&&set.set){e.focus();set.set.call(e,"+_json.dumps(body)+");"
                    "e.dispatchEvent(new Event('input',{bubbles:true}));"
                    "e.dispatchEvent(new Event('change',{bubbles:true}));}})()"
                )
                _time.sleep(0.2)
                got = _readback()
            except Exception:  # noqa: BLE001 — best effort, fall through to the signal below
                pass
        # Emit a CLEAR failure signal (not a soft note) so the model knows this step did
        # NOT land and must switch tactics — but NEVER raise: raising aborts the whole
        # program and pushes the model into brittle js/coordinate escapes. The message
        # names concrete alternatives so the model changes approach instead of flailing.
        if got not in ("__SCU_PW__", "__SCU_SKIP__", "__SCU_NONE__") and not _landed(got):
            _emit_notice(
                f"⚠️ ACTION FAILED: type into {ref!r} did NOT land (field reads back "
                f"{(got or '')[:40]!r}). The text is NOT there — do not assume success. "
                "This is usually a controlled/custom editor or the wrong target. Switch "
                "tactics: re-snapshot to pick the ref marked [edit]; or drive it another "
                "way (a spreadsheet cell / rich editor often takes bu.press_key or "
                "clicking then typing, not type-into-ref)."
            )
    if enter:
        # fill_input leaves focus on the element; press a REAL Enter key event
        # (CDP Input.dispatchKeyEvent) so <form>s submit — a synthetic DOM
        # KeyboardEvent is often ignored by real submit handlers.
        with _dispatch("press_key", ["Enter"]):
            _bh.press_key("Enter")


def select(ref, value):
    """Pick an option by value / visible text — works for BOTH a native <select> and a
    custom (div/li-based) dropdown, the kind React mock apps overwhelmingly use.

    Native <select>: sets value (matched exact / trimmed / case-insensitive / substring on
    value+text) and fires input+change.

    Custom dropdown: a plain <div>/<li> menu with no <option> elements. We CDP-click the
    trigger ref to open it, then look for the option ONLY inside the UI that opening
    produced — the trigger's aria-controls/aria-owns target, or whatever became visible
    after the click, or (last resort) the trigger's immediate neighbourhood. Matching is
    on an element's OWN text (not aggregated innerText) so we click the leaf option
    rather than a wrapper. This folds the click→snapshot→find-option→click dance the
    model otherwise burns 3-4 steps on into one call. All generic DOM/CDP, no per-site
    selectors.

    Raises if nothing matches, and raises rather than guessing when several options in
    the popup match (a silent no-op reads as success and hides the failure; a guess is
    worse — it clicks something the caller did not ask for).
    """
    kind = _bh.js(
        f"(()=>{{const s=document.querySelector({_json.dumps(_read_selector(ref))});if(!s)return 'NO_ELEMENT';"
        "return s.tagName.toLowerCase()==='select'?'NATIVE':'CUSTOM';})()"
    )
    if kind == "NO_ELEMENT":
        raise RuntimeError(f"gac_runtime: select ref {ref!r} not found — re-run bu.snapshot().")
    if kind == "CUSTOM":
        return _select_custom(ref, value)
    with _dispatch("select_native", [_audit_value(ref), _audit_value(value)]):
        r = _bh.js(
            f"(()=>{{const s=document.querySelector({_json.dumps(_read_selector(ref))});if(!s)return 'NO_ELEMENT';"
            f"const v={_json.dumps(str(value))};const vt=v.trim().toLowerCase();"
            "const opts=[...s.options];"
            "const norm=o=>[o.value,(o.text||'').trim()];"
            "let m=opts.find(o=>o.value===v||o.text===v)"
            "||opts.find(o=>norm(o).some(t=>t.toLowerCase()===vt))"
            "||opts.find(o=>norm(o).some(t=>t.toLowerCase().includes(vt)));"
            "if(!m)return 'NO_MATCH';"
            "s.value=m.value;"
            "s.dispatchEvent(new Event('input',{bubbles:true}));"
            "s.dispatchEvent(new Event('change',{bubbles:true}));return 'OK';})()"
        )
    if r == "NO_ELEMENT":
        raise RuntimeError(f"gac_runtime: select ref {ref!r} not found — re-run bu.snapshot().")
    if r == "NO_MATCH":
        raise RuntimeError(
            f"gac_runtime: select {ref!r} has no option matching {value!r} "
            "(tried exact / trimmed / case-insensitive / substring on value and text)."
        )


#: Snapshot the set of currently-visible elements so the option search after the
#: open click can ask "what appeared?". Visibility, not existence: plenty of
#: dropdowns pre-render their options hidden and merely toggle display.
_MARK_PRE_VISIBLE_JS = r"""
(() => {
  const vis = e => { const b = e.getBoundingClientRect(); const st = getComputedStyle(e);
    return b.width > 0 && b.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  try {
    const s = new WeakSet();
    for (const e of document.querySelectorAll('*')) { if (vis(e)) s.add(e); }
    window['__PRE_VISIBLE_PROP__'] = s;
    return true;
  } catch (_) { return false; }
})()
""".replace("__PRE_VISIBLE_PROP__", _PRE_VISIBLE_PROP)

#: Find the option inside the UI the open click produced. Searching the whole page
#: (the old behaviour) happily clicks an unrelated element elsewhere that happens to
#: carry the same label.
_FIND_OPTION_JS = r"""
(() => {
  const v = __VALUE__, vt = v.trim().toLowerCase();
  const trig = document.querySelector(__TRIGGER__);
  const pre = window['__PRE_VISIBLE_PROP__'] || null;
  const own = e => { let s = ''; for (const n of e.childNodes) if (n.nodeType === 3) s += n.textContent; return s.trim(); };
  const vis = e => { const b = e.getBoundingClientRect(); const st = getComputedStyle(e);
    return b.width > 0 && b.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const appeared = e => !pre || !pre.has(e);
  // 1) Explicit ARIA wiring is the strongest possible statement of "this popup
  //    belongs to that trigger" — trust it over any heuristic.
  const roots = [];
  const ids = ((trig && (trig.getAttribute('aria-controls') || trig.getAttribute('aria-owns'))) || '').split(/\s+/);
  for (const id of ids) {
    if (!id) continue;
    const n = document.getElementById(id);
    if (n && vis(n)) roots.push(n);
  }
  let scope = roots.length ? 'aria' : '';
  // 2) Otherwise: the subtree roots that became visible because of the click. Requires
  //    the pre-click marker — without it "became visible" means every visible element,
  //    which is the page-wide search this replaced. Fall through instead.
  if (!roots.length && pre) {
    for (const e of document.querySelectorAll('*')) {
      if (!vis(e) || !appeared(e)) continue;
      if (e.parentElement && vis(e.parentElement) && appeared(e.parentElement)) continue;
      roots.push(e);
    }
    if (roots.length) scope = 'appeared';
  }
  // 3) Last resort: the trigger's immediate neighbourhood, exact matches only.
  const exactOnly = !roots.length;
  if (!roots.length && trig && trig.parentElement) { roots.push(trig.parentElement); scope = 'neighbourhood'; }
  if (!roots.length) return JSON.stringify({status: 'NO_SCOPE'});
  const seen = new Set(), cands = [];
  for (const root of roots) {
    for (const e of [root, ...root.querySelectorAll('*')]) {
      if (seen.has(e) || e === trig || !vis(e)) continue;
      seen.add(e);
      if (scope === 'neighbourhood' && !appeared(e) && trig && trig.contains(e)) continue;
      const t = own(e).toLowerCase();
      if (!t) continue;
      if (t === vt) cands.push({e: e, exact: true, text: own(e)});
      else if (!exactOnly && t.includes(vt)) cands.push({e: e, exact: false, text: own(e)});
    }
  }
  if (!cands.length) return JSON.stringify({status: 'NO_OPT', scope: scope});
  let best = cands.filter(c => c.exact);
  if (!best.length) best = cands;
  // Both a container and its own text-bearing child can match; keep the innermost.
  const inner = best.filter(c => !best.some(o => o !== c && c.e.contains(o.e)));
  if (inner.length > 1) {
    return JSON.stringify({status: 'AMBIGUOUS', scope: scope,
                           texts: inner.slice(0, 5).map(c => c.text)});
  }
  const m = inner[0].e;
  m.scrollIntoView({block: 'center'});
  const b = m.getBoundingClientRect();
  return JSON.stringify({status: 'OK', scope: scope, text: inner[0].text,
                         x: b.left + b.width / 2, y: b.top + b.height / 2});
})()
""".replace("__PRE_VISIBLE_PROP__", _PRE_VISIBLE_PROP)


def _select_custom(ref, value):
    """Open a custom (non-<select>) dropdown at ``ref`` and click the option matching value."""
    # 1) open: CDP-click the trigger's center (real isTrusted click, opens React menus).
    cxy = _box_center_css(ref)
    if cxy is None:
        raise RuntimeError(
            f"gac_runtime: select ref {ref!r} is stale/gone — re-run bu.snapshot()."
        )
    cx, cy = cxy
    trigger_sel = _read_selector(ref)
    try:
        _bh.js(_MARK_PRE_VISIBLE_JS)
    except Exception:  # noqa: BLE001 — without the marker the search just falls back
        pass
    # The open click is a real side effect: record it BEFORE dispatch so a failure to
    # find the option still reports the dropdown as opened.
    with _dispatch("click_at", [_audit_value(ref), "select_open"]):
        _bh.click_at_xy(cx, cy)
    find_opt = (
        _FIND_OPTION_JS
        .replace("__VALUE__", _json.dumps(str(value)))
        .replace("__TRIGGER__", _json.dumps(trigger_sel))
    )
    result = {}
    for _ in range(8):  # ~1.6s: options may render async after the open click
        raw = _bh.js(find_opt)
        try:
            result = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            result = {}
        if isinstance(result, dict) and result.get("status") in ("OK", "AMBIGUOUS"):
            break
        _time.sleep(0.2)
    if not isinstance(result, dict):
        result = {}
    if result.get("status") == "AMBIGUOUS":
        raise BrowserUseError(
            f"gac_runtime: select {ref!r} — the dropdown is "
            f"open and {len(result.get('texts') or [])} of its entries match {value!r} "
            f"({', '.join(repr(t) for t in (result.get('texts') or []))}). Not guessing: "
            "bu.snapshot() the open dropdown and click the exact option ref.",
            code=ERR_SELECT_AMBIGUOUS,
        )
    if result.get("status") != "OK":
        raise BrowserUseError(
            f"gac_runtime: select {ref!r} — clicked the "
            f"trigger but found no option matching {value!r} "
            f"(searched: {result.get('scope') or result.get('status') or 'nothing'}). "
            "The click DID land, so the dropdown may well be open: bu.snapshot() now to "
            "see the options and click the option's ref directly.",
            code=ERR_SELECT_NO_OPTION,
        )
    with _dispatch("click_at", [_audit_value(result.get("text") or value), "select_option"]):
        _bh.click_at_xy(float(result["x"]), float(result["y"]))
    return "OK"


# --------------------------------------------------------------------------- #
# coordinate normalization + coord actions (escape hatch: canvas / no DOM)      #
# --------------------------------------------------------------------------- #
def _viewport():
    """(innerWidth, innerHeight) in CSS px of the current viewport."""
    info = _bh.page_info()
    if isinstance(info, dict) and "dialog" in info:
        raise RuntimeError(
            f"gac_runtime: a native dialog is open ({info['dialog']!r}); "
            "handle it first with bu.handle_dialog(accept=True) or "
            "bu.cdp('Page.handleJavaScriptDialog', accept=True)."
        )
    return float(info["w"]), float(info["h"])


def _normalized_coordinate(value, axis):
    """Validate one 0-1000 viewport coordinate before any backend call."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _coded_error(
            TypeError,
            "BU_COORDINATE_SPACE",
            "gac_runtime: %s must be a number in the 0-1000 viewport "
            "coordinate space. No browser action was dispatched." % axis,
        )
    normalized = float(value)
    if not _math.isfinite(normalized) or not 0 <= normalized <= COORD_SPACE:
        raise _coded_error(
            ValueError,
            "BU_COORDINATE_SPACE",
            "gac_runtime: %s=%s is outside the 0-1000 viewport coordinate "
            "space. Convert screenshot pixels by dividing by viewport width/height "
            "and multiplying by 1000. No browser action was dispatched."
            % (axis, value),
        )
    return normalized


def _to_css(x, y):
    """0-1000 viewport coords -> CSS pixel coords for CDP Input events."""
    normalized_x = _normalized_coordinate(x, "x")
    normalized_y = _normalized_coordinate(y, "y")
    w, h = _viewport()
    return (
        round(normalized_x / COORD_SPACE * w, 3),
        round(normalized_y / COORD_SPACE * h, 3),
    )


def click_xy(x, y):
    """Click at 0-1000 viewport coords. Only when no ref/DOM node exists (canvas)."""
    px, py = _to_css(x, y)
    with _dispatch("click_at", ["%s,%s" % (x, y)]):
        _bh.click_at_xy(px, py, button="left", clicks=1)


def left_double(x, y):
    """Double click at 0-1000 viewport coords (canvas / no DOM)."""
    px, py = _to_css(x, y)
    with _dispatch("left_double", ["%s,%s" % (x, y)]):
        _bh.click_at_xy(px, py, button="left", clicks=2)


def right_single(x, y):
    """Right click at 0-1000 viewport coords (canvas / no DOM)."""
    px, py = _to_css(x, y)
    with _dispatch("right_single", ["%s,%s" % (x, y)]):
        _bh.click_at_xy(px, py, button="right", clicks=1)


def drag(x1, y1, x2, y2, *, steps=12):
    """Press at (x1,y1), move to (x2,y2) over several steps, release. 0-1000 viewport coords.

    A single jump from start to end (one mouseMoved) does NOT reliably trigger HTML5
    native drag (draggable=true) or JS drag libraries (dnd-kit, react-beautiful-dnd,
    SortableJS): they need a small initial delta to start the drag session, then a
    sequence of mousemoves to fire dragover before drop. So we nudge off the start
    point, then interpolate ``steps`` moves to the target before releasing.
    """
    sx, sy = _to_css(x1, y1)
    ex, ey = _to_css(x2, y2)
    with _dispatch("drag", ["%s,%s->%s,%s" % (x1, y1, x2, y2)]):
        _bh.cdp("Input.dispatchMouseEvent", type="mousePressed", x=sx, y=sy,
                button="left", clickCount=1)
        # initial nudge so libraries requiring a movement threshold start the drag
        _bh.cdp("Input.dispatchMouseEvent", type="mouseMoved",
                x=sx + (6 if ex >= sx else -6), y=sy + (6 if ey >= sy else -6), button="left")
        n = max(2, int(steps))
        for i in range(1, n + 1):
            _bh.cdp("Input.dispatchMouseEvent", type="mouseMoved",
                    x=sx + (ex - sx) * i / n, y=sy + (ey - sy) * i / n, button="left")
        _bh.cdp("Input.dispatchMouseEvent", type="mouseReleased", x=ex, y=ey,
                button="left", clickCount=1)


def scroll(x, y, direction, amount=3):
    """Scroll at 0-1000 viewport coords. direction: 'up'|'down'|'left'|'right'."""
    d = str(direction).lower()
    if d not in {"up", "down", "left", "right"}:
        raise _coded_error(
            ValueError,
            "BU_SCROLL_DIRECTION",
            "gac_runtime.scroll: direction must be exactly 'up', 'down', "
            "'left', or 'right'. No scroll was dispatched.",
        )
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise _coded_error(
            TypeError,
            "BU_SCROLL_AMOUNT",
            "gac_runtime.scroll: amount must be an integer in [1, 10].",
        )
    if not 1 <= amount <= _MAX_SCROLL_AMOUNT:
        raise _coded_error(
            ValueError,
            "BU_SCROLL_AMOUNT",
            "gac_runtime.scroll: amount must be an integer in [1, 10]; "
            "large pixel-like values are invalid.",
        )
    px, py = _to_css(x, y)
    dist = _SCROLL_STEP_PX * amount
    dx = dy = 0
    if d == "up":
        dy = -dist
    elif d == "down":
        dy = dist
    elif d == "right":
        dx = dist
    else:
        dx = -dist
    with _dispatch("scroll", ["%s,%s" % (x, y), d]):
        _bh.scroll(px, py, dy=dy, dx=dx)
