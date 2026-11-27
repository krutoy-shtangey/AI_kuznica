import { ArrowUpRight, ChevronDown, CircleAlert, FileText, Layers3, ShieldCheck } from 'lucide-react'
import type { ReactNode } from 'react'
import type { Source, Usage } from './api'

export const departments: Record<string, string> = {
  ib: 'Дирекция ИБ', tech: 'Техническая дирекция', dbrct: 'ДБРЦТ',
}
export const stages: Record<string, string> = { presale: 'Пресейл', implementation: 'Реализация проекта' }
export const statuses: Record<string, string> = {
  new: 'Новое', in_work: 'В работе', draft: 'Черновик', approved: 'Утверждено', closed: 'Закрыто',
}
export const delivery: Record<string, string> = {
  disabled: 'Не отправлено · SMTP выключен', not_configured: 'Не отправлено · SMTP не настроен',
  pending: 'В очереди', sending: 'Отправляется', retry: 'Ожидает повтора',
  sent: 'Принято SMTP-сервером', failed: 'Не доставлено', uncertain: 'Доставка не подтверждена',
}
export const errorLabels: Record<string, string> = {
  source_changed: 'Источник изменился. Задайте вопрос заново.',
  evidence_invalid: 'Серверу не удалось подтвердить ответ по источникам.',
  model_invalid_response: 'Модель вернула непригодный ответ.',
  model_unavailable: 'Языковая модель временно недоступна.',
  retrieval_unavailable: 'Поиск временно недоступен.',
  source_review_required: 'Не удалось сверить разъяснение с регламентом. Обратитесь в службу качества.',
  internal_error: 'Внутренняя ошибка обработки запроса.',
}
export const statusText = (labels: Record<string, string>, key?: string | null) => key ? labels[key] ?? key : '—'
export const errorText = (error: unknown) => error instanceof Error ? error.message : 'Не удалось выполнить запрос.'
export function formatDate(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(+date) ? '—' : new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium' }).format(date)
}
const formatNumber = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 })

export function Notice({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'error' | 'success' }) {
  return <div className={'notice ' + tone} role={tone === 'error' ? 'alert' : 'status'}>
    <CircleAlert size={17} aria-hidden="true" /> <div>{children}</div>
  </div>
}

export function BrandMark({ small = false }: { small?: boolean }) {
  return <span className={small ? 'brand-mark small' : 'brand-mark'} aria-hidden="true">
    <Layers3 size={small ? 16 : 22} strokeWidth={1.8} />
  </span>
}

export function SourceCard({ source }: { source: Source }) {
  const expert = source.source_type === 'expert_answer'
  const Icon = expert ? ShieldCheck : FileText
  const link = source.source_url && /^https?:\/\//i.test(source.source_url) ? source.source_url : null
  const metadata = [
    ['Подразделение', source.department && statusText(departments, source.department)],
    ['Этап', source.stage && statusText(stages, source.stage)],
    ['Условия', source.conditions],
    ['Дата утверждения', expert && source.approved_at && formatDate(source.approved_at)],
    ['Номер', expert && (source.appeal_id ?? source.source_id)],
    ['Редакция', expert && source.revision],
    ['Статус', source.status === 'approved' && (expert ? 'Утверждено' : 'Актуальность подтверждена')],
  ] as const
  return <details className={'source-card' + (expert ? ' expert' : '')}>
    <summary className="source-top">
      <span className="source-icon"><Icon size={17} /></span>
      <span className="source-label">
        {expert && <small>Разъяснение службы качества</small>}
        <strong>{source.document}</strong>
        <span className="source-subline">
          {source.section && <span>Пункт {source.section}</span>}
          {source.status === 'unknown' && <span className="source-warning">Актуальность не подтверждена</span>}
        </span>
      </span>
      <ChevronDown className="source-chevron" size={16} aria-hidden="true" />
    </summary>
    <div className="source-body">
      {source.excerpt && <blockquote>{source.excerpt}</blockquote>}
      {source.excerpt_partial && <small className="muted">Показан фрагмент пункта</small>}
      <dl className="source-meta">{metadata.map(([label, value]) => value ?
        <div key={label}><dt>{label}</dt><dd>{value}</dd></div> : null)}</dl>
      {link && <a className="source-link" href={link} target="_blank" rel="noopener noreferrer">Открыть источник <ArrowUpRight size={15} /></a>}
    </div>
  </details>
}

function costText(cost: Usage['provider_cost']) {
  const known = Object.entries(cost.known_by_currency).map(([currency, amount]) => amount.replace('.', ',') + ' ' + currency)
  const text = known.length ? known.join(' · ') : '—'
  return cost.unknown_attempts ? text + ' · неполные данные' : text
}
export function UsageCards({ usage }: { usage: Usage }) {
  const data = [
    ['Входные токены', usage.input_tokens === null ? '—' : formatNumber.format(usage.input_tokens)],
    ['Выходные токены', usage.output_tokens === null ? '—' : formatNumber.format(usage.output_tokens)],
    ['Вызовы модели', formatNumber.format(usage.attempts)],
    ['Время генерации', formatNumber.format(usage.seconds) + ' с'],
    ['ProxyAPI · расчёт', costText(usage.provider_cost)],
    ['Сравнительная стоимость · расчёт', costText(usage.alibaba_estimate)],
  ]
  return <dl className="metrics-grid">{data.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
}
