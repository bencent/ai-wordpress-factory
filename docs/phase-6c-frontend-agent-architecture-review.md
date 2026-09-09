# Phase 6C — FrontendAgent Architecture Review

**Date:** 2026-09-08  
**Reviewer:** Kilo  
**Scope:** Read-only architecture review — no code modified  
**Status:** COMPLETE

---

## 1. Executive Summary

This project is **AI WordPress Factory**: an AI-powered content generation system that produces WordPress posts with images, SEO metadata, and human-in-the-loop review. It is **not** a full frontend IDE. Frontend production in this context means: generating HTML/CSS/JS for WordPress pages/blocks that respect Bencent Brand, converting that code to Greenshift/GreenLight blocks, and publishing through WordPress.

**Primary Recommendation:** Option C — `FrontendAgent` + Specialized Tools, with a strong tool boundary for GreenLight conversion.

**Key principle:** AI reasoning should stop where deterministic tooling begins. GreenLight conversion is deterministic code execution, not LLM reasoning. GSAP is knowledge, not execution. WordPress transport is a tool, not an Agent.

**FrontendAgent readiness:** NOT READY yet — requires Phase 7A contracts and Phase 7B Agent implementation.

---

## 2. Current Architecture Findings

### Project Identity

- **Name:** AI WordPress Factory
- **Primary output:** WordPress posts/pages with content + hero images
- **CMS:** WordPress + Greenshift/GreenLight plugin
- **Content language:** Traditional Chinese
- **Brand:** Bencent (AI WordPress studio)

### Current Agent Architecture

| Agent | Responsibility | Skill Loading |
|-------|---------------|---------------|
| Planner | Content planning | Constitution only |
| Research | Data gathering | Constitution only |
| Writer | Article writing | Constitution + Humanizer TW |
| Critic | Self-critique, AI pattern detection | Constitution + Humanizer TW |
| SEO | SEO optimization | Constitution only |
| QualityEvaluator | Quality scoring | Constitution only |
| ContentFixer | Content repair | Constitution only |
| FinalReviewer | Style/AI-pattern review | Constitution + Humanizer TW |
| Router | Workflow routing | Constitution only |
| ImageAgent | Hero image generation | Constitution + Taste sections + Minimalist |
| Learner | Review analysis | Constitution only |

### Current Tool Architecture

| Tool | Responsibility |
|------|---------------|
| WordPressPublisher | WordPress REST API transport |
| OpenAIImageProvider | DALL-E 3 image generation |

### Current Skill Architecture

| Skill | Category | Size | Loading |
|-------|----------|------|---------|
| Bencent | Constitution | Small | Always first |
| Taste v2 | Design | Very Large | Section-level |
| Minimalist | Design | Medium | Full when requested |
| UI Rules | Design | Small | Full when requested |
| Humanizer TW | Writing | Medium | Full when requested |
| GreenLight | Production | Very Large | Instruction-level |
| GSAP | Production | Very Large | Sub-skill level |

### Key Finding

The project already has a clean Agent-per-responsibility pattern with explicit skill selection. There is **no frontend production capability** yet. Adding it requires minimal disruption to existing architecture.

---

## 3. Frontend Responsibility Definition

Frontend production in AI WordPress Factory means producing **WordPress-ready HTML/CSS/JS** that can be published through WordPress/Greenshift. It does NOT mean building a standalone web app.

### Responsibility Breakdown

| # | Responsibility | Owner | Rationale |
|---|----------------|-------|-----------|
| 1 | Design interpretation | FrontendAgent | Requires brand-aware AI reasoning |
| 2 | Design system application | FrontendAgent + Taste/Minimalist skills | AI reasoning with skill guidance |
| 3 | Layout planning | FrontendAgent | AI reasoning |
| 4 | Component planning | FrontendAgent | AI reasoning |
| 5 | HTML generation | FrontendAgent | AI code generation |
| 6 | CSS generation | FrontendAgent | AI code generation |
| 7 | JavaScript generation | FrontendAgent | AI code generation, conditional on GSAP |
| 8 | Animation generation | FrontendAgent + GSAP skill | AI reasoning with GSAP knowledge |
| 9 | Accessibility | FrontendAgent + deterministic validation | AI + code rules |
| 10 | Responsive behavior | FrontendAgent + deterministic validation | AI + code rules |
| 11 | GreenLight conversion | GreenLightConverter Tool | Deterministic execution |
| 12 | Greenshift validation | GreenLightConverter Tool + LLM validation | Tool + AI |
| 13 | WordPress publishing | WordPressPublisher | Existing tool |
| 14 | Human review | Human | Existing workflow |

### Critical Distinction

**AI-generated content** vs **deterministic execution**:

| Type | Examples | Owner |
|------|----------|-------|
| AI reasoning | Design decisions, layout planning, code generation | FrontendAgent |
| Deterministic execution | HTML-to-block conversion, validation, WordPress upload | Tools |
| Knowledge | Taste sections, UI Rules, GSAP sub-skills, GreenLight instructions | Skills loaded by Agent |

---

## 4. Agent / Skill / Tool Boundary

### Design Reasoning

**Should be:** Agent

**Reason:** Design interpretation requires brand-aware reasoning, trade-off evaluation, and creative decisions. This is AI work, not deterministic code.

**Skills required:**
- Bencent (constitution)
- Taste v2 (selected sections: layout, color, typography, anti-slop)
- Minimalist (density, whitespace)
- UI Rules (component rules)

### HTML/CSS/JS Generation

**Should be:** Agent

**Reason:** Generating production-quality HTML/CSS/JS from design intent is code generation, which benefits from LLM capabilities. However, the Agent should produce **clean vanilla HTML/CSS/JS**, not framework-specific code.

**Skills required:**
- Bencent (constitution)
- Taste v2 (selected sections: implementation rules)
- Minimalist (style constraints)
- UI Rules (component rules)

### Animation Generation

**Should be:** Agent with GSAP skill

**Reason:** Animation code is JavaScript that requires domain knowledge (GSAP API, timelines, ScrollTrigger). The Agent writes the code; it does not execute it.

**Skills required:**
- Bencent (constitution)
- GSAP sub-skill (e.g., `gsap-scrolltrigger`)
- Taste v2 (motion section if relevant)

### GreenLight Conversion

**Should be:** Tool (NOT Agent)

**Reason:** GreenLight contains `scripts/convert.js` and `scripts/deconvert.js` — deterministic conversion programs. This is execution, not reasoning. The boundary is:

```text
FrontendAgent → HTML/CSS/JS → GreenLightConverter → Greenshift Blocks
```

**Skills required:** None (tool executes deterministic code)

### WordPress Publishing

**Should be:** Tool (existing)

**Reason:** `WordPressPublisher` already exists and handles all CMS transport. FrontendAgent should never directly call WordPress APIs.

---

## 5. Frontend Skill Matrix

### Design Phase

| Skill | Loading Strategy | Sections |
|-------|-----------------|----------|
| Bencent | Always | Full (constitution) |
| Taste v2 | Section-level | layout, color, typography, anti-slop, visual-asset-strategy |
| Minimalist | Full | Full (8 KB is acceptable) |
| UI Rules | Full | Full (small) |
| Humanizer TW | No | Not relevant to design |
| GreenLight | No | Not relevant to design |
| GSAP | No | Not relevant unless animation requested |

### Code Generation Phase

| Skill | Loading Strategy | Sections |
|-------|-----------------|----------|
| Bencent | Always | Full |
| Taste v2 | Section-level | implementation-rules, anti-slop |
| Minimalist | Full | Full |
| UI Rules | Full | Full |
| Humanizer TW | No | Not relevant |
| GreenLight | No | Not relevant to generation |
| GSAP | Sub-skill | Only if animation requested |

### Conversion Phase

| Skill | Loading Strategy | Sections |
|-------|-----------------|----------|
| Bencent | Always | Full |
| Taste v2 | No | Not relevant to deterministic conversion |
| Minimalist | No | Not relevant |
| UI Rules | No | Not relevant |
| Humanizer TW | No | Not relevant |
| GreenLight | Instruction-level | core-structure, attributes, variables |
| GSAP | No | Not relevant |

### Validation Phase

| Skill | Loading Strategy | Sections |
|-------|-----------------|----------|
| Bencent | Always | Full |
| Taste v2 | Section-level | pre-flight, anti-slop |
| Minimalist | Full | Full |
| UI Rules | Full | Full |
| Humanizer TW | No | Not relevant |
| GreenLight | Instruction-level | validate-styles, validate-scripts |
| GSAP | No | Not relevant |

---

## 6. Contract Analysis

### Minimum Useful Contract Set

| Contract | Purpose | Input | Output | Owner |
|----------|---------|-------|--------|-------|
| **FrontendRequest** | Request frontend work | task_id, content_type, design_brief, brand_constraints | — | Planner/Workflow |
| **FrontendResult** | Return generated frontend | — | html, css, js, blocks, validation_status, errors | FrontendAgent |
| **GreenLightConversionResult** | Return conversion result | html, css, js | blocks, warnings, errors | GreenLightConverter |

### Contracts NOT Needed Now

| Contract | Reason |
|----------|--------|
| DesignSpec | Overkill — design intent is expressed in the prompt/context, not a separate structured contract |
| FrontendCode | Redundant with FrontendResult |
| FrontendValidationResult | Can be a field in FrontendResult |
| GreenLightConversionResult | Useful but can start as a simple dataclass |

### Recommended Minimum

```python
@dataclass
class FrontendRequest:
    task_id: str
    content_type: ContentType
    design_brief: str
    brand_constraints: Dict[str, Any]
    animation_required: bool = False
    use_case: str = "page"  # page, section, component

@dataclass
class FrontendResult:
    task_id: str
    success: bool
    html: Optional[str] = None
    css: Optional[str] = None
    javascript: Optional[str] = None
    blocks: Optional[str] = None
    validation_status: str = "pending"
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
```

---

## 7. Workflow Integration Analysis

### Current Workflow

```text
Task -> Planner -> Research -> Writer -> Critic -> SEO -> Reviewer -> Router
    -> ImageAgent -> Publisher -> COMPLETED -> Human Review -> Learner
```

### Proposed Frontend Integration

**Option A: Serial extension**

```text
Task -> Planner -> Research -> Writer -> Critic -> SEO -> Reviewer -> Router
    -> FrontendAgent -> GreenLightConverter -> Publisher -> COMPLETED
    -> Human Review -> Learner
```

**Option B: Parallel branch**

```text
Task
    ├── Content workflow
    │   Planner -> Research -> Writer -> Critic -> SEO -> Reviewer
    │
    └── Frontend workflow
        FrontendAgent -> GreenLightConverter -> Publisher
```

### Recommendation: Serial Extension

**Reason:**
- Frontend depends on finalized content (`task.final_content`)
- Frontend is a natural continuation after content review
- Parallel branches would require content synchronization
- The current workflow already serializes content production; adding frontend as another step is consistent
- Simpler to implement and debug

### Proposed Workflow States

Add to `TaskStatus`:
- `FRONTEND_DESIGN` — design reasoning
- `FRONTEND_CODE` — HTML/CSS/JS generation
- `FRONTEND_VALIDATION` — validation
- `FRONTEND_CONVERSION` — GreenLight conversion
- `FRONTEND_PUBLISHING` — WordPress publishing

---

## 8. Validation Architecture

### What Needs Validation

| Check | Type | Owner | Rationale |
|-------|------|-------|-----------|
| HTML validity | Deterministic | Tool/Validator | Standard HTML parsing |
| CSS correctness | Deterministic | Tool/Validator | CSS parsing |
| JS syntax | Deterministic | Tool/Validator | JS parsing |
| Responsive behavior | Deterministic + LLM | Tool + Agent | Media queries + visual reasoning |
| Accessibility | LLM | Agent | Requires brand-aware judgment |
| GreenLight compatibility | Deterministic | Tool | Conversion output validation |
| Greenshift structure | Deterministic | Tool | Block structure validation |
| Brand compliance | LLM | Agent | Requires Bencent knowledge |
| Anti-slop compliance | LLM | Agent | Requires Taste knowledge |

### Validation Boundary

```
FrontendAgent
    ↓
FrontendResult
    ↓
FrontendValidator (LLM + deterministic checks)
    ↓
Validation report
    ↓
Fix or proceed
```

**Do NOT use LLM for:**
- HTML syntax checking
- CSS syntax checking
- JS syntax checking
- Block structure validation

**DO use LLM for:**
- Brand compliance
- Anti-slop detection
- Accessibility judgment
- Design quality assessment

---

## 9. GreenLight Converter Architecture

### Current State

GreenLight is currently a **Skill only**. The actual conversion logic exists in:
- `scripts/convert.js` (40 KB)
- `scripts/deconvert.js` (26 KB)

These are **not currently callable** from Python code.

### Proposed Architecture

```python
class GreenLightConverter:
    def convert(self, html: str, css: str, js: str) -> GreenLightConversionResult:
        """Convert HTML/CSS/JS to Greenshift blocks."""
        # Write to temp file
        # Execute node scripts/convert.js
        # Parse output
        # Return blocks + validation
    
    def deconvert(self, blocks: str) -> str:
        """Convert Greenshift blocks back to HTML."""
        # Execute node scripts/deconvert.js
        # Return HTML
```

### Subprocess Boundary

```text
Python FrontendAgent
    ↓
GreenLightConverter
    ↓
subprocess.run(["node", "scripts/convert.js", input.html, "-o", output.txt])
    ↓
Parse stdout/stderr
    ↓
Return structured result
```

### Key Requirements

| Requirement | Recommendation |
|-------------|----------------|
| Node.js availability | Check at startup; fail fast if missing |
| Temp files | Use `tempfile` module; cleanup after conversion |
| Error handling | Capture stderr, exit codes; return structured errors |
| Validation | Run `validate-scripts.md` and `validate-styles.md` rules after conversion |
| Determinism | Same input → same output; no LLM in conversion path |

### Important

The JS files should **never** be loaded into LLM context. They are executable tooling, not knowledge. The loader already enforces this boundary.

---

## 10. GSAP Architecture

### Current State

GSAP is a **Skill namespace** with 8 sub-skills, all under `skills/gsap/`:

| Sub-skill | Size | Relevance |
|-----------|------|-----------|
| gsap-core | Medium | Always for animation |
| gsap-timeline | Small | Sequencing |
| gsap-scrolltrigger | Medium | Scroll-based animation |
| gsap-plugins | Large | Advanced effects |
| gsap-utils | Medium | Utility functions |
| gsap-react | Small | React integration |
| gsap-performance | Small | Optimization |
| gsap-frameworks | Medium | Vue/Svelte/etc. |

### Project-Specific Relevance

This project uses **vanilla HTML/CSS/JS** (per GreenLight constraints). Therefore:

| Sub-skill | Needed? | Reason |
|-----------|---------|--------|
| gsap-core | **YES** | Core animation API |
| gsap-timeline | **YES** | Sequencing animations |
| gsap-scrolltrigger | **YES** | Scroll-linked animations |
| gsap-plugins | Conditional | Only if advanced effects needed |
| gsap-utils | **YES** | Utility functions |
| gsap-react | NO | Project uses vanilla JS |
| gsap-performance | Conditional | Optimization |
| gsap-frameworks | NO | Project uses vanilla JS |

### Loading Strategy

GSAP sub-skills should be loaded **only when animation is required**:

```python
required_skills=[
    "Bencent",
    "gsap-core",
    "gsap-scrolltrigger",  # if scroll animation needed
]
```

**Do NOT load GSAP by default for non-animation frontend tasks.**

---

## 11. WordPress Boundary

### Current Boundary

```
Agent/Tool
    ↓
WordPressPublisher
    ↓
WordPress REST API
```

### Proposed Boundary

```
FrontendAgent
    ↓
FrontendResult (HTML/CSS/JS/Blocks)
    ↓
GreenLightConverter (if blocks needed)
    ↓
FrontendResult.blocks
    ↓
WordPressPublisher
    ↓
WordPress
```

### Key Principle

**FrontendAgent should NEVER directly call WordPress APIs.**

All WordPress interaction goes through:
1. `WordPressPublisher` for content/media
2. `GreenLightConverter` for block conversion (which produces block content for WordPressPublisher)

This preserves:
- Separation of concerns
- Testability
- CMS independence
- Security boundary

---

## 12. Architecture Comparison

### Decision Matrix

| Criterion | A: Single Agent | B: Designer + Producer | C: Agent + Tools | D: Other |
|-----------|----------------:|-----------------------:|-----------------:|---------:|
| Simplicity | Good | Poor | Excellent | — |
| Separation of concerns | Acceptable | Excellent | Excellent | — |
| Context efficiency | Acceptable | Good | Excellent | — |
| Skill management | Acceptable | Poor | Excellent | — |
| Testability | Acceptable | Poor | Excellent | — |
| GreenLight integration | Acceptable | Acceptable | Excellent | — |
| GSAP integration | Acceptable | Acceptable | Excellent | — |
| WordPress integration | Good | Acceptable | Excellent | — |
| Future scalability | Acceptable | Poor | Excellent | — |
| Portfolio value | Good | Poor | Excellent | — |
| Current project fit | Good | Poor | Excellent | — |

### Ratings Explanation

**Option A — Single FrontendAgent:**
- Acceptable simplicity but Agent becomes too broad
- Hard to test individual responsibilities
- GreenLight/GSAP/WordPress boundaries blur

**Option B — Designer + Producer:**
- Conceptually clean but over-engineered for this project
- Two Agents for one user-facing capability
- Hard to maintain skill boundaries between them
- Unnecessary complexity

**Option C — Agent + Tools (RECOMMENDED):**
- One Agent for AI reasoning
- Tools for deterministic execution
- Clean boundaries, testable, maintainable
- Matches existing project pattern (ImageAgent + OpenAIImageProvider + WordPressPublisher)

---

## 13. Recommended Architecture

### Target Architecture

```
Task
 ↓
Planner
 ↓
FrontendAgent
    ├── Design reasoning (Bencent + Taste sections + Minimalist + UI Rules)
    ├── Code generation (Bencent + Taste sections + Minimalist + UI Rules)
    └── Animation generation (Bencent + GSAP sub-skill)
 ↓
FrontendResult
    ├── html
    ├── css
    ├── javascript
    ├── blocks (optional)
    └── validation_status
 ↓
GreenLightConverter (Tool)
    ├── convert.js → Greenshift blocks
    └── validate-styles/scripts
 ↓
WordPressPublisher (Tool)
 ↓
WordPress
```

### Component Responsibilities

| Component | Type | Responsibility |
|-----------|------|----------------|
| **FrontendAgent** | Agent | AI reasoning: design, code, animation |
| **FrontendResult** | Contract | Structured output from FrontendAgent |
| **GreenLightConverter** | Tool | Deterministic HTML→Greenshift conversion |
| **WordPressPublisher** | Tool | WordPress REST API transport |
| **Bencent** | Skill | Constitution — always loaded |
| **Taste v2** | Skill | Section-level design guidance |
| **Minimalist** | Skill | Full design constraints |
| **UI Rules** | Skill | Full component rules |
| **GreenLight** | Skill/Tool | Instructions for Agent, scripts for Tool |
| **GSAP** | Skill | Sub-skill animation knowledge |

### What FrontendAgent Does NOT Do

- Direct WordPress API calls
- Execute conversion scripts
- Load full Taste v2 by default
- Load GreenLight JS into prompt context
- Load GSAP by default when no animation needed

---

## 14. Implementation Roadmap

### Phase 6C — Architecture Review (This Document)

**Goal:** Define frontend architecture without implementation  
**Deliverable:** `docs/phase-6c-frontend-agent-architecture-review.md`  
**Status:** COMPLETE

### Phase 7A — Contracts

**Goal:** Define minimum contracts for frontend production  
**Files:** `contracts.py`  
**Deliverables:**
- `FrontendRequest`
- `FrontendResult`
- `GreenLightConversionResult`

**Dependencies:** Phase 6C approval  
**Risk:** Low — contracts are additive

### Phase 7B — FrontendAgent

**Goal:** Implement FrontendAgent with method-level skill selection  
**Files:** `agents/frontend.py`  
**Deliverables:**
- `FrontendAgent` class
- `_design()` method
- `_generate_code()` method
- `_generate_animation()` method
- Method-level `required_skills` integration

**Dependencies:** Phase 7A  
**Risk:** Medium — requires careful skill selection per method

### Phase 7C — Frontend Validation

**Goal:** Add deterministic + LLM validation layer  
**Files:** `agents/frontend_validator.py` or tool  
**Deliverables:**
- HTML/CSS/JS syntax validation
- GreenLight compatibility check
- Brand compliance check (LLM)
- Anti-slop check (LLM)

**Dependencies:** Phase 7B  
**Risk:** Medium — validation coverage must be pragmatic

### Phase 8 — GreenLight Converter Tool

**Goal:** Wrap `convert.js`/`deconvert.js` in Python tool  
**Files:** `tools/greenlight_converter.py`  
**Deliverables:**
- `GreenLightConverter` class
- Subprocess execution boundary
- Temp file management
- Error handling
- Node.js availability check

**Dependencies:** Phase 7B  
**Risk:** Medium — subprocess execution and error handling

### Phase 9 — Workflow Integration

**Goal:** Integrate frontend into existing workflow  
**Files:** `main.py`, `state.py`  
**Deliverables:**
- New `TaskStatus` values
- Frontend workflow steps
- Router integration

**Dependencies:** Phases 7A-7C  
**Risk:** Low — additive workflow extension

### Phase 9.5 — Real bencent.cc Page Test

**Goal:** Test end-to-end with real WordPress site  
**Deliverables:**
- Published test page
- Validation report

**Dependencies:** All previous phases  
**Risk:** High — depends on external systems

---

## 15. Risks / Technical Debt

### Current Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| FrontendAgent context bloat | High | Method-level skill selection, section loading |
| GreenLight JS in LLM context | Medium | Loader already prevents this; enforce in Agent |
| GSAP over-loading | Medium | Load sub-skills only when animation needed |
| Taste v2 verbosity | Medium | Use section IDs, not full skill |
| Workflow complexity | Medium | Serial extension, not parallel branches |

### Technical Debt

| Item | Priority | Notes |
|------|----------|-------|
| No FrontendAgent yet | High | This review defines the architecture |
| No GreenLightConverter yet | High | Requires Node.js subprocess boundary |
| No frontend contracts yet | Medium | Phase 7A |
| Taste section IDs are verbose | Low | Future: consider aliases |
| Skill metadata hardcoded | Low | Future: auto-generate from frontmatter |

---

## 16. Open Questions

### 1. Should FrontendAgent produce full pages or sections?

**Question:** Should FrontendAgent generate complete WordPress pages, or individual sections/components?

**Recommendation:** Start with sections/components. Full pages can be composed from sections. This is more modular and testable.

### 2. Should animation be always-on or conditional?

**Question:** Should FrontendAgent always consider animation, or only when explicitly requested?

**Recommendation:** Conditional. Load GSAP skills only when `animation_required=True` in `FrontendRequest`.

### 3. Should FrontendAgent handle responsive design explicitly?

**Question:** Should responsive behavior be an explicit output concern, or implicit in generated CSS?

**Recommendation:** Implicit in generated CSS, with explicit validation. FrontendAgent should generate responsive CSS; validation should check for mobile breakpoints.

### 4. Should GreenLight conversion be synchronous or async?

**Question:** Should `GreenLightConverter.convert()` block until completion, or return a job ID?

**Recommendation:** Synchronous for MVP. Conversion is fast (seconds), not minutes. Async can be added later if needed.

### 5. Should FrontendAgent support component reuse?

**Question:** Should FrontendAgent maintain a component library or generate each component fresh?

**Recommendation:** Fresh generation for MVP. Component library is a future optimization.

### 6. How should frontend errors be handled in the workflow?

**Question:** Should frontend validation failures route back to FrontendAgent for fixing, or fail the task?

**Recommendation:** Route back to FrontendAgent with validation feedback. Use existing retry mechanism.

---

## 17. Validation Results

### Syntax Check
- All existing Python files compile cleanly

### Import Check
- All existing modules import successfully

### Test Check
- All existing tests pass
- No production code was modified

### Git Status
- Only `docs/phase-6c-frontend-agent-architecture-review.md` was created
- No Agents, Skills, Tools, or workflow code was modified

---

## 18. Git Status

### Files Created
- `docs/phase-6c-frontend-agent-architecture-review.md`

### Files Modified
- None

### Files Deleted
- None

---

**PHASE 6C — FRONTENDAGENT ARCHITECTURE REVIEW COMPLETE**

**STATUS:** PASS

**RECOMMENDED ARCHITECTURE:** Option C — FrontendAgent + Specialized Tools

**FRONTENDAGENT READY:** NO — requires Phase 7A contracts and Phase 7B implementation

**NEXT PHASE:** Phase 7A — Contracts
