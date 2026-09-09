# AI WordPress Factory — Architecture Review

**Date:** 2026-09-08  
**Reviewer:** Kilo  
**Scope:** Read-only audit — no code modified  
**Status:** COMPLETE

---

## 1. Executive Summary

AI WordPress Factory is a **Python-based, agent-driven WordPress content generation system** with a multi-phase workflow: Plan → Research → Write → Critique → SEO → Review → Route → Image → Publish → Learn. The architecture is **functional but has significant responsibility overlap, minor bugs, and skill/agent boundary confusion**.

**Key Findings:**
- **3 major architectural concerns** (Retry double-counting, Reviewer multi-responsibility, Learner safety bypass)
- **4 minor bugs** (undefined variable, private method access, import placement, unused imports)
- **2 skill conflicts** (Bencent Brand vs Taste, Minimalist vs UI Rules)
- **1 overengineering concern** (Router as Agent class instead of strategy function)
- **1 missing capability** (no real web research / search integration)
- **FrontendAgent is NOT recommended** for current phase

---

## 2. Current Architecture

```
User
 ↓
AIWordPressFactory (main.py)
 ↓
Task (state.py)
 ↓
Workflow Controller (main.py run_workflow)
 ↓
 ├── PlannerAgent
 ├── ResearchAgent
 ├── WriterAgent
 ├── CriticAgent
 ├── SEOAgent
 ├── ReviewerAgent
 ├── Router
 ├── ImageAgent
 ├── WordPressPublisher (tools/wordpress.py)
 └── LearnerAgent
 ↓
Contracts (contracts.py)
 ↓
Skills (prompts/ + skills/)
 ↓
State (state.py)
```

### Workflow (as implemented in main.py)

```
Step 1:  PLANNING
Step 2:  RESEARCHING
Step 3:  WRITING
Step 4:  CRITIQUING → optional REWRITING
Step 5:  OPTIMIZING (SEO)
Step 6:  REVIEWING → retry loop with Router
Step 7:  MANUAL_REVIEW (human checkpoint)
Step 8:  GENERATING_IMAGE
Step 9:  PUBLISHING
Step 10: COMPLETED
Step 11: LEARNING
```

### Actual Code Flow

```python
# main.py run_workflow()
Planner → Research → Writer → Critic → SEO → Reviewer → Router
    → (rewrite/research/seo loop) → Image → Publisher → Learner
```

---

## 3. Capability Inventory

### Agents

| Agent | Type | Responsibility | Input | Output | Consumer | Status |
|-------|------|---------------|-------|--------|----------|--------|
| PlannerAgent | Agent | Content planning | Task | Dict (plan) | Writer, Research | ✅ Active |
| ResearchAgent | Agent | Data gathering | Task | List[Dict] | Writer, SEO | ⚠️ Simulated |
| WriterAgent | Agent | Content generation | Task | str (content) | Critic, SEO, Reviewer | ✅ Active |
| CriticAgent | Agent | Self-critique + AI pattern detection | Task + content | CritiqueResult | Main (rewrite decision) | ✅ Active |
| SEOAgent | Agent | SEO optimization | Task | Tuple[str, Dict] | Reviewer, Publisher | ✅ Active |
| ReviewerAgent | Agent | Quality evaluation + content modification | Task | ReviewResult | Router | ✅ Active |
| Router | Agent | Routing decision | ReviewResult + Task | str (action) | Main (workflow control) | ✅ Active |
| ImageAgent | Agent | Hero image generation + upload | Task | Tuple[Optional[int], Optional[str]] | Publisher | ✅ Active |
| LearnerAgent | Agent | Learning from human edits | Task | Dict (proposals) | Main (stored in state) | ✅ Active |

### Skills

| Skill | Type | Responsibility | Used By | Status |
|-------|------|---------------|---------|--------|
| bencent-brand | Constitution | Brand identity, colors, typography | Future integration | ✅ Migrated |
| taste-skill | Design Skill | Anti-slop frontend, visual direction | Future FrontendAgent | ✅ Migrated (v2) |
| minimalist-skill | Design Skill | Minimalist UI protocol | Future integration | ✅ Migrated |
| ui-rules | Design Skill | UI design rules | Future integration | ✅ Migrated |
| humanizer-tw | Writing Skill | Traditional Chinese natural writing | Future WriterAgent | ✅ Migrated (Primary) |
| greenlight-vibe | Production Tool | HTML → Greenshift block conversion | Future FrontendAgent | ✅ Migrated |
| gsap | Animation Skill | GSAP animation guidance | Future FrontendAgent | ✅ Migrated |

### Tools

| Tool | Type | Responsibility | Consumer | Status |
|------|------|---------------|----------|--------|
| WordPressPublisher | Tool | WordPress REST API publishing | Main | ✅ Active |

### Contracts

| Contract | Type | Purpose | Consumer | Status |
|----------|------|---------|----------|--------|
| CritiqueResult | Dataclass | Structured critique output | Main, Critic | ✅ Active |
| ReviewResult | Dataclass | Structured review output | Main, Reviewer, Router | ✅ Active |
| LearningProposal | Dataclass | Learning proposal structure | Main, Learner | ✅ Active |
| AIPattern | Dataclass | AI pattern detection result | Critic | ✅ Active |
| CritiqueAction | Enum | Critique action types | Critic | ✅ Active |
| ReviewAction | Enum | Review action types | Reviewer, Router | ✅ Active |
| ProposalStatus | Enum | Proposal status | Learner | ✅ Active |

### State

| Component | Type | Purpose | Status |
|-----------|------|---------|--------|
| Task | Dataclass | Single task data | ✅ Active |
| TaskStatus | Enum | Task lifecycle states | ✅ Active |
| ContentType | Enum | Content type enum | ✅ Active |
| WorkflowState | Dataclass | Global state manager | ✅ Active |

### Workflow

| Component | Type | Purpose | Status |
|-----------|------|---------|--------|
| AIWordPressFactory | Entry Point | System coordinator | ✅ Active |
| run_workflow | Method | Main execution pipeline | ✅ Active |
| _get_agent | Factory | Agent instantiation | ✅ Active |
| _apply_critique | Method | Critique-based revision | ✅ Active |
| _rewrite_content | Method | Review-based rewrite | ✅ Active |
| _manual_review_checkpoint | Method | Human review gate | ✅ Active |

---

## 4. Responsibility Matrix

| Component | Responsible for | NOT responsible for | Potential overlap |
| --------- | --------------- | ------------------- | ----------------- |
| **PlannerAgent** | Content planning, structure outline | Writing content, SEO, quality review | — |
| **ResearchAgent** | Data gathering, search simulation | Content generation, SEO optimization | — |
| **WriterAgent** | Content generation, style compliance | SEO metadata, quality scoring, publishing | SEOAgent (SEO elements), Critic (self-critique) |
| **CriticAgent** | Self-critique, AI pattern detection | Content generation, quality scoring, routing | ReviewerAgent (both evaluate quality) |
| **SEOAgent** | SEO optimization, metadata generation | Content generation, quality review | WriterAgent (injects SEO into content) |
| **ReviewerAgent** | Quality evaluation, content modification, final review | Content generation, routing, publishing | CriticAgent (both challenge content quality) |
| **Router** | Routing decision | Content modification, retry counting, agent execution | — |
| **ImageAgent** | Hero image generation + upload | Content writing, SEO, publishing | — |
| **LearnerAgent** | Learning analysis + proposal generation | Content generation, rule enforcement | — |
| **WordPressPublisher** | WordPress REST API publishing | Content generation, image generation | — |
| **Bencent Brand** | Brand identity, colors, typography, spacing | Design taste, animation, content writing | Taste (both judge visuals) |
| **Taste v2** | Anti-slop frontend, layout, motion, density | Brand identity, content writing, SEO | Bencent Brand (both judge visuals) |
| **Minimalist** | Minimalist UI protocol | Brand identity, content writing, animation | UI Rules (both govern UI) |
| **UI Rules** | UI design rules, font standards | Brand identity, content writing, animation | Minimalist (both govern UI) |
| **Humanizer TW** | Traditional Chinese natural writing | Brand identity, design, SEO | style-rules.md (overlapping rules) |
| **GreenLight** | HTML → Greenshift block conversion | Content writing, animation, design | — |
| **GSAP** | Animation guidance, code skeletons | Content writing, design, conversion | — |

---

## 5. Skill Conflict Analysis

### 5.1 Bencent Brand vs Taste

**Potential Conflict:** Both judge visual aesthetics.

| Aspect | Bencent Brand | Taste v2 |
|--------|--------------|----------|
| Typography | Noto Serif TC, specific font rules | Discourages Inter, prefers Geist/Outfit/Satoshi |
| Color |墨炭 #2C2420, 赭石 #8B5E3C, specific palette | Max 1 accent, saturation <80%, bans AI purple |
| Layout | Specific spacing/layout rules | Anti-center bias, DESIGN_VARIANCE dials |
| Motion | Not specified | Detailed motion direction (MOTION_INTENSITY) |

**Conflict Assessment:** LOW-MEDIUM. Bencent Brand is higher priority. Taste v2 provides complementary frontend guidance but should defer to Bencent Brand on identity-level decisions.

### 5.2 Bencent Brand vs Minimalist

**Potential Conflict:** Bencent Brand defines specific visual identity; Minimalist defines UI restraint.

| Aspect | Bencent Brand | Minimalist |
|--------|--------------|------------|
| Visual identity | Specific colors, fonts, spacing | Restraint, whitespace, minimal chrome |
| Density | Not specified | Very low density preferred |

**Conflict Assessment:** LOW. Bencent Brand already embodies minimalist principles. Minimalist skill provides reinforcement, not contradiction.

### 5.3 Bencent Brand vs UI Rules

**Potential Conflict:** Both govern UI.

| Aspect | Bencent Brand | UI Rules |
|--------|--------------|----------|
| Typography | Specific font choices | Font hierarchy rules |
| Spacing | Specific values | General spacing principles |
| Colors | Specific palette | Color usage guidelines |

**Conflict Assessment:** LOW. UI Rules appears to be a subset/generalization of Bencent Brand. Bencent Brand wins on specificity.

### 5.4 Taste vs Minimalist

**Potential Conflict:** Taste v2 has specific layout/variance directives; Minimalist prescribes restraint.

| Aspect | Taste v2 | Minimalist |
|--------|----------|------------|
| Layout | DESIGN_VARIANCE 8/10 by default | Prefers centered, clean layouts |
| Density | VISUAL_DENSITY 4/10 default | Very airy, low density |
| Motion | MOTION_INTENSITY 6/10 default | Restrained motion |

**Conflict Assessment:** MEDIUM. Taste v2 defaults to higher variance/motion than Minimalist would prefer. For Bencent projects, Minimalist should act as a constraint on Taste's defaults.

### 5.5 Taste vs UI Rules

**Potential Conflict:** Both govern UI design.

| Aspect | Taste v2 | UI Rules |
|--------|----------|----------|
| Scope | Full frontend (layout, motion, density) | UI-specific rules only |
| Detail level | Very detailed (dial-based) | Rule-based checklist |

**Conflict Assessment:** LOW. UI Rules is narrower in scope. Taste v2 subsumes UI Rules but UI Rules can serve as a quick checklist.

### 5.6 Minimalist vs UI Rules

**Potential Conflict:** Both prescribe UI behavior.

| Aspect | Minimalist | UI Rules |
|--------|-----------|----------|
| Scope | Full minimalist philosophy | Specific UI rule checklist |
| Overlap | High | High |

**Conflict Assessment:** HIGH overlap. These two skills are largely redundant. Minimalist is broader; UI Rules is a narrow subset.

---

## 6. Priority Hierarchy

**Recommended precedence (highest to lowest):**

```
1. Bencent Brand (Constitution / Identity)
2. Taste v2 (Anti-slop frontend judgment)
3. Minimalist (UI restraint — overrides Taste defaults when conflicting)
4. UI Rules (Quick checklist — redundant with Minimalist, keep as reference)
5. Humanizer TW (Content naturalization)
6. GreenLight (Production tooling — not a "style" skill)
7. GSAP (Animation — not a "style" skill)
```

**Rationale:**
- Bencent Brand is identity-level and cannot be overridden by taste preferences.
- Taste v2 provides the most comprehensive anti-slop framework.
- Minimalist constrains Taste's defaults toward restraint (important for Bencent's brand).
- UI Rules is redundant with Minimalist but kept as a quick reference checklist.
- Humanizer operates at content level, not visual level — no conflict with visual skills.
- GreenLight and GSAP are production tools, not style arbiters.

---

## 7. Agent / Skill / Tool Boundary

### Current State

| Category | Items | Count | Correct? |
|----------|-------|-------|----------|
| **Agent** | Planner, Research, Writer, SEO, Reviewer, Critic, Router, Image, Learner | 9 | ⚠️ Partially |
| **Skill** | bencent-brand, taste-skill, minimalist-skill, ui-rules, humanizer-tw, greenlight-vibe, gsap | 7 | ✅ Correct |
| **Tool** | WordPressPublisher | 1 | ✅ Correct |
| **Contract** | CritiqueResult, ReviewResult, LearningProposal, AIPattern, Enums | 5 | ✅ Correct |
| **State** | Task, WorkflowState, TaskStatus, ContentType | 4 | ✅ Correct |

### Boundary Issues

**Issue 1: Router is an Agent but should be a Strategy/Function**
- Router has no AI component. It makes deterministic decisions based on ReviewResult.
- It should be a simple function or strategy pattern, not an Agent class.
- **Impact:** Low — it works, but the abstraction is misleading.

**Issue 2: CriticAgent and ReviewerAgent overlap**
- Both evaluate content quality.
- Critic does self-critique + AI pattern detection.
- Reviewer does quality scoring + content modification + final review.
- **Impact:** Medium — creates confusion about which agent "owns" quality evaluation.

**Issue 3: WordPressPublisher is a Tool but lives in tools/ and is instantiated as an Agent**
- In `_get_agent()`, `WordPressPublisher` is mapped as "publisher" alongside Agent classes.
- It's functionally a Tool, not an Agent.
- **Impact:** Low — works correctly, but semantically confusing.

**Issue 4: Skills are not yet integrated into Agents**
- Skills exist as markdown files but are not loaded by any Agent at runtime.
- WriterAgent reads `style-rules.md` and `tone-sample.md` directly, not the migrated Skills.
- **Impact:** High — the Skills migration is incomplete without Agent integration.

**Issue 5: LearnerAgent has two modes of operation**
- `analyze_review()` returns Dict with proposals.
- `apply_proposals()` directly modifies `style-rules.md`.
- The architecture doc says proposals should be "pending → approved → skill", but `apply_proposals()` bypasses this.
- **Impact:** Medium — safety mechanism is documented but not enforced in code.

---

## 8. Workflow Analysis

### Documented vs Actual Workflow

**Documented (README.md):**
```
Task → Planner → Research → Writer → Critic → SEO → Reviewer → Router → Image → Publish
```

**Actual (main.py):**
```
Task → Planner → Research → Writer → Critic → (optional rewrite) → SEO → Reviewer → Router → (retry loop) → Image → Publisher → Learner
```

**Differences:**
1. Documented workflow omits the retry loop.
2. Documented workflow omits the manual review checkpoint.
3. Documented workflow omits the learning phase.
4. Documented workflow doesn't show the conditional rewrite after Critic.

### Retry Loop Analysis

**Location:** `main.py` lines 157-208

**Issues:**

1. **Double retry_count increment:** `task.retry_count += 1` at line 172 (before Router) AND again inside each router action branch (lines 189, 197, 205). This causes double-counting.

2. **Router doesn't control retry counter:** The spec says "Router is NOT responsible for retry counter" but the workflow increments it both before and after Router decisions.

3. **Router is called inside the retry loop** but the loop condition is `task.retry_count < task.max_retries`. With double-counting, actual retries are fewer than expected.

### Reviewer Responsibility

**Issue:** ReviewerAgent does too much:
1. Quality checking (`_check_quality`)
2. Content fixing (`_fix_issues`)
3. Final review (`_final_review`)
4. Content scoring (`_score_content`)
5. Feedback generation (`_generate_feedback`)

This violates single-responsibility principle. The "final review" step should arguably be separate from "quality evaluation."

### Publisher Placement

**Correct:** Publisher only runs after Reviewer PASS. ✅

**Issue:** Publisher is retrieved via `_get_agent("publisher")` which maps to `WordPressPublisher`. This works but is semantically odd since WordPressPublisher is a Tool, not an Agent.

---

## 9. FrontendAgent Recommendation

### Is FrontendAgent Necessary?

**Recommendation: NOT YET.**

The current project is a **content generation and publishing system**. FrontendAgent would be needed only when the project needs to **generate frontend code** (HTML/CSS/JS) for WordPress pages.

### When FrontendAgent Would Be Needed

| Trigger | FrontendAgent Responsibility | Required Skills | Required Tools |
|---------|------------------------------|-----------------|----------------|
| Bencent.cc page generation | Read brief → infer design → generate HTML → convert to Greenshift blocks | Taste, Minimalist, UI Rules, GreenLight | GreenLight converter |
| Custom block creation | Design block structure → generate HTML → validate → convert | GreenLight, Taste | GreenLight validator |
| Landing page generation | Full page from brief → design system → HTML → blocks | Taste, Bencent Brand, GreenLight | GreenLight converter |

### Current State

- GreenLight skill is migrated ✅
- GSAP skill is migrated ✅
- Taste v2 is migrated ✅
- But: **no Python wrapper** for GreenLight
- **no FrontendAgent** to orchestrate frontend generation
- **no integration** between skills and agents

### Recommendation

FrontendAgent should be introduced **only when**:
1. GreenLight Python wrapper is built
2. There is a clear brief → HTML → Greenshift pipeline requirement
3. The current content-only workflow is stable

**Do NOT create FrontendAgent in current phase.**

---

## 10. Skill Usage Timing

| Skill | Planning | Generation | Review | Production | Publish | Notes |
| ----- | -------: | ---------: | -----: | ---------: | ------: | ----- |
| Bencent Brand | ✅ | ✅ | ✅ | ✅ | — | Highest priority, applies everywhere |
| Taste v2 | — | — | — | ✅ | — | Frontend production only |
| Minimalist | — | — | — | ✅ | — | Constrains Taste defaults |
| UI Rules | — | — | — | ✅ | — | Quick UI checklist |
| Humanizer TW | — | ✅ | ✅ | — | — | Content naturalization |
| GreenLight | — | — | — | ✅ | — | HTML → Greenshift conversion |
| GSAP | — | — | — | ✅ | — | Animation code skeletons |

**Notes:**
- Skills currently have **no runtime integration** with Agents. All are static reference files.
- When integrated, Bencent Brand should be loaded first (constitution).
- Humanizer TW should be loaded during Writer and Reviewer phases.
- GreenLight and GSAP should only be loaded when a FrontendAgent exists.

---

## 11. Missing Capabilities

### MUST HAVE

| Capability | Reason | Current Gap |
|------------|--------|-------------|
| **Real web research** | ResearchAgent returns simulated data. `_search_web()` generates fake results. | No actual search API integration. `search_api_key` config exists but is unused in practice. |
| **Skill-Agent integration** | Skills are migrated but not loaded by any Agent at runtime. | WriterAgent reads `prompts/style-rules.md` directly, not the migrated `skills/humanizer-tw/SKILL.md`. |
| **Humanizer integration** | humanizer-tw is the primary humanizer but is not referenced by any Agent. | WriterAgent and ReviewerAgent use inline prompts, not the humanizer skill. |

### SHOULD HAVE

| Capability | Reason | Current Gap |
|------------|--------|-------------|
| **GreenLight Python wrapper** | Critical for Bencent.cc production workflow. | No `tools/greenlight_converter.py` exists yet. |
| **Content validation** | No validation of generated content against actual WordPress capabilities. | No schema validation, no pre-publish check. |
| **Real WordPress media handling** | ImageAgent can upload but has limited error handling. | No retry logic, no format validation, no size checks. |
| **Structured error handling** | Workflow uses generic `except Exception` blocks. | No typed errors, no recovery strategies per error type. |

### FUTURE

| Capability | Reason |
|------------|--------|
| **FrontendAgent** | Requires GreenLight wrapper + stable content workflow |
| **Supervisor** | ROADMAP.md mentions it but no implementation exists |
| **Memory system** | ROADMAP.md Q4 2026 item |
| **Multi-platform publishing** | Currently WordPress-only |
| **Batch processing** | ROADMAP.md Q3-Q4 2026 item |
| **Plugin system** | ROADMAP.md 2027 item |

---

## 12. Overengineering Risks

### Risk 1: Unnecessary Agent Abstraction for Router
**Severity:** Low  
Router is a deterministic decision function. Making it an Agent class adds complexity without benefit. A simple function or strategy pattern would suffice.

### Risk 2: Reviewer Does Too Much
**Severity:** Medium  
ReviewerAgent combines quality checking, content fixing, final review, and scoring. This should be split into:
- QualityEvaluator (scoring + issues)
- ContentFixer (fixing issues)
- FinalReviewer (style/anti-AI check)

### Risk 3: Learner Safety Bypass
**Severity:** Medium  
`LearnerAgent._append_rule()` directly modifies `style-rules.md`. The architecture document says proposals should be "pending → approved → skill" but the code bypasses this. In production, this could corrupt the constitution file.

### Risk 4: Simulated Data in Research
**Severity:** Medium  
`ResearchAgent._search_web()` returns fake search results. `_generate_simulated_research()` generates AI-simulated data. This means the "research" phase is theatrical, not functional. Any content generated with this data is based on hallucinated research.

### Risk 5: Main.py God Method
**Severity:** Medium  
`run_workflow()` is 270+ lines with embedded agent orchestration, retry logic, routing, and error handling. This should be broken into a proper Workflow Engine class.

### Risk 6: Skill Duplication
**Severity:** Low  
Minimalist and UI Rules have significant overlap. Taste v2 subsumes much of both. Having three skills governing similar territory creates confusion about which to consult.

### Risk 7: Unused/Dead Code
**Severity:** Low  
- `WriterAgent.refine_content()` uses undefined variable `content` (bug)
- `agents/__init__.py` has unused imports
- `COURSE_CONTEXT.md` references "Validator" agent which doesn't exist

---

## 13. Recommended Architecture

### Current (Actual)

```
                    AIWordPressFactory
                             │
              ┌──────────────┴──────────────┐
              │                             │
          Workflow Controller           State Manager
              │                             │
     ┌────────┼────────┐                    │
     │        │        │                    │
  Planner  Research  Writer                 │
     │        │        │                    │
     │        │      Critic                 │
     │        │        │                    │
     │      SEO      SEO                   │
     │        │        │                    │
     │     Reviewer   │                    │
     │        │        │                    │
     │     Router      │                    │
     │        │        │                    │
     │   ┌────┴────┐   │                    │
     │   │         │   │                    │
     │ Rewrite Research │                    │
     │   │         │   │                    │
     │   └────┬────┘   │                    │
     │        │        │                    │
     │   Reviewer      │                    │
     │        │        │                    │
     │      Image      │                    │
     │        │        │                    │
     │   Publisher     │                    │
     │        │        │                    │
     │     Learner     │                    │
     │                  │                    │
     └──────────────────┴────────────────────┘
              │
         Contracts
         (CritiqueResult, ReviewResult, LearningProposal)
```

### Recommended (Simplified)

```
                    AIWordPressFactory
                             │
              ┌──────────────┴──────────────┐
              │                             │
          Workflow Engine               State Store
              │                             │
     ┌────────┼────────┐                    │
     │        │        │                    │
  Planner  Research  Writer                 │
     │        │        │                    │
     │        │      Critic                 │
     │        │        │                    │
     │      SEO      SEO                   │
     │        │        │                    │
     │     Reviewer   │                    │
     │        │        │                    │
     │     Router      │                    │
     │        │        │                    │
     │   ┌────┴────┐   │                    │
     │   │         │   │                    │
     │ Rewrite Research │                    │
     │   │         │   │                    │
     │   └────┬────┘   │                    │
     │        │        │                    │
     │   Reviewer      │                    │
     │        │        │                    │
     │      Image      │                    │
     │        │        │                    │
     │   Publisher     │                    │
     │        │        │                    │
     │     Learner     │                    │
     │                  │                    │
     └──────────────────┴────────────────────┘
              │
         Contracts
         Skills (loaded at runtime)
```

**Key changes recommended:**
1. Extract `WorkflowEngine` class from `main.py`
2. Router → function (not Agent)
3. Reviewer → split into QualityEvaluator + ContentFixer + FinalReviewer
4. Skills loaded at runtime by Agents (not hardcoded file paths)
5. Learner → enforce pending/approved cycle (no direct file writes)

---

## 14. Recommended Next Phase

### Phase 6 — Skill-Agent Integration

**Goal:** Connect migrated Skills to Agents at runtime.

**Tasks:**
1. Create `SkillLoader` utility to load Skills by name
2. Integrate `humanizer-tw` into WriterAgent and ReviewerAgent
3. Integrate `bencent-brand` as constitution loaded by all Agents
4. Integrate `style-rules.md` + `anti-ai-writing.md` into CriticAgent
5. Fix identified bugs (retry double-count, undefined variable, private method access)
6. Replace simulated research with real search integration

**Do NOT:**
- Create FrontendAgent
- Modify workflow structure
- Add new Agents
- Start GreenLight wrapper integration

---

## 15. Detailed Findings

### Major Issues (3)

| # | Issue | Severity | Location | Description |
|---|-------|----------|----------|-------------|
| 1 | Retry double-counting | High | main.py:172,189,197,205 | `retry_count` incremented twice per loop iteration |
| 2 | Reviewer multi-responsibility | Medium | agents/reviewer.py:17-240 | Quality check, fix, final review, scoring all in one agent |
| 3 | Learner safety bypass | Medium | agents/learner.py:154-168 | Direct file write bypasses pending/approved cycle |

### Minor Bugs (4)

| # | Issue | Severity | Location | Description |
|---|-------|----------|----------|-------------|
| 1 | Undefined variable | Low | agents/writer.py:121 | `refine_content()` references `{content}` but variable is `task.draft_content` |
| 2 | Private method access | Low | main.py:162-165 | `_final_review()` called from outside the class |
| 3 | Import placement | Low | agents/seo.py:143 | `import json` at bottom of file |
| 4 | Unused imports | Low | agents/__init__.py:4-5 | `Dict, Any` imported but unused |

### Responsibility Overlaps (4)

| # | Overlap | Agents/Skills | Recommendation |
|---|---------|---------------|----------------|
| 1 | Quality evaluation | CriticAgent + ReviewerAgent | Clarify: Critic = self-doubt, Reviewer = quality gate |
| 2 | Visual judgment | Bencent Brand + Taste | Bencent Brand wins on identity; Taste on frontend execution |
| 3 | UI governance | Minimalist + UI Rules | Merge or designate one as primary |
| 4 | Content naturalization | humanizer-tw + style-rules.md + anti-ai-writing.md | Consolidate into single constitution |

### Missing Capabilities (3 MUST HAVE)

| # | Capability | Why Critical |
|---|-----------|-------------|
| 1 | Real web research | Current research is simulated/fake |
| 2 | Skill-Agent runtime integration | Skills exist but are never loaded by Agents |
| 3 | Humanizer integration | Primary humanizer skill is unused by any Agent |

---

## 16. Git Status

```
M  __pycache__/state.cpython-314.pyc
M  agents/__pycache__/seo.cpython-314.pyc
M  agents/seo.py
M  prompts/planner.md
M  state.py
A  workflow_state.json
?? docs/humanizer-comparison.md
?? docs/taste-skill-source.md
?? prompts/router.md
?? skills/
    ├── bencent-brand/
    ├── greenlight-vibe/
    ├── gsap/
    ├── humanizer-tw/
    ├── minimalist-skill/
    ├── taste-skill/
    └── ui-rules/
```

No existing Python files were modified in this review. All changes are from previous migration phases.

---

**ARCHITECTURE REVIEW COMPLETE**
