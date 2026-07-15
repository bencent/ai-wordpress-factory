# Chapter 01 - Agent

## 1. 核心觀念

### Agent = LLM + State + Tool + Decision

代理人（Agent）是 AI 系統的核心組成單元，不同於單純的 LLM 呼叫，代理人具備：

- **LLM**: 大型語言模型，提供智能和推理能力
- **State**: 狀態管理，維持對話和任務的上下文
- **Tool**: 工具使用，能夠執行外部功能和獲取數據
- **Decision**: 決策能力，能夠根據情況選擇合適的行動

### 代理人的演進

```
單純 LLM → LLM + 提示工程 → LLM + 記憶 → LLM + 工具 → 代理人
```

### 代理人的特徵

1. **自主性** (Autonomy): 能夠獨立完成任務
2. **反應性** (Reactivity): 能夠回應環境變化
3. **主動性** (Proactiveness): 能夠主動採取行動
4. **社交性** (Social Ability): 能夠與人類和其他代理人溝通

## 2. Architecture

### 代理人架構圖

```
┌─────────────────────────────────────┐
│              Agent                    │
├─────────────────────────────────────┤
│  ┌─────────┐  ┌─────────┐            │
│  │  Memory  │  │  State   │            │
│  └─────────┘  └─────────┘            │
│         ↓               ↓              │
│  ┌─────────────────────────────┐    │
│  │         Brain (LLM)           │    │
│  │  - Reasoning                  │    │
│  │  - Planning                   │    │
│  │  - Decision Making            │    │
│  └─────────────────────────────┘    │
│         ↓               ↓              │
│  ┌─────────┐  ┌─────────┐            │
│  │  Tools   │  │ Actions  │            │
│  └─────────┘  └─────────┘            │
└─────────────────────────────────────┘
```

### 代理人類型

1. **單一代理人** (Single Agent)
   - 獨立完成特定任務
   - 例如：PlannerAgent、WriterAgent

2. **多代理人** (Multi-Agent)
   - 多個代理人協作完成複雜任務
   - 需要 Supervisor 進行協調

3. **階層代理人** (Hierarchical Agent)
   - 高層代理人指揮低層代理人
   - 形成樹狀結構

## 3. Python

### 基礎代理人類別

```python
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

class BaseAgent(ABC):
    """
    基礎代理人抽象類別
    """
    
    def __init__(self, name: str, config: Optional[Dict] = None):
        self.name = name
        self.config = config or {}
        self.memory = {}
        self.state = "idle"
        self.tools = {}
    
    @abstractmethod
    def think(self, prompt: str) -> str:
        """思考邏輯"""
        pass
    
    @abstractmethod
    def execute(self, task: Dict) -> Any:
        """執行任務"""
        pass
    
    def add_tool(self, name: str, tool_callable):
        """添加工具"""
        self.tools[name] = tool_callable
    
    def use_tool(self, tool_name: str, *args, **kwargs) -> Any:
        """使用工具"""
        if tool_name not in self.tools:
            raise ValueError(f"Tool {tool_name} not available")
        return self.tools[tool_name](*args, **kwargs)
```

### 具體實現範例

```python
class PlannerAgent(BaseAgent):
    """
    規劃代理人 - 負責內容生成的規劃
    """
    
    def __init__(self, config: Optional[Dict] = None):
        super().__init__("PlannerAgent", config)
        self.prompt_template = self.config.get("prompt_template", "")
    
    def think(self, prompt: str) -> str:
        """使用 LLM 進行思考"""
        # 這裡會整合 LLM API 呼叫
        return f"Planner thinking about: {prompt}"
    
    def execute(self, task: Dict) -> Dict:
        """執行規劃任務"""
        self.state = "working"
        
        # 分析任務
        analysis = self._analyze_task(task)
        
        # 生成許可
        outline = self._generate_outline(task, analysis)
        
        self.state = "idle"
        return {
            "analysis": analysis,
            "outline": outline,
            "status": "completed"
        }
    
    def _analyze_task(self, task: Dict) -> Dict:
        """分析任務需求"""
        return {
            "target_audience": task.get("audience", "general"),
            "keywords": task.get("keywords", []),
            "complexity": "medium"
        }
    
    def _generate_outline(self, task: Dict, analysis: Dict) -> list:
        """生成文章大綱"""
        return [
            "導言",
            "主要內容",
            "結論"
        ]
```

## 4. GitHub Implementation

### 本專案中的代理人實現

本專案的代理人實現在 [`agents/`](../agents/) 目錄中：

```
agents/
├── __init__.py          # 基礎代理人類別
├── planner.py           # 規劃代理人
├── research.py          # 調研代理人
├── writer.py            # 撰寫代理人
├── seo.py               # SEO 代理人
├── reviewer.py          # 審閱代理人
└── (待擴展)
```

### 核心代理人類別分析

#### [`agents/__init__.py`](../agents/__init__.py)

```python
# 基礎代理人定義
class BaseAgent:
    def __init__(self, config):
        self.config = config
        
    def run(self, task):
        raise NotImplementedError
```

#### [`agents/planner.py`](../agents/planner.py)

```python
class PlannerAgent(BaseAgent):
    def run(self, task):
        # 規劃邏輯
        return plan
```

## 5. Code Review

### 代理人設計最佳實踐

#### ✅ 良好的代理人設計

1. **單一責任原則** (Single Responsibility)
   - 每個代理人只做一件事
   - 例如：Planner 只負責規劃，Writer 只負責撰寫

2. **依賴注入** (Dependency Injection)
   - 拿取外部依賴（如 LLM 客戶端、工具）
   - 便於測試和替換

3. **狀態管理** (State Management)
   - 清晰的狀態轉換
   - 支持暫停、恢復

#### ❌ 待改進的地方

1. **硬編碼提示** (Hardcoded Prompts)
   - 目前提示內容寫死在代碼中
   - 建議：移到 [`prompts/`](../prompts/) 目錄

2. **工具套用** (Tool Integration)
   - 手動呼叫工具
   - 建議：建立 Tool Manager

3. **錯誤處理** (Error Handling)
   - 缺乏完善的錯誤處理
   - 建議：新增重試、回滾機制

## 6. QA

### 常見問題

#### Q: 代理人和 LLM 的差異是什麼？
A: LLM 是代理人的一部分，代理人還包含狀態、工具、決策能力。代理人更像是一個智能系統，而 LLM 是這個系統的大腦。

#### Q: 為什麼需要多個代理人？
A: 單一代理人很難擅長所有任務。多代理人協作可以：
- 分工明確，各司其職
- 降低複雜度
- 提高可維護性
- 支持並行處理

#### Q: 代理人怎麼溝通？
A: 代理人之間透過明確的介面（如函數呼叫、消息傳遞）進行溝通。本專案中使用 Supervisor 進行協調。

#### Q: 代理人的狀態深度怎麼控制？
A: 狀態深度取決於任務需求：
- **短狀態**：單次對話（如簡單查詢）
- **中狀態**：單次任務（如文章撰寫）
- **長狀態**：多次交互（如專案管理）

## 7. Exercise

### 練習 1: 基礎代理人

建立一個簡單的 `GreetingAgent`，能夠：
1. 回應用戶問候
2. 記住用戶名字
3. 並回答「今天幾號？」

```python
# 你的実現
class GreetingAgent(BaseAgent):
    def __init__(self):
        super().__init__("GreetingAgent")
        # 你的代碼
    
    def execute(self, message: str) -> str:
        # 你的代碼
        pass
```

### 練習 2: 工具代理人

建立一個 `CalculatorAgent`，能夠：
1. 接收數學表達式
2. 使用工具進行計算
3. 回傳結果

```python
# 提示：使用 python 的 eval() 或 ast.literal_eval()
class CalculatorAgent(BaseAgent):
    def __init__(self):
        super().__init__("CalculatorAgent")
        self.add_tool("calculate", self._calculate)
    
    def _calculate(self, expression: str) -> float:
        # 你的代碼
        pass
    
    def execute(self, expression: str) -> float:
        # 你的代碼
        pass
```

### 練習 3: 多代理人協作

建立兩個代理人協作：
1. `TranslatorAgent`: 翻譯英語到中文
2. `SummerizerAgent`: 總結文章

然後讓他們協作完成：把英文文章翻譯成中文再總結。

```python
# 你的实現
class TranslatorAgent(BaseAgent):
    pass

class SummerizerAgent(BaseAgent):
    pass

# 協作邏輯
def translate_and_summarize(english_text: str) -> str:
    # 你的代碼
    pass
```

## 8. Reflection

### 學習本章後的思考

1. **代理人的本質**
   - 代理人不只是 LLM 的封裝，而是智能系統的抽象
   - 代理人 = 智能 + 記憶 + 工具 + 目標

2. **設計見解**
   - 代理人的邊界很重要：Clear boundaries, clear responsibilities
   - 狀態管理是挑戰：How much to remember? How to manage?
   - 工具使用是 enhancement：Tools make agents powerful

3. **實踐經驗**
   - 從簡單代理人開始：Start simple, then add complexity
   - 專注於實際問題：Solve real problems, not hypothetical ones
   - 可測試性很重要：Agents should be testable

4. **未來方向**
   - 代理人之間的協作：How to coordinate multiple agents?
   - 代理人的自主性：How to give agents more autonomy?
   - 代理人的學習：Can agents learn and improve?

---

**下一章**: [Chapter 02 - State](02-State.md)
**上一級**: [../COURSE_CONTEXT.md](../COURSE_CONTEXT.md) | [../ROADMAP.md](../ROADMAP.md)