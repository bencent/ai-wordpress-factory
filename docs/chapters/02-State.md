# Chapter 02 - State

## 1. 核心觀念

### State = Memory + Context + Progress

狀態（State）是代理人和工作流程的核心概念，它決定了：

- **記憶** (Memory): 代理人記住什麼
- **上下文** (Context): 代理人理解當前情境
- **進度** (Progress): 代理人完成到哪一步

### 為什麼需要狀態管理？

沒有狀態的代理人：
```
User: "幫我寫一篇關於 AI 的文章"
Agent: "好的，主題是什麼？"
User: "AI 在醫療中的應用"
Agent: "好的，目標受眾是誰？"  # ❌ 兩次提問，沒有記住
```

有狀態的代理人：
```
User: "幫我寫一篇關於 AI 的文章"
Agent: "好的，主題是什麼？"  # 狀態: waiting_for_topic
User: "AI 在醫療中的應用"
Agent: "好的，目標受眾是誰？"  # 狀態: waiting_for_audience, 記得主題
```

### 狀態的層級

```
┌─────────────────────────────────────────────┐
│            系統狀態層級                          │
├─────────────────────────────────────────────┤
│  Level 1: 代理人狀態 (Agent State)               │
│  ├── 當前行為 (current_action)                 │
│  ├── 思考過程 (thought_process)                │
│  └── 部分記憶 (short_term_memory)               │
│                                                    │
│  Level 2: 任務狀態 (Task State)                  │
│  ├── 任務進度 (progress)                         │
│  ├── 已完成步驟 (completed_steps)               │
│  ├── 目前步驟 (current_step)                    │
│  └── 任務上下文 (task_context)                  │
│                                                    │
│  Level 3: 工作流程狀態 (Workflow State)          │
│  ├── 所有任務狀態                               │
│  ├── 任務隊列 (task_queue)                     │
│  ├── 代理人分配 (agent_assignment)              │
│  └── 全局上下文 (global_context)                │
│                                                    │
│  Level 4: 會話狀態 (Session State)               │
│  ├── 使用者偏好 (user_preferences)              │
│  ├── 歷史紀錄 (conversation_history)            │
│  └── 會話上下文 (session_context)               │
└─────────────────────────────────────────────┘
```

### 狀態的生命周期

```
┌──────┐     ┌───────┐     ┌──────┐     ┌───────┐
│ 初始  │────▶│ 進行中 │────▶│ 暫停  │────▶│ 已完成 │
└──────┘     └───────┘     └──────┘     └───────┘
       ↓           ↓           ↓
    可中斷      可暫停      可恢復
```

## 2. Architecture

### 狀態管理架構

```
┌─────────────────────────────────────────────┐
│              State Management                   │
├─────────────────────────────────────────────┤
│                                                     │
│  ┌──────────────┐   ┌──────────────┐         │
│  │ State Manager │   │ State Store   │         │
│  │               │   │               │         │
│  │ - save()      │   │ - In-Memory   │         │
│  │ - load()      │   │ - File-based  │         │
│  │ - update()    │   │ - Database    │         │
│  │ - clear()     │   │ - Redis       │         │
│  └──────────────┘   └──────────────┘         │
│            ↓                     ↓               │
│  ┌──────────────────────────────────────┐    │
│  │           State Objects                │    │
│  │  - AgentState                          │    │
│  │  - TaskState                           │    │
│  │  - WorkflowState                       │    │
│  └──────────────────────────────────────┘    │
│                                                     │
└─────────────────────────────────────────────┘
```

### 狀態儲存策略

| 策略 | 用途 | 實現 | 穩固性 | 速度 |
|------|------|------|--------|------|
| In-Memory | 瞬間狀態 | Python dict | ❌ 重啟消失 | ⚡⚡⚡ |
| File-based | 短期持久化 | JSON/YAML | ✅ 重啟保留 | ⚡⚡ |
| Database | 長期儲存 | SQLite/PostgreSQL | ✅✅✅ 複雜查詢 | ⚡ |
| Redis | 高效緩存 | Redis | ✅✅ 重啟保留 | ⚡⚡⚡ |

## 3. Python

### 基礎狀態類別

```python
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
from enum import Enum
import json
import os
from datetime import datetime

class StateStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class BaseState:
    """基礎狀態類別"""
    id: str
    status: StateStatus = StateStatus.IDLE
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def update(self, **kwargs):
        """更新狀態"""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.updated_at = datetime.now().isoformat()
    
    def to_dict(self) -> Dict:
        """序列化為字典"""
        return {
            'id': self.id,
            'status': self.status.value,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'metadata': self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'BaseState':
        """從字典反序列化"""
        data['status'] = StateStatus(data.get('status', 'idle'))
        return cls(**data)
```

### 任務狀態類別

```python
@dataclass
class TaskState(BaseState):
    """任務狀態"""
    title: str
    description: str
    content_type: str = "blog_post"
    priority: int = 1
    current_step: str = "pending"
    completed_steps: List[str] = field(default_factory=list)
    result: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    
    # 進度計算
    @property
    def progress(self) -> float:
        """計算完成進度 (0-1)"""
        total_steps = 5  # 標準任務步驟數
        return len(self.completed_steps) / total_steps
    
    def complete_step(self, step_name: str):
        """完成一個步驟"""
        if step_name not in self.completed_steps:
            self.completed_steps.append(step_name)
            self.current_step = step_name
            self.update()
```

### 工作流程狀態類別

```python
@dataclass
class WorkflowState:
    """工作流程狀態 - 管理多個任務"""
    tasks: Dict[str, TaskState] = field(default_factory=dict)
    task_queue: List[str] = field(default_factory=list)
    current_task_id: Optional[str] = None
    global_context: Dict[str, Any] = field(default_factory=dict)
    
    def add_task(self, task_state: TaskState) -> str:
        """新增任務"""
        task_id = task_state.id
        self.tasks[task_id] = task_state
        self.task_queue.append(task_id)
        return task_id
    
    def get_task(self, task_id: str) -> Optional[TaskState]:
        """獲取任務"""
        return self.tasks.get(task_id)
    
    def update_task(self, task_id: str, **kwargs) -> bool:
        """更新任務狀態"""
        if task_id in self.tasks:
            self.tasks[task_id].update(**kwargs)
            return True
        return False
    
    def get_next_task(self) -> Optional[str]:
        """獲取下一個任務 ID"""
        if self.task_queue:
            return self.task_queue.pop(0)
        return None
    
    def to_dict(self) -> Dict:
        """序列化為字典"""
        return {
            'tasks': {tid: task.to_dict() for tid, task in self.tasks.items()},
            'task_queue': self.task_queue,
            'current_task_id': self.current_task_id,
            'global_context': self.global_context
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'WorkflowState':
        """從字典反序列化"""
        workflow = cls()
        workflow.tasks = {
            tid: TaskState.from_dict(task_data) 
            for tid, task_data in data.get('tasks', {}).items()
        }
        workflow.task_queue = data.get('task_queue', [])
        workflow.current_task_id = data.get('current_task_id')
        workflow.global_context = data.get('global_context', {})
        return workflow
```

### 狀態管理器

```python
class StateManager:
    """狀態管理器 - 處理狀態的儲存和載入"""
    
    def __init__(self, storage_backend: str = "file"):
        self.storage_backend = storage_backend
        self.workflow_state = WorkflowState()
        
    def save_state(self, file_path: str = "workflow_state.json") -> bool:
        """保存狀態到檔案"""
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(self.workflow_state.to_dict(), f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"保存狀態失敗: {e}")
            return False
    
    def load_state(self, file_path: str = "workflow_state.json") -> bool:
        """從檔案載入狀態"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.workflow_state = WorkflowState.from_dict(data)
            return True
        except FileNotFoundError:
            # 文件不存在，使用空狀態
            self.workflow_state = WorkflowState()
            return False
        except Exception as e:
            print(f"載入狀態失敗: {e}")
            return False
    
    def create_task(self, task_data: Dict) -> str:
        """建立新任務"""
        task_state = TaskState(
            id=f"task_{len(self.workflow_state.tasks) + 1}",
            title=task_data.get('title', '未命名任務'),
            description=task_data.get('description', ''),
            content_type=task_data.get('content_type', 'blog_post'),
            priority=task_data.get('priority', 1)
        )
        return self.workflow_state.add_task(task_state)
    
    def update_task_status(self, task_id: str, status: StateStatus) -> bool:
        """更新任務狀態"""
        if task_id in self.workflow_state.tasks:
            self.workflow_state.tasks[task_id].status = status
            self.workflow_state.tasks[task_id].updated_at = datetime.now().isoformat()
            return True
        return False
```

## 4. GitHub Implementation

### 本專案中的狀態管理

本專案的狀態管理實現在 [`state.py`](../state.py) 文件中：

```python
# state.py 的核心結構

# 狀態類別定義
class Task:
    def __init__(self, task_id, title, description, content_type="post", priority=1):
        self.task_id = task_id
        self.title = title
        self.description = description
        self.content_type = content_type
        self.priority = priority
        self.status = "pending"
        self.plan = None
        self.research_data = None
        self.draft_content = None
        self.final_content = None
        
class WorkflowState:
    def __init__(self):
        self.tasks = {}  # task_id -> Task
        self.current_task_id = None
    
    def add_task(self, task_id, title, description, content_type="post", priority=1):
        task = Task(task_id, title, description, content_type, priority)
        self.tasks[task_id] = task
        return task_id
```

## 5. Code Review

### 狀態管理最佳實踐

#### ✅ 良好的狀態管理

1. **不可變性原則** (Immutability)
   - 狀態更新要建立新的狀態物件，而不是修改原有的
   - 便於追踪變更和それぞれ實現 undo/redo

2. **序列化支援** (Serialization)
   - 狀態必須能夠序列化（JSON、Pickle 等）
   - 支持持久化儲存

3. **型別提示** (Type Hints)
   - 使用 Python 型別提示
   - 提高代碼可讀性和 IDE 支援

4. **狀態驗證** (State Validation)
   - 限制狀態轉換的合法性
   - 防止非法的狀態變更

#### ❌ 待改進的地方

1. **全局狀態污染** (Global State Pollution)
   - 目前狀態管理器是單例模式
   - 建議：使用依賴注入

2. **錯誤處理** (Error Handling)
   - 缺乏完善的錯誤處理
   - 建議：新增狀態恢復機制

3. **並發控制** (Concurrency Control)
   - 多執行緒存取狀態時可能有 race condition
   - 建議：新增鎖機制

## 6. QA

### 常見問題

#### Q: 狀態和記憶的差異是什麼？
A: 
- **狀態**：描述系統或任務的當前情況（如：任務進行到哪一步）
- **記憶**：儲存的資訊（如：用戶偏好、歷史對話）
- 簡單說：狀態是「現在怎麼樣」，記憶是「過去發生了什麼」

#### Q: 狀態管理的最大挑戰是什麼？
A: 
1. **一致性** (Consistency): 多個代理人、多個任務的狀態保持一致
2. **持久化** (Persistence): 狀態在重啟後仍然可用
3. **並發性** (Concurrency): 多個使用者、多個任務同時存取
4. **版本控制** (Versioning): 狀態變更的歷史追踪

#### Q: 為什麼不使用全局變數儲存狀態？
A: 
- **可測試性差**：難以 mock 全局狀態
- **並發問題**：多執行緒同時修改會有 race condition
- **可維護性差**：狀態變更難以追踪
- **可擴展性差**：難以支持多使用者、多任務

#### Q: 狀態儲存選擇哪個策略？
A: 
- **In-Memory**: 開發測試時使用，速度快
- **File-based**: 單機生產環境，簡單可靠
- **Database**: 多使用者環境，支持複雜查詢
- **Redis**: 高效能需求，頻繁讀寫的狀態

## 7. Exercise

### 練習 1: 簡單狀態管理

建立一個 `CounterState` 類別，支援：
1. 增加計數
2. 減少計數
3. 重置計數
4. 獲取當前計數
5. 狀態歷史追踪

```python
class CounterState:
    def __init__(self):
        # 你的代碼
        pass
    
    def increment(self):
        # 你的代碼
        pass
    
    def decrement(self):
        # 你的代碼
        pass
    
    def reset(self):
        # 你的代碼
        pass
    
    def get_count(self) -> int:
        # 你的代碼
        pass
    
    def get_history(self) -> List[int]:
        # 你的代碼
        pass
```

### 練習 2: 任務隊列狀態

建立一個 `TaskQueueState` 類別，支援：
1. 新增任務
2. 移除任務
3. 獲取下一個任務
4. 獲取隊列狀態
5. 任務優先級排序

```python
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class QueueTask:
    task_id: str
    title: str
    priority: int = 1  # 1=最高, 5=最低

class TaskQueueState:
    def __init__(self):
        # 你的代碼
        pass
    
    def add_task(self, task_id: str, title: str, priority: int = 1):
        # 你的代碼
        pass
    
    def remove_task(self, task_id: str) -> bool:
        # 你的代碼
        pass
    
    def get_next_task(self) -> Optional[QueueTask]:
        # 讓最高優先級的任務先執行
        # 你的代碼
        pass
    
    def get_queue_status(self) -> Dict:
        # 回傳隊列統計信息
        # 你的代碼
        pass
```

### 練習 3: 狀態機器

建立一個 `StateMachine` 類別，支援：
1. 註冊狀態轉換規則
2. 觸發狀態轉換
3. 檢查目前狀態
4. 狀態轉換歷史

```python
class StateMachine:
    def __init__(self, initial_state: str):
        # 你的代碼
        pass
    
    def add_transition(self, from_state: str, to_state: str, condition=None):
        """
        新增狀態轉換規則
        condition: 可選的條件函數，返回 True 才能轉換
        """
        # 你的代碼
        pass
    
    def trigger(self, event: str) -> bool:
        """
        觸發狀態轉換
        event: 轉換事件名稱
        回傳: 是否成功轉換
        """
        # 你的代碼
        pass
    
    def get_state(self) -> str:
        # 你的代碼
        pass
    
    def get_history(self) -> List[str]:
        # 你的代碼
        pass

# 使用範例
sm = StateMachine("pending")
sm.add_transition("pending", "running")
sm.add_transition("running", "completed")
sm.add_transition("running", "failed")
sm.add_transition("failed", "pending")  # 可以重試

sm.trigger("start")  # pending -> running
sm.trigger("complete")  # running -> completed
```

## 8. Reflection

### 學習本章後的思考

1. **狀態的本質**
   - 狀態是時間的凍結：State is a snapshot of time
   - 狀態是溝通的媒介：State is the medium of communication
   - 狀態是複雜性的管理：State is complexity management

2. **設計見解**
   - 狀態模型要盡量簡單：Simplicity over complexity
   - 狀態變更要可預測：Predictability over flexibility
   - 狀態管理要集中化：Centralized over distributed

3. **實踐經驗**
   - 狀態序列化很重要：Always make state serializable
   - 狀態驗證不可少：Always validate state transitions
   - 狀態監控要及時：Always monitor state changes

4. **未來方向**
   - 狀態的時效性：How to handle stale state?
   - 狀態的版本控制：How to track state history?
   - 狀態的同步：How to sync state across instances?

---

**下一章**: [Chapter 03 - Dictionary](03-Dictionary.md)
**上一級**: [../COURSE_CONTEXT.md](../COURSE_CONTEXT.md) | [../ROADMAP.md](../ROADMAP.md)