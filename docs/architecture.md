# Architecture

> **Repo:** `Coldaine/comet-kvm-codex-plugin` (selective fork of `kennypeh85/glkvm-mcp`)
> **Confirmed:** 2026-07-10 against the repository, MCP process, and Comet at `192.168.0.126`

## Architecture Thesis

One stdio MCP process composes a universal physical KVM core with a BIOS-aware
sidecar. The core owns Comet transport and screen primitives; the sidecar
consumes those primitives and adds firmware semantics. The core is the ordinary
product path. The sidecar is a specialist lane, even though it currently loads
in the same process (set `COMET_DISABLE_BIOS_SIDECAR=1` to omit it). Dependency
direction is one-way: sidecar may depend on KVM core, never the reverse.

## Maturity — two layers

The product has two deliberately unequal lanes:

| Layer | Role | Maturity |
|---|---|---|
| Core: universal KVM (`src/kvm_core/`) | Transport, HID, screenshots, host OCR, Comet hardware tools, plugin packaging | Primary implemented path; physical qualification varies by capability and target |
| Specialist: BIOS sidecar (`src/bios_sidecar/`) | Observation, graph/state, VLM grounding, navigation, mutation, cartography | Optional firmware lane — code exists; end-to-end board proof is **Planned** |

Until specialist firmware work signs off on a named board, treat BIOS
mutation/save paths as lab-only. Ordinary core operations remain independently
useful.

## Status Legend

- **Current** — implemented in the repository or verified in the live setup.
- **Planned** — decided direction, not fully implemented.
- **Candidate** — plausible option, not yet accepted as implementation work.
- **Deferred** — intentionally not being built now.

## System Shape

| Area | Status | Approach |
|---|---|---|
| MCP composition | Current | `glkvm_mcp.py` registers KVM and BIOS tools on one `FastMCP("comet-kvm")` instance. |
| Codex plugin packaging | Current | `.codex-plugin/plugin.json` bundles `.claude/skills/` + `.mcp.json`; the launcher starts **this repo's** MCP server (not an external upstream package). |
| Plugin launch | Current | `.mcp.json` launches via `uv run --locked --python 3.13 python ./glkvm_mcp.py`; `kvm_connect` fetches `GLCOMET_ADMIN_PASSWORD` from Doppler CLI. |
| Universal KVM | Current | `src/kvm_core/` owns auth, HTTP/WebSocket transport, HID, screenshots, OCR, logging, and Comet hardware tools. |
| BIOS sidecar | Specialist | `src/bios_sidecar/` owns optional BIOS observation, graph/state, VLM grounding, navigation, mutation, recovery, and trace resources/tools. |
| Host OCR | Current | Pillow decodes frames; pytesseract returns ordered text and word boxes with a timeout off the asyncio event loop. |
| MCP text OCR | Current | `kvm_ocr_text` captures a frame and runs host Tesseract. GL.iNet's product UI Text Recognition is browser-side Tesseract.js and is not a device/API backend for this process. |
| Bounded KVM command observer | Current (POSIX) | `kvm_terminal_run` types an isolated POSIX `sh -c` wrapper, OCR-confirms it before Enter, polls visible output only for that call, then returns and discards a best-effort transcript. |
| Exact target shell | Candidate | Optional AsyncSSH companion for network-reachable targets; separate credentials and trust policy. |
| Always-on OCR transcript | Deferred | Avoid background cost, sensitive-text retention, and false claims of complete scrollback. |

## Current Runtime Composition

```text
AI agent
  │ MCP stdio tool call/result
  ▼
glkvm_mcp.py (composition entry point)
  ├── src/kvm_core
  │     ├── FastMCP server + universal tools
  │     ├── KVMRuntime (one physical session per target)
  │     ├── CometClient (HTTPS + WebSocket)
  │     ├── CaptureManager
  │     └── OCRManager (Pillow + pytesseract)
  └── src/bios_sidecar
        ├── bios_* tools and bios:// resources
        ├── state graph, matcher, store, and sync
        ├── controller and trace ledger
        └── optional VLM perception call
```

The dependency direction is `bios_sidecar -> kvm_core`. `src/kvm_core` does not import BIOS state, policies, prompts, or VLM code. Both layers share one MCP server and one KVM runtime; the sidecar does not open a second physical connection.

## Agent and Service Topology

The project has two agent roles and one packaging surface:

1. The **developer agent** edits this repo (MCP server, skills, tests) by following `AGENTS.md` into `docs/NORTH_STAR.md`, `docs/decisions.md`, `docs/architecture.md`, and `docs/kvm-core.md`. `AGENTS.md` is a thin router — not part of the Codex plugin payload.
2. The **driver agent** operates a physical machine using bundled skills under `.claude/skills/comet-kvm-operations/` and `.claude/skills/comet-bios-triage/` (plugin payload) and the MCP tools this server exposes.
3. The **Codex plugin** is how the MCP server + skills are installed; it does not replace the MCP server.

The VLM is a stateless perception service called by the BIOS sidecar. It returns structured screen interpretation; it does not send input, navigate, edit code, or hold the project state.

## Physical KVM Boundary

The Comet provides HDMI capture plus USB HID and hardware-control APIs. It does not expose the controlled computer's shell byte stream. Therefore:

- BIOS, POST, recovery, installers, and network-down systems use KVM screenshots/OCR for output and HID for input.
- `kvm_ocr_text()` captures the visible frame, runs host Tesseract, and returns terminal text directly in the tool result.
- Output that scrolls off-screen before capture cannot be reconstructed reliably from a later screenshot.
- The Comet's own administration terminal, where present, is a shell on the KVM appliance—not the controlled computer.

Detailed call order and the bounded-observer design live in `docs/kvm-core.md#9-command-output-delivery`.

## Specialist lane: BIOS sidecar

The KVM core is the normal engine; the BIOS sidecar is specialist steering:

- `kvm_*` and `comet_*` remain general physical primitives.
- `bios_*` adds screen semantics, graph state, transition verification, and BIOS workflow behavior only when a firmware task calls for it.
- Raw KVM calls are not automatically intercepted or state-checked during BIOS work; the driver selects the correct layer.
- Visual verification remains required for BIOS actions such as save confirmation. This is state verification, not an approval-token system.

## Specialist lane: state and cartography

The BIOS sidecar owns the optional graph and map state used for firmware work.
It updates on demand through semantic `bios_*` calls and never becomes a
dependency of the KVM core. Runtime procedure, scope limits, and board-specific
work belong under `.claude/skills/comet-bios-triage/`.

## Architectural Invariants

- The KVM core never depends on BIOS semantics; this prevents universal transport from becoming firmware-specific.
- One MCP process owns its physical Comet sessions, one per connected target; this prevents conflicting HID state and duplicate watchdog/pinger loops within each target session.
- Commands, OCR text, credentials, screenshots, and live traces are not written to diagnostic logs; this limits accidental sensitive-data retention.
- MCP tool results are the primary agent data path; logs, progress events, and resources cannot silently replace explicit output.
- Exact SSH, if added, verifies host keys and uses credentials distinct from the Comet admin password; this prevents KVM appliance trust from becoming target-host trust.

## Open Architecture Questions

- Should a future bounded observer add a PowerShell wrapper without weakening the POSIX-only v1 completion evidence?
- Which target host aliases and known-host source should an AsyncSSH companion accept? Resolve before promoting exact target shell from Candidate.
- Does recorded terminal OCR show enough jitter to justify fuzzy matching beyond exact overlap and standard-library `difflib`? Resolve only from fixtures.

## Links

- Intent and goals: `docs/NORTH_STAR.md`
- Accepted implementation choices: `docs/decisions.md`
- Universal KVM detail and runtime call order: `docs/kvm-core.md`
- Verified Comet API surface: `docs/reference/comet-api.md`
- BIOS perception contract (sidecar design): `docs/vlm-prompt-contract.md`
- Live hardware / MSI proof: `docs/workflows/live-hardware-qualification.md`
- Developer doc ladder: `AGENTS.md`
- How to **use** the product at runtime: `.claude/skills/comet-kvm-operations/`, `.claude/skills/comet-bios-triage/` (plugin payload; not develop authority)
