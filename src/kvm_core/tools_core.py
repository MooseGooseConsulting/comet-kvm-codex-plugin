from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import Image

from src.kvm_core.runtime import DEFAULT_TARGET, get_kvm_runtime, resolve_default_target
from src.kvm_core.server import mcp
from src.kvm_core.ocr import validate_psm
from src.kvm_core.terminal import run_posix_terminal_command

LOG = logging.getLogger("kvm_core.tools")

ATX_DISABLED_WARNING = (
    "ATX subsystem disabled (enabled=false): the power field does not reflect "
    "the real machine state; classify from the console instead."
)
MAX_TERMINAL_TIMEOUT_SECONDS = 300
MIN_TERMINAL_POLL_INTERVAL_SECONDS = 0.1


async def _managed_client(target: str | None = None):
    """Return a live client for ``target``, connecting the default on demand."""
    return await get_kvm_runtime().ensure_connected(target)


@asynccontextmanager
async def _operation_fence(runtime=None) -> AsyncIterator[None]:
    """Serialize mutating device work against disconnect/reconnect.

    Lock ordering: this fence is always the OUTERMOST lock — acquire it before
    calling ``ensure_connected``/``connect``/``disconnect`` (which take the
    runtime connection lock internally). ``ensure_connected`` never takes this
    fence, so an in-flight operation holding it cannot deadlock an ensure.
    Duck-typed runtimes without the fence degrade to a no-op.
    """
    r = runtime if runtime is not None else get_kvm_runtime()
    lock = getattr(r, "_operation_lock", None)
    if lock is None:
        yield
        return
    async with lock:
        yield


def _client_is_live(client) -> bool:
    if client is None:
        return False
    probe = getattr(client, "is_connected", None)
    if probe is None:
        return True
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - a client that cannot answer is not live
        return False


def _live_client(runtime, target: str | None = None):
    """The already-connected client for ``target``, or None. Never connects."""
    targets = getattr(runtime, "targets", None) or {}
    selected = getattr(runtime, "selected_target", None) or DEFAULT_TARGET
    target_id = target or selected
    entry = targets.get(target_id)
    client = getattr(entry, "client", None) if entry is not None else None
    if client is None and target_id == selected:
        # Sidecar rigs may install a client directly on the runtime.
        client = getattr(runtime, "client", None)
    return client if _client_is_live(client) else None


def _normalize_host(value: str | None) -> str:
    text = (value or "").strip().lower()
    for scheme in ("https://", "http://"):
        if text.startswith(scheme):
            text = text[len(scheme):]
            break
    return text.rstrip("/")


def _hosts_match(client, host: str) -> bool:
    wanted = _normalize_host(host)
    if not wanted:
        return False
    candidates = {
        _normalize_host(getattr(client, "host", None)),
        _normalize_host(getattr(client, "base_url", None)),
    }
    return wanted in candidates


def _usernames_match(client, username: str) -> bool:
    """Whether ``client`` was authenticated as the requested username."""
    return getattr(client, "username", None) == username


def _capture_state(client) -> dict:
    """Capture-path diagnostics (503 retry health) for kvm_status."""
    if client is None:
        return {"ok": None, "error": None, "at": None, "retries": 0}
    return {
        "ok": getattr(client, "last_capture_ok", None),
        "error": getattr(client, "last_capture_error", None),
        "at": getattr(client, "last_capture_at", None),
        "retries": getattr(client, "capture_retry_count", 0),
    }


def _safe_screenshot_path(requested_path: str) -> Path:
    requested = Path(requested_path)
    if requested.is_absolute() or ".." in requested.parts:
        raise ValueError("path must be a filename or relative path under the screenshot cache directory")
    root = Path(get_kvm_runtime().capture_mgr.cache_dir).resolve()
    destination = (root / requested).resolve()
    if root != destination and root not in destination.parents:
        raise ValueError("path escapes the screenshot cache directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def resolve_screenshot_ref(screenshot_ref: str) -> Path:
    """Resolve an opaque screenshot id/name strictly under the screenshot cache."""
    requested = Path(screenshot_ref)
    if requested.is_absolute() or ".." in requested.parts:
        raise ValueError("invalid screenshot reference")
    root = Path(get_kvm_runtime().capture_mgr.cache_dir).resolve()
    candidates = [
        (root / requested).resolve(),
        (root / f"{screenshot_ref}.jpg").resolve(),
        (root / requested.name).resolve(),
    ]
    for candidate in candidates:
        if root != candidate and root not in candidate.parents:
            continue
        if candidate.is_file() and candidate.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            return candidate
    raise FileNotFoundError(f"Screenshot ref not found in cache: {screenshot_ref}")


@mcp.tool(name="kvm_connect", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def kvm_connect(
    host: str | None = None,
    password: str | None = None,
    username: str | None = None,
    target: str = DEFAULT_TARGET,
    force_reconnect: bool = False,
) -> dict:
    """OPTIONAL override for the managed Comet session.

    Device tools connect on demand, so this tool is only needed for a
    non-default host, explicit credentials, a named multi-target session, or
    force_reconnect=True. Omitted host/username resolve to the managed default
    (COMET_HOST/COMET_USERNAME, else 192.168.0.126/admin). When password is
    omitted, GLCOMET_ADMIN_PASSWORD is read from the Doppler CLI using
    doppler.yaml; the process environment is never read. A live session that
    already matches the requested host is reused (reused=True) without touching
    Doppler.
    """
    r = get_kvm_runtime()
    default_host, default_username = resolve_default_target()
    resolved_host = (host or default_host).strip()
    resolved_username = (username or default_username).strip()

    async with _operation_fence(r):
        # Keep the preflight and connect in the same operation fence. The
        # runtime repeats this identity check under its connection lock, which
        # also protects direct runtime callers.
        if not force_reconnect:
            existing = _live_client(r, target)
            if (
                existing is not None
                and _hosts_match(existing, resolved_host)
                and _usernames_match(existing, resolved_username)
            ):
                return {
                    "connected": True,
                    "host": getattr(existing, "base_url", resolved_host),
                    "target": target,
                    "capabilities": getattr(existing, "capabilities", {}),
                    "reused": True,
                    "message": "reused live session",
                }

        if password is None:
            from src.kvm_core.doppler_credentials import DopplerAuthError, resolve_comet_password

            try:
                password = await asyncio.to_thread(resolve_comet_password, require=True)
            except DopplerAuthError as exc:
                raise ValueError(str(exc)) from exc
        if not password:
            raise ValueError(
                "No Comet password available. Pass password explicitly or ensure "
                "Doppler CLI is logged in and GLCOMET_ADMIN_PASSWORD exists in the configured project."
            )

        connect_kwargs = {
            "host": resolved_host,
            "username": resolved_username,
            "password": password,
            "target": target,
        }
        # Keep lightweight legacy runtime doubles compatible when no forced
        # replacement is requested; the concrete runtime defaults this flag.
        if force_reconnect:
            connect_kwargs["force_reconnect"] = True
        connect_result = await r.connect(**connect_kwargs)
        if isinstance(connect_result, tuple):
            ok, reused = connect_result
        else:
            # Permit lightweight legacy runtimes in focused tool tests.
            ok, reused = bool(connect_result), False
        client = _live_client(r, target) or getattr(r, "client", None)
    return {
        "connected": ok,
        "host": getattr(client, "base_url", resolved_host),
        "target": target,
        "capabilities": getattr(client, "capabilities", {}),
        "reused": reused,
        "message": "reused live session" if reused else "ok",
    }


@mcp.tool(name="kvm_disconnect", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def kvm_disconnect(target: str | None = None) -> dict:
    """Close WebSocket/HTTP session for one target, or all targets when omitted.

    Not required cleanup: the next device operation reconnects the default.
    """
    r = get_kvm_runtime()
    async with _operation_fence(r):
        await r.disconnect(target)
    return {"connected": False, "target": target, "targets": r.list_targets(), "message": "disconnected"}


@mcp.tool(name="kvm_send_text", annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_send_text(text: str, wpm: int = 200) -> dict:
    """Type a string on the remote machine using the bug-fix atomic press patterns."""
    async with _operation_fence():
        client = await _managed_client()
        return await client.send_text(text, wpm)


@mcp.tool(name="kvm_terminal_run", annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_terminal_run(
    command: str,
    timeout_seconds: float = 30,
    poll_interval_seconds: float = 0.5,
) -> dict:
    """Run one POSIX command through the visible console with bounded OCR observation.

    The command is wrapped in an isolated ``sh -c`` invocation, so shell state
    does not persist. Its transcript is best-effort visible-console evidence,
    not exact stdout/stderr. A timeout never sends Ctrl+C; it only releases HID
    state and reports that command completion was not observed.
    """
    if not command.strip():
        raise ValueError("command must not be empty")
    if "\n" in command or "\r" in command:
        raise ValueError("command must be a single line; newline input can submit Enter before OCR confirmation")
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be non-negative")
    if timeout_seconds > MAX_TERMINAL_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be <= {MAX_TERMINAL_TIMEOUT_SECONDS}")
    if poll_interval_seconds < 0:
        raise ValueError("poll_interval_seconds must be non-negative")
    if poll_interval_seconds < MIN_TERMINAL_POLL_INTERVAL_SECONDS:
        raise ValueError(
            f"poll_interval_seconds must be >= {MIN_TERMINAL_POLL_INTERVAL_SECONDS}"
        )
    _require_tesseract()
    runtime = get_kvm_runtime()
    async with _operation_fence(runtime):
        client = await runtime.ensure_connected()
        return await run_posix_terminal_command(
            client,
            runtime.ocr_mgr,
            command,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )


@mcp.tool(name="kvm_send_keys", annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_send_keys(combo: str) -> dict:
    """Send a single key chord, e.g. "Ctrl+Alt+Delete", "Escape", "ArrowDown"."""
    async with _operation_fence():
        client = await _managed_client()
        return await client.send_combo(combo)


@mcp.tool(name="kvm_hold_key", annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_hold_key(key: str, duration_ms: int) -> dict:
    """Press and hold a single key for an explicit duration, then release."""
    async with _operation_fence():
        client = await _managed_client()
        return await client.hold_key(key, duration_ms)


@mcp.tool(name="kvm_release_all", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def kvm_release_all() -> dict:
    """Force-release every key currently held. Recovery tool.

    Never connects: with no live session there is nothing held to release.
    """
    r = get_kvm_runtime()
    async with _operation_fence(r):
        client = _live_client(r)
        if client is None:
            return {
                "released": [],
                "connected": False,
                "message": "no session; nothing to release",
            }
        return await client.release_all()


@mcp.tool(name="kvm_mouse_move", annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_move(x: int, y: int) -> dict:
    """Move cursor to absolute coordinates in PiKVM-normalized space."""
    x_pct = (x + 32768) / 65535.0 * 100.0
    y_pct = (y + 32768) / 65535.0 * 100.0
    async with _operation_fence():
        client = await _managed_client()
        return await client.mouse_move_pct(x_pct, y_pct)


@mcp.tool(name="kvm_mouse_move_pct", annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_move_pct(x_pct: float, y_pct: float) -> dict:
    """Move cursor to screen percentage coordinates: (0,0)=top-left, (100,100)=bottom-right."""
    async with _operation_fence():
        client = await _managed_client()
        return await client.mouse_move_pct(x_pct, y_pct)


@mcp.tool(name="kvm_mouse_click", annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_click(button: str = "left", count: int = 1) -> dict:
    """Clicks named button count times at the current cursor position."""
    async with _operation_fence():
        client = await _managed_client()
        return await client.mouse_click(button, count)


@mcp.tool(name="kvm_mouse_scroll", annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_scroll(dx: int = 0, dy: int = 0) -> dict:
    """Scroll mouse wheel delta."""
    async with _operation_fence():
        client = await _managed_client()
        return await client.mouse_scroll(dx, dy)


@mcp.tool(name="kvm_screenshot", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_screenshot(preview: bool = True, max_width: int = 1024, quality: int = 60) -> Image:
    """Capture snapshot frame."""
    client = await _managed_client()
    data = await client.get_screenshot(preview, max_width, quality)
    return Image(data=data, format="jpeg")


@mcp.tool(name="kvm_screenshot_to_file", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_screenshot_to_file(path: str, preview: bool = False, max_width: int = 1920, quality: int = 80) -> dict:
    """Capture snapshot and store under the screenshot cache directory."""
    client = await _managed_client()
    data = await client.get_screenshot(preview, max_width, quality)
    destination = _safe_screenshot_path(path)
    with open(destination, "wb") as f:
        f.write(data)
    return {"path": str(destination), "bytes": len(data), "mime_type": "image/jpeg"}


@mcp.tool(name="kvm_ocr_screenshot", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_ocr_screenshot(search_text: str = "", preview: bool = False, psm: int = 3) -> dict:
    """Capture a screenshot and return ordered screen text plus word coordinates.

    Use psm=6 for a full-screen terminal or other single text block.
    """
    _require_tesseract()
    client = await _managed_client()
    r = get_kvm_runtime()
    img_bytes = await client.get_screenshot(preview=preview)
    return await asyncio.to_thread(r.ocr_mgr.run_ocr, img_bytes, search_text, psm)


def _require_tesseract() -> None:
    """OCR tools check host Tesseract before connecting or capturing."""
    status = get_kvm_runtime().ocr_mgr.get_status()
    if not status.get("available"):
        raise RuntimeError(
            "Tesseract OCR is not available on the MCP host; no connection or "
            "capture was attempted. Install Tesseract or set TESSERACT_PATH "
            "(see kvm_ocr_status)."
        )


def _ocr_crop(left: int, top: int, right: int, bottom: int) -> tuple[int, int, int, int] | None:
    values = (left, top, right, bottom)
    if all(value < 0 for value in values):
        return None
    if right >= 0 and left >= right:
        raise ValueError("right must be greater than left")
    if bottom >= 0 and top >= bottom:
        raise ValueError("bottom must be greater than top")
    return values


@mcp.tool(name="kvm_ocr_status", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_ocr_status() -> dict:
    """Report OCR backends that are callable by this MCP server."""
    r = get_kvm_runtime()
    host = r.ocr_mgr.get_status()
    return {
        "host": host,
        "recommended_text_engine": "host-tesseract" if host.get("available") else "unavailable",
        "product_ui_ocr": {
            "engine": "tesseract.js",
            "execution": "controlling-browser",
            "available_to_mcp": False,
        },
    }


@mcp.tool(name="kvm_ocr_text", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_ocr_text(
    psm: int = 6,
    languages: str = "",
    left: int = -1,
    top: int = -1,
    right: int = -1,
    bottom: int = -1,
) -> dict:
    """Capture the visible screen and return text through host Tesseract.

    The GL.iNet 1.9 Text Recognition feature is browser-side Tesseract.js and is
    not a device API. This MCP path captures the frame directly. Crop coordinates
    are pixels; leave all four at -1 for the full frame.
    """
    _require_tesseract()
    crop = _ocr_crop(left, top, right, bottom)
    validate_psm(psm)
    client = await _managed_client()
    r = get_kvm_runtime()
    image_bytes = await client.get_screenshot(preview=False)
    host = await asyncio.to_thread(r.ocr_mgr.run_text_ocr, image_bytes, psm, languages, crop)
    if "error" in host:
        raise RuntimeError(host["error"])
    host.update({
        "engine": "host-tesseract",
        "crop": list(crop) if crop else None,
    })
    return host


@mcp.tool(name="kvm_ocr_click", annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_ocr_click(text: str, button: str = "left", count: int = 1, search_area: str = "") -> dict:
    """Find text coordinates on screen and mouse click."""
    _require_tesseract()
    client = await _managed_client()
    r = get_kvm_runtime()
    img_bytes = await client.get_screenshot(preview=False)
    ocr = await asyncio.to_thread(r.ocr_mgr.run_ocr, img_bytes, text)
    if "error" in ocr:
        raise RuntimeError(ocr["error"])
    if not ocr["elements"]:
        return {"found": False, "text": text, "message": "No matches."}

    elements = ocr["elements"]
    if search_area:
        area_filters = {
            "top-left":     lambda e: e["x_pct"] < 50 and e["y_pct"] < 50,
            "top-right":    lambda e: e["x_pct"] >= 50 and e["y_pct"] < 50,
            "bottom-left":  lambda e: e["x_pct"] < 50 and e["y_pct"] >= 50,
            "bottom-right": lambda e: e["x_pct"] >= 50 and e["y_pct"] >= 50,
        }
        f = area_filters.get(search_area)
        if f:
            filtered = [e for e in elements if f(e)]
            if filtered:
                elements = filtered

    elements.sort(key=lambda e: -e["confidence"])
    best = elements[0]
    # Capture/OCR above stays outside the fence; only the mutation is serialized.
    async with _operation_fence(r):
        await client.mouse_move_pct(best["x_pct"], best["y_pct"])
        await client.mouse_click(button, count)
    return {
        "found": True,
        "text": best["text"],
        "confidence": best["confidence"],
        "x_pct": best["x_pct"],
        "y_pct": best["y_pct"],
        "clicked": True
    }


@mcp.tool(name="kvm_status", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_status(target: str | None = None) -> dict:
    """Report current connection status. Observes only — never connects."""
    r = get_kvm_runtime()
    default_host, default_username = resolve_default_target()
    client = _live_client(r, target)
    managed = {
        "default_host": default_host,
        "default_username": default_username,
        "default_target": DEFAULT_TARGET,
        "auto_connect": True,
        "capture": _capture_state(client),
        "selected_target": r.selected_target,
        "targets": r.list_targets(),
    }
    if client is None:
        return {
            "connected": False,
            "host": "",
            "held_keys": [],
            "ws_open": False,
            **managed,
        }
    return {
        "connected": True,
        "host": client.base_url,
        "target": getattr(client, "target_id", target or r.selected_target),
        "held_keys": list(getattr(client, "held", {}).keys()),
        "ws_open": client.is_connected(),
        "last_server_event_at": getattr(client, "last_server_event_at", None),
        "last_pong_at": getattr(client, "last_pong_at", None),
        "server_state_keys": sorted(getattr(client, "server_state", {}).keys()),
        "capabilities": getattr(client, "capabilities", {}),
        **managed,
    }


@mcp.tool(name="kvm_select_target", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def kvm_select_target(target: str) -> dict:
    """Select the Comet target for subsequent tool calls.

    Selecting 'default' is always allowed — it is a preference for the managed
    session, which connects on demand. Named targets must already be connected.
    """
    r = get_kvm_runtime()
    if target == DEFAULT_TARGET and target not in (getattr(r, "targets", None) or {}):
        r.selected_target = DEFAULT_TARGET
        selected = DEFAULT_TARGET
    else:
        selected = r.select_target(target)
    return {"selected_target": selected, "targets": r.list_targets()}


@mcp.tool(name="comet_power_state", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def comet_power_state(target: str | None = None) -> dict:
    """Read ATX power/LED state via GET /api/atx.

    When enabled=false the ATX subsystem is not wired up: the power field does
    not reflect the real machine state — classify from the console instead.
    """
    client = await _managed_client(target)
    state = await client.atx_state()
    if isinstance(state, dict) and state.get("enabled") is False:
        state = {**state, "warning": ATX_DISABLED_WARNING}
    return state


@mcp.tool(name="comet_atx_power", annotations={"readOnlyHint": False, "destructiveHint": True})
async def comet_atx_power(action: str, wait: bool = True, target: str | None = None) -> dict:
    """ATX power action via query params. Actions: on, off, off_hard, reset_hard (aliases: reset, force_off)."""
    async with _operation_fence():
        client = await _managed_client(target)
        return await client.atx_power(action, wait=wait)


@mcp.tool(name="comet_atx_click", annotations={"readOnlyHint": False, "destructiveHint": True})
async def comet_atx_click(button: str, wait: bool = True, target: str | None = None) -> dict:
    """Momentary ATX button press: power, power_long, or reset."""
    async with _operation_fence():
        client = await _managed_client(target)
        return await client.atx_click(button, wait=wait)


@mcp.tool(name="comet_sysinfo", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def comet_sysinfo(target: str | None = None) -> dict:
    """Retrieve device metadata: model, firmware version, capabilities."""
    client = await _managed_client(target)
    return await client.get_sysinfo()


@mcp.tool(name="comet_capabilities", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def comet_capabilities(refresh: bool = False, target: str | None = None) -> dict:
    """Return connect-time capability profile (model/firmware/features)."""
    client = await _managed_client(target)
    if refresh or not client.capabilities:
        return await client.discover_capabilities()
    return client.capabilities


