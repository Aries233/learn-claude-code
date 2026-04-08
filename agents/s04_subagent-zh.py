#!/usr/bin/env python3
# Harness: 上下文隔离 -- 保护模型思路的清晰度
"""
s04_subagent-zh.py - 子智能体

派生一个拥有全新 messages=[] 的子智能体。子智能体在自己的
上下文中工作，共享文件系统，然后仅向父智能体返回摘要。

    父智能体                         子智能体
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- 全新上下文
    |                  |   派发      |                  |
    | tool: task       | ---------->| while tool_use:  |
    |   prompt="..."   |             |   调用工具       |
    |   description="" |             |   追加结果       |
    |                  |   摘要      |                  |
    |   result = "..." | <--------- | 返回最后文本     |
    +------------------+             +------------------+
              |
    父智能体上下文保持干净。
    子智能体上下文被丢弃。

核心洞见："进程隔离天然带来上下文隔离。"
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

SYSTEM = f"你是一个位于 {WORKDIR} 的编程智能体。使用 task 工具来委托探索或子任务。"
SUBAGENT_SYSTEM = f"你是一个位于 {WORKDIR} 的编程子智能体。完成给定任务，然后总结你的发现。"


# -- 父子智能体共享的工具实现 --
def safe_path(p: str) -> Path:
    """安全路径检查，防止路径逃逸"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径逃逸出工作空间: {p}")
    return path

def run_bash(command: str) -> str:
    """执行 bash 命令，带有安全检查"""
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
    except (FileNotFoundError, OSError) as e:
        return f"错误: {e}"

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
        content = fp.read_text()
        if old_text not in content:
            return f"错误: 在 {path} 中未找到文本"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误: {e}"


TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# 子智能体获得除 task 外的所有基础工具（禁止递归派生）
CHILD_TOOLS = [
    {"name": "bash", "description": "执行 shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "写入内容到文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "替换文件中的精确文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]


# -- 子智能体: 全新上下文、过滤工具、仅返回摘要 --
def run_subagent(prompt: str) -> str:
    """运行子智能体，使用全新上下文"""
    sub_messages = [{"role": "user", "content": prompt}]  # 全新上下文
    for _ in range(30):  # 安全限制
        response = client.messages.create(
            model=MODEL, system=SUBAGENT_SYSTEM, messages=sub_messages,
            tools=CHILD_TOOLS, max_tokens=8000,
        )
        sub_messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"未知工具: {block.name}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
        sub_messages.append({"role": "user", "content": results})
    # 只有最终文本返回给父智能体 -- 子智能体上下文被丢弃
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(无摘要)"


# -- 父智能体工具: 基础工具 + 任务派发器 --
PARENT_TOOLS = CHILD_TOOLS + [
    {"name": "task", "description": "派生一个拥有全新上下文的子智能体。它共享文件系统但不共享对话历史。",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "description": {"type": "string", "description": "任务的简短描述"}}, "required": ["prompt"]}},
]


def agent_loop(messages: list):
    """主智能体循环"""
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=PARENT_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "task":
                    desc = block.input.get("description", "子任务")
                    prompt = block.input.get("prompt", "")
                    print(f"> 任务 ({desc}): {prompt[:80]}")
                    output = run_subagent(prompt)
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**block.input) if handler else f"未知工具: {block.name}"
                print(f"  {str(output)[:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
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
