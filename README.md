# AI WordPress Factory 🏭

一個**自動化的 AI 驅動 WordPress內容生成和發布系統**，能夠協助你快速、高質量地生成博客文章、頁面等內容，並自動發布到 WordPress 網站。

- 🚀 **自動化工作流程**：從規劃、調研、撰寫、SEO 優化到審閱和發布，一鍵完成
- ✍️ **高質量內容**：使用先進的 AI 模型生成原創、專業的內容
- 🔍 **智能 SEO**: 自動優化內容的搜索引擎可見性
- 📊 **靈活可配置**：支持自定義配置和工作流程

---

## 📋 功能特性

### 🎯 核心功能
- **內容規劃**：自動生成文章的結構大綱、目標受眾分析和關鍵字策略
- **智能調研**：收集和整理相關資料，為內容生成提供充分的信息支持
- **專業撰寫**：基於計劃和資料生成高質量、原創的文章內容
- **SEO 優化**：自動優化文章的標題、描述、關鍵字等 SEO 元數據
- **質量審閱**：檢查內容的語法、事實準確性、邏輯一致性等
- **自動發布**：一鍵發布內容到 WordPress 網站

### 🔧 技術特性
- **模塊化設計**：各個功能模塊可以獨立使用或配合工作
- **可擴展架構**：支持添加新的 AI 代理人和工具
- **配置靈活**：支持環境變量、JSON 文件等多種配置方式
- **狀態管理**：支持工作流程狀態的保存和恢復

---

## 🏗️ Enterprise AI Agent Workflow

本專案採用企業級 AI 代理人工作流程，提供結構化、可擴展的内容生成管道。

---

## 🏛️ Architecture

系統架構包含以下核心組件：
- **State**: 工作流程狀態管理
- **Agent**: AI 代理人模組
- **Workflow**: 工作流程協調
- **Contract**: 代理人合約定義
- **Router**: 任務路由與分配

詳細架構文檔請參閱 [`docs/architecture/`](docs/architecture/) 目錄。

---

## 📚 Learning Roadmap

### 第一層：基礎概念
- [State](docs/chapters/02-State.md) - 狀態管理機制
- [Dictionary](docs/chapters/03-Dictionary.md) - 在專案中雙向がる動的用語集
- [Function](docs/chapters/04-Function.md) - 核心函數與工具
- [Agent](docs/chapters/01-Agent.md) - AI 代理人基礎

### 第二層：核心模組
- [Planner](docs/chapters/05-Planner.md) - 內容規劃代理人
- [Research](docs/chapters/06-Research.md) - 智能調研代理人
- [Writer](docs/chapters/07-Writer.md) - 專業撰寫代理人
- [Validator](docs/chapters/08-Validator.md) - 資料驗證模組

### 第三層：進階主題
- 工作流程自定義
- 代理人擴展開發
- 系統整合與部署

更多教材內容請參閱 [`docs/chapters/`](docs/chapters/) 目錄。

---

## 📁 Folder Structure

```
ai-wordpress-factory/
├── main.py              # 系統入口，協調工作流程
├── config.py            # 全局配置管理
├── state.py             # 工作流程狀態管理
├── requirements.txt     # Python 依賴包
│
├── agents/              # AI 代理人模組
│   ├── __init__.py      # 代理人基類
│   ├── planner.py       # 規劃代理人
│   ├── research.py      # 調研代理人
│   ├── writer.py        # 撰寫代理人
│   ├── seo.py           # SEO 代理人
│   └── reviewer.py      # 審閱代理人
│
├── tools/               # 外部工具模組
│   ├── __init__.py      # 工具基類
│   ├── search.py        # 搜索工具
│   └── wordpress.py     # WordPress 發布工具
│
├── docs/                # 文檔
│   ├── architecture/    # 架構文檔
│   │   ├── State.md
│   │   ├── Workflow.md
│   │   ├── Agent.md
│   │   ├── Contract.md
│   │   └── Router.md
│   ├── chapters/        # 教材章節
│   │   ├── 01-Agent.md
│   │   ├── 02-State.md
│   │   ├── 03-Dictionary.md
│   │   ├── 04-Function.md
│   │   ├── 05-Planner.md
│   │   ├── 06-Research.md
│   │   ├── 07-Writer.md
│   │   └── 08-Validator.md
│   └── glossary/        # 術語詞彙
│       └── Glossary.md
│
└── prompts/             # AI 指令文件
    ├── planner.md        # 規劃代理人提示
    └── writer.md         # 撰寫代理人提示
```

---

## 📊 Current Progress

### ✅ 已完成
- [x] 核心 AI 代理人開發（Planner, Research, Writer, SEO, Reviewer）
- [x] WordPress REST API 整合
- [x] 工作流程狀態管理.system
- [x] 基本配置管理
- [x] 教材文檔架構建立
- [x] 架構文檔創建
- [x] 術語詞彙集

### 🚧 進行中
- [ ]進階工作流程自定義
- [ ] 代理人智能協作機制
- [ ] 系統效能填優化
- [ ] 更多教材內容補充

### 📋 待完成
- [ ] 更多 AI 模型支持
- [ ] 多語言內容生成
- [ ] 批次處理功能
- [ ] 監控與日誌系統增強

---

## 🔜 Next Chapter

接下來將專注於：
- **代理人智能協作**：優化多代理人之間的協作流程
- **系統效能**：提升大規模內容生成的效率
- **教材完善**：補充更多實用案例和最佳實踐

---

## 🛠️ 技術棧

- **程式語言**: Python 3.8+
- **AI 服務**: OpenAI API (GPT-4, GPT-3.5 等)
- **WordPress 交互**: WordPress REST API
- **搜索服務**: Google Custom Search API (可選)
- **主要依賴**:
  - `openai` - OpenAI API 客戶端
  - `requests` - HTTP 請求庫
  - `python-dotenv` - 環境變量管理
  - 更多依賴請見 [`requirements.txt`](requirements.txt:1)

---

## 📦 安裝指南

### 前置要求

1. Python 3.8 或更高版本
2. pip (Python 包管理器)
3. OpenAI API 鑰匙
4. WordPress 網站 (支持 REST API)
5. (可選) Google Custom Search API 鑰匙

### 安裝步驟

1. **克隆專案**
   ```bash
   git clone https://github.com/you-repo/ai-wordpress-factory.git
   cd ai-wordpress-factory
   ```

2. **創建虛擬環境**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   # 或
   venv\Scripts\activate  # Windows
   ```

3. **安裝依賴**
   ```bash
   pip install -r requirements.txt
   ```

4. **配置環境變量**
   創建 `.env` 文件並添加以下配置：
   ```env
   # OpenAI 配置
   OPENAI_API_KEY=your_openai_api_key
   
   # WordPress 配置
   WORDPRESS_URL=https://your-wordpress-site.com
   WORDPRESS_USERNAME=your_username
   WORDPRESS_APP_PASSWORD=your_application_password
   
   # 搜索 API 配置 (可選)
   SEARCH_API_KEY=your_search_api_key
   SEARCH_ENGINE_ID=your_search_engine_id
   
   # AI 模型配置 (可選)
   AI_MODEL=gpt-4
   AI_TEMPERATURE=0.7
   AI_MAX_TOKENS=2000
   ```

   或直接修改 [`config.py`](config.py:1) 文件中的配置。

---

## 🚀 使用方式

### 基本用法

1. **創建並執行新任務**
   ```bash
   python main.py --task "AI 在數位行銷中的應用" --description "探討 AI 如何改變數位行銷領域"
   ```

2. **指定內容類型**
   ```bash
   python main.py --task "我們的服務" --content-type PAGE --description "公司服務介紹頁面"
   ```

3. **使用自定義配置文件**
   ```bash
   python main.py --config custom_config.json --task "2024 年 SEO 趨勢"
   ```

4. **保存和加載狀態**
   ```bash
   # 保存狀態
   python main.py --save-state workflow_state.json
   
   # 加載狀態
   python main.py --load-state workflow_state.json
   ```

### 進階用法

你可以在 Python 代碼中直接使用 `AIWordPressFactory` 類：

```python
from main import AIWordPressFactory
from state import ContentType

# 初始化工廠
factory = AIWordPressFactory(config_path="config.json")

# 創建新任務
task_id = factory.create_task(
    title="AI 技術的未來發展",
    description="探討 AI 技術在未來 10 年的發展趨勢",
    content_type=ContentType.BLOG_POST,
    priority=1
)

# 執行工作流程
success = factory.run_workflow(task_id)

if success:
    print(f"任務 {task_id} 已成功完成！")
else:
    print(f"任務 {task_id} 執行失敗。")

# 保存狀態
factory.save_state("workflow_state.json")
```

---

## 🔧 配置說明

### OpenAI 配置
- `OPENAI_API_KEY`: OpenAI API 鑰匙 (必須)
- `AI_MODEL`: 使用的 AI 模型 (默認: `gpt-4`)
- `AI_TEMPERATURE`: AI 生成的溫度參數 (默認: `0.7`)
- `AI_MAX_TOKENS`: AI 生成的最大 token 數 (默認: `2000`)

### WordPress 配置
- `WORDPRESS_URL`: WordPress 網站 URL (必須)
- `WORDPRESS_USERNAME`: WordPress 用戶名 (必須)
- `WORDPRESS_APP_PASSWORD`: WordPress 應用密碼 (必須)

> ⚠️ **注意**: WordPress 應用密碼不是你的登入密碼。你需要在 WordPress 後台 (用戶 > 你的個人檔案 > 應用密碼) 生成一個新的應用密碼。

### 搜索 API 配置 (可選)
- `SEARCH_API_KEY`: 搜索 API 鑰匙
- `SEARCH_ENGINE_ID`: 搜索引擎 ID

目前支持 Google Custom Search API。如果不配置，系統將使用模擬數據。

---

## 🤖 AI 代理人

### 📝 PlannerAgent (規劃代理人)
- **功能**: 創建內容生成的詳細計劃，包括主題、目標受眾、核心信息、結構大綱等
- **提示文件**: [`prompts/planner.md`](prompts/planner.md:1)

### 🔍 ResearchAgent (調研代理人)
- **功能**: 收集和整理與任務相關的資料，包括網路搜索、數據庫查詢等
- **依賴**: 可選的搜索 API

### ✍️ WriterAgent (撰寫代理人)
- **功能**: 根據計劃和資料撰寫高質量的文章內容
- **提示文件**: [`prompts/writer.md`](prompts/writer.md:1)

### 🎯 SEOAgent (SEO 代理人)
- **功能**: 优化内容的 SEO 屬性，包括標題、描述、關鍵字等

### 🔄 ReviewerAgent (審閱代理人)
- **功能**: 審閱和修改內容，確保質量、準確性、語法、流暢性和一致性

---

## ⚙️ 自定義工作流程

你可以通過修改 [`main.py`](main.py:1) 中的 `run_workflow` 方法來自定義工作流程。例如：

```python
def run_workflow(self, task_id: str) -> bool:
    # 自定義工作流程
    task = workflow_state.get_task(task_id)
    
    # 只執行撰寫和發布
    writer = WriterAgent(self.config)
    task.draft_content = writer.write_content(task)
    
    publisher = WordPressPublisher(self.config)
    wordpress_id, wordpress_url = publisher.publish_content(task)
    
    return True
```

---

## 📊 狀態管理

系統使用 [`state.py`](state.py:1) 來管理工作流程的狀態。你可以：

1. **保存狀態**:
   ```python
   factory.save_state("workflow_state.json")
   ```

2. **加載狀態**:
   ```python
   factory.load_state("workflow_state.json")
   ```

3. **查看任務狀態**:
   ```python
   task = workflow_state.get_task(task_id)
   print(f"任務狀態: {task.status}")
   ```

---

## 🐛 故障排除

### 常見問題

1. **OpenAI API 錯誤**
   - 確認 `OPENAI_API_KEY` 配置正確
   - 確認 API 鑰匙有足夠的額度
   - 確認使用的模型名稱正確

2. **WordPress 發布失敗**
   - 確認 `WORDPRESS_URL` 配置正確
   - 確認用戶名和應用密碼正確
   - 確認 WordPress 網站啟用了 REST API
   - 確認用戶有發布文章的權限

3. **搜索功能失敗**
   - 確認搜索 API 配置正確
   - 如果沒有配置搜索 API，系統將使用模擬數據

4. **依賴包缺失**
   - 運行 `pip install -r requirements.txt` 安裝所有依賴

### 日誌

系統使用 Python 的 `logging` 模組記錄日誌。你可以通過修改 [`main.py`](main.py:1) 中的日誌配置來調整日誌級別和格式。

---

## 📜 許可證

此專案採用 MIT 許可證 - 歡迎任何形式的使用和修改。

---

## 🤝 貢獻指南

歡迎貢獻！你可以：

1. **報告 Bug**: 在 Issue 區域提交 Bug 報告
2. **提交功能請求**: 在 Issue 區域提交功能請求
3. **提交 Pull Request**: 直接提交代碼修改

### 本地開發

1. Fork 專案
2. 創建功能分支 (`git checkout -b feature/your-feature`)
3. 提交修改 (`git commit -m 'Add some feature'`)
4. 推送到分支 (`git push origin feature/your-feature`)
5. 打開 Pull Request

---

## 📞 聯系方式

如有任何問題或建議，請聯系：
- Email: your-email@example.com
- GitHub: [your-github-profile](https://github.com/your-username)

---

**感謝你使用 AI WordPress Factory！** ✨
