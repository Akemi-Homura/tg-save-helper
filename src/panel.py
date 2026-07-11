from __future__ import annotations

import asyncio
import base64
import html
import json
import secrets
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .commands import CommandError, parse_command
from .db import WatchedSource


class PanelEvent:
    id = 0
    client = None

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def respond(self, text: str, reply_to: int | None = None) -> None:
        self.messages.append(text)


class PanelServer:
    def __init__(self, helper: Any) -> None:
        self.helper = helper
        self.config = helper.config
        self.loop = asyncio.get_running_loop()
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.config.panel_enabled:
            return
        handler = self._handler()
        self.server = ThreadingHTTPServer(
            (self.config.panel_host, self.config.panel_port), handler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                panel._serve(self, "GET")

            def do_POST(self) -> None:
                panel._serve(self, "POST")

        return Handler

    def _serve(self, request: BaseHTTPRequestHandler, method: str) -> None:
        if not self._authorized(request):
            request.send_response(HTTPStatus.UNAUTHORIZED)
            request.send_header("WWW-Authenticate", 'Basic realm="TG Helper"')
            request.end_headers()
            return
        parsed = urlsplit(request.path)
        base = self.config.panel_base_path.rstrip("/")
        if parsed.path == base:
            self._redirect(request, base + "/")
            return
        if not parsed.path.startswith(base + "/"):
            self._send(request, HTTPStatus.NOT_FOUND, "not found", "text/plain")
            return
        route = parsed.path[len(base) :].rstrip("/") or "/"
        try:
            if method == "GET" and route == "/healthz":
                self._send(request, HTTPStatus.OK, "ok", "text/plain")
                return
            if method == "GET" and route == "/":
                html_text = self._run(self._render(parse_qs(parsed.query)))
                self._send(request, HTTPStatus.OK, html_text, "text/html; charset=utf-8")
                return
            if method == "POST":
                form = self._form(request)
                message = self._run(self._action(route, form))
                self._redirect(request, base + "/?msg=" + self._quote(message))
                return
            self._send(request, HTTPStatus.NOT_FOUND, "not found", "text/plain")
        except Exception as exc:
            self._send(
                request,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                html.escape(str(exc)),
                "text/plain; charset=utf-8",
            )

    def _authorized(self, request: BaseHTTPRequestHandler) -> bool:
        username = self.config.panel_username
        password = self.config.panel_password
        if not username or not password:
            return False
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode()
            actual_user, actual_password = decoded.split(":", 1)
        except Exception:
            return False
        return secrets.compare_digest(actual_user, username) and secrets.compare_digest(
            actual_password, password
        )

    def _run(self, awaitable: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(awaitable, self.loop).result(timeout=30)

    @staticmethod
    def _form(request: BaseHTTPRequestHandler) -> dict[str, str]:
        length = int(request.headers.get("Content-Length") or "0")
        raw = request.rfile.read(length).decode()
        return {key: values[-1] for key, values in parse_qs(raw).items()}

    @staticmethod
    def _quote(text: str) -> str:
        from urllib.parse import quote

        return quote(text)

    @staticmethod
    def _send(
        request: BaseHTTPRequestHandler, status: HTTPStatus, body: str, content_type: str
    ) -> None:
        data = body.encode()
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    @staticmethod
    def _redirect(request: BaseHTTPRequestHandler, location: str) -> None:
        request.send_response(HTTPStatus.SEE_OTHER)
        request.send_header("Location", location)
        request.end_headers()

    async def _action(self, route: str, form: dict[str, str]) -> str:
        if route == "/command":
            return await self._start_command(form.get("command", ""))
        if route == "/task/stop":
            return await self._stop_task(form.get("task_id", ""), remove_pending=True)
        if route == "/task/pause":
            return await self._stop_task(form.get("task_id", ""), remove_pending=False)
        if route == "/task/restart":
            return await self._start_command(form.get("command", ""))
        if route == "/watch/pause":
            return self._pause_watch(form.get("source", ""), form.get("mode", ""))
        if route == "/watch/resume":
            return self._resume_watch(form.get("source", ""), form.get("mode", ""))
        if route == "/watch/stop":
            return self._stop_watch(form.get("source", ""), form.get("mode", ""))
        if route == "/savedwatch/stop":
            mode = form.get("mode", "")
            if mode not in {"backup", "stream"}:
                return "未知收藏监听"
            self.helper.db.set_saved_watch(mode, False, f"/unwatch{'' if mode == 'backup' else 'stream'}saved")
            return "已停止收藏监听"
        return "未知操作"

    async def _start_command(self, text: str) -> str:
        text = text.strip()
        if not text:
            return "命令为空"
        command = parse_command(text)
        if command is None:
            return "不是命令"
        asyncio.create_task(self.helper._execute_command(command, PanelEvent()))
        return f"已启动：{text}"

    async def _stop_task(self, task_id: str, *, remove_pending: bool) -> str:
        for task, description in list(self.helper.active_command_tasks.items()):
            if str(id(task)) != task_id or task.done():
                continue
            if remove_pending:
                self.helper.db.remove_pending_manual_command(
                    self.helper.active_pending_commands.get(task, description)
                )
            task.cancel()
            return ("已停止：" if remove_pending else "已暂停：") + description
        return "未找到任务"

    def _pause_watch(self, source: str, mode: str) -> str:
        watch = self._find_watch(source, mode)
        if watch is None:
            return "未找到监听"
        paused = self._paused_watches()
        key = self._watch_key(source, mode)
        paused[key] = asdict(watch)
        self.helper.db.set_state("panel_paused_watches", json.dumps(paused, ensure_ascii=False))
        self.helper.db.remove_watch(source=source, mode=mode)
        return f"已暂停监听：{source}"

    def _resume_watch(self, source: str, mode: str) -> str:
        paused = self._paused_watches()
        item = paused.pop(self._watch_key(source, mode), None)
        if item is None:
            return "未找到暂停监听"
        self.helper.db.add_watch(
            item["source"],
            int(item["peer_id"]),
            item["title"],
            mode=item.get("mode", "standard"),
            linked_peer_id=item.get("linked_peer_id"),
            linked_title=item.get("linked_title"),
        )
        self.helper.db.set_state("panel_paused_watches", json.dumps(paused, ensure_ascii=False))
        return f"已恢复监听：{source}"

    def _stop_watch(self, source: str, mode: str) -> str:
        paused = self._paused_watches()
        paused.pop(self._watch_key(source, mode), None)
        self.helper.db.set_state("panel_paused_watches", json.dumps(paused, ensure_ascii=False))
        removed = self.helper.db.remove_watch(source=source, mode=mode)
        return "已停止监听" if removed else "未找到监听"

    def _find_watch(self, source: str, mode: str) -> WatchedSource | None:
        for watch in self.helper.db.list_watches():
            if watch.source == source and watch.mode == mode:
                return watch
        return None

    def _paused_watches(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.helper.db.get_state("panel_paused_watches", "{}"))
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _watch_key(source: str, mode: str) -> str:
        return f"{mode}:{source}"

    async def _render(self, query: dict[str, list[str]]) -> str:
        active = self._active_tasks()
        active_commands = {
            value
            for item in active
            for value in (item.get("command"), item.get("checkpoint"))
            if value
        }
        pending = [
            command
            for command in self.helper.db.pending_manual_commands()
            if command not in active_commands
        ]
        watches = self.helper.db.list_watches()
        paused = list(self._paused_watches().values())
        stats = self._recent_forward_stats()
        latest_errors = self._latest_errors()
        msg = html.escape(query.get("msg", [""])[0])
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>TG Helper Panel</title>
<style>
*{{box-sizing:border-box}}
:root{{--bg:#eef2f7;--card:#ffffff;--text:#111827;--muted:#64748b;--line:#dbe3ee;--accent:#2563eb;--danger:#dc2626;--ok:#059669;--warn:#b45309}}
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:radial-gradient(circle at top left,#dbeafe 0,#eef2f7 280px);color:var(--text)}}
main{{max-width:1180px;margin:0 auto;padding:24px}}
.topbar{{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:18px}}
h1{{font-size:28px;line-height:1.1;margin:0;letter-spacing:-.04em}}h2{{font-size:15px;letter-spacing:.02em;margin:0 0 14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-bottom:16px}}
.card{{background:color-mix(in srgb,var(--card) 92%,transparent);border:1px solid var(--line);border-radius:16px;padding:16px;box-shadow:0 18px 50px #33415514;overflow:hidden}}
table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #e8eef6;padding:10px 8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{font-size:12px;color:var(--muted);font-weight:650}}
input,button{{font:inherit;padding:10px 12px;border-radius:10px;border:1px solid var(--line)}}input[type=text]{{width:100%;min-width:0;background:#f8fafc}}input:focus{{outline:2px solid #93c5fd;border-color:var(--accent)}}
button{{background:var(--accent);color:white;cursor:pointer;white-space:nowrap;border-color:transparent;font-weight:650}}button.secondary{{background:#f8fafc;color:#1e293b;border-color:var(--line)}}button.danger{{background:var(--danger)}}button:active{{transform:translateY(1px)}}
.muted{{color:var(--muted)}}.ok{{color:var(--ok)}}.bad{{color:var(--danger)}}.msg{{background:#eff6ff;border-color:#bfdbfe}}.pill{{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;padding:5px 9px;background:#f8fafc;color:var(--muted);font-size:12px}}
.metrics{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:12px}}.metric{{border:1px solid var(--line);border-radius:14px;padding:12px;background:#f8fafc}}.metric b{{display:block;font-size:22px;line-height:1.1}}.metric span{{font-size:12px;color:var(--muted)}}
.command-form{{display:flex;gap:8px;align-items:center}}form.inline{{display:inline-block;margin:2px}}pre{{white-space:pre-wrap}}
@media (max-width:720px){{
main{{padding:12px}}.topbar{{display:block;margin-bottom:12px}}h1{{font-size:24px;margin:6px 0 8px}}h2{{font-size:16px;margin:0 0 10px}}
.grid{{display:block;margin-bottom:0}}.card{{margin-bottom:12px;padding:12px}}
.metrics{{grid-template-columns:1fr}}.command-form{{display:block}}.command-form button{{width:100%;margin-top:8px}}
table,tbody,tr,td{{display:block;width:100%}}thead,th{{display:none}}tr{{border-bottom:1px solid #e5e7eb;padding:8px 0}}td{{border:0;padding:4px 0}}
form.inline button{{min-width:74px;margin-top:4px}}
}}
</style>
</head>
<body>
<main>
<div class="topbar"><h1>TG Helper 管理面板</h1><span class="pill">每 10 秒自动刷新</span></div>
{f'<div class="card msg">{msg}</div>' if msg else ''}
<div class="grid">
<section class="card"><h2>仪表盘</h2>{self._dashboard_html(active, watches, stats)}</section>
<section class="card"><h2>启动任务</h2>
<form class="command-form" method="post" action="{self.config.panel_base_path}/command">
<input name="command" type="text" placeholder="/status 或 /resource https://t.me/example all">
<button>启动</button>
</form>
<p class="muted">长任务会在后台执行；页面每 10 秒刷新。</p>
</section>
</div>
<div class="grid">
<section class="card"><h2>手动任务</h2>{self._tasks_html(active, pending)}</section>
<section class="card"><h2>监听任务</h2>{self._watches_html(watches, paused)}</section>
</div>
<section class="card"><h2>最近失败</h2>{self._errors_html(latest_errors)}</section>
</main>
</body></html>"""

    def _dashboard_html(
        self, active: list[dict[str, Any]], watches: list[WatchedSource], stats: dict[str, int]
    ) -> str:
        last_forward = html.escape(self.helper.db.get_state("last_forward_at", "无"))
        last_error = html.escape(self.helper.db.get_state("last_error", "无"))
        return f"""
<div class="metrics">
<div class="metric"><b>{len(active)}</b><span>活跃手动任务</span></div>
<div class="metric"><b>{len(watches) + len(self.helper.db.saved_watch_rows())}</b><span>监听任务</span></div>
<div class="metric"><b>{stats.get('success',0)}</b><span>24 小时成功</span></div>
</div>
<p>已登录：<b>{self.helper.owner_id}</b></p>
<p>最近 24 小时：<span class="ok">成功 {stats.get('success',0)}</span>，失败 {stats.get('failed',0)}，跳过 {stats.get('skipped',0)}</p>
<p>最近转发：{last_forward}</p>
<p>最近错误：<span class="bad">{last_error}</span></p>"""

    def _tasks_html(self, active: list[dict[str, Any]], pending: list[str]) -> str:
        rows = []
        for item in active:
            command = html.escape(item["command"])
            status = html.escape(item.get("state") or "执行中")
            current = html.escape(item.get("current") or "")
            checkpoint = html.escape(item.get("checkpoint") or "")
            checkpoint_html = (
                f"<br><span class='muted'>恢复断点：{checkpoint}</span>"
                if checkpoint and checkpoint != command
                else ""
            )
            rows.append(
                f"<tr><td>{command}<br><span class='muted'>{status} {current}</span>{checkpoint_html}</td>"
                f"<td>{item.get('processed',0)}/{item.get('total','未知')}</td>"
                f"<td>{self._task_buttons(item['id'], command)}</td></tr>"
            )
        for command in pending[:50]:
            escaped = html.escape(command)
            rows.append(
                f"<tr><td>{escaped}<br><span class='muted'>待恢复</span></td><td>-</td>"
                f"<td>{self._restart_button(escaped)}</td></tr>"
            )
        if not rows:
            return "<p class='muted'>无</p>"
        return "<table><tr><th>任务</th><th>进度</th><th>操作</th></tr>" + "".join(rows) + "</table>"

    def _task_buttons(self, task_id: str, command: str) -> str:
        base = self.config.panel_base_path
        return (
            f"<form class='inline' method='post' action='{base}/task/pause'>"
            f"<input type='hidden' name='task_id' value='{task_id}'><button class='secondary'>暂停</button></form> "
            f"<form class='inline' method='post' action='{base}/task/stop'>"
            f"<input type='hidden' name='task_id' value='{task_id}'><button class='danger'>停止</button></form> "
            f"{self._restart_button(command)}"
        )

    def _restart_button(self, command: str) -> str:
        return (
            f"<form class='inline' method='post' action='{self.config.panel_base_path}/task/restart'>"
            f"<input type='hidden' name='command' value='{command}'><button>重启</button></form>"
        )

    def _watches_html(
        self, watches: list[WatchedSource], paused: list[dict[str, Any]]
    ) -> str:
        rows = []
        for watch in watches:
            rows.append(self._watch_row(watch.source, watch.mode, watch.title, "运行中", paused=False))
        for watch in self.helper.db.saved_watch_rows():
            mode = str(watch["mode"])
            command = "/watchsaved" if mode == "backup" else "/watchstreamsaved"
            last = watch["last_message_id"] or "尚未开始"
            mode_e = html.escape(mode)
            rows.append(
                f"<tr><td>{command}<br><span class='muted'>我的收藏</span></td>"
                f"<td>运行中；最近消息 {last}</td><td>"
                f"<form class='inline' method='post' action='{self.config.panel_base_path}/savedwatch/stop'>"
                f"<input type='hidden' name='mode' value='{mode_e}'>"
                f"<button class='danger'>停止</button></form></td></tr>"
            )
        for item in paused:
            rows.append(
                self._watch_row(item["source"], item.get("mode", "standard"), item["title"], "已暂停", paused=True)
            )
        if not rows:
            return "<p class='muted'>无</p>"
        return "<table><tr><th>监听</th><th>状态</th><th>操作</th></tr>" + "".join(rows) + "</table>"

    def _watch_row(self, source: str, mode: str, title: str, state: str, *, paused: bool) -> str:
        base = self.config.panel_base_path
        source_e = html.escape(source)
        mode_e = html.escape(mode)
        title_e = html.escape(title)
        if paused:
            action = "resume"
            label = "恢复"
        else:
            action = "pause"
            label = "暂停"
        buttons = (
            f"<form class='inline' method='post' action='{base}/watch/{action}'>"
            f"<input type='hidden' name='source' value='{source_e}'>"
            f"<input type='hidden' name='mode' value='{mode_e}'><button class='secondary'>{label}</button></form> "
            f"<form class='inline' method='post' action='{base}/watch/stop'>"
            f"<input type='hidden' name='source' value='{source_e}'>"
            f"<input type='hidden' name='mode' value='{mode_e}'><button class='danger'>停止</button></form>"
        )
        return f"<tr><td>{title_e}<br><span class='muted'>{mode_e}: {source_e}</span></td><td>{state}</td><td>{buttons}</td></tr>"

    @staticmethod
    def _errors_html(rows: list[Any]) -> str:
        if not rows:
            return "<p class='muted'>无</p>"
        body = "".join(
            f"<tr><td>{html.escape(str(row['created_at']))}</td>"
            f"<td>{html.escape(str(row['source']))}/{row['message_id']}</td>"
            f"<td>{html.escape(str(row['status']))}</td>"
            f"<td>{html.escape(str(row['error'] or ''))}</td></tr>"
            for row in rows
        )
        return "<table><tr><th>时间</th><th>消息</th><th>状态</th><th>错误</th></tr>" + body + "</table>"

    def _active_tasks(self) -> list[dict[str, Any]]:
        items = []
        for task, command in self.helper.active_command_tasks.items():
            if task.done():
                continue
            status = self.helper.task_status.get(task, {})
            checkpoint = self.helper.active_pending_commands.get(task)
            items.append(
                {
                    "id": str(id(task)),
                    "command": command,
                    "checkpoint": checkpoint,
                    **status,
                }
            )
        return items

    def _recent_forward_stats(self) -> dict[str, int]:
        start = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        rows = self.helper.db.connection.execute(
            """SELECT status, COUNT(*) AS count
               FROM forwarding_logs
               WHERE created_at >= ?
               GROUP BY status""",
            (start,),
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def _latest_errors(self) -> list[Any]:
        return self.helper.db.connection.execute(
            """SELECT source, message_id, status, error, created_at
               FROM forwarding_logs
               WHERE status IN ('failed', 'skipped')
               ORDER BY id DESC
               LIMIT 20"""
        ).fetchall()
