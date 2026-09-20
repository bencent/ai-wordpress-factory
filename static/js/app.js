import {createApiClient, NetworkError, TimeoutError} from './api.js';
import {createState} from './state.js';

const api = createApiClient();
const state = createState();
const shell = document.querySelector('[data-active-panel]');
const tabs = [...document.querySelectorAll('[data-panel][role="tab"]')];
const panelButtons = [...document.querySelectorAll('[data-panel]:not([role="tab"])')];
const elements = Object.fromEntries([...document.querySelectorAll('[id]')].map((element) => [element.id, element]));

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

function render(next) {
  const connection = elements['connection-label'];
  const message = elements['system-message'];
  const loading = elements['timeline-loading'];
  const empty = elements['timeline-empty'];
  const date = elements['timeline-date'];
  const status = elements['current-task-status'];

  connection.textContent = next.loading ? '載入中' : next.error ? '連線異常' : '已載入';
  connection.closest('.connection')?.classList.toggle('is-error', Boolean(next.error));
  connection.closest('.connection')?.classList.toggle('is-ready', !next.loading && !next.error);
  message.textContent = next.error ? next.error.message : next.message;
  message.classList.toggle('is-error', Boolean(next.error));
  loading.hidden = !next.loading;
  empty.hidden = next.loading || Boolean(next.bootstrap);
  date.textContent = next.loading ? '載入中' : '目前沒有任務';
  status.textContent = next.loading ? '等待載入' : '尚未選擇任務';

  if (next.status) {
    elements['health-api'].textContent = next.status.api || '未知';
    elements['health-database'].textContent = next.status.database || '未知';
    elements['health-worker'].textContent = next.status.worker?.status || '未知';
  }
}

function reportError(error) {
  const normalized = error instanceof NetworkError || error instanceof TimeoutError || error?.code
    ? error
    : new NetworkError('無法完成系統載入。', {cause: error});
  state.set({loading: false, error: normalized, message: normalized.message});
}

async function loadBootstrap() {
  try {
    const bootstrap = await api.bootstrap();
    state.set({bootstrap, loading: false, message: '工作區設定已載入。'});
  } catch (error) {
    reportError(error);
  }
}

async function loadStatus() {
  try {
    const status = await api.getStatus();
    state.set({status, message: state.snapshot.error ? state.snapshot.message : '系統狀態已載入。'});
  } catch (error) {
    reportError(error);
  }
}

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
state.subscribe(render);
render(state.snapshot);
Promise.allSettled([loadBootstrap(), loadStatus()]);
