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
}

function safeMessage(error, fallback = '目前無法完成操作，請稍後再試。') {
  if (error instanceof TimeoutError || error instanceof NetworkError) return error.message;
  if (error instanceof ApiError && error.code === 'IDEMPOTENCY_CONFLICT') return '提交識別與先前需求不一致，請取消後重新建立。';
  if (error instanceof ApiError && error.code === 'VALIDATION_ERROR') return '請檢查欄位內容後再送出。';
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
  state.set({selectedTaskId: taskId, taskDetail: null, events: [], lastSequence: 0, eventsLoading: true});
  const detailPromise = api.getTask(taskId).then((detail) => {
    if (generation === detailGeneration && state.snapshot.selectedTaskId === taskId) state.set({taskDetail: detail});
  }).catch(() => {
    if (generation === detailGeneration) state.set({message: '任務內容暫時無法載入。'});
  });
  const eventPromise = loadEvents({taskId, generation: eventGeneration, afterSequence: 0});
  await Promise.allSettled([detailPromise, eventPromise]);
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
  loadStatus();
}, POLL_INTERVAL_MS);
