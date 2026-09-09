# Phase 7A — Frontend Contracts Architecture Review

**Date:** 2026-09-08  
**Reviewer:** Kilo  
**Scope:** Read-only architecture review — no code modified  
**Status:** COMPLETE

---

## 1. Executive Summary

This document defines the minimum contract boundary for introducing Frontend production into AI WordPress Factory.

**Primary recommendation:** Three contracts are justified and sufficient:

| Contract | Purpose |
|----------|---------|
| **FrontendRequest** | Request frontend work from Planner to FrontendAgent |
| **FrontendResult** | Return generated frontend from FrontendAgent to downstream tools |
| **GreenLightConversionResult** | Return conversion result from GreenLightConverter to FrontendAgent/Publisher |

**Contracts rejected:**
- `DesignSpec` — overkill; design intent lives in prompt/context, not a separate structured contract
- `FrontendCode` — redundant with `FrontendResult`
- `FrontendValidationResult` — validation can be expressed as fields within `FrontendResult`

**Readiness:** READY FOR PHASE 7B — contracts are minimal, typed, and aligned with existing project conventions.

---

## 2. Repository Evidence

### Existing Contract Patterns

**File:** `contracts.py`

| Pattern | Example | Usage |
|---------|---------|-------|
| `@dataclass` | `CritiqueResult`, `ReviewResult`, `ImageGenerationRequest`, `ImageGenerationResult` | All contracts use dataclasses with default values |
| `Enum` | `ReviewAction`, `CritiqueAction`, `AIPatternType` | Action/status enums use string values |
| `field(default_factory=list)` | `issues: List[str] = field(default_factory=list)` | Mutable defaults use factory |
| `Optional[T]` | `image_url: Optional[str] = None` | Nullable fields use Optional with None default |
| `Dict[str, Any]` | `metadata: Dict[str, Any] = field(default_factory=dict)` | Arbitrary metadata uses Dict[str, Any] |
| `task_id: str = ""` | All result/request contracts include `task_id` | Task correlation is mandatory |

**Naming conventions:**
- Request contracts: `*Request`
- Result contracts: `*Result`
- Actions/statuses: `*Action`, `*Status`
- Values are snake_case
- String enums use uppercase values

### Existing State Patterns

**File:** `state.py`

| Pattern | Example | Usage |
|---------|---------|-------|
| `Task` dataclass | Central business object | Carries all task data through workflow |
| `TaskStatus` enum | `PENDING`, `PLANNING`, `WRITING`, ... | Execution state |
| `ContentType` enum | `BLOG_POST`, `PAGE`, `PRODUCT` | Content classification |
| `WorkflowState` | `tasks: Dict[str, Task]` | Execution tracking |
| `to_dict()` / `from_dict()` | Serialization support | Persistence |

### Existing Workflow Patterns

**File:** `main.py`

```python
# Step pattern
workflow_state.update_task_status(task_id, TaskStatus.PLANNING)
agent = self._get_agent("planner")
result = agent.create_plan(task)
task.plan = result  # Direct assignment to Task

# Agent invocation
writer = self._get_agent("writer")
draft_content = writer.write_content(task)
task.draft_content = draft_content
```

**Key observation:** The workflow directly assigns Agent results to `Task` fields. There is no intermediate contract layer for most Agent outputs. Contracts are used only for structured results that need field-level access (e.g., `CritiqueResult`, `ReviewResult`, `ImageGenerationResult`).

### Existing Tool Patterns

**File:** `tools/wordpress.py`

```python
class WordPressPublisher(BaseTool):
    def publish_content(self, task, featured_media_id=None) -> Tuple[Optional[int], Optional[str]]:
        # Reads from task directly
        content = task.final_content or task.optimized_content or task.draft_content
        # Returns simple tuple, not a contract
        return post_id, post_url
```

**Key observation:** Tools currently read directly from `Task` and return simple tuples/dicts. They do not use contracts. `WordPressPublisher` is the only existing Tool, and its interface is simple: `(task, optional_args) -> (id, url)`.

### Existing Provider Patterns

**File:** `providers/image_provider.py`

```python
class ImageProvider(ABC):
    @abstractmethod
    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        raise NotImplementedError
```

**Key observation:** Providers use contracts for both input and output. `ImageProvider.generate()` accepts `ImageGenerationRequest` and returns `ImageGenerationResult`. This is the pattern that `GreenLightConverter` should follow.

---

## 3. Frontend Boundary Analysis

### What crosses the Planner → FrontendAgent boundary?

The Planner currently produces:

```python
task.plan = {
    "主題": str,
    "目標受眾": str,
    "核心信息": List[str],
    "結構大綱": List[Dict],
    "關鍵字": List[str],
    "參考資源": List[str],
}
```

FrontendAgent needs:

| Need | Source | Currently Available? |
|------|--------|---------------------|
| Content text | `task.final_content` | Yes, but frontend may run before content is finalized |
| Content structure | `task.plan["結構大綱"]` | Yes |
| SEO metadata | `task.seo_title`, `task.seo_description`, `task.seo_keywords` | Yes |
| Brand constraints | Skills (Bencent, Taste, Minimalist, UI Rules) | Yes, via skill loader |
| Animation requirement | Not currently tracked | No |
| Page/section type | `task.content_type` | Partial (BLOG_POST, PAGE, PRODUCT) |
| Visual direction | Not currently tracked | No |

### Key Finding

**FrontendRequest IS justified.**

The Planner produces a content plan, but frontend generation needs additional information that is not currently in `Task`:
- Whether animation is required
- Whether this is a full page or a section
- Visual direction/brand constraints beyond what Skills provide
- Explicit frontend scope

A `FrontendRequest` contract creates a clear boundary between content planning and frontend production. It is not over-engineering because:
1. It carries information that does not naturally belong in `Task`
2. It makes the FrontendAgent interface explicit and testable
3. It allows the workflow to pass frontend-specific parameters without polluting `Task` with frontend-only fields
4. It follows the existing pattern of `ImageGenerationRequest` for provider boundaries

---

## 4. FrontendRequest Design

### Recommended Fields

```python
@dataclass
class FrontendRequest:
    task_id: str = ""
    content_type: ContentType = ContentType.BLOG_POST
    frontend_scope: str = "page"  # "page", "section", "component"
    animation_required: bool = False
    design_brief: str = ""
    brand_constraints: Dict[str, Any] = field(default_factory=dict)
    seo_title: str = ""
    seo_description: str = ""
    seo_keywords: List[str] = field(default_factory=list)
    content_outline: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
```

### Field Justification

| Field | Why Required | Producer | Consumer | In Task? |
|-------|-------------|----------|----------|----------|
| `task_id` | Correlate frontend work with content task | Workflow | FrontendAgent, downstream | Yes (Task.id) |
| `content_type` | Determine layout density, component patterns | Workflow/Planner | FrontendAgent | Yes (Task.content_type) |
| `frontend_scope` | Page vs section vs component changes output size/structure | Planner/Workflow | FrontendAgent | No — frontend-specific |
| `animation_required` | Controls GSAP skill loading | Planner/Workflow | FrontendAgent | No — frontend-specific |
| `design_brief` | Visual direction from content plan | Planner | FrontendAgent | No — belongs in request |
| `brand_constraints` | Explicit brand rules for this task | Workflow/Skills | FrontendAgent | Partial (via Skills) |
| `seo_title` | Ensure frontend supports SEO title | SEOAgent | FrontendAgent | Yes (Task.seo_title) |
| `seo_description` | Ensure frontend supports SEO description | SEOAgent | FrontendAgent | Yes (Task.seo_description) |
| `seo_keywords` | Ensure frontend supports SEO keywords | SEOAgent | FrontendAgent | Yes (Task.seo_keywords) |
| `content_outline` | Structural outline for layout planning | Planner | FrontendAgent | Yes (Task.plan) |
| `metadata` | Extension point for future fields | Any | Any | No |

### What is NOT included

| Field | Why Excluded |
|-------|-------------|
| `final_content` | FrontendAgent should read from `Task` directly, not via contract |
| `image_url` | Not relevant to frontend generation |
| `wordpress_id` | Not relevant at generation time |
| `research_data` | Not relevant to frontend layout |

---

## 5. FrontendResult Design

### Recommended Fields

```python
@dataclass
class FrontendResult:
    task_id: str = ""
    success: bool = False
    html: Optional[str] = None
    css: Optional[str] = None
    javascript: Optional[str] = None
    blocks: Optional[str] = None
    validation_status: str = "pending"  # "pending", "passed", "failed", "warnings"
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    animation_used: bool = False
    gsap_subskills_loaded: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
```

### Field Justification

| Field | Why Required | Producer | Consumer | Persist? |
|-------|-------------|----------|----------|----------|
| `task_id` | Correlate result with task | FrontendAgent | Workflow, GreenLightConverter, Publisher | Yes |
| `success` | Indicate generation success/failure | FrontendAgent | Workflow | Yes |
| `html` | Generated HTML | FrontendAgent | GreenLightConverter, Publisher | Yes |
| `css` | Generated CSS | FrontendAgent | GreenLightConverter | Yes |
| `javascript` | Generated JS | FrontendAgent | GreenLightConverter | Yes |
| `blocks` | Greenshift blocks after conversion | GreenLightConverter | WordPressPublisher | Yes |
| `validation_status` | Validation outcome | FrontendValidator | Workflow | Yes |
| `errors` | Blocking issues | FrontendAgent/Validator | Workflow | Yes |
| `warnings` | Non-blocking issues | FrontendAgent/Validator | Workflow | Yes |
| `animation_used` | Track GSAP usage | FrontendAgent | Analytics, debugging | Optional |
| `gsap_subskills_loaded` | Track which GSAP skills were used | FrontendAgent | Analytics, debugging | Optional |
| `metadata` | Extension point | Any | Any | Optional |

### What is NOT included

| Field | Why Excluded |
|-------|-------------|
| `design_spec` | Design intent is in the prompt, not a separate structured artifact |
| `frontend_code` | Redundant — `html`, `css`, `javascript` already present |
| `conversion_log` | Implementation detail of GreenLightConverter, not business result |
| `node_version` | Implementation detail, not business result |

---

## 6. DesignSpec Decision

**Recommendation: DO NOT create DesignSpec.**

### Evaluation

| Criterion | DesignSpec | No DesignSpec |
|-----------|-----------|---------------|
| Creates real boundary | No | Yes — FrontendResult is the boundary |
| Enables validation | No — validation happens on FrontendResult | Yes |
| Enables persistence | No — FrontendResult is persisted | Yes |
| Enables reuse | No | Yes |
| Adds complexity | Yes | No |
| Matches existing patterns | No | Yes — ImageGenerationResult has no intermediate spec |

### Rationale

In this project, "design" is a reasoning step inside FrontendAgent, not a separate deliverable. The Agent reasons about design, then generates code. There is no downstream consumer that needs a structured design specification separate from the generated code.

Creating `DesignSpec` would:
1. Add an unnecessary contract layer
2. Require FrontendAgent to produce two outputs instead of one
3. Not enable any validation, persistence, or reuse that FrontendResult doesn't already handle
4. Add complexity without architectural benefit

**DesignSpec is rejected.**

---

## 7. FrontendCode Decision

**Recommendation: DO NOT create FrontendCode.**

### Evaluation

`FrontendCode` would be a contract holding `html`, `css`, `javascript`. This is already covered by `FrontendResult`.

| Criterion | FrontendCode | Use FrontendResult |
|-----------|-------------|-------------------|
| Adds clarity | No | Yes — simpler |
| Matches patterns | No | Yes — ImageGenerationResult holds all output |
| Enables validation | No | Yes |
| Adds complexity | Yes | No |

**FrontendCode is rejected. FrontendResult already contains the necessary fields.**

---

## 8. GreenLightConversionResult Design

### Recommended Fields

```python
@dataclass
class GreenLightConversionResult:
    task_id: str = ""
    success: bool = False
    blocks: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    conversion_metadata: Dict[str, Any] = field(default_factory=dict)
```

### Field Justification

| Field | Why Required | Producer | Consumer | Persist? |
|-------|-------------|----------|----------|----------|
| `task_id` | Correlate conversion with task | GreenLightConverter | FrontendAgent, Publisher | Yes |
| `success` | Indicate conversion success | GreenLightConverter | FrontendAgent, Workflow | Yes |
| `blocks` | Converted Greenshift block code | GreenLightConverter | WordPressPublisher | Yes |
| `errors` | Blocking conversion errors | GreenLightConverter | FrontendAgent | Yes |
| `warnings` | Non-blocking conversion warnings | GreenLightConverter | FrontendAgent | Optional |
| `conversion_metadata` | Conversion stats, timing, etc. | GreenLightConverter | Observability | Optional |

### What is NOT included

| Field | Why Excluded |
|-------|-------------|
| `html` | Input to converter, not output |
| `css` | Input to converter, not output |
| `javascript` | Input to converter, not output |
| `node_version` | Implementation detail |
| `convert_js_version` | Implementation detail |
| `stdout` | Implementation detail |
| `stderr` | Implementation detail |

### Boundary Enforcement

The contract explicitly separates:
- **Input:** HTML/CSS/JS from `FrontendResult`
- **Output:** Greenshift blocks
- **Internal:** Node.js execution details are NOT exposed

This preserves the Python ↔ Node.js boundary and keeps the contract clean.

---

## 9. FrontendValidationResult Decision

**Recommendation: DO NOT create a separate FrontendValidationResult contract.**

Instead, validation results are expressed as fields within `FrontendResult`:

```python
# In FrontendResult
validation_status: str = "pending"  # "pending", "passed", "failed", "warnings"
errors: List[str] = field(default_factory=list)
warnings: List[str] = field(default_factory=list)
```

### Rationale

1. Validation is a step in the frontend pipeline, not a separate module boundary
2. The existing `ReviewResult` pattern uses a single dataclass with `passed`, `score`, `issues`, `feedback` — not a separate validation contract
3. Creating a separate contract would require an additional mapping step: `FrontendValidationResult` → `FrontendResult` → downstream
4. Validation errors and warnings are naturally consumed by the same workflow step that receives `FrontendResult`

**If validation becomes complex enough to warrant its own contract later, that can be revisited. For Phase 7B, validation fields belong in FrontendResult.**

---

## 10. Task vs WorkflowState Ownership

### Ownership Rules

| Concept | Owner | Rationale |
|---------|-------|-----------|
| **Task** | Business data/content | Carries content, plan, SEO, images, frontend output through the entire workflow |
| **WorkflowState** | Execution state | Tracks which step is active, current task ID, global execution state |
| **FrontendRequest** | Workflow → FrontendAgent boundary | Carries frontend-specific parameters that don't belong in Task |
| **FrontendResult** | FrontendAgent → downstream boundary | Carries generated frontend output before conversion/publishing |
| **GreenLightConversionResult** | GreenLightConverter → Publisher boundary | Carries converted blocks and conversion metadata |

### What belongs in Task

| Field | Reason |
|-------|--------|
| `final_content` | Core business output — persists through workflow |
| `plan` | Content plan — may be referenced by frontend |
| `seo_title`, `seo_description`, `seo_keywords` | SEO metadata — may inform frontend |
| `frontend_html`, `frontend_css`, `frontend_javascript` | Generated frontend — persists through conversion/publishing |
| `frontend_blocks` | Converted blocks — persists to WordPress |
| `frontend_validation_status` | Validation outcome — persists for audit |
| `frontend_errors`, `frontend_warnings` | Validation issues — persists for audit |

### What belongs in contracts (not Task)

| Contract | Why Not in Task |
|----------|----------------|
| `FrontendRequest` | Carries parameters, not persistent state |
| `FrontendResult` | Transient output; downstream tools consume it and produce persisted state |
| `GreenLightConversionResult` | Transient conversion output; blocks are persisted to Task |

### What belongs in WorkflowState

| Field | Why In WorkflowState |
|-------|---------------------|
| `current_task_id` | Execution tracking |
| `tasks` | All tasks in workflow |
| `global_state` | Global execution data |

WorkflowState does NOT need frontend-specific fields. Frontend execution state is tracked via `TaskStatus`:

```python
FRONTEND_DESIGN = auto()
FRONTEND_CODE = auto()
FRONTEND_VALIDATION = auto()
FRONTEND_CONVERSION = auto()
FRONTEND_PUBLISHING = auto()
```

---

## 11. Workflow Integration

### Recommended Position

FrontendAgent enters the workflow **after content review** and **before publishing**:

```text
Task
 ↓
Planner
 ↓
Research
 ↓
Writer
 ↓
Critic
 ↓
SEO
 ↓
Reviewer
 ↓
FrontendAgent       ← New
 ↓
FrontendValidator   ← New (can be part of FrontendAgent or separate)
 ↓
GreenLightConverter  ← New Tool
 ↓
WordPressPublisher
 ↓
COMPLETED
 ↓
Human Review
 ↓
Learner
```

### Rationale

1. **Frontend depends on finalized content** — `task.final_content` must exist
2. **Frontend may depend on SEO** — `task.seo_title`, `task.seo_description` should be available
3. **Frontend should be reviewed before publishing** — validation before WordPress upload
4. **Serial extension is simpler than parallel** — no content synchronization needed

### Proposed TaskStatus Values

Add to `TaskStatus`:

```python
FRONTEND_DESIGN = auto()      # Frontend design reasoning
FRONTEND_CODE = auto()        # HTML/CSS/JS generation
FRONTEND_VALIDATION = auto()  # Frontend validation
FRONTEND_CONVERSION = auto()  # GreenLight conversion
FRONTEND_PUBLISHING = auto()  # Frontend WordPress publishing
```

### Router Integration

The existing Router already handles publish/rewrite/research/seo/fail actions. Frontend workflow does not need Router integration unless frontend validation fails and requires routing back to FrontendAgent. In that case, the existing retry mechanism in `main.py` handles it:

```python
while task.retry_count < task.max_retries:
    # ... validation ...
    if not validation_passed:
        task.retry_count += 1
        # route back to FrontendAgent
        continue
```

---

## 12. GreenLightConverter Boundary

### Conceptual Interface

```python
class GreenLightConverter:
    def __init__(self, config):
        self.config = config
        self.scripts_dir = os.path.join(os.path.dirname(__file__), "..", "skills", "greenlight-vibe", "scripts")
    
    def convert(self, html: str, css: str, js: str) -> GreenLightConversionResult:
        """Convert HTML/CSS/JS to Greenshift blocks.
        
        Args:
            html: HTML markup
            css: CSS styles
            js: JavaScript code
            
        Returns:
            GreenLightConversionResult with blocks or errors
        """
        pass
    
    def deconvert(self, blocks: str) -> str:
        """Convert Greenshift blocks back to HTML.
        
        Args:
            blocks: Greenshift block markup
            
        Returns:
            HTML string
        """
        pass
```

### Subprocess Boundary

```python
def convert(self, html: str, css: str, js: str) -> GreenLightConversionResult:
    import subprocess
    import tempfile
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as f:
        f.write(html)
        input_path = f.name
    
    output_path = input_path + '.blocks.txt'
    
    try:
        result = subprocess.run(
            ['node', os.path.join(self.scripts_dir, 'convert.js'), input_path, '-o', output_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        
        if result.returncode != 0:
            return GreenLightConversionResult(
                task_id="",
                success=False,
                errors=[result.stderr],
            )
        
        with open(output_path, 'r', encoding='utf-8') as f:
            blocks = f.read()
        
        return GreenLightConversionResult(
            task_id="",
            success=True,
            blocks=blocks,
        )
    except subprocess.TimeoutExpired:
        return GreenLightConversionResult(
            task_id="",
            success=False,
            errors=["Conversion timed out"],
        )
    except Exception as e:
        return GreenLightConversionResult(
            task_id="",
            success=False,
            errors=[str(e)],
        )
    finally:
        for path in [input_path, output_path]:
            if os.path.exists(path):
                os.unlink(path)
```

### Key Requirements

| Requirement | Implementation |
|-------------|----------------|
| Node.js availability | Check at tool initialization; fail fast |
| Temp files | `tempfile` module; cleanup in finally |
| Error handling | Capture stderr, exit codes; return structured errors |
| Timeout | 30 seconds; configurable |
| Determinism | Same input → same output; no LLM in conversion path |
| Testability | Can mock subprocess.run for tests |

### What GreenLightConverter Does NOT Do

- Validate HTML/CSS/JS syntax (that's a separate validator)
- Generate HTML/CSS/JS (that's FrontendAgent)
- Load Skills into LLM context (Skills are for Agents, not Tools)
- Call WordPress APIs (that's WordPressPublisher)

---

## 13. GSAP Skill Boundary

### Confirmed Architecture

GSAP remains **Skill / Knowledge only**. It is NOT:
- A Tool
- An Agent
- A Provider
- Executable Python code

### Loading Strategy

GSAP sub-skills are loaded only when animation is required:

```python
# FrontendAgent method with animation
def _generate_animation(self, ...):
    return self.call_ai(
        prompt,
        required_skills=[
            "Bencent",
            "gsap-core",
            "gsap-scrolltrigger",  # if scroll animation
        ],
    )
```

### Project-Specific Relevance

| Sub-skill | Needed? | Rationale |
|-----------|---------|-----------|
| gsap-core | **YES** | Core animation API for all animation |
| gsap-timeline | **YES** | Sequencing animations |
| gsap-scrolltrigger | **YES** | Scroll-linked animations common in WordPress |
| gsap-plugins | Conditional | Only if advanced effects needed |
| gsap-utils | **YES** | Utility functions |
| gsap-react | NO | Project uses vanilla HTML/CSS/JS |
| gsap-performance | Conditional | Optimization |
| gsap-frameworks | NO | Project uses vanilla HTML/CSS/JS |

### Default Behavior

**GSAP skills should NOT be loaded for non-animated frontend work.**

Default frontend methods load:
- Bencent
- Taste sections
- Minimalist
- UI Rules

Animation methods additionally load:
- Bencent
- Relevant GSAP sub-skills

---

## 14. Frontend Contract Matrix

| Contract | Producer | Consumer | Purpose | Persist? | Required? |
|----------|----------|----------|---------|----------|-----------|
| **FrontendRequest** | Workflow/Planner | FrontendAgent | Carry frontend-specific parameters across Agent boundary | No | Yes |
| **FrontendResult** | FrontendAgent | GreenLightConverter, WordPressPublisher | Carry generated frontend output | Yes | Yes |
| **GreenLightConversionResult** | GreenLightConverter | FrontendAgent, WordPressPublisher | Carry converted blocks and conversion status | Yes | Yes |
| DesignSpec | — | — | Rejected — design intent lives in prompt/context | — | No |
| FrontendCode | — | — | Rejected — redundant with FrontendResult | — | No |
| FrontendValidationResult | — | — | Rejected — validation fields in FrontendResult | — | No |

---

## 15. Recommended Architecture

```
Task
 ↓
Planner
 ↓
FrontendRequest
 ↓
FrontendAgent
 ├── Bencent Constitution
 ├── Taste sections
 ├── Minimalist
 ├── UI Rules
 └── GSAP sub-skills (when animation_required=True)
 ↓
FrontendResult
 ├── html
 ├── css
 ├── javascript
 ├── validation_status
 ├── errors
 └── warnings
 ↓
GreenLightConverter Tool
 ├── convert.js (deterministic)
 └── deconvert.js (deterministic)
 ↓
GreenLightConversionResult
 ├── blocks
 ├── errors
 └── warnings
 ↓
FrontendValidator
 ├── Deterministic checks (HTML, CSS, JS syntax)
 └── LLM checks (brand, anti-slop)
 ↓
FrontendResult (updated with validation)
 ↓
WordPressPublisher
 ↓
WordPress
```

### Component Responsibilities

| Component | Type | Responsibility |
|-----------|------|----------------|
| **FrontendRequest** | Contract | Workflow → FrontendAgent parameter boundary |
| **FrontendAgent** | Agent | AI reasoning: design, code, animation |
| **FrontendResult** | Contract | FrontendAgent → downstream output boundary |
| **GreenLightConverter** | Tool | Deterministic HTML/CSS/JS → Greenshift blocks |
| **GreenLightConversionResult** | Contract | GreenLightConverter → downstream output boundary |
| **FrontendValidator** | Tool/Agent | Deterministic + LLM validation |
| **WordPressPublisher** | Tool | WordPress REST API transport |
| **Bencent** | Skill | Constitution — always loaded |
| **Taste v2** | Skill | Section-level design guidance |
| **Minimalist** | Skill | Full design constraints |
| **UI Rules** | Skill | Full component rules |
| **GSAP** | Skill | Sub-skill animation knowledge, loaded on demand |

---

## 16. Phase 7B Implementation Plan

### Pre-requisites

Phase 7B may begin when:
1. This review is approved
2. Phase 6C architecture is confirmed
3. Phase 6A/6B skill loading is stable

### Phase 7B Scope

**Goal:** Implement FrontendAgent and supporting contracts/tools.

**Files to create:**
- `contracts.py` — add `FrontendRequest`, `FrontendResult`, `GreenLightConversionResult`
- `agents/frontend.py` — `FrontendAgent` class
- `tools/greenlight_converter.py` — `GreenLightConverter` tool
- `tests/test_frontend_contracts.py` — contract tests
- `tests/test_frontend_agent.py` — agent tests
- `tests/test_greenlight_converter.py` — converter tests

**Files to modify:**
- `state.py` — add frontend `TaskStatus` values, add frontend fields to `Task`
- `main.py` — add frontend workflow steps
- `contracts.py` — add new contracts

### Implementation Order

1. **Contracts first** — define `FrontendRequest`, `FrontendResult`, `GreenLightConversionResult`
2. **FrontendAgent** — implement with method-level skill selection
3. **GreenLightConverter** — wrap existing `convert.js`/`deconvert.js`
4. **FrontendValidator** — deterministic + LLM validation
5. **Workflow integration** — add frontend steps to `main.py`

### Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Context bloat | Method-level skill selection, section loading |
| GreenLight JS in LLM context | Loader enforces `.md`-only; Agent never requests JS files |
| GSAP over-loading | Load sub-skills only when animation_required=True |
| Conversion failures | Timeout, error handling, validation feedback loop |
| Workflow complexity | Serial extension, not parallel |

---

## 17. Risks / Technical Debt

### Current Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Frontend context bloat | High | Method-level skill selection, section loading |
| GreenLight JS leakage | Medium | Loader enforces boundary; Agent discipline |
| GSAP over-loading | Medium | Conditional loading on animation_required |
| Conversion failures | Medium | Timeout, error handling, retry |
| Workflow complexity | Medium | Serial extension |

### Technical Debt

| Item | Priority | Notes |
|------|----------|-------|
| No FrontendAgent yet | High | This review defines the contract boundary |
| No GreenLightConverter yet | High | Requires Node.js subprocess boundary |
| Frontend fields not in Task | Medium | Will be added in Phase 7B |
| Taste section IDs verbose | Low | Future: consider aliases |
| Skill metadata hardcoded | Low | Future: auto-generate from frontmatter |

---

## 18. Final Decision

### Contracts Recommended

| Contract | Status |
|----------|--------|
| **FrontendRequest** | **APPROVED** |
| **FrontendResult** | **APPROVED** |
| **GreenLightConversionResult** | **APPROVED** |
| DesignSpec | **REJECTED** |
| FrontendCode | **REJECTED** |
| FrontendValidationResult | **REJECTED** (use FrontendResult fields) |

### Architecture Boundary

```
Planner
    ↓
FrontendRequest
    ↓
FrontendAgent
    ↓
FrontendResult
    ↓
GreenLightConverter
    ↓
GreenLightConversionResult
    ↓
FrontendValidator
    ↓
FrontendResult (updated)
    ↓
WordPressPublisher
```

### Ownership

| Concept | Owner |
|---------|-------|
| FrontendRequest | Workflow produces, FrontendAgent consumes |
| FrontendResult | FrontendAgent produces, downstream tools consume |
| GreenLightConversionResult | GreenLightConverter produces, Publisher consumes |
| Frontend execution state | TaskStatus enum in state.py |
| Frontend output data | Task fields (frontend_html, frontend_css, etc.) |

### Workflow Position

FrontendAgent enters **after content review** and **before publishing**:

```text
Writer → Critic → SEO → Reviewer → FrontendAgent → GreenLightConverter → Publisher
```

---

**READY FOR PHASE 7B**

All contract boundaries are defined, justified, and aligned with existing project patterns. No over-engineering. Minimum viable contract set approved.
