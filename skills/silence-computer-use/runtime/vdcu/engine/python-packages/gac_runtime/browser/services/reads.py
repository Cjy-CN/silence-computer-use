"""Element text, attributes, and bounded structured page reads."""

from ..domain.core import *


def get_page_text():
    """Extract the page's main visible text (document.body.innerText, trimmed).

    Read-only whole-page text for lookups/summaries — cheaper and surer than a
    screenshot. For one element use bu.text(ref); for raw data use bu.http_get(url).
    """
    return _bh.js("(()=>{return (document.body && document.body.innerText || '').trim()})()")


def get(ref, attr):
    """Read a live DOM property, falling back to an HTML attribute."""
    selector = _read_selector(ref)
    return _bh.js(
        f"(()=>{{const e=document.querySelector({_json.dumps(selector)});if(!e)return null;"
        f"const a={_json.dumps(attr)},v=e[a];"
        "return v===undefined?e.getAttribute(a):v;})()"
    )


def text(ref):
    """Read text/value from a ref, CSS selector, or plain element id."""
    selector = _read_selector(ref)
    return _bh.js(
        f"(()=>{{const e=document.querySelector({_json.dumps(selector)});if(!e)return null;"
        "return (e.innerText||e.value||'').trim();})()"
    )


_READ_ALL_FIELDS = frozenset({
    "text", "value", "href", "src", "tag", "role", "type", "name",
    "id", "title", "alt", "placeholder", "aria_label", "checked",
    "disabled", "selected",
})
_READ_ALL_DEFAULT_FIELDS = ("text", "href")
_READ_ALL_MAX_ROWS = 200
_CONTENT_EXPORT_MIN_CHARS = 200
_CONTENT_EXPORT_MAX_CHARS = 40_000


def _validated_read_fields(fields):
    if fields is None:
        return list(_READ_ALL_DEFAULT_FIELDS)
    if isinstance(fields, str):
        values = [fields]
    elif isinstance(fields, (list, tuple)):
        values = list(fields)
    else:
        raise _coded_error(
            TypeError,
            ERR_READ_FIELDS,
            "gac_runtime.read_all: fields must be a field name or a list "
            "of field names. No page JavaScript was dispatched.",
        )
    normalized = []
    for field in values:
        name = str(field or "").strip()
        if name not in _READ_ALL_FIELDS:
            raise _coded_error(
                ValueError,
                ERR_READ_FIELDS,
                "gac_runtime.read_all: unsupported field %r. Allowed fields: "
                "%s. No page JavaScript was dispatched."
                % (field, ", ".join(sorted(_READ_ALL_FIELDS))),
            )
        if name not in normalized:
            normalized.append(name)
    if not normalized:
        raise _coded_error(
            ValueError,
            ERR_READ_FIELDS,
            "gac_runtime.read_all: fields cannot be empty. No page "
            "JavaScript was dispatched.",
        )
    return normalized


def _validated_read_limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise _coded_error(
            TypeError,
            ERR_READ_LIMIT,
            "gac_runtime.read_all: limit must be an integer in [1, 200]. "
            "No page JavaScript was dispatched.",
        )
    if not 1 <= limit <= _READ_ALL_MAX_ROWS:
        raise _coded_error(
            ValueError,
            ERR_READ_LIMIT,
            "gac_runtime.read_all: limit must be in [1, 200]. No page "
            "JavaScript was dispatched.",
        )
    return limit


def read_all(selector_or_refs, fields=None, limit=50):
    """Read repeated elements into structured rows without dumping the whole page.

    ``selector_or_refs`` is a CSS selector, one document-scoped ref, or a list of
    document-scoped refs. ``fields`` defaults to ``text`` and ``href``.
    """
    selected_fields = _validated_read_fields(fields)
    row_limit = _validated_read_limit(limit)
    mode = "css"
    selector = ""
    entries = []
    if isinstance(selector_or_refs, (list, tuple, RefList)):
        mode = "refs"
        for ref in list(selector_or_refs)[:row_limit]:
            entries.append({"selector": _sel(ref), "ref": str(ref)})
    elif isinstance(selector_or_refs, str):
        _scope, node = _split_ref(selector_or_refs)
        if node is not None:
            mode = "refs"
            entries = [{"selector": _sel(selector_or_refs), "ref": str(selector_or_refs)}]
        else:
            selector = selector_or_refs.strip()
            if not selector:
                raise _coded_error(
                    ValueError,
                    ERR_READ_TARGET,
                    "gac_runtime.read_all: CSS selector cannot be empty. "
                    "No page JavaScript was dispatched.",
                )
    else:
        raise _coded_error(
            TypeError,
            ERR_READ_TARGET,
            "gac_runtime.read_all: selector_or_refs must be a CSS string or "
            "a list of document-scoped refs. No page JavaScript was dispatched.",
        )
    script = r"""
(() => {
  const mode = __MODE__, selector = __SELECTOR__, entries = __ENTRIES__;
  const fields = __FIELDS__, limit = __LIMIT__;
  let items;
  if (mode === 'css') {
    items = [...document.querySelectorAll(selector)].slice(0, limit)
      .map(e => ({e, ref: null}));
  } else {
    items = entries.map(item => ({
      e: document.querySelector(item.selector), ref: item.ref
    })).filter(item => !!item.e).slice(0, limit);
  }
  const read = (e, field) => {
    if (field === 'text') return (e.innerText || e.textContent || '').trim();
    if (field === 'value') return e.value === undefined ? null : e.value;
    if (field === 'href') return e.href || e.getAttribute('href') || null;
    if (field === 'src') return e.currentSrc || e.src || e.getAttribute('src') || null;
    if (field === 'tag') return (e.tagName || '').toLowerCase();
    if (field === 'aria_label') return e.getAttribute('aria-label');
    if (field === 'checked' || field === 'disabled' || field === 'selected') {
      return field in e ? !!e[field] : null;
    }
    return e.getAttribute(field) ?? e[field] ?? null;
  };
  return JSON.stringify(items.map((item, index) => {
    const row = {index};
    if (item.ref) row.ref = item.ref;
    for (const field of fields) row[field] = read(item.e, field);
    return row;
  }));
})()
"""
    raw = _bh.js(
        script
        .replace("__MODE__", _json.dumps(mode))
        .replace("__SELECTOR__", _json.dumps(selector))
        .replace("__ENTRIES__", _json.dumps(entries))
        .replace("__FIELDS__", _json.dumps(selected_fields))
        .replace("__LIMIT__", str(row_limit))
    )
    value = _json.loads(raw) if isinstance(raw, str) else raw
    return value if isinstance(value, list) else []


_CONTENT_EXPORT_JS = r"""
(() => {
  const MAX = __MAX__;
  const budget = {
    headings: Math.max(30, Math.floor(MAX * 0.15)),
    paragraphs: Math.max(80, Math.floor(MAX * 0.45)),
    links: Math.max(40, Math.floor(MAX * 0.20)),
    tables: Math.max(40, Math.floor(MAX * 0.20)),
  };
  const used = {headings: 0, paragraphs: 0, links: 0, tables: 0};
  let truncated = false;
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = e => {
    if (!e || e.getAttribute('aria-hidden') === 'true') return false;
    const style = getComputedStyle(e);
    return style.display !== 'none' && style.visibility !== 'hidden'
      && e.getClientRects().length > 0;
  };
  const take = (value, bucket, cap) => {
    let text = clean(value);
    if (!text) return '';
    if (text.length > cap) { text = text.slice(0, cap - 1) + '…'; truncated = true; }
    const room = budget[bucket] - used[bucket];
    if (room <= 0) { truncated = true; return ''; }
    if (text.length > room) { text = text.slice(0, Math.max(0, room - 1)) + '…'; truncated = true; }
    used[bucket] += text.length;
    return text;
  };
  const candidates = [...document.querySelectorAll('main,article,[role="main"]')]
    .filter(visible);
  candidates.sort((a, b) => clean(b.innerText).length - clean(a.innerText).length);
  const root = candidates[0] || document.body || document.documentElement;
  const headings = [], paragraphs = [], links = [], tables = [];
  const seenParagraphs = new Set(), seenLinks = new Set();
  for (const e of root.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
    if (!visible(e)) continue;
    const text = take(e.innerText || e.textContent, 'headings', 500);
    if (text) headings.push({level: Number(e.tagName.slice(1)), text});
  }
  for (const e of root.querySelectorAll('p,li,blockquote,pre')) {
    if (!visible(e)) continue;
    const original = clean(e.innerText || e.textContent);
    if (!original || seenParagraphs.has(original)) continue;
    seenParagraphs.add(original);
    const text = take(original, 'paragraphs', 1500);
    if (text) paragraphs.push(text);
  }
  for (const e of root.querySelectorAll('a[href]')) {
    if (!visible(e)) continue;
    const key = clean(e.innerText || e.textContent) + '\n' + String(e.href || '');
    if (seenLinks.has(key)) continue;
    seenLinks.add(key);
    const text = take(e.innerText || e.textContent, 'links', 400);
    const href = take(e.href, 'links', 1000);
    if (text || href) links.push({text, href});
  }
  for (const table of [...root.querySelectorAll('table')].slice(0, 10)) {
    if (!visible(table)) continue;
    const rows = [];
    for (const tr of [...table.querySelectorAll('tr')].slice(0, 30)) {
      const cells = [];
      for (const cell of [...tr.querySelectorAll('th,td')].slice(0, 12)) {
        const text = take(cell.innerText || cell.textContent, 'tables', 250);
        if (text) cells.push(text);
      }
      if (cells.length) rows.push(cells);
    }
    const caption = take(
      (table.caption && table.caption.innerText) || '', 'tables', 300
    );
    if (rows.length || caption) tables.push({caption, rows});
  }
  return JSON.stringify({
    title: document.title || '',
    url: location.href,
    language: document.documentElement.lang || '',
    headings, paragraphs, links, tables,
    truncated,
    chars: Object.values(used).reduce((a, b) => a + b, 0),
  });
})()
"""


def content_export(max_chars=3000):
    """Export structured main content without returning document.body wholesale."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise _coded_error(
            TypeError,
            ERR_EXPORT_LIMIT,
            "gac_runtime.content_export: max_chars must be an integer in "
            "[200, 40000]. No page JavaScript was dispatched.",
        )
    if not _CONTENT_EXPORT_MIN_CHARS <= max_chars <= _CONTENT_EXPORT_MAX_CHARS:
        raise _coded_error(
            ValueError,
            ERR_EXPORT_LIMIT,
            "gac_runtime.content_export: max_chars must be in [200, 40000]. "
            "No page JavaScript was dispatched.",
        )
    raw = _bh.js(_CONTENT_EXPORT_JS.replace("__MAX__", str(max_chars)))
    value = _json.loads(raw) if isinstance(raw, str) else raw
    return value if isinstance(value, dict) else {}
