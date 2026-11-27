# Круглосуточный стенд на Windows 10

Команды выполняются из корня проекта в обычном Windows PowerShell. Планировщик запускает две независимые задачи от той же учётной записи Windows, которой принадлежат проект и конфигурация ngrok. Запущенные задачи продолжают работу при закрытии терминала, браузера и Codex.

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

## ngrok для жюри

Проверьте закреплённый hostname в **ngrok Dashboard → Domains**. Подготовьте локальный файл `storage/windows/stand.json`, заменив значения домена и адресов на согласованные. Пока оставьте `publicEnabled` равным `false`.

```json
{
  "publicEnabled": false,
  "publicHost": "assigned.ngrok-free.app",
  "judges": ["judge@example.invalid"]
}
```

Агент по умолчанию используется из Microsoft Store той же учётной записи. Если пакет Store не запускается в сеансе без входа, скачайте [standalone ngrok для Windows](https://ngrok.com/download/windows?tab=download) в постоянный каталог и укажите его абсолютный путь в `ngrokExe`. При необходимости укажите абсолютный путь к существующему YAML с токеном в `ngrokConfig`. Для Store логический путь по умолчанию — `%LOCALAPPDATA%\ngrok\ngrok.yml`. Токен хранится в конфигурации агента.

1. Прогоните проверку файла и конфигурации агента без туннеля:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\stand.ps1 Tunnel -Check
```

2. Только после согласования передачи регламентов выключите **Full Capture** в Dashboard → Traffic Inspector. В `stand.json` явно включите `publicEnabled`. Перезапустите backend, затем разрешите и запустите задачу туннеля:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\app.ps1 Stop -Mode retrieval-proxyapi
Start-ScheduledTask -TaskName 'Reglament AI Backend'
Enable-ScheduledTask -TaskName 'Reglament AI Tunnel'
Start-ScheduledTask -TaskName 'Reglament AI Tunnel'
```

Разовый публичный запуск вместо задачи туннеля выполняет ту же команду с доменом из `stand.json` и обязательной OAuth-политикой:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\stand.ps1 Tunnel
```

Она вызывает `ngrok http http://127.0.0.1:8002 --url "https://$standHost" --inspect=false --config $agentConfig --traffic-policy-file $policyPath`. Перед этим проверяет, что backend принимает только указанный публичный Host по HTTPS и возвращает 403 на административном маршруте в публичном режиме. OAuth использует согласованный список адресов, а без флага задача завершится без запуска агента. Проверьте разрешённый и посторонний аккаунты, внешний ответ 403 на `/api/quality/appeals`, `/docs` и `/.env`. Служба качества доступна локально с отдельным токеном. Флаг инспекции относится к локальному инспектору, а обработка трафика и настройки хранения на стороне ngrok проверяются отдельно.

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

[Планировщик Windows](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/register-scheduledtask?view=win10-ps) · [OAuth ngrok](https://ngrok.com/docs/gateway/traffic-policy/examples/oauth-protection) · [HTTP-инспекция ngrok](https://ngrok.com/docs/obs/traffic-inspection/http-requests)

