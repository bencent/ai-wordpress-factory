# AI Architect Notes

---

# 2026-07-23

## Agent 設計模板

每一個 Agent 都應遵守固定模板。


Input（從 Task 讀資料）

↓

Reason（依照 Prompt 思考）

↓

Tool（需要時才使用）

↓

Output（寫回 Task）

---

心得

- Agent 不應直接呼叫下一個 Agent。
- Agent 只負責自己的工作。
- 所有 Agent 都透過 Task(State) 溝通。


## Agent Responsibility

每個 Agent 都有明確責任邊界。

PlannerAgent:
負責將需求轉換為計畫。

不負責:
- 搜尋資料
- 寫文章
- 控制其他 Agent

Agent 不應管理 Workflow。
Workflow 應由外部協調。

## Enterprise Agent Lifecycle

一個企業級 Agent 不只是呼叫 LLM。

完整流程應包含：

Input
↓
Prompt Builder
↓
LLM
↓
Output Parser
↓
Validator
↓
Return

Agent 的責任除了產生結果，也要確保輸出的品質符合 Contract。
BaseAgent 不是一個真正工作的 Agent，它是所有 Agent 共用能力的基底（Base Class）。
Function 適合完成一次性的工作；Class 適合描述一個長時間存在、擁有資料、工具與能力的角色。AI Agent 本質上就是一個長時間存在的角色，因此多數會用 Class 來設計。


## Python AI Agent 閱讀規則

1.
class
=
設計圖

2.
object = Class()
=
建立物件

3.
object.function()
=
請物件工作

4.
main.py
=
建立所有物件

5.
Workflow
=
安排大家工作的順序

6.
Agent
=
真正執行工作的角色


## 說明self
self

=
目前這個物件自己


self.config

=
這個 Agent 保存的設定


self.xxx

=
這個物件自己的資料或能力