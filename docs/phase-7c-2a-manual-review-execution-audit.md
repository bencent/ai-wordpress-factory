# Phase 7C-2A — MANUAL_REVIEW Execution Audit

**Date:** 2026-09-10  
**Mode:** Read-only investigation  
**Scope:** Current runtime behavior of `TaskStatus.MANUAL_REVIEW`

---

## 1. Where MANUAL_REVIEW Is Assigned

**Exact Code Path:** `main.py:314`

```python
# Step 7: 人工校稿檢查點
workflow_state.update_task_status(task_id, TaskStatus.MANUAL_REVIEW)
self._manual_review_checkpoint(task)
```

This occurs in `AIWordPressFactory.run_workflow()` **after** the complete frontend pipeline (FrontendAgent → FrontendSecurityGate → GreenLightConverter → FrontendValidator) and **before** ImageAgent and WordPressPublisher.

The `_manual_review_checkpoint` method is defined at `main.py:498-534`.

---

## 2. Does It Actually Block Execution?

**YES — It truly blocks execution.** The workflow **pauses and waits for human input** via blocking `input()` call.

### Execution Path

```
FinalReviewer
    ↓
FRONTEND_GENERATING → FrontendAgent
    ↓
FRONTEND_SECURITY_CHECK → FrontendSecurityGate
    ↓
FRONTEND_CONVERTING → GreenLightConverter
    ↓
FRONTEND_VALIDATING → FrontendValidator
    ↓
MANUAL_REVIEW ← UPDATE STATUS HERE
    ↓
_manual_review_checkpoint(task)  ← BLOCKS HERE (input())
    ├─ User enters '.' → accepts AI draft → continues
    ├─ User enters 'skip' → skips review → continues  
    └─ User enters text → replaces task.final_content → continues
    ↓
GENERATING_IMAGE → ImageAgent
    ↓
PUBLISHING → WordPressPublisher
    ↓
COMPLETED
    ↓
LEARNING
```

### Evidence from `_manual_review_checkpoint` (`main.py:498-534`)

```python
def _manual_review_checkpoint(self, task: Task) -> None:
    content = task.final_content or task.revised_content or task.optimized_content or task.draft_content or ""
    
    print("\n" + "=" * 60)
    print(f"人工校稿檢查點：任務「{task.title}」")
    print("=" * 60)
    print("\n【AI 草稿內容】\n")
    print(content[:2000] + ("..." if len(content) > 2000 else ""))
    print("\n" + "=" * 60)
    print("請審閱以上內容。")
    print("- 直接貼上修正後的完整內容")
    print("- 或輸入 '.' 表示接受原稿")
    print("- 或輸入 'skip' 跳過校稿（不建議）")
    print("=" * 60)
    
    try:
        user_input = input("\n請輸入修正內容: ").strip()  # ← BLOCKING CALL
    except EOFError:
        logger.warning("無法讀取使用者輸入，使用 AI 草稿")
        return
    
    if user_input == ".":
        logger.info("使用者接受原稿")
        return
    
    if user_input.lower() == "skip":
        logger.warning("使用者跳過校稿")
        return
    
    if user_input:
        task.final_content = user_input
        logger.info("使用者提供了修正內容")
```

**Key observations:**
- `input()` is a **blocking synchronous call** — the process stops here until user provides stdin
- No timeout, no async, no background thread
- On `EOFError` (non-interactive terminal), it logs warning and **continues with AI draft** (does not fail)
- The status `MANUAL_REVIEW` persists in `workflow_state` during the pause

---

## 3. Is There a Resume Mechanism?

**NO — There is no resume mechanism.**

### What Doesn't Exist

| Mechanism | Exists? | Evidence |
|-----------|---------|----------|
| Explicit `resume()` / `continue()` method | ❌ | No such method in `AIWordPressFactory` or `WorkflowState` |
| CLI argument to resume from MANUAL_REVIEW | ❌ | `--load-state` loads state but `run_workflow()` restarts from beginning |
| API endpoint for manual review completion | ❌ | No HTTP server / API in codebase |
| State machine that skips completed steps on reload | ❌ | `run_workflow()` always executes sequentially from Step 1 |
| Persisted "paused" state distinct from MANUAL_REVIEW | ❌ | Status is just an enum value, no paused/waiting sub-state |

### What `load_state` Does (`main.py:546-559`)

```python
def load_state(self, file_path: str = "workflow_state.json") -> None:
    global workflow_state
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        workflow_state = WorkflowState.from_dict(data)
        logger.info(f"工作流程狀態已從 {file_path} 加載")
    except FileNotFoundError:
        logger.warning(f"文件 {file_path} 不存在，使用默認狀態")
```

- Only deserializes tasks and global state
- **Does not** resume workflow from the paused step
- Calling `run_workflow(task_id)` after `load_state()` **re-executes the entire workflow from Planner**

### Current Behavior on Reload

```
CLI: python main.py --load-state workflow.json --task "Title"
    ↓
load_state() → workflow_state restored (task.status == MANUAL_REVIEW)
    ↓
create_task() → CREATES NEW TASK (different ID)
    ↓
run_workflow() → STARTS FROM PLANNING (Step 1)
```

The original task with `MANUAL_REVIEW` status sits in `workflow_state.tasks` but is **never resumed**.

---

## 4. CLI / Runtime Implications

### In Interactive Terminal (Normal Case)

```
$ python main.py --task "My Blog Post"
...
人工校稿檢查點：任務「My Blog Post」
============================================================
【AI 草稿內容】

...content...

============================================================
請輸入修正內容: .     ← USER MUST TYPE HERE
```

- Process **blocks indefinitely** at `input()`
- Cannot be backgrounded, cannot run in CI/CD, cannot run headless
- User must be physically present at terminal

### In Non-Interactive Environment (CI/CD, Docker, systemd, Cron)

```
$ python main.py --task "My Blog Post"
...
人工校稿檢查點：任務「My Blog Post」
...
請輸入修正內容: 
無法讀取使用者輸入，使用 AI 草稿
```

- `EOFError` raised immediately
- Logs warning: `"無法讀取使用者輸入，使用 AI 草稿"`
- **Continues with AI draft automatically** — effectively bypasses manual review
- No failure, no pause, no alert

### State Persistence

- If process is killed during `input()`, task remains in `MANUAL_REVIEW` in JSON
- On restart with `--load-state`, see §3 — workflow restarts from beginning, old task orphaned

---

## 5. Commercial Product Implications

### Target Model: Client (業主) Operates Independently

| Mode | Current Support | Reality |
|------|-----------------|---------|
| **Mode A — Autonomous** (AI → gates → publish) | ❌ Not supported | `MANUAL_REVIEW` is **mandatory**, hardcoded in workflow |
| **Mode B — Client Review** (AI → gates → client approval → publish) | ⚠️ Partially supported | Blocking `input()` only works for local CLI; no web UI, no async notification, no multi-user |
| **Mode C — Internal/Dev** (AI → gates → debug checkpoint → continue) | ✅ Supported | Works for Bencent at terminal during development |

### Critical Gaps for Commercial Deployment

1. **No policy control** — `MANUAL_REVIEW` is unconditional; cannot be disabled via config
2. **No async/headless support** — Blocks on stdin; incompatible with server/deployment
3. **No resume** — Cannot stop and continue later; process must stay alive
4. **No multi-user** — Single terminal, single user; no client portal
5. **No audit trail** — No record of who approved, when, what changes made
6. **No timeout/fallback policy** — Infinite wait or silent bypass on EOF

---

## 6. Recommended Future Architecture

### Policy-Controlled Approval Gate

```
Client Configuration (config.yaml / DB)
        ↓
ApprovalPolicy enum: AUTO_PUBLISH | REQUIRE_HUMAN_REVIEW | REQUIRE_CLIENT_REVIEW
        ↓
Workflow Branch:
  ├─ AUTO_PUBLISH        → Skip MANUAL_REVIEW entirely
  ├─ REQUIRE_HUMAN_REVIEW → Current blocking input() (dev only)
  └─ REQUIRE_CLIENT_REVIEW → Async: persist task, notify client, webhook/callback on completion
```

### Required Changes for Production

| Component | Change |
|-----------|--------|
| `config.py` | Add `approval_policy` setting |
| `main.py` | Branch workflow based on policy |
| `state.py` | Add `approval_status`, `approved_by`, `approved_at`, `review_notes` to Task |
| New: `approval_gate.py` | Async approval interface (webhook, polling, callback) |
| New: CLI `resume` command | Resume from persisted MANUAL_REVIEW task |
| New: Web UI / API | Client review interface |

---

## 7. KEEP / REWORK / REMOVE Recommendation

### Recommendation: **KEEP (as dev/debug tool) + REWORK (for production)**

| Aspect | Verdict | Rationale |
|--------|---------|-----------|
| `MANUAL_REVIEW` enum value | **KEEP** | Useful semantic state for future policy gate |
| `_manual_review_checkpoint()` blocking `input()` | **REWORK** | Replace with policy-controlled async gate |
| Hardcoded position in workflow | **REWORK** | Make conditional on `approval_policy` |
| Current resume mechanism | **REMOVE** | Doesn't exist; build proper resume from persisted state |

---

## 8. Scope for Future Implementation

### Phase 7C-3 (Policy Gate) — Suggested Scope

1. **Config**: Add `approval_policy: "auto" | "human" | "client"` to config schema
2. **Workflow Branch**: In `run_workflow()`, skip `_manual_review_checkpoint()` when `auto`
3. **Task Fields**: Add `approval_status`, `approved_by`, `approved_at`, `review_notes`
4. **CLI**: Add `--resume <task_id>` to continue from `MANUAL_REVIEW` (load state, jump to ImageAgent)
5. **Tests**: Verify all three policy modes

### Phase 7C-4 (Client Review UI) — Out of Scope for Now

- Web dashboard for client review
- Webhook/callback for async completion
- Email/notification integration
- Multi-tenant approval workflows

---

## Summary

| Question | Answer |
|----------|--------|
| **Where assigned?** | `main.py:314` in `run_workflow()` after frontend pipeline |
| **Blocks execution?** | **YES** — blocking `input()` call in `_manual_review_checkpoint()` |
| **Resume mechanism?** | **NONE** — `load_state` doesn't resume; workflow restarts from Step 1 |
| **CLI behavior?** | Interactive: waits for user. Non-interactive: silent bypass with warning |
| **Commercial fit?** | **Mode A (Autonomous): Broken** — mandatory gate cannot be disabled<br>**Mode B (Client Review): Broken** — no async, no UI, no multi-user<br>**Mode C (Dev): Works** — suitable for local development only |

**Conclusion:** The current `MANUAL_REVIEW` is a **development checkpoint**, not a production approval gate. It must be made policy-controlled and async for commercial use.