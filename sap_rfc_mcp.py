"""
SAP RFC MCP Server (Multi-Client & Multi-System)
Единый транспорт: Streamable HTTP → http://127.0.0.1:8000/mcp/
Клиенты: Qwen Desktop, Codex, OpenCode, Antigravity
Правила работы систем (DEV/Sandbox) — согласно AGENTS.md

Секреты хранятся ТОЛЬКО в файле .env рядом со скриптом:
    SAP_USER_DEV=...
    SAP_PASSWORD_DEV=...
    SAP_USER_SANDBOX=...
    SAP_PASSWORD_SANDBOX=...
"""
import os
import sys

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


def get_sap_connection(system_name: str) -> Connection:
    """Создает подключение к указанной системе"""
    config = SYSTEMS.get(str(system_name).lower())
    if not config:
        raise ValueError(f"Неизвестная система: {system_name}. Используйте 'dev' или 'sandbox'.")
    return Connection(**config)


@mcp.tool()
def get_table_structure(table_name: str, system: str = "dev", max_fields: int = 50) -> str:
    """Получает структуру таблицы SAP (только DEV)."""
    if str(system).lower() != "dev":
        return "⚠️ Структуры таблиц берутся только из системы 'dev'."
    table_name = table_name.upper()
    conn = get_sap_connection("dev")
    try:
        result = conn.call("DDIF_FIELDINFO_GET", TABNAME=table_name)
    finally:
        conn.close()
    fields = result.get("DFIES_TAB", [])
    if not fields:
        return f"❌ Таблица {table_name} не найдена в DEV"
    output = f"📊 **Структура таблицы {table_name} (DEV)**\n"
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
    """Читает бизнес-данные из таблицы SAP (только Sandbox)."""
    if str(system).lower() != "sandbox":
        return "⚠️ Бизнес-данные читаются только из системы 'sandbox'."
    table_name = table_name.upper()
    conn = get_sap_connection("sandbox")
    try:
        options = [{"TEXT": where_clause}] if where_clause else []
        field_list = [{"FIELDNAME": x.strip()} for x in fields.split(",") if x.strip()] if fields else []
        result = conn.call("RFC_READ_TABLE", QUERY_TABLE=table_name, DELIMITER="|",
                           OPTIONS=options, FIELDS=field_list, ROWCOUNT=int(max_rows))
    finally:
        conn.close()
    if not result.get("DATA"):
        return f"❌ Данные не найдены в {table_name} (Sandbox)"
    headers = [f["FIELDNAME"] for f in result.get("FIELDS", [])]
    output = f"📋 **Данные из {table_name} (Sandbox)**\n" + " | ".join(headers) + "\n" + "-" * 80 + "\n"
    for row in result["DATA"]:
        output += " | ".join(row["WA"].split("|")) + "\n"
    return output


@mcp.tool()
def search_tables(pattern: str, system: str = "dev", max_results: int = 10) -> str:
    """Ищет таблицы SAP по маске (только DEV)."""
    mask = pattern.upper().replace("*", "%")
    conn = get_sap_connection("dev")
    try:
        result = conn.call("RFC_READ_TABLE", QUERY_TABLE="DD02L", DELIMITER="|",
                           OPTIONS=[{"TEXT": f"TABNAME LIKE '{mask}' AND AS4LOCAL='A'"}],
                           FIELDS=[{"FIELDNAME": "TABNAME"}, {"FIELDNAME": "DDTEXT"}],
                           ROWCOUNT=int(max_results))
    finally:
        conn.close()
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
    mcp.run(transport="streamable-http", host=host, port=port)
