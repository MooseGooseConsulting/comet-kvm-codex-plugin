from __future__ import annotations

import asyncio
from collections import deque

import pytest

import src.kvm_core.tools as kvm_tools
from src.kvm_core.runtime import KVMRuntime, TargetRuntime
from src.kvm_core.terminal import TerminalMarkers, build_posix_wrapper, extract_exit_code, merge_visible_text
from tests.bios_test_helpers import installed_kvm_runtime


class RecordedOCR:
    def __init__(self, values: dict[bytes, dict]):
        self.values = values
        self.calls: list[bytes] = []

    def get_status(self) -> dict:
        return {"available": True}

    def run_ocr(self, image: bytes, search_text: str, psm: int) -> dict:
        self.calls.append(image)
        return self.values[image]


class TerminalClient:
    def __init__(self, frames: list[bytes]):
        self.frames = deque(frames)
        self.sent_text: list[str] = []
        self.sent_combos: list[str] = []
        self.release_calls = 0
        self.release_error: Exception | None = None
        self.base_url = "https://127.0.0.1"
        self.typing_result: dict = {"skipped": []}

    def is_connected(self) -> bool:
        return True

    async def send_text(self, text: str, wpm: int = 200) -> dict:
        self.sent_text.append(text)
        return self.typing_result

    async def send_combo(self, combo: str) -> dict:
        self.sent_combos.append(combo)
        return {"sent": combo}

    async def get_screenshot(self, preview: bool = False) -> bytes:
        return self.frames.popleft()

    async def release_all(self) -> dict:
        self.release_calls += 1
        if self.release_error is not None:
            raise self.release_error
        return {"released": []}


def _runtime(client: TerminalClient, ocr: RecordedOCR, tmp_path) -> KVMRuntime:
    runtime = KVMRuntime(screenshot_cache=str(tmp_path / "shots"))
    runtime.ocr_mgr = ocr
    runtime.targets["default"] = TargetRuntime("default", client)
    runtime._sync_selected_client()
    return runtime


@pytest.fixture
def markers(monkeypatch) -> TerminalMarkers:
    fixed = TerminalMarkers(start="__KVM_START_a1b2c3__", end="__KVM_END_a1b2c3__", typed="__KVM_TYPED_a1b2c3__")
    monkeypatch.setattr("src.kvm_core.terminal.make_markers", lambda: fixed)
    return fixed


def test_posix_wrapper_isolated_and_preserves_command_quoting() -> None:
    markers = TerminalMarkers(start="START", end="END", typed="TYPED")
    wrapper = build_posix_wrapper("printf '%s' \"a b\"; exit 7", markers)

    assert wrapper.startswith("sh -c ")
    assert "sh -c" in wrapper
    assert markers.start in wrapper
    assert markers.end in wrapper
    assert markers.typed in wrapper
    assert "exit 7" in wrapper


def test_exit_code_is_reported_only_from_an_exact_end_marker() -> None:
    marker = "__KVM_END_a1b2c3__"

    assert extract_exit_code(f"done\n{marker}:17", marker) == 17
    assert extract_exit_code(f"done\n{marker}:seventeen", marker) is None
    assert extract_exit_code("done\n__KVM_END_other__:17", marker) is None


def test_visible_transcript_merges_only_exact_overlap() -> None:
    transcript, overlap = merge_visible_text("first\nsecond", "second\nthird")

    assert transcript == "first\nsecond\nthird"
    assert overlap is True
    transcript, overlap = merge_visible_text(transcript, "unrelated")
    assert transcript == "first\nsecond\nthird\nunrelated"
    assert overlap is False


def test_terminal_run_returns_observed_exit_marker_and_transcript(tmp_path, markers) -> None:
    typed = b"typed"
    complete = b"complete"
    client = TerminalClient([typed, complete])
    ocr = RecordedOCR(
        {
            typed: {"text": f"$ sh -c {markers.start} {markers.end} {markers.typed}", "lines": []},
            complete: {"text": f"{markers.start}\nhello\n{markers.end}:7", "lines": []},
        }
    )
    runtime = _runtime(client, ocr, tmp_path)

    with installed_kvm_runtime(runtime):
        result = asyncio.run(kvm_tools.kvm_terminal_run("printf hello; exit 7", timeout_seconds=1, poll_interval_seconds=0.1))

    assert result["status"] == "completed"
    assert result["exit_code"] == 7
    assert result["completion_observed"] is True
    assert result["transcript"] == f"{markers.start}\nhello\n{markers.end}:7"
    assert result["marker_evidence"]["end"] == markers.end
    assert client.sent_combos == ["Enter"]
    assert client.release_calls == 1


def test_terminal_run_does_not_treat_a_lone_typed_marker_as_wrapper_confirmation(tmp_path, markers) -> None:
    typed = b"typed"
    client = TerminalClient([typed])
    ocr = RecordedOCR({typed: {"text": f"$ {markers.typed}", "lines": []}})
    runtime = _runtime(client, ocr, tmp_path)

    with installed_kvm_runtime(runtime):
        result = asyncio.run(kvm_tools.kvm_terminal_run("uname", timeout_seconds=1))

    assert result["status"] == "not_submitted"
    assert client.sent_combos == []


def test_terminal_run_does_not_submit_when_hid_skips_command_characters(tmp_path, markers) -> None:
    typed = b"typed"
    client = TerminalClient([typed])
    client.typing_result = {"chars": 100, "skipped": ["\N{LATIN SMALL LETTER E WITH ACUTE}"]}
    ocr = RecordedOCR(
        {typed: {"text": f"$ sh -c {markers.start} {markers.end} {markers.typed}", "lines": []}}
    )
    runtime = _runtime(client, ocr, tmp_path)

    with installed_kvm_runtime(runtime):
        result = asyncio.run(kvm_tools.kvm_terminal_run("rm /tmp/caf\N{LATIN SMALL LETTER E WITH ACUTE}", timeout_seconds=1))

    assert result["status"] == "not_submitted"
    assert result["typing_skipped_characters"] == 1
    assert result["uncertainty"]["typing_incomplete"] is True
    assert client.sent_combos == []
    assert client.release_calls == 1


def test_terminal_run_rejects_multiline_commands_before_typing(tmp_path) -> None:
    client = TerminalClient([])
    ocr = RecordedOCR({})
    runtime = _runtime(client, ocr, tmp_path)

    with installed_kvm_runtime(runtime):
        with pytest.raises(ValueError, match="single line"):
            asyncio.run(kvm_tools.kvm_terminal_run("echo first\necho second"))

    assert client.sent_text == []
    assert client.sent_combos == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timeout_seconds": 301}, "timeout_seconds must be <= 300"),
        ({"poll_interval_seconds": 0}, "poll_interval_seconds must be >= 0.1"),
        ({"poll_interval_seconds": 0.05}, "poll_interval_seconds must be >= 0.1"),
    ],
)
def test_terminal_run_rejects_unbounded_observation_settings(tmp_path, kwargs, message) -> None:
    client = TerminalClient([])
    ocr = RecordedOCR({})
    runtime = _runtime(client, ocr, tmp_path)

    with installed_kvm_runtime(runtime):
        with pytest.raises(ValueError, match=message):
            asyncio.run(kvm_tools.kvm_terminal_run("true", **kwargs))

    assert client.sent_text == []


def test_terminal_run_does_not_submit_when_typed_wrapper_is_unconfirmed(tmp_path, markers) -> None:
    typed = b"typed"
    client = TerminalClient([typed])
    ocr = RecordedOCR({typed: {"text": "$ incomplete wrapper", "lines": []}})
    runtime = _runtime(client, ocr, tmp_path)

    with installed_kvm_runtime(runtime):
        result = asyncio.run(kvm_tools.kvm_terminal_run("uname", timeout_seconds=1))

    assert result["status"] == "not_submitted"
    assert result["typed_command_confirmed"] is False
    assert result["completion_observed"] is False
    assert client.sent_combos == []
    assert client.release_calls == 1


def test_terminal_run_reports_ocr_failure_without_submitting(tmp_path, markers) -> None:
    typed = b"typed"
    client = TerminalClient([typed])
    ocr = RecordedOCR({typed: {"error": "decoder failed", "text": "", "lines": []}})
    runtime = _runtime(client, ocr, tmp_path)

    with installed_kvm_runtime(runtime):
        result = asyncio.run(kvm_tools.kvm_terminal_run("uname", timeout_seconds=1))

    assert result["status"] == "not_submitted"
    assert result["uncertainty"]["ocr_failure"] is True
    assert client.sent_combos == []


def test_terminal_run_skips_ocr_for_unchanged_frames(tmp_path, markers, monkeypatch) -> None:
    typed = b"typed"
    output = b"output"
    complete = b"complete"
    client = TerminalClient([typed, output, output, complete])
    ocr = RecordedOCR(
        {
            typed: {"text": f"sh -c {markers.start} {markers.end} {markers.typed}", "lines": []},
            output: {"text": f"{markers.start}\nline one", "lines": []},
            complete: {"text": f"line one\n{markers.end}:0", "lines": []},
        }
    )
    runtime = _runtime(client, ocr, tmp_path)
    scheduled_delays: list[float] = []
    original_sleep = asyncio.sleep

    async def record_sleep(delay: float) -> None:
        scheduled_delays.append(delay)
        await original_sleep(0)

    monkeypatch.setattr("src.kvm_core.terminal.asyncio.sleep", record_sleep)

    with installed_kvm_runtime(runtime):
        result = asyncio.run(kvm_tools.kvm_terminal_run("printf line", timeout_seconds=1, poll_interval_seconds=0.1))

    assert result["status"] == "completed"
    assert result["unchanged_frames_skipped"] == 1
    assert ocr.calls == [typed, output, complete]
    assert result["transcript"] == f"{markers.start}\nline one\n{markers.end}:0"
    assert scheduled_delays == [0.1, 0.1]


def test_terminal_run_timeout_releases_hid_without_interrupting_remote_command(tmp_path, markers) -> None:
    typed = b"typed"
    waiting = b"waiting"
    client = TerminalClient([typed, waiting])
    ocr = RecordedOCR(
        {
            typed: {"text": f"sh -c {markers.start} {markers.end} {markers.typed}", "lines": []},
            waiting: {"text": f"{markers.start}\nstill running", "lines": []},
        }
    )
    runtime = _runtime(client, ocr, tmp_path)

    with installed_kvm_runtime(runtime):
        result = asyncio.run(kvm_tools.kvm_terminal_run("sleep 999", timeout_seconds=0, poll_interval_seconds=0.1))

    assert result["status"] == "timeout"
    assert result["completion_observed"] is False
    assert result["uncertainty"]["remote_command_may_still_be_running"] is True
    assert client.sent_combos == ["Enter"]
    assert client.release_calls == 1


def test_terminal_run_preserves_completed_result_when_hid_release_fails(tmp_path, markers) -> None:
    typed = b"typed"
    complete = b"complete"
    client = TerminalClient([typed, complete])
    client.release_error = RuntimeError("websocket disconnected")
    ocr = RecordedOCR(
        {
            typed: {"text": f"sh -c {markers.start} {markers.end} {markers.typed}", "lines": []},
            complete: {"text": f"{markers.end}:0", "lines": []},
        }
    )
    runtime = _runtime(client, ocr, tmp_path)

    with installed_kvm_runtime(runtime):
        result = asyncio.run(kvm_tools.kvm_terminal_run("true", timeout_seconds=1, poll_interval_seconds=0.1))

    assert result["status"] == "completed"
    assert result["uncertainty"]["hid_release_failed"] is True
    assert client.release_calls == 1


def test_terminal_run_holds_operation_fence_until_observation_finishes(tmp_path, markers) -> None:
    typed = b"typed"
    complete = b"complete"

    class BlockingClient(TerminalClient):
        def __init__(self):
            super().__init__([typed, complete])
            self.poll_started = asyncio.Event()
            self.release_poll = asyncio.Event()

        async def get_screenshot(self, preview: bool = False) -> bytes:
            if self.sent_combos:
                self.poll_started.set()
                await self.release_poll.wait()
            return await super().get_screenshot(preview)

    async def scenario() -> list[str]:
        client = BlockingClient()
        ocr = RecordedOCR(
            {
                typed: {"text": f"sh -c {markers.start} {markers.end} {markers.typed}", "lines": []},
                complete: {"text": f"{markers.end}:0", "lines": []},
            }
        )
        runtime = _runtime(client, ocr, tmp_path)
        with installed_kvm_runtime(runtime):
            terminal = asyncio.create_task(kvm_tools.kvm_terminal_run("true", timeout_seconds=1, poll_interval_seconds=0.1))
            await client.poll_started.wait()
            send_key = asyncio.create_task(kvm_tools.kvm_send_keys("F5"))
            await asyncio.sleep(0)
            assert client.sent_combos == ["Enter"]
            client.release_poll.set()
            await terminal
            await send_key
        return client.sent_combos

    assert asyncio.run(scenario()) == ["Enter", "F5"]
