import { BarChart3, BookOpen, ChevronRight, Database, Layers3, Menu, MessageSquare, PanelLeftClose, Plus, Search, ShieldCheck } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { Dialogue, Mode, PublicAppeal, Source, Usage } from './api'
import Chat from './Chat'
import Quality from './Quality'
import { errorText, Notice, SourceCard, UsageCards } from './ui'

const sections = [
  { id: 'chat', title: 'ИИ-ассистент', icon: MessageSquare },
  { id: 'knowledge', title: 'База знаний', icon: BookOpen },
  { id: 'quality', title: 'Служба качества', icon: ShieldCheck },
  { id: 'analytics', title: 'Аналитика', icon: BarChart3 },
] as const
type Page = typeof sections[number]['id']

const STORAGE = 'reglament-ai-dialogues-v1'
type Saved = { dialogues: Dialogue[]; active: string | null; appeals: Record<string, PublicAppeal> }

function saved(): Saved {
  const empty: Saved = { dialogues: [], active: null, appeals: {} }
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE) ?? 'null')
    if (!raw || !Array.isArray(raw.dialogues)) return empty
    const dialogues = (raw.dialogues as Dialogue[]).filter((d) =>
      d && typeof d.dialogue_id === 'string' && typeof d.access_token === 'string' && typeof d.title === 'string')
    return { dialogues, active: typeof raw.active === 'string' ? raw.active : null, appeals: raw.appeals ?? {} }
  } catch { return empty }
}

function currentPage(): Page {
  const hash = window.location.hash.slice(1)
  return sections.find((entry) => entry.id === hash)?.id ?? 'chat'
}

export default function App() {
  const [data, setData] = useState(saved)
  const [page, setPage] = useState<Page>(currentPage)
  const [mode, setMode] = useState<Mode | null>(null)
  const [healthError, setHealthError] = useState(false)
  const [drawer, setDrawer] = useState(false)
  const [secret, setSecret] = useState<string | null>(null)
  const [usage, setUsage] = useState<Record<string, Usage>>({})
  const [createError, setCreateError] = useState('')
  const [creating, setCreating] = useState(false)
  const [storageError, setStorageError] = useState(false)
  const creatingRef = useRef(false)
  const navToggle = useRef<HTMLButtonElement>(null)
  const closeToggle = useRef<HTMLButtonElement>(null)
  const sidebar = useRef<HTMLElement>(null)
  const current = data.dialogues.find((d) => d.dialogue_id === data.active) ?? null
  const activeSection = sections.find((entry) => entry.id === page)!

  useEffect(() => {
    const listener = () => { setPage(currentPage()); setDrawer(false) }
    window.addEventListener('hashchange', listener)
    return () => window.removeEventListener('hashchange', listener)
  }, [])

  useEffect(() => {
    api.health().then((result) => { setMode(result.retrieval); setHealthError(false) })
      .catch(() => setHealthError(true))
  }, [])

  useEffect(() => {
    try { localStorage.setItem(STORAGE, JSON.stringify(data)) }
    catch { setStorageError(true) }
  }, [data])

  useEffect(() => {
    if (!drawer) return
    closeToggle.current?.focus()
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setDrawer(false); navToggle.current?.focus() }
      if (event.key === 'Tab') {
        const links = sidebar.current?.querySelectorAll<HTMLElement>('a[href],button:not(:disabled)')
        if (!links?.length) return
        const first = links[0], last = links[links.length - 1]
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
      }
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [drawer])

  function navigate(next: Page) {
    if (currentPage() !== next) window.location.hash = next
    setPage(next)
    setDrawer(false)
  }

  async function create() {
    if (creatingRef.current) throw new Error('Диалог уже создаётся.')
    creatingRef.current = true
    setCreating(true); setCreateError('')
    try {
      const target = { ...await api.create(), title: 'Новый диалог' }
      setData((old) => ({ ...old, dialogues: [target, ...old.dialogues], active: target.dialogue_id }))
      navigate('chat')
      return target
    } finally {
      creatingRef.current = false
      setCreating(false)
    }
  }

  function select(id: string) {
    setData((old) => ({ ...old, active: id }))
    navigate('chat')
  }

  const title = useCallback((id: string, value: string) => setData((old) => ({
    ...old, dialogues: old.dialogues.map((d) => d.dialogue_id === id && d.title === 'Новый диалог'
      ? { ...d, title: value.replace(/\s+/g, ' ').slice(0, 85) } : d),
  })), [])
  const updateUsage = useCallback((id: string, value: Usage) => setUsage((old) => ({ ...old, [id]: value })), [])
  const addAppeal = useCallback((value: PublicAppeal, dialogueId: string) => setData((old) => {
    const key = dialogueId + ':' + value.turn_id
    const previous = old.appeals[key]
    if (previous && previous.status === value.status && previous.notification.status === value.notification.status
      && previous.notification.attempts === value.notification.attempts) return old
    return { ...old, appeals: { ...old.appeals, [key]: value } }
  }), [])

  return <div className="app-shell">
    <a className="skip-link" href="#content" onClick={(event) => { event.preventDefault(); document.getElementById('content')?.focus() }}>К содержимому</a>
    {drawer && <button className="drawer-backdrop" aria-label="Закрыть меню" tabIndex={-1}
      onClick={() => { setDrawer(false); navToggle.current?.focus() }} />}
    <aside className={'sidebar' + (drawer ? ' open' : '')} id="sidebar" ref={sidebar}>
      <div className="brand"><span className="brand-icon"><Layers3 size={21} strokeWidth={2} /></span>
        <div><strong>Reglament AI</strong><small>Рабочее пространство</small></div>
        <button className="icon-button drawer-close" ref={closeToggle} aria-label="Закрыть навигацию"
          onClick={() => { setDrawer(false); navToggle.current?.focus() }}><PanelLeftClose size={19} /></button>
      </div>
      <nav aria-label="Навигация" className="navigation">
        {sections.map(({ id, title, icon: Icon }) => <a key={id} href={'#' + id} aria-current={page === id ? 'page' : undefined}
          onClick={(event) => { event.preventDefault(); navigate(id) }}><Icon size={18} strokeWidth={1.8} />{title}</a>)}
      </nav>
      <div className="dialogue-nav">
        <button className="button new-dialogue" disabled={creating} onClick={() => void create().catch((error: unknown) => setCreateError(errorText(error)))}>
          <Plus size={17} /> Новый диалог
        </button>
        <p className="side-heading">Диалоги</p>
        <div className="dialogue-list">
          {!data.dialogues.length && <p className="empty-list">Здесь появятся ваши диалоги</p>}
          {data.dialogues.map((d) => <button key={d.dialogue_id} onClick={() => select(d.dialogue_id)}
            aria-current={page === 'chat' && d.dialogue_id === current?.dialogue_id ? 'true' : undefined} title={d.title}>
            <MessageSquare size={15} /><span>{d.title}</span>
          </button>)}
        </div>
      </div>
      <div className="sidebar-status"><span className={'status-dot ' + (healthError || mode === 'disabled' ? 'inactive' : '')} />
        {healthError ? 'Нет связи с сервером' : mode === 'demo' ? 'Демонстрационный режим' : mode === 'configured' ? 'Поиск подключён' : mode === 'disabled' ? 'Поиск не подключён' : 'Проверяем подключение…'}
      </div>
    </aside>
    <main id="content" tabIndex={-1} className="workspace">
      <header className="topbar">
        <button className="icon-button mobile-menu" ref={navToggle} aria-expanded={drawer} aria-controls="sidebar"
          aria-label="Открыть навигацию" onClick={() => setDrawer(!drawer)}><Menu size={23} /></button>
        <div className="breadcrumb"><span>Рабочее пространство</span><ChevronRight size={14} /><strong>{activeSection.title}</strong></div>
        {page === 'chat' && <button className="button header-metrics" onClick={() => navigate('analytics')}><BarChart3 size={16} /> <span>Метрики диалога</span></button>}
      </header>
      {(createError || storageError) && <div className="global-notice"><Notice tone="error">
        {createError || 'Не удалось сохранить список диалогов в браузере.'}
      </Notice></div>}
      <Chat current={current} mode={mode} visible={page === 'chat'} onCreate={create} onTitle={title}
        onUsage={updateUsage} appeals={data.appeals} onAppeal={addAppeal} />
      {page === 'quality' && <Quality token={secret} onToken={setSecret} />}
      {page === 'knowledge' && <Knowledge dialogues={data.dialogues} />}
      {page === 'analytics' && <Analytics dialogue={current} usage={current ? usage[current.dialogue_id] : undefined} onUsage={updateUsage} />}
    </main>
  </div>
}

function Knowledge({ dialogues }: { dialogues: Dialogue[] }) {
  const [sources, setSources] = useState<Source[]>([])
  const [busy, setBusy] = useState(true)
  const [incomplete, setIncomplete] = useState(false)
  const [term, setTerm] = useState('')
  const [kind, setKind] = useState<Source['source_type'] | ''>('')
  useEffect(() => {
    const controller = new AbortController()
    async function catalogue() {
      const collected = new Map<string, Source>()
      let failed = false
      setBusy(true)
      for (const dialogue of dialogues) {
        try {
          let cursor: number | null = null
          do {
            const history = await api.history(dialogue, cursor ?? undefined, controller.signal)
            for (const turn of [...history.messages].reverse()) for (const source of turn.sources) {
              if (!collected.has(source.source_id) || (collected.get(source.source_id)?.revision ?? 0) < (source.revision ?? 0)) {
                collected.set(source.source_id, source)
              }
            }
            cursor = history.next_before
          } while (cursor && !controller.signal.aborted)
        } catch {
          if (controller.signal.aborted) return
          failed = true
        }
      }
      if (!controller.signal.aborted) { setSources([...collected.values()]); setIncomplete(failed); setBusy(false) }
    }
    void catalogue()
    return () => controller.abort()
  }, [dialogues])
  const found = sources.filter((source) => (!kind || kind === source.source_type) && (
    [source.document, source.section, source.excerpt].some((value) => value?.toLowerCase().includes(term.trim().toLowerCase()))
  ))
  return <div className="page">
    <div className="page-heading"><div><h1>База знаний</h1><p>Источники, которые использовались в ваших сохранённых диалогах.</p></div></div>
    <div className="catalog-controls"><label className="search-input"><Search size={17} /><span className="sr-only">Поиск по источникам</span>
      <input placeholder="Название документа или текст цитаты" value={term} onChange={(event) => setTerm(event.target.value)} /></label>
      <label><span className="sr-only">Тип источника</span><select value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}>
        <option value="">Все источники</option><option value="regulation">Регламенты</option><option value="expert_answer">Экспертные разъяснения</option>
      </select></label>
    </div>
    <div className="catalog-note"><Notice>Полный каталог регламентов появится после подключения соответствующего API.</Notice></div>
    {incomplete && <Notice tone="error">Часть сохранённых диалогов недоступна.</Notice>}
    {busy ? <p role="status" className="muted">Загрузка источников…</p> : found.length ?
      <div className="catalog-grid">{found.map((source) => <SourceCard key={source.source_id} source={source} />)}</div> :
      <div className="panel empty-page"><Database size={28} /><h2>{sources.length ? 'Ничего не найдено' : 'Источников пока нет'}</h2>
        <p>{sources.length ? 'Измените текст поиска или фильтр.' : 'Задайте вопрос ассистенту, чтобы здесь появились использованные источники.'}</p></div>}
  </div>
}

function Analytics({ dialogue, usage, onUsage }: { dialogue: Dialogue | null; usage?: Usage; onUsage: (id: string, usage: Usage) => void }) {
  const [error, setError] = useState('')
  useEffect(() => {
    if (!dialogue) return
    setError('')
    const controller = new AbortController()
    api.history(dialogue, undefined, controller.signal).then((data) => onUsage(dialogue.dialogue_id, data.usage))
      .catch((failure: unknown) => { if (!controller.signal.aborted) setError(errorText(failure)) })
    return () => controller.abort()
  }, [dialogue, onUsage])
  return <div className="page">
    <div className="page-heading"><div><h1>Аналитика диалога</h1><p>{dialogue ? dialogue.title : 'Выберите диалог в боковом меню.'}</p></div></div>
    {error && <Notice tone="error">{error}</Notice>}
    {dialogue && usage && <><UsageCards usage={usage} /><Notice>Стоимость рассчитана по тарифам. Фактическое списание здесь не отображается.</Notice></>}
    {!dialogue && <div className="panel empty-page"><BarChart3 size={28} /><h2>Нет выбранного диалога</h2><p>Метрики доступны после создания диалога.</p></div>}
    {dialogue && !usage && !error && <p className="muted" role="status">Загрузка метрик…</p>}
  </div>
}
