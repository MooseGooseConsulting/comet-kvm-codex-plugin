# Visible-Console Commands

Read this file only when the machine exposes a shell through pixels and an exact
transport such as SSH, serial console, or the Proxmox API is unavailable.

Establish the visible prompt and baseline with `kvm_screenshot` plus
`kvm_ocr_text`. Confirm the intended target, focus, and shell before mutation.

For one POSIX command, prefer `kvm_terminal_run(command=...)`. It wraps the
command in a state-isolated `sh -c`, OCR-confirms the wrapper shell and all of
its unique markers before submitting Enter, and observes only for its bounded
invocation. If HID reports skipped input characters, it will refuse submission.
Its result includes `completed`, `timeout`, or `not_submitted`, visible marker
evidence, a best-effort transcript, and uncertainty flags. An exit code is
credible only when the exact end marker is visibly observed. On timeout, do not
assume the remote command stopped: the tool releases HID state but never sends
Ctrl+C. This v1 composite is POSIX-only; use the primitive flow for PowerShell
or another shell.

For the primitive flow, call `kvm_send_text(text=...)`. Before submitting,
re-capture the command line and OCR it to confirm the typed text matches the
intended command — mandatory when the command is destructive or has a
similar-looking dangerous variant. If the visible text does not match, clear
the line and retype instead of submitting. Only then send
`kvm_send_keys(combo="Enter")`, and re-read the relevant screen region until
output stops changing or a bounded timeout is reached.

Use short commands and bounded output. When shell quoting is known, append a
visible completion marker and exit code. Do not invent an exit status when no
marker was observed.

Pixel OCR cannot guarantee bytes that scrolled off screen, stdout versus stderr,
complete whitespace, an undisplayed exit status, or output that changed faster
than capture. Do not describe OCR as exact SSH output.

Avoid entering secrets through the visible console when a safer credential path
exists. Switch to an exact transport when one becomes available. Report the
visible command, observed output, completion evidence, and uncertainty.
