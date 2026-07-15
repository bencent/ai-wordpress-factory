# Chapter 04 - Function

## 1. 核心觀念

### Function = Input + Logic + Output + Side Effect

函數（Function）在 AI 代理人系統中是 **動作的基本單位**。不同於傳統程式中的函數，AI 代理人的函數需要：

- **明確的輸入** (Explicit Input): 定義清楚的參數和型別
- **可預測的邏輯** (Predictable Logic): 邏輯清晰、可測試
- **明確的輸出** (Explicit Output): 返回值型別明確
- **可控的副作用** (Controlled Side Effects): side effect 要可追踪

### 函數的演進

```
傳統函數 → Lambda 函數 → 高階函數 → 裝飾器 → Tool Function → AI Function
```

### AI 代理人的函數特性

1. **工具化** (Tool-like)
   - 函數作為工具供代理人使用
   - 符合 Tool Calling 介面

2. **可描述性** (Describable)  
   - 函數能夠描述自己的功能
   - 支持自動生成文檔

3. **可組合性** (Composable)
   - 函數可以組合成更複雜的功能
   - 支持函數鏈呼叫

4. **可觀察性** (Observable)
   - 函數的執行過程可追踪
   - 支持日誌記錄和監控

### 函數在 AI 系統中的分類

```
┌─────────────────────────────────────────────┐
│              Function Types                    │
├─────────────────────────────────────────────┤
│                                                 │
│  1. 核心函數 (Core Functions)                   │
│     └── 系統核心邏輯，不依賴 AI                   │
│                                                 │
│  2. AI 函數 (AI Functions)                      │
│     └── 使用 AI 模型的智能函數                  │
│                                                 │
│  3. 工具函數 (Tool Functions)                   │
│     └── 給代理人使用的外部工具                  │
│                                                 │
│  4. 輔助函數 (Helper Functions)                │
│     └── 支持其他函數的實用工具                  │
│                                                 │
└─────────────────────────────────────────────┘
```

## 2. Architecture

### 函數架構層級

```
┌─────────────────────────────────────────────┐
│            Function Architecture                │
├─────────────────────────────────────────────┤
│                                                     │
│  ┌─────────────────────────────────────────────┐ │
│  │            Application Layer                   │ │
│  │  - Workflow Orchestration                      │ │
│  │  - Agent Coordination                          │ │
│  │  - Task Management                              │ │
│  └─────────────────────────────────────────────┘ │
│                              ↓                            │
│  ┌─────────────────────────────────────────────┐ │
│  │            Function Layer                       │ │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐     │ │
│  │  │ Core     │ │ AI        │ │ Tool     │     │ │
│  │  │ Function │ │ Function │ │ Function │     │ │
│  │  └──────────┘ └──────────┘ └──────────┘     │ │
│  └─────────────────────────────────────────────┘ │
│                              ↓                            │
│  ┌─────────────────────────────────────────────┐ │
│  │            Utility Layer                        │ │
│  │  - Helper Functions                           │ │
│  │  - Validation Functions                         │ │
│  │  - Transformation Functions                     │ │
│  └─────────────────────────────────────────────┘ │
│                                                     │
└─────────────────────────────────────────────┘
```

### 函數設計模式

#### 1. 純函數 (Pure Function)
```python
# 沒有副作用，相同輸入總是相同輸出
def calculate_word_count(text: str) -> int:
    return len(text.split())
```

#### 2. 工具函數 (Tool Function)
```python
# 符合 Tool Calling 介面
def search_web(query: str, max_results: int = 5) -> List[Dict]:
    """
    搜索網路
    
    Args:
        query: 搜索查詢
        max_results: 最大返回結果數
        
    Returns:
        搜索結果列表，每個結果包含 title, url, snippet
    """
    # 執行搜索邏輯
    pass
```

#### 3. AI 函數 (AI Function)
```python
# 使用 AI 模型的函數
async def generate_content(
    prompt: str,
    system_message: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2000
) -> str:
    """
    使用 LLM 生成内容
    """
    # 呼叫 LLM API
    pass
```

#### 4. 工作流程函數 (Workflow Function)
```python
# 協調多個函數的導演
async def research_workflow(task: Dict) -> Dict:
    """
    執行研究工作流程
    """
    # 1. 使用工具搜索
    search_results = await search_web(task['query'])
    
    # 2. 使用 AI 分析
    analysis = await analyze_results(search_results)
    
    # 3. 使用工具儲存
    save_results(analysis)
    
    return analysis
```

### 函數註冊機制

```
┌─────────────────────────────────────────────┐
│            Function Registry                    │
├─────────────────────────────────────────────┤
│                                                     │
│  Function Table:                                   │
│  ┌────────────┬──────────┬──────────┬────────┐ │
│  │ Name        │ Category │ Return   │ Async   │ │
│  ├────────────┼──────────┼──────────┼────────┤ │
│  │ search_web │ tool      │ List[Dict]│ ✓       │ │
│  │ generate   │ ai        │ str       │ ✓       │ │
│  │ validate   │ helper    │ bool      │ ❌      │ │
│  │ analyze    │ ai        │ Dict      │ ✓       │ │
│  └────────────┴──────────┴──────────┴────────┘ │
│                                                     │
│  Function Schema:                                   │
│  {                                                  │
│    "name": "search_web",                         │
│    "description": "Search the web...",           │
│    "parameters": {                                │
│      "query": {"type": "string", "required": true},
│      "max_results": {"type": "integer", "default": 5}
│    },                                             │
│    "returns": {"type": "array", "items": "object"} │
│  }                                                │
│                                                     │
└─────────────────────────────────────────────┘
```

## 3. Python

### 基礎函數定義

```python
from typing import Callable, Any, Dict, List, Optional, Union
from dataclasses import dataclass, field
from enum import Enum
import inspect
import asyncio

# 函數類別
class FunctionCategory(Enum):
    CORE = "core"
    AI = "ai"  
    TOOL = "tool"
    HELPER = "helper"
    WORKFLOW = "workflow"

# 函數參數定義
@dataclass
class FunctionParameter:
    name: str
    type: str  # Python type as string
    required: bool = True
    default: Any = None
    description: str = ""

# 函數定義
@dataclass
class FunctionDefinition:
    name: str
    category: FunctionCategory
    description: str
    parameters: List[FunctionParameter] = field(default_factory=list)
    return_type: str = "Any"
    is_async: bool = False
    is_tool: bool = False
    schema: Optional[Dict] = None
    
    def to_tool_schema(self) -> Dict:
        """轉換為 Tool Calling 規格"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    param.name: {
                        "type": param.type,
                        "description": param.description,
                        "required": param.required,
                        "default": param.default
                    }
                    for param in self.parameters
                }
            }
        }
```

### 函數註冊器

```python
class FunctionRegistry:
    """函數註冊器 - 管理可用的函數"""
    
    def __init__(self):
        self.functions: Dict[str, FunctionDefinition] = {}
        self.tool_functions: Dict[str, Callable] = {}
        self.ai_functions: Dict[str, Callable] = {}
        
    def register(self, func: Callable, category: FunctionCategory = FunctionCategory.CORE) -> str:
        """註冊函數"""
        func_name = func.__name__
        
        # 提取函數簽名
        sig = inspect.signature(func)
        parameters = []
        
        for name, param in sig.parameters.items():
            param_type = "Any"
            if param.annotation != inspect.Parameter.empty:
                param_type = param.annotation.__name__ if hasattr(param.annotation, '__name__') else str(param.annotation)
            
            parameters.append(FunctionParameter(
                name=name,
                type=param_type,
                required=param.default == inspect.Parameter.empty,
                default=param.default if param.default != inspect.Parameter.empty else None,
                description=param.annotation if hasattr(param, 'description') else ""
            ))
        
        # 判斷是否為異步函數
        is_async = inspect.iscoroutinefunction(func)
        
        # 判斷是否為工具函數（有裝飾器或符合命名規則）
        is_tool = hasattr(func, '_is_tool') or func_name.startswith('tool_')
        
        # 提取文檔字串
        doc = inspect.getdoc(func) or ""
        
        # 建立函數定義
        func_def = FunctionDefinition(
            name=func_name,
            category=category,
            description=doc.split('\n')[0],  # 取第一行
            parameters=parameters,
            return_type=self._get_return_type(func),
            is_async=is_async,
            is_tool=is_tool,
            schema=self._generate_schema(func)
        )
        
        # 儲存函數
        self.functions[func_name] = func_def
        
        # 按類別儲存
        if is_tool:
            self.tool_functions[func_name] = func
        if category == FunctionCategory.AI:
            self.ai_functions[func_name] = func
        
        return func_name
    
    def _get_return_type(self, func: Callable) -> str:
        """獲取返回型別"""
        sig = inspect.signature(func)
        if sig.return_annotation != inspect.Signature.empty:
            return sig.return_annotation.__name__ if hasattr(sig.return_annotation, '__name__') else str(sig.return_annotation)
        return "Any"
    
    def _generate_schema(self, func: Callable) -> Dict:
        """生成函數 Schema"""
        sig = inspect.signature(func)
        schema = {
            "name": func.__name__,
            "description": inspect.getdoc(func) or "",
            "parameters": {}
        }
        
        for name, param in sig.parameters.items():
            schema["parameters"][name] = {
                "type": self._get_type_name(param.annotation),
                "description": "",
                "required": param.default == inspect.Parameter.empty
            }
            if param.default != inspect.Parameter.empty:
                schema["parameters"][name]["default"] = param.default
        
        # 添加返回型別
        return_type = self._get_return_type(func)
        if return_type != "Any":
            schema["returns"] = {"type": return_type}
        
        return schema
    
    def _get_type_name(self, annotation) -> str:
        """獲取型別名稱"""
        if annotation == inspect.Parameter.empty:
            return "Any"
        if hasattr(annotation, '__name__'):
            return annotation.__name__
        if hasattr(annotation, '_name'):
            return annotation._name
        return str(annotation)
    
    def get_function(self, name: str) -> Optional[FunctionDefinition]:
        """獲取函數定義"""
        return self.functions.get(name)
    
    def get_tool_functions(self) -> List[str]:
        """獲取所有工具函數名稱"""
        return list(self.tool_functions.keys())
    
    def call_function(self, name: str, *args, **kwargs) -> Any:
        """呼叫函數"""
        # 檢查是否為工具函數
        if name in self.tool_functions:
            return self.tool_functions[name](*args, **kwargs)
        
        # 檢查是否在函數註冊表
        if name in self.functions:
            # 這裡需要更智能的呼叫邏輯
            pass
        
        raise ValueError(f"Function {name} not found")
    
    async def call_async_function(self, name: str, *args, **kwargs) -> Any:
        """呼叫異步函數"""
        if name in self.tool_functions:
            func = self.tool_functions[name]
            if inspect.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            else:
                return func(*args, **kwargs)
        
        raise ValueError(f"Function {name} not found")
```

### 裝飾器

```python
# 工具函數裝飾器
def tool_function(category: FunctionCategory = FunctionCategory.TOOL):
    """標記函數為工具函數"""
    def decorator(func: Callable):
        func._is_tool = True
        func._category = category
        return func
    return decorator

# AI 函數裝飾器
def ai_function(category: FunctionCategory = FunctionCategory.AI):
    """標記函數為 AI 函數"""
    def decorator(func: Callable):
        func._is_ai = True
        func._category = category
        return func
    return decorator

# 使用範例
@tool_function()
def search_web(query: str, max_results: int = 5) -> List[Dict]:
    """搜索網路，返回相關結果"""
    pass

@ai_function()
async def generate_content(prompt: str, temperature: float = 0.7) -> str:
    """使用 AI 生成内容"""
    pass

# 自動註冊所有裝飾的函數
registry = FunctionRegistry()
registry.register(search_web)
registry.register(generate_content)
```

### 函數呼叫工具

```python
class FunctionCaller:
    """函數呼叫工具 - 供代理人使用"""
    
    def __init__(self, registry: FunctionRegistry):
        self.registry = registry
        self.call_history: List[Dict] = []
        
    def call(self, function_name: str, *args, **kwargs) -> Any:
        """呼叫函數並記錄執行歷史"""
        start_time = time.time()
        
        try:
            # 呼叫函數
            result = self.registry.call_function(function_name, *args, **kwargs)
            
            # 記錄執行歷史
            self.call_history.append({
                "function": function_name,
                "args": args,
                "kwargs": kwargs,
                "result": result,
                "duration": time.time() - start_time,
                "status": "success",
                "timestamp": datetime.now().isoformat()
            })
            
            return result
            
        except Exception as e:
            # 記錄錯誤
            self.call_history.append({
                "function": function_name,
                "args": args,
                "kwargs": kwargs,
                "error": str(e),
                "duration": time.time() - start_time,
                "status": "failed",
                "timestamp": datetime.now().isoformat()
            })
            raise
    
    async def call_async(self, function_name: str, *args, **kwargs) -> Any:
        """呼叫異步函數"""
        start_time = time.time()
        
        try:
            result = await self.registry.call_async_function(function_name, *args, **kwargs)
            
            self.call_history.append({
                "function": function_name,
                "args": args,
                "kwargs": kwargs,
                "result": result,
                "duration": time.time() - start_time,
                "status": "success",
                "timestamp": datetime.now().isoformat()
            })
            
            return result
            
        except Exception as e:
            self.call_history.append({
                "function": function_name,
                "args": args,
                "kwargs": kwargs,
                "error": str(e),
                "duration": time.time() - start_time,
                "status": "failed",
                "timestamp": datetime.now().isoformat()
            })
            raise
    
    def get_history(self) -> List[Dict]:
        """獲取呼叫歷史"""
        return self.call_history.copy()
```

## 4. GitHub Implementation

### 本專案中的函數實現

本專案的工具函數實現在 [`tools/`](../tools/) 目錄中：

```
tools/
├── __init__.py          # 工具基類
├── search.py            # 搜索工具
└── wordpress.py         # WordPress 發布工具
```

#### [`tools/__init__.py`](../tools/__init__.py)

```python
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

class Tool(ABC):
    """工具基類"""
    
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
    
    @abstractmethod
    def execute(self, *args, **kwargs) -> Any:
        """執行工具"""
        pass
    
    def to_dict(self) -> Dict:
        """轉為字典（供 Tool Calling 用）"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.get_parameters()
            }
        }
    
    @abstractmethod
    def get_parameters(self) -> Dict:
        """獲取參數定義"""
        pass
```

#### [`tools/search.py`](../tools/search.py)

```python
from . import Tool
from typing import List, Dict, Any
import requests

class SearchTool(Tool):
    """搜索工具"""
    
    def __init__(self, api_key: str = None, engine_id: str = None):
        super().__init__(
            name="search_web",
            description="搜索網路，返回相關的搜索結果"
        )
        self.api_key = api_key
        self.engine_id = engine_id
    
    def execute(self, query: str, max_results: int = 5) -> List[Dict]:
        """執行搜索"""
        # 如果有配置 API，使用真實搜索
        if self.api_key and self.engine_id:
            return self._search_with_api(query, max_results)
        
        # 否則返回模擬數據
        return self._mock_search(query, max_results)
    
    def _search_with_api(self, query: str, max_results: int) -> List[Dict]:
        """使用 API 進行搜索"""
        # 省略實際 API 呼叫邏輯
        pass
    
    def _mock_search(self, query: str, max_results: int) -> List[Dict]:
        """返回模擬的搜索結果"""
        return [
            {
                "title": f"搜索結果：{query}",
                "url": f"https://example.com/{query}",
                "snippet": f"這是關於 {query} 的搜索結果"
            }
            for _ in range(max_results)
        ]
    
    def get_parameters(self) -> Dict:
        """獲取參數定義"""
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查詢字串",
                    "required": True
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回結果數",
                    "default": 5
                }
            },
            "required": ["query"]
        }
```

## 5. Code Review
n
### 函數設計最佳實踐

#### ✅ 良好的函數設計

1. **單一責任原則** (Single Responsibility)
   - 一個函數只做一件事
   - 便於測試和維護

2. **型別提示** (Type Hints)
   - 使用 Python 型別提示
   - 提高代碼可讀性

3. **文檔完整** (Complete Documentation)
   - 每個函數都有 docstring
   - 參數和返回值都有說明

4. **錯誤處理** (Error Handling)
   - 合理的錯誤處理
   - 返回有意義的錯誤訊息

#### ❌ 待改進的地方

1. **函數命名** (Function Naming)
   - 部分函數命名不夠清晰
   - 建議：使用動詞 + 名詞的命名方式

2. **型別不完整** (Incomplete Types)
   - 部分函數缺少返回型別提示
   - 建議：為所有函數添加型別提示

3. **錯誤處理不足** (Insufficient Error Handling)
   - 缺乏外部 API 呼叫的錯誤處理
   - 建議：添加重試和回滾機制

4. **異步函數混亂** (Async Function Chaos)
   - 異步和同步函數混用
   - 建議：明確標註異步函數

## 6. QA

### 常見問題

#### Q: 函數和工具的差異是什麼？
A: 
- **函數 (Function)**: 程式碼層級的功能封裝
- **工具 (Tool)**: 供代理人使用的函數，符合 Tool Calling 介面
- 簡單說：所有工具都是函數，但不是所有函數都是工具

#### Q: 什麼時候該用異步函數？
A: 
- I/O 密集型操作（網路請求、文件讀寫、資料庫查詢）
- 等待外部服務回應
- 需要並發執行的任務

#### Q: 如何設計良好的 Tool Function？
A: 
1. **明確的參數定義**：每個參數都有型別和說明
2. **可預測的返回值**：返回值型別和格式明確
3. **完整的錯誤處理**：處理所有可能的錯誤
4. **詳細的文檔**：說明功能、參數、返回值

#### Q: 函數的副作用怎麼控制？
A: 
- **最小化副作用**：盡量使用純函數
- **明確副作用**：副作用要明確聲明
- **可追踪副作用**：副作用要可記錄和追踪
- **可恢復副作用**：副作用要能回滾

## 7. Exercise

### 練習 1: 純函數練習

實現以下純函數：

```python
# 1. 計算字串中的單詞數
def count_words(text: str) -> int:
    """
    計算字串中的單詞數
    類似 str.split() 但要處理更多邊界情況
    """
    # 你的代碼
    pass

# 2. 計算字串的 MD5 雜湊
def calculate_md5(text: str) -> str:
    """
    計算字串的 MD5 雜湊值
    """
    # 提示：使用 hashlib.md5
    # 你的代碼
    pass

# 3. 驗證電子郵件格式
def is_valid_email(email: str) -> bool:
    """
    驗證電子郵件格式是否有效
    """
    # 提示：使用正則表達式
    # 你的代碼
    pass
```

### 練習 2: 工具函數實現

實現以下工具函數：

```python
# 1. 文字翻譯工具
class TranslationTool(Tool):
    """文字翻譯工具"""
    
    def __init__(self, target_language: str = "zh-TW"):
        super().__init__(
            name="translate",
            description="翻譯文字到目標語言"
        )
        self.target_language = target_language
    
    def execute(self, text: str, source_language: str = "auto") -> str:
        """執行翻譯"""
        # 提示：使用外部 API 或本地模型
        # 你的代碼
        pass
    
    def get_parameters(self) -> Dict:
        """獲取參數定義"""
        # 你的代碼
        pass

# 2. 文字摘要工具
class SummaryTool(Tool):
    """文字摘要工具"""
    
    def __init__(self, max_length: int = 100):
        super().__init__(
            name="summarize",
            description="生成文字摘要"
        )
        self.max_length = max_length
    
    def execute(self, text: str, ratio: float = 0.2) -> str:
        """執行摘要"""
        # 提示：可以使用 LLM 或傳統方法
        # 你的代碼
        pass
    
    def get_parameters(self) -> Dict:
        """獲取參數定義"""
        # 你的代碼
        pass
```

### 練習 3: 高階函數

實現以下高階函數：

```python
# 1. 函數組合
def compose(*functions):
    """
    組合多個函數，返回一個新的函數
    範例: add_then_square = compose(square, add_one)
           add_then_square(5)  # (5+1)^2 = 36
    """
    def composed_function(x):
        # 你的代碼
        pass
    return composed_function

def add_one(x):
    return x + 1

def square(x):
    return x * x

# 2. 函數管道
def pipe(x, *functions):
    """
    將值通過多個函數處理
    範例: pipe(5, add_one, square)  # ((5+1)^2) = 36
    """
    # 你的代碼
    pass

# 3. 函數重試
def retry(max_attempts: int = 3, delay: float = 1.0):
    """
    重試裝飾器
    範例: @retry(max_attempts=3)
            def flaky_function():
                pass
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            # 你的代碼
            pass
        return wrapper
    return decorator

# 4. 函數計時
def timeit(func):
    """
    計時裝飾器
    範例: @timeit
            def slow_function():
                time.sleep(1)
    """
    def wrapper(*args, **kwargs):
        # 你的代碼
        pass
    return wrapper
```

## 8. Reflection

### 學習本章後的思考

1. **函數的本質**
   - 函數是動作的抽象：Function is abstraction of action
   - 函數是邏輯的封裝：Function is encapsulation of logic
   - 函數是可重用的單元：Function is a reusable component

2. **設計見解**
   - 函數要小而精：Small and focused functions
   - 函數要型別清楚：Clear types, clear contracts
   - 函數要易於測試：Testable functions are maintainable functions

3. **實踐經驗**
   - 函數命名要有意義：Meaningful names over clever names
   - 函數參數要合理：Reasonable parameters, not too many, not too few
   - 函數返回要明確：Clear return values, avoid None when possible

4. **未來方向**
   - 函數的智能生成：Can AI generate functions automatically?
   - 函數的動態組合：Can functions compose themselves?
   - 函數的自我優化：Can functions optimize themselves?

---

**下一章**: [Chapter 05 - Planner](05-Planner.md)
**上一級**: [../COURSE_CONTEXT.md](../COURSE_CONTEXT.md) | [../ROADMAP.md](../ROADMAP.md)