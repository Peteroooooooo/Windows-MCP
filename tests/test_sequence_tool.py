"""Tests for the Sequence batch action tool, fractional Wait, and focused Desktop.type."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from windows_mcp.desktop.control import ControlBlocked
from windows_mcp.desktop.service import Desktop
from windows_mcp.desktop.views import DesktopState, Status, Window
from windows_mcp.tools.input import register as register_input
from windows_mcp.tools.sequence import MAX_SEQUENCE_STEPS, register as register_sequence
from windows_mcp.tree.views import BoundingBox, TreeState


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self, *, name: str, **kwargs: object) -> Callable:
        def decorator(func: Callable) -> Callable:
            self.tools[name] = func
            return func

        return decorator


def _state(active_window_name: str = "Google Chrome") -> DesktopState:
    box = BoundingBox(left=0, top=0, right=1280, bottom=800, width=1280, height=800)
    return DesktopState(
        active_desktop={"name": "Desktop 1"},
        all_desktops=[],
        active_window=Window(
            name=active_window_name,
            is_browser=True,
            depth=0,
            status=Status.NORMAL,
            bounding_box=box,
            handle=101,
            process_id=202,
        ),
        windows=[],
        tree_state=TreeState(),
    )


def _register_seq(desktop: object) -> Callable:
    mcp = FakeMCP()
    register_sequence(mcp, get_desktop=lambda: desktop, get_analytics=lambda: None)
    return mcp.tools["Sequence"]


def test_sequence_executes_ordered_steps_including_focused_type_and_fractional_wait() -> None:
    events: list[tuple] = []
    desktop = MagicMock()
    desktop.app.side_effect = lambda mode, name, loc, size: (
        events.append(("app", mode, name)) or f"Switched to {name}"
    )
    desktop.shortcut.side_effect = lambda s: events.append(("shortcut", s))
    desktop.type.side_effect = lambda **kw: (events.append(("type", kw)) or "")
    desktop.click.side_effect = lambda **kw: (events.append(("click", kw)) or "")
    desktop.get_state.return_value = _state("小红书 - Google Chrome")

    seq = _register_seq(desktop)
    result = asyncio.run(
        seq(
            steps=[
                {"action": "app", "mode": "switch", "name": "Google Chrome"},
                {"action": "shortcut", "shortcut": "ctrl+t"},
                {"action": "wait", "seconds": 0.01},
                {"action": "shortcut", "shortcut": "ctrl+l"},
                {
                    "action": "type",
                    "text": "https://www.xiaohongshu.com",
                    "press_enter": True,
                },
                {"action": "click", "loc": [100, 200]},
                {"action": "wait_for", "condition": "active_window", "text": "小红书"},
            ],
            final_capture="none",
        )
    )

    assert len(result) == 1
    assert "Sequence completed 7/7 step(s)" in result[0]
    assert "[1/7] app: OK" in result[0]
    assert "[3/7] wait: OK - Waited 0.01s" in result[0]
    assert "[5/7] type: OK - Typed 27 char(s) at focused control" in result[0]
    assert "[7/7] wait_for: OK" in result[0]
    assert events == [
        ("app", "switch", "Google Chrome"),
        ("shortcut", "ctrl+t"),
        ("shortcut", "ctrl+l"),
        (
            "type",
            {
                "loc": None,
                "text": "https://www.xiaohongshu.com",
                "caret_position": "idle",
                "clear": False,
                "press_enter": True,
            },
        ),
        ("click", {"loc": [100, 200], "button": "left", "clicks": 1}),
    ]


def test_sequence_accepts_json_string_steps_and_returns_final_screenshot(monkeypatch) -> None:
    desktop = MagicMock()
    captured_calls: list[dict] = []

    def fake_capture(_desktop, **kwargs):
        captured_calls.append(kwargs)
        return {
            "state": _state(),
            "screenshot_bytes": b"pngbytes",
            "tree_text": "",
            "scale": 1.0,
            "image_origin": (0, 0),
            "display_indices": None,
            "region": None,
            "backend": "fake",
        }

    monkeypatch.setattr("windows_mcp.tools.sequence.capture_desktop_state", fake_capture)
    monkeypatch.setattr(
        "windows_mcp.tools.sequence.build_snapshot_response",
        lambda cap, **kw: ["Snapshot Header", "FAKE_IMAGE"],
    )

    seq = _register_seq(desktop)
    steps_json = json.dumps([{"action": "shortcut", "shortcut": "ctrl+l"}])
    result = asyncio.run(
        seq(steps=steps_json, final_capture="screenshot", capture_region="[0, 0, 800, 600]")
    )

    desktop.shortcut.assert_called_once_with("ctrl+l")
    assert captured_calls and captured_calls[0]["use_ui_tree"] is False
    assert captured_calls[0]["region"] == [0, 0, 800, 600]
    assert "Sequence completed 1/1 step(s)" in result[0]
    assert "Snapshot Header" in result[0]
    assert result[1] == "FAKE_IMAGE"


def test_sequence_prevalidates_all_steps_before_executing_any() -> None:
    desktop = MagicMock()
    seq = _register_seq(desktop)

    with pytest.raises(ValueError, match="Step 2 has unsupported action"):
        asyncio.run(
            seq(
                steps=[
                    {"action": "shortcut", "shortcut": "ctrl+t"},
                    {"action": "nonexistent_action"},
                ],
                final_capture="none",
            )
        )

    desktop.shortcut.assert_not_called()

    with pytest.raises(ValueError, match=f"exceed {MAX_SEQUENCE_STEPS}"):
        asyncio.run(
            seq(
                steps=[{"action": "wait", "seconds": 0}] * (MAX_SEQUENCE_STEPS + 1),
                final_capture="none",
            )
        )


def test_sequence_stop_on_error_returns_failure_with_screenshot_or_raises(monkeypatch) -> None:
    desktop = MagicMock()
    desktop.app.side_effect = ValueError("Window 'Nope' not found")

    seq = _register_seq(desktop)

    with pytest.raises(RuntimeError, match=r"Sequence stopped at step 1/2 \(app\)"):
        asyncio.run(
            seq(
                steps=[
                    {"action": "app", "mode": "switch", "name": "Nope"},
                    {"action": "shortcut", "shortcut": "ctrl+t"},
                ],
                stop_on_error=True,
                final_capture="none",
            )
        )
    desktop.shortcut.assert_not_called()

    monkeypatch.setattr(
        "windows_mcp.tools.sequence.capture_desktop_state",
        lambda *_a, **_kw: {"state": _state()},
    )
    monkeypatch.setattr(
        "windows_mcp.tools.sequence.build_snapshot_response",
        lambda *_a, **_kw: ["Desktop screen", "IMG_ERR"],
    )
    result = asyncio.run(
        seq(
            steps=[
                {"action": "app", "mode": "switch", "name": "Nope"},
                {"action": "shortcut", "shortcut": "ctrl+t"},
            ],
            stop_on_error=True,
            final_capture="screenshot",
        )
    )
    assert "Sequence stopped at step 1/2 (app)" in result[0]
    assert "[1/2] app: FAILED - Window 'Nope' not found" in result[0]
    assert result[1] == "IMG_ERR"
    desktop.shortcut.assert_not_called()


def test_sequence_continue_when_stop_on_error_false() -> None:
    desktop = MagicMock()
    desktop.app.side_effect = ValueError("No window")

    seq = _register_seq(desktop)
    result = asyncio.run(
        seq(
            steps=[
                {"action": "app", "mode": "switch", "name": "Missing"},
                {"action": "shortcut", "shortcut": "ctrl+t"},
            ],
            stop_on_error=False,
            final_capture="none",
        )
    )

    assert "Sequence finished with errors" in result[0]
    assert "[1/2] app: FAILED" in result[0]
    assert "[2/2] shortcut: OK - Pressed ctrl+t" in result[0]
    desktop.shortcut.assert_called_once_with("ctrl+t")


def test_sequence_propagates_control_blocked() -> None:
    desktop = MagicMock()
    desktop.shortcut.side_effect = ControlBlocked("CONTROL_PREEMPTED", {"state": "user"})
    seq = _register_seq(desktop)

    with pytest.raises(ControlBlocked):
        asyncio.run(
            seq(
                steps=[{"action": "shortcut", "shortcut": "ctrl+t"}],
                final_capture="screenshot",
            )
        )


def test_wait_tool_supports_fractional_seconds() -> None:
    mcp = FakeMCP()
    register_input(mcp, get_desktop=lambda: MagicMock(), get_analytics=lambda: None)
    result = asyncio.run(mcp.tools["Wait"](duration=0.01))
    assert result == "Waited for 0.01 seconds."

    with pytest.raises(ValueError, match="between 0 and 300"):
        asyncio.run(mcp.tools["Wait"](duration=-0.5))


def test_desktop_type_without_loc_types_into_focused_control(monkeypatch) -> None:
    sent_keys: list[str] = []
    clicks: list[tuple[int, int]] = []

    monkeypatch.setattr("windows_mcp.desktop.service.uia.Click", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(
        "windows_mcp.desktop.service.uia.SendKeys",
        lambda key, **_kw: sent_keys.append(key),
    )

    desktop = Desktop.__new__(Desktop)
    monkeypatch.setattr(desktop, "_focused_control", lambda: None)
    monkeypatch.setattr(desktop, "_read_back", lambda *a, **kw: "Field reads back 'hi'.")

    note = desktop.type(loc=None, text="hi", press_enter=True)

    assert clicks == []
    assert sent_keys == ["h", "i", "{Enter}"]
    assert note == "Field reads back 'hi'."


def test_sequence_defaults_to_no_final_capture(monkeypatch) -> None:
    desktop = MagicMock()
    capture_called = False

    def fail_if_called(*_a, **_kw):
        nonlocal capture_called
        capture_called = True
        raise AssertionError("capture_desktop_state should not be called by default")

    monkeypatch.setattr("windows_mcp.tools.sequence.capture_desktop_state", fail_if_called)

    seq = _register_seq(desktop)
    result = asyncio.run(seq(steps=[{"action": "shortcut", "shortcut": "ctrl+t"}]))

    assert capture_called is False
    assert len(result) == 1
    assert "Sequence completed 1/1 step(s)" in result[0]


def test_sequence_wait_for_active_window_skips_ui_tree() -> None:
    desktop = MagicMock()
    desktop.get_state.return_value = _state("Notepad")
    seq = _register_seq(desktop)

    result = asyncio.run(
        seq(
            steps=[
                {
                    "action": "wait_for",
                    "condition": "active_window",
                    "window_name": "Notepad",
                    "use_dom": True,
                }
            ],
            final_capture="none",
        )
    )

    assert "Sequence completed 1/1 step(s)" in result[0]
    desktop.get_state.assert_called_once_with(
        use_vision=False,
        use_dom=False,
        use_ui_tree=False,
        use_annotation=False,
    )


def test_validate_screen_point_rejects_outside_active_clip_bounds(monkeypatch) -> None:
    real_rect = (0, -666, 4000, 2560)
    monkeypatch.setattr("windows_mcp.desktop.service.uia.GetVirtualScreenRect", lambda: real_rect)
    monkeypatch.setattr("windows_mcp.desktop.service.uia.RealVirtualScreenRect", lambda: real_rect)
    monkeypatch.setattr(
        "windows_mcp.desktop.service.uia.GetActiveClipBounds",
        lambda: (2560, -666, 4000, 1894),
    )

    Desktop._validate_screen_point(2700, 200)
    with pytest.raises(ValueError, match="active OS ClipCursor bounds"):
        Desktop._validate_screen_point(650, 409)


def test_sequence_tool_description_documents_wait_for_conditions() -> None:
    mcp = FakeMCP()
    captured: dict[str, str] = {}

    def capture_tool(*, name: str, description: str = "", **kwargs: object) -> Callable:
        def decorator(func: Callable) -> Callable:
            captured[name] = description
            return func

        return decorator

    mcp.tool = capture_tool  # type: ignore[method-assign]
    register_sequence(mcp, get_desktop=lambda: MagicMock(), get_analytics=lambda: None)
    desc = captured["Sequence"]
    for cond in (
        "text_exists",
        "active_window",
        "element_exists",
        "element_enabled",
        "focused_element",
    ):
        assert cond in desc


def test_wait_for_active_window_uses_fast_desktop_title_helper() -> None:
    class FastTitleDesktop:
        def get_active_window_name(self) -> str:
            return "无标题 - Notepad"

        def get_state(self, **_kw: object) -> object:
            raise AssertionError("get_state should not be called when get_active_window_name exists")

    seq = _register_seq(FastTitleDesktop())
    result = asyncio.run(
        seq(
            steps=[
                {
                    "action": "wait_for",
                    "condition": "active_window",
                    "window_name": "Notepad",
                }
            ],
            final_capture="none",
        )
    )

    assert "Sequence completed 1/1 step(s)" in result[0]
    assert "无标题 - Notepad" in result[0]


def test_sequence_applies_and_restores_fast_operation_wait_override(monkeypatch) -> None:
    from windows_mcp import uia
    from windows_mcp.desktop import service as desktop_service

    sleeps: list[float] = []
    seen_overrides: list[float | None] = []
    monkeypatch.setattr(uia.time, "sleep", lambda sec: sleeps.append(sec))
    monkeypatch.setattr(uia, "SetCursorPos", lambda _x, _y: None)
    monkeypatch.setattr(uia, "AbsoluteMouseEvent", lambda *_a: None)
    monkeypatch.setattr(uia, "mouse_event", lambda *_a: None)
    monkeypatch.setattr(desktop_service, "sleep", lambda sec: sleeps.append(sec))
    monkeypatch.setattr(desktop_service.uia, "GetVirtualScreenRect", lambda: (0, 0, 1920, 1080))

    desktop = Desktop.__new__(Desktop)
    monkeypatch.setattr(desktop, "_modal_dialog_warning", lambda _x, _y: "")

    orig_click = desktop.click

    def spy_click(**kwargs):
        seen_overrides.append(uia.get_operation_wait_override())
        return orig_click(**kwargs)

    monkeypatch.setattr(desktop, "click", spy_click)

    seq = _register_seq(desktop)
    assert uia.get_operation_wait_override() is None
    asyncio.run(
        seq(
            steps=[
                {"action": "click", "loc": [100, 100]},
                {"action": "scroll", "direction": "down", "wheel_times": 4},
            ],
            final_capture="none",
        )
    )
    assert uia.get_operation_wait_override() is None
    assert seen_overrides == [0.05]
    # Click post-wait should be 0.05 (not 0.5) and intermediate scroll ticks 0.02 (not 0.5).
    assert 0.5 not in sleeps


def test_sequence_release_after_releases_control_on_success_and_error(monkeypatch) -> None:
    controller = MagicMock()
    monkeypatch.setattr("windows_mcp.tools.sequence.get_controller", lambda: controller)

    desktop = MagicMock()
    seq = _register_seq(desktop)

    asyncio.run(
        seq(
            steps=[{"action": "shortcut", "shortcut": "ctrl+t"}],
            final_capture="none",
        )
    )
    controller.release.assert_not_called()

    asyncio.run(
        seq(
            steps=[{"action": "shortcut", "shortcut": "ctrl+t"}],
            final_capture="none",
            release_after="true",
        )
    )
    controller.release.assert_called_once()

    controller.release.reset_mock()
    desktop.shortcut.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="Sequence stopped at step 1/1"):
        asyncio.run(
            seq(
                steps=[{"action": "shortcut", "shortcut": "ctrl+t"}],
                final_capture="none",
                release_after=True,
            )
        )
    controller.release.assert_called_once()




