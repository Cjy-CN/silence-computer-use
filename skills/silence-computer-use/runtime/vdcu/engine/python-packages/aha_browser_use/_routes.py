"""Complete and immutable routing contract for the public tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Route(str, Enum):
    # Only pure seed helpers with no selected-page synchronization, Aha ref
    # translation, return reshaping, or error-code adaptation may use this route.
    SEED_DIRECT = "SEED_DIRECT"
    AHA_BUA = "AHA_BUA"
    NATIVE_COMPAT = "NATIVE_COMPAT"
    CDP_COMPAT = "CDP_COMPAT"
    LOCAL = "LOCAL"
    COMPOSITE = "COMPOSITE"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class RouteSpec:
    route: Route
    operations: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    reason: str = ""
    cases: tuple[str, ...] = ()
    seed_tool: str | None = None


ROUTES = {
    "snapshot": RouteSpec(
        Route.AHA_BUA,
        ("page.observe",),
        reason="Snapshot owns the Aha observation/ref contract and must use Native page.observe.",
        cases=("observe.viewport", "observe.scopes"),
    ),
    "webmcp_tools": RouteSpec(
        Route.AHA_BUA,
        ("page.webmcp_tools",),
        reason="WebMCP tool details are read through the dedicated Native WebMCP discovery operation.",
        cases=("webmcp.tools",),
    ),
    "webmcp_call": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("webmcp",),
        reason="WebMCP tool execution is carried by Native page.act through the AhaBUA renderer agent.",
        cases=("action.webmcp_call",),
    ),
    "text": RouteSpec(
        Route.AHA_BUA,
        ("page.read",),
        reason="Element text reads consume Aha native refs through Native page.read.",
        cases=("read.text",),
    ),
    "get": RouteSpec(
        Route.AHA_BUA,
        ("page.read",),
        reason="Element attribute/property reads consume Aha native refs through Native page.read.",
        cases=("read.get_custom_attribute",),
    ),
    "http_get": RouteSpec(
        Route.SEED_DIRECT,
        reason="Pure HTTP read has no page state and matches the seed public contract.",
        cases=("local.http_get_gzip_headers",),
        seed_tool="http_get",
    ),
    "find": RouteSpec(
        Route.AHA_BUA,
        ("page.query",),
        reason="Find is an Aha ref producer and must return Native nN refs without injecting seed DOM markers.",
        cases=("query.find",),
    ),
    "get_page_text": RouteSpec(
        Route.AHA_BUA,
        ("page.read",),
        reason="Whole-page and scoped text reads use Native page.read without seed page state.",
        cases=(
            "read.document_text",
            "read.scoped_text",
            "read.scoped_text_all",
        ),
    ),
    "read_all": RouteSpec(
        Route.AHA_BUA,
        ("page.read",),
        reason="Structured repeated reads use Native page.read and keep the Aha field/ref contract.",
        cases=("read.all_selector", "read.all_relative_fields", "read.all_refs_branch"),
    ),
    "content_export": RouteSpec(
        Route.AHA_BUA,
        ("page.read",),
        reason="Content export uses Native document content extraction.",
        cases=("read.content_export",),
    ),
    "extract_content": RouteSpec(
        Route.AHA_BUA,
        ("page.read",),
        reason="Ordered content extraction consumes raw Native APC without Snapshot projection.",
        cases=("read.extract_content",),
    ),
    "console_messages": RouteSpec(
        Route.NATIVE_COMPAT,
        reason="Console events drain from the Aha Native GAC event buffer.",
        cases=("log.console_all", "log.console_filtered", "log.console_drain_consumes"),
    ),
    "network_requests": RouteSpec(
        Route.NATIVE_COMPAT,
        reason="Network events drain from the Aha Native GAC event buffer.",
        cases=("log.network_filtered", "log.network_drain_consumes"),
    ),
    "page_info": RouteSpec(
        Route.COMPOSITE,
        reason="Page info combines Aha page metadata, viewport size, and allowlisted CDP layout/history reads.",
        cases=("page.info", "return.page_info_shape"),
    ),
    "new_tab": RouteSpec(
        Route.NATIVE_COMPAT,
        reason="Tab creation uses Aha Native page management and returns the GAC page id.",
        cases=("tab.new_explicit_url", "tab.new_default_equivalent"),
    ),
    "list_tabs": RouteSpec(
        Route.NATIVE_COMPAT,
        reason="Tab listing uses Aha Native page management and exposes tolerant TabRecord aliases.",
        cases=("tab.list_include_chrome", "return.list_tabs_shape"),
    ),
    "switch_tab": RouteSpec(
        Route.NATIVE_COMPAT,
        reason="Tab switching selects the Aha page and returns a page-scoped GAC session id.",
        cases=("tab.switch_record", "tab.switch_index", "tab.restore_main"),
    ),
    "current_tab": RouteSpec(
        Route.NATIVE_COMPAT,
        reason="Current tab is read from Aha selected-page state.",
        cases=("tab.current", "return.current_tab_shape"),
    ),
    "close_tab": RouteSpec(
        Route.NATIVE_COMPAT,
        reason="Tab close uses Aha Native page management and restores selection from remaining Aha pages.",
        cases=("tab.close_explicit", "tab.close_current"),
    ),
    "ensure_real_tab": RouteSpec(
        Route.NATIVE_COMPAT,
        reason="Ensuring a tab uses Aha selected-page state without seed synchronization.",
        cases=("tab.ensure_real",),
    ),
    "resync": RouteSpec(
        Route.COMPOSITE,
        reason="Visible-tab recovery uses Aha tab metadata plus allowlisted Native CDP focus probes.",
        cases=("tab.resync",),
    ),
    "click": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("click",),
        reason="Ref/CSS clicks resolve through Aha locator state without seed refs.",
        cases=("action.click_left", "action.click_multiple", "return.action_none"),
    ),
    "type": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("type",),
        reason="Native Actor owns exact-target focus, editing and optional Enter, including rendered offscreen inputs.",
        cases=(
            "action.type_replace",
            "action.type_append",
            "action.type_submit",
            "return.type_none",
        ),
    ),
    "select": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("select",),
        reason="Select uses Native Actor option matching and Aha error shaping.",
        cases=("action.select", "action.select_missing_option"),
    ),
    "upload": RouteSpec(
        Route.COMPOSITE,
        ("page.getSelector",),
        reason="Ref-based file upload resolves the Aha ref through page.getSelector, then uses allowlisted Native CDP DOM.setFileInputFiles; upload_file remains the CSS selector compatibility entry.",
        cases=("action.upload_ref_contract", "action.upload_ref_path_object"),
    ),
    "hover": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("moveMouse",),
        reason="Hover uses Native Actor pointer movement against Aha refs.",
        cases=("action.hover",),
    ),
    "handle_dialog": RouteSpec(
        Route.COMPOSITE,
        reason="Dialog handling polls Aha Native dialog state and then uses allowlisted CDP.",
        cases=("dialog.handle_prompt",),
    ),
    "click_and_handle_dialog": RouteSpec(
        Route.COMPOSITE,
        reason="Dialog click scheduling converts Aha refs to CSS through page.getSelector, then handles the dialog with Native CDP.",
        cases=("dialog.click_and_confirm_reject",),
    ),
    "click_xy": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("click",),
        reason="Viewport coordinate clicks use Native coordinate conversion and Actor.",
        cases=("coordinate.click", "coordinate.invalid_space"),
    ),
    "left_double": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("click",),
        reason="Double click is one Native point click action with clickCount=2.",
        cases=("coordinate.double_click",),
    ),
    "right_single": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("click",),
        reason="Right click is one Native point click action with button=right.",
        cases=("coordinate.right_click",),
    ),
    "drag": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("drag",),
        reason="Drag is a single Native Actor action; steps are compatibility no-op.",
        cases=("coordinate.drag",),
    ),
    "scroll": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("scroll",),
        reason="Main-page, viewport-coordinate and ref/CSS targets use Aha page.act scroll.",
        cases=(
            "scroll.down",
            "scroll.node_ancestor",
            "scroll.locator_ancestor",
            "scroll.invalid_direction",
        ),
    ),
    "scroll_into_view": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("scrollTo",),
        reason="Known Native ref/CSS targets use Aha page.act scrollTo to enter the viewport.",
        cases=("scroll.into_view",),
    ),
    "navigate": RouteSpec(
        Route.COMPOSITE,
        ("page.act",),
        ("navigate", "back", "forward", "reload"),
        reason="Cold URL navigation uses Aha Native page creation; existing pages use page.act.",
        cases=(
            "navigation.absolute_url",
            "navigation.back",
            "navigation.forward",
            "navigation.reload",
        ),
    ),
    "wait": RouteSpec(
        Route.SEED_DIRECT,
        reason="Local sleep validation and return value match the seed public contract.",
        cases=("wait.zero", "wait.invalid_seconds"),
        seed_tool="wait",
    ),
    "wait_for_load": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("wait",),
        reason="Document-load readiness is determined by Aha Native page.act settlement.",
        cases=("wait.load_with_since", "return.wait_for_load_bool"),
    ),
    "wait_for_element": RouteSpec(
        Route.COMPOSITE,
        ("page.queryCss",),
        reason="Element wait polls Aha Native CSS query results and exposes only the public bool contract.",
        cases=("wait.element_visible", "wait.element_timeout", "return.wait_for_element_bool"),
    ),
    "wait_for_network_idle": RouteSpec(
        Route.NATIVE_COMPAT,
        reason="Network-idle wait is computed from Aha Native network event drains.",
        cases=("wait.network_idle",),
    ),
    "hotkey": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("pressKey",),
        reason="Native page.act dispatches a complete key sequence with modifiers.",
        cases=("keyboard.hotkey", "keyboard.hotkey_modifier_only"),
    ),
    "press_key": RouteSpec(
        Route.AHA_BUA,
        ("page.act",),
        ("pressKey",),
        reason="Native page.act owns keydown, character/default action and keyup.",
        cases=("keyboard.press_key", "keyboard.press_invalid_key"),
    ),
    "screenshot": RouteSpec(
        Route.COMPOSITE,
        ("page.getSelector",),
        reason="Viewport screenshots use Native capture_screenshot; element screenshots first resolve Aha refs through page.getSelector and then pass a CDP clip to the same native bridge.",
        cases=(
            "capture.viewport",
            "capture.viewport_no_show",
            "capture.element",
            "capture.element_missing_target",
        ),
    ),
    "download": RouteSpec(
        Route.COMPOSITE,
        reason="Downloads use Aha Native direct download or Aha page.read href/src extraction.",
        cases=("download.direct_url", "download.ref_url", "download.invalid_target"),
    ),
    "wait_for_download": RouteSpec(
        Route.SEED_DIRECT,
        reason="Browser-triggered download completion is a stateless seed helper and does not need Aha page synchronization.",
        cases=("download.browser_event", "download.wait_timeout"),
        seed_tool="wait_for_download",
    ),
    "js": RouteSpec(
        Route.AHA_BUA,
        ("page.evaluate",),
        reason="JavaScript evaluation runs through Aha Native page.evaluate.",
        cases=(
            "evaluate.page_state",
            "evaluate.shared_reference_result",
            "evaluate.missing_target",
        ),
    ),
    "cdp": RouteSpec(
        Route.CDP_COMPAT,
        reason="Raw CDP handles Aha page Target commands locally and routes other methods through Native send_cdp_command.",
        cases=("cdp.main_session", "return.cdp_result_shape"),
    ),
    "iframe_target": RouteSpec(
        Route.CDP_COMPAT,
        reason="Iframe target lookup uses Target.getTargets on the selected Aha page so it stays aligned with Aha page ownership.",
        cases=("iframe.attach", "iframe.missing_target_none", "return.iframe_target_shape"),
    ),
    "upload_file": RouteSpec(
        Route.CDP_COMPAT,
        reason="CSS file upload uses allowlisted Native CDP DOM.setFileInputFiles on the selected Aha page.",
        cases=("compat.upload_file_list", "compat.upload_file_invalid_path"),
    ),
    "install_executor": RouteSpec(
        Route.LOCAL,
        reason="Executor installation mutates only in-process Python state.",
        cases=("local.install_executor",),
    ),
    "make_ref": RouteSpec(
        Route.LOCAL,
        reason="Ref construction is a pure Aha helper with native-node-shape checks.",
        cases=("return.make_ref_shape", "locator.make_ref"),
    ),
}

PUBLIC_TOOLS = frozenset(ROUTES)
if len(PUBLIC_TOOLS) != 50:
    raise RuntimeError(f"AhaBUA route registry must contain 50 tools, got {len(PUBLIC_TOOLS)}")

for _name, _spec in ROUTES.items():
    if not _spec.reason.strip():
        raise RuntimeError(f"AhaBUA route {_name!r} is missing a reason")
    if not _spec.cases:
        raise RuntimeError(f"AhaBUA route {_name!r} is missing acceptance cases")
    if _spec.route == Route.SEED_DIRECT and not _spec.seed_tool:
        raise RuntimeError(f"AhaBUA seed direct route {_name!r} is missing seed_tool")
    if _spec.route != Route.SEED_DIRECT and _spec.seed_tool:
        raise RuntimeError(f"AhaBUA non-seed route {_name!r} must not set seed_tool")

__all__ = ["PUBLIC_TOOLS", "ROUTES", "Route", "RouteSpec"]
