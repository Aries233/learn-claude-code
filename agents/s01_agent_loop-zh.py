#!/usr/bin/env python3
# 智能体框架：循环 —— 模型与真实世界的首次连接。
"""
s01_agent_loop-zh.py - 智能体循环

AI 编码智能体的全部秘密就在这一个模式中：

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        执行工具
        追加结果

    +----------+      +-------+      +---------+
    |   用户   | ---> |  LLM  | ---> |  工具   |
    |   提示   |      |       |      |  执行   |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   工具结果    |
                          +---------------+
                          （循环继续）

这就是核心循环：将工具结果反馈给模型，
直到模型决定停止。生产级智能体在此基础上
叠加策略、钩子和生命周期控制。
"""

import os
import subprocess

try:
    import readline
    # #143 macOS libedit 的 UTF-8 退格修复
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
    readline.parse_and_bind('set enable-meta-keybindings on')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"你是在 {os.getcwd()} 的编码智能体。使用 bash 来完成任务。直接行动，不要解释。"

TOOLS = [{
    "name": "bash",
    "description": "运行 shell 命令。",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误：危险命令已被阻止"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误：超时（120秒）"
    except (FileNotFoundError, OSError) as e:
        return f"错误：{e}"


# -- 核心模式：一个 while 循环，调用工具直到模型停止 --
def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 追加助手轮次
        messages.append({"role": "assistant", "content": response.content})
        # 如果模型没有调用工具，则完成
        if response.stop_reason != "tool_use":
            return
        # 执行每个工具调用，收集结果
        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\033[33m$ {block.input['command']}\033[0m")
                output = run_bash(block.input["command"])
                print(output[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
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
