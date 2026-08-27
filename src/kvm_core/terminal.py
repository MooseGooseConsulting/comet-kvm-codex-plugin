"""Bounded visual-console command observation for POSIX shells.

This module intentionally treats HDMI OCR as evidence, not as a byte stream.
It keeps no transcript after returning from an invocation.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import shlex
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalMarkers:
    """Unique visible markers for one shell invocation."""

    start: str
    end: str
    typed: str


def make_markers() -> TerminalMarkers:
    nonce = secrets.token_hex(8)
    return TerminalMarkers(
        start=f"__KVM_START_{nonce}__",
        end=f"__KVM_END_{nonce}__",
        typed=f"__KVM_TYPED_{nonce}__",
    )


def build_posix_wrapper(command: str, markers: TerminalMarkers) -> str:
    """Return one safely quoted, state-isolated POSIX shell command.

    The inner ``sh -c`` owns the supplied command, so ``cd``, exports, shell
    options, and an explicit ``exit`` cannot affect the interactive shell. The
    outer wrapper always gets a chance to print the end marker and captured
    inner exit code.
    """
    inner_command = shlex.quote(command)
    outer_script = (
        f"printf '%s\\n' {shlex.quote(markers.start)}; "
        f"sh -c {inner_command}; "
        "kvm_terminal_status=$?; "
        f"printf '%s:%s\\n' {shlex.quote(markers.end)} \"$kvm_terminal_status\"; "
        "exit \"$kvm_terminal_status\""
    )
    # This comment gives OCR a short, unique confirmation target after the
    # complete wrapper has been typed. It is intentionally outside ``sh -c``.
    return f"sh -c {shlex.quote(outer_script)} # {markers.typed}"


def extract_exit_code(text: str, end_marker: str) -> int | None:
    """Extract an exit code only when the exact end marker is visible."""
    match = re.search(rf"(?:^|\n){re.escape(end_marker)}:([0-9]+)(?:\s|$)", text)
    return int(match.group(1)) if match else None


def merge_visible_text(existing: str, incoming: str) -> tuple[str, bool]:
    """Append a frame using only a literal suffix/prefix text overlap."""
    if not existing:
        return incoming, True
    if not incoming or incoming in existing:
        return existing, True
    if existing in incoming:
        return incoming, True

    limit = min(len(existing), len(incoming))
    for size in range(limit, 0, -1):
        if existing[-size:] == incoming[:size]:
            return existing + incoming[size:], True
    return f"{existing}\n{incoming}", False


def typed_wrapper_confirmed(text: str, markers: TerminalMarkers) -> bool:
    """Require OCR evidence for the wrapper's shell and all unique markers."""
    return all(token in text for token in ("sh -c", markers.start, markers.end, markers.typed))


async def _capture_frame(client) -> tuple[bytes | None, str | None]:
    """Capture exactly one visual frame without doing OCR."""
    try:
        return await client.get_screenshot(preview=False), None
    except Exception as exc:  # noqa: BLE001 - tool result reports visual uncertainty
        return None, f"screenshot failed: {exc}"


async def _ocr_frame(ocr_manager, image: bytes) -> tuple[str | None, str | None]:
    """OCR one already-captured frame with terminal-oriented layout."""
    result = await asyncio.to_thread(ocr_manager.run_ocr, image, "", 6)
    if result.get("error"):
        return None, str(result["error"])
    return str(result.get("text", "")), None


async def _read_visible_text(client, ocr_manager) -> tuple[str | None, str | None, bytes | None]:
    """Capture exactly one frame and OCR it with terminal-oriented layout."""
    image, capture_error = await _capture_frame(client)
    if capture_error is not None or image is None:
        return None, capture_error, None
    text, ocr_error = await _ocr_frame(ocr_manager, image)
    return text, ocr_error, image


def _result(
    *,
    status: str,
    transcript: str,
    markers: TerminalMarkers,
    exit_code: int | None,
    typed_command_confirmed: bool,
    start_observed: bool,
    end_observed: bool,
    duration_seconds: float,
    poll_count: int,
    unchanged_frames_skipped: int,
    transcript_truncated: bool,
    ocr_failure: bool,
    typing_skipped_characters: int,
) -> dict:
    completion_observed = exit_code is not None
    return {
        "status": status,
        "transcript": transcript,
        "exit_code": exit_code,
        "completion_observed": completion_observed,
        "typed_command_confirmed": typed_command_confirmed,
        "marker_evidence": {
            "start": markers.start,
            "start_observed": start_observed,
            "end": markers.end,
            "end_observed": end_observed,
            "exit_code_observed": completion_observed,
        },
        "duration_seconds": duration_seconds,
        "poll_count": poll_count,
        "unchanged_frames_skipped": unchanged_frames_skipped,
        "transcript_truncated": transcript_truncated,
        "typing_skipped_characters": typing_skipped_characters,
        "uncertainty": {
            "visual_output_not_exact": True,
            "completion_not_observed": not completion_observed,
            "remote_command_may_still_be_running": status == "timeout",
            "ocr_failure": ocr_failure,
            "transcript_may_be_truncated": transcript_truncated,
            "typing_incomplete": typing_skipped_characters > 0,
            "hid_release_failed": False,
        },
    }


async def run_posix_terminal_command(
    client,
    ocr_manager,
    command: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict:
    """Type, confirm, submit, and boundedly observe one POSIX command."""
    markers = make_markers()
    wrapper = build_posix_wrapper(command, markers)
    started_at = time.monotonic()
    transcript = ""
    poll_count = 0
    unchanged_frames_skipped = 0
    transcript_truncated = False
    start_observed = False
    end_observed = False
    exit_code: int | None = None
    ocr_failure = False
    seen_frame_hashes: set[bytes] = set()
    result: dict | None = None

    def finish(**kwargs) -> dict:
        nonlocal result
        result = _result(**kwargs)
        return result

    try:
        typing_result = await client.send_text(wrapper)
        skipped = typing_result.get("skipped", []) if isinstance(typing_result, dict) else []
        skipped_count = len(skipped) if isinstance(skipped, list) else 0
        if skipped_count:
            return finish(
                status="not_submitted",
                transcript="",
                markers=markers,
                exit_code=None,
                typed_command_confirmed=False,
                start_observed=False,
                end_observed=False,
                duration_seconds=time.monotonic() - started_at,
                poll_count=0,
                unchanged_frames_skipped=0,
                transcript_truncated=False,
                ocr_failure=False,
                typing_skipped_characters=skipped_count,
            )
        typed_text, typed_error, typed_frame = await _read_visible_text(client, ocr_manager)
        if typed_frame is not None:
            seen_frame_hashes.add(hashlib.sha256(typed_frame).digest())
        if typed_error is not None:
            ocr_failure = True
        typed_confirmed = (
            typed_error is None
            and typed_text is not None
            and typed_wrapper_confirmed(typed_text, markers)
        )
        if not typed_confirmed:
            return finish(
                status="not_submitted",
                transcript="",
                markers=markers,
                exit_code=None,
                typed_command_confirmed=False,
                start_observed=False,
                end_observed=False,
                duration_seconds=time.monotonic() - started_at,
                poll_count=0,
                unchanged_frames_skipped=0,
                transcript_truncated=False,
                ocr_failure=ocr_failure,
                typing_skipped_characters=0,
            )

        await client.send_combo("Enter")
        while time.monotonic() < started_at + timeout_seconds:
            poll_count += 1
            frame, capture_error = await _capture_frame(client)
            read_error = capture_error
            visible_text: str | None = None
            if frame is not None:
                frame_hash = hashlib.sha256(frame).digest()
                if frame_hash in seen_frame_hashes:
                    unchanged_frames_skipped += 1
                    await asyncio.sleep(poll_interval_seconds)
                    continue
                seen_frame_hashes.add(frame_hash)
                visible_text, ocr_error = await _ocr_frame(ocr_manager, frame)
                read_error = ocr_error
            if read_error is not None:
                ocr_failure = True
            elif visible_text is not None:
                transcript, overlapped = merge_visible_text(transcript, visible_text)
                transcript_truncated = transcript_truncated or not overlapped
                start_observed = start_observed or markers.start in visible_text
                end_observed = end_observed or markers.end in visible_text
                exit_code = extract_exit_code(visible_text, markers.end)
                if exit_code is not None:
                    return finish(
                        status="completed",
                        transcript=transcript,
                        markers=markers,
                        exit_code=exit_code,
                        typed_command_confirmed=True,
                        start_observed=start_observed,
                        end_observed=end_observed,
                        duration_seconds=time.monotonic() - started_at,
                        poll_count=poll_count,
                        unchanged_frames_skipped=unchanged_frames_skipped,
                        transcript_truncated=transcript_truncated,
                        ocr_failure=ocr_failure,
                        typing_skipped_characters=0,
                    )
            await asyncio.sleep(poll_interval_seconds)

        return finish(
            status="timeout",
            transcript=transcript,
            markers=markers,
            exit_code=None,
            typed_command_confirmed=True,
            start_observed=start_observed,
            end_observed=end_observed,
            duration_seconds=time.monotonic() - started_at,
            poll_count=poll_count,
            unchanged_frames_skipped=unchanged_frames_skipped,
            transcript_truncated=transcript_truncated,
            ocr_failure=ocr_failure,
            typing_skipped_characters=0,
        )
    finally:
        # This releases the Comet HID state only; it does not send Ctrl+C or
        # make any claim about whether a timed-out remote command was stopped.
        try:
            await client.release_all()
        except Exception:  # noqa: BLE001 - cleanup must not erase command evidence
            if result is not None:
                result["uncertainty"]["hid_release_failed"] = True
