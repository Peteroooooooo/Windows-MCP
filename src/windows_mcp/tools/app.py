"""App tool — launch, resize, switch applications."""

import json
import os
import subprocess
import winreg
from pathlib import Path
from typing import Literal

from mcp.types import ToolAnnotations
from windows_mcp.desktop.utils import as_int_list as _as_int_list
from windows_mcp.infrastructure import with_analytics
from fastmcp import Context


def _as_args(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        args = value
    else:
        args = json.loads(value)
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("args must be a list of strings")
    return args


def _resolve_from_path(command: str) -> Path | None:
    path_env = os.environ.get("PATH", "")
    if not path_env:
        return None
    pathext = [
        ext for ext in (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if ext
    ]
    lower_cmd = command.lower()
    candidates = [command] if any(lower_cmd.endswith(ext.lower()) for ext in pathext) else []
    candidates.extend(command + ext for ext in pathext)
    for directory in path_env.split(os.pathsep):
        if not directory or directory == ".":
            continue
        dir_path = Path(directory)
        for name in candidates:
            candidate = dir_path / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
    return None


def _resolve_from_app_paths(command: str) -> Path | None:
    subkey_base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    names = [command] if command.lower().endswith(".exe") else [f"{command}.exe", command]
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for name in names:
            try:
                with winreg.OpenKey(root, f"{subkey_base}\\{name}") as key:
                    raw_value, _ = winreg.QueryValueEx(key, None)
            except OSError:
                continue
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            expanded = os.path.expandvars(raw_value.strip().strip('"'))
            candidate = Path(expanded).expanduser()
            if candidate.is_file():
                return candidate.resolve()
    return None


def _resolve_executable(executable: str) -> Path:
    stripped = executable.strip()
    if not stripped:
        raise ValueError("Executable path must not be empty")
    has_path_hint = any(sep in stripped for sep in ("/", "\\", ":")) or stripped.startswith((".", "~"))
    if not has_path_hint:
        found = _resolve_from_path(stripped) or _resolve_from_app_paths(stripped)
        if found is not None:
            return found
    path = Path(stripped).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Executable does not exist: {path}")
    return path


def _resolve_cwd(cwd: str | None) -> Path | None:
    if cwd is None:
        return None
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Working directory does not exist: {path}")
    return path


def _launch_executable(
    executable: str,
    args: list[str] | str | None,
    cwd: str | None,
) -> str:
    resolved_executable = _resolve_executable(executable)
    resolved_cwd = _resolve_cwd(cwd)
    resolved_args = _as_args(args)

    process = subprocess.Popen(
        [str(resolved_executable), *resolved_args],
        cwd=str(resolved_cwd) if resolved_cwd is not None else None,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return json.dumps(
        {
            "pid": process.pid,
            "executable": str(resolved_executable),
            "args": resolved_args,
            "cwd": str(resolved_cwd) if resolved_cwd is not None else None,
        },
        indent=2,
    )


def execute_app(
    desktop,
    *,
    mode: Literal["launch", "launch_executable", "resize", "switch"] = "launch",
    name: str | None = None,
    window_loc: list[int] | str | None = None,
    window_size: list[int] | str | None = None,
    executable: str | None = None,
    args: list[str] | str | None = None,
    cwd: str | None = None,
) -> str:
    exact_launch_inputs = (executable, args, cwd)
    if mode != "launch_executable" and any(value is not None for value in exact_launch_inputs):
        raise ValueError('executable, args, and cwd require mode="launch_executable"')

    if mode == "launch_executable":
        if executable is None:
            raise ValueError('executable is required for mode="launch_executable"')
        if name is not None or window_loc is not None or window_size is not None:
            raise ValueError(
                "name, window_loc, and window_size are not supported for "
                'mode="launch_executable"'
            )
        return _launch_executable(executable, args, cwd)

    window_loc = _as_int_list(window_loc, "window_loc")
    window_size = _as_int_list(window_size, "window_size")
    return desktop.app(mode, name, window_loc, window_size)


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="App",
        description=(
            "Open/start/launch applications and manage windows. Keywords: open, start, launch, program, "
            "application, window, foreground, focus, resize. Four modes: 'launch' (opens an application "
            "by Start Menu name), 'launch_executable' (strictly launches one executable path with separated "
            "argv and optional cwd), 'resize' (adjusts a named or active window), and 'switch' (brings a "
            "specific window into focus). For multi-step flows (launch/switch followed by wait_for/shortcuts/typing), "
            "prefer using 'app' steps directly inside Sequence. Prefer background execution when supported. If launching an application is known to take focus or disrupt the user, obtain permission first, reusing existing authorization. Relaxed policy does not gate launch modes; this guidance is not server-enforced approval."
        ),
        annotations=ToolAnnotations(
            title="App",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "App-Tool", run_sync_in_thread=False)
    def app_tool(
        mode: Literal["launch", "launch_executable", "resize", "switch"] = "launch",
        name: str | None = None,
        window_loc: list[int] | str | None = None,
        window_size: list[int] | str | None = None,
        executable: str | None = None,
        args: list[str] | str | None = None,
        cwd: str | None = None,
        ctx: Context = None,
    ):
        return execute_app(
            get_desktop(),
            mode=mode,
            name=name,
            window_loc=window_loc,
            window_size=window_size,
            executable=executable,
            args=args,
            cwd=cwd,
        )
