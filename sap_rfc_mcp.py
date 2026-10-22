"""
SAP RFC MCP Server (Multi-Client & Multi-System)
Единый транспорт: Streamable HTTP → http://127.0.0.1:8000/mcp/
Клиенты: Qwen Desktop, Codex, OpenCode, Antigravity
Правила работы систем (DEV / Sandbox / Prod) — согласно AGENTS.md

Секреты хранятся ТОЛЬКО в файле .env рядом со скриптом:
    SAP_USER_DEV=...
    SAP_PASSWORD_DEV=...
    SAP_USER_SANDBOX=...
    SAP_PASSWORD_SANDBOX=...
    SAP_USER_PROD=...
    SAP_PASSWORD_PROD=...

Продуктив подключается ТОЛЬКО на чтение (пользователь типа "B Система",
напр. AGENT_READ). ADT/HTTP к продуктиву не используется — только RFC.
Destinations ведутся в SM59.
"""
import json
import os
import re
import sys
from datetime import datetime

from dotenv import load_dotenv
from fastmcp import FastMCP

# Загружаем .env из каталога скрипта (не зависит от текущей директории запуска)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Держим handle открытым, чтобы SDK DLL были доступны на Windows/Python >= 3.8.
_SDK_DLL_DIR = None
if os.name == "nt" and os.getenv("SAPNWRFC_HOME"):
    _SDK_DLL_DIR = os.add_dll_directory(os.path.join(os.environ["SAPNWRFC_HOME"], "lib"))

from pyrfc import Connection

# Проверка наличия секретов при старте
_REQUIRED = [
    f"SAP_{key}_{system}"
    for system in ("DEV", "SANDBOX")
    for key in ("ASHOST", "SYSNR", "CLIENT", "USER", "PASSWORD")
]
_MISSING = [k for k in _REQUIRED if not os.getenv(k)]
if _MISSING:
    sys.exit(
        "❌ В .env отсутствуют переменные: " + ", ".join(_MISSING) +
        f"\nСоздайте файл {os.path.join(BASE_DIR, '.env')} и заполните их."
    )

mcp = FastMCP("SAP RFC Reader")

# Конфигурация систем согласно AGENTS.md (секреты — только из .env)
SYSTEMS = {
    "dev": {
        "ashost": os.getenv("SAP_ASHOST_DEV"),
        "sysnr": os.getenv("SAP_SYSNR_DEV"), "client": os.getenv("SAP_CLIENT_DEV"),
        "user": os.getenv("SAP_USER_DEV"),
        "passwd": os.getenv("SAP_PASSWORD_DEV"),
    },
    "sandbox": {
        "ashost": os.getenv("SAP_ASHOST_SANDBOX"),
        "sysnr": os.getenv("SAP_SYSNR_SANDBOX"), "client": os.getenv("SAP_CLIENT_SANDBOX"),
        "user": os.getenv("SAP_USER_SANDBOX"),
        "passwd": os.getenv("SAP_PASSWORD_SANDBOX"),
    },
}


def _prod_config():
    """Конфигурация продуктивa. Возвращает None, если PROD не настроен.

    PROD подключается опционально: пока в .env нет реквизитов продуктивa,
    сервер работает как раньше (dev + sandbox).
    """
    config = {
        "ashost": os.getenv("SAP_ASHOST_PROD"),
        "sysnr": os.getenv("SAP_SYSNR_PROD", "01"),
        "client": os.getenv("SAP_CLIENT_PROD"),
        "user": os.getenv("SAP_USER_PROD"),
        "passwd": os.getenv("SAP_PASSWORD_PROD"),
    }
    return config if all(config.values()) else None


_prod = _prod_config()
if _prod:
    SYSTEMS["prod"] = _prod

# --- Политика доступа к Продуктиву (только чтение) ---
LOG_DIR = os.path.join(BASE_DIR, "logs")
READ_LOG = os.path.join(LOG_DIR, "reads.log")
PROD_MAX_ROWS = int(os.getenv("SAP_PROD_MAX_ROWS", "50"))
PROD_REQUIRE_WHERE = os.getenv("SAP_PROD_REQUIRE_WHERE", "1") != "0"

# Блоклист: персональные и банковские данные, системные таблицы пользователей.
PROD_BLOCKED_TABLES = {
    "PASSWORD", "T002", "T002C", "T002D",
    "BNKA", "KNBK", "LFBK",
    "USR02", "USR03", "USR04", "USR05", "USR21", "USR40",
}
PROD_BLOCKED_PREFIXES = ("USR",)
_HR_INFOTYPE = re.compile(r"^PA\d{4}$")


def _prod_blocked(table_name: str) -> bool:
    """True, если таблица запрещена к чтению из PROD."""
    if table_name in PROD_BLOCKED_TABLES:
        return True
    if table_name.startswith(PROD_BLOCKED_PREFIXES):
        return True
    return bool(_HR_INFOTYPE.match(table_name))


def _conn_label(system: str) -> str:
    """Человекочитаемое описание соединения для сообщений об ошибках."""
    cfg = SYSTEMS.get(system) or {}
    return (
        f"{system.upper()} {cfg.get('ashost')}:{cfg.get('sysnr')} "
        f"клиент {cfg.get('client')}, пользователь {cfg.get('user')}"
    )


def _log_read(system: str, table: str, where: str, max_rows: int, rows: int, error: str = "") -> None:
    """Журнал чтений: кто, что, когда, с каким фильтром и сколько строк."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "system": system,
            "table": table,
            "where": where or "",
            "max_rows": max_rows,
            "rows": rows,
            "error": error,
        }
        with open(READ_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def get_sap_connection(system_name: str) -> Connection:
    """Создает подключение к указанной системе"""
    config = SYSTEMS.get(str(system_name).lower())
    if not config:
        raise ValueError(
            f"Неизвестная система: {system_name}. "
            f"Доступны: {', '.join(sorted(SYSTEMS))}."
        )
    return Connection(**config)


@mcp.tool()
def get_table_structure(table_name: str, system: str = "dev", max_fields: int = 50) -> str:
    """Получает структуру таблицы SAP (из DEV; из PROD — только для сверки структур)."""
    system = str(system).lower()
    if system not in ("dev", "prod"):
        return "⚠️ Структуры таблиц берутся из системы 'dev' (или 'prod' только для сверки)."
    table_name = table_name.upper()
    try:
        conn = get_sap_connection(system)
        try:
            result = conn.call("DDIF_FIELDINFO_GET", TABNAME=table_name)
        finally:
            conn.close()
    except Exception as e:
        return f"❌ Ошибка чтения структуры {table_name} ({_conn_label(system)}):\n{e}"
    fields = result.get("DFIES_TAB", [])
    if not fields:
        return f"❌ Таблица {table_name} не найдена в {system.upper()}"
    output = f"📊 **Структура таблицы {table_name} ({system.upper()})**\n"
    output += f"{'Поле':<20} | {'Тип':<6} | {'Длина':<6} | {'Описание'}\n" + "-" * 80 + "\n"
    for f in fields[:int(max_fields)]:
        output += (
            f"{f.get('FIELDNAME', ''):<20} | {f.get('DATATYPE', ''):<6} | "
            f"{f.get('LENG', ''):<6} | {f.get('FIELDTEXT', '')}\n"
        )
    return output


@mcp.tool()
def read_table_data(table_name: str, system: str = "sandbox", max_rows: int = 10,
                    where_clause: str = "", fields: str = "") -> str:
    """Читает бизнес-данные из таблицы SAP (Sandbox — основной источник; Prod — только чтение)."""
    system = str(system).lower()
    if system not in ("sandbox", "prod"):
        return "⚠️ Бизнес-данные читаются из системы 'sandbox' или 'prod'."
    table_name = table_name.upper()
    max_rows = int(max_rows)

    if system == "prod":
        if _prod_blocked(table_name):
            _log_read(system, table_name, where_clause, max_rows, 0, error="blocked")
            return (
                f"🚫 Таблица {table_name} заблокирована для чтения из PROD "
                "(персональные/банковские данные или системные таблицы пользователей). "
                "Если доступ действительно нужен — требуется отдельное согласование."
            )
        if PROD_REQUIRE_WHERE and not where_clause.strip():
            return (
                "⚠️ Для PROD обязателен where_clause: чтение без фильтра запрещено. "
                "Укажи условие WHERE (например: MANDT = '303' AND BUKRS = '1000')."
            )
        if max_rows > PROD_MAX_ROWS:
            max_rows = PROD_MAX_ROWS

    try:
        conn = get_sap_connection(system)
        try:
            options = [{"TEXT": where_clause}] if where_clause else []
            field_list = [{"FIELDNAME": x.strip()} for x in fields.split(",") if x.strip()] if fields else []
            result = conn.call("RFC_READ_TABLE", QUERY_TABLE=table_name, DELIMITER="|",
                               OPTIONS=options, FIELDS=field_list, ROWCOUNT=max_rows)
        finally:
            conn.close()
    except Exception as e:
        _log_read(system, table_name, where_clause, max_rows, 0, error=str(e))
        return f"❌ Ошибка чтения {table_name} ({_conn_label(system)}):\n{e}"

    data = result.get("DATA", [])
    _log_read(system, table_name, where_clause, max_rows, len(data))
    if not data:
        return f"❌ Данные не найдены в {table_name} ({system.upper()})"
    headers = [f["FIELDNAME"] for f in result.get("FIELDS", [])]
    output = f"📋 **Данные из {table_name} ({system.upper()})**\n" + " | ".join(headers) + "\n" + "-" * 80 + "\n"
    for row in data:
        output += " | ".join(row["WA"].split("|")) + "\n"
    return output


@mcp.tool()
def search_tables(pattern: str, system: str = "dev", max_results: int = 10) -> str:
    """Ищет таблицы SAP по маске (только DEV)."""
    mask = pattern.upper().replace("*", "%")
    try:
        conn = get_sap_connection("dev")
        try:
            result = conn.call("RFC_READ_TABLE", QUERY_TABLE="DD02L", DELIMITER="|",
                               OPTIONS=[{"TEXT": f"TABNAME LIKE '{mask}' AND AS4LOCAL='A'"}],
                               FIELDS=[{"FIELDNAME": "TABNAME"}, {"FIELDNAME": "DDTEXT"}],
                               ROWCOUNT=int(max_results))
        finally:
            conn.close()
    except Exception as e:
        return f"❌ Ошибка поиска таблиц ({_conn_label('dev')}):\n{e}"
    output = f"🔍 **Таблицы по маске {mask} (DEV)**\n"
    for row in result.get("DATA", []):
        parts = row["WA"].split("|")
        if len(parts) >= 2:
            output += f"- **{parts[0]}**: {parts[1]}\n"
    return output


if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8000"))
    print("🚀 SAP RFC MCP Server (FastMCP, Streamable HTTP only)")
    print(f"📡 Эндпоинт: http://{host}:{port}/mcp/")
    print(f"🔌 Системы: {', '.join(sorted(SYSTEMS))}")
    mcp.run(transport="streamable-http", host=host, port=port)
