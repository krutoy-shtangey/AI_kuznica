export type ContextMode = 'auto' | 'new' | 'follow_up'
export type ResultType = 'ANSWER' | 'CLARIFY' | 'NO_ANSWER' | 'CONFLICT'
export type Status = 'new' | 'in_work' | 'draft' | 'approved' | 'closed'
export type Mode = 'demo' | 'configured' | 'disabled'
export type Dialogue = { dialogue_id: string; access_token: string; title: string }
export type AgentResult = {
  result_type: ResultType
  text: string | null
  clarification_question: string | null
  clarification_options?: string[]
  source_ids: string[]
}
export type Source = {
  source_id: string
  source_type: 'regulation' | 'expert_answer'
  document: string
  section: string | null
  department: string | null
  stage: string | null
  excerpt: string
  excerpt_partial?: boolean
  status: 'approved' | 'unknown'
  source_url: string | null
  approved_at: string | null
  appeal_id: string | null
  revision: number | null
  conditions?: string | null
}
type Cost = { known_by_currency: Record<string, string>; unknown_attempts: number }
export type Usage = {
  attempts: number
  seconds: number
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  provider_cost: Cost
  alibaba_estimate: Cost
}
export type Turn = {
  id: number
  message: string
  result: AgentResult | null
  sources: Source[]
  error: string | null
}
export type History = { messages: Turn[]; next_before: number | null; usage: Usage }
export type MessageReply = { turn_id: number; result: AgentResult; sources: Source[]; dialogue_usage: Usage }
export type Notification = { status: string; attempts: number }
export type PublicAppeal = { id: string; turn_id: number; status: Status; created_at: string; notification: Notification }
export type Draft = { text: string; department: string | null; stage: string | null; conditions: string | null }
export type Publication = {
  revision: number
  action: 'upsert' | 'delete'
  payload: (Draft & { author: string }) | null
  approved_by: string
  approved_at: string
  state: 'pending' | 'running' | 'indexed' | 'failed'
  attempts: number
  published: boolean
}
export type Appeal = PublicAppeal & {
  original_question: string
  question: string
  clarifications: string[]
  considered_source_ids: string[]
  department: string | null
  stage: string | null
  reason: 'NO_ANSWER' | 'CONFLICT'
  facts: Record<string, string>
  updated_at: string
  version: number
  draft: (Draft & { author: string }) | null
  publication: Publication | null
}

export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string, message: string) { super(message) }
}

async function request<T>(path: string, options: {
  token?: string; method?: string; body?: unknown; signal?: AbortSignal
} = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (options.token) headers.Authorization = 'Bearer ' + options.token
  let response: Response
  try {
    response = await fetch('/api' + path, {
      headers, method: options.method, signal: options.signal, cache: 'no-store',
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    })
  } catch (error) {
    if (options.signal?.aborted) throw error
    throw new ApiError(0, 'offline', 'Не удалось связаться с сервером. Проверьте соединение и повторите.')
  }
  const data = await response.json().catch(() => null)
  if (!response.ok || !data) {
    throw new ApiError(response.status, data?.code ?? 'invalid_response',
      typeof data?.message === 'string' ? data.message : 'Сервер не вернул корректный ответ. Повторите позже.')
  }
  return data as T
}

const pathFor = (dialogue: Dialogue) => '/dialogues/' + encodeURIComponent(dialogue.dialogue_id)

export const api = {
  health: () => request<{ status: string; retrieval: Mode; model?: 'offline-fixture' | 'proxyapi-qwen' }>('/health'),
  knowledge: (signal: AbortSignal) => request<{ documents: Source[] }>('/knowledge', { signal }),
  create: () => request<Omit<Dialogue, 'title'>>('/dialogues', { method: 'POST' }),
  history: (dialogue: Dialogue, before?: number, signal?: AbortSignal) =>
    request<History>(pathFor(dialogue) + '/messages?limit=30' + (before ? '&before=' + before : ''),
      { token: dialogue.access_token, signal }),
  message: (d: Dialogue, message: string, context: ContextMode) =>
    request<MessageReply>(pathFor(d) + '/messages', { token: d.access_token, method: 'POST', body: { message, context } }),
  appeal: (d: Dialogue, turn_id: number) =>
    request<PublicAppeal>(pathFor(d) + '/appeals', { token: d.access_token, method: 'POST', body: { turn_id } }),
  appealStatus: (d: Dialogue, id: string) =>
    request<PublicAppeal>(pathFor(d) + '/appeals/' + encodeURIComponent(id), { token: d.access_token }),
  appeals: (token: string, status: Status | '', offset = 0, explanations = false, signal?: AbortSignal) =>
    request<{ items: Appeal[] }>(
      '/quality/' + (explanations ? 'explanations' : 'appeals') + '?limit=30&offset=' + offset
        + (status && !explanations ? '&status=' + status : ''), { token, signal }),
  detail: (token: string, id: string, signal?: AbortSignal) =>
    request<Appeal>('/quality/appeals/' + encodeURIComponent(id), { token, signal }),
  update: (token: string, appeal: Appeal, action: 'status' | 'draft' | 'approve' | 'revoke' | 'retry-index',
    fields: Partial<Draft> | { status: 'in_work' | 'closed' } = {}) =>
    request<Appeal>('/quality/appeals/' + encodeURIComponent(appeal.id) + '/' + action, {
      token, method: action === 'draft' ? 'PUT' : action === 'status' ? 'PATCH' : 'POST',
      body: { version: appeal.version, ...fields },
    }),
}
