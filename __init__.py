"""KARDS AI Assistant: screenshot-guided desktop control for N.E.K.O."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image

from plugin.sdk.plugin import NekoPluginBase, Ok, SdkError, lifecycle, llm_tool, neko_plugin

PLUGIN_DIR = Path(__file__).resolve().parent
HELPER_PATH = PLUGIN_DIR / "helper" / "kards_helper.ps1"
ORB_PATH = PLUGIN_DIR / "helper" / "orb.ps1"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "N.E.K.O" / "kards-ai-assistant"
ORB_STATE = STATE_DIR / "state.json"
ORB_COMMAND = STATE_DIR / "command.json"
ORB_POSITION = STATE_DIR / "position.json"
DEFAULT_KNOWLEDGE_DIRS = (
    Path.home() / "Desktop" / "kards",
    STATE_DIR / "knowledge",
)
KNOWLEDGE_MAX_CHARS = 60000
KNOWLEDGE_PATTERNS = ("kards_db_*.md", "kards_rules_zh.md", "kards_dahuayu.md")

ACTIONS = ("click", "double_click", "right_click", "drag", "move", "wheel", "key", "focus", "wait")


def _json_image(data: str, *, max_width: int = 1400) -> dict[str, Any]:
    """Compress a helper data URL for the N.E.K.O. tool-image envelope."""
    if not isinstance(data, str) or not data.startswith("data:image/"):
        raise ValueError("helper returned an invalid image")
    encoded = data.split(",", 1)[1]
    image = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")
    original_width, original_height = image.size
    if image.width > max_width:
        height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, height), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=78, optimize=True)
    return {
        "data_b64": base64.b64encode(output.getvalue()).decode("ascii"),
        "mime": "image/jpeg",
        "image_width": image.width,
        "image_height": image.height,
        "source_width": original_width,
        "source_height": original_height,
    }


def _capture_request(value: dict[str, Any], *, max_width: int = 1400) -> dict[str, Any]:
    """Read a helper PNG path and add a model-sized image envelope."""
    path = value.get("path")
    if not isinstance(path, str) or not path:
        raise RuntimeError("helper did not return a capture path")
    try:
        raw = Path(path).read_bytes()
        image = _json_image(f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}", max_width=max_width)
        return {**value, **image}
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def _knowledge_priority(path: Path) -> tuple[int, str]:
    """Order the pack: rules first, then deck notes, then the card database.

    Knowledge is concatenated in this order and truncated at
    KNOWLEDGE_MAX_CHARS, so the files the agent must not lose come first.
    """
    name = path.name.lower()
    if "rules" in name:
        return (0, name)
    if "db_" in name:
        return (2, name)
    return (1, name)


def _find_knowledge_files() -> list[Path]:
    roots = [Path(os.path.expandvars(value)) for value in os.environ.get("KARDS_KNOWLEDGE_DIRS", "").split(os.pathsep) if value]
    roots.extend(DEFAULT_KNOWLEDGE_DIRS)
    found: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in KNOWLEDGE_PATTERNS:
            for path in root.glob(pattern):
                if path.is_file():
                    found[str(path.resolve()).lower()] = path
    return sorted(found.values(), key=_knowledge_priority)


def _load_knowledge() -> tuple[list[dict[str, str]], str]:
    files = _find_knowledge_files()
    blocks: list[dict[str, str]] = []
    used = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        remaining = KNOWLEDGE_MAX_CHARS - used
        if remaining <= 0:
            break
        text = text[:remaining]
        blocks.append({"name": path.name, "path": str(path), "content": text})
        used += len(text)
    context = "\n\n".join(f"### {item['name']}\n{item['content']}" for item in blocks)
    return blocks, context


def _stop_process(process: subprocess.Popen[str]) -> None:
    """Confirm exit before releasing a handle (and the orb's named mutex)."""
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass  # Still wait and attempt kill; neither API guarantees exit.
    try:
        process.wait(timeout=2)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("child process did not exit after terminate/kill") from exc


class HelperClient:
    """One serialized JSONL PowerShell helper for all desktop operations."""

    def __init__(self, logger):
        self.logger = logger
        self.process: subprocess.Popen[str] | None = None
        self._lock = asyncio.Lock()
        self._next_id = 1

    def _ensure(self) -> subprocess.Popen[str]:
        if self.process and self.process.poll() is None:
            return self.process
        self.close()
        if sys.platform != "win32":
            raise RuntimeError("KARDS AI Assistant requires Windows")
        self.process = subprocess.Popen(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(HELPER_PATH),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # No unread pipe can block the helper.
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return self.process

    async def call(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            # shot can focus a window; only these operations are side-effect free.
            retry_after_send = operation in {"ping", "info", "windows"}
            for attempt in range(2):
                write_attempted = False
                try:
                    process = self._ensure()
                    if process.stdin is None or process.stdout is None:
                        raise ConnectionError("helper pipes are unavailable")
                    request_id = self._next_id
                    self._next_id += 1
                    request = {"id": request_id, "op": operation, **(payload or {})}
                    encoded = json.dumps(request, ensure_ascii=False) + "\n"
                    # A write/flush failure may still have delivered some or all bytes.
                    write_attempted = True
                    process.stdin.write(encoded)
                    process.stdin.flush()
                    line = await asyncio.to_thread(process.stdout.readline)
                    if not line:
                        raise ConnectionError("helper exited unexpectedly")
                    response = json.loads(line)
                    if not isinstance(response, dict) or response.get("id") != request_id:
                        raise ConnectionError("helper response id mismatch")
                except (OSError, ValueError) as exc:
                    uncertain = write_attempted and not retry_after_send
                    try:
                        self.close()
                    except Exception as cleanup_error:
                        detail = "; execution result unknown; request not replayed" if uncertain else ""
                        raise RuntimeError(f"helper transport failed: {exc}; cleanup failed: {cleanup_error}{detail}") from exc
                    if uncertain:
                        raise RuntimeError(
                            f"helper {operation} execution result unknown; request not replayed: {exc}. "
                            "先截图确认动作是否生效，不要自动重复输入。"
                        ) from exc
                    if attempt:
                        raise RuntimeError(f"helper transport failed after restart: {exc}") from exc
                    self.logger.warning("helper transport failed; safe restart once: %s", exc)
                    continue
                if not response.get("ok"):
                    # An operation error is not a broken transport; do not replay it.
                    raise RuntimeError(str(response.get("error") or "helper operation failed"))
                value = response.get("data")
                return value if isinstance(value, dict) else {"value": value}
        raise RuntimeError("helper recovery exhausted")

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        _stop_process(process)
        self.process = None
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


@neko_plugin
class KardsAiAssistantPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.logger = ctx.logger
        self.helper = HelperClient(self.logger)
        self.last_capture: dict[str, Any] | None = None
        self.orb_process: subprocess.Popen[str] | None = None
        self.orb_task: asyncio.Task[None] | None = None
        self.pilot_session_id: str | None = None
        self.pilot_running = False
        self.knowledge_files: list[dict[str, str]] = []
        self.knowledge_context = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._startup_armed = False
        self._orb_last_at = 0.0
        self._orb_phase = "idle"
        self._orb_label = "点击开始 KARDS"
        self._orb_written_at = 0.0
        self._orb_error_until = 0.0
        self._orb_state_pending = False
        self._orb_next_write_at = 0.0

    @lifecycle(id="startup")
    async def startup(self, **_):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Commands belong to this session; the orb timestamps them in milliseconds.
        ORB_COMMAND.unlink(missing_ok=True)
        self._orb_last_at = time.time() * 1000
        self.knowledge_files, self.knowledge_context = _load_knowledge()
        self._write_orb_state("idle", "点击开始 KARDS")
        self._startup_armed = sys.platform == "win32" and ORB_PATH.is_file()
        if self._startup_armed:
            self._start_orb()
        # SDK lifecycle hooks use a transient asyncio.run loop.
        self._maybe_schedule_orb_polling()
        return Ok({"status": "ready", "plugin": self.plugin_id, "orb": self.orb_process is not None})

    async def _on_command_loop_start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._maybe_schedule_orb_polling()

    def _maybe_schedule_orb_polling(self) -> None:
        loop = self._loop
        if not self._startup_armed or loop is None or not loop.is_running():
            return

        def start() -> None:
            if self._startup_armed and (self.orb_task is None or self.orb_task.done()):
                self.orb_task = loop.create_task(self._poll_orb_commands())

        loop.call_soon_threadsafe(start)

    async def _stop_orb_polling(self) -> None:
        task = self.orb_task
        if task is None or task.done():
            self.orb_task = None
            return
        owner = task.get_loop()

        async def cancel_on_owner() -> None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        if owner is asyncio.get_running_loop():
            await cancel_on_owner()
        elif owner.is_running():
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(cancel_on_owner(), owner))
        else:
            raise RuntimeError("orb poller event loop stopped before shutdown")
        self.orb_task = None

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        self._startup_armed = False
        self.pilot_running = False
        try:
            await self._stop_orb_polling()
            self._loop = None
        finally:
            try:
                if self.orb_process is not None:
                    _stop_process(self.orb_process)
                    self.orb_process = None
            finally:
                self.helper.close()
                self._write_orb_state("idle", "已停止")
        return Ok({"status": "stopped"})

    @llm_tool(
        name="kards_knowledge",
        description="读取 KARDS 知识库文件。默认加载桌面 kards 文件夹或 KARDS_KNOWLEDGE_DIRS 中的 kards_db_*.md、kards_rules_zh.md、kards_dahuayu.md。",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "refresh": {"type": "boolean", "description": "重新扫描知识库文件", "default": False},
            },
        },
        timeout=20,
    )
    async def kards_knowledge(self, *, refresh: bool = False, **_):
        if refresh or not self.knowledge_files:
            self.knowledge_files, self.knowledge_context = _load_knowledge()
        return {
            "files": self.knowledge_files,
            "total_chars": len(self.knowledge_context),
            "context": self.knowledge_context,
        }

    @llm_tool(
        name="kards_windows",
        description="列出 Windows 桌面窗口，帮助定位 KARDS。只读，不会移动鼠标或改变窗口。",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "filter": {"type": "string", "description": "按窗口标题大小写不敏感过滤"},
            },
        },
        timeout=20,
    )
    async def kards_windows(self, *, filter: str = "", **_):
        try:
            value = await self.helper.call("windows", {})
            needle = str(filter or "").strip().lower()
            if needle:
                value["windows"] = [
                    item for item in value.get("windows", [])
                    if needle in str(item.get("title", "")).lower()
                ]
            info = await self.helper.call("info", {})
            value["screen"] = info.get("screen", {})
            value["cursor"] = info.get("cursor", {})
            return value
        except Exception as exc:
            return {"output": None, "is_error": True, "error": str(exc)}

    @llm_tool(
        name="kards_screen",
        description="截取整个 Windows 桌面或指定 KARDS 窗口，并把截图作为图片返回。默认最大宽度 1400。",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "window": {"type": "string", "description": "窗口标题子串，例如 KARDS；指定后会尝试恢复并置前"},
                "max_width": {"type": "integer", "description": "返回图片最大宽度，默认 1400", "minimum": 320, "maximum": 2400},
            },
        },
        timeout=30,
    )
    async def kards_screen(self, *, window: str = "", max_width: int = 1400, **_):
        try:
            path = str(self.data_path("captures", f"capture-{time.time_ns()}.png"))
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            value = await self.helper.call("shot", {"title": str(window or ""), "path": path, "maxWidth": int(max_width or 1400)})
            image = _capture_request(value, max_width=int(max_width or 1400))
            self.last_capture = {**value, **image}
            output = {key: value[key] for key in value if key != "path"}
            return {"output": output | {"image_width": image["image_width"], "image_height": image["image_height"]}, "images": [{"data_b64": image["data_b64"], "mime": image["mime"], "vision_prompt": "这是最近一次 KARDS 桌面截图。请只根据可见画面判断下一步。"}]}
        except Exception as exc:
            return {"output": None, "is_error": True, "error": str(exc)}

    @llm_tool(
        name="kards_act",
        description="控制真实 Windows 鼠标和键盘来操作 KARDS，不是模拟器。默认 x/y 是最近截图的 image 坐标；只有 space=screen 才直接使用桌面坐标。动作后默认自动返回新截图。",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["action"],
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "to_x": {"type": "integer"},
                "to_y": {"type": "integer"},
                "space": {"type": "string", "enum": ["image", "screen"], "default": "image"},
                "keys": {"type": "string", "description": "key 动作的按键，如 enter、esc、ctrl+s、1"},
                "delta": {"type": "integer", "description": "wheel 滚轮量，负数向下，默认 -120"},
                "window": {"type": "string", "description": "focus 的窗口标题子串"},
                "ms": {"type": "integer", "description": "wait 等待时间，或动作后截图延迟，默认 350"},
                "look": {"type": "boolean", "description": "动作后截图，默认 true；批量动作时可设 false"},
            },
        },
        timeout=45,
    )
    async def kards_act(
        self,
        *,
        action: str,
        x: int | None = None,
        y: int | None = None,
        to_x: int | None = None,
        to_y: int | None = None,
        space: str = "image",
        keys: str = "",
        delta: int = -120,
        window: str = "",
        ms: int = 350,
        look: bool = True,
        **_,
    ):
        try:
            if action not in ACTIONS:
                raise SdkError(f"不支持的动作: {action}")
            if action in {"click", "double_click", "right_click", "drag", "move"} and (x is None or y is None):
                raise SdkError(f"动作 {action} 需要 x 和 y")
            if action == "drag" and (to_x is None or to_y is None):
                raise SdkError("drag 需要 to_x 和 to_y")
            point = self._map_point(x, y, space)
            end = self._map_point(to_x, to_y, space) if action == "drag" else None
            if action == "focus":
                value = await self.helper.call("focus", {"title": window})
            elif action == "wait":
                value = await self.helper.call("sleep", {"ms": max(0, int(ms))})
            elif action == "key":
                value = await self.helper.call("key", {"keys": keys})
            elif action == "wheel":
                value = await self.helper.call("wheel", {"delta": int(delta)})
            elif action == "drag":
                value = await self.helper.call("drag", {"x": point[0], "y": point[1], "toX": end[0], "toY": end[1]})
            else:
                value = await self.helper.call("mouse", {"action": action, "x": point[0], "y": point[1]})
            if not look:
                return value
            if action != "wait":
                await asyncio.sleep(max(0, int(ms)) / 1000)
            path = str(self.data_path("captures", f"capture-{time.time_ns()}.png"))
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            capture = await self.helper.call("shot", {"title": window, "path": path, "maxWidth": 1400})
            image = _capture_request(capture, max_width=1400)
            self.last_capture = {**capture, **image}
            output_capture = {key: capture[key] for key in capture if key != "path"}
            output = value | {"capture": output_capture | {"image_width": image["image_width"], "image_height": image["image_height"]}}
            return {
                "output": output,
                "images": [{"data_b64": image["data_b64"], "mime": image["mime"], "vision_prompt": "这是动作完成后的 KARDS 截图，请确认动作是否生效。"}],
            }
        except Exception as exc:
            return {"output": None, "is_error": True, "error": str(exc)}

    def _map_point(self, x: int | None, y: int | None, space: str) -> tuple[int, int] | None:
        if x is None or y is None:
            return None
        if space == "screen":
            return int(x), int(y)
        if space != "image":
            raise SdkError("space 必须是 image 或 screen")
        if not self.last_capture:
            raise SdkError("image 坐标需要先调用 kards_screen 截图，或显式指定 space=screen")
        return (
            round(int(self.last_capture.get("regionX", 0)) + int(x) * float(self.last_capture.get("scaleX", 1))),
            round(int(self.last_capture.get("regionY", 0)) + int(y) * float(self.last_capture.get("scaleY", 1))),
        )

    def _write_orb_state(self, phase: str, label: str) -> None:
        now = time.monotonic()
        # Keep the latest desired state even when a Windows reader denies replace.
        if phase == "error" and self._orb_phase != "error":
            self._orb_error_until = now + 3
        elif phase != "error":
            self._orb_error_until = 0.0
        self._orb_phase, self._orb_label = phase, label
        self._orb_state_pending = True
        if now < self._orb_next_write_at:
            return
        temporary = ORB_STATE.with_suffix(".tmp")
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            payload = {"phase": phase, "label": label, "at": time.time()}
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(ORB_STATE)
        except OSError as exc:
            # One bounded attempt per poll; never turn a display failure into a
            # command failure, or retry a toggle that has already been consumed.
            self._orb_next_write_at = now + 0.25
            self.logger.warning("orb state write deferred: %s", exc)
            return
        self._orb_written_at = time.monotonic()
        self._orb_next_write_at = 0.0
        self._orb_state_pending = False

    def _refresh_orb_state(self) -> None:
        now = time.monotonic()
        if self._orb_phase == "error" and now >= self._orb_error_until:
            self._write_orb_state(
                "active" if self.pilot_running else "idle",
                "结束对局" if self.pilot_running else "点击开始 KARDS",
            )
        elif self._orb_state_pending or now - self._orb_written_at >= 3:
            self._write_orb_state(self._orb_phase, self._orb_label)

    def _start_orb(self) -> None:
        if self.orb_process and self.orb_process.poll() is None:
            return
        self.orb_process = subprocess.Popen(
            [
                "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(ORB_PATH),
                "-StatePath", str(ORB_STATE),
                "-CommandPath", str(ORB_COMMAND),
                "-PosPath", str(ORB_POSITION),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    async def _poll_orb_commands(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            try:
                # Refresh even with no command, or the orb treats a live host as absent.
                self._refresh_orb_state()
                if not ORB_COMMAND.is_file():
                    continue
                command = json.loads(ORB_COMMAND.read_text(encoding="utf-8"))
                at = float(command.get("at", 0))
                if at <= self._orb_last_at:
                    continue
                self._orb_last_at = at
                action = command.get("action")
                if action == "toggle":
                    await self._toggle_pilot()
                elif action == "wake":
                    if not self.pilot_running:
                        await self._toggle_pilot()
                elif action == "stop":
                    if self.pilot_running:
                        await self._toggle_pilot()
                else:
                    raise ValueError(f"unsupported orb action: {action!r}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("orb command failed: %s", exc)
                try:
                    self._write_orb_state("error", str(exc))
                except Exception as state_error:
                    self.logger.warning("orb error state unavailable: %s", state_error)

    async def _toggle_pilot(self) -> None:
        if self.pilot_running:
            self.pilot_running = False
            self._write_orb_state("idle", "点击开始 KARDS")
            self.push_message(
                source="kards_ai_assistant",
                visibility=[],
                ai_behavior="respond",
                parts=[{"type": "text", "text": "KARDS AI 助手已停止继续操作。不要再调用 kards_act，等待用户下一次明确授权。"}],
                priority=8,
                coalesce_key="kards_pilot_stop",
            )
            return
        self.pilot_running = True
        self._write_orb_state("waking", "唤醒中...")
        try:
            receipt = self.push_message(
                source="kards_ai_assistant",
                visibility=[],
                ai_behavior="respond",
                parts=[{"type": "text", "text": PLAYBOOK + ("\n\n【KARDS 知识库】\n" + self.knowledge_context if self.knowledge_context else "\n\n【KARDS 知识库】未找到匹配的 Markdown 文件，请先准备 kards_db_*.md、kards_rules_zh.md、kards_dahuayu.md。")}],
                priority=8,
                coalesce_key="kards_pilot_start",
            )
            if getattr(receipt, "submitted", True) is False:
                raise RuntimeError("N.E.K.O 未接受悬浮球唤醒消息")
        except Exception:
            self.pilot_running = False
            raise
        self._write_orb_state("active", "结束对局")


PLAYBOOK = """【KARDS 操控任务】用户主动点击了悬浮球，授权你接管这台电脑上的 KARDS 对局。只打训练模式，不要开始排位。

## 一、开场（只做一次）
1. `kards_windows` 找标题含 KARDS 的窗口，记下 rect 和是否最小化。
2. `kards_act(action="focus", window="KARDS")` 切前台；最小化的窗口会自动还原。
3. `kards_screen(window="KARDS")` 看画面，判断现在处在主菜单、选卡组、调度、对局中还是结算，然后从当前状态接着打，不要重新开一局。

## 二、每一步的固定循环
`kards_screen` 看局面 → 心里写一句「局面 + 这一步要做什么」 → `kards_act` 执行 → 看返回的截图确认生效 → 下一步。

- `kards_act` 默认动作后自动返回新截图（`look=true`），不必再单独截一次。
- 只想看牌不操作：`kards_act(action="move", x, y)` 悬停到卡上触发游戏内 tooltip，再 `kards_screen` 读中文效果。这是你最主要的侦察手段。
- 出牌/攻击动画、弹窗：先 `kards_act(action="wait", ms=1500)` 再截图。
- 坐标默认是「最近一张截图的像素坐标」，不要自己换算成屏幕坐标；只有确定要用桌面绝对坐标时才传 `space="screen"`。
- 所有鼠标键盘操作只用 `kards_act`，不要自己写脚本或用别的方式注入输入。

## 三、看不清就不要点
宁可多悬停一次、多截一张图，也不要盲猜坐标。点错一次的代价（打错目标、误结束回合）远大于多截一张图。

## 四、决策优先级
1. 能斩杀就打脸，先算清总伤害。
2. 对面下回合能斩杀你 → 解场或抢前线。
3. 前线空着 → 派单位占住（前线是单向的，进去就出不来）。
4. 有富余指挥点 → 铺单位。
5. 都没有 → 结束回合，不要为了花而花。

## 五、三条铁律
- 只有坦克能「移动后接攻击」；其他单位移动了这回合就不能打。
- 打 HQ 前先确认没有 Guard 挡着；被守护的目标只能被轰炸机和炮兵攻击。
- 炮兵、轰炸机、战斗机待在后排就能攻击，别把它们派上前线。

## 六、知识库
下方附带中文规则速查、卡组与打法手册、日本卡牌数据。规则冲突时以游戏内实际提示为准。卡名在游戏里是中文，数据库是英文原名，不要用中文名去数据库里检索。

## 七、结束
结算画面出现后：汇报胜负、关键转折和这局的失误，然后停止操作等用户下一次授权。用户再次点悬浮球 = 停止。"""
