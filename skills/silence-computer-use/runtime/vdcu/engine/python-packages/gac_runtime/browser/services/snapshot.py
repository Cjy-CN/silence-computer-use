"""Snapshot generation, document-qualified ref construction, and ref lookup."""

from ..domain import core as _core
from ..domain.core import *


_SNAPSHOT_JS = r"""
(() => {
  const FULL = __FULL__;
  const SCOPE = __SCOPE_READ__;
  const SEL = 'a,button,input,textarea,select,[role=button],[role=link],'
            + '[role=textbox],[role=checkbox],[role=tab],[role=menuitem],'
            + '[onclick],[contenteditable]';
  // Refs are stable DOM identities, not positions in one snapshot. A document-wide
  // monotonic counter guarantees that an element omitted by a compact/viewport
  // snapshot never donates its id to a new element, and that a node id is never
  // reused after its element is removed. Switching full=False/True keeps the same
  // ref on the same node. Navigation creates a new document and document number.
  const nodes = FULL ? document.querySelectorAll('*') : document.querySelectorAll(SEL);
  const out = [];
  let i = Number(window.__seedCuaRefCounter || 0);
  for (const e of nodes) {
    const r = e.getBoundingClientRect();
    const visible = r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight
                    && getComputedStyle(e).visibility !== 'hidden';
    if (!visible) continue;
    // Prefer the element's own label over aggregated innerText from descendants.
    let _own = '';
    for (const n of e.childNodes) { if (n.nodeType === 3) _own += n.textContent; }
    _own = _own.trim();
    const name = (e.value || e.placeholder || e.getAttribute('aria-label')
                  || _own || (e.innerText || '').trim()).trim().slice(0, 80);
    if (!FULL && !name && !['input','textarea','select'].includes(e.tagName.toLowerCase())) continue;
    let ref = e.getAttribute('__REF_ATTR__');
    if (!ref) {
      ref = 'e' + (i++);
      e.setAttribute('__REF_ATTR__', ref);
    }
    const st = [];
    if (e.id) st.push('id=' + String(e.id).slice(0, 60));
    if (e.disabled) st.push('disabled');
    if (e.checked) st.push('checked');
    if (e.getAttribute('aria-selected') === 'true') st.push('selected');
    const _tag = e.tagName.toLowerCase();
    if (_tag === 'input' || _tag === 'textarea' || e.isContentEditable
        || e.getAttribute('contenteditable') !== null || e.getAttribute('role') === 'textbox') st.push('edit');
    out.push({
      ref, tag: e.tagName.toLowerCase(),
      role: e.getAttribute('role') || null,
      type: e.getAttribute('type') || null,
      name, state: st,
      url: e.tagName.toLowerCase() === 'a' ? (e.getAttribute('href') || null) : null,
    });
  }
  window.__seedCuaRefCounter = i;
  return JSON.stringify({scope: SCOPE, url: location.href, title: document.title,
                         w: innerWidth, h: innerHeight, els: out});
})()
""".replace("__REF_ATTR__", _REF_ATTR).replace("__SCOPE_READ__", _SCOPE_READ_JS.strip())


def make_ref(scope, node):
    """The model-facing ref form: document-qualified, therefore verifiable.

    One form only, no ``@``: this is both what a snapshot line prints and what an
    action accepts, so "pass it back exactly as printed" has a single reading.
    """
    return Ref("%s:%s" % (scope, node)) if scope else Ref(node)


def _snapshot_lines(full=False):
    """Silent snapshot kernel: (scope_state, lines). Emits no observation.

    ``find`` needs the snapshot without paying for it twice: it calls this kernel
    instead of the self-displaying ``snapshot()``.
    """
    raw = _bh.js(_SNAPSHOT_JS.replace("__FULL__", "true" if full else "false"))
    try:
        payload = _json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    state = _as_scope_state(payload)
    if not state["scope"]:
        state["scope"] = _number_document(state)
    _core._SCOPE_MEMO = state
    scope = state["scope"]
    lines = []
    for element in payload.get("els") or []:
        parts = [
            str(make_ref(scope, element["ref"])),
            element.get("role") or element.get("tag", ""),
        ]
        if element.get("name"):
            parts.append(f'"{element["name"]}"')
        tags = []
        if element.get("type"):
            tags.append(f'type={element["type"]}')
        tags += element.get("state") or []
        if element.get("url"):
            tags.append(f'-> {element["url"]}')
        line = " ".join(parts)
        if tags:
            line += " [" + " ".join(tags) + "]"
        lines.append(line)
    return state, lines


def _scope_header(state):
    """Format the document context shared by snapshot and find observations."""
    return "scope=%s url=%s viewport=%sx%s" % (
        state.get("scope") or _NO_DOC_SCOPE,
        state.get("url") or "",
        state.get("w") or 0,
        state.get("h") or 0,
    )


def snapshot(full=False):
    """Observe a compact interactive-element map with document-bound refs.

    First line is the document number and context; then lines like
    ``d7:e2 button "Sign In"``. The document number is part of the ref: pass it
    back exactly as printed. Re-run after navigation or a tab change.
    """
    state, lines = _snapshot_lines(full=full)
    body = "\n".join([_scope_header(state)] + lines)
    _emit_observation("dom", body, scope=state.get("scope"))
    return body


def find(query, *, limit=10):
    """Find interactive elements matching ``query`` case-insensitively.

    Returns ranked document-qualified refs that can be passed directly to an
    action. Only matching lines are observed. Returns an empty list when nothing
    matches.
    """
    q = str(query).strip().lower()
    state, all_lines = _snapshot_lines(full=True)
    scope = state.get("scope") or ""
    lines = [line for line in all_lines if q in line.lower()]
    preferred_roles = {
        "button", "a", "input", "textarea", "select", "textbox", "checkbox",
        "link", "menuitem", "tab",
    }
    container_roles = {"html", "body"}

    def _rank(line):
        parts = line.lstrip().lstrip("@").split(None, 2)
        role = parts[1].lower() if len(parts) > 1 else ""
        quoted = ""
        if '"' in line:
            quoted = line.split('"', 2)[1].strip().lower()
        exact = 0 if quoted == q else 1
        role_rank = 0 if role in preferred_roles else (2 if role in container_roles else 1)
        return exact, role_rank, len(quoted or line)

    lines = sorted(lines, key=_rank)[:limit]
    refs = RefList()
    for line in lines:
        match = _re.match(r'\s*@?(?:d[0-9a-f]+:)?(e\d+)', line)
        if match:
            refs.append(make_ref(scope, match.group(1)))
    if lines:
        body = "\n".join([_scope_header(state)] + lines)
    else:
        body = "%s\nno match for %r — try bu.snapshot(full=True) or scroll and retry" % (
            _scope_header(state), str(query),
        )
    _emit_observation("dom", body, scope=scope)
    return refs


__all__ = ["find", "make_ref", "snapshot"]
