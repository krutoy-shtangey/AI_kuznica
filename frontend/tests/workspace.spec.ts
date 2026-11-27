import { expect, test as base } from '@playwright/test'
import type { Page } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'

// Some hosts need one Chromium process per test for the single-process launcher.
const test = process.env.UI_TEST_SINGLE_PROCESS === '1' ? base.extend({
  page: async ({ playwright, baseURL, viewport }, use, info) => {
    const browser = await playwright.chromium.launch(info.project.use.launchOptions)
    try {
      await use(await browser.newPage({ baseURL, viewport: viewport ?? undefined }))
    } finally {
      await browser.close()
    }
  },
}) : base

async function snap(page: Page, file: string) {
  const directory = process.env.UI_SCREENSHOTS_DIR
  if (!directory) return
  await mkdir(directory, { recursive: true })
  await page.evaluate(() => document.fonts.ready)
  await page.screenshot({ path: join(directory, file), animations: 'disabled' })
}

const tabs = (page: Page) => page.getByRole('navigation', { name: 'Навигация' })
const field = (page: Page) => page.getByRole('textbox', { name: 'Сообщение', exact: true })

async function ask(page: Page, message: string) {
  const response = page.waitForResponse((item) => item.url().endsWith('/messages') && item.request().method() === 'POST')
  await field(page).fill(message)
  await expect(page.getByRole('button', { name: 'Отправить сообщение' })).toBeEnabled()
  await field(page).press('Enter')
  return response
}
async function newDialogue(page: Page) {
  const response = page.waitForResponse((item) => item.url().endsWith('/dialogues') && item.request().method() === 'POST')
  await page.getByRole('button', { name: 'Новый диалог' }).click()
  return (await response).json()
}
async function login(page: Page) {
  await tabs(page).getByRole('link', { name: 'Служба качества' }).click()
  await page.getByLabel('Административный секрет').fill(process.env.UI_TEST_ADMIN_SECRET!)
  await page.getByRole('button', { name: 'Войти', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Выйти' })).toBeVisible()
}

test('чат: два уточнения, источники, дополнительный вопрос, отдельные истории и восстановление', async ({ page, request }) => {
  await page.goto('/')
  await expect(page.getByText('Демонстрационный режим')).toBeVisible()
  await snap(page, 'Reglament-AI — старт.png')
  const first = await newDialogue(page)
  await expect(page.getByRole('heading', { name: 'Вопросы по регламентам' })).toBeVisible()
  await field(page).fill('Первая строка')
  await field(page).press('Shift+Enter')
  await expect(field(page)).toHaveValue('Первая строка\n')
  await ask(page, 'Кто принимает опытный макет?')
  await expect(page.getByText('Нужно уточнение')).toBeVisible()
  await expect(page.getByText(/Какое подразделение относится/)).toBeVisible()
  await ask(page, 'Техническая дирекция')
  await expect(page.getByText(/О каком этапе речь/)).toBeVisible()
  await ask(page, 'На реализации')
  await expect(page.getByText('Ответ по источникам')).toBeVisible()
  await expect(page.getByText('Пункт 2.1')).toBeVisible()
  await expect(page.getByText('Актуальность не подтверждена')).toBeVisible()
  await expect(page.getByText('Опытный макет на этапе реализации принимает руководитель стенда.')).toBeVisible()
  await snap(page, 'Reglament-AI — чат.png')
  await ask(page, 'А где фиксируют приёмку опытного макета?')
  await expect(page.getByText('Пункт 2.2')).toBeVisible()
  const second = await newDialogue(page)
  await expect(page.getByRole('heading', { name: 'Вопросы по регламентам' })).toBeVisible()
  expect((await request.get('/api/dialogues/' + first.dialogue_id + '/messages', {
    headers: { Authorization: 'Bearer ' + second.access_token },
  })).status()).toBe(404)

  await page.getByRole('button', { name: 'Кто принимает опытный макет?', exact: true }).click()
  await expect(page.getByText('Пункт 2.2')).toBeVisible()
  await page.reload()
  await expect(page.getByText('Пункт 2.2')).toBeVisible()
  expect(page.url()).not.toContain(first.access_token)
  await tabs(page).getByRole('link', { name: 'База знаний' }).click()
  const catalogue = page.locator('.catalog-grid')
  await expect(catalogue.getByText('Пункт 2.1')).toBeVisible()
  await expect(catalogue.getByText('Пункт 2.2')).toBeVisible()
  await snap(page, 'Reglament-AI — база знаний.png')
  await tabs(page).getByRole('link', { name: 'Аналитика' }).click()
  await expect(page.getByText('Вызовы модели')).toBeVisible()
  await expect(page.getByText('Сравнительная стоимость · расчёт')).toBeVisible()
})

test('служба качества: карточка, отказ SMTP, черновик, публикация, отдельный диалог, отзыв', async ({ page, request }) => {
  await page.goto('/')
  await ask(page, 'Кто выдаёт пропуск в зал маяка?')
  await expect(page.getByText('Недостаточно информации', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Создать обращение' }).click()
  await expect(page.getByText(/Обращение [a-f0-9]{8}/)).toBeVisible()
  await expect(page.getByText(/Не доставлено/)).toBeVisible({ timeout: 15_000 })
  await login(page)
  await page.getByRole('region', { name: 'Список обращений' }).getByRole('button', { name: /Кто выдаёт пропуск в зал маяка/ }).click()
  await expect(page.getByText('Рассмотренные источники (0)')).toBeVisible()
  await page.getByRole('button', { name: 'Взять в работу' }).click()
  await expect(page.locator('.case-kicker').getByText('В работе')).toBeVisible()
  await page.getByLabel('Фильтр по статусу').selectOption('in_work')
  await page.getByRole('region', { name: 'Список обращений' }).getByRole('button', { name: /Кто выдаёт пропуск в зал маяка/ }).click()
  await page.getByRole('button', { name: 'Создать черновик' }).click()
  await page.getByLabel('Экспертный ответ').fill('Пропуск в зал маяка выдаёт смотритель.')
  await page.getByRole('button', { name: 'Сохранить черновик' }).click()
  await expect(page.getByText('Пропуск в зал маяка выдаёт смотритель.')).toBeVisible()
  await expect(page.getByText('Публикации нет')).toBeVisible()
  const id = (await page.locator('.record-id').innerText()).replace('Обращение ', '')
  const denied = await request.post('/api/quality/appeals/' + id + '/approve', { data: { version: 1 } })
  expect(denied.status()).toBe(401)
  await page.route('**/quality/appeals/*/approve', async (route) => {
    const response = await route.fetch()
    const actual = await response.json()
    await route.fulfill({ response, status: 202, json: { ...actual, publication: {
      ...actual.publication, state: 'pending', published: false,
    } } })
  }, { times: 1 })
  await page.getByRole('button', { name: 'Утвердить и опубликовать' }).click()
  await page.getByRole('button', { name: 'Подтвердить утверждение' }).click()
  await expect(page.getByText('Ожидает индексации · редакция 1')).toBeVisible()
  await expect(page.getByText('Опубликовано · редакция 1')).toBeVisible()
  await page.getByLabel('Фильтр по статусу').selectOption('approved')
  await page.getByRole('region', { name: 'Список обращений' }).getByRole('button', { name: /Кто выдаёт пропуск в зал маяка/ }).click()
  await expect(page.getByText('Опубликовано · редакция 1')).toBeVisible()
  await snap(page, 'Reglament-AI — служба качества.png')
  await newDialogue(page)
  await ask(page, 'Кто выдаёт пропуск в зал маяка?')
  await expect(page.getByText('Разъяснение службы качества', { exact: true })).toBeVisible()
  await expect(page.getByText('Пропуск в зал маяка выдаёт смотритель.')).toBeVisible()
  await tabs(page).getByRole('link', { name: 'Служба качества' }).click()
  await page.getByRole('region', { name: 'Список обращений' }).getByRole('button', { name: /Кто выдаёт пропуск в зал маяка/ }).click()
  await page.getByRole('button', { name: 'Редактировать ответ' }).click()
  await page.getByLabel('Экспертный ответ').fill('Пропуск в зал маяка выдаёт администратор.')
  await page.getByRole('button', { name: 'Сохранить черновик' }).click()
  await expect(page.getByText('Опубликовано · редакция 1')).toBeVisible()
  await page.getByRole('button', { name: 'Утвердить и опубликовать' }).click()
  await page.getByRole('button', { name: 'Подтвердить утверждение' }).click()
  await expect(page.getByText('Опубликовано · редакция 2')).toBeVisible()
  await page.getByRole('button', { name: 'Отозвать', exact: true }).click()
  await page.getByRole('button', { name: 'Подтвердить отзыв' }).click()
  await expect(page.getByText('Отозвано · редакция 3')).toBeVisible()
  await page.getByRole('button', { name: 'Выйти' }).click()
  await expect(page.getByLabel('Административный секрет')).toBeVisible()
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(process.env.UI_TEST_ADMIN_SECRET!)
  await newDialogue(page)
  await ask(page, 'Кто выдаёт пропуск в зал маяка?')
  await expect(page.getByText('Недостаточно информации', { exact: true })).toBeVisible()
})

test('конфликт направляется на проверку, технический сбой показывается отдельно', async ({ page }) => {
  await page.goto('/')
  await ask(page, 'Кто подписывает акт испытаний опытного макета в Технической дирекции на реализации?')
  await expect(page.getByText('Противоречие источников')).toBeVisible()
  await expect(page.getByText('Пункт 4.1')).toBeVisible()
  await expect(page.getByText('Пункт 4.2')).toBeVisible()
  await page.getByRole('button', { name: 'Создать обращение' }).click()
  await expect(page.getByText(/Обращение [a-f0-9]{8}/)).toBeVisible()
  await newDialogue(page)
  await ask(page, 'Сбой модели: кто принимает опытный макет?')
  await expect(page.getByRole('alert').filter({ hasText: /Языковая модель временно недоступна/ }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: 'Создать обращение' })).toHaveCount(0)
})

test('мобильная навигация, доступность и отсутствие горизонтальной прокрутки', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Вопросы по регламентам' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390)
  await snap(page, 'Reglament-AI — мобильный.png')
  await page.getByRole('button', { name: 'Открыть навигацию' }).click()
  await expect(page.getByRole('button', { name: 'Закрыть навигацию' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'Открыть навигацию' })).toBeFocused()
  await page.getByRole('button', { name: 'Открыть навигацию' }).click()
  await tabs(page).getByRole('link', { name: 'Служба качества' }).click()
  await expect(page.getByLabel('Административный секрет')).toBeVisible()
})

test('production: FastAPI отдаёт сборку и защищённый API с одного origin', async ({ page, request }) => {
  const origin = 'http://127.0.0.1:8117'
  const response = await page.goto(origin)
  expect(response?.headers()['x-robots-tag']).toBe('noindex, nofollow')
  await expect(page.getByRole('heading', { name: 'Вопросы по регламентам' })).toBeVisible()
  const health = await request.get(origin + '/api/health')
  expect(await health.json()).toEqual({ status: 'ok', retrieval: 'demo' })
  expect((await request.get(origin + '/api/quality/appeals')).status()).toBe(401)
  expect(await (await request.get(origin + '/robots.txt')).text()).toContain('Disallow: /')
})
