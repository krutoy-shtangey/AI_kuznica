# Развёртывание Reglament AI на VPS

Целевая машина — Ubuntu 24.04 LTS на x86-64, 4 vCPU, 8 ГБ RAM, 40 ГБ SSD, без GPU. Nginx принимает HTTPS, а один процесс Uvicorn на `127.0.0.1:8000` обслуживает frontend и API. SQLite работает на локальном диске в режиме WAL. E5 выполняется на CPU. Генеративная модель в состав VPS не входит.

| Режим | Поведение |
| --- | --- |
| `disabled` | Frontend, API и проверка состояния доступны. Поиск и отправка вопросов отключены. Подходит для первичной проверки сервера. |
| `demo` | Явно включает синтетический поиск и отдельную базу `demo.sqlite3`. Запрос с результатами поиска может обратиться к модели. Используйте только вымышленные данные. |
| `retrieval` | Подключает согласованную фабрику поиска и отдельную базу `agent.sqlite3`. До интеграции поискового модуля этот режим недоступен. |

Текущий backend требует loopback-адрес модели при использовании корпоративного поиска. Обычная подстановка облачного URL остановит рабочий режим при запуске. Для запуска без генеративной модели на VPS сначала согласуйте конкретный внешний API и допустимые к передаче данные. Поддержка этого утверждённого адреса потребует отдельного изменения с сохранением запрета по умолчанию. Подтверждение разграничения данных нужно отдельно от технической проверки подключения.

## 1. Подготовить систему и собрать выпуск

Нужен пользователь с `sudo` и рабочим SSH-доступом. Для стенда настройте DNS A на публичный IPv4 и откройте TCP 80/443. AAAA добавляйте, только если VPS принимает IPv6. Оставьте SSH доступным для своего адреса. Порт 8000 остаётся на loopback. Для SMTP и выбранного API понадобятся разрешённые исходящие подключения.

Из проверенной копии репозитория на VPS:

```bash
sudo bash deploy/install-system.sh --with-node
```

Скрипт устанавливает системные пакеты, создаёт служебного пользователя `reglament` и каталоги. `--with-node` добавляет закреплённый официальный Node.js для сборки интерфейса с проверкой SHA-256. Флаг можно опустить, если совместимый Node.js 22.12+ уже доступен оператору. Скрипт не загружает модели и не активирует приложение. [Официальный выпуск Node.js](https://nodejs.org/en/blog/release/v22.23.5/)

Собирайте каждый выпуск в собственном каталоге. Укажите проверенный коммит, в котором уже присутствует эта директория:

```bash
COMMIT=$(git rev-parse HEAD) &&
RELEASE="/opt/reglament-ai/releases/$COMMIT" && (
    set -euo pipefail
    umask 022
    sudo mkdir -m 0755 -- "$RELEASE"
    sudo chown "$(id -un)" "$RELEASE"
    git archive "$COMMIT" | tar -x -C "$RELEASE"
    bash "$RELEASE/deploy/install-app.sh"
    sudo chown -R root:root "$RELEASE"
)
```

`install-app.sh` создаёт Python-окружение выпуска, проверяет зависимости и собирает `frontend/dist`. Исходники и окружение после сборки доступны сервису только для чтения. После интеграции адаптера соберите согласованный коммит с флагом `--with-retrieval`. Этот режим устанавливает закреплённый адаптером CPU-вариант PyTorch перед остальными зависимостями поиска. [Установка CPU PyTorch](https://docs.pytorch.org/get-started/previous-versions/)

## 2. Разместить конфигурацию и данные

| Путь | Назначение |
| --- | --- |
| `/etc/reglament-ai/app.env` | Секреты и настройки процесса. Владелец root, права 0600. |
| `/var/lib/reglament-ai/storage` | SQLite, WAL, диалоги, обращения, очередь, бюджет вызовов. Запись от пользователя сервиса. |
| `/var/lib/reglament-ai/documents` | Четыре DOCX и рабочий реестр. Пользователь сервиса имеет доступ на чтение. |
| `/var/lib/reglament-ai/index` | Постоянный поисковый индекс. Запись от пользователя сервиса. |
| `/var/lib/reglament-ai/models` | Веса и конфигурация E5 в структуре, которую ожидает адаптер. Пользователь сервиса имеет доступ на чтение. |

Для новой установки:

```bash
sudo install -o root -g root -m 0600 "$RELEASE/deploy/app.env.example" /etc/reglament-ai/app.env
sudoedit /etc/reglament-ai/app.env
```

Если конфигурация уже есть на Mac, подготовьте отдельный файл для VPS с нужными серверу значениями. Передайте его и DOCX по SSH в закрытый каталог пользователя. Пример, запускаемый на Mac:

```bash
SERVER=operator@vps.example.org
INBOX=$(ssh "$SERVER" 'umask 077; mktemp -d')
scp ./vps.env "$SERVER:$INBOX/app.env"
scp ./private-regulations/*.docx "$SERVER:$INBOX/"
ssh -t "$SERVER" "printf 'Временный каталог: %s\n' '$INBOX'; exec bash -l"
```

На VPS подставьте путь из вывода в `INBOX`. Импортируйте конфигурацию один раз и уберите закрытую промежуточную копию:

```bash
sudo install -o root -g root -m 0600 "$INBOX/app.env" /etc/reglament-ai/app.env
sudo install -o root -g reglament -m 0640 "$INBOX/"*.docx /var/lib/reglament-ai/documents/
rm -- "$INBOX/app.env" "$INBOX/"*.docx
rmdir -- "$INBOX"
```

Для первой проверки оставьте `REGLAMENT_MODE=disabled` и `SMTP_ENABLED=0`. Задайте случайный `QUALITY_ADMIN_SECRET` длиной от 32 символов из менеджера паролей. Сохраняйте формат `KEY=value`, подходящий для `EnvironmentFile`. Не копируйте секреты в каталог выпуска, Git, переменные `VITE_*` или аргументы команд. Для реестра и весов используйте такой же закрытый канал, переносите файлы без символьных ссылок.

## 3. Установить сервис и проверить его локально

Переключите `current` только после успешной сборки. Если уже есть рабочий выпуск, сначала сделайте резервную копию по шагу 7:

```bash
test -s "$RELEASE/frontend/dist/index.html" &&
    sudo ln -s "$RELEASE" /opt/reglament-ai/current.next &&
    sudo mv -Tf /opt/reglament-ai/current.next /opt/reglament-ai/current
```

При первой установке:

```bash
sudo install -m 0644 "$RELEASE/deploy/reglament-ai.service" /etc/systemd/system/reglament-ai.service
sudo systemd-analyze verify /etc/systemd/system/reglament-ai.service
sudo systemctl daemon-reload
sudo systemctl enable --now reglament-ai
curl -fsS http://127.0.0.1:8000/api/health
```

Первый ожидаемый ответ — `{"status":"ok","retrieval":"disabled"}`. Сервис стартует после перезагрузки и перезапускается при сбое. Рабочие файлы защищены от записи, запись разрешена в SQLite и индекс. При изменении конфигурации перезапустите сервис. [Параметры systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)

## 4. Получить HTTPS и включить один публичный адрес

Выполняйте команды после настройки DNS. `DOMAIN` — проверенное публичное имя хоста без схемы, порта и пути. Замените пример на свой домен:

```bash
DOMAIN=reglament.example.org
(
    set -euo pipefail
    [[ "$DOMAIN" =~ ^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$ ]]
    SITE=/etc/nginx/sites-available/reglament-ai
    sed "s/__DOMAIN__/$DOMAIN/g" "$RELEASE/deploy/nginx-bootstrap.conf" | sudo tee "$SITE" >/dev/null
    sudo ln -sfn "$SITE" /etc/nginx/sites-enabled/reglament-ai
    sudo nginx -t
    sudo systemctl reload nginx

    sudo certbot certonly --webroot --webroot-path /var/www/reglament-acme \
        --cert-name "$DOMAIN" -d "$DOMAIN"
    sed "s/__DOMAIN__/$DOMAIN/g" "$RELEASE/deploy/nginx.conf" | sudo tee "$SITE" >/dev/null
    sudo nginx -t
    sudo systemctl reload nginx

    curl -fsS "https://$DOMAIN/api/health"
    curl -fsSI "https://$DOMAIN/"
)
```

Первый конфиг обслуживает только проверку ACME по HTTP. Второй перенаправляет обычные HTTP-запросы на HTTPS. Frontend и `/api/*` работают с одного адреса. `/docs` доступен для ссылки из уведомления. Административные операции сохраняют Bearer-авторизацию. API по своим старым путям без `/api` остаётся доступен только локально. [Nginx reverse proxy](https://nginx.org/en/docs/http/ngx_http_proxy_module.html) · [Certbot webroot](https://certbot.eff.org/instructions?ws=webproduct&os=ubuntufocal)

Установите обработчик успешного продления, включите таймер и проверьте его:

```bash
sudo install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\n/usr/sbin/nginx -t && /usr/bin/systemctl reload nginx\n' |
    sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-reglament-nginx >/dev/null
sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/reload-reglament-nginx
sudo systemctl enable --now certbot.timer
sudo certbot renew --dry-run
```

## 5. Лимиты и SMTP

Nginx ограничивает размер тела 16 КиБ, частоту API и отдельно частоту записывающих запросов с одного IP. Прокси передаёт фактический IP клиента, а Uvicorn доверяет только локальному прокси. Backend дополнительно ограничивает отправки в минуту и попытки LLM за 24 часа через SQLite. Начальные значения — 12 отправок в минуту и 80 попыток LLM в сутки на базу. Сервис использует одну попытку на сообщение, выход ограничен 512 токенами. Лимит вызовов не задаёт денежный предел. Настройте отдельный ключ и лимит расходов у провайдера, если эта функция поддерживается. [Лимиты Nginx](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html)

Для Яндекс SMTP используйте один из режимов: `SMTP_PORT=465`, `SMTP_USE_SSL=1`, `SMTP_USE_TLS=0` или `SMTP_PORT=587`, `SMTP_USE_SSL=0`, `SMTP_USE_TLS=1`. Заполните логин, пароль приложения, отправителя и `QUALITY_EMAIL`. `PUBLIC_APP_URL` должен содержать публичный адрес HTTPS. Включите `SMTP_ENABLED=1` после проверки разрешённого получателя и перезапустите сервис. Системные и добавленные администратором корневые сертификаты дополняются `certifi`. Проверяются и цепочка сертификатов, и имя хоста. Устанавливать `SSL_CERT_FILE` для обычного запуска не требуется. [Настройки Яндекс Почты](https://yandex.ru/support/yandex-360/customers/mail/ru/mail-clients/others)

Без отправки или аутентификации можно проверить доступность обоих вариантов:

```bash
timeout 12 openssl s_client -connect smtp.yandex.ru:465 -servername smtp.yandex.ru \
    -verify_hostname smtp.yandex.ru -verify_return_error -brief </dev/null
timeout 12 openssl s_client -starttls smtp -connect smtp.yandex.ru:587 -servername smtp.yandex.ru \
    -verify_hostname smtp.yandex.ru -verify_return_error -brief </dev/null
```

## 6. Подключить настоящий поиск после интеграции

`deploy.app` передаёт существующему `create_app` фабрику `retrieval.agent_adapter.retrieval_factory` только при `REGLAMENT_MODE=retrieval`. Фабрика получает действующий `ExpertPublicationLookup`. Режим не подменяется демонстрационным при сбое.

В интеграционном выпуске положите четыре DOCX в `documents/`, подготовьте рабочий реестр `documents/registry.json` и перенесите заранее подготовленные веса E5 в `models/`. Команды и требуемую ревизию сверяйте с `docs/retrieval-agent.md` принятого коммита адаптера. Для проверенной локальной ссылки ветки поиска используются следующие команды после ручного утверждения реестра:

```bash
cd /opt/reglament-ai/current
sudo -u reglament .venv/bin/python -B -m retrieval.index \
    --documents /var/lib/reglament-ai/documents \
    --registry /var/lib/reglament-ai/documents/registry.json \
    --index /var/lib/reglament-ai/index --model-root /var/lib/reglament-ai/models
sudo -u reglament .venv/bin/python -B -m retrieval.index --index /var/lib/reglament-ai/index --check
```

Запускайте эти команды до включения рабочего режима или в окно обслуживания. В проверенной версии адаптера чтение регламентов реализовано, запись и отзыв экспертных ответов пока возвращают ошибку. Подтвердите оба действия и восстановление после перезапуска на финальной версии. Проверка `retrieval=configured` подтверждает подключение адаптера. Корректность цитат и индексации проверяется отдельными сценариями.

## 7. Резервное копирование, восстановление и обновление

Остановите приложение и остановите или дождитесь завершения любых внешних задач индексации. Резервная копия включает SQLite через API резервного копирования, документы, индекс и E5. Файл конфигурации храните отдельно в зашифрованном хранилище секретов. Архив содержит корпоративные данные. Храните его с ограниченным доступом и переносите за пределы VPS по защищённому каналу. Не оставляйте единственную копию на том же диске. [SQLite Online Backup API](https://www.sqlite.org/backup.html)

```bash
sudo systemctl stop reglament-ai
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARCHIVE="/var/backups/reglament-ai/state-$STAMP.tar.gz"
sudo python3 /opt/reglament-ai/current/deploy/state.py backup --archive "$ARCHIVE"
sudo sha256sum "$ARCHIVE"
sudo systemctl start reglament-ai
```

Для восстановления остановите сервис и поставьте в конфигурации `SMTP_ENABLED=0`. Проверьте контрольную сумму архива по сохранённой отдельно. Скрипт восстанавливает только в новый каталог, проверяет SQLite и не перезаписывает текущие данные:

```bash
sudo systemctl stop reglament-ai
NEW="/var/lib/reglament-ai-restore-$(date -u +%Y%m%dT%H%M%SZ)"
sudo python3 /opt/reglament-ai/current/deploy/state.py restore --archive "$ARCHIVE" --state-dir "$NEW"
sudo chown -R root:reglament "$NEW"
sudo chmod -R u=rwX,g=rX,o= "$NEW"
sudo chown -R reglament:reglament "$NEW/storage" "$NEW/index"
sudo mv /var/lib/reglament-ai "/var/lib/reglament-ai-before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
sudo mv "$NEW" /var/lib/reglament-ai
sudo systemctl start reglament-ai
```

Перед включением SMTP заново проверьте очередь. В старом архиве письмо может числиться ожидающим, хотя после снятия копии оно было доставлено. Журнал лимитов также вернётся к моменту копии. Проверьте остаток бюджета у провайдера и статус известных публикаций.

После новых коммитов обновите проверочную копию, выберите утверждённый SHA и соберите новый каталог по шагу 1. До переключения сделайте резервную копию. Если сборка завершилась ошибкой, `current` остаётся прежним. Разберите причину, затем переместите незавершённый каталог выпуска, прежде чем повторять сборку того же SHA. Примените изменения systemd или Nginx при необходимости, затем переключите `current` командой из шага 3 и выполните `sudo systemctl restart reglament-ai`. Проверьте HTTPS, `/api/health` и один разрешённый сценарий поиска. Сохраните предыдущий выпуск для отката. При несовместимом изменении данных возвращайте совместимый выпуск вместе с проверенной копией состояния.

## 8. Состояние и ресурсы

```bash
sudo systemctl status reglament-ai nginx --no-pager
sudo journalctl -u reglament-ai -n 100 --no-pager
sudo journalctl -u reglament-ai -f
sudo journalctl -u nginx -n 50 --no-pager
sudo systemctl show reglament-ai -p MemoryCurrent -p CPUUsageNSec -p NRestarts
curl -fsS http://127.0.0.1:8000/api/health
sudo ss -lntp
free -h
df -h /var/lib/reglament-ai
sudo du -sh /var/lib/reglament-ai/{storage,index,documents,models}
```

Проверьте память после загрузки E5 и использование диска после индексации. Один процесс держит один экземпляр модели поиска. Увеличение числа процессов требует отдельного замера и может повысить конкуренцию за запись SQLite.
