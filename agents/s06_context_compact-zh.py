#!/usr/bin/env python3
# Harness: 压缩 -- 清理内存以实现无限会话
"""
s06_context_compact-zh.py - 上下文压缩

三层压缩流水线，让智能体可以永远工作下去：

    每一轮:
    +------------------+
    | 工具调用结果      |
    +------------------+
            |
            v
    [第一层: micro_compact]         (静默，每一轮都执行)
      将最近 3 个之前的非 read_file 工具结果
      替换为 "[Previous: used {tool_name}]"
            |
            v
    [检查: tokens > 50000?]
       |               |
       否              是
       |               |
       v               v
    继续执行     [第二层: auto_compact]
                  保存完整对话到 .transcripts/
                  请求 LLM 总结对话
                  用 [摘要] 替换所有消息
                        |
                        v
                [第三层: compact 工具]
                  模型调用 compact -> 立即压缩
                  与自动压缩相同，手动触发

核心洞见："智能体可以策略性地遗忘，从而永远工作下去。"
"""

import json
import os
import subprocess
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"你是一个位于 {WORKDIR} 的编程智能体。使用工具来完成任务。"

THRESHOLD = 50000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
KEEP_RECENT = 3
PRESERVE_RESULT_TOOLS = {"read_file"}


def estimate_tokens(messages: list) -> int:
    """估算 token 数量：约 4 个字符 = 1 个 token"""
    return len(str(messages)) // 4


# -- 第一层: micro_compact - 用占位符替换旧的工具结果 --
def micro_compact(messages: list) -> list:
    """
    微压缩：将旧的工具结果替换为简短占位符
    保留最近 KEEP_RECENT 个工具结果，以及 read_file 的输出
    """
    # 收集所有 tool_result 条目的 (消息索引, 部分索引, tool_result字典)
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))
    if len(tool_results) <= KEEP_RECENT:
        return messages
    # 通过匹配前一条 assistant 消息中的 tool_use_id 来找到每个结果对应的工具名
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name
    # 清理旧结果（保留最近 KEEP_RECENT 个）。保留 read_file 输出，因为
    # 它们是参考材料；压缩它们会迫使智能体重新读取文件。
    to_clear = tool_results[:-KEEP_RECENT]
    for _, _, result in to_clear:
        if not isinstance(result.get("content"), str) or len(result["content"]) <= 100:
            continue
        tool_id = result.get("tool_use_id", "")
        tool_name = tool_name_map.get(tool_id, "unknown")
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue
        result["content"] = f"[Previous: used {tool_name}]"
    return messages


# -- 第二层: auto_compact - 保存对话、总结、替换消息 --
def auto_compact(messages: list) -> list:
    """
    自动压缩：保存完整对话到磁盘，请求 LLM 总结，用摘要替换消息
    """
    # 保存完整对话到磁盘
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"[对话已保存: {transcript_path}]")
    # 请求 LLM 总结
    conversation_text = json.dumps(messages, default=str)[-80000:]
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content":
            "总结这段对话以便延续。包括："
            "1) 已完成的工作，2) 当前状态，3) 做出的关键决策。"
            "简洁但保留关键细节。\n\n" + conversation_text}],
        max_tokens=2000,
    )
    summary = next((block.text for block in response.content if hasattr(block, "text")), "")
    if not summary:
        summary = "未能生成摘要。"
    # 用压缩后的摘要替换所有消息
    return [
        {"role": "user", "content": f"[对话已压缩。完整记录: {transcript_path}]\n\n{summary}"},
    ]


# -- 工具实现 --
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
    "compact":    lambda **kw: "请求手动压缩。",
}

TOOLS = [
    {"name": "bash", "description": "执行 shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "写入内容到文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "替换文件中的精确文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "compact", "description": "触发手动对话压缩。",
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "在摘要中保留的内容重点"}}}},
]


def agent_loop(messages: list):
    """主智能体循环，包含三层压缩机制"""
    while True:
        # 第一层: 每次调用 LLM 前执行 micro_compact
        micro_compact(messages)
        # 第二层: 如果 token 估算超过阈值，执行 auto_compact
        if estimate_tokens(messages) > THRESHOLD:
            print("[触发自动压缩]")
            messages[:] = auto_compact(messages)
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        manual_compact = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compact":
                    manual_compact = True
                    output = "正在压缩..."
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output = handler(**block.input) if handler else f"未知工具: {block.name}"
                    except Exception as e:
                        output = f"错误: {e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})
        # 第三层: 由 compact 工具触发的手动压缩
        if manual_compact:
            print("[手动压缩]")
            messages[:] = auto_compact(messages)
            return


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms06 >> \033[0m")
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
