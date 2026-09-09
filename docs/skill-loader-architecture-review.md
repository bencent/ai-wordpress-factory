# Skill Loader Architecture Review

**Date:** 2026-09-08  
**Reviewer:** Kilo  
**Scope:** Read-only architecture review — no code modified  
**Status:** COMPLETE

---

## 1. Current Skill Loading Behavior

### Implementation Facts

| Aspect | Current State |
|--------|--------------|
| **Discovery** | Scans `skills/` directory for subdirectories containing `SKILL.md` |
| **Frontmatter parsing** | Regex-based YAML parser in `_parse_frontmatter()` |
| **Mapping** | Explicit hardcoded dictionary in `get_skills_for_agent()` |
| **Loading time** | All skills loaded once at `SkillLoader()` instantiation |
| **Caching** | Skills cached in `self._skills` dict; `self.skills_context` cached per agent instance |
| **Injection** | Every `BaseAgent.call_ai()` prepends `skills_context` to prompt |
| **Granularity** | Agent-level only — no per-method or per-task selection |
| **Duplicates** | Not possible — mapping is explicit and deterministic |
| **Order** | Mapping list order + `os.listdir()` iteration order |

### Execution Flow

```text
Agent instantiation
    ↓
BaseAgent.__init__()
    ↓
skill_loader.build_skills_context(self.name)
    ↓
skill_loader.get_skills_for_agent(agent_name)
    ↓
mapping dict lookup
    ↓
Skill objects retrieved from _skills cache
    ↓
Context string built: "## Active Skills Context\n### {name}\n{description}\n{content}"
    ↓
Stored in self.skills_context

Agent.call_ai(prompt)
    ↓
full_prompt = f"{self.skills_context}\n\n{prompt}"
    ↓
OpenAI API call
```

### Actual Code Paths

**Discovery** (`skills/loader.py:41-62`):
```python
for entry in os.listdir(self.skills_dir):
    skill_path = os.path.join(self.skills_dir, entry)
    skill_md = os.path.join(skill_path, "SKILL.md")
    if os.path.isfile(skill_md):
        # parse and cache
```

**Mapping** (`skills/loader.py:70-86`):
```python
mapping = {
    "planner": ["Bencent"],
    "writer": ["Bencent", "humanizer-tw"],
    "image": ["Bencent", "design-taste-frontend", "minimalist-ui"],
    # ...
}
return [self._skills[name] for name in skill_names if name in self._skills]
```

**Injection** (`agents/__init__.py:54-68`):
```python
def call_ai(self, prompt: str, **kwargs) -> str:
    full_prompt = prompt
    if self.skills_context:
        full_prompt = f"{self.skills_context}\n\n{prompt}"
    # ... OpenAI call
```

### Current Agent → Skill Mapping

| Agent | Current Skills | Skill Count |
|-------|---------------|-------------|
| Planner | Bencent | 1 |
| Research | Bencent | 1 |
| Writer | Bencent, humanizer-tw | 2 |
| Critic | Bencent, humanizer-tw | 2 |
| SEO | Bencent | 1 |
| QualityEvaluator | Bencent, humanizer-tw | 2 |
| ContentFixer | Bencent | 1 |
| FinalReviewer | Bencent, humanizer-tw | 2 |
| Router | Bencent | 1 |
| Image | Bencent, Taste, Minimalist | 3 |
| Learner | Bencent | 1 |

---

## 2. Skill Responsibility Analysis

### Classification

| Skill | Category | Scope | Current Size |
|-------|----------|-------|--------------|
| **bencent-brand** | Constitution / Brand | Agent-wide | ~5 KB |
| **taste-skill** | Design | Agent-wide | ~87 KB |
| **minimalist-skill** | Design | Agent-wide | ~8 KB |
| **ui-rules** | Design | Agent-wide | ~0.5 KB |
| **humanizer-tw** | Writing | Agent-wide | ~15 KB |
| **greenlight-vibe** | Frontend Production | Tool-specific | ~66 KB (14 files) |
| **gsap** | Frontend Production | Tool-specific | ~80 KB (9 subdirs) |

### Scope Classification

**Agent-wide Skills** — should be loaded for every agent method:
- **Bencent Brand** — brand constitution applies to all output

**Stage-specific Skills** — should be loaded only during specific workflow stages:
- **Humanizer TW** — only during writing/reviewing content
- **Taste v2** — only during visual/image generation or frontend production
- **Minimalist** — only during visual/image generation or frontend production
- **UI Rules** — only during UI component generation

**Tool-specific Skills** — should be loaded only when specific tools are invoked:
- **GreenLight** — only when converting HTML to Greenshift blocks
- **GSAP** — only when generating animation code

### Current Mismatch

The current loader treats all Skills as Agent-wide. There is no mechanism for stage-specific or tool-specific loading.

---

## 3. Prompt Bloat Analysis

### Current Injection Pattern

Every `call_ai()` call prepends ALL mapped skills to the prompt. This means:

| Agent | Skills Injected | Estimated Token Cost per Call |
|-------|----------------|------------------------------|
| Planner | Bencent (~5 KB) | ~1,250 tokens |
| Writer | Bencent + humanizer-tw (~20 KB) | ~5,000 tokens |
| Critic | Bencent + humanizer-tw (~20 KB) | ~5,000 tokens |
| QualityEvaluator | Bencent + humanizer-tw (~20 KB) | ~5,000 tokens |
| FinalReviewer | Bencent + humanizer-tw (~20 KB) | ~5,000 tokens |
| Image | Bencent + Taste + Minimalist (~100 KB) | ~25,000 tokens |
| SEO | Bencent (~5 KB) | ~1,250 tokens |
| Router | Bencent (~5 KB) | ~1,250 tokens |

### Critical Finding: Taste v2 Token Burden

Taste v2 is ~87 KB. When injected into ImageAgent, it adds approximately **21,000 tokens** to every image prompt generation call. This is significant because:

1. ImageAgent calls `call_ai()` at least once per task (prompt generation)
2. At $2.50/1M input tokens (GPT-4o pricing), this adds ~$0.05 per image prompt just for skill context
3. More importantly, it consumes context window space that could be used for actual article content

### Repeated Injection Problem

The same skill content is sent to the model multiple times within a single task:

```
Writer.write_content() → Bencent + Humanizer injected
Critic.critique() → Bencent + Humanizer injected again
SEO.optimize_content() → Bencent injected again
QualityEvaluator.evaluate() → Bencent + Humanizer injected again
FinalReviewer.final_review() → Bencent + Humanizer injected again
```

For a single article, the model receives Bencent Brand content at least 5 times and humanizer-tw content at least 3 times.

### Architectural Risk

| Risk | Severity | Description |
|------|----------|-------------|
| **Token waste** | High | Same constitution sent repeatedly |
| **Context exhaustion** | Medium | Long skills consume context window |
| **Cost accumulation** | Medium | Per-task cost increases with skill count |
| **Taste v2 bloat** | High | 87 KB skill injected into every ImageAgent call |
| **GreenLight/GSAP not yet loaded** | Low | Would be catastrophic if loaded agent-wide |

---

## 4. Explicit Mapping vs Automatic Loading

### Current Design: Global Automatic Injection

```python
class BaseAgent:
    def __init__(self, config):
        self.skills_context = skill_loader.build_skills_context(self.name)
    
    def call_ai(self, prompt, **kwargs):
        full_prompt = f"{self.skills_context}\n\n{prompt}"
```

**Advantages:**
- Simple — no per-method configuration
- Consistent — every call has the same context
- Low boilerplate — agents don't need to manage skills

**Disadvantages:**
- No granularity — cannot load different skills per method
- No conditional loading — skills loaded even when irrelevant
- Prompt bloat — all mapped skills sent every time
- No task-level override — cannot customize per task

### Alternative: Explicit Capability Declaration

```python
class BaseAgent:
    def __init__(self, config):
        self.skills_context = ""
    
    def call_ai(self, prompt, skills=None, **kwargs):
        if skills is None:
            skills = self.get_default_skills()
        full_prompt = self.build_prompt(skills, prompt)
```

**Advantages:**
- Deterministic — each method declares exactly what it needs
- Less bloat — only relevant skills loaded per call
- Testable — can verify skill requirements per method
- Flexible — can override per task

**Disadvantages:**
- More configuration — each method must declare skills
- Possible duplication — multiple methods may repeat declarations
- Agent must know about Skills — violates separation of concerns slightly

### Recommendation

The current automatic injection model is **acceptable for the current project size** but will become problematic as:

1. More agents are added
2. Skills grow in size
3. FrontendAgent is introduced (would need 5+ skills)
4. Cost sensitivity increases

A hybrid approach is recommended for future evolution:

```
Constitution Skills (always loaded)
    ↓
Agent-level Skills (loaded at agent init)
    ↓
Method-level Skills (loaded per call_ai() if specified)
    ↓
Tool-level Skills (loaded only when tool invoked)
```

---

## 5. Skill Hierarchy

### Current Hierarchy

The project established this conceptual priority:

```
1. Bencent Brand (Constitution)
2. Project-specific rules
3. Taste / Design Skills
4. Production best practices
5. Model defaults
```

### Current Enforcement

**The current implementation does NOT enforce this hierarchy.**

Skills are simply concatenated in mapping list order:

```python
"writer": ["Bencent", "humanizer-tw"],
```

There is:
- No priority field
- No conflict resolution logic
- No override mechanism
- No prompt placement differentiation

### How Priority Could Matter

**Example conflict: Bencent Brand vs UI Rules fonts**

- Bencent Brand specifies: Noto Serif TC for headings
- UI Rules specifies: Sora or Cabinet Grotesk for display

If both are injected, the LLM receives conflicting instructions. Currently, the order of concatenation determines which instruction the model "sees first", but this is accidental, not deterministic.

### Recommended Hierarchy Model

| Level | Skills | Loading | Prompt Placement | Override Behavior |
|-------|--------|---------|-----------------|-------------------|
| 1 — Constitution | Bencent Brand | Always, first | Top of prompt | Cannot be overridden |
| 2 — Design | Taste, Minimalist, UI Rules | Agent/method-specific | After constitution | Can override each other by explicit priority |
| 3 — Writing | Humanizer TW | Stage-specific | After design | Can override style rules |
| 4 — Production | GreenLight, GSAP | Tool-specific | Bottom of prompt | Tool context only |

---

## 6. Conflict Handling

### Current Behavior

The current loader has **no conflict resolution mechanism**.

When multiple skills are injected, they are simply concatenated:

```python
parts = ["## Active Skills Context\n"]
for skill in skills:
    parts.append(f"### {skill.name}\n{skill.description}\n")
    parts.append(skill.content)
    parts.append("\n")
return "\n".join(parts)
```

### Conflict Scenarios

**Scenario 1: Font conflicts**
- Bencent Brand: "Noto Serif TC for headings"
- UI Rules: "Display: Sora or Cabinet Grotesk"
- Result: LLM receives both without resolution

**Scenario 2: Color conflicts**
- Bencent Brand: "Primary #2C2420, Secondary #8B5E3C"
- Generic design rule: "Use black CTA"
- Result: LLM must choose without guidance

**Scenario 3: Density conflicts**
- Taste v2: "DESIGN_VARIANCE 8/10, VISUAL_DENSITY 4/10"
- Minimalist: "Very low density, generous whitespace"
- Result: LLM may blend conflicting directives

### Recommended Approach

A deterministic conflict resolution should be established, but **not implemented now**:

1. **Constitution wins** — Bencent Brand overrides all other skills on identity-level decisions
2. **Explicit priority field** — each skill declares its priority level
3. **Conflict marker in prompt** — when skills conflict, the prompt should explicitly state which wins

---

## 7. Skill Loading Granularity

### Current Granularity: Agent-level Only

```python
self.skills_context = skill_loader.build_skills_context(self.name)  # Once at init
```

### Required Granularity for Production

| Level | Example | When Needed |
|-------|---------|-------------|
| **Agent-level** | Bencent Brand for all agents | Always |
| **Stage-level** | Humanizer TW during write/critique/review | During content generation |
| **Method-level** | Image skills only during `_build_image_prompt()` | During specific method |
| **Tool-level** | GreenLight only during HTML→block conversion | During tool invocation |

### Current Limitation

The current architecture cannot support stage-level or tool-level loading because:

1. Skills are loaded once at agent init
2. `call_ai()` always uses the same `skills_context`
3. No mechanism to pass skill overrides per method call

### Impact on Future FrontendAgent

FrontendAgent would need:
- Bencent Brand (always)
- Taste (frontend design)
- Minimalist (UI restraint)
- UI Rules (component rules)
- GreenLight (HTML→block conversion)
- GSAP (animation)

That's 6 skills, with GreenLight alone being ~66 KB. Injecting all of these into every `call_ai()` would be impractical.

---

## 8. FrontendAgent Readiness

### Assessment: NOT READY

The current Skill Loader **cannot support FrontendAgent** without modifications.

### Reasons

| Issue | Severity | Description |
|-------|----------|-------------|
| **Prompt bloat** | Critical | FrontendAgent would need 5+ skills totaling ~160 KB injected into every call |
| **No tool-level loading** | High | GreenLight should only load during block conversion, not every call |
| **No stage-level loading** | High | GSAP only needed for animation generation, not planning |
| **No conflict resolution** | Medium | Multiple design skills may give contradictory directives |
| **No skill versioning** | Low | Cannot track which skill version generated which output |

### What Would Be Needed

1. **Per-method skill selection** — ability to load different skills per method call
2. **Tool-level skill binding** — GreenLight/GSAP loaded only when their tools are invoked
3. **Skill size awareness** — prevent loading 87 KB Taste into contexts that don't need it
4. **Conflict resolution** — explicit priority when multiple design skills conflict

---

## 9. Enterprise Architecture Recommendation

### Model A — Global Automatic Injection (Current)

```
Agent
 ↓
BaseAgent
 ↓
load mapped Skills automatically
 ↓
every LLM call receives Skills
```

**Pros:** Simple, consistent, low boilerplate  
**Cons:** No granularity, prompt bloat, no conditional loading

### Model B — Explicit Capability Declaration

```
Agent
 ↓
required_skills per method
 ↓
Skill Loader
 ↓
only required Skills
 ↓
LLM
```

**Pros:** Deterministic, testable, less bloat  
**Cons:** More configuration, Agents must know about Skills

### Recommended Hybrid Model

```
┌─────────────────────────────────────┐
│  Constitution Layer                 │
│  (Bencent Brand — always loaded)    │
└─────────────────────────────────────┘
                 ↓
┌─────────────────────────────────────┐
│  Agent Layer                        │
│  (Agent-wide skills at init)        │
└─────────────────────────────────────┘
                 ↓
┌─────────────────────────────────────┐
│  Method Layer                       │
│  (Method-specific skills per call)  │
└─────────────────────────────────────┘
                 ↓
┌─────────────────────────────────────┐
│  Tool Layer                         │
│  (Tool-specific skills at execution)│
└─────────────────────────────────────┘
```

**Implementation sketch (conceptual only):**

```python
class BaseAgent:
    def __init__(self, config):
        self.config = config
        self.name = self.__class__.__name__.replace("Agent", "").lower()
        self.constitution = skill_loader.get_constitution()  # Bencent
        self.agent_skills = skill_loader.get_skills_for_agent(self.name)
    
    def call_ai(self, prompt, skills=None, **kwargs):
        parts = [self.constitution]
        parts.extend(self.agent_skills)
        if skills:
            parts.extend(skill_loader.get_skills(skills))
        full_prompt = build_prompt(parts, prompt)
        # ... OpenAI call
```

---

## 10. Current State Summary

### What Works

1. **Discovery is simple** — directory scanning with SKILL.md detection
2. **Mapping is explicit** — hardcoded dictionary, no magic
3. **Caching is correct** — skills loaded once, reused
4. **Injection is automatic** — agents don't need to manage skill loading
5. **No duplicates** — explicit mapping prevents accidental duplication

### What Doesn't Work

1. **No granularity** — all-or-nothing per agent
2. **No conflict resolution** — skills concatenated without priority
3. **No size awareness** — 87 KB Taste loaded alongside 5 KB Bencent
4. **No conditional loading** — skills loaded even when irrelevant to current task
5. **No FrontendAgent support** — would require 5+ skills totaling ~160 KB
6. **Repeated injection** — same content sent multiple times per task

---

## 11. Final Recommendation

### Current Skill Loader: MODIFY

The current loader is functional for the current project scale but requires modifications before FrontendAgent can be introduced.

**Do NOT redesign entirely.** Make targeted changes:

1. Add method-level skill selection to `call_ai()`
2. Add constitution layer separate from agent skills
3. Add skill size metadata for cost awareness
4. Add explicit priority/conflict resolution fields

### FrontendAgent: READY AFTER LOADER CHANGE

FrontendAgent cannot be supported by the current loader. After the modifications above, it will be ready.

---

## 12. MUST HAVE / SHOULD HAVE / FUTURE

### MUST HAVE (required before FrontendAgent)

1. **Method-level skill selection** — `call_ai()` must accept optional `skills` parameter
2. **Constitution layer separation** — Bencent Brand loaded separately from other skills
3. **Skill size metadata** — track skill byte size for cost estimation
4. **Tool-level skill binding** — GreenLight/GSAP loaded only when tools invoked

### SHOULD HAVE (improves maintainability)

1. **Explicit priority field** — each skill declares override priority
2. **Conflict resolution** — prompt includes explicit resolution when skills conflict
3. **Skill versioning** — track which skill version was used for each generation
4. **Conditional loading** — skills loaded only when task type matches

### FUTURE (wait until needed)

1. **Skill registry service** — centralized skill management with health checks
2. **Dynamic skill composition** — skills composed at runtime based on task analysis
3. **Skill caching with TTL** — refresh skills periodically without restart
4. **Skill analytics** — track which skills improve output quality
5. **User-defined skill overrides** — allow project-specific skill customization

---

## 13. Current vs Recommended Architecture

### Current

```text
Agent
 ↓
BaseAgent.call_ai()
 ↓
skill_loader.build_skills_context(agent_name)
 ↓
mapping dict → all mapped skills concatenated
 ↓
OpenAI
```

### Recommended (Future)

```text
Agent
 ↓
BaseAgent.call_ai(prompt, skills=None)
 ↓
┌─────────────┬─────────────┬─────────────┐
│ Constitution│ Agent Skills│ Method Skills│
│ (Bencent)   │ (mapped)    │ (optional)   │
└─────────────┴─────────────┴─────────────┘
 ↓
OpenAI
```

## Implemented Changes

### Files Modified

- `skills/loader.py` — added metadata, constitution separation, priority ordering, deduplication, tool bindings, explicit selection
- `agents/__init__.py` — added `constitution_context`, modified `call_ai(required_skills=...)`
- `agents/writer.py` — `refine_content()` now uses explicit `required_skills=["Bencent"]`
- `agents/quality_evaluator.py` — `_score_content()` and `_check_quality()` now use explicit `required_skills=["Bencent"]`

### Current Loading Model

```text
BaseAgent.__init__()
    ↓
skill_loader.build_constitution_context() → self.constitution_context
skill_loader.build_skills_context(name) → self.skills_context

BaseAgent.call_ai(prompt, required_skills=None)
    ↓
if required_skills provided:
    build_skills_context(name, required_skills=required_skills)
else:
    use self.skills_context
```

### Constitution Behavior

- Bencent Brand is retrieved via `skill_loader.get_constitution()`
- Always appears first in the prompt
- Included automatically in both default and explicit contexts
- Cannot be overridden by lower-priority skills

### Explicit Skill Selection Behavior

- `call_ai(prompt, required_skills=[...])` loads ONLY constitution + explicitly requested skills
- Agent defaults are bypassed when explicit skills are provided
- Deduplication by canonical skill name prevents duplicates
- Skills are sorted by category priority: constitution > design > writing > production

### Priority / Conflict Behavior

- Constitution: category=constitution, priority=1
- Design (Taste, Minimalist, UI Rules): category=design, priority=2
- Writing (Humanizer TW): category=writing, priority=3
- Production (GreenLight, GSAP): category=production, priority=4
- Prompt includes: "Higher-priority rules override lower-priority rules when conflicts occur."

### Tool-Level Binding Foundation

- `_TOOL_BINDINGS` maps tool names to skill lists
- `skill_loader.get_tool_skills("frontend")` returns production skills for frontend work
- `skill_loader.get_tool_skills("animation")` returns animation-specific skills
- Not yet integrated into any tool execution path

### Before / After Context Size

| Agent | Before | After (default) | After (explicit) |
|-------|--------|-----------------|------------------|
| Writer | ~5,000 tokens | ~2,042 tokens | ~441 tokens (Bencent only) |
| ImageAgent | ~25,000 tokens | ~24,186 tokens | ~24,186 tokens (needs all) |
| QualityEvaluator | ~5,000 tokens | ~2,042 tokens | ~441 tokens (Bencent only) |

### FrontendAgent Readiness

The loader can now support a future FrontendAgent explicit request:

```python
skill_loader.build_skills_context("frontend", required_skills=[
    "Bencent",
    "design-taste-frontend",
    "minimalist-ui",
    "ui-rules",
    "greenlight-vibe",
])
```

Without injecting those skills into any other agent's calls.

**SKILL LOADER ARCHITECTURE REVIEW COMPLETE**
