"""Sequence tool — execute an ordered batch of desktop actions in a single MCP call."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Literal

from fastmcp import Context
from mcp.types import ToolAnnotations

from windows_mcp.desktop.control import ControlBlocked, get_controller
from windows_mcp.desktop.utils import as_bool as _as_bool
from windows_mcp.desktop.utils import as_int_list as _as_int_list
from windows_mcp.infrastructure import with_analytics
from windows_mcp.tools._snapshot_helpers import (
    _as_region,
    build_snapshot_response,
    capture_desktop_state,
)
from windows_mcp.tools.app import execute_app
from windows_mcp.tools.clipboard import execute_clipboard
from windows_mcp.tools.input import (
    _as_loc,
    _as_point,
    _resolve_label,
    _validate_finite_number,
    _validate_wait_for_args,
    execute_wait_for,
)

MAX_SEQUENCE_STEPS = 50
_SLEEP_SLICE_SECONDS = 0.1

_ACTION_ALIASES = {
    "waitfor": "wait_for",
    "wait-for": "wait_for",
}

_SUPPORTED_ACTIONS = frozenset(
    {
        "app",
        "click",
        "type",
        "move",
        "scroll",
        "shortcut",
        "wait",
        "wait_for",
        "clipboard",
    }
)


def _sleep_with_checkpoint(seconds: float, checkpoint: Callable[[], None]) -> None:
    if seconds <= 0:
        checkpoint()
        return
    deadline = time.monotonic() + seconds
    while True:
        checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_SLEEP_SLICE_SECONDS, remaining))


def _coerce_steps(steps: list[dict[str, Any]] | str) -> list[dict[str, Any]]:
    if isinstance(steps, str):
        try:
            parsed = json.loads(steps)
        except json.JSONDecodeError as e:
            raise ValueError(f"steps must be a valid JSON list of step objects: {e}") from e
    else:
        parsed = steps

    if not isinstance(parsed, list) or not parsed:
        raise ValueError("steps must be a non-empty list of action objects")
    if len(parsed) > MAX_SEQUENCE_STEPS:
        raise ValueError(f"steps cannot exceed {MAX_SEQUENCE_STEPS} actions per call")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Step {index} must be an object/dict")
        normalized.append(dict(item))
    return normalized


def _normalize_action(step: dict[str, Any], index: int) -> str:
    raw = step.get("action") or step.get("tool")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"Step {index} is missing a valid 'action' string")
    action = raw.strip().lower().replace("-", "_")
    action = _ACTION_ALIASES.get(action, action)
    if action not in _SUPPORTED_ACTIONS:
        supported = ", ".join(sorted(_SUPPORTED_ACTIONS))
        raise ValueError(f"Step {index} has unsupported action {raw!r}. Supported: {supported}")
    return action


def _validate_and_normalize_step(step: dict[str, Any], index: int) -> dict[str, Any]:
    action = _normalize_action(step, index)

    if action == "app":
        mode = step.get("mode", "launch")
        if mode not in ("launch", "launch_executable", "resize", "switch"):
            raise ValueError(f"Step {index} (app): unsupported mode {mode!r}")
        executable = step.get("executable")
        args = step.get("args")
        cwd = step.get("cwd")
        name = step.get("name")
        window_loc = _as_int_list(step.get("window_loc"), "window_loc")
        window_size = _as_int_list(step.get("window_size"), "window_size")
        if mode != "launch_executable" and any(v is not None for v in (executable, args, cwd)):
            raise ValueError(
                f'Step {index} (app): executable, args, and cwd require mode="launch_executable"'
            )
        if mode == "launch_executable":
            if not executable:
                raise ValueError(
                    f'Step {index} (app): executable is required for mode="launch_executable"'
                )
            if name is not None or window_loc is not None or window_size is not None:
                raise ValueError(
                    f"Step {index} (app): name, window_loc, and window_size are not supported "
                    'for mode="launch_executable"'
                )
        return {
            "action": "app",
            "mode": mode,
            "name": name,
            "window_loc": window_loc,
            "window_size": window_size,
            "executable": executable,
            "args": args,
            "cwd": cwd,
        }

    if action == "click":
        loc = _as_loc(step.get("loc"))
        label = step.get("label")
        if loc is None and label is None:
            raise ValueError(f"Step {index} (click): either loc or label must be provided")
        if loc is not None:
            loc = _as_point(loc, f"step {index} loc")
        if label is not None and (isinstance(label, bool) or not isinstance(label, int)):
            raise ValueError(f"Step {index} (click): label must be an integer")
        button = step.get("button", "left")
        if button not in ("left", "right", "middle"):
            raise ValueError(f"Step {index} (click): button must be left, right, or middle")
        clicks = step.get("clicks", 1)
        if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks < 0:
            raise ValueError(f"Step {index} (click): clicks must be an integer >= 0")
        return {
            "action": "click",
            "loc": loc,
            "label": label,
            "button": button,
            "clicks": clicks,
        }

    if action == "type":
        if "text" not in step or not isinstance(step["text"], str):
            raise ValueError(f"Step {index} (type): 'text' string is required")
        loc = _as_loc(step.get("loc"))
        label = step.get("label")
        if loc is not None:
            loc = _as_point(loc, f"step {index} loc")
        if label is not None and (isinstance(label, bool) or not isinstance(label, int)):
            raise ValueError(f"Step {index} (type): label must be an integer")
        clear = _as_bool(step.get("clear", False), "clear")
        press_enter = _as_bool(step.get("press_enter", False), "press_enter")
        caret_position = step.get("caret_position", "idle")
        if caret_position not in ("start", "idle", "end"):
            raise ValueError(
                f"Step {index} (type): caret_position must be start, idle, or end"
            )
        return {
            "action": "type",
            "text": step["text"],
            "loc": loc,
            "label": label,
            "clear": clear,
            "caret_position": caret_position,
            "press_enter": press_enter,
        }

    if action == "move":
        loc = _as_loc(step.get("loc"))
        label = step.get("label")
        if loc is None and label is None:
            raise ValueError(f"Step {index} (move): either loc or label must be provided")
        if label is not None and (isinstance(label, bool) or not isinstance(label, int)):
            raise ValueError(f"Step {index} (move): label must be an integer")
        drag = _as_bool(step.get("drag", False), "drag")
        from_loc = _as_loc(step.get("from_loc"))
        duration = step.get("duration")
        if not drag and (from_loc is not None or duration is not None):
            raise ValueError(f"Step {index} (move): from_loc and duration require drag=True")
        if loc is not None:
            loc = _as_point(loc, f"step {index} loc")
        if from_loc is not None:
            from_loc = _as_point(from_loc, f"step {index} from_loc")
        if duration is not None:
            _validate_finite_number(duration, f"step {index} duration")
            if duration < 0 or duration > 10:
                raise ValueError(f"Step {index} (move): duration must be between 0 and 10")
        return {
            "action": "move",
            "loc": loc,
            "label": label,
            "drag": drag,
            "from_loc": from_loc,
            "duration": duration,
        }

    if action == "scroll":
        loc = _as_loc(step.get("loc"))
        label = step.get("label")
        if loc is not None:
            loc = _as_point(loc, f"step {index} loc")
        if label is not None and (isinstance(label, bool) or not isinstance(label, int)):
            raise ValueError(f"Step {index} (scroll): label must be an integer")
        scroll_type = step.get("type", "vertical")
        if scroll_type not in ("vertical", "horizontal"):
            raise ValueError(f"Step {index} (scroll): type must be vertical or horizontal")
        direction = step.get("direction", "down")
        if direction not in ("up", "down", "left", "right"):
            raise ValueError(f"Step {index} (scroll): direction must be up, down, left, or right")
        wheel_times = step.get("wheel_times", 1)
        if isinstance(wheel_times, bool) or not isinstance(wheel_times, int) or wheel_times <= 0:
            raise ValueError(f"Step {index} (scroll): wheel_times must be a positive integer")
        return {
            "action": "scroll",
            "loc": loc,
            "label": label,
            "type": scroll_type,
            "direction": direction,
            "wheel_times": wheel_times,
        }

    if action == "shortcut":
        shortcut = step.get("shortcut")
        if not isinstance(shortcut, str) or not shortcut.strip():
            raise ValueError(f"Step {index} (shortcut): non-empty 'shortcut' string is required")
        return {"action": "shortcut", "shortcut": shortcut.strip()}

    if action == "wait":
        duration = step.get("duration")
        if duration is None:
            duration = step.get("seconds")
        if duration is None:
            raise ValueError(f"Step {index} (wait): 'duration' (or 'seconds') is required")
        _validate_finite_number(duration, f"step {index} duration")
        if duration < 0 or duration > 60:
            raise ValueError(f"Step {index} (wait): duration must be between 0 and 60 seconds")
        return {"action": "wait", "duration": float(duration)}

    if action == "wait_for":
        condition = step.get("condition")
        if not isinstance(condition, str) or not condition.strip():
            raise ValueError(f"Step {index} (wait_for): 'condition' string is required")
        text = step.get("text")
        window_name = step.get("window_name")
        timeout = step.get("timeout", 10.0)
        interval = step.get("interval", 0.25)
        use_dom = _as_bool(step.get("use_dom", False), "use_dom")
        normalized = _validate_wait_for_args(
            condition=condition,
            text=text,
            window_name=window_name,
            timeout=timeout,
            interval=interval,
        )
        return {
            "action": "wait_for",
            "condition": normalized,
            "text": text,
            "window_name": window_name,
            "timeout": float(timeout),
            "interval": float(interval),
            "use_dom": use_dom,
        }

    # action == "clipboard"
    mode = step.get("mode")
    if mode not in ("get", "set"):
        raise ValueError(f"Step {index} (clipboard): mode must be 'get' or 'set'")
    text = step.get("text")
    if mode == "set" and not isinstance(text, str):
        raise ValueError(f"Step {index} (clipboard): 'text' string is required for mode='set'")
    return {"action": "clipboard", "mode": mode, "text": text}


def _execute_step(
    desktop: Any,
    step: dict[str, Any],
    checkpoint: Callable[[], None],
) -> str:
    action = step["action"]

    if action == "app":
        return str(
            execute_app(
                desktop,
                mode=step["mode"],
                name=step["name"],
                window_loc=step["window_loc"],
                window_size=step["window_size"],
                executable=step["executable"],
                args=step["args"],
                cwd=step["cwd"],
            )
        )

    if action == "click":
        loc = (
            _resolve_label(desktop, step["label"])
            if step["label"] is not None
            else step["loc"]
        )
        warning = desktop.click(loc=loc, button=step["button"], clicks=step["clicks"])
        msg = f"Clicked {step['button']} x{step['clicks']} at ({loc[0]},{loc[1]})"
        return f"{msg} ({warning})" if warning else msg

    if action == "type":
        loc = (
            _resolve_label(desktop, step["label"])
            if step["label"] is not None
            else step["loc"]
        )
        note = desktop.type(
            loc=loc,
            text=step["text"],
            caret_position=step["caret_position"],
            clear=step["clear"],
            press_enter=step["press_enter"],
        )
        target_desc = f"({loc[0]},{loc[1]})" if loc is not None else "focused control"
        msg = f"Typed {len(step['text'])} char(s) at {target_desc}"
        return f"{msg}. {note}" if note else msg

    if action == "move":
        loc = (
            _resolve_label(desktop, step["label"])
            if step["label"] is not None
            else step["loc"]
        )
        if step["drag"]:
            result = desktop.drag(loc=loc, from_loc=step["from_loc"], duration=step["duration"])
            return f"Dragged {result['start']} -> {result['end']} ({result['duration']}s)"
        desktop.move(loc)
        return f"Moved cursor to ({loc[0]},{loc[1]})"

    if action == "scroll":
        loc = (
            _resolve_label(desktop, step["label"])
            if step["label"] is not None
            else step["loc"]
        )
        desktop.scroll(loc, step["type"], step["direction"], step["wheel_times"])
        where = f"({loc[0]},{loc[1]})" if loc is not None else "current cursor"
        return f"Scrolled {step['type']} {step['direction']} x{step['wheel_times']} at {where}"

    if action == "shortcut":
        desktop.shortcut(step["shortcut"])
        return f"Pressed {step['shortcut']}"

    if action == "wait":
        _sleep_with_checkpoint(step["duration"], checkpoint)
        return f"Waited {step['duration']:.2f}s"

    if action == "wait_for":
        return execute_wait_for(
            desktop,
            condition=step["condition"],
            text=step["text"],
            window_name=step["window_name"],
            timeout=step["timeout"],
            interval=step["interval"],
            use_dom=step["use_dom"],
            checkpoint=checkpoint,
        )

    return execute_clipboard(mode=step["mode"], text=step["text"])


def _capture_final(
    desktop: Any,
    *,
    final_capture: Literal["none", "screenshot", "snapshot"],
    use_dom: bool,
    display: list[int] | None,
    region: tuple[int, int, int, int] | None,
) -> list[Any]:
    if final_capture == "none":
        return []
    is_snapshot = final_capture == "snapshot"
    capture_result = capture_desktop_state(
        desktop,
        use_vision=True,
        use_dom=use_dom if is_snapshot else False,
        use_annotation=is_snapshot,
        use_ui_tree=is_snapshot,
        width_reference_line=None,
        height_reference_line=None,
        display=display,
        region=region,
        tool_name="Sequence tool",
    )
    return build_snapshot_response(
        capture_result,
        include_ui_details=is_snapshot,
        ui_detail_note=(
            None
            if is_snapshot
            else "UI Tree: Skipped for fast sequence verification. Use final_capture='snapshot' or call Snapshot if element labels are needed."
        ),
    )


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Sequence",
        description=(
            "PREFER THIS TOOL whenever chaining 2+ desktop or browser actions — executes up to 50 "
            "steps in a single MCP call to eliminate multi-turn LLM round-trip latency. Supported actions: "
            "'app' (mode='launch'|'launch_executable'|'resize'|'switch', name/executable, args, cwd, window_loc, window_size), "
            "'click' (loc=[x,y] or label, button, clicks), "
            "'type' (text; optional loc=[x,y]/label — omit both to type into the currently focused control; clear, caret_position, press_enter), "
            "'move' (loc=[x,y] or label, drag, from_loc, duration), "
            "'scroll' (optional loc=[x,y]/label, type, direction, wheel_times), "
            "'shortcut' (shortcut, e.g. 'ctrl+t', 'ctrl+l', 'ctrl+v'), "
            "'wait' (duration/seconds as float, e.g. 0.15), "
            "'wait_for' (condition in {'text_exists','active_window','element_exists','element_enabled','focused_element'}, text, window_name, timeout, interval, use_dom — 'active_window' skips UI tree traversal and is ultra-fast), and "
            "'clipboard' (mode='get'|'set', text). "
            "Tips: pass loc=[x,y] directly from Snapshot text output; start coordinate clicks with app(mode='switch') or wait_for(condition='active_window') if focus is not guaranteed. "
            "All steps are validated before execution starts. Set final_capture='none' (default, fastest; verify via wait_for or type read-back), "
            "'screenshot' (fast visual verification — pass capture_display=[0] or capture_region=[l,t,r,b] on multi-monitor setups), or 'snapshot' (full UI tree + screenshot)."
        ),
        annotations=ToolAnnotations(
            title="Sequence",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Sequence-Tool", run_sync_in_thread=False)
    def sequence_tool(
        steps: list[dict[str, Any]] | str,
        stop_on_error: bool | str = True,
        step_delay: float = 0.0,
        final_capture: Literal["none", "screenshot", "snapshot"] = "none",
        use_dom: bool | str = False,
        capture_region: list[int] | str | None = None,
        capture_display: list[int] | str | None = None,
        ctx: Context = None,
    ) -> list[Any]:
        raw_steps = _coerce_steps(steps)
        stop_on_error_bool = _as_bool(stop_on_error, "stop_on_error")
        use_dom_bool = _as_bool(use_dom, "use_dom")
        _validate_finite_number(step_delay, "step_delay")
        if step_delay < 0 or step_delay > 5:
            raise ValueError("step_delay must be between 0 and 5 seconds")
        if final_capture not in ("none", "screenshot", "snapshot"):
            raise ValueError("final_capture must be 'none', 'screenshot', or 'snapshot'")
        if use_dom_bool and final_capture != "snapshot":
            raise ValueError("use_dom=True requires final_capture='snapshot'")

        parsed_region = _as_region(capture_region)
        parsed_display = _as_int_list(capture_display, "capture_display")
        validated_steps = [
            _validate_and_normalize_step(step, index)
            for index, step in enumerate(raw_steps, start=1)
        ]

        desktop = get_desktop()
        controller = get_controller()
        checkpoint = controller.checkpoint_current
        total = len(validated_steps)
        logs: list[str] = []
        failed_step: tuple[int, str, Exception] | None = None
        started_at = time.monotonic()

        for index, step in enumerate(validated_steps, start=1):
            checkpoint()
            action = step["action"]
            try:
                outcome = _execute_step(desktop, step, checkpoint)
                logs.append(f"[{index}/{total}] {action}: OK - {outcome}")
            except ControlBlocked:
                raise
            except Exception as exc:
                logs.append(f"[{index}/{total}] {action}: FAILED - {exc}")
                failed_step = (index, action, exc)
                if stop_on_error_bool:
                    break
                continue

            if (
                step_delay > 0
                and index < total
                and action not in ("wait", "wait_for")
            ):
                _sleep_with_checkpoint(float(step_delay), checkpoint)

        elapsed = time.monotonic() - started_at
        if failed_step is not None and stop_on_error_bool:
            header = (
                f"Sequence stopped at step {failed_step[0]}/{total} "
                f"({failed_step[1]}) after {elapsed:.2f}s: {failed_step[2]}"
            )
        elif failed_step is not None:
            header = f"Sequence finished with errors in {elapsed:.2f}s ({total} step(s)):"
        else:
            header = f"Sequence completed {total}/{total} step(s) in {elapsed:.2f}s:"

        summary = "\n".join([header, *logs])

        if failed_step is not None and stop_on_error_bool and final_capture == "none":
            raise RuntimeError(summary)

        capture_parts: list[Any] = []
        if final_capture != "none":
            try:
                capture_parts = _capture_final(
                    desktop,
                    final_capture=final_capture,
                    use_dom=use_dom_bool,
                    display=parsed_display,
                    region=parsed_region,
                )
            except Exception as capture_exc:
                if failed_step is not None and stop_on_error_bool:
                    raise RuntimeError(
                        f"{summary}\nFinal capture also failed: {capture_exc}"
                    ) from failed_step[2]
                raise

        if capture_parts and isinstance(capture_parts[0], str):
            combined_text = f"{summary}\n\n{capture_parts[0]}"
            return [combined_text, *capture_parts[1:]]
        return [summary, *capture_parts]
