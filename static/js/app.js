import {ApiError, NetworkError, TimeoutError, createApiClient} from './api.js';
import {createState} from './state.js';

const POLL_INTERVAL_MS = 4000;
const TASK_LIMIT = 20;
const api = createApiClient();
const state = createState();
const shell = document.querySelector('[data-active-panel]');
const tabs = [...document.querySelectorAll('[data-panel][role="tab"]')];
const panelButtons = [...document.querySelectorAll('[data-panel]:not([role="tab"])')];
const elements = Object.fromEntries([...document.querySelectorAll('[id]')].map((element) => [element.id, element]));
const form = elements['task-form'];
let listInFlight = false;
let eventInFlightGeneration = null;
let detailGeneration = 0;
let eventGeneration = 0;
let publicationGeneration = 0;
let publicationInFlightGeneration = null;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function replaceChildren(target, children) {
  target.replaceChildren(...children);
}

function formatTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '—' : new Intl.DateTimeFormat('zh-Hant', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }).format(date);
}

function panelFor(button) {
  return button.dataset.panel;
}

function setPanel(panel) {
  if (!['tasks', 'menu', 'messages'].includes(panel)) return;
  shell.dataset.activePanel = panel;
  tabs.forEach((tab) => tab.setAttribute('aria-selected', String(tab.dataset.panel === panel)));
  document.querySelectorAll('[data-panel][aria-current]').forEach((button) => {
    if (button.dataset.panel === panel) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
}

function setDraft(patch, {resetSubmission = true} = {}) {
  const current = state.snapshot;
  const submission = resetSubmission && !['submitting', 'uncertain', 'success'].includes(current.submission.phase)
    ? {phase: 'idle', key: null, body: null, task: null, message: ''}
    : current.submission;
  state.set({draft: {...current.draft, ...patch}, submission});
}

function renderForm(next) {
  const ready = Boolean(next.bootstrap);
  const locked = ['submitting', 'uncertain', 'success'].includes(next.submission.phase);
  elements['task-fields'].disabled = !ready || locked;
  elements['content-type'].value = next.draft.content_type;
  elements.topic.value = next.draft.topic;
  elements.brief.value = next.draft.brief;
  elements['target-audience'].value = next.draft.target_audience;
  elements['page-purpose'].value = next.draft.page_purpose;
  const isPage = next.draft.content_type === 'PAGE';
  elements['audience-field'].hidden = isPage;
  elements['purpose-field'].hidden = !isPage;
  elements['target-audience'].required = !isPage;
  elements['page-purpose'].required = isPage;
  elements['site-label'].textContent = next.bootstrap?.defaults?.site_id || '等待載入';
  elements['brand-label'].textContent = next.bootstrap?.defaults?.brand_profile_id || '等待載入';
  elements['submission-message'].textContent = next.submission.message || '';
  elements['submission-message'].classList.toggle('is-error', ['failed', 'uncertain'].includes(next.submission.phase));
  elements['uncertain-actions'].hidden = next.submission.phase !== 'uncertain';
  elements['new-task-button'].hidden = next.submission.phase !== 'success';
  elements['request-summary'].hidden = next.submission.phase !== 'success';
  if (next.submission.phase === 'success' && next.submission.body) renderRequestSummary(next.submission.body);
}

function renderRequestSummary(body) {
  const rows = [
    ['內容類型', body.content_type], ['主題', body.topic], ['需求說明', body.brief],
    [body.content_type === 'POST' ? '目標讀者' : '頁面用途', body.target_audience || body.page_purpose],
    ['Site', body.site_id], ['Brand', body.brand_profile_id],
  ];
  replaceChildren(elements['request-summary'], rows.flatMap(([label, value]) => {
    const term = element('dt', '', label);
    const detail = element('dd', '', value || '—');
    return [term, detail];
  }));
}

function renderTasks(next) {
  elements['timeline-loading'].hidden = !next.tasksLoading || next.tasks.length > 0;
  elements['timeline-empty'].hidden = next.tasksLoading || next.tasks.length > 0;
  elements['timeline-content'].hidden = next.tasks.length === 0;
  elements['load-more'].hidden = !next.nextCursor;
  if (!next.tasks.length) return replaceChildren(elements['timeline-content'], []);
  const items = next.tasks.map((task) => {
    const button = element('button', 'task-row');
    button.type = 'button';
    button.dataset.taskId = task.task_id;
    button.setAttribute('aria-pressed', String(task.task_id === next.selectedTaskId));
    const main = element('span', 'task-row-main');
    main.append(element('strong', '', task.topic || '未命名任務'), element('span', 'muted', task.content_type || '—'));
    const meta = element('span', 'task-row-meta');
    const chip = element('span', 'status-chip', task.status || 'UNKNOWN');
    chip.dataset.status = task.status || 'UNKNOWN';
    meta.append(chip, element('time', '', formatTime(task.created_at)));
    button.append(main, meta);
    button.addEventListener('click', () => selectTask(task.task_id));
    return button;
  });
  replaceChildren(elements['timeline-content'], items);
}

function appendDetailRow(container, label, value) {
  if (value === undefined || value === null || value === '') return;
  container.append(element('dt', '', label), element('dd', '', value));
}

function renderDetail(next) {
  const detail = next.taskDetail;
  const status = detail?.status ?? null;
  elements['current-task-empty'].hidden = Boolean(detail);
  elements['current-task-detail'].hidden = !detail;
  elements['event-region'].hidden = !next.selectedTaskId;
  elements['current-task-status'].textContent = detail?.status || (next.selectedTaskId ? '載入中' : '尚未選擇');
  elements['current-task-status'].dataset.status = detail?.status || '';
  const retryable = (status === 'FAILED' || status === 'WORKER_LOST');
  const retryIntent = next.retryIntent;
  const retryActive = retryIntent?.taskId === next.selectedTaskId;
  const retrySubmitting = retryActive && retryIntent.phase === 'submitting';
  elements['retry-actions'].hidden = !(retryable && next.selectedTaskId);
  elements['retry-task'].disabled = retrySubmitting;
  elements['retry-task'].textContent = retryActive && retryIntent.phase === 'uncertain'
    ? '再次送出重試'
    : '以相同需求重試';
  elements['retry-cancel'].hidden = !(retryActive && retryIntent.phase === 'uncertain');
  if (detail) {
    const list = element('dl', 'detail-grid');
    appendDetailRow(list, '主題', detail.topic);
    appendDetailRow(list, '內容類型', detail.content_type);
    appendDetailRow(list, '狀態', detail.status);
    appendDetailRow(list, '建立時間', formatTime(detail.created_at));
    appendDetailRow(list, '更新時間', formatTime(detail.updated_at));
    appendDetailRow(list, '執行次數', detail.current_run?.attempt);
    appendDetailRow(list, '目前執行狀態', detail.current_run?.status);
    appendDetailRow(list, '安全錯誤代碼', detail.current_run?.error_code);
    replaceChildren(elements['current-task-detail'], [list]);
  }
  elements['event-empty'].hidden = next.events.length > 0;
  elements['event-sync'].textContent = next.eventsLoading ? '同步中' : '已同步';
  const events = next.events.map((event) => {
    const item = element('li', 'event-item');
    const heading = element('div', 'event-heading');
    heading.append(element('strong', '', event.summary || '任務狀態已更新'), element('time', 'muted', formatTime(event.created_at)));
    const meta = element('div', 'event-meta');
    [event.status, event.metadata?.stage, event.metadata?.agent, event.attempt ? `第 ${event.attempt} 次` : null]
      .filter(Boolean).forEach((value) => meta.append(element('span', '', value)));
    item.append(heading, meta);
    return item;
  });
  replaceChildren(elements['event-list'], events);
}

// Human Review. Task detail status is the only eligibility signal: events, run
// attempt, content type and the task list are never consulted. latest_content_version_id
// is sent verbatim; when it is missing neither action is enabled and no id is invented.
function taskVersionId(detail) {
  const value = detail?.latest_content_version_id;
  return typeof value === 'string' && value ? value : null;
}

function reviewVersionId(detail) {
  if (detail?.status !== 'AWAITING_APPROVAL') return null;
  return taskVersionId(detail);
}

function reviewTarget() {
  const next = state.snapshot;
  const contentVersionId = reviewVersionId(next.taskDetail);
  if (!contentVersionId || !next.selectedTaskId) return null;
  return {taskId: next.selectedTaskId, contentVersionId};
}

// An intent only ever belongs to the exact task + content version it was created for.
// Selecting another task therefore cannot render a foreign intent, and a version that
// moved on invalidates the intent instead of reusing its key.
function matchedIntent(intent, next) {
  const contentVersionId = reviewVersionId(next.taskDetail);
  if (!intent || !contentVersionId) return null;
  if (intent.taskId !== next.selectedTaskId) return null;
  return intent.contentVersionId === contentVersionId ? intent : null;
}

function reviewBusy(next) {
  return matchedIntent(next.approveIntent, next)?.phase === 'submitting'
    || matchedIntent(next.revisionIntent, next)?.phase === 'submitting';
}

function renderReview(next) {
  const detail = next.taskDetail;
  const region = elements['review-region'];
  const awaiting = detail?.status === 'AWAITING_APPROVAL';
  const contentVersionId = reviewVersionId(detail);
  const approve = matchedIntent(next.approveIntent, next);
  const revision = matchedIntent(next.revisionIntent, next);
  const submitting = reviewBusy(next);
  // An uncertain request owns the block until it is retried or cancelled, so a second
  // logical request can never be minted for the same content version.
  const uncertain = approve?.phase === 'uncertain' || revision?.phase === 'uncertain';
  const blocked = submitting || uncertain;

  let message = next.reviewMessage || '';
  let failed = Boolean(next.reviewMessageError);
  if (!message && awaiting) {
    if (!contentVersionId) {
      message = '目前無法取得待核准的內容版本，請重新載入後再試。';
      failed = true;
    } else if (approve?.phase === 'submitting') {
      message = '正在核准…';
    } else if (revision?.phase === 'submitting') {
      message = '正在送出修改要求…';
    } else if (approve?.phase === 'uncertain') {
      message = '無法確認核准是否完成。';
      failed = true;
    } else if (revision?.phase === 'uncertain') {
      message = '無法確認修改要求是否已送出。';
      failed = true;
    }
  }

  // The controls exist only for AWAITING_APPROVAL. The panel itself stays up while a
  // review outcome is still on screen, so a confirmed result is never silently dropped
  // by the very transition that removes the controls.
  region.hidden = !detail || (!awaiting && !message);
  elements['review-note'].hidden = !awaiting;
  elements['review-actions'].hidden = !awaiting || !contentVersionId;
  elements['approve-task'].disabled = !contentVersionId || blocked;
  elements['approve-task'].textContent = approve?.phase === 'submitting' ? '正在核准…' : '核准';
  elements['request-revision'].disabled = !contentVersionId || blocked;

  const open = awaiting && Boolean(next.revisionOpen) && Boolean(contentVersionId);
  elements['request-revision'].setAttribute('aria-expanded', String(open));
  elements['revision-fields'].hidden = !open;
  // A frozen uncertain payload is written back into the field so the operator can read
  // exactly what was sent, and marked read-only so it cannot drift before the retry.
  if (revision?.phase === 'uncertain' && elements['revision-feedback'].value !== revision.feedback) {
    elements['revision-feedback'].value = revision.feedback;
  }
  elements['revision-feedback'].readOnly = revision?.phase === 'uncertain';
  elements['revision-feedback'].disabled = submitting;
  elements['submit-revision'].disabled = submitting || !contentVersionId;
  elements['cancel-revision'].hidden = uncertain;
  elements['cancel-revision'].disabled = submitting;

  elements['review-uncertain-actions'].hidden = !uncertain;
  elements['resubmit-review'].disabled = submitting;

  elements['review-message'].textContent = message;
  elements['review-message'].classList.toggle('is-error', failed);
}

// Publication & Recovery.
//
// PublicationRequest state is the only authority for publication truth. Task status is
// never consulted, and no lineage is ever collapsed or reordered: a task can hold
// several (a second content version, or the same version sent elsewhere after a
// re-point), and "the latest one" would be a guess about which destination the operator
// meant. Every card carries its own publication_id and nothing is derived from position.
const PUBLICATION_STATE_COPY = {
  PENDING: {title: '等待發布', detail: '已排入發布佇列。'},
  IN_PROGRESS: {title: '正在發布', detail: '內容正在送出至網站。'},
  SUCCEEDED: {title: '已發布', detail: null},
  FAILED: {title: '發布失敗', detail: null},
  INDETERMINATE: {title: '無法確認發布結果', detail: '目前無法在網站上確認這次發布結果。'},
};

// check_state is a view projection that is null for every non-INDETERMINATE publication.
const CHECK_STATE_COPY = {
  UNCERTAIN: {title: '無法確認發布結果', detail: '目前無法在網站上確認這次發布結果。', reconcilable: true},
  CHECK_REQUESTED: {title: '已排入發布結果確認', detail: '系統將於稍後檢查網站上的發布紀錄。', reconcilable: false},
  CHECKING: {title: '正在確認發布結果', detail: '系統正在查詢網站上的發布紀錄。', reconcilable: false},
  STILL_UNCERTAIN: {title: '仍無法確認發布結果', detail: '上次確認仍無法證明這次發布的最終結果。', reconcilable: true},
};

// A deliberately small whitelist of product-level explanations. Target, credential and
// configuration codes are excluded on purpose, and anything unlisted shows no technical
// detail at all rather than echoing a raw code.
const PUBLICATION_ERROR_COPY = {
  AUTHENTICATION: '網站驗證未通過。',
  PERMISSION: '網站拒絕了這次發布。',
  RATE_LIMIT: '網站暫時限制了發布頻率。',
  CONNECTION_NOT_ESTABLISHED: '無法連線到網站。',
  CONNECTION_LOST: '與網站的連線中斷。',
  READ_TIMEOUT: '讀取網站回應逾時。',
  TIMEOUT: '與網站連線逾時。',
  UNAVAILABLE: '網站暫時無法使用。',
};

function contentTypeLabel(value) {
  if (value === 'POST') return '文章';
  if (value === 'PAGE') return '頁面';
  return '內容';
}

// "發布失敗" is reachable from exactly one place: state === 'FAILED'. Every other
// outcome, including an unrecognised check_state, is presented as uncertainty.
function publicationPresentation(publication) {
  const state = publication?.state;
  const base = PUBLICATION_STATE_COPY[state];
  if (!base) return {title: '發布狀態未知', detail: null, reconcilable: false, badge: state || 'UNKNOWN'};
  if (state !== 'INDETERMINATE') {
    const detail = state === 'FAILED' ? PUBLICATION_ERROR_COPY[publication.error_code] || null : base.detail;
    return {title: base.title, detail, reconcilable: false, badge: state};
  }
  const check = CHECK_STATE_COPY[publication.check_state];
  if (!check) {
    // Fail safe: an unknown or missing check_state is never treated as licence to re-arm.
    return {title: PUBLICATION_STATE_COPY.INDETERMINATE.title, detail: PUBLICATION_STATE_COPY.INDETERMINATE.detail, reconcilable: false, badge: state};
  }
  return {title: check.title, detail: check.detail, reconcilable: check.reconcilable, badge: state};
}

function publishTarget(next = state.snapshot) {
  if (next.taskDetail?.status !== 'APPROVED') return null;
  const contentVersionId = taskVersionId(next.taskDetail);
  if (!contentVersionId || !next.selectedTaskId) return null;
  return {taskId: next.selectedTaskId, contentVersionId};
}

function matchedPublishIntent(next = state.snapshot) {
  const target = publishTarget(next);
  const intent = next.publishIntent;
  if (!intent || !target || intent.taskId !== target.taskId) return null;
  return intent.contentVersionId === target.contentVersionId ? intent : null;
}

// Lineage uniqueness is a durable backend fact, so the suppression is read from durable
// publication rows -- never inferred from Task status, and never offering a republish.
function publishedForVersion(publications, contentVersionId) {
  return publications.some((publication) => publication?.content_version_id === contentVersionId);
}

function publicationCard(publication, index, next) {
  const kind = contentTypeLabel(publication.content_type);
  const view = publicationPresentation(publication);
  const card = element('article', 'publication-card');
  card.dataset.publicationId = publication.publication_id;

  const head = element('div', 'publication-head');
  const badge = element('span', 'publication-state', view.title);
  badge.dataset.publicationState = view.badge;
  head.append(element('span', 'publication-kind', kind), badge);

  const detail = view.detail ? element('p', 'publication-detail', view.detail) : null;
  const time = element('p', 'publication-time', `建立 ${formatTime(publication.created_at)} · 更新 ${formatTime(publication.updated_at)}`);

  const actions = element('div', 'form-actions');
  // A remote link exists only when the executor recorded one. The publishing target's
  // base URL is deliberately not in the safe view, so there is nothing to fall back to.
  if (publication.state === 'SUCCEEDED' && typeof publication.remote_url === 'string' && publication.remote_url) {
    const link = element('a', 'text-button', `查看已發布${kind}`);
    link.href = publication.remote_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    actions.append(link);
  }
  if (view.reconcilable) {
    const inFlight = next.reconcileIntents[publication.publication_id]?.taskId === next.selectedTaskId;
    const button = element('button', 'text-button', '重新確認發布結果');
    button.type = 'button';
    button.disabled = Boolean(inFlight);
    // Sibling lineages render several of these, so each needs its own accessible name.
    button.setAttribute('aria-label', `重新確認發布結果（${kind}，第 ${index + 1} 筆發布紀錄）`);
    // Closes over this card's own id. Nothing here can address a sibling lineage.
    button.addEventListener('click', () => requestReconciliation(publication.publication_id));
    actions.append(button);
  }

  card.append(head);
  if (detail) card.append(detail);
  card.append(time);
  if (actions.childElementCount) card.append(actions);
  return card;
}

function renderPublications(next) {
  const region = elements['publication-region'];
  const list = Array.isArray(next.publications) ? next.publications : [];
  const target = publishTarget(next);
  const intent = matchedPublishIntent(next);
  const suppressed = Boolean(target) && publishedForVersion(list, target.contentVersionId);
  const versionUnavailable = next.taskDetail?.status === 'APPROVED' && !taskVersionId(next.taskDetail);
  const visible = Boolean(next.selectedTaskId)
    && (list.length > 0 || Boolean(target) || Boolean(intent) || versionUnavailable);
  region.hidden = !visible;

  const submitting = intent?.phase === 'submitting';
  const uncertain = intent?.phase === 'uncertain';
  const offerPublish = Boolean(intent) || (Boolean(target) && !suppressed);
  elements['publish-actions'].hidden = !offerPublish;
  elements['publish-task'].hidden = Boolean(intent);
  elements['publish-task'].disabled = submitting || !target;
  elements['publish-task'].textContent = submitting
    ? '發布中…'
    : `發布${target ? contentTypeLabel(next.taskDetail?.content_type) : ''}`;
  elements['resubmit-publish'].hidden = !uncertain;
  elements['resubmit-publish'].disabled = submitting;

  let message = next.publicationMessage || '';
  let failed = Boolean(next.publicationMessageError);
  if (!message) {
    if (submitting) message = '正在送出發布要求…';
    else if (uncertain) {
      message = '無法確認發布要求是否已送出。';
      failed = true;
    } else if (versionUnavailable) {
      message = '目前無法取得可發布的內容版本，請重新載入後再試。';
      failed = true;
    } else if (suppressed) message = '這個內容已有發布紀錄，發布狀態請見下方。';
    else if (next.publicationsError) {
      message = next.publicationsError;
      failed = true;
    } else if (next.publicationsLoading && list.length === 0) message = '正在載入發布紀錄…';
  }
  elements['publication-message'].textContent = message;
  elements['publication-message'].classList.toggle('is-error', failed);

  elements['publication-sync'].textContent = next.publicationsLoading
    ? '同步中'
    : next.publicationsError ? '無法同步' : '已同步';
  elements['publication-empty'].hidden = next.publicationsLoading || list.length > 0;
  elements['publication-list'].hidden = list.length === 0;
  // Server order, exactly as returned. No sort, no reverse, no collapsing.
  replaceChildren(elements['publication-list'],
    list.length ? list.map((publication, index) => publicationCard(publication, index, next)) : []);
}

function render(next) {
  const connection = elements['connection-status'];
  const message = elements['system-message'];
  connection.classList.toggle('is-error', Boolean(next.error));
  connection.classList.toggle('is-ready', !next.loading && !next.error && next.connectionState === 'online');
  connection.classList.toggle('is-reconnecting', !next.error && next.connectionState === 'reconnecting');
  connection.classList.toggle('is-offline', !next.error && next.connectionState === 'offline');
  elements['connection-label'].textContent = next.error
    ? '連線異常'
    : next.loading
      ? '載入中'
      : next.connectionState === 'offline'
        ? '離線'
        : next.connectionState === 'reconnecting'
          ? '重新連線中'
          : '已連線';
  message.textContent = next.error ? next.error.message : next.message;
  message.classList.toggle('is-error', Boolean(next.error));
  elements['timeline-date'].textContent = new Intl.DateTimeFormat('zh-Hant', {dateStyle: 'medium'}).format(new Date());
  if (next.status) {
    elements['health-api'].textContent = next.status.api || '未知';
    elements['health-database'].textContent = next.status.database || '未知';
    elements['health-worker'].textContent = next.status.worker?.status || '未知';
  }
  renderForm(next);
  renderTasks(next);
  renderDetail(next);
  renderReview(next);
  renderPublications(next);
}

function safeMessage(error, fallback = '目前無法完成操作，請稍後再試。') {
  if (error instanceof TimeoutError || error instanceof NetworkError) return error.message;
  if (error instanceof ApiError && error.code === 'IDEMPOTENCY_CONFLICT') return '提交識別與先前需求不一致，請取消後重新建立。';
  if (error instanceof ApiError && error.code === 'VALIDATION_ERROR') return '請檢查欄位內容後再送出。';
  // Human Review. Only known, already-mapped backend codes get specific copy; every
  // other failure keeps the generic fallback so raw server text never reaches the DOM.
  if (error instanceof ApiError && error.code === 'APPROVAL_CONFLICT') return '目前狀態無法核准此任務，請重新載入後再試。';
  if (error instanceof ApiError && error.code === 'REVISION_CONFLICT') return '目前狀態無法提出修改要求，請重新載入後再試。';
  if (error instanceof ApiError && error.code === 'TASK_NOT_FOUND') return '找不到這個任務，可能已被移除。';
  // Publication & Recovery. Each of these says what the operator should do next and
  // nothing about the workspace. No target, credential, configuration or idempotency
  // detail is ever named, because the safe view does not contain any and the error
  // body must not become a back channel for it.
  if (error instanceof ApiError && error.code === 'PUBLISH_CONFLICT') return '目前狀態無法發布，請重新載入後再試。';
  if (error instanceof ApiError && error.code === 'PUBLICATION_ALREADY_EXISTS') return '這個內容已有發布紀錄，已重新載入發布狀態。';
  if (error instanceof ApiError && error.code === 'PUBLISH_TARGET_UNAVAILABLE') return '目前尚未設定可用的發布網站。';
  if (error instanceof ApiError && error.code === 'PUBLICATION_NOT_FOUND') return '找不到這筆發布紀錄，已重新載入發布狀態。';
  if (error instanceof ApiError && error.code === 'RECONCILIATION_CONFLICT') return '目前狀態無法重新確認發布結果，已重新載入發布狀態。';
  return fallback;
}

function normalizedBody() {
  const next = state.snapshot;
  const defaults = next.bootstrap?.defaults;
  const draft = next.draft;
  if (!defaults?.site_id || !defaults?.brand_profile_id) throw new ApiError('工作區設定尚未載入。');
  return {
    content_type: draft.content_type,
    topic: draft.topic,
    brief: draft.brief,
    site_id: defaults.site_id,
    brand_profile_id: defaults.brand_profile_id,
    target_audience: draft.content_type === 'POST' ? draft.target_audience : null,
    page_purpose: draft.content_type === 'PAGE' ? draft.page_purpose : null,
  };
}

async function submitTask({reuse = false} = {}) {
  const current = state.snapshot;
  if (current.submission.phase === 'submitting') return;
  if (!reuse && !form.checkValidity()) {
    form.reportValidity();
    state.set({submission: {...current.submission, phase: 'failed', message: '請完成所有必填欄位。'}});
    return;
  }
  const body = reuse ? current.submission.body : normalizedBody();
  const key = reuse ? current.submission.key : crypto.randomUUID();
  if (!body || !key) return;
  state.set({submission: {phase: 'submitting', key, body, task: null, message: '正在建立任務…'}});
  try {
    const task = await api.createTask(body, key);
    state.set({submission: {phase: 'success', key, body, task, message: '任務已建立，需求內容已鎖定。'}});
    mergeTasks([task]);
    await selectTask(task.task_id);
  } catch (error) {
    const uncertain = error instanceof NetworkError || error instanceof TimeoutError;
    state.set({submission: {
      phase: uncertain ? 'uncertain' : 'failed', key, body, task: null,
      message: uncertain ? '結果尚未確認。可使用相同提交識別重新送出。' : safeMessage(error),
    }});
  }
}

function markConnectionFailure() {
  const failures = (state.snapshot.connectionFailures || 0) + 1;
  const connectionState = failures >= 3 ? 'offline' : 'reconnecting';
  state.set({connectionFailures: failures, connectionState});
}

function markConnectionSuccess() {
  if (state.snapshot.connectionState === 'online' && !state.snapshot.connectionFailures) return;
  state.set({connectionFailures: 0, connectionState: 'online'});
}

async function retrySelectedTask() {
  const current = state.snapshot;
  const taskId = current.selectedTaskId;
  const detail = current.taskDetail;
  if (!taskId || !detail) return;
  if (detail.status !== 'FAILED' && detail.status !== 'WORKER_LOST') return;
  const intent = current.retryIntent;
  if (intent?.taskId === taskId && intent.phase === 'submitting') return;
  const key = intent?.taskId === taskId ? intent.key : crypto.randomUUID();
  state.set({retryIntent: {taskId, key, phase: 'submitting'}});
  try {
    const updated = await api.retryTask(taskId, key);
    state.set({retryIntent: null, message: '任務已重新提交。'});
    mergeTasks([updated]);
    await selectTask(taskId);
  } catch (error) {
    const uncertain = error instanceof NetworkError || error instanceof TimeoutError;
    if (uncertain) {
      state.set({retryIntent: {taskId, key, phase: 'uncertain'}, message: '重試結果尚未確認。可再次重試，同一個提交識別會被沿用。'});
    } else {
      state.set({retryIntent: null, message: safeMessage(error, '目前無法重試此任務。')});
    }
  }
}

// A review message belongs to the action context. selectTask clears it so it can never
// be read as belonging to a newly selected task.
function clearReviewNotice() {
  return {reviewMessage: '', reviewMessageError: false};
}

async function settleReview(taskId, notice) {
  // Durable state is the authority. A late response for a task the user has already
  // left refreshes nothing and claims no selection.
  if (state.snapshot.selectedTaskId !== taskId) return;
  await selectTask(taskId);
  state.set(notice);
}

async function approveSelectedTask({reuse = false} = {}) {
  const current = state.snapshot;
  const target = reviewTarget();
  if (!target) return;
  const {taskId, contentVersionId} = target;
  const intent = matchedIntent(current.approveIntent, current);
  if (intent?.phase === 'submitting') return;
  if (reuse && intent?.phase !== 'uncertain') return;
  if (!reuse && reviewBusy(current)) return;
  // One key per logical approval. Reuse only across an uncertain transport result for
  // this exact task + version; a new version always mints a new key.
  const key = intent?.phase === 'uncertain' ? intent.key : crypto.randomUUID();
  state.set({approveIntent: {taskId, contentVersionId, key, phase: 'submitting'}, ...clearReviewNotice()});
  try {
    const updated = await api.approveTask(taskId, contentVersionId, key);
    state.set({approveIntent: null, message: '已送出核准。'});
    mergeTasks([updated]);
    await settleReview(taskId, {reviewMessage: '已送出核准。', reviewMessageError: false});
  } catch (error) {
    if (error instanceof NetworkError || error instanceof TimeoutError) {
      // The request may or may not have been applied. Keep the key so a retry is the
      // same logical approval rather than a second one.
      state.set({approveIntent: {taskId, contentVersionId, key, phase: 'uncertain'}});
      return;
    }
    state.set({approveIntent: null, message: safeMessage(error, '目前無法核准此任務。')});
    await settleReview(taskId, {reviewMessage: safeMessage(error, '目前無法核准此任務。'), reviewMessageError: true});
  }
}

function openRevisionFields() {
  const current = state.snapshot;
  if (!reviewTarget() || reviewBusy(current)) return;
  if (matchedIntent(current.revisionIntent, current)?.phase === 'uncertain') return;
  state.set({revisionOpen: true, ...clearReviewNotice()});
  elements['revision-feedback'].focus();
}

function cancelRevision() {
  const current = state.snapshot;
  if (matchedIntent(current.revisionIntent, current)?.phase === 'submitting') return;
  elements['revision-feedback'].value = '';
  state.set({revisionIntent: null, revisionOpen: false, ...clearReviewNotice()});
}

async function submitRevision({reuse = false} = {}) {
  const current = state.snapshot;
  const target = reviewTarget();
  if (!target) return;
  const {taskId, contentVersionId} = target;
  const intent = matchedIntent(current.revisionIntent, current);
  if (intent?.phase === 'submitting') return;
  if (reuse && intent?.phase !== 'uncertain') return;
  if (!reuse && reviewBusy(current)) return;
  // A retry must resend the frozen payload from the intent, never the textarea: one
  // idempotency key may only ever refer to one payload.
  const feedback = reuse ? intent.feedback : elements['revision-feedback'].value.trim();
  if (!feedback) {
    state.set({reviewMessage: '請先填寫修改方向再送出。', reviewMessageError: true});
    elements['revision-feedback'].focus();
    return;
  }
  const key = intent?.phase === 'uncertain' ? intent.key : crypto.randomUUID();
  state.set({revisionIntent: {taskId, contentVersionId, key, feedback, phase: 'submitting'}, ...clearReviewNotice()});
  try {
    const updated = await api.requestRevision(taskId, contentVersionId, feedback, key);
    state.set({revisionIntent: null, revisionOpen: false, message: '已送出修改要求。'});
    elements['revision-feedback'].value = '';
    mergeTasks([updated]);
    await settleReview(taskId, {reviewMessage: '已送出修改要求。', reviewMessageError: false});
  } catch (error) {
    if (error instanceof NetworkError || error instanceof TimeoutError) {
      state.set({revisionIntent: {taskId, contentVersionId, key, feedback, phase: 'uncertain'}});
      return;
    }
    state.set({revisionIntent: null, message: safeMessage(error, '目前無法提出修改要求。')});
    await settleReview(taskId, {reviewMessage: safeMessage(error, '目前無法提出修改要求。'), reviewMessageError: true});
  }
}

function resubmitReview() {
  const current = state.snapshot;
  if (reviewBusy(current)) return;
  if (matchedIntent(current.approveIntent, current)?.phase === 'uncertain') {
    approveSelectedTask({reuse: true});
    return;
  }
  if (matchedIntent(current.revisionIntent, current)?.phase === 'uncertain') {
    submitRevision({reuse: true});
  }
}

function cancelReview() {
  if (reviewBusy(state.snapshot)) return;
  elements['revision-feedback'].value = '';
  state.set({approveIntent: null, revisionIntent: null, revisionOpen: false, reviewMessage: '已取消未確認的送出。', reviewMessageError: false});
}

function clearPublicationNotice() {
  return {publicationMessage: '', publicationMessageError: false};
}

// A durable publication_id is the identity from this point on. publishIntent only ever
// named the request we sent, and is cleared the moment a response confirms it.
function mergePublication(publication) {
  if (!publication?.publication_id) return;
  // A late response for a task the operator already left must never repaint the task
  // they are looking at now. The view carries its own task_id, so the check is exact.
  if (publication.task_id && publication.task_id !== state.snapshot.selectedTaskId) return;
  const current = state.snapshot.publications;
  const index = current.findIndex((item) => item?.publication_id === publication.publication_id);
  if (index === -1) {
    state.set({publications: [...current, publication]});
    return;
  }
  const next = [...current];
  next[index] = publication;
  state.set({publications: next});
}

function clearReconcileIntent(publicationId) {
  const current = state.snapshot.reconcileIntents;
  if (!(publicationId in current)) return;
  const next = {...current};
  delete next[publicationId];
  state.set({reconcileIntents: next});
}

// The publication read is the authority. A late response for a task the operator has
// already left claims no selection and repaints nothing.
async function settlePublications(taskId, notice) {
  if (state.snapshot.selectedTaskId !== taskId) return;
  await loadPublications({taskId});
  state.set(notice);
}

async function publishSelectedTask({reuse = false} = {}) {
  const current = state.snapshot;
  const target = publishTarget(current);
  if (!target) return;
  const {taskId, contentVersionId} = target;
  const intent = matchedPublishIntent(current);
  if (intent?.phase === 'submitting') return;
  if (reuse && intent?.phase !== 'uncertain') return;
  if (!reuse && publishedForVersion(current.publications, contentVersionId)) return;
  // One key per logical publish, reused only across an uncertain transport result for
  // this exact task + version. A revised or re-published version always mints a new key.
  const key = intent?.phase === 'uncertain' ? intent.key : crypto.randomUUID();
  state.set({publishIntent: {taskId, contentVersionId, key, phase: 'submitting'}, ...clearPublicationNotice()});
  try {
    const publication = await api.publishTask(taskId, contentVersionId, key);
    state.set({publishIntent: null, message: '已送出發布要求。'});
    mergePublication(publication);
    await settlePublications(taskId, {publicationMessage: '已送出發布要求。', publicationMessageError: false});
  } catch (error) {
    if (error instanceof NetworkError || error instanceof TimeoutError) {
      // The durable row may already exist, so this is not a failure and must never be
      // shown as one. The key is kept so a retry is the same logical publish, not a
      // second one that would collide with the backend's publication lineage.
      state.set({publishIntent: {taskId, contentVersionId, key, phase: 'uncertain'}});
      return;
    }
    // A definitive server answer. No key is offered again: a retry would either replay
    // the same durable intent or collide with the lineage, and the read is the truth.
    const copy = safeMessage(error, '目前無法送出發布要求。');
    const informational = error instanceof ApiError && error.code === 'PUBLICATION_ALREADY_EXISTS';
    state.set({publishIntent: null, message: copy});
    await settlePublications(taskId, {publicationMessage: copy, publicationMessageError: !informational});
  }
}

async function requestReconciliation(publicationId) {
  const current = state.snapshot;
  const taskId = current.selectedTaskId;
  if (!taskId || typeof publicationId !== 'string' || !publicationId) return;
  // One in-flight reconciliation per publication, and never for a state that the
  // durable contract does not consider reconcilable.
  if (current.reconcileIntents[publicationId]?.taskId === taskId) return;
  const publication = current.publications.find((item) => item?.publication_id === publicationId);
  if (!publicationPresentation(publication).reconcilable) return;
  state.set({reconcileIntents: {...current.reconcileIntents, [publicationId]: {taskId, publicationId, phase: 'submitting'}}});
  try {
    const result = await api.requestReconciliation(taskId, publicationId);
    clearReconcileIntent(publicationId);
    // The returned view is the server's own projection, so CHECK_REQUESTED vs CHECKING
    // is never forced here; the refresh below re-derives it from the durable row.
    mergePublication(result?.publication);
    await settlePublications(taskId, {publicationMessage: '已送出重新確認要求，發布狀態已重新載入。', publicationMessageError: false});
  } catch (error) {
    // No Idempotency-Key is involved and the call is durable-state idempotent, so an
    // uncertain transport is resolved by reading, never by repeating the request.
    const uncertain = error instanceof NetworkError || error instanceof TimeoutError;
    const copy = uncertain
      ? '無法確認重新確認要求是否已送出，已重新載入發布狀態。'
      : safeMessage(error, '目前無法重新確認發布結果，已重新載入發布狀態。');
    clearReconcileIntent(publicationId);
    await settlePublications(taskId, {publicationMessage: copy, publicationMessageError: true});
  }
}

function mergeTasks(incoming, {append = false, nextCursor = state.snapshot.nextCursor} = {}) {
  const existing = state.snapshot.tasks;
  const combined = append ? [...existing, ...incoming] : [...incoming, ...existing];
  const seen = new Set();
  const tasks = combined.filter((task) => task?.task_id && !seen.has(task.task_id) && seen.add(task.task_id));
  state.set({tasks, nextCursor});
}

async function loadTasks({append = false} = {}) {
  if (listInFlight) return;
  listInFlight = true;
  const cursor = append ? state.snapshot.nextCursor : null;
  state.set({tasksLoading: true, taskListError: null});
  try {
    const result = await api.listTasks({limit: TASK_LIMIT, cursor});
    mergeTasks(Array.isArray(result.tasks) ? result.tasks : [], {append, nextCursor: result.next_cursor ?? null});
    state.set({tasksLoading: false});
  } catch (error) {
    state.set({tasksLoading: false, taskListError: safeMessage(error), message: '任務列表暫時無法同步。'});
  } finally {
    listInFlight = false;
  }
}

async function selectTask(taskId) {
  if (!taskId) return;
  const generation = ++detailGeneration;
  ++eventGeneration;
  const publicationGen = ++publicationGeneration;
  state.set({
    selectedTaskId: taskId, taskDetail: null, events: [], lastSequence: 0, eventsLoading: true,
    // Clear the previous task's publications the instant the selection changes, and drop
    // the transient in-flight flags with them. publishIntent deliberately survives: an
    // uncertain publish must still be retryable when the operator comes back here.
    publications: [], publicationsLoading: false, publicationsError: null, reconcileIntents: {},
    ...clearReviewNotice(), ...clearPublicationNotice(),
  });
  const detailPromise = api.getTask(taskId).then((detail) => {
    if (generation === detailGeneration && state.snapshot.selectedTaskId === taskId) state.set({taskDetail: detail});
  }).catch(() => {
    if (generation === detailGeneration) state.set({message: '任務內容暫時無法載入。'});
  });
  const eventPromise = loadEvents({taskId, generation: eventGeneration, afterSequence: 0});
  const publicationPromise = loadPublications({taskId, generation: publicationGen});
  await Promise.allSettled([detailPromise, eventPromise, publicationPromise]);
}

async function loadEvents({taskId = state.snapshot.selectedTaskId, generation = eventGeneration, afterSequence = state.snapshot.lastSequence} = {}) {
  if (!taskId || eventInFlightGeneration === generation) return;
  eventInFlightGeneration = generation;
  state.set({eventsLoading: true});
  try {
    const result = await api.getTaskEvents(taskId, afterSequence);
    if (generation !== eventGeneration || state.snapshot.selectedTaskId !== taskId) return;
    const byId = new Map(state.snapshot.events.map((event) => [event.event_id || `${event.sequence_number}`, event]));
    for (const event of result.events || []) byId.set(event.event_id || `${event.sequence_number}`, event);
    const events = [...byId.values()].sort((a, b) => a.sequence_number - b.sequence_number);
    state.set({events, lastSequence: Number.isInteger(result.last_sequence) ? result.last_sequence : afterSequence, eventsLoading: false});
  } catch (error) {
    if (generation === eventGeneration) state.set({eventsLoading: false, message: '事件紀錄暫時無法同步。'});
  } finally {
    if (eventInFlightGeneration === generation) eventInFlightGeneration = null;
  }
}

// Publication read. Mirrors the events guard: one in-flight request per generation, and
// a response for a task that is no longer selected is discarded rather than painted.
async function loadPublications({taskId = state.snapshot.selectedTaskId, generation = publicationGeneration} = {}) {
  if (!taskId || publicationInFlightGeneration === generation) return;
  publicationInFlightGeneration = generation;
  state.set({publicationsLoading: true});
  try {
    const result = await api.getPublications(taskId);
    if (generation !== publicationGeneration || state.snapshot.selectedTaskId !== taskId) return;
    state.set({
      publications: Array.isArray(result.publications) ? result.publications : [],
      publicationsLoading: false,
      publicationsError: null,
    });
  } catch (error) {
    if (generation !== publicationGeneration) return;
    state.set({publicationsLoading: false, publicationsError: safeMessage(error, '發布紀錄暫時無法同步。')});
  } finally {
    if (publicationInFlightGeneration === generation) publicationInFlightGeneration = null;
  }
}

async function loadBootstrap() {
  try {
    const bootstrap = await api.bootstrap();
    const limits = bootstrap.limits || {};
    elements.topic.minLength = limits.topic_min || 3;
    elements.topic.maxLength = limits.topic_max || 150;
    elements.brief.minLength = limits.brief_min || 20;
    elements.brief.maxLength = limits.brief_max || 5000;
    const placeholder = element('option', '', '請選擇');
    placeholder.value = '';
    replaceChildren(elements['page-purpose'], [placeholder, ...(bootstrap.page_purposes || []).map((purpose) => {
      const option = element('option', '', purpose);
      option.value = purpose;
      return option;
    })]);
    state.set({bootstrap, loading: false, message: '工作區設定已載入。'});
  } catch (error) {
    state.set({loading: false, error, message: safeMessage(error, '工作區設定無法載入。')});
  }
}

async function loadStatus() {
  try {
    state.set({status: await api.getStatus()});
    markConnectionSuccess();
  } catch (error) {
    markConnectionFailure();
    state.set({message: '系統狀態暫時無法同步。'});
  }
}

function resetComposer() {
  state.set({
    draft: {content_type: 'POST', topic: '', brief: '', target_audience: '', page_purpose: ''},
    submission: {phase: 'idle', key: null, body: null, task: null, message: ''},
  });
}

form.addEventListener('input', (event) => {
  if (!event.target.name) return;
  setDraft({[event.target.name]: event.target.value});
});
form.addEventListener('change', (event) => {
  if (event.target.name === 'content_type') {
    setDraft({content_type: event.target.value, target_audience: '', page_purpose: ''});
  }
});
form.addEventListener('submit', (event) => {
  event.preventDefault();
  submitTask();
});
elements['resubmit-task'].addEventListener('click', () => submitTask({reuse: true}));
elements['cancel-submission'].addEventListener('click', resetComposer);
elements['new-task-button'].addEventListener('click', resetComposer);
elements['retry-task'].addEventListener('click', retrySelectedTask);
elements['retry-cancel'].addEventListener('click', () => {
  state.set({retryIntent: null, message: '已取消不確定的重試。'});
});
elements['load-more'].addEventListener('click', () => loadTasks({append: true}));
elements['approve-task'].addEventListener('click', () => approveSelectedTask());
elements['request-revision'].addEventListener('click', openRevisionFields);
elements['submit-revision'].addEventListener('click', () => submitRevision());
elements['cancel-revision'].addEventListener('click', cancelRevision);
elements['resubmit-review'].addEventListener('click', resubmitReview);
elements['cancel-review'].addEventListener('click', cancelReview);
elements['publish-task'].addEventListener('click', () => publishSelectedTask());
elements['resubmit-publish'].addEventListener('click', () => publishSelectedTask({reuse: true}));
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => setPanel(panelFor(tab)));
  tab.addEventListener('keydown', (event) => {
    let nextIndex = null;
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = tabs.length - 1;
    if (nextIndex !== null) {
      event.preventDefault();
      tabs[nextIndex].focus();
      setPanel(panelFor(tabs[nextIndex]));
    }
  });
});
panelButtons.forEach((button) => button.addEventListener('click', () => setPanel(panelFor(button))));
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    loadTasks();
    loadEvents();
    loadPublications();
    loadStatus();
  }
});

state.subscribe(render);
render(state.snapshot);
Promise.allSettled([loadBootstrap(), loadTasks(), loadStatus()]);
setInterval(() => {
  if (document.visibilityState !== 'visible') return;
  loadTasks();
  loadEvents();
  loadPublications();
  loadStatus();
}, POLL_INTERVAL_MS);
