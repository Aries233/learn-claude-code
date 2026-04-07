# Session: Claude Code Agent 运行机制

## Learner Profile
- Level: beginner（有 Claude Code 使用经验，但对底层机制不了解）
- Language: zh
- Started: 2026-04-07 18:50
- Last Active: 2026-04-07 19:00

## Concept Map
| # | Concept | Prerequisites | Status | Score | Last Reviewed | Review Interval |
|---|---------|---------------|--------|-------|---------------|-----------------|
| 1 | Tool 定义（TOOLS 变量） | - | mastered | 90% | 2026-04-07 | 1d |
| 2 | System Prompt 的作用 | - | mastered | 85% | 2026-04-07 | 1d |
| 3 | Agent Loop 循环机制 | 1, 2 | in-progress | 40% | - | - |
| 4 | tool_result 反馈机制 | 3 | not-started | - | - | - |
| 5 | stop_reason 条件判断 | 3 | not-started | - | - | - |
| 6 | messages 历史累积 | 3, 4 | not-started | - | - | - |
| 7 | s02: 多工具系统 | 1-6 | not-started | - | - | - |
| 8 | s03: TodoWrite 任务管理 | 1-6 | not-started | - | - | - |
| 9 | s04: Subagent 子代理 | 1-6 | not-started | - | - | - |
| 10 | s_full: 完整实现 | 1-9 | not-started | - | - | - |

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

## Current State
- 正在学习: s01_agent_loop.py
- 当前概念: Agent Loop 循环机制 (概念 #3)
- 待回答问题: "为什么要用 while True 循环？为什么不能调用一次 LLM 就结束？"
- 下一步: 理解 tool_result 如何反馈给 LLM，形成循环

## Files Studied
- `agents/s01_agent_loop.py` (部分，第 1-101 行)

## Resume Instructions
继续时，先问用户："上次我们正在讨论 Agent Loop 的 while True 循环，你能回忆一下当时的问题吗？"
然后根据用户的回答决定是直接讲解还是重新引导。
