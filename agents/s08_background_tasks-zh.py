#!/usr/bin/env python3
# Harness: 后台执行 -- 模型思考时，框架在等待
"""
s08_background_tasks-zh.py - 后台任务

在后台线程中运行命令。在每次 LLM 调用前清空通知队列以传递结果。

    主线程                      后台线程
    +-----------------+        +-----------------+
    | 智能体循环       |        | 任务执行        |
    | ...             |        | ...             |
    | [LLM 调用] <----+------- | 入队(结果)      |
    |  ^清空队列       |        +-----------------+
    +-----------------+

    时间线:
    智能体 ----[启动 A]----[启动 B]----[其他工作]----
                 |              |
                 v              v
              [A 运行]      [B 运行]        (并行)
                 |              |
                 +-- 通知队列 --> [结果注入]

核心洞见："发射后即忘 -- 命令运行时智能体不会阻塞。"
"""

import os
import subprocess
import threading
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"你是一个位于 {WORKDIR} 的编程智能体。使用 background_run 执行长时间运行的命令。"


# -- BackgroundManager: 线程执行 + 通知队列 --
class BackgroundManager:
    """后台任务管理器：管理后台线程执行和结果通知"""

    def __init__(self):
        self.tasks = {}  # task_id -> {status, result, command}
        self._notification_queue = []  # 已完成任务的结果
        self._lock = threading.Lock()

    def run(self, command: str) -> str:
        """启动后台线程，立即返回 task_id"""
        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.start()
        return f"后台任务 {task_id} 已启动: {command[:80]}"

    def _execute(self, task_id: str, command: str):
        """线程目标：运行子进程，捕获输出，推送到队列"""
        try:
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=300
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "错误: 超时 (300秒)"
            status = "timeout"
        except Exception as e:
            output = f"错误: {e}"
            status = "error"
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output or "(无输出)"
        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "result": (output or "(无输出)")[:500],
            })

    def check(self, task_id: str = None) -> str:
        """检查单个任务状态或列出所有任务"""
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"错误: 未知任务 {task_id}"
            return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(运行中)'}"
        lines = []
        for tid, t in self.tasks.items():
            lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
        return "\n".join(lines) if lines else "无后台任务。"

    def drain_notifications(self) -> list:
        """返回并清空所有待处理的通知"""
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs


BG = BackgroundManager()


# -- 工具实现 --
def safe_path(p: str) -> Path:
    """安全路径检查，防止路径逃逸"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径逃逸出工作空间: {p}")
    return path

def run_bash(command: str) -> str:
    """执行 bash 命令，带有安全检查（阻塞式）"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误: 危险命令已被阻止"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误: 超时 (120秒)"

def run_read(path: str, limit: int = None) -> str:
    """读取文件内容"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... (还有 {len(lines) - limit} 行)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误: {e}"

def run_write(path: str, content: str) -> str:
    """写入内容到文件"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"已写入 {len(content)} 字节"
    except Exception as e:
        return f"错误: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """替换文件中的精确文本"""
    try:
        fp = safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"错误: 在 {path} 中未找到文本"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误: {e}"


TOOL_HANDLERS = {
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "background_run":   lambda **kw: BG.run(kw["command"]),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
}

TOOLS = [
    {"name": "bash", "description": "执行 shell 命令（阻塞式）。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "写入内容到文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "替换文件中的精确文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "background_run", "description": "在后台线程中运行命令。立即返回 task_id。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "check_background", "description": "检查后台任务状态。省略 task_id 则列出所有任务。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
]


def agent_loop(messages: list):
    """主智能体循环，在 LLM 调用前注入后台通知"""
    while True:
        # 清空后台通知，在 LLM 调用前作为系统消息注入
        notifs = BG.drain_notifications()
        if notifs and messages:
            notif_text = "\n".join(
                f"[后台:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"未知工具: {block.name}"
                except Exception as e:
                    output = f"错误: {e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
