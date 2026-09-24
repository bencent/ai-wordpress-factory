const DEFAULT_TIMEOUT_MS = 8000;

export class ApiError extends Error {
  constructor(message, {code = 'API_ERROR', status = null, requestId = null, cause = null} = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.requestId = requestId;
    this.cause = cause;
  }
}

export class NetworkError extends ApiError {
  constructor(message = '無法連線到 API，請稍後再試。', options = {}) {
    super(message, {code: 'NETWORK_ERROR', ...options});
    this.name = 'NetworkError';
  }
}

export class TimeoutError extends ApiError {
  constructor(message = 'API 回應時間過長，請稍後再試。', options = {}) {
    super(message, {code: 'TIMEOUT', ...options});
    this.name = 'TimeoutError';
  }
}

function assertSameOriginPath(path) {
  if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//')) {
    throw new ApiError('API 只能使用同源相對路徑。', {code: 'INVALID_URL'});
  }
  const base = globalThis.location;
  if (base?.origin) {
    const url = new URL(path, base.href);
    if (url.origin !== base.origin) throw new ApiError('API 只能使用同源相對路徑。', {code: 'INVALID_URL'});
  }
  return path;
}

function requireId(value, field) {
  if (typeof value !== 'string' || !value) throw new ApiError('缺少必要的識別碼。', {code: 'INVALID_REQUEST', field});
  return value;
}

function parsePayload(text, status) {
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new ApiError('API 回傳了無法讀取的內容。', {code: 'INVALID_RESPONSE', status, cause: error});
  }
}

function errorFromPayload(payload, status) {
  const detail = payload && typeof payload === 'object' ? payload.error : null;
  const code = typeof detail?.code === 'string' ? detail.code : 'API_ERROR';
  const message = typeof detail?.message === 'string' && detail.message ? detail.message : 'API 請求失敗。';
  const requestId = typeof detail?.request_id === 'string' ? detail.request_id : null;
  return new ApiError(message, {code, status, requestId});
}

export function createApiClient({fetchImpl = globalThis.fetch?.bind(globalThis), timeoutMs = DEFAULT_TIMEOUT_MS} = {}) {
  if (typeof fetchImpl !== 'function') throw new NetworkError('目前環境不提供 fetch。');
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new ApiError('API timeout 設定無效。', {code: 'INVALID_REQUEST'});

  async function request(path, options = {}) {
    const url = assertSameOriginPath(path);
    const controller = new AbortController();
    const timer = globalThis.setTimeout(() => controller.abort(), timeoutMs);
    const headers = options.body ? {'Content-Type': 'application/json', ...(options.headers || {})} : options.headers;
    try {
      const response = await fetchImpl(url, {...options, headers, signal: controller.signal});
      const payload = parsePayload(await response.text(), response.status);
      if (!response.ok) throw errorFromPayload(payload, response.status);
      return payload;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (error?.name === 'AbortError') throw new TimeoutError('API 回應時間過長，請稍後再試。', {cause: error});
      throw new NetworkError('無法連線到 API，請稍後再試。', {cause: error});
    } finally {
      globalThis.clearTimeout(timer);
    }
  }

  return Object.freeze({
    bootstrap: () => request('/api/v1/ui/bootstrap'),
    createTask: (body, idempotencyKey) => request('/api/v1/tasks', {method: 'POST', headers: {'Idempotency-Key': requireId(idempotencyKey, 'idempotency_key')}, body: JSON.stringify(body)}),
    listTasks: (params = {}) => {
      const query = new URLSearchParams();
      if (params.limit !== undefined) query.set('limit', String(params.limit));
      if (params.cursor !== undefined && params.cursor !== null) query.set('cursor', String(params.cursor));
      return request(`/api/v1/tasks${query.size ? `?${query}` : ''}`);
    },
    getTask: (taskId) => request(`/api/v1/tasks/${encodeURIComponent(requireId(taskId, 'task_id'))}`),
    getTaskEvents: (taskId, afterSequence = 0) => request(`/api/v1/tasks/${encodeURIComponent(requireId(taskId, 'task_id'))}/events?after_sequence=${encodeURIComponent(String(afterSequence))}`),
    retryTask: (taskId, idempotencyKey) => request(`/api/v1/tasks/${encodeURIComponent(requireId(taskId, 'task_id'))}/retry`, {
      method: 'POST',
      headers: {'Idempotency-Key': requireId(idempotencyKey, 'idempotency_key')},
      body: '{}',
    }),
    getStatus: () => request('/api/v1/system/status'),
  });
}
