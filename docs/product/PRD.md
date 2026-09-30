# AI WordPress Factory — 產品需求文件（PRD）

> 本文件為 **Repository-level 產品文件**。
> 它描述產品定位、長期方向與設計原則，**不是**實作規格書，**不構成**任何階段的承諾開發範圍。
> 本文件不會、也不得用來改寫既有架構或擴大當前階段範圍。

---

## 0. 文件狀態

| 項目 | 內容 |
|---|---|
| 文件定位 | 產品願景、產品循環、五層智慧、能力邊界與長期方向 |
| 與 ROADMAP.md 關係 | 互補。本文件說明「為什麼」與「往哪裡走」；ROADMAP.md 說明「何時做」 |
| 對 Phase 8 的影響 | **無。** Phase 8 範圍與行為維持現狀 |
| 變更方式 | 新構想先進入 backlog，成為方向後才進入實作範圍 |

---

## 1. 產品願景

> 讓企業即使沒有專職寫手，也能持續發現值得寫的內容，
> 將自己的產品、素材、案例與觀點轉化為可審核、可發布的網站內容，
> 從企業決策與市場成效中持續學習，
> 並以可控且具成本效益的 AI 資源持續改善內容成效。

---

## 2. 產品定位

### 2.1 本產品**不是**

- 「AI 幫你寫文章，然後發佈到 WordPress。」
- 單純的文章改寫／洗稿工具
- 只有生成、沒有審核與成效管理的內容機器

### 2.2 本產品**是**

**企業內容智慧與自動化系統（Enterprise Content Intelligence & Automation System）**

系統的長期目標是協助企業持續地：

```
發現值得寫的內容
    ↓
研究事實與觀點
    ↓
產出符合企業自身定位的內容
    ↓
安全地審核與發布
    ↓
衡量是否真的有效
    ↓
從企業決策與市場表現中學習
    ↓
在可控的 AI 成本下持續改善
```

### 2.3 系統如何隨時間變得更有用

系統應從四個面向持續累積判斷力：

1. **這家企業認為什麼值得寫。**
2. **老闆／編輯實際上是怎麼做內容決策的。**
3. **哪些內容在市場上真的有效果。**
4. **AI 的花費在哪裡真正產生了價值。**

> 這四項是本產品與「內容生成工具」的關鍵差異。
> 生成能力會被競爭者追平；**累積的判斷力不會**。

---

## 3. 核心產品循環

```
Idea / Source / Discover
        ↓
    Discover
        ↓
    Research
        ↓
      Plan
        ↓
     Create
        ↓
     Review
        ↓
    Publish
        ↓
    Measure
        ↓
     Learn
        ↓
    Improve
        ↺
```

### 3.1 循環的閉環意義

| 階段 | 產出 | 餵入下一階段的學習 |
|---|---|---|
| Discover | 內容機會 | 企業策略與歷史偏好 |
| Research | 研究套件 | 事實、來源、爭議、未知 |
| Plan | 內容策略 | 結構型態、角度、受眾意圖 |
| Create | 草稿 | 生成成本與品質 |
| Review | 核准／退回 | **決策行為本身是最強的偏好證據** |
| Publish | 已發布內容 | 發布路徑與對帳結果 |
| Measure | 成效訊號 | 市場回饋 |
| Learn | 偏好候選、成效洞察 | 回到 Discover 與 Plan |
| Improve | 改善後的策略 | — |

> 這個循環是**產品的敘事主軸**。所有功能都應說明自己在循環中的位置。

---

## 4. 五層智慧（Five Intelligence Layers）

五層智慧彼此獨立又互相供給。**Phase 8 主力建立的是第一層。**

| 層 | 核心問題 | 主要產出 | 現況 |
|---|---|---|---|
| 1. Production Intelligence | 「能不能可靠地跑完？」 | 可稽核的執行與發布 | **Phase 8 建立中** |
| 2. Content Intelligence | 「現在值得寫什麼？」 | 研究套件、內容機會 | 方向 |
| 3. Editorial Intelligence | 「這家企業／這位老闆偏好什麼？」 | 版本化偏好檔案 | 方向 |
| 4. Cost Intelligence | 「AI 資源有沒有花在刀口上？」 | 成本帳本、路由政策 | 跨層原則 |
| 5. Performance Intelligence | 「發布出去到底有沒有用？」 | 成效洞察與建議 | 方向 |

---

### 4.1 第一層：Production Intelligence（生產智慧）

**角色：可靠的內容生產基礎設施。**

這一層不負責「想出好點子」，而負責「**每一次執行都可被信任、可被重現、可被稽核**」。

涵蓋能力：

- Task / TaskRun（任務與執行嘗試）
- Workflow execution（工作流程執行）
- ContentVersion（不可變的內容版本）
- Preview（發布前預覽）
- Approval（人工核准）
- Revision（退回修改）
- Publication（發布意圖與紀錄）
- Reconciliation（遠端結果對帳）
- Recovery（失敗復原）
- Idempotency（冪等性）
- Workspace isolation（工作區隔離）

> **Phase 8 主要就是在建立這一層。**
> 這一層的價值不在功能多，而在於「每次執行的結果都是可以被追溯與信任的」。

---

### 4.2 第二層：Content Intelligence（內容智慧）

**核心問題：「對這家企業來說，現在值得寫什麼？」**

#### 4.2.1 未來能力方向

- Source Discovery（來源發現）
- Topic Discovery（主題發現）
- Evidence Analysis（證據分析）
- Perspective Discovery（觀點發現）
- Counterarguments（反對意見）
- Uncertainty（不確定性標示）
- Content Opportunities（內容機會）

#### 4.2.2 研究是雙向的

研究不應只是「先有主題再找資料」，也應能「**先看到資料再決定主題**」：

```
Topic  →  Research      （已知主題，研究佐證）
Research  →  Topic       （看到新證據，發現新主題）
```

#### 4.2.3 Researcher 的演進方向

Researcher 應逐漸演進為 **Research Editor** 角色。
它的產出不應只是一堆 URL，而應是**結構化研究套件（Research Package）**。

Research Package 建議欄位：

| 欄位 | 說明 |
|---|---|
| facts | 已確認事實 |
| evidence | 支持該事實的證據 |
| sources | 來源清單 |
| claims | 可被檢驗的具體主張 |
| different_positions | 不同立場／不同來源的观点 |
| counterarguments | 反對意見 |
| uncertainties | 已知不確定性 |
| follow_up_questions | 值得追問的問題 |
| company_brand_relevance | 與該企業／品牌的關聯性 |
| content_opportunities | 可衍生的內容機會 |

#### 4.2.4 責任邊界（避免重疊與越界）

| 角色 | 負責 | 不負責 |
|---|---|---|
| **Researcher** | 事實、證據、來源、多方觀點、不確定性 | 內容角度、結構、策略 |
| **Planner / Content Strategist** | 內容角度、受眾意圖、架構、品牌關聯 | 撰寫內文、事實查核 |
| **Writer** | 依既定策略撰寫 | 自行改變策略與角度 |

> 這三層邊界的價值在於：**研究不越權做決策，寫作不越權改策略。**

#### 4.2.5 Structure Library（結構型態庫）

內容結構應可選擇，而非一律硬套。建議長期維護的結構型態：

| 結構 | 適用情境 |
|---|---|
| Problem → Solution | 客戶痛點導向 |
| Claim → Evidence | 觀點／主張導向 |
| Pros → Cons → Judgment Criteria | 比較、選購 |
| Thesis → Antithesis → Synthesis | 議題／辯論型 |
| What → Why → How | 教學／知識型 |
| Before → Process → After | 見證／案例型 |
| News → Context → Impact → Brand View | 時事回應型 |
| Compare A vs B | 產品比較型 |
| Myth → Evidence → Correction | 謬誤澄清型 |
| Question → Evidence → Answer | FAQ／搜尋型 |

> **重要原則：不是每一篇文章都必須使用辯證式（pro-con）結構。**
> 結構應由 Planner 依內容機會與讀者意圖決定。

---

### 4.3 第三層：Editorial Intelligence（編輯智慧）

**核心問題：「這家企業／這位老闆偏好什麼？」**

#### 4.3.1 學習來源：真實決策

系統應從以下**真實發生過的決策**中學習，而非從抽象設定中猜測：

- 主題選擇
- 角度選擇
- 退回修改的原因
- 核准
- 駁回
- 明確指令

#### 4.3.2 偏好優先順序

```
Company Rules（公司規則）
        >
Explicit Owner Preferences（老闆明確指定）
        >
Learned Preferences（系統學到的）
        >
Agent Defaults（代理人預設）
```

> 上層永遠壓過下層。學到的偏好**不得**覆蓋公司規則或老闆的明確指定。

#### 4.3.3 LearnerAgent 的硬性限制

LearnerAgent **絕不可**靜默改寫生產提示（production prompts）。

學習流程必須是：

```
Behavior / Feedback（行為與回饋）
        ↓
    LearnerAgent
        ↓
Preference Candidate（偏好候選）
        ↓
Evidence（證據）
        ↓
Versioned Preference Profile（版本化偏好檔案）
```

學習結果必須具備四個性質：

| 性質 | 意義 |
|---|---|
| Evidence-based | 每條偏好都附帶支持它的證據 |
| Explainable | 能說明「為什麼推導出這條偏好」 |
| Versioned | 偏好有版本，可比較前後變化 |
| Reviewable | 使用者可檢視、修改、駁回 |

#### 4.3.4 使用者可控性

UI 最終應允許老闆／編輯：

- **保留**一條已學習到的偏好
- **修改**它
- **駁回**它

#### 4.3.5 學習目標不只是文風

系統最終要學會回答的不只是「怎麼寫」，而是：

> **「這家企業認為什麼值得寫，以及從什麼角度寫。」**

---

### 4.4 第四層：Cost Intelligence（成本智慧）

> **成本控制是架構原則，不是後期最佳化。**

#### 4.4.1 核心原則：Cost-Aware by Design

| 規則 | 說明 |
|---|---|
| 不重算已知結果 | 已經算過的結果不重跑 |
| 不送不必要的脈絡 | 最小必要上下文 |
| 不用超出任務需求的昂貴模型 | Right Model for the Job |
| 適合時先用確定性邏輯 | Deterministic before AI |
| 重用已持久化的產物 | Reuse persisted artifacts |
| 增量學習，而非重跑全部歷史 | Incremental learning |

#### 4.4.2 六項成本原則

```
Reuse before Regenerate        先重用，再重生產
Pass structured artifacts       傳結構化產物，不是全部
Right Model for the Job        對的模型做對的事
Deterministic before AI        能用規則就不要用 AI
Incremental Learning           增量學習
Cache / Persist valuable AI work  有價值的 AI 產出要留下
```

#### 4.4.3 執行漏斗（Execution Funnel）

```
大型來源集合
    ↓
確定性過濾
    ↓
低成本相關性分類
    ↓
偏好 / 品牌過濾
    ↓
小型候選集合
    ↓
深度研究
    ↓
高價值內容
    ↓
僅在有理由時使用頂級模型做生成
```

> **越靠近漏斗頂端的作業，每一筆應該越便宜。**
> 這是整個成本智慧最重要的直覺。

#### 4.4.4 未來 Workspace AI Policy 可能包含

- 月度預算
- 每日預算
- 單一任務／單篇文章成本上限
- 研究呼叫次數上限
- 警告門檻
- 硬性上限
- 偏好的低成本供應商
- 頂級模型使用門檻

#### 4.4.5 Kill Switch（熔斷開關）可能存在於

- Workspace AI
- TEXT capability
- IMAGE capability
- VISUAL / multimodal capability
- DISCOVERY

> **重要：熔斷開關應停止新的 AI 支出，而不應不必要地關閉產品其餘部分。**
>
> 也就是說，Preview、Approval、Dashboard，以及**已產生的既有產物**，在安全的情況下仍應可用。

#### 4.4.6 預算執行順序

```
AI Request
    ↓
Budget / Policy Gate      ← 付費 AI 執行前先過關
    ↓
Provider Routing
    ↓
AI Call
    ↓
Usage / Cost Ledger
```

#### 4.4.7 Free-First Provider Strategy

針對中小企業，系統應在品質、隱私、條款與可靠性允許的前提下，
善用**合法且免費或低成本的 AI API**。

**五項特質：**

```
Free-first          先用免費
Cost-aware          成本感知
Quality-gated       品質有把關
Privacy-aware       隱私有考量
Never waste computation  不浪費運算
```

> **Free-first 不等於 Free-only。**

**概念性路由順序：**

1. 確定性解法
2. 既有快取／已持久化產物
3. 合適的免費層供應商
4. 低成本付費供應商
5. 有理由時才使用頂級模型

當免費額度用盡時，**是否允許付費 fallback，應由 Workspace policy 決定**。

#### 4.4.8 Provider Intelligence（供應商智慧）

> Provider Intelligence 是 **Cost Intelligence 的子系統**，不是第六層智慧。

**長期目的：**系統應能主動發現新的 AI 供應商機會，例如：

- 免費額度
- 價格變動
- 新模型
- 新能力
- 更低成本的替代方案

**概念流程：**

```
Provider Discovery
        ↓
Capability Analysis
        ↓
Pricing / Free-tier Analysis
        ↓
Privacy / Terms / Commercial-use Review
        ↓
Sandbox Evaluation
        ↓
Quality / Cost / Latency Comparison
        ↓
Recommendation
        ↓
Owner Notification
        ↓
Owner Approval
        ↓
Provider Routing
```

> **核心安全原則：**
> 系統可以**自動發現、評估並推薦**供應商，
> 但**絕不可**在未經核准的政策／所有者授權下，**靜默地**把生產工作負載切換到新發現的供應商。

**Business-facing UI 應以簡單語言說明價值：**

- 預估每月省下的費用
- 適合改用該替代供應商的工作負載
- 品質評估結果
- 隱私／政策警告
- 是否需要付費 fallback

---

### 4.5 第五層：Performance Intelligence（成效智慧）

**核心問題：「發布出去的內容到底有沒有用？」**

#### 4.5.1 兩種回饋來源

```
Human Feedback（人的回饋）
        +
Market Feedback（市場的回饋）
```

#### 4.5.2 潛在訊號

**搜尋：**
impressions、clicks、CTR、queries、visibility

**流量／互動：**
page views、engaged sessions、engagement time

**轉換：**
inquiry、form submission、phone click、messaging click、download、quotation request、appointment、其他由 workspace 定義的目標

**AI 成本：**
calls、input tokens、output tokens、image generations、估計／實際 AI 成本

#### 4.5.3 成效需依 Workspace 商業目標解讀

| 商業型態 | 成功定義 |
|---|---|
| 企業網站 | 潛在客戶開發 |
| 電子商務 | 營收 |
| 診所／醫療 | 預約 |
| B2B | 產品 → 下載 → 詢問 |
| 內容媒體 | 流量／互動／訂閱 |

#### 4.5.4 重要產品原則：Measure → Insight → Action

**不要停在「顯示儀表板」。**

系統最終應能說明：

- 什麼有效
- 什麼沒用
- 為什麼可能有關係
- **接下來該寫什麼內容**

**長期概念：**

```
AI Cost
    ↓
Content
    ↓
Traffic
    ↓
Conversion
    ↓
Business Value
```

這使得未來的 **Content ROI 分析**成為可能。

---

## 5. Content Input Modes（內容輸入模式）

產品最終應支援三種進入模式。

### 5.1 IDEA（已知主題）

**情境：**客戶已經知道自己要寫什麼。

```
客戶已有主題
    ↓
系統協助研究、擬定策略、產出內容
```

### 5.2 SOURCE（已有素材）

**情境：**客戶已經有素材，例如：

- 產品照片
- 作品集／專案照片
- Word 文件
- PDF
- 產品資訊
- 參考 URL
- 既有內部素材

**系統應理解這些素材，並轉化為原生的、符合企業自身定位的內容。**

### 5.3 DISCOVER（尚未確定）

**情境：**客戶不知道要寫什麼。

系統可從下列來源發現機會：

- 產業來源
- RSS／新聞
- 政府公告
- 研究
- 競爭對手
- 搜尋趨勢
- 自訂來源

### 5.4 核心原則：參考 URL 的處理鏈

```
Reference URL
    ↓
分析 / 事實 / 角度 / 佐證
    ↓
公司知識 / 產品 / 案例 / 觀點
    ↓
原創文章
```

> **本產品不得被定義為「文章改寫系統」。**
>
> 參考資料的價值在於**提供事實與角度的原料**，
> 而產出必須建立在**企業自己的知識、產品、案例與觀點**之上。

---

## 6. 兩個學習循環（Two Learning Loops）

### 6.1 Owner Loop（企業決策循環）

```
Select / Edit / Approve / Reject
        ↓
    Learner
        ↓
Owner Preference
```

### 6.2 Market Loop（市場成效循環）

```
Publish
    ↓
Measure
    ↓
Performance Analysis
    ↓
Content Performance
```

### 6.3 未來的內容決策

未來的內容決策應**同時結合**三個來源：

```
Company Strategy（公司策略）
        +
Owner Preference（老闆偏好）
        +
Market Performance（市場表現）
```

---

## 7. 與當前 Phase 8 的關係

> **本文件不得、也不會導致 Phase 8 重寫。**

### 7.1 Phase 8 的定位不變

Phase 8 仍然是：

**Production Intelligence Foundation（生產智慧基礎）**

### 7.2 當前 roadmap 維持現狀

```
8.3   Approval / Revision / Publication 收尾
8.4   Recovery
8.5   Multi-task UX
8.6   Production / Deployment
```

### 7.3 既有核心概念持續有效

以下概念仍是有效基礎，本產品願景不取代它們：

`Task`、`TaskRun`、`ContentVersion`、`Preview`、`Approval`、`Revision`、`Publication`、`Reconciliation`、`Workspace`、`Provider`、`Idempotency`、`Worker`、`Events`

### 7.4 範圍紀律

> **本文件中描述的第二、三、五層智慧與未來 roadmap，皆為方向性敘述，不是承諾的實作範圍。**
>
> 有價值的構想**不得**成為擴大當前階段範圍的理由。

---

## 8. Phase 8.4 的成本原則（記錄為未來設計輸入）

> 本節**只記錄原則，不設計 Phase 8.4 實作**。

Recovery（復原）除了問：

> 「失敗之後要怎麼重跑？」

還應該問：

> **「有哪些已經成功、且 AI 付費成本已經產生的輸出可以被重用？**
> **以及可以從哪個 checkpoint 續跑，而不必浪費不必要的 token／API 支出？」**

這是 Recovery 與 Cost-Aware by Design 的交會點，應在 8.4 設計時被明確考慮。

---

## 9. 未來 Roadmap（僅產品層面方向，非承諾範圍）

> 以下為**方向性**描述，不是已承諾的實作範圍。

### Phase 9 — Content Intelligence

可能的內容：

- 多來源接入
- 文件／圖片／URL 接入
- 來源收集
- Research Package
- Topic Discovery
- Content Opportunity

### Phase 10 — Editorial Intelligence

可能的內容：

- Preference Evidence
- 選擇行為學習
- 修改行為學習
- Owner Preference Profile
- LearnerAgent 演進

### Phase 11 — Performance Intelligence

可能的內容：

- Search Console 整合
- Analytics 整合
- 轉換追蹤
- Content Performance
- 建議循環

### Cost Intelligence 的定位

**Cost Intelligence 是跨層的（cross-cutting）。**

它應**逐步出現在各個階段中**，而不是被延後到某一個孤立階段。

---

## 10. 產品原則（Product Principles）

| # | 原則 | 內涵 |
|---|---|---|
| 1 | **Product First / Vertical Slice** | 先做能走完整條路的垂直切片，不做半成品元件堆疊 |
| 2 | **Config over Code** | 行為差異優先用設定表達，而不是改程式碼 |
| 3 | **Evidence before Learning** | 沒有證據就不學習；沒有學習就不影響生產 |
| 4 | **Human + Market Feedback** | 同時運用人的決策與市場的表現 |
| 5 | **Cost-Aware by Design** | 成本是架構原則，不是後期最佳化 |
| 6 | **Free-first but not Free-only** | 優先利用合法免費資源，但不被免費綁架 |
| 7 | **Measure → Insight → Action** | 數據必須能轉成洞察與下一步行動 |
| 8 | **Core Stability** | 已穩定的核心不為新功能反覆重寫 |
| 9 | **新構想先進 backlog** | 任何新想法先進入 backlog，不直接進入實作範圍 |
| 10 | **不因構想有價值就擴大當前階段** | 再有價值的未來構想，也不能成為現在加做事情的藉口 |

---

## 11. 附錄：名詞對照

| 英文 | 中文 | 說明 |
|---|---|---|
| Production Intelligence | 生產智慧 | 可靠執行、可稽核的基礎設施 |
| Content Intelligence | 內容智慧 | 值得寫什麼 |
| Editorial Intelligence | 編輯智慧 | 這家企業偏好什麼 |
| Cost Intelligence | 成本智慧 | AI 資源有無花在刀口上 |
| Performance Intelligence | 成效智慧 | 發布內容是否有效 |
| Research Package | 研究套件 | 結構化研究產出 |
| Structure Library | 結構型態庫 | 可選用的內容結構範型 |
| Preference Profile | 偏好檔案 | 版本化的偏好集合 |
| Vertical Slice | 垂直切片 | 可走完整流程的最小完整功能 |
| Cross-cutting | 跨層 | 橫跨多個階段持續存在 |

---

*本文件為產品層面文件。其內容不構成對任何未來階段的實作承諾，亦不改變當前階段的範圍與行為。*
