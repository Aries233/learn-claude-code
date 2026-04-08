#!/usr/bin/env python3
# 框架：规划——让模型保持正轨，而无需预先设定路线。
"""
s03_todo_write-zh.py - 待办事项写入

模型通过 TodoManager 追踪自己的进度。一个提醒机制会在模型忘记更新时强制它继续更新。

    +----------+      +-------+      +---------+
    |   用户   | ---> |  LLM  | ---> | 工具    |
    |  提示词  |      |       |      | + todo  |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   工具结果    |
                          +---------------+
                                |
                    +-----------+-----------+
                    | TodoManager 状态      |
                    | [ ] 任务 A            |
                    | [>] 任务 B <- 进行中  |
                    | [x] 任务 C            |
                    +-----------------------+
                                |
                    if rounds_since_todo >= 3:
                      注入 <reminder>

核心洞见："智能体可以追踪自己的进度——而且我能看到它。"
"""

import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"""你是位于 {WORKDIR} 的编程智能体。
使用 todo 工具来规划多步骤任务。开始前标记为 in_progress，完成后标记为 completed。
优先使用工具而非冗长的解释。"""


# -- TodoManager：LLM 写入的结构化状态 --
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        """更新待办事项列表。"""
        if len(items) > 20:
            raise ValueError("最多允许 20 个待办事项")
        validated = []
        in_progress_count = 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            if not text:
                raise ValueError(f"项目 {item_id}：需要文本内容")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"项目 {item_id}：无效状态 '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress_count > 1:
            raise ValueError("同一时间只能有一个任务处于 in_progress 状态")
        self.items = validated
        return self.render()

    def render(self) -> str:
        """渲染待办事项列表。"""
        if not self.items:
            return "暂无待办事项。"
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n(已完成 {done}/{len(self.items)})")
        return "\n".join(lines)


TODO = TodoManager()


# -- 工具实现 --
def safe_path(p: str) -> Path:
    """确保路径不会逃逸出工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径逃逸出工作空间: {p}")
    return path

def run_bash(command: str) -> str:
    """执行 shell 命令，带安全检查。"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误：危险命令已被阻止"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误：超时 (120秒)"

def run_read(path: str, limit: int = None) -> str:
    """读取文件内容。"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... (还有 {len(lines) - limit} 行)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误：{e}"

def run_write(path: str, content: str) -> str:
    """将内容写入文件。"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"已写入 {len(content)} 字节"
    except Exception as e:
        return f"错误：{e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """在文件中替换精确匹配的文本。"""
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"错误：在 {path} 中未找到该文本"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误：{e}"


TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo":       lambda **kw: TODO.update(kw["items"]),
}

TOOLS = [
    {"name": "bash", "description": "执行 shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "将内容写入文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "在文件中替换精确匹配的文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "todo", "description": "更新任务列表。追踪多步骤任务的进度。",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["id", "text", "status"]}}}, "required": ["items"]}},
]


# -- 带提醒注入的智能体循环 --
def agent_loop(messages: list):
    """智能体主循环：带待办事项提醒机制。"""
    rounds_since_todo = 0
    while True:
        # 提醒在下方注入，与工具结果一起
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        used_todo = False
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"未知工具: {block.name}"
                except Exception as e:
                    output = f"错误：{e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                if block.name == "todo":
                    used_todo = True
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        if rounds_since_todo >= 3:
            results.append({"type": "text", "text": "<reminder>更新你的待办事项。</reminder>"})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms03 >> \033[0m")
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
