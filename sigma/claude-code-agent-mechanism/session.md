# Session: Claude Code Agent 运行机制

## Learner Profile
- Level: intermediate（已掌握 s01-s11 核心机制）
- Language: zh
- Started: 2026-04-07 18:50
- Last Active: 2026-04-08

## Concept Map
| # | Concept | Prerequisites | Status | Score | Last Reviewed | Review Interval |
|---|---------|---------------|--------|-------|---------------|-----------------|
| 1 | Tool 定义（TOOLS 变量） | - | mastered | 90% | 2026-04-07 | 1d |
| 2 | System Prompt 的作用 | - | mastered | 85% | 2026-04-07 | 1d |
| 3 | Agent Loop 循环机制 | 1, 2 | mastered | 85% | 2026-04-07 | 1d |
| 4 | tool_result 反馈机制 | 3 | mastered | 85% | 2026-04-07 | 1d |
| 5 | stop_reason 条件判断 | 3 | mastered | 80% | 2026-04-07 | 1d |
| 6 | messages 历史累积 | 3, 4 | mastered | 80% | 2026-04-07 | 1d |
| 7 | s02: 多工具系统 | 1-6 | mastered | 85% | 2026-04-08 | 1d |
| 8 | s03: TodoWrite 任务管理 | 1-6 | mastered | 85% | 2026-04-08 | 1d |
| 9 | s04: Subagent 子代理 | 1-6 | mastered | 90% | 2026-04-08 | 1d |
| 10 | s05: Skill 两层注入 | - | mastered | 85% | 2026-04-08 | 1d |
| 11 | s06: Context 压缩 | - | mastered | 85% | 2026-04-08 | 1d |
| 12 | s07: 文件任务系统 | - | mastered | 85% | 2026-04-08 | 1d |
| 13 | s08: 后台任务 | - | mastered | 85% | 2026-04-08 | 1d |
| 14 | s09: Teammate + 消息 | - | mastered | 85% | 2026-04-08 | 1d |
| 15 | s10: Shutdown + Plan | - | mastered | 85% | 2026-04-08 | 1d |
| 16 | s11: Autonomous agents | - | mastered | 90% | 2026-04-08 | 1d |
| 17 | s12: Worktree 隔离 | - | not-started | - | - | - |
| 18 | s_full: 完整实现 | 1-17 | not-started | - | - | - |

## Misconceptions
| # | Concept | Misconception | Root Cause | Status | Counter-Example Used |
|---|---------|---------------|------------|--------|---------------------|
| (none yet) | | | | | |

## Session Log
- [2026-04-07 18:50] 开始学习，诊断水平：有 Claude Code 使用经验，但对底层机制不了解
- [2026-04-07 18:52] 讲解 Tool 定义，用户理解了 TOOLS 变量和 input_schema
- [2026-04-07 18:55] 讲解 System Prompt，用户理解了 LLM 如何知道可用工具
- [2026-04-07 18:58] 引入 Agent Loop 概念，正在讨论 while True 循环的必要性
- [2026-04-07 19:00] 用户暂停，保存会话状态
- [2026-04-07 21:58] 恢复会话，从 s01 重新开始
- [2026-04-07 22:05] 用户理解了 stop_reason 判断循环终止条件
- [2026-04-07 22:08] 用户理解了 messages 累积机制: assistant 追加 tool_use，user 追加 tool_result
- [2026-04-07 22:15] 用户完成 mastery check，理解错误处理机制（tool_result 返回错误信息）
- [2026-04-07 22:18] 用户完成 practice task（添加 max_loops 限制），概念 #3 #4 掌握
- [2026-04-07 22:25] 用户理解多工具系统：TOOLS 契约 + handler 实现 + TOOL_HANDLERS 分发
- [2026-04-07 22:28] 用户理解 **block.input 字典展开语法，契约驱动设计
- [2026-04-07 22:35] 用户理解 TodoWrite：模型自跟踪进度 + nag reminder 软性约束 + reminder 混入 tool_result
- [2026-04-07 22:40] 用户暂停，保存会话状态
- [2026-04-08] 恢复会话，间隔重复回顾 tool_result 机制
- [2026-04-08] 完成 s04 Subagent：context isolation、防止递归派发、安全限制
- [2026-04-08] 完成 s05 Skill 两层注入：Layer 1 metadata + Layer 2 按需加载，节省 ~90% token
- [2026-04-08] 完成 s06 Context 压缩：micro_compact + auto_compact + manual compact，transcript 保存
- [2026-04-08] 完成 s07 文件任务系统：持久化任务 + blockedBy 依赖图
- [2026-04-08] 完成 s08 后台任务：并行执行 + notification queue + drain 机制
- [2026-04-08] 完成 s09 Agent Teams：独立 inbox + drain 机制 + Teammate 生命周期 + 多 Agent 协作流程设计
- [2026-04-08] 完成 s10 Team Protocols：request_id 关联模式 + Shutdown 协商式关闭 + Plan Approval 审批
- [2026-04-08] 完成 s11 Autonomous Agents：Idle 轮询 + 主动认领任务 + Identity Re-injection
- [2026-04-08] 用户完成综合串联：从 spawn 到 context 压缩到 plan approval 到 shutdown 到 idle 到主动认领，全部概念融会贯通
- [2026-04-08] 用户暂停，待继续 s12 Worktree 隔离

## Current State
- 正在学习: s12_worktree_task_isolation.py
- 当前概念: Worktree + Task Isolation (概念 #17) - 未开始
- 下一步: 回答"为什么并行 Agent 需要目录隔离？"
- 待回答问题: Alice 修 Bug，Bob 写新功能，如果他们改同一个文件会怎样？

## Files Studied
- `agents/s01_agent_loop.py` ✓
- `agents/s02_tool_use.py` ✓
- `agents/s03_todo_write.py` ✓
- `agents/s04_subagent.py` ✓
- `agents/s05_skill_loading.py` ✓
- `agents/s06_context_compact.py` ✓
- `agents/s07_task_system.py` ✓
- `agents/s08_background_tasks.py` ✓
- `agents/s09_agent_teams.py` ✓
- `agents/s10_team_protocols.py` ✓
- `agents/s11_autonomous_agents.py` ✓
- `agents/s12_worktree_task_isolation.py` (进行中，已读取)

## Resume Instructions
继续时，从 s12: Worktree 隔离开始。
待回答问题：Alice 修 Bug，Bob 写新功能，如果他们改同一个文件会怎样？
用户已掌握 s01-s11 的全部核心概念。
