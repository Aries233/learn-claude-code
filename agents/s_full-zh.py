#!/usr/bin/env python3
# 套件: 所有机制融合 -- 模型的完整驾驶舱
"""
s_full-zh.py - 完整参考代理

整合 s01-s11 所有机制的综合实现。
课程 s12 (任务感知的工作树隔离) 单独教学。
这不是教学课程 -- 这是"融会贯通"的参考实现。

    +------------------------------------------------------------------+
    |                        完整代理                                    |
    |                                                                   |
    |  系统提示词 (s05 技能, 任务优先 + 可选待办提醒)                    |
    |                                                                   |
    |  每次 LLM 调用前:                                                 |
    |  +--------------------+  +------------------+  +--------------+   |
    |  | 微压缩 (s06)       |  | 排空后台 (s08)   |  | 检查收件箱   |   |
    |  | 自动压缩 (s06)     |  | 通知队列         |  | (s09)        |   |
    |  +--------------------+  +------------------+  +--------------+   |
    |                                                                   |
    |  工具分发 (s02 模式):                                             |
    |  +--------+----------+----------+---------+-----------+           |
    |  | bash   | read     | write    | edit    | TodoWrite |           |
    |  | task   | load_sk  | compress | bg_run  | bg_check  |           |
    |  | t_crt  | t_get    | t_upd    | t_list  | spawn_tm  |           |
    |  | list_tm| send_msg | rd_inbox | bcast   | shutdown  |           |
    |  | plan   | idle     | claim    |         |           |           |
    |  +--------+----------+----------+---------+-----------+           |
    |                                                                   |
    |  子代理 (s04):  生成 -> 工作 -> 返回摘要                          |
    |  队友 (s09):    生成 -> 工作 -> 空闲 -> 自动认领 (s11)            |
    |  关机 (s10):    request_id 握手协议                                |
    |  计划门控 (s10): 提交 -> 批准/拒绝                                 |
    +------------------------------------------------------------------+

    REPL 命令: /compact /tasks /team /inbox
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from queue import Queue

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
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOKEN_THRESHOLD = 100000
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

VALID_MSG_TYPES = {"message", "broadcast", "shutdown_request",
                   "shutdown_response", "plan_approval_response"}


# === 章节: 基础工具 ===
def safe_path(p: str) -> Path:
    """安全路径检查，防止路径逃逸"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径超出工作区: {p}")
    return path

def run_bash(command: str) -> str:
    """运行 bash 命令，拦截危险操作"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误: 危险命令已被拦截"
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
    """写入文件"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"已写入 {len(content)} 字节到 {path}"
    except Exception as e:
        return f"错误: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """编辑文件: 替换精确匹配的文本"""
    try:
        fp = safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"错误: 未在 {path} 中找到指定文本"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误: {e}"


# === 章节: 待办事项 (s03) ===
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        """更新待办事项列表"""
        validated, ip = [], 0
        for i, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            af = str(item.get("activeForm", "")).strip()
            if not content: raise ValueError(f"第 {i} 项: content 为必填")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"第 {i} 项: 无效状态 '{status}'")
            if not af: raise ValueError(f"第 {i} 项: activeForm 为必填")
            if status == "in_progress": ip += 1
            validated.append({"content": content, "status": status, "activeForm": af})
        if len(validated) > 20: raise ValueError("最多 20 个待办事项")
        if ip > 1: raise ValueError("只允许一个进行中的任务")
        self.items = validated
        return self.render()

    def render(self) -> str:
        """渲染待办事项列表"""
        if not self.items: return "暂无待办事项。"
        lines = []
        for item in self.items:
            m = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(item["status"], "[?]")
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            lines.append(f"{m} {item['content']}{suffix}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} 已完成)")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        """是否有未完成的待办事项"""
        return any(item.get("status") != "completed" for item in self.items)


# === 章节: 子代理 (s04) ===
def run_subagent(prompt: str, agent_type: str = "Explore") -> str:
    """运行子代理完成独立探索或工作"""
    sub_tools = [
        {"name": "bash", "description": "运行命令。",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "description": "读取文件。",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    ]
    if agent_type != "Explore":
        sub_tools += [
            {"name": "write_file", "description": "写入文件。",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "编辑文件。",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
        ]
    sub_handlers = {
        "bash": lambda **kw: run_bash(kw["command"]),
        "read_file": lambda **kw: run_read(kw["path"]),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    }
    sub_msgs = [{"role": "user", "content": prompt}]
    resp = None
    for _ in range(30):
        resp = client.messages.create(model=MODEL, messages=sub_msgs, tools=sub_tools, max_tokens=8000)
        sub_msgs.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                h = sub_handlers.get(b.name, lambda **kw: "未知工具")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(h(**b.input))[:50000]})
        sub_msgs.append({"role": "user", "content": results})
    if resp:
        return "".join(b.text for b in resp.content if hasattr(b, "text")) or "(无摘要)"
    return "(子代理失败)"


# === 章节: 技能 (s05) ===
class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills = {}
        if skills_dir.exists():
            for f in sorted(skills_dir.rglob("SKILL.md")):
                text = f.read_text()
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                if match:
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    body = match.group(2).strip()
                name = meta.get("name", f.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

    def descriptions(self) -> str:
        """列出所有可用技能的描述"""
        if not self.skills: return "(无技能)"
        return "\n".join(f"  - {n}: {s['meta'].get('description', '-')}" for n, s in self.skills.items())

    def load(self, name: str) -> str:
        """按名称加载技能"""
        s = self.skills.get(name)
        if not s: return f"错误: 未知技能 '{name}'。可用: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"


# === 章节: 上下文压缩 (s06) ===
def estimate_tokens(messages: list) -> int:
    """估算消息的 token 数量"""
    return len(json.dumps(messages, default=str)) // 4

def microcompact(messages: list):
    """微压缩: 清理旧的 tool_result 内容"""
    indices = []
    for i, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    indices.append(part)
    if len(indices) <= 3:
        return
    for part in indices[:-3]:
        if isinstance(part.get("content"), str) and len(part["content"]) > 100:
            part["content"] = "[已清理]"

def auto_compact(messages: list) -> list:
    """自动压缩: 将对话摘要为一条消息"""
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    conv_text = json.dumps(messages, default=str)[-80000:]
    resp = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": f"为保持上下文连续性进行摘要:\n{conv_text}"}],
        max_tokens=2000,
    )
    summary = resp.content[0].text
    return [
        {"role": "user", "content": f"[已压缩。转录文件: {path}]\n{summary}"},
    ]


# === 章节: 文件任务 (s07) ===
class TaskManager:
    def __init__(self):
        TASKS_DIR.mkdir(exist_ok=True)

    def _next_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in TASKS_DIR.glob("task_*.json")]
        return max(ids, default=0) + 1

    def _load(self, tid: int) -> dict:
        p = TASKS_DIR / f"task_{tid}.json"
        if not p.exists(): raise ValueError(f"任务 {tid} 不存在")
        return json.loads(p.read_text())

    def _save(self, task: dict):
        (TASKS_DIR / f"task_{task['id']}.json").write_text(json.dumps(task, indent=2))

    def create(self, subject: str, description: str = "") -> str:
        """创建一个持久化文件任务"""
        task = {"id": self._next_id(), "subject": subject, "description": description,
                "status": "pending", "owner": None, "blockedBy": []}
        self._save(task)
        return json.dumps(task, indent=2)

    def get(self, tid: int) -> str:
        """获取任务详情"""
        return json.dumps(self._load(tid), indent=2)

    def update(self, tid: int, status: str = None,
               add_blocked_by: list = None, remove_blocked_by: list = None) -> str:
        """更新任务状态或依赖关系"""
        task = self._load(tid)
        if status:
            task["status"] = status
            if status == "completed":
                # 完成任务时，解除其他任务对该任务的阻塞
                for f in TASKS_DIR.glob("task_*.json"):
                    t = json.loads(f.read_text())
                    if tid in t.get("blockedBy", []):
                        t["blockedBy"].remove(tid)
                        self._save(t)
            if status == "deleted":
                (TASKS_DIR / f"task_{tid}.json").unlink(missing_ok=True)
                return f"任务 {tid} 已删除"
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if remove_blocked_by:
            task["blockedBy"] = [x for x in task["blockedBy"] if x not in remove_blocked_by]
        self._save(task)
        return json.dumps(task, indent=2)

    def list_all(self) -> str:
        """列出所有任务"""
        tasks = [json.loads(f.read_text()) for f in sorted(TASKS_DIR.glob("task_*.json"))]
        if not tasks: return "暂无任务。"
        lines = []
        for t in tasks:
            m = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            owner = f" @{t['owner']}" if t.get("owner") else ""
            blocked = f" (被阻塞: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{m} #{t['id']}: {t['subject']}{owner}{blocked}")
        return "\n".join(lines)

    def claim(self, tid: int, owner: str) -> str:
        """认领一个任务"""
        task = self._load(tid)
        task["owner"] = owner
        task["status"] = "in_progress"
        self._save(task)
        return f"已为 {owner} 认领任务 #{tid}"


# === 章节: 后台任务 (s08) ===
class BackgroundManager:
    def __init__(self):
        self.tasks = {}
        self.notifications = Queue()

    def run(self, command: str, timeout: int = 120) -> str:
        """在后台线程中运行命令"""
        tid = str(uuid.uuid4())[:8]
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        threading.Thread(target=self._exec, args=(tid, command, timeout), daemon=True).start()
        return f"后台任务 {tid} 已启动: {command[:80]}"

    def _exec(self, tid: str, command: str, timeout: int):
        try:
            r = subprocess.run(command, shell=True, cwd=WORKDIR,
                               capture_output=True, text=True, timeout=timeout)
            output = (r.stdout + r.stderr).strip()[:50000]
            self.tasks[tid].update({"status": "completed", "result": output or "(无输出)"})
        except Exception as e:
            self.tasks[tid].update({"status": "error", "result": str(e)})
        self.notifications.put({"task_id": tid, "status": self.tasks[tid]["status"],
                                "result": self.tasks[tid]["result"][:500]})

    def check(self, tid: str = None) -> str:
        """检查后台任务状态"""
        if tid:
            t = self.tasks.get(tid)
            return f"[{t['status']}] {t.get('result') or '(运行中)'}" if t else f"未知任务: {tid}"
        return "\n".join(f"{k}: [{v['status']}] {v['command'][:60]}" for k, v in self.tasks.items()) or "无后台任务。"

    def drain(self) -> list:
        """排空通知队列"""
        notifs = []
        while not self.notifications.empty():
            notifs.append(self.notifications.get_nowait())
        return notifs


# === 章节: 消息总线 (s09) ===
class MessageBus:
    def __init__(self):
        INBOX_DIR.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        """发送消息"""
        msg = {"type": msg_type, "from": sender, "content": content,
               "timestamp": time.time()}
        if extra: msg.update(extra)
        with open(INBOX_DIR / f"{to}.jsonl", "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"已发送 {msg_type} 给 {to}"

    def read_inbox(self, name: str) -> list:
        """读取并清空收件箱"""
        path = INBOX_DIR / f"{name}.jsonl"
        if not path.exists(): return []
        msgs = [json.loads(l) for l in path.read_text().strip().splitlines() if l]
        path.write_text("")
        return msgs

    def broadcast(self, sender: str, content: str, names: list) -> str:
        """广播消息给所有队友"""
        count = 0
        for n in names:
            if n != sender:
                self.send(sender, n, content, "broadcast")
                count += 1
        return f"已广播给 {count} 位队友"


# === 章节: 关机 + 计划跟踪 (s10) ===
shutdown_requests = {}
plan_requests = {}


# === 章节: 团队管理 (s09/s11) ===
class TeammateManager:
    def __init__(self, bus: MessageBus, task_mgr: TaskManager):
        TEAM_DIR.mkdir(exist_ok=True)
        self.bus = bus
        self.task_mgr = task_mgr
        self.config_path = TEAM_DIR / "config.json"
        self.config = self._load()
        self.threads = {}

    def _load(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save(self):
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name: return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """生成一个持久化自主队友"""
        member = self._find(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"错误: '{name}' 当前状态为 {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save()
        threading.Thread(target=self._loop, args=(name, role, prompt), daemon=True).start()
        return f"已生成 '{name}' (角色: {role})"

    def _set_status(self, name: str, status: str):
        """设置队友状态"""
        member = self._find(name)
        if member:
            member["status"] = status
            self._save()

    def _loop(self, name: str, role: str, prompt: str):
        """队友主循环: 工作 -> 空闲轮询 -> 自动认领"""
        team_name = self.config["team_name"]
        sys_prompt = (f"你是 '{name}'，角色: {role}，团队: {team_name}，位于 {WORKDIR}。"
                      f"完成当前工作后使用 idle。你可以自动认领任务。")
        messages = [{"role": "user", "content": prompt}]
        tools = [
            {"name": "bash", "description": "运行命令。", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "读取文件。", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "写入文件。", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "编辑文件。", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "发送消息。", "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}}, "required": ["to", "content"]}},
            {"name": "idle", "description": "表示没有更多工作。", "input_schema": {"type": "object", "properties": {}}},
            {"name": "claim_task", "description": "通过 ID 认领任务。", "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        ]
        while True:
            # -- 工作阶段 --
            for _ in range(50):
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg)})
                try:
                    response = client.messages.create(
                        model=MODEL, system=sys_prompt, messages=messages,
                        tools=tools, max_tokens=8000)
                except Exception:
                    self._set_status(name, "shutdown")
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
                            output = "进入空闲阶段。"
                        elif block.name == "claim_task":
                            output = self.task_mgr.claim(block.input["task_id"], name)
                        elif block.name == "send_message":
                            output = self.bus.send(name, block.input["to"], block.input["content"])
                        else:
                            dispatch = {"bash": lambda **kw: run_bash(kw["command"]),
                                        "read_file": lambda **kw: run_read(kw["path"]),
                                        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
                                        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"])}
                            output = dispatch.get(block.name, lambda **kw: "未知")(**block.input)
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                messages.append({"role": "user", "content": results})
                if idle_requested:
                    break
            # -- 空闲阶段: 轮询收件箱消息和未认领任务 --
            self._set_status(name, "idle")
            resume = False
            for _ in range(IDLE_TIMEOUT // max(POLL_INTERVAL, 1)):
                time.sleep(POLL_INTERVAL)
                inbox = self.bus.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break
                unclaimed = []
                for f in sorted(TASKS_DIR.glob("task_*.json")):
                    t = json.loads(f.read_text())
                    if t.get("status") == "pending" and not t.get("owner") and not t.get("blockedBy"):
                        unclaimed.append(t)
                if unclaimed:
                    task = unclaimed[0]
                    self.task_mgr.claim(task["id"], name)
                    # 上下文压缩后的身份重新注入
                    if len(messages) <= 3:
                        messages.insert(0, {"role": "user", "content":
                            f"<identity>你是 '{name}'，角色: {role}，团队: {team_name}。</identity>"})
                        messages.insert(1, {"role": "assistant", "content": f"我是 {name}。继续工作。"})
                    messages.append({"role": "user", "content":
                        f"<auto-claimed>任务 #{task['id']}: {task['subject']}\n{task.get('description', '')}</auto-claimed>"})
                    messages.append({"role": "assistant", "content": f"已认领任务 #{task['id']}。正在处理。"})
                    resume = True
                    break
            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def list_all(self) -> str:
        """列出所有队友"""
        if not self.config["members"]: return "暂无队友。"
        lines = [f"团队: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


# === 章节: 全局实例 ===
TODO = TodoManager()
SKILLS = SkillLoader(SKILLS_DIR)
TASK_MGR = TaskManager()
BG = BackgroundManager()
BUS = MessageBus()
TEAM = TeammateManager(BUS, TASK_MGR)

# === 章节: 系统提示词 ===
SYSTEM = f"""你是位于 {WORKDIR} 的编码代理。使用工具完成任务。
多步工作请优先使用 task_create/task_update/task_list。短清单使用 TodoWrite。
使用 task 进行子代理委派。使用 load_skill 获取专业知识。
技能: {SKILLS.descriptions()}"""


# === 章节: 关机协议 (s10) ===
def handle_shutdown_request(teammate: str) -> str:
    """向指定队友发送关机请求"""
    req_id = str(uuid.uuid4())[:8]
    shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "请关闭。", "shutdown_request", {"request_id": req_id})
    return f"关机请求 {req_id} 已发送给 '{teammate}'"

# === 章节: 计划审批 (s10) ===
def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    """审批队友提交的计划"""
    req = plan_requests.get(request_id)
    if not req: return f"错误: 未知的计划请求 '{request_id}'"
    req["status"] = "approved" if approve else "rejected"
    BUS.send("lead", req["from"], feedback, "plan_approval_response",
             {"request_id": request_id, "approve": approve, "feedback": feedback})
    return f"来自 '{req['from']}' 的计划已{req['status']}"


# === 章节: 工具分发 (s02) ===
TOOL_HANDLERS = {
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "TodoWrite":        lambda **kw: TODO.update(kw["items"]),
    "task":             lambda **kw: run_subagent(kw["prompt"], kw.get("agent_type", "Explore")),
    "load_skill":       lambda **kw: SKILLS.load(kw["name"]),
    "compress":         lambda **kw: "正在压缩...",
    "background_run":   lambda **kw: BG.run(kw["command"], kw.get("timeout", 120)),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
    "task_create":      lambda **kw: TASK_MGR.create(kw["subject"], kw.get("description", "")),
    "task_get":         lambda **kw: TASK_MGR.get(kw["task_id"]),
    "task_update":      lambda **kw: TASK_MGR.update(kw["task_id"], kw.get("status"), kw.get("add_blocked_by"), kw.get("remove_blocked_by")),
    "task_list":        lambda **kw: TASK_MGR.list_all(),
    "spawn_teammate":   lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":   lambda **kw: TEAM.list_all(),
    "send_message":     lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":       lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":        lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request": lambda **kw: handle_shutdown_request(kw["teammate"]),
    "plan_approval":    lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    "idle":             lambda **kw: "负责人不会进入空闲状态。",
    "claim_task":       lambda **kw: TASK_MGR.claim(kw["task_id"], "lead"),
}

TOOLS = [
    {"name": "bash", "description": "运行 shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "将内容写入文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "替换文件中的精确文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "TodoWrite", "description": "更新任务跟踪列表。",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "activeForm": {"type": "string"}}, "required": ["content", "status", "activeForm"]}}}, "required": ["items"]}},
    {"name": "task", "description": "生成子代理进行独立探索或工作。",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "agent_type": {"type": "string", "enum": ["Explore", "general-purpose"]}}, "required": ["prompt"]}},
    {"name": "load_skill", "description": "按名称加载专业知识。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "compress", "description": "手动压缩对话上下文。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "background_run", "description": "在后台线程中运行命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}},
    {"name": "check_background", "description": "检查后台任务状态。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
    {"name": "task_create", "description": "创建一个持久化文件任务。",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
    {"name": "task_get", "description": "通过 ID 获取任务详情。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
    {"name": "task_update", "description": "更新任务状态或依赖关系。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]}, "add_blocked_by": {"type": "array", "items": {"type": "integer"}}, "remove_blocked_by": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
    {"name": "task_list", "description": "列出所有任务。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "spawn_teammate", "description": "生成一个持久化自主队友。",
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
    {"name": "plan_approval", "description": "批准或拒绝队友的计划。",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    {"name": "idle", "description": "进入空闲状态。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "claim_task", "description": "从任务板认领一个任务。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


# === 章节: 代理主循环 ===
def agent_loop(messages: list):
    """主代理循环，整合压缩、后台通知、收件箱检查和待办提醒"""
    rounds_without_todo = 0
    while True:
        # s06: 压缩流水线
        microcompact(messages)
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            print("[自动压缩已触发]")
            messages[:] = auto_compact(messages)
        # s08: 排空后台通知
        notifs = BG.drain()
        if notifs:
            txt = "\n".join(f"[后台:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs)
            messages.append({"role": "user", "content": f"<background-results>\n{txt}\n</background-results>"})
        # s10: 检查负责人收件箱
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({"role": "user", "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>"})
        # LLM 调用
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        # 工具执行
        results = []
        used_todo = False
        manual_compress = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compress":
                    manual_compress = True
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"未知工具: {block.name}"
                except Exception as e:
                    output = f"错误: {e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                if block.name == "TodoWrite":
                    used_todo = True
        # s03: 待办提醒 (仅在待办工作流激活时)
        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if TODO.has_open_items() and rounds_without_todo >= 3:
            results.append({"type": "text", "text": "<reminder>请更新你的待办事项。</reminder>"})
        messages.append({"role": "user", "content": results})
        # s06: 手动压缩
        if manual_compress:
            print("[手动压缩]")
            messages[:] = auto_compact(messages)
            return


# === 章节: REPL 交互 ===
if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms_full >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip() == "/compact":
            if history:
                print("[通过 /compact 手动压缩]")
                history[:] = auto_compact(history)
            continue
        if query.strip() == "/tasks":
            print(TASK_MGR.list_all())
            continue
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
