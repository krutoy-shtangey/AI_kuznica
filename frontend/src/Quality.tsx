import { ArrowLeft, ChevronRight, CircleCheck, ClipboardList, Clock3, Inbox, KeyRound, LoaderCircle, LogOut, Pencil, RefreshCw, ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError } from './api'
import type { Appeal, Draft, Publication, Status } from './api'
import { delivery, departments, errorText, formatDate, Notice, stages, statuses, statusText } from './ui'

type Section = 'appeals' | 'explanations'
type Action = Parameters<typeof api.update>[2]
type Fields = Parameters<typeof api.update>[3]

const reasons = { NO_ANSWER: 'Недостаточно информации', CONFLICT: 'Противоречие источников' }

function publicationText(value: Publication | null) {
  if (!value) return 'Публикации нет'
  if (value.published && value.state === 'indexed') return 'Опубликовано'
  if (value.action === 'delete') {
    if (value.state === 'indexed') return 'Отозвано'
    return value.state === 'failed' ? 'Отозвано · сбой удаления из индекса' : 'Отозвано · ожидает удаления из индекса'
  }
  if (value.state === 'failed') return 'Ошибка индексации'
  return value.state === 'running' ? 'Индексируется' : 'Ожидает индексации'
}

export default function Quality({ token, onToken }: { token: string | null; onToken: (value: string | null) => void }) {
  return token ? <Panel token={token} logout={() => onToken(null)} /> : <Login onToken={onToken} />
}

function Login({ onToken }: { onToken: (value: string | null) => void }) {
  const [secret, setSecret] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function enter(event: FormEvent) {
    event.preventDefault()
    if (busy || !secret.trim()) return
    setBusy(true); setError('')
    try { await api.appeals(secret.trim(), '', 0); onToken(secret.trim()) }
    catch (failure) { setError(errorText(failure)) }
    finally { setBusy(false) }
  }

  return <div className="page login-page">
    <section className="login-card">
      <span className="welcome-icon"><ShieldCheck size={26} strokeWidth={1.7} /></span>
      <h1>Служба качества</h1>
      <p>Введите административный секрет, чтобы просматривать обращения и утверждать разъяснения.</p>
      <form onSubmit={(event) => void enter(event)}>
        <label htmlFor="quality-secret">Административный секрет</label>
        <div className="secret-input"><KeyRound size={16} />
          <input id="quality-secret" type="password" autoComplete="off" maxLength={256} required value={secret}
            onChange={(event) => setSecret(event.target.value)} />
        </div>
        {error && <Notice tone="error">{error}</Notice>}
        <button className="button primary full" type="submit" disabled={busy || !secret.trim()}>
          {busy && <LoaderCircle size={16} className="spin" />} Войти
        </button>
      </form>
    </section>
  </div>
}

function Panel({ token, logout }: { token: string; logout: () => void }) {
  const [section, setSection] = useState<Section>('appeals')
  const [status, setStatus] = useState<Status | ''>('')
  const [offset, setOffset] = useState(0)
  const [cards, setCards] = useState<Appeal[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<Appeal | null>(null)
  const [mobileDetail, setMobileDetail] = useState(false)
  const [listError, setListError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [loadingList, setLoadingList] = useState(true)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [listRefresh, setListRefresh] = useState(0)
  const [detailRefresh, setDetailRefresh] = useState(0)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setLoadingList(true); setListError('')
    api.appeals(token, status, offset, section === 'explanations', controller.signal).then((data) => {
      if (!controller.signal.aborted) setCards(data.items)
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) { setCards([]); setListError(errorText(error)) }
    }).finally(() => { if (!controller.signal.aborted) setLoadingList(false) })
    return () => controller.abort()
  }, [token, section, status, offset, listRefresh])

  useEffect(() => {
    if (!selected) return
    const controller = new AbortController()
    setLoadingDetail(true); setDetailError('')
    api.detail(token, selected, controller.signal).then((value) => {
      if (!controller.signal.aborted) setDetail(value)
    }).catch((error: unknown) => { if (!controller.signal.aborted) setDetailError(errorText(error)) })
      .finally(() => { if (!controller.signal.aborted) setLoadingDetail(false) })
    return () => controller.abort()
  }, [token, selected, detailRefresh])

  const publicationState = detail?.publication?.state
  const mailState = detail?.notification.status
  const polling = ['pending', 'running'].includes(publicationState ?? '') || ['pending', 'retry', 'sending'].includes(mailState ?? '')
  useEffect(() => {
    if (!selected || !polling || busy) return
    const controller = new AbortController()
    const timer = window.setInterval(() => {
      api.detail(token, selected, controller.signal).then((updated) => {
        if (!controller.signal.aborted) {
          setDetail((previous) => previous?.id === updated.id && previous.version <= updated.version ? updated : previous)
          setCards((previous) => previous.map((card) => card.id === updated.id && card.version <= updated.version ? updated : card))
        }
      }).catch(() => undefined)
    }, 2500)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [token, selected, polling, busy])

  function reset(next: Section, filter: Status | '') {
    setSection(next); setStatus(filter); setOffset(0); setSelected(null); setDetail(null); setMobileDetail(false)
  }

  async function mutate(action: Action, fields?: Fields) {
    if (!detail || detail.id !== selected || busy) return false
    setBusy(true); setDetailError('')
    try {
      const next = await api.update(token, detail, action, fields)
      setDetail(next)
      setCards((old) => old.map((item) => item.id === next.id ? next : item))
      setListRefresh((previous) => previous + 1)
      return true
    } catch (error) {
      setDetailError(errorText(error))
      if (error instanceof ApiError && error.status === 409) setDetailRefresh((value) => value + 1)
      return false
    } finally { setBusy(false) }
  }

  return <div className="page quality-page">
    <div className="page-heading"><div><h1>Служба качества</h1><p>Обращения сотрудников и публикация проверенных разъяснений.</p></div>
      <button className="button" onClick={logout}><LogOut size={16} /> Выйти</button>
    </div>
    <div className="tabs" aria-label="Разделы службы качества">
      <button aria-pressed={section === 'appeals'} disabled={busy} onClick={() => reset('appeals', '')}>Обращения</button>
      <button aria-pressed={section === 'explanations'} disabled={busy} onClick={() => reset('explanations', '')}>Опубликованные разъяснения</button>
    </div>
    <div className={'quality-grid' + (mobileDetail ? ' mobile-detail' : '')}>
      <section className="queue panel" aria-label={section === 'appeals' ? 'Список обращений' : 'Список разъяснений'}>
        <div className="queue-heading">
          {section === 'appeals' ? <label htmlFor="status-filter" className="sr-only">Фильтр по статусу</label> : <strong>Разъяснения</strong>}
          {section === 'appeals' && <select id="status-filter" value={status} disabled={busy}
            onChange={(event) => reset('appeals', event.target.value as Status | '')}>
            <option value="">Все статусы</option>
            {Object.entries(statuses).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select>}
          <button className="icon-button" title="Обновить список" aria-label="Обновить список" disabled={loadingList || busy}
            onClick={() => setListRefresh((value) => value + 1)}><RefreshCw size={17} /></button>
        </div>
        {listError && <Notice tone="error">{listError}</Notice>}
        {loadingList && <p className="list-empty muted" role="status">Загрузка…</p>}
        {!loadingList && !listError && !cards.length && <div className="list-empty muted"><Inbox size={23} />{section === 'appeals' ? 'Нет обращений' : 'Нет опубликованных разъяснений'}</div>}
        {!loadingList && cards.map((card) => <button key={card.id} className={'queue-item' + (selected === card.id ? ' selected' : '')}
          disabled={busy} aria-pressed={selected === card.id} onClick={() => {
            if (selected !== card.id) { setDetail(null); setSelected(card.id) }
            setMobileDetail(true)
          }}>
          <span><strong>{card.question}</strong><small>{formatDate(card.created_at)} · {section === 'appeals' ? statusText(statuses, card.status) : publicationText(card.publication)}</small></span>
          <ChevronRight size={16} />
        </button>)}
        {(offset > 0 || cards.length === 30) && <div className="pagination">
          <button className="button" disabled={!offset || loadingList || busy} onClick={() => setOffset((value) => value - 30)}>Назад</button>
          <button className="button" disabled={cards.length < 30 || loadingList || busy} onClick={() => setOffset((value) => value + 30)}>Далее</button>
        </div>}
      </section>
      <section className="case panel" aria-label="Карточка обращения">
        <button className="button mobile-back" onClick={() => setMobileDetail(false)}><ArrowLeft size={16} /> К списку</button>
        {detailError && <Notice tone="error">{detailError}</Notice>}
        {loadingDetail && <p className="list-empty muted" role="status">Загрузка карточки…</p>}
        {!detail && !loadingDetail && !detailError && <div className="detail-empty"><ClipboardList size={28} /><p>Выберите обращение</p></div>}
        {detail && detail.id === selected && <CaseDetail key={detail.id} card={detail} busy={busy || loadingDetail}
          onUpdate={mutate} onReload={() => setDetailRefresh((value) => value + 1)} />}
      </section>
    </div>
  </div>
}

function CaseDetail({ card, busy, onUpdate, onReload }: {
  card: Appeal; busy: boolean; onUpdate: (action: Action, fields?: Fields) => Promise<boolean>; onReload: () => void
}) {
  const [form, setForm] = useState<Draft | null>(null)
  const [confirmation, setConfirmation] = useState<'approve' | 'revoke' | null>(null)
  const doc = card.publication

  function edit() {
    const base = card.draft ?? doc?.payload
    setForm({
      text: base?.text ?? '', department: base ? base.department : card.department,
      stage: base ? base.stage : card.stage, conditions: base?.conditions ?? null,
    })
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (form?.text.trim() && await onUpdate('draft', form)) setForm(null)
  }

  return <>
    <div className="case-title"><div className="case-kicker"><span className="pill">{statusText(statuses, card.status)}</span><span>{formatDate(card.created_at)}</span></div>
      <h2>{card.question}</h2><small className="muted record-id">Обращение {card.id}</small>
    </div>
    <dl className="case-facts">
      <div><dt>Причина</dt><dd>{reasons[card.reason]}</dd></div>
      <div><dt>Уведомление</dt><dd>{statusText(delivery, card.notification.status)}</dd></div>
      <div><dt>Подразделение</dt><dd>{statusText(departments, card.department)}</dd></div>
      <div><dt>Этап</dt><dd>{statusText(stages, card.stage)}</dd></div>
    </dl>
    {(card.original_question !== card.question || card.clarifications.length > 0 || Object.keys(card.facts).length > 0) &&
      <details className="case-details" open><summary>Уточнения и исходный вопрос</summary>
        <p>{card.original_question}</p>
        {!!card.clarifications.length && <ul>{card.clarifications.map((item, index) => <li key={index}>{item}</li>)}</ul>}
        {!!Object.keys(card.facts).length && <p>{Object.entries(card.facts).map(([key, value]) => key + '=' + value).join(' · ')}</p>}
      </details>}
    <details className="case-details"><summary>Рассмотренные источники ({card.considered_source_ids.length})</summary>
      {card.considered_source_ids.length ? <ul>{card.considered_source_ids.map((id) => <li key={id}><code>{id}</code></li>)}</ul> : <p>Источники не найдены.</p>}
    </details>
    <div className="button-row case-actions">
      {['new', 'closed'].includes(card.status) && <button className="button" disabled={busy}
        onClick={() => void onUpdate('status', { status: 'in_work' })}>{card.status === 'closed' ? 'Вернуть в работу' : 'Взять в работу'}</button>}
      {card.status !== 'closed' && <button className="button" disabled={busy}
        onClick={() => void onUpdate('status', { status: 'closed' })}>Закрыть обращение</button>}
    </div>
    <div className="publication">
      <div className="subheading"><h3>Экспертное разъяснение</h3>
        <button className="icon-button" title="Обновить карточку" aria-label="Обновить карточку" disabled={busy} onClick={onReload}><RefreshCw size={16} /></button>
      </div>
      <div className={'publication-state' + (doc?.state === 'failed' ? ' failed' : '')} role="status">
        {doc?.published && doc.state === 'indexed' ? <CircleCheck size={17} /> : <Clock3 size={17} />}
        <span>{publicationText(doc)}{doc ? ' · редакция ' + doc.revision : ''}</span>
      </div>
      {doc?.payload && <div className="published-copy">
        <p>{doc.payload.text}</p><small>Утвердил: {doc.approved_by} · {formatDate(doc.approved_at)}</small>
        <div className="chips">
          {doc.payload.department && <span>{statusText(departments, doc.payload.department)}</span>}
          {doc.payload.stage && <span>{statusText(stages, doc.payload.stage)}</span>}
          {doc.payload.conditions && <span>{doc.payload.conditions}</span>}
        </div>
      </div>}
      {doc?.state === 'failed' && <button className="button" disabled={busy}
        onClick={() => void onUpdate('retry-index')}><RefreshCw size={16} /> Повторить синхронизацию</button>}
      {card.draft && !form && <div className="published-copy draft-copy"><strong>Черновик</strong><p>{card.draft.text}</p></div>}
      {form ? <form className="draft-form" onSubmit={(event) => void save(event)}>
        <label htmlFor="expert-text">Экспертный ответ</label>
        <textarea id="expert-text" value={form.text} maxLength={2400} required rows={5}
          onChange={(event) => setForm({ ...form, text: event.target.value })} />
        <div className="draft-scope">
          <label>Подразделение<select value={form.department ?? ''} onChange={(e) => setForm({ ...form, department: e.target.value || null })}>
            <option value="">Любое подразделение</option>
            {Object.entries(departments).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select></label>
          <label>Этап<select value={form.stage ?? ''} onChange={(e) => setForm({ ...form, stage: e.target.value || null })}>
            <option value="">Любой этап</option>
            {Object.entries(stages).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select></label>
        </div>
        <label htmlFor="conditions">Условия применимости</label>
        <input id="conditions" value={form.conditions ?? ''} maxLength={500}
          placeholder="вид=учебный;цвет=синий" onChange={(event) => setForm({ ...form, conditions: event.target.value || null })} />
        <div className="button-row"><button className="button primary" type="submit" disabled={busy || !form.text.trim()}>Сохранить черновик</button>
          <button className="button" type="button" disabled={busy} onClick={() => setForm(null)}>Отмена</button></div>
      </form> : <div className="button-row">
        {card.status !== 'closed' && <button className="button" disabled={busy} onClick={edit}><Pencil size={15} />
          {card.draft ? 'Редактировать черновик' : doc?.payload ? 'Редактировать ответ' : 'Создать черновик'}</button>}
        {card.draft && card.status !== 'closed' && <button className="button primary" disabled={busy}
          onClick={() => setConfirmation('approve')}>Утвердить и опубликовать</button>}
        {doc?.action === 'upsert' && <button className="button danger" disabled={busy} onClick={() => setConfirmation('revoke')}>Отозвать</button>}
      </div>}
      {confirmation && <div className="confirm-action">
        <p>{confirmation === 'approve' ? 'Утвердить сохранённый черновик и передать его в поиск?' : 'Отозвать эту редакцию и убрать её из поиска?'}</p>
        <div className="button-row">
          <button className={'button ' + (confirmation === 'approve' ? 'primary' : 'danger')} disabled={busy}
            onClick={() => { void onUpdate(confirmation).then((ok) => { if (ok) setConfirmation(null) }) }}>
            {confirmation === 'approve' ? 'Подтвердить утверждение' : 'Подтвердить отзыв'}
          </button>
          <button className="button" disabled={busy} onClick={() => setConfirmation(null)}>Отмена</button>
        </div>
      </div>}
    </div>
  </>
}
