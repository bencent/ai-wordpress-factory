export function createState(initial = {}) {
  let state = {
    bootstrap: null,
    status: null,
    activePanel: 'tasks',
    loading: true,
    message: '正在載入',
    error: null,
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
