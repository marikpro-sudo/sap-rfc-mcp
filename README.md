# SAP RFC MCP Reader

Python MCP-сервер для чтения SAP через RFC. Транспорт — Streamable HTTP, адрес по умолчанию `http://127.0.0.1:8000/mcp/`. Используются FastMCP, PyRFC и python-dotenv. Сервер не создаёт ABAP-объекты и не выполняет ABAP через ADT.

## Инструменты

| Инструмент | Назначение | Система |
|---|---|---|
| `get_table_structure` | Поля таблицы через `DDIF_FIELDINFO_GET` | `dev` |
| `read_table_data` | Чтение через `RFC_READ_TABLE` | `sandbox` |
| `search_tables` | Поиск по маске (`Z*`) | `dev` |

Всегда передавайте `system` явно. `max_rows`, `max_fields`, `max_results` — JSON-числа, например `10`, а не строки. До чтения проверяйте существование таблицы и полей через `get_table_structure`.

## Требования

- Windows x64 и CPython **3.11 или 3.12 x64**: для этих версий есть готовые Windows wheels PyRFC 3.3.1 на [PyPI](https://pypi.org/project/pyrfc/3.3.1/#files).
- Git для клонирования приватного репозитория с авторизацией GitHub.
- SAP NetWeaver RFC SDK 7.50 x64, полученный отдельно через [SAP Support Portal](https://support.sap.com/nwrfcsdk). Нужны права на скачивание. SDK не входит в pip-пакеты и этот репозиторий.
- Windows runtime Visual C++ 2013 x64 согласно [требованиям PyRFC](https://github.com/SAP-archive/PyRFC#requirements). Другие runtime устанавливайте согласно требованиям выбранного SDK.
- Сетевой доступ к SAP DEV и Sandbox и RFC-пользователи с правами на используемые функции и таблицы. Права согласуются с Basis; широкие административные права не нужны.

**Статус PyRFC:** upstream архивирован и больше не поддерживается. Релиз 3.3.1 помечен на PyPI как yanked, поэтому в `requirements.txt` задана точная версия; pip может вывести предупреждение. Upstream ориентируется на SDK 7.50 PL12 и предупреждает, что новые патчи могут быть несовместимы. Совместимость конкретного SDK проверяйте импортом и тестовым RFC-вызовом. Версия SDK на клиенте и SAP Basis на сервере — разные вещи.

## Установка на Windows (PowerShell)

Установите Python с launcher `py`, Git и runtime. Распакуйте SDK, например в `C:\nwrfc\nwrfcsdk`; внутри должны быть `lib\sapnwrfc.dll`, `lib\sapucum.dll`, остальные поставляемые DLL и каталог `include`.

```powershell
git clone https://github.com/marikpro-sudo/sap-rfc-mcp.git
cd sap-rfc-mcp
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
Copy-Item .env.example .env
notepad .env
```

Для Python 3.11 замените `py -3.12` на `py -3.11`. Активация venv не требуется. `pip` устанавливает также транзитивные Python-зависимости; системный SAP SDK и C++ runtime устанавливаются отдельно. Диапазоны FastMCP и python-dotenv ограничивают основные версии, но не являются полным lock-файлом.

В `.env` заполните адрес, номер инстанции, мандант, пользователя и пароль **для обеих систем**. `SAPNWRFC_HOME` указывает на корень SDK, а не на `lib`. Не используйте демонстрационные адреса из примера. Значения с `#` или пробелами заключайте в кавычки. `.env` читается рядом со скриптом независимо от рабочего каталога; уже заданные переменные окружения имеют приоритет.

Проверка загрузки зависимостей без подключения к SAP:

```powershell
.\.venv\Scripts\python.exe -c "import sap_rfc_mcp; print('Imports and configuration OK')"
```

Модуль проверит конфигурацию и загрузит DLL, но не откроет SAP-соединение до вызова инструмента.

## Запуск и подключение

```powershell
.\.venv\Scripts\python.exe sap_rfc_mcp.py
```

В MCP-клиенте выберите удалённый сервер / Streamable HTTP и URL `http://127.0.0.1:8000/mcp/`. Процесс сервера должен оставаться запущенным. Обычное открытие URL в браузере не является проверкой MCP: нужен MCP-клиент. Настройка HTTP-транспорта описана в [документации FastMCP](https://gofastmcp.com/deployment/running-server).

Примеры аргументов инструментов:

```json
{"table_name": "MARA", "system": "dev", "max_fields": 20}
```

```json
{"table_name": "MARA", "system": "sandbox", "max_rows": 10, "fields": "MATNR,MTART", "where_clause": "MTART = 'FERT'"}
```

Первый пример передаётся `get_table_structure`, второй — `read_table_data` после проверки полей. Доступность данных зависит от вашей системы и прав пользователя.

По умолчанию сервер слушает только localhost. Для доступа с другого компьютера можно изменить `MCP_HOST`, но встроенной аутентификации в этом скрипте нет: доступ к MCP даёт возможность читать SAP от имени настроенных пользователей. Сетевое развёртывание требует ограничения доступа и аутентификации на внешнем шлюзе.

## Linux и сборка из исходников

Основной сценарий этого README — Windows с готовым wheel. На Linux нужен SDK для соответствующей архитектуры, компилятор C/C++, заголовки Python, Cython и инструменты сборки. Задайте `SAPNWRFC_HOME` и путь к библиотекам **до запуска Python**:

```bash
export SAPNWRFC_HOME=/opt/nwrfcsdk
export LD_LIBRARY_PATH="$SAPNWRFC_HOME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel Cython
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
# Отредактируйте .env, включая SAPNWRFC_HOME=/opt/nwrfcsdk
.venv/bin/python sap_rfc_mcp.py
```

На Windows сборка нужна только при отсутствии подходящего wheel: дополнительно установите Microsoft C++ Build Tools. Подробности платформенной сборки — в [документации PyRFC](https://sap.github.io/PyRFC/build.html). Linux-сценарий здесь не проверен.

## Ограничения исходного сервера и диагностика

| Симптом | Что проверить |
|---|---|
| `No matching distribution found` | Python 3.11/3.12 x64 и точная версия `pyrfc==3.3.1`; не заменяйте её просто на `pyrfc` |
| `DLL load failed` | Путь SDK, все DLL из поставки, разрядность Python/SDK и C++ runtime |
| Сообщение об отсутствующих переменных | Заполнение `.env` для обеих систем |
| RFC logon/communication error | VPN/маршрутизацию, адрес, sysnr, мандант и учётную запись |
| `NOT_AUTHORIZED` | RFC- и табличные права, согласованные с Basis |
| `DATA_BUFFER_EXCEEDED` | Сократите `fields`: классический `RFC_READ_TABLE` ограничен размером строки |
| Ошибка фильтра | В исходной реализации `where_clause` передаётся одной строкой OPTIONS (обычно до 72 символов); сложные длинные фильтры не разбиваются |
| Ошибка `search_tables` / `FIELD_NOT_VALID` | Исходник запрашивает `DDTEXT` из `DD02L`; в стандартном DDIC описания обычно находятся в `DD02T`. Этот известный дефект исходника требует отдельной правки и проверки структуры в DEV |

`search_tables` всегда подключается к DEV, даже если передан другой `system`. Не передавайте произвольные SQL-выражения в `pattern`: исходник вставляет маску в фильтр без экранирования. Используйте простые маски имён таблиц. Ограничения количества строк не валидируются: передавайте положительные небольшие значения; `max_rows=0` может снять ограничение RFC.

## Состав и проверка

- `sap_rfc_mcp.py` — исходный сервер с вынесенной в окружение конфигурацией и загрузкой DLL SDK на Windows.
- `requirements.txt` — прямые Python-зависимости.
- `.env.example` — шаблон без учётных данных.
- `.gitignore` — исключения для секретов, SDK, окружений и логов.

Локальный исходник в `C:\nwrfc` не изменяется при подготовке этого репозитория. В публикуемой версии адреса/манданты вынесены в `.env`, а адрес прослушивания по умолчанию изменён на `127.0.0.1`. Логика RFC-инструментов сохранена. SDK, `.env`, журналы и бизнес-данные в Git не включаются.

Проверка синтаксиса без SAP:

```powershell
.\.venv\Scripts\python.exe -m py_compile sap_rfc_mcp.py
```

Полный тест требует установленного SDK, заполненной конфигурации и вызова инструмента через MCP-клиент. Публикация репозитория сама по себе не подтверждает совместимость с конкретной SAP-системой.
