import { ArrowRight, ArrowUp, Check, ChevronDown, CircleHelp, ClipboardCopy, FileSearch, GitBranch, Inbox, LifeBuoy, LoaderCircle, MessageSquare, Send, TriangleAlert } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
import { api } from './api'
import type { ContextMode, Dialogue, History, Mode, PublicAppeal, Turn, Usage } from './api'
import { delivery, errorLabels, errorText, Notice, SourceCard, statuses, statusText } from './ui'

const examples = [
  { icon: FileSearch, title: 'Найти порядок действий', text: 'Кто принимает опытный макет?', live: 'Какой порядок действий описан для моего этапа проекта?' },
  { icon: GitBranch, title: 'Уточнить ответственность', text: 'Кто подписывает акт испытаний макета?', live: 'Кто отвечает за согласование результатов проекта?' },
  { icon: LifeBuoy, title: 'Проверить, есть ли правило', text: 'Как оформить компенсацию проезда до аэропорта?', live: 'Как оформить компенсацию проезда до аэропорта?' },
]

type ChatProps = {
  current: Dialogue | null
  mode: Mode | null
  onCreate: () => Promise<Dialogue>
  onTitle: (id: string, title: string) => void
  onUsage: (id: string, usage: Usage) => void
  appeals: Record<string, PublicAppeal>
  onAppeal: (appeal: PublicAppeal, dialogueId: string) => void
  visible: boolean
}

export default function Chat({ current, mode, onCreate, onTitle, onUsage, appeals, onAppeal, visible }: ChatProps) {
  const [histories, setHistories] = useState<Record<string, History>>({})
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [contexts, setContexts] = useState<Record<string, ContextMode>>({})
  const [readError, setReadError] = useState('')
  const [sendError, setSendError] = useState<{ id: string; message: string; submitted: string } | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [sending, setSending] = useState<{ id: string; text: string } | null>(null)
  const lock = useRef(false)
  const scroll = useRef<HTMLDivElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const preserveScroll = useRef<number | null>(null)
  const id = current?.dialogue_id ?? ''
  const token = current?.access_token
  const history = histories[id]
  const turns = history?.messages ?? []
  const pending = sending?.id === id ? sending : null
  const draft = drafts[id] ?? ''
  const context = contexts[id] ?? 'auto'
  const last = turns.at(-1)
  const canSend = !!draft.trim() && !sending && (!id || !!history) && mode !== 'disabled'

  useEffect(() => {
    setReadError('')
    if (!id || !token || !visible) return
    const controller = new AbortController()
    const dialogue = { dialogue_id: id, access_token: token, title: '' }
    api.history(dialogue, undefined, controller.signal).then((data) => {
      if (controller.signal.aborted) return
      setHistories((old) => {
        const cached = old[id]
        // A concurrent send can complete after the history request was issued.
        const latest = cached?.messages.at(-1)?.id
        return latest && latest > (data.messages.at(-1)?.id ?? 0) ? old : { ...old, [id]: data }
      })
      onUsage(id, data.usage)
    }).catch((error: unknown) => { if (!controller.signal.aborted) setReadError(errorText(error)) })
    return () => controller.abort()
  }, [id, token, refresh, visible, onUsage])

  useLayoutEffect(() => {
    if (!visible || !scroll.current) return
    if (preserveScroll.current !== null) {
      scroll.current.scrollTop += scroll.current.scrollHeight - preserveScroll.current
      preserveScroll.current = null
    } else scroll.current.scrollTop = scroll.current.scrollHeight
  }, [id, last?.id, pending, turns.length, visible])

  function changeDraft(text: string) {
    setDrafts((old) => ({ ...old, [id]: text }))
  }

  async function submit(text = draft) {
    const message = text.trim()
    if (!message || lock.current || (id && !history) || mode === 'disabled') return
    lock.current = true
    let target = current
    setSending({ id, text: message })
    setSendError(null)
    try {
      target = target ?? await onCreate()
      const targetId = target.dialogue_id
      setSending({ id: targetId, text: message })
      setDrafts((old) => ({ ...old, [id]: '', [targetId]: '' }))
      if (!turns.length) onTitle(targetId, message)
      const answer = await api.message(target, message, context)
      setHistories((old) => {
        const previous = old[targetId]
        return { ...old, [targetId]: {
          messages: [...(previous?.messages ?? []).filter((turn) => turn.id !== answer.turn_id),
            { id: answer.turn_id, message, result: answer.result, sources: answer.sources, error: null }],
          next_before: previous?.next_before ?? null, usage: answer.dialogue_usage,
        } }
      })
      onUsage(targetId, answer.dialogue_usage)
      setContexts((old) => ({ ...old, [targetId]: 'auto' }))
    } catch (error) {
      const targetId = target?.dialogue_id ?? id
      setSendError({ id: targetId, message: errorText(error), submitted: message })
      setDrafts((old) => ({ ...old, [targetId]: old[targetId] || message }))
      if (target) setRefresh((value) => value + 1)
    } finally {
      lock.current = false
      setSending(null)
      textarea.current?.focus()
    }
  }

  async function older() {
    const cursor = history?.next_before
    if (!current || !cursor || loadingMore) return
    setLoadingMore(true)
    try {
      const data = await api.history(current, cursor)
      if (scroll.current?.dataset.dialogue === id) preserveScroll.current = scroll.current.scrollHeight
      setHistories((old) => {
        const currentHistory = old[id]
        if (!currentHistory) return old
        const known = new Set(currentHistory.messages.map((turn) => turn.id))
        return { ...old, [id]: {
          ...currentHistory, next_before: data.next_before,
          messages: [...data.messages.filter((turn) => !known.has(turn.id)), ...currentHistory.messages],
        } }
      })
    } catch (error) { setReadError(errorText(error)) }
    finally { setLoadingMore(false) }
  }

  function keyboard(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      if (canSend) void submit()
    }
  }

  return <section className="chat" hidden={!visible} aria-label="ИИ-ассистент">
    <div className={'chat-scroll' + (!turns.length && !pending ? ' empty' : '')} ref={scroll} data-dialogue={id}>
      {mode === 'disabled' && <div className="chat-width top-notice"><Notice>Поиск не подключён. Отправка сообщений недоступна.</Notice></div>}
      {id && !history && !readError && !pending && <div className="chat-width top-notice loading" role="status"><LoaderCircle size={17} /> Загрузка истории…</div>}
      {readError && <div className="chat-width top-notice"><Notice tone="error">{readError} <button className="text-button" onClick={() => setRefresh((value) => value + 1)}>Повторить</button></Notice></div>}
      {!turns.length && !pending && (!id || (history && !readError)) &&
        <div className="welcome chat-width">
          <span className="welcome-icon"><MessageSquare size={27} strokeWidth={1.65} /></span>
          <h1>Вопросы по регламентам</h1>
          <p>Спросите о рабочих процедурах и получите ответ со ссылками на конкретные пункты.</p>
          <div className="examples" aria-label="Примеры вопросов">
            {examples.map(({ icon: Icon, title, text, live }) => <button key={title} onClick={() => {
              changeDraft(mode === 'demo' ? text : live)
              textarea.current?.focus()
            }}>
              <Icon size={18} /><span>{title}</span><ArrowRight size={15} className="example-arrow" />
            </button>)}
          </div>
        </div>}
      {(!!turns.length || pending) && <div className="chat-width messages" aria-label="История переписки">
        {history?.next_before && <button className="button load-more" disabled={loadingMore} onClick={() => void older()}>Загрузить ранние сообщения</button>}
        {current && turns.map((turn) => <Message key={turn.id} turn={turn} dialogue={current}
          appeal={appeals[id + ':' + turn.id]} onAppeal={onAppeal}
          choose={(text) => void submit(text)} busy={!!sending} />)}
        {pending && <><div className="user-message">{pending.text}</div><div className="thinking" role="status"><LoaderCircle size={17} /> Ищу ответ в источниках…</div></>}
      </div>}
    </div>
    <div className="composer-zone">
      <div className="chat-width">
        {sendError?.id === id && !(last?.error && last.message === sendError.submitted) && <div className="send-error"><Notice tone="error">{sendError.message}</Notice></div>}
        <form className="composer" onSubmit={(event: FormEvent) => { event.preventDefault(); if (canSend) void submit() }}>
          <label className="sr-only" htmlFor="message">Сообщение</label>
          <textarea ref={textarea} id="message" rows={2} maxLength={2000} value={draft}
            placeholder={last?.result?.result_type === 'CLARIFY' ? 'Напишите уточнение…' : 'Задайте вопрос по регламентам…'}
            onChange={(event) => changeDraft(event.target.value)} onKeyDown={keyboard} />
          <div className="composer-actions">
            <label className="context-select"><span className="sr-only">Контекст сообщения</span>
              <select value={context} onChange={(event) => setContexts((old) => ({ ...old, [id]: event.target.value as ContextMode }))}>
                <option value="auto">Автоконтекст</option><option value="new">Новая тема</option><option value="follow_up">Продолжить тему</option>
              </select><ChevronDown size={13} aria-hidden="true" />
            </label>
            <div className="send-controls"><span className="counter">{draft.length} / 2000</span>
              <button className="send-button" type="submit" disabled={!canSend} aria-label="Отправить сообщение">
                {pending ? <LoaderCircle className="spin" size={17} /> : <ArrowUp size={20} />}
              </button></div>
          </div>
        </form>
        <div className="composer-hint"><span>Ответы основаны на доступных источниках</span><span>Enter — отправить · Shift+Enter — перенос</span></div>
      </div>
    </div>
  </section>
}

const presentation = {
  ANSWER: { label: 'Ответ по источникам', icon: Check },
  CLARIFY: { label: 'Нужно уточнение', icon: CircleHelp },
  NO_ANSWER: { label: 'Недостаточно информации', icon: Inbox },
  CONFLICT: { label: 'Противоречие источников', icon: TriangleAlert },
}

function Message({ turn, dialogue, appeal, onAppeal, choose, busy }: {
  turn: Turn; dialogue: Dialogue; appeal?: PublicAppeal; onAppeal: ChatProps['onAppeal']; choose: (text: string) => void; busy: boolean
}) {
  const [action, setAction] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const result = turn.result
  const eligible = result?.result_type === 'NO_ANSWER' || result?.result_type === 'CONFLICT'
  const meta = result && presentation[result.result_type]
  const Icon = meta?.icon ?? CircleHelp
  const text = result?.result_type === 'CLARIFY' ? result.clarification_question : result?.text
  const renderedBySources = result?.result_type === 'ANSWER' && turn.sources.length
    && (text?.startsWith(turn.sources[0].document) || text?.startsWith('Разъяснение службы качества '))
  const shownText = renderedBySources ? null
    : result?.result_type === 'CONFLICT' && turn.sources.length ? text?.split('\n\n')[0] : text

  useEffect(() => {
    if (!appeal) return
    let cancelled = false
    const id = appeal.id
    const poll = () => api.appealStatus(dialogue, id).then((fresh) => {
      if (!cancelled) onAppeal(fresh, dialogue.dialogue_id)
    }).catch(() => undefined)
    void poll()
    const timer = window.setInterval(poll, 8000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [appeal?.id, dialogue, onAppeal]) // The ID is the polling key.

  async function createAppeal() {
    setAction(true); setError('')
    try { onAppeal(await api.appeal(dialogue, turn.id), dialogue.dialogue_id) }
    catch (failure) { setError(errorText(failure)) }
    finally { setAction(false) }
  }

  async function copy() {
    setError('')
    try { await navigator.clipboard.writeText(text ?? ''); setCopied(true) }
    catch { setError('Не удалось скопировать ответ. Вы можете выделить текст вручную.') }
  }

  return <div className="message-pair">
    <div className="user-message">{turn.message}</div>
    {turn.error && <Notice tone="error">{errorLabels[turn.error] ?? 'Запрос завершился технической ошибкой.'}</Notice>}
    {!turn.error && result && <article className={'agent-message ' + result.result_type.toLowerCase()}>
      <div className="message-heading"><span className="message-type"><Icon size={17} />{meta?.label}</span>
        {text && <button className="icon-button" onClick={() => void copy()} title={copied ? 'Скопировано' : 'Копировать ответ'}
          aria-label={copied ? 'Скопировано' : 'Копировать ответ'}>{copied ? <Check size={16} /> : <ClipboardCopy size={16} />}</button>}
      </div>
      {shownText && <p className="answer-text">{shownText}</p>}
      {result.result_type === 'CLARIFY' && !!result.clarification_options?.length &&
        <div className="button-row">{result.clarification_options.map((option) => <button key={option} className="button" disabled={busy} onClick={() => choose(option)}>{option}</button>)}</div>}
      {!!turn.sources.length && <div className="message-sources">{turn.sources.map((source) => <SourceCard key={source.source_id} source={source} />)}</div>}
      {eligible && (appeal ? <div className="appeal-receipt">
        <strong>Обращение {appeal.id}</strong><span>{statusText(statuses, appeal.status)} · {statusText(delivery, appeal.notification.status)}</span>
      </div> : <button className="button appeal-button" disabled={action} onClick={() => void createAppeal()}>
        {action ? <LoaderCircle className="spin" size={16} /> : <Send size={16} />} Создать обращение
      </button>)}
      {error && <Notice tone="error">{error}</Notice>}
    </article>}
  </div>
}
