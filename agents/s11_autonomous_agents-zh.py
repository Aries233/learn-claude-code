#!/usr/bin/env python3
# 套件: autonomy -- 自主寻找工作的模型
"""
s11_autonomous_agents-zh.py - 自主代理

空闲循环配合任务板轮询、自动认领未分配任务，以及
上下文压缩后的身份重新注入。基于 s10 的协议构建。

    队友生命周期:
    +-------+
    | 生成  |
    +---+---+
        |
        v
    +-------+  tool_use    +-------+
    | 工作  | <----------- |  LLM  |
    +---+---+              +-------+
        |
        | stop_reason != tool_use
        v
    +--------+
    | 空闲   | 每 5 秒轮询一次，最长 60 秒
    +---+----+
        |
        +---> 检查收件箱 -> 有消息? -> 恢复工作
        |
        +---> 扫描 .tasks/ -> 有未认领任务? -> 认领 -> 恢复工作
        |
        +---> 超时 (60秒) -> 关机

    上下文压缩后的身份重新注入:
    messages = [身份块, ...其余消息...]
    "你是 'coder'，角色: 后端，团队: my-team"

核心洞察: "代理自己发现工作。"
"""

import json
import os
import subprocess
import threading
import time
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
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TASKS_DIR = WORKDIR / ".tasks"

POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

SYSTEM = f"你是 {WORKDIR} 的团队负责人。队友是自主的 -- 他们自己发现工作。"

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

# -- 请求跟踪器 --
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()
_claim_lock = threading.Lock()


# -- MessageBus: 每个队友一个 JSONL 收件箱 --
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"错误: 无效的消息类型 '{msg_type}'。有效类型: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"已发送 {msg_type} 给 {to}"

    def read_inbox(self, name: str) -> list:
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))
        inbox_path.write_text("")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"已广播给 {count} 位队友"


BUS = MessageBus(INBOX_DIR)


# -- 任务板扫描 --
def scan_unclaimed_tasks() -> list:
    """扫描未认领的任务"""
    TASKS_DIR.mkdir(exist_ok=True)
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and not task.get("blockedBy")):
            unclaimed.append(task)
    return unclaimed


def claim_task(task_id: int, owner: str) -> str:
    """认领一个任务"""
    with _claim_lock:
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"错误: 任务 {task_id} 不存在"
        task = json.loads(path.read_text())
        if task.get("owner"):
            existing_owner = task.get("owner") or "其他人"
            return f"错误: 任务 {task_id} 已被 {existing_owner} 认领"
        if task.get("status") != "pending":
            status = task.get("status")
            return f"错误: 任务 {task_id} 无法认领，当前状态为 '{status}'"
        if task.get("blockedBy"):
            return f"错误: 任务 {task_id} 被其他任务阻塞，暂时无法认领"
        task["owner"] = owner
        task["status"] = "in_progress"
        path.write_text(json.dumps(task, indent=2))
    return f"已为 {owner} 认领任务 #{task_id}"


# -- 上下文压缩后的身份重新注入 --
def make_identity_block(name: str, role: str, team_name: str) -> dict:
    """创建身份注入块，用于上下文压缩后恢复身份"""
    return {
        "role": "user",
        "content": f"<identity>你是 '{name}'，角色: {role}，团队: {team_name}。继续你的工作。</identity>",
    }


# -- 自主 TeammateManager --
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        """设置队友状态"""
        member = self._find_member(name)
        if member:
            member["status"] = status
            self._save_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """生成一个自主队友线程"""
        member = self._find_member(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"错误: '{name}' 当前状态为 {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        thread = threading.Thread(
            target=self._loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return f"已生成 '{name}' (角色: {role})"

    def _loop(self, name: str, role: str, prompt: str):
        """队友主循环: 工作 -> 空闲轮询 -> 工作"""
        team_name = self.config["team_name"]
        sys_prompt = (
            f"你是 '{name}'，角色: {role}，团队: {team_name}，位于 {WORKDIR}。"
            f"当没有更多工作时使用 idle 工具。你将自动认领新任务。"
        )
        messages = [{"role": "user", "content": prompt}]
        tools = self._teammate_tools()

        while True:
            # -- 工作阶段: 标准代理循环 --
            for _ in range(50):
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg)})
                try:
                    response = client.messages.create(
                        model=MODEL,
                        system=sys_prompt,
                        messages=messages,
                        tools=tools,
                        max_tokens=8000,
                    )
                except Exception:
                    self._set_status(name, "idle")
                    return
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    break
                results = []
                idle_requested = False
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "idle":
                            idle_requested = True
                            output = "进入空闲阶段。将轮询新任务。"
                        else:
                            output = self._exec(name, block.name, block.input)
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output),
                        })
                messages.append({"role": "user", "content": results})
                if idle_requested:
                    break

            # -- 空闲阶段: 轮询收件箱消息和未认领任务 --
            self._set_status(name, "idle")
            resume = False
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)
            for _ in range(polls):
                time.sleep(POLL_INTERVAL)
                inbox = BUS.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break
                unclaimed = scan_unclaimed_tasks()
                if unclaimed:
                    task = unclaimed[0]
                    result = claim_task(task["id"], name)
                    if result.startswith("错误:"):
                        continue
                    task_prompt = (
                        f"<auto-claimed>任务 #{task['id']}: {task['subject']}\n"
                        f"{task.get('description', '')}</auto-claimed>"
                    )
                    if len(messages) <= 3:
                        messages.insert(0, make_identity_block(name, role, team_name))
                        messages.insert(1, {"role": "assistant", "content": f"我是 {name}。继续工作。"})
                    messages.append({"role": "user", "content": task_prompt})
                    messages.append({"role": "assistant", "content": f"已认领任务 #{task['id']}。正在处理。"})
                    resume = True
                    break

            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        """执行队友调用的工具"""
        # 基础工具与 s02 相同
        if tool_name == "bash":
            return _run_bash(args["command"])
        if tool_name == "read_file":
            return _run_read(args["path"])
        if tool_name == "write_file":
            return _run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return _run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            return json.dumps(BUS.read_inbox(sender), indent=2)
        if tool_name == "shutdown_response":
            # 关机响应
            req_id = args["request_id"]
            with _tracker_lock:
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = "approved" if args["approve"] else "rejected"
            BUS.send(
                sender, "lead", args.get("reason", ""),
                "shutdown_response", {"request_id": req_id, "approve": args["approve"]},
            )
            return f"关机已{'批准' if args['approve'] else '拒绝'}"
        if tool_name == "plan_approval":
            # 提交计划等待审批
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            with _tracker_lock:
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
            BUS.send(
                sender, "lead", plan_text, "plan_approval_response",
                {"request_id": req_id, "plan": plan_text},
            )
            return f"计划已提交 (request_id={req_id})。等待审批。"
        if tool_name == "claim_task":
            # 认领任务
            return claim_task(args["task_id"], sender)
        return f"未知工具: {tool_name}"

    def _teammate_tools(self) -> list:
        """队友可用的工具列表"""
        # 基础工具与 s02 相同
        return [
            {"name": "bash", "description": "运行 shell 命令。",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "读取文件内容。",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "将内容写入文件。",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "替换文件中的精确文本。",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "向队友发送消息。",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
            {"name": "read_inbox", "description": "读取并清空你的收件箱。",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "shutdown_response", "description": "回应关机请求。",
             "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}},
            {"name": "plan_approval", "description": "提交计划等待负责人审批。",
             "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
            {"name": "idle", "description": "表示你没有更多工作了。进入空闲轮询阶段。",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "claim_task", "description": "通过 ID 从任务板认领一个任务。",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        ]

    def list_all(self) -> str:
        """列出所有队友"""
        if not self.config["members"]:
            return "暂无队友。"
        lines = [f"团队: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(TEAM_DIR)


# -- 基础工具实现 (与 s02 相同) --
def _safe_path(p: str) -> Path:
    """安全路径检查，防止路径逃逸"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径超出工作区: {p}")
    return path


def _run_bash(command: str) -> str:
    """运行 bash 命令，拦截危险操作"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(d in command for d in dangerous):
        return "错误: 危险命令已被拦截"
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误: 超时 (120秒)"


def _run_read(path: str, limit: int = None) -> str:
    """读取文件内容"""
    try:
        lines = _safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... (还有 {len(lines) - limit} 行)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误: {e}"


def _run_write(path: str, content: str) -> str:
    """写入文件"""
    try:
        fp = _safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"已写入 {len(content)} 字节"
    except Exception as e:
        return f"错误: {e}"


def _run_edit(path: str, old_text: str, new_text: str) -> str:
    """编辑文件: 替换精确匹配的文本"""
    try:
        fp = _safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"错误: 未在 {path} 中找到指定文本"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误: {e}"


# -- 负责人专用协议处理器 --
def handle_shutdown_request(teammate: str) -> str:
    """向指定队友发送关机请求"""
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "请优雅地关闭。",
        "shutdown_request", {"request_id": req_id},
    )
    return f"关机请求 {req_id} 已发送给 '{teammate}'"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    """审批队友提交的计划"""
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"错误: 未知的计划请求 '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"来自 '{req['from']}' 的计划已{req['status']}"


def _check_shutdown_status(request_id: str) -> str:
    """查询关机请求状态"""
    with _tracker_lock:
        return json.dumps(shutdown_requests.get(request_id, {"error": "未找到"}))


# -- 负责人工具分发 (14 个工具) --
TOOL_HANDLERS = {
    "bash":              lambda **kw: _run_bash(kw["command"]),
    "read_file":         lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file":        lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file":         lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "spawn_teammate":    lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":    lambda **kw: TEAM.list_all(),
    "send_message":      lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":        lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":         lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request":  lambda **kw: handle_shutdown_request(kw["teammate"]),
    "shutdown_response": lambda **kw: _check_shutdown_status(kw.get("request_id", "")),
    "plan_approval":     lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    "idle":              lambda **kw: "负责人不会进入空闲状态。",
    "claim_task":        lambda **kw: claim_task(kw["task_id"], "lead"),
}

# 负责人可用工具列表 (基础工具与 s02 相同)
TOOLS = [
    {"name": "bash", "description": "运行 shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "将内容写入文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "替换文件中的精确文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "spawn_teammate", "description": "生成一个自主队友。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "列出所有队友。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "向队友发送消息。",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "读取并清空负责人的收件箱。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "向所有队友广播消息。",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "shutdown_request", "description": "请求队友关闭。",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "shutdown_response", "description": "查询关机请求状态。",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "plan_approval", "description": "批准或拒绝队友的计划。",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    {"name": "idle", "description": "进入空闲状态 (负责人使用 -- 较少使用)。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "claim_task", "description": "通过 ID 从任务板认领一个任务。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


def agent_loop(messages: list):
    """负责人的代理循环"""
    while True:
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
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
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
        if query.strip() == "/tasks":
            TASKS_DIR.mkdir(exist_ok=True)
            for f in sorted(TASKS_DIR.glob("task_*.json")):
                t = json.loads(f.read_text())
                marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
                owner = f" @{t['owner']}" if t.get("owner") else ""
                print(f"  {marker} #{t['id']}: {t['subject']}{owner}")
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
