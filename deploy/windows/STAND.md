# Круглосуточный стенд на Windows 10

Команды выполняются из корня проекта в обычном Windows PowerShell. Для запуска до входа в Windows в локальном stand.json заданы абсолютные пути к подписанному standalone ngrok и к существующему файлу конфигурации агента. Это исключает зависимость от псевдонима Microsoft Store и перенаправления пути настроек в интерактивном сеансе. Токен остаётся в исходном файле агента.

Планировщик запускает две независимые задачи от той же учётной записи Windows, которой принадлежат проект и конфигурация ngrok. Запущенные задачи продолжают работу при закрытии терминала, браузера и Codex.

## Постоянный Python

Новый интерпретатор и его виртуальное окружение размещаются в игнорируемом каталоге `storage/windows/runtime`. Старые `.venv` и данные остаются на месте. Проверка установленного окружения:

```powershell
Set-Location 'D:\КУЗНИЦА\AI_kuznica'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\stand.ps1 Backend -Check
```

Если нужно повторить установку на другом компьютере, установите [uv](https://docs.astral.sh/uv/getting-started/installation/) из официального релиза. Следующие команды скачивают актуальный CPython 3.12 x64 в постоянный каталог и подготавливают новое окружение без изменений в старом `.venv`. Для повторной установки пользуйтесь отдельным каталогом и переключайтесь после проверки. PyTorch устанавливается в CPU-варианте. Модель E5 и индекс из существующего состояния используются на месте.

```powershell
$runtime = Join-Path $PWD 'storage\windows\runtime'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $runtime 'python'
uv python install 3.12 --no-bin
$basePython = uv python find --managed-python 3.12
uv venv --python $basePython (Join-Path $runtime 'venv')
$python = Join-Path $runtime 'venv\Scripts\python.exe'
uv pip install --python $python --index https://download.pytorch.org/whl/cpu 'torch==2.9.1+cpu'
uv pip install --python $python -r requirements.txt -r requirements-retrieval.txt
uv pip check --python $python
& $python -B -m retrieval.index --index .\storage\windows\index --check
```

Для ручного переключения используйте `app.ps1 Restart -Mode retrieval-proxyapi -Python $python` и проверьте HTTP. После успешной проверки создайте `storage/windows/runtime/venv-ready`, чтобы обычные команды `app.ps1` использовали постоянное окружение по умолчанию. В случае отката можно явно указать `-Python .\.venv\Scripts\python.exe`.

## Автозапуск backend

1. Проверьте, что сервер отвечает локально. Запустите регистрацию задач **по своему решению** из Windows PowerShell. Введите настоящий пароль текущей Windows-учётной записи в системном диалоге. PIN Windows Hello для этого не подходит. Если Windows отказывает в создании задачи, откройте Windows PowerShell с повышением прав под той же учётной записью. Сами задачи запускаются с обычными правами.
2. Остановите ручной экземпляр backend и запустите задачу. Посмотрите локальный health. Затем при удобном времени перезагрузите компьютер и проверьте задачи после загрузки. Backend должен быть доступен до входа пользователя. Туннель после регистрации отключён.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\register-stand-tasks.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\app.ps1 Stop -Mode retrieval-proxyapi
Start-ScheduledTask -TaskName 'Reglament AI Backend'
Get-ScheduledTask -TaskName 'Reglament AI Backend','Reglament AI Tunnel' | Select-Object TaskName,State
Invoke-RestMethod 'http://127.0.0.1:8002/api/health'
```

Настроены триггер при запуске ОС, один экземпляр, отсутствие ограничения по длительности и перезапуск задачи после сбоя через минуту, до 60 попыток. Uvicorn дополнительно перезапускает существующий supervisor. Если процесс жив, но HTTP завис, используйте ручной перезапуск. Доступ к сети, питание и обновления Windows остаются внешними зависимостями.

## Публичный стенд через ngrok

В `storage/windows/stand.json` разрешена публикация только на `oak-update-dealing.ngrok-free.dev`. Пользовательские страницы и API доступны без OAuth. Панель службы качества, административный API, Swagger и файлы компьютера закрыты через туннель. Каждый диалог по-прежнему требует свой токен. Минутные лимиты и 150 попыток модели за 24 часа общие для стенда. Индексация роботами запрещена.

```json
{
  "publicEnabled": true,
  "publicHost": "oak-update-dealing.ngrok-free.dev",
  "smtpNotifications": false,
  "ngrokExe": "C:\\path\\to\\ngrok.exe",
  "ngrokConfig": "C:\\path\\to\\ngrok.yml"
}
```

Для новой установки замените оба пути в локальном файле. Проверка агента и конфигурации без запуска туннеля:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\stand.ps1 Tunnel -Check
```

Планировщик запускает `stand.ps1 Tunnel`. Скрипт использует текущую конфигурацию агента, ждёт рабочего backend и запрашивает локально маршруты с публичным Host. Агент запускается после HTTP 200 от `/api/health` и HTTP 403 от `/api/quality/appeals`. `--inspect=false` отключает HTTP-инспекцию агента. Настройки захвата трафика в Dashboard отдельные. Если Full Capture доступен в аккаунте, держите его выключенным.

Для запуска или переноса текущего backend на публичную конфигурацию выполните в PowerShell с правами администратора:

```powershell
Set-Location 'D:\КУЗНИЦА\AI_kuznica'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\app.ps1 Stop -Mode retrieval-proxyapi
Start-ScheduledTask -TaskName 'Reglament AI Backend'
Enable-ScheduledTask -TaskName 'Reglament AI Tunnel'
Start-ScheduledTask -TaskName 'Reglament AI Tunnel'
Get-ScheduledTask -TaskName 'Reglament AI Backend','Reglament AI Tunnel' |
    Select-Object TaskName,State
```

Проверьте публичную главную страницу, `/api/health` и `/robots.txt`. Снаружи `/api/quality/appeals`, `/docs` и `/.env` должны вернуть 403. При завершении агента скрипт снова проверяет backend и запускает ngrok через 10 секунд. Если завершился сам PowerShell, перезапуск зависит от настроек задачи в Планировщике. Для запланированной остановки сначала отключите задачу туннеля, затем остановите её.

Для диагностики автозапуска проверьте время фактического выполнения задачи и её код завершения:

```powershell
Get-ScheduledTaskInfo -TaskName 'Reglament AI Tunnel' |
    Select-Object LastRunTime,LastTaskResult
```

Журнал запуска и завершения агента находится в storage/windows/tunnel-lifecycle.log. После перезагрузки проверьте, что публичный `/api/health` отвечает с `model=proxyapi-qwen`.

### SMTP

SMTP выключен. Для включения подтвердите адрес служебного почтового ящика в локальном `.env`, проверьте настройки TLS и установите `SMTP_MIN_INTERVAL_SECONDS=300`. В `stand.json` задайте `"smtpNotifications": true` и перезапустите задачу backend. В рабочем режиме письмо содержит только идентификатор обращения. Текст обращения читается в локальной панели. Интервал общий для очереди и сохраняется в SQLite между перезапусками, включая попытки повторной отправки. По умолчанию допускается не более одной попытки SMTP за 5 минут.

## Управление и резервная копия

Локальный запуск без планировщика проводится при выключенном публичном режиме. Запускайте один экземпляр. Меняйте значение команды после `app.ps1` на `Start`, `Stop`, `Restart`, `Status` или `Logs`.

```powershell
$env:REGLAMENT_ALLOW_PUBLIC = '0'
$env:SMTP_ENABLED = '0'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\app.ps1 Status -Mode retrieval-proxyapi
```

При управлении через планировщик используйте `Start-ScheduledTask -TaskName 'Reglament AI Backend'` для запуска. Для перезапуска сначала дождитесь завершения команды `app.ps1 Stop -Mode retrieval-proxyapi` и статуса Ready в Планировщике, затем выполните `Start-ScheduledTask`.

Для остановки публикации отключите и остановите задачу туннеля, верните `publicEnabled` в `false` и перезапустите backend. Для полной остановки и резервного копирования выполните следующий блок. Если задачи ещё не зарегистрированы, выполните только `app.ps1 Stop` и `Backup`.

```powershell
Disable-ScheduledTask -TaskName 'Reglament AI Tunnel'
Stop-ScheduledTask -TaskName 'Reglament AI Tunnel'
Disable-ScheduledTask -TaskName 'Reglament AI Backend'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\app.ps1 Stop -Mode retrieval-proxyapi
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\app.ps1 Backup -Mode retrieval-proxyapi
```

Остановите также любые отдельно запущенные процессы индексации. Архив включает SQLite, полный индекс, DOCX и веса E5. Папка `backups` игнорируется Git. Храните архив на защищённом носителе. Отдельно сохраните `.env`, `storage/windows/registry.json` и, если настроен, `storage/windows/stand.json` на зашифрованном носителе. Токен ngrok сохраняется или восстанавливается отдельно через аккаунт. После обслуживания включите и запустите задачу backend. Включайте задачу туннеля снова только если публикация должна продолжаться.

## Питание

В Windows 10 откройте **Параметры → Система → Питание и спящий режим**. Для питания от сети установите сон **Никогда**. Выключение дисплея можно оставить. Если включён отдельный таймер гибернации, проверьте его в дополнительных параметрах схемы питания. Брандмауэр, Defender и обновления Windows оставьте включёнными.

[Планировщик Windows](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/register-scheduledtask?view=win10-ps) · [HTTP-инспекция ngrok](https://ngrok.com/docs/obs/traffic-inspection/http-requests)

