# Implementation Decisions

> **Repo:** `Coldaine/comet-kvm-codex-plugin` (fork of `kennypeh85/glkvm-mcp`)
> **Authority:** #2 after `docs/NORTH_STAR.md` (ladder in `AGENTS.md`). These are decisions about *how* we build the thing, not *what* the thing is (that's NORTH_STAR.md). `AGENTS.md` is a thin router into these docs. See `docs/architecture.md` and `docs/kvm-core.md` for shape and pipeline detail.

## D1 — Screenshot retention: TTL, not permanent

Runtime screenshots are persisted temporarily for retry, debugging, and map-building, then automatically purged after approximately 30 days. They are never committed to Git. The retention is a cleanup policy, not a git policy — the TTL runs against whatever runtime data directory the installed plugin uses (host-side or on-Comet).


## D3 — Cartography lives under the BIOS triage skill

BIOS cartography is a specialized subset of the `comet-bios-triage` skill, not a sibling skill. It is documented under `docs/architecture.md` and `docs/kvm-core.md`. It is a firmware-only lane, not a prerequisite for ordinary Comet operation or a gate on the universal KVM core.

## D4 — Map store runtime location: on-Comet preferred, pending verification

When the specialist lane stores BIOS maps, they should persist on the Comet device itself, co-located with the hardware they describe. This decision does not create a storage requirement for ordinary Comet operation.

**Probe result (2026-07-27):** The deployed Comet Pro (GL-RM10) reports a
writable `GLKVM` media partition through its authenticated `GET /api/msd`
endpoint: 28,797,599,744 bytes total and 28,797,403,136 bytes free. This
establishes device-reported media capacity, not a map write/read cycle or a
specific map path. Verify that path before relying on it; otherwise use the
host-side plugin data directory for that specialist run.

**Fallback:** If on-Comet storage proves impractical, maps persist in the host-side plugin data directory. The VLM interpretation layer always runs on the host (the Comet has no GPU) regardless of where maps are stored.

## D5 — Fuzzy matching against similar boards: not a goal

Fuzzy matching is not a core requirement. The driver agent can look at stored maps and then decide if a map is similar enough to be imported and reused. We prioritize agent-led comparison over automated heuristic matching.

## D6 — glkvm_mcp.py is a composition entry point

`glkvm_mcp.py` remains the PEP 723 executable entry point, but it is no longer the implementation container. Universal transport, session, OCR, and tool code lives under `src/kvm_core/`; BIOS-specific state and orchestration lives under `src/bios_sidecar/`. Both layers register against the shared `FastMCP("comet-kvm")` instance. Preserve this dependency direction: the sidecar may depend on the KVM core, while the KVM core must not depend on BIOS semantics.

## D7 — State engine deployment: on-demand internal tracking

The stateful screen-level position tracker runs inside the MCP server process, keeping track of which graph node the session is currently on. It does not run an always-on screenshot/OCR loop. The sidecar updates state on demand when the Driver Agent calls tools such as `bios_observe_state`, `bios_navigate_to`, or `bios_apply_setting_change`. It matches screens locally using perceptual hashes and OCR fingerprints (`kvm_match_screen`), calling the VLM tool (`kvm_vlm_parse`) only when grounding is needed.

## D8 — Two granularity levels: workflow phases vs screen position

**Status: open / not product architecture.** Do not treat the workflow-phase ledger as settled product design until re-evaluated.

The project may operate at two distinct granularity levels that complement, not replace, each other:

- **Workflow level** (`stateful-control-model.md`): phases like `planned → preflight → bios-entry → bios-edit → save-confirm → windows-boot → hwinfo-log → analysis → done`. Agent-maintained, persisted in the run ledger. Asks "are we in the edit phase?"
- **Screen level** (state engine): which BIOS menu node are we on right now, matched against a stored map. Maintained on demand and ephemeral per session. Asks "are we on the Overclocking submenu row 3, and did that Enter press land where the map predicted?"

## D9 — Output format: Semantic Capability Index + screen-node graph

The crawler produces two views of the same crawl data:

- **Semantic Capability Index** (for the driver agent): a JSON file keyed by setting name, containing the navigation path, UI type, available options, and interaction keys. The driver reads this to navigate deterministically without calling the VLM.
- **Screen-node graph** (for the state engine): a network of screen nodes keyed by perceptual hash + OCR fingerprint, with edges labeled by the keystroke that transitions between them. The state engine matches live screenshots against these nodes for transition validation.

The crawler produces the graph (raw crawl data). A post-processing step derives the index from the graph. Both are persisted. See `docs/architecture.md#state-and-cartography` for the current framing.

## D10 — REMOVED: VLM framework choice as product architecture

Removed as a durable decision. VLM implementation details may still exist inside the BIOS sidecar, but they are sidecar-internal and do not define the KVM MCP core product.

## D11 — REMOVED: Approval/policy-gated authority model

Removed as product architecture. Approval tokens, `bios_grant_human_approval`, and policy-gated authority are cut from the docs. Tool annotations, path safety, stale-key watchdog behavior, destructive labeling, and visual verification remain. `bios_save_and_reboot` checking that a save dialog is visible before pressing Enter is screen-state verification, not approval-token policy.

## D-K1 — KVM tool surface plus deprecated aliases

The KVM core exposes these unique driver-facing tool families (exact schemas in `docs/reference/comet-api.md` and the live MCP tool list):

- **Session / HID / capture:** `kvm_connect`, `kvm_disconnect`, `kvm_status`, `kvm_select_target`, `kvm_send_text`, `kvm_terminal_run`, `kvm_send_keys`, `kvm_hold_key`, `kvm_release_all`, `kvm_mouse_move`, `kvm_mouse_move_pct`, `kvm_mouse_click`, `kvm_mouse_scroll`, `kvm_screenshot`, `kvm_screenshot_to_file`, `kvm_ocr_status`, `kvm_ocr_text`, `kvm_ocr_screenshot`, `kvm_ocr_click`
- **Power / discovery:** `comet_power_state`, `comet_atx_power`, `comet_atx_click`, `comet_sysinfo`, `comet_capabilities`
- **Virtual media:** `comet_media_state`, `comet_media_upload`, `comet_media_fetch`, `comet_media_mount`, `comet_media_unmount`, `comet_media_remove`, `comet_media_reset` (legacy alias `comet_msd_upload` remains)
- **WOL / stream / recorder / admin:** `comet_wol_list`, `comet_wol_scan`, `comet_wol_wake`, `comet_streamer_state`, `comet_streamer_set_params`, `comet_recorder_state`, `comet_recorder_start`, `comet_recorder_stop`, `comet_metrics`, `comet_tailscale_status`, `comet_redfish_power`

The 10 `comet_raw_*` aliases duplicate `kvm_*` tools and are deprecated in documentation only. Do not remove them in this docs pass; `tests/test_smoke.py` currently expects `comet_raw_send_keys` and `comet_raw_screenshot`.

## D-K2 — ATX power control is wrapped

ATX power control is available through `comet_atx_power` and `comet_atx_click`. The tools still require the ATX add-on board to be physically installed and wired to the target.

## D-K3 — Upstream sync is fetch-only and selective

Keep `upstream` pointing at `kennypeh85/glkvm-mcp` as a fetch-only remote. Selectively cherry-pick upstream bug fixes or API improvements when relevant. This repo is not a mirror, not an upstream PR staging area, and does not track upstream releases.

## D-K4 — Watchdog and pinger are core firmware workarounds

The stale key watchdog and WebSocket pinger are required KVM-core reliability mechanisms. The watchdog runs every 40ms and force-releases keys held longer than 250ms. The pinger sends WebSocket pings every 1s to prevent kvmd timeout. These are firmware/API workarounds, not policy or approval mechanisms.

## D-K5 — Security model is LAN-first with per-session password

The Comet is operated on a trusted LAN or through Tailscale/VPN. TLS verification is disabled because the device uses a self-signed certificate. The password is supplied per session via `kvm_connect` or fetched from the Doppler CLI (`GLCOMET_ADMIN_PASSWORD` in `homelab`/`dev` per `doppler.yaml`); no Comet password is committed to the repository or read from process environment variables.

## D-K6 — Locked Python 3.13 deployment

`glkvm_mcp.py` remains the composition entry point. Production launchers must use `uv run --locked --python 3.13 python ./glkvm_mcp.py` so they honor the reviewed lockfile; `uv run --script` intentionally is not supported because it ignores that lockfile. The PEP 723 metadata is retained only for metadata-aware tooling and matches the runtime dependencies.

## D-K7 — Terminal output uses explicit, bounded transports

MCP tool return values are the primary agent data path. Runtime logs and MCP progress/resource notifications are diagnostics or optional mirrors; they are not command-output transport.

For a pixel-only KVM console, the primitive flow remains `kvm_send_text` → `kvm_send_keys("Enter")` → `kvm_ocr_text()`, whose host-Tesseract ordered text is returned directly to the calling agent. `kvm_terminal_run` is the bounded composite for one **POSIX** command: it types an isolated `sh -c` wrapper, OCR-confirms the shell plus all unique per-run marker evidence before it submits Enter, polls only for that call, accumulates literal-overlap OCR deltas, and returns the bounded best-effort transcript as evidence. It returns an exit code only when its exact end marker is visibly observed. Timeouts release HID state but never send Ctrl+C or claim that the remote command stopped. An always-on rolling OCR buffer is **Deferred** until recorded workloads prove that the bounded call is insufficient.

Exact stdout/stderr/exit status requires a real byte-stream transport, not HDMI OCR. A separate AsyncSSH-backed target-shell component is a **Candidate** for machines that are directly reachable on the network. It must keep target credentials separate from the Comet admin password, verify known hosts, and use an allowlist. It does not belong inside `kvm_core` and it cannot replace KVM access for BIOS, recovery, or network-down states.

`kvm_ocr_text` captures the frame and uses host Pillow plus pytesseract. The
full host-only OCR decision, rejected native/device paths, and live-probe
findings are recorded in [D-K9](#d-k9--mcp-ocr-is-host-only-native-first-removed).

## D-K8 — Prefer small standard adapters over dependency expansion

Keep Pillow for image decoding, pytesseract for Tesseract integration, and the Python standard library for rotating logs, bounded queues, subprocess offloading, and initial text overlap. Do not add `screen-ocr`, pandas, OpenCV, RapidFuzz, Loguru, structlog, or OpenTelemetry without fixture- or profiling-backed need. If direct SSH is implemented, use AsyncSSH rather than writing an SSH protocol/session layer or wrapping synchronous Paramiko.

**MCP Python SDK:** Pin `mcp[cli]>=1.28,<2` and use `FastMCP` from the 1.x line. "Stay on 1.x" means defer a deliberate **`mcp` 2.x / `MCPServer`** migration — not avoid the high-level server API (the project already uses FastMCP). MCP v2 is a **candidate**, not rejected: elicitation for the Comet admin password (plugin distribution), progress notifications for long crawl/observe calls, and resource subscriptions for `bios://*` resources. See [`docs/plans/02-mcp-v2-migration-evaluation.md`](plans/02-mcp-v2-migration-evaluation.md).

## D-K9 — MCP OCR is host-only; native-first removed

**Settled path:** `kvm_ocr_*` captures a JPEG from `GET /api/streamer/snapshot` and runs host Tesseract (Pillow + pytesseract) in the MCP process. Pipeline detail: [`docs/kvm-core.md`](kvm-core.md) §5 / §9 and [`docs/reference/comet-api.md`](reference/comet-api.md)#ocr-integration.

The inherited `/api/streamer/ocr` route is discovery-only. GL.iNet's Text
Recognition feature runs browser-side Tesseract.js/WASM and is not a device API
for this process. Installing host Tesseract is therefore a normal MCP-host
dependency, not an open device-OCR project.

Evidence and the correction history are retained as a dated, non-authoritative
record in [`docs/research/glkvm-client-audit-2026-07-15.md`](research/glkvm-client-audit-2026-07-15.md).
