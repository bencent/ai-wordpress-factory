export function createState(initial = {}) {
  let state = {
    bootstrap: null,
    status: null,
    activePanel: 'tasks',
    loading: true,
    message: '正在載入',
    error: null,
    draft: {content_type: 'POST', topic: '', brief: '', target_audience: '', page_purpose: ''},
    submission: {phase: 'idle', key: null, body: null, task: null, message: ''},
    tasks: [],
    tasksLoading: true,
    taskListError: null,
    nextCursor: null,
    selectedTaskId: null,
    taskDetail: null,
    events: [],
    lastSequence: 0,
    eventsLoading: false,
    ...initial,
  };
  const listeners = new Set();

  return Object.freeze({
    get snapshot() {
      return {...state};
    },
    set(patch) {
      state = {...state, ...patch};
      const snapshot = this.snapshot;
      listeners.forEach((listener) => listener(snapshot));
    },
    subscribe(listener) {
      if (typeof listener !== 'function') throw new TypeError('State listener must be a function.');
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  });
}
