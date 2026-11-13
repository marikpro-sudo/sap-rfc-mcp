"""
SAP RFC MCP Server (Multi-Client & Multi-System)
Единый транспорт: Streamable HTTP → http://127.0.0.1:8000/mcp/
Клиенты: Claude, Qwen Desktop, Codex, OpenCode, Antigravity
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

Режимы чтения данных (Sandbox и Prod):
    read_table_data(output="inline") — строки в ответе (малые лимиты);
    read_table_data(output="file")   — CSV в SAP_EXPORT_DIR, в ответе путь и первые 10 строк;
    aggregate_table_data             — count/sum/min/max по группам, подсчёт на стороне сервера.
Лимиты и тайм-ауты — в .env (см. .env.example). Все обращения пишутся в logs/reads.log.
"""
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Union

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


# --- Настройки лимитов (все — из окружения, значения по умолчанию ниже) ---
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        sys.exit(f"❌ {name}={raw!r} в .env: нужно целое число.")
    if value <= 0:
        sys.exit(f"❌ {name}={raw!r} в .env: нужно число больше 0.")
    return value


DATA_SYSTEMS = ("sandbox", "prod")

# Таблицы репозитория, которые можно читать из DEV (бизнес-данных в DEV нет, но запросы,
# каталог объектов и перекрёстные ссылки новых объектов есть только там).
# Список закрытый; переопределяется через SAP_DEV_REPO_TABLES (имена через запятую).
_DEV_REPO_DEFAULT = ("E070,E071,E07T,TADIR,TRDIR,D010INC,TFDIR,ENLFDIR,TSTC,"
                     "DD02L,DD02T,DD03L,WBCROSSGT,CROSS,DWINACTIV")
DEV_REPO_TABLES = frozenset(
    t.strip().upper() for t in (os.getenv("SAP_DEV_REPO_TABLES") or _DEV_REPO_DEFAULT).split(",") if t.strip()
)
LIMITS = {
    "dev": {
        "inline": _env_int("SAP_LIMIT_INLINE_DEV", 200),
        "inline_narrow": _env_int("SAP_LIMIT_INLINE_NARROW_DEV", 1_000),
        "file": _env_int("SAP_LIMIT_FILE_DEV", 20_000),
        "scan": _env_int("SAP_LIMIT_SCAN_DEV", 200_000),
        "timeout": _env_int("SAP_TIMEOUT_DEV", 120),
    },
    "prod": {
        # SAP_PROD_MAX_ROWS — прежнее имя настройки, учитывается для совместимости
        "inline": _env_int("SAP_LIMIT_INLINE_PROD", _env_int("SAP_PROD_MAX_ROWS", 50)),
        "inline_narrow": _env_int("SAP_LIMIT_INLINE_NARROW_PROD", 500),
        "file": _env_int("SAP_LIMIT_FILE_PROD", 20_000),
        "scan": _env_int("SAP_LIMIT_SCAN_PROD", 200_000),
        "timeout": _env_int("SAP_TIMEOUT_PROD", 120),
    },
    "sandbox": {
        "inline": _env_int("SAP_LIMIT_INLINE_SANDBOX", 200),
        "inline_narrow": _env_int("SAP_LIMIT_INLINE_NARROW_SANDBOX", 1_000),
        "file": _env_int("SAP_LIMIT_FILE_SANDBOX", 100_000),
        "scan": _env_int("SAP_LIMIT_SCAN_SANDBOX", 1_000_000),
        "timeout": _env_int("SAP_TIMEOUT_SANDBOX", 300),
    },
}
NARROW_MAX_FIELDS = _env_int("SAP_NARROW_MAX_FIELDS", 6)  # повышенный лимит inline при fields ≤ N
PAGE_ROWS = _env_int("SAP_PAGE_ROWS", 20_000)             # строк за один вызов RFC_READ_TABLE
WHERE_MAX_LEN = _env_int("SAP_WHERE_MAX_LEN", 1_000)
EXPORT_DIR = os.path.abspath(os.getenv("SAP_EXPORT_DIR") or os.path.join(BASE_DIR, "exports"))
EXPORT_KEEP_DAYS = _env_int("SAP_EXPORT_KEEP_DAYS", 30)
PROD_REQUIRE_WHERE = os.getenv("SAP_PROD_REQUIRE_WHERE", "1") != "0"
DEV_REQUIRE_WHERE = os.getenv("SAP_DEV_REQUIRE_WHERE", "1") != "0"
SEARCH_MAX_RESULTS = 200

MAX_GROUP_BY = 3
MAX_GROUPS_SHOWN = 200
FILE_PREVIEW_ROWS = 10
ROW_WIDTH_MAX = 512  # TAB512: ширина строки результата RFC_READ_TABLE

LOG_DIR = os.path.join(BASE_DIR, "logs")
READ_LOG = os.path.join(LOG_DIR, "reads.log")

# Блоклист: персональные и банковские данные, системные таблицы пользователей.
# Действует для всех систем с данными (sandbox, prod) и всех режимов (inline, file, aggregate).
BLOCKED_TABLES = {"PASSWORD", "BNKA", "KNBK", "LFBK"}
BLOCKED_PREFIXES = ("USR", "T002")
_HR_INFOTYPE = re.compile(r"^PA\d{4}$")

# DDIC-типы, которые можно суммировать
NUMERIC_DDIC = {
    "CURR", "QUAN", "DEC", "INT1", "INT2", "INT4", "INT8", "FLTP", "PREC",
    "D16D", "D34D", "D16R", "D34R", "D16N", "D34N", "DF16_DEC", "DF34_DEC", "DF16_RAW", "DF34_RAW",
}
# Внутренние ABAP-типы числовых полей в ответе RFC_READ_TABLE (FIELDS-TYPE)
NUMERIC_INTTYPE = {"P", "F", "I", "b", "s", "8", "a", "e"}

_SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud",
                 "yandex.disk", "yandexdisk", "box sync", "sharepoint", "mega")


class ToolError(Exception):
    """Понятный отказ: текст уходит агенту, code — в журнал."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code or message


def _blocked(table_name: str) -> bool:
    """True, если таблица запрещена к чтению (все системы, все режимы)."""
    if table_name in BLOCKED_TABLES:
        return True
    if table_name.startswith(BLOCKED_PREFIXES):
        return True
    return bool(_HR_INFOTYPE.match(table_name))


# Прежнее имя функции — для совместимости
_prod_blocked = _blocked


def _conn_label(system: str) -> str:
    """Человекочитаемое описание соединения для сообщений об ошибках."""
    cfg = SYSTEMS.get(system) or {}
    return (
        f"{system.upper()} {cfg.get('ashost')}:{cfg.get('sysnr')} "
        f"клиент {cfg.get('client')}, пользователь {cfg.get('user')}"
    )


def _log_read(system: str, table: str, where: str, max_rows=None, rows: int = 0,
              error: str = "", **extra) -> None:
    """Журнал чтений: время, система, таблица, условие, режим, строки, файл, ошибка."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "system": system,
            "table": table,
            "where": where or "",
            "mode": extra.pop("mode", "inline"),
            "max_rows": max_rows,
            "rows": rows,
        }
        record.update({k: v for k, v in extra.items() if v not in (None, "")})
        record["error"] = error
        with open(READ_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


OPTIONS_WIDTH = 72  # RFC_DB_OPT-TEXT = CHAR72; всё длиннее SAP молча обрезает


def _split_options(where: str, width: int = OPTIONS_WIDTH) -> list:
    """Разбивает WHERE на строки OPTIONS для RFC_READ_TABLE (≤ 72 символов каждая).

    SAP склеивает строки OPTIONS, но токен не может переходить через границу строки,
    поэтому режем только по пробелам и после запятых вне литералов. Литерал '...'
    (включая экранированные '') никогда не разрывается.

    До вызова RFC проверяет незакрытые кавычки, баланс скобок и токены длиннее 72
    символов — иначе RFC_READ_TABLE падает дампом SAPSQL_PARSE_ERROR в ST22.
    """
    where = where.strip()
    if not where:
        return []

    tokens, cur, in_lit, depth = [], "", False, 0
    i = 0
    while i < len(where):
        ch = where[i]
        if in_lit:
            cur += ch
            if ch == "'":
                if i + 1 < len(where) and where[i + 1] == "'":  # экранированная кавычка ''
                    cur += "'"
                    i += 1
                else:
                    in_lit = False
        elif ch == "'":
            in_lit = True
            cur += ch
        elif ch.isspace():  # пробелы/переводы строк вне литерала — разделители
            if cur:
                tokens.append(cur)
            cur = ""
        elif ch == ",":
            tokens.append(cur + ",")
            cur = ""
        else:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    raise ValueError("лишняя закрывающая скобка ')'")
            cur += ch
        i += 1
    if in_lit:
        raise ValueError("незакрытый литерал: не хватает одинарной кавычки '")
    if depth:
        raise ValueError("несбалансированные скобки")
    if cur:
        tokens.append(cur)

    rows, line = [], ""
    for t in tokens:
        if len(t) > width:
            raise ValueError(f"токен длиннее {width} символов, его нельзя перенести: {t[:40]}...")
        cand = f"{line} {t}" if line else t
        if len(cand) <= width:
            line = cand
        else:
            rows.append({"TEXT": line})
            line = t
    if line:
        rows.append({"TEXT": line})
    return rows


def get_sap_connection(system_name: str) -> Connection:
    """Создает подключение к указанной системе"""
    config = SYSTEMS.get(str(system_name).lower())
    if not config:
        raise ValueError(
            f"Неизвестная система: {system_name}. "
            f"Доступны: {', '.join(sorted(SYSTEMS))}."
        )
    return Connection(**config)


# --- Проверки параметров ---
def _to_int(value, name: str) -> int:
    """Целое > 0. Строка из цифр ("10") принимается, остальное — понятная ошибка."""
    if isinstance(value, bool):
        raise ToolError(f"⚠️ {name} должен быть целым числом, получено {value!r}.", f"invalid {name}")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    elif isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        result = int(value)
    else:
        raise ToolError(f"⚠️ {name} должен быть целым числом, например 10; получено {value!r}.",
                        f"invalid {name}")
    if result <= 0:
        raise ToolError(f"⚠️ {name} должен быть больше 0 (0 снял бы ограничение строк).",
                        f"invalid {name}")
    return result


_NAME_RE = re.compile(r"^[A-Z0-9_/]+$")


def _parse_names(value: str, what: str) -> list:
    """'BELNR, gjahr' → ['BELNR', 'GJAHR'] без повторов."""
    result = []
    for item in (value or "").split(","):
        name = item.strip().upper()
        if not name:
            continue
        if not _NAME_RE.match(name):
            raise ToolError(f"⚠️ Некорректное имя поля в {what}: {item.strip()!r}.", f"invalid {what}")
        if name not in result:
            result.append(name)
    return result


def _check_data_system(system: str, table: str = "") -> str:
    system = str(system).strip().lower()
    if system == "dev" and table in DEV_REPO_TABLES:
        return system  # таблица репозитория: читается из DEV (where_clause обязателен)
    if system not in DATA_SYSTEMS:
        raise ToolError(
            "⚠️ Бизнес-данные читаются из системы 'sandbox' или 'prod'. "
            "Из 'dev' доступны только таблицы репозитория: " + ", ".join(sorted(DEV_REPO_TABLES)) + ".",
            "invalid system",
        )
    if system not in SYSTEMS:
        raise ToolError("⚠️ PROD не настроен в .env этого сервера.", "prod not configured")
    return system


def _check_policy(system: str, table: str, where: str) -> list:
    """Блоклист, обязательный WHERE в Проде, длина и синтаксис WHERE. Возвращает OPTIONS."""
    if _blocked(table):
        raise ToolError(
            f"🚫 Таблица {table} заблокирована для чтения (персональные/банковские данные "
            "или системные таблицы пользователей) — в любом режиме и любой системе. "
            "Если доступ действительно нужен — требуется отдельное согласование.",
            "blocked",
        )
    need_where = (system == "prod" and PROD_REQUIRE_WHERE) or (system == "dev" and DEV_REQUIRE_WHERE)
    if need_where and not where.strip():
        example = "TRKORR = 'MHDK900001'" if system == "dev" else "GJAHR = '2026' AND BUKRS = '1000'"
        raise ToolError(
            f"⚠️ Для {system.upper()} обязателен where_clause: чтение без фильтра запрещено (во всех режимах). "
            f"Укажи условие WHERE, например: {example}.",
            "where required",
        )
    if len(where) > WHERE_MAX_LEN:
        raise ToolError(
            f"⚠️ where_clause длиной {len(where)} символов больше предела {WHERE_MAX_LEN}. "
            "Сократи условие: IN (...) вместо цепочек OR, BETWEEN для диапазонов.",
            "where too long",
        )
    try:
        return _split_options(where)
    except ValueError as e:
        raise ToolError(
            f"⚠️ Некорректный where_clause ({e}). Запрос в SAP не отправлялся.\n"
            "Литералы — в одинарных кавычках, пробелы вокруг операторов: BUKRS = '1000'.",
            f"invalid where: {e}",
        )


# --- Числа SAP ---
def _parse_number(raw: str) -> Decimal:
    """'8646.91-' → Decimal('-8646.91'). Пустое значение — 0."""
    s = (raw or "").strip().replace(" ", "")
    if not s:
        return Decimal(0)
    neg = False
    if s.endswith("-"):
        neg, s = True, s[:-1]
    elif s.startswith("-"):
        neg, s = True, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"не число: {raw!r}")
    return -value if neg else value


def _fmt_number(value: Decimal) -> str:
    if value.is_zero():
        value = value.copy_abs()
    return format(value, "f")


def _normalize(value: str, inttype: str) -> str:
    """Числа — со знаком слева ('-8646.91'); остальное — как есть (без пробелов по краям)."""
    if inttype in NUMERIC_INTTYPE and value:
        try:
            return _fmt_number(_parse_number(value))
        except ValueError:
            return value
    return value


# --- Метаданные ---
def _field_info(system: str, table: str) -> dict:
    """Описание полей (DDIF_FIELDINFO_GET): из целевой системы, при неудаче — из DEV."""
    last_error = None
    for name in dict.fromkeys((system, "dev")):
        if name not in SYSTEMS:
            continue
        try:
            conn = get_sap_connection(name)
            try:
                result = conn.call("DDIF_FIELDINFO_GET", TABNAME=table)
            finally:
                conn.close()
        except Exception as e:
            last_error = e
            continue
        fields = {
            f["FIELDNAME"]: f for f in result.get("DFIES_TAB", [])
            if f.get("FIELDNAME") and not f["FIELDNAME"].startswith(".")
        }
        if fields:
            return fields
    raise ToolError(f"❌ Не удалось получить описание полей {table}: {last_error or 'таблица не найдена'}",
                    f"no field info: {last_error}")


def _key_fields(info: dict) -> list:
    """Ключ таблицы без манданта — для контроля повторов при постраничном чтении."""
    return [n for n, f in info.items()
            if f.get("KEYFLAG") == "X" and f.get("DATATYPE") != "CLNT" and n != "MANDT"]


# Типы ключевых полей, которые сравниваются как строки и в SAP, и в Python
_RANGE_KEY_TYPES = {"CHAR", "NUMC", "DATS", "TIMS", "CUKY", "UNIT", "LANG", "ACCP", "CLNT", "SSTR"}


def _range_keys(info: dict) -> tuple:
    """Ведущие поля ключа символьных типов для деления на диапазоны.

    Возвращает (поля, это_весь_ключ). Если весь ключ — повтор строки означает ошибку разбиения.
    """
    keys, usable = _key_fields(info), []
    for name in keys:
        if info[name].get("DATATYPE") not in _RANGE_KEY_TYPES:
            break
        usable.append(name)
    return usable, bool(usable) and len(usable) == len(keys)


def _check_fields_exist(info: dict, names: list, table: str) -> None:
    unknown = [n for n in names if n not in info]
    if unknown:
        raise ToolError(f"⚠️ В таблице {table} нет полей: {', '.join(unknown)}. "
                        "Проверь имена через get_table_structure (system='dev').",
                        "unknown fields: " + ",".join(unknown))


def _width_hint(system: str, table: str, fieldnames: list) -> str:
    """Подсказка при DATA_BUFFER_EXCEEDED: самые широкие поля."""
    try:
        info = _field_info(system, table)
    except Exception:
        return ""
    names = fieldnames or list(info)
    widths = sorted(((max(int(info[n].get("OUTPUTLEN") or 0), int(info[n].get("LENG") or 0)), n)
                     for n in names if n in info), reverse=True)
    total = sum(w for w, _ in widths)
    top = ", ".join(f"{n} ({w})" for w, n in widths[:5])
    return f" Примерная ширина выбранных полей ≈ {total}; самые широкие: {top}."


# --- Чтение ---
def _call_read(conn, system: str, table: str, options: list, fieldnames: list,
               rowcount: int, rowskips: int, deadline: float):
    """Один вызов RFC_READ_TABLE. Возвращает (layout, rows); значения разобраны по OFFSET/LENGTH."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _timeout_error(system)
    params = dict(QUERY_TABLE=table, DELIMITER="", OPTIONS=options,
                  FIELDS=[{"FIELDNAME": f} for f in fieldnames],
                  ROWCOUNT=rowcount, ROWSKIPS=rowskips)
    try:
        result = _rfc_call(conn, "RFC_READ_TABLE", remaining, **params)
    except Exception as e:
        text = str(e)
        if "DATA_BUFFER_EXCEEDED" in text:
            raise ToolError(
                f"⚠️ Строка результата шире {ROW_WIDTH_MAX} символов (ограничение RFC_READ_TABLE). "
                "Уменьши число полей в fields" + ("" if fieldnames else " (сейчас запрошены все поля таблицы)")
                + "." + _width_hint(system, table, fieldnames)
                + " Склейка нескольких вызовов по полям не выполняется.",
                "DATA_BUFFER_EXCEEDED",
            )
        if time.monotonic() >= deadline - 1:
            raise _timeout_error(system)
        raise
    layout = [(f["FIELDNAME"], int(f["OFFSET"]), int(f["LENGTH"]), f.get("TYPE", ""))
              for f in result.get("FIELDS", [])]
    rows = []
    for item in result.get("DATA", []):
        wa = item.get("WA", "")
        rows.append([_normalize(wa[o:o + n].strip(), t) for _, o, n, t in layout])
    return layout, rows


_TIMEOUT_SUPPORTED = True


def _rfc_call(conn, func: str, timeout: float, **params):
    """conn.call с клиентским тайм-аутом PyRFC (options={'timeout': с}); без него — если PyRFC не умеет."""
    global _TIMEOUT_SUPPORTED
    if _TIMEOUT_SUPPORTED:
        try:
            return conn.call(func, options={"timeout": max(1, int(timeout))}, **params)
        except TypeError as e:
            if "options" not in str(e) and "timeout" not in str(e):
                raise
            _TIMEOUT_SUPPORTED = False
    return conn.call(func, **params)


def _timeout_error(system: str) -> ToolError:
    return ToolError(
        f"⏱ Превышен тайм-аут {LIMITS[system]['timeout']} с для {system.upper()}. "
        "Сузь where_clause (период, компания, вид документа) и повтори.",
        "timeout",
    )


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _tuple_cmp(keys: list, values: tuple, last_op: str) -> str:
    """Лексикографическое сравнение кортежа ключа в Open SQL.

    (K1, K2) >= (a, b)  →  ( K1 > 'a' OR ( K1 = 'a' AND K2 >= 'b' ) )
    last_op: '>=' для нижней границы, '<' для верхней.
    """
    strict = ">" if last_op == ">=" else "<"
    expr = f"{keys[-1]} {last_op} {_lit(values[-1])}"
    for k, v in zip(reversed(keys[:-1]), reversed(values[:-1])):
        expr = f"( {k} {strict} {_lit(v)} OR ( {k} = {_lit(v)} AND {expr} ) )"
    return expr


def _range_where(where: str, keys: list, lo, hi) -> str:
    parts = [f"( {where.strip()} )"] if where.strip() else []
    if lo is not None:
        parts.append(_tuple_cmp(keys, lo, ">="))
    if hi is not None:
        parts.append(_tuple_cmp(keys, hi, "<"))
    return " AND ".join(parts)


def _scan(conn, system: str, table: str, where: str, fieldnames: list, limit: int,
          deadline: float, keys: list, full_key: bool, on_page) -> tuple:
    """Чтение до limit строк без опоры на порядок строк в SAP.

    У RFC_READ_TABLE нет ORDER BY, поэтому ROWSKIPS между вызовами даёт повторы и пропуски.
    Вместо этого данные делятся на диапазоны по ключу (первые len(keys) полей в fieldnames):
    диапазон читается вызовом с ROWCOUNT = PAGE_ROWS + 1; если строк больше PAGE_ROWS —
    он делится пополам по медиане ключа прочитанных строк, и половины читаются отдельно.
    Без ключа читается одна страница. Возвращает (просмотрено, есть_ещё_строки).
    """
    if not keys:
        cap = min(limit, PAGE_ROWS)
        _, rows = _call_read(conn, system, table, _split_options(where), fieldnames, cap + 1, 0, deadline)
        more = len(rows) > cap
        rows = rows[:cap]
        on_page(rows)
        return len(rows), more

    n = len(keys)
    seen = set() if full_key else None
    stack = [(None, None)]  # (нижняя граница включительно, верхняя — не включительно)
    scanned, calls, max_calls = 0, 0, 4 * (limit // PAGE_ROWS + 1) + 100
    while stack:
        if scanned >= limit:
            # лимит исчерпан: есть ли строки в непрочитанных диапазонах?
            for lo, hi in reversed(stack):
                options = _split_options(_range_where(where, keys, lo, hi))
                if _call_read(conn, system, table, options, fieldnames[:1], 1, 0, deadline)[1]:
                    return scanned, True
            return scanned, False
        calls += 1
        if calls > max_calls:
            raise ToolError("⚠️ Не удалось разбить таблицу на диапазоны по ключу. Сузь where_clause.",
                            "range split failed")
        lo, hi = stack.pop()
        options = _split_options(_range_where(where, keys, lo, hi))
        _, rows = _call_read(conn, system, table, options, fieldnames, PAGE_ROWS + 1, 0, deadline)
        if len(rows) <= PAGE_ROWS:
            room = limit - scanned
            truncated = len(rows) > room
            rows = rows[:room]
            if seen is not None:
                for row in rows:
                    key = hash(tuple(row[:n]))
                    if key in seen:
                        raise ToolError("⚠️ Строка прочитана повторно при делении на диапазоны — "
                                        "итог был бы неточным. Сузь where_clause.", "duplicate row")
                    seen.add(key)
            on_page(rows)
            scanned += len(rows)
            if truncated:
                return scanned, True
            continue
        distinct = sorted({tuple(r[:n]) for r in rows})
        if len(distinct) < 2:
            raise ToolError(
                f"⚠️ Больше {PAGE_ROWS} строк с одинаковым значением ключа ({', '.join(keys)}) — "
                "разбить на части нельзя. Сузь where_clause.", "range split failed")
        mid = distinct[len(distinct) // 2]
        stack.append((mid, hi))   # правая половина — позже
        stack.append((lo, mid))   # левая — сразу: строки идут по возрастанию ключа
    return scanned, False


def _inline_limit(system: str, fields: list) -> int:
    lim = LIMITS[system]
    return lim["inline_narrow"] if 0 < len(fields) <= NARROW_MAX_FIELDS else lim["inline"]


def _limit_error(system: str, mode: str, requested: int) -> ToolError:
    lim = LIMITS[system]
    if mode == "inline":
        text = (f"⚠️ max_rows={requested} больше лимита ответа для {system.upper()}: "
                f"{lim['inline']} строк при всех полях или {NARROW_MAX_FIELDS + 1}+ полях; "
                f"{lim['inline_narrow']} — если в fields не больше {NARROW_MAX_FIELDS} полей. "
                f"Для большего объёма: output='file' (до {lim['file']} строк) "
                "или aggregate_table_data для подсчёта.")
    else:
        text = (f"⚠️ max_rows={requested} больше лимита выгрузки в файл для {system.upper()}: "
                f"{lim['file']} строк. Сузь where_clause или используй aggregate_table_data "
                f"(просмотр до {lim['scan']} строк).")
    return ToolError(text, f"limit exceeded: {mode} {requested}")


# --- Файлы выгрузки ---
def _export_dir() -> str:
    low = EXPORT_DIR.lower()
    for env in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.getenv(env)
        if root and low.startswith(os.path.abspath(root).lower()):
            raise ToolError(f"⚠️ Папка выгрузки {EXPORT_DIR} внутри OneDrive. Задай SAP_EXPORT_DIR вне "
                            "общих и синхронизируемых папок.", "export dir in sync folder")
    if any(m in low for m in _SYNC_MARKERS):
        raise ToolError(f"⚠️ Папка выгрузки {EXPORT_DIR} похожа на синхронизируемую. Задай SAP_EXPORT_DIR "
                        "вне общих и синхронизируемых папок.", "export dir in sync folder")
    os.makedirs(EXPORT_DIR, exist_ok=True)
    return EXPORT_DIR


def _cleanup_exports(folder: str) -> None:
    """Удаляет выгрузки старше SAP_EXPORT_KEEP_DAYS дней (только *.csv и недописанные *.part)."""
    cutoff = time.time() - EXPORT_KEEP_DAYS * 86400
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if not name.lower().endswith((".csv", ".part")):
            continue
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def _export_path(folder: str, system: str, table: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", table).strip("_") or "TABLE"
    path = os.path.join(folder, f"{stamp}_{system}_{safe}.csv")
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stamp}_{system}_{safe}_{n}.csv")
        n += 1
    return path


def _render_table(headers: list, rows: list) -> str:
    out = " | ".join(headers) + "\n" + "-" * 80 + "\n"
    for row in rows:
        out += " | ".join(row) + "\n"
    return out


# --- Инструменты ---
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


def read_table_data(table_name: str, system: str = "sandbox",
                    max_rows: Union[int, str, None] = None,
                    where_clause: str = "", fields: str = "", output: str = "inline") -> str:
    """Чтение строк: inline — в ответ, file — в CSV (см. описание инструмента ниже)."""
    table = str(table_name).strip().upper()
    where = where_clause or ""
    mode = str(output or "inline").strip().lower()
    log = {"mode": mode, "fields": fields or ""}
    sys_label = str(system).lower()
    try:
        system = _check_data_system(system, table)
        sys_label = system
        if mode not in ("inline", "file"):
            raise ToolError("⚠️ output может быть 'inline' или 'file'.", "invalid output")
        names = _parse_names(fields, "fields")
        options = _check_policy(system, table, where)
        lim = LIMITS[system]
        if mode == "inline":
            requested = _to_int(10 if max_rows in (None, "") else max_rows, "max_rows")
            log["max_rows"] = requested
            if requested > _inline_limit(system, names):
                raise _limit_error(system, "inline", requested)
            return _read_inline(system, table, where, options, names, requested, log)
        requested = _to_int(lim["file"] if max_rows in (None, "") else max_rows, "max_rows")
        log["max_rows"] = requested
        if requested > lim["file"]:
            raise _limit_error(system, "file", requested)
        return _read_to_file(system, table, where, names, requested, log)
    except ToolError as e:
        _log_read(sys_label, table, where, error=e.code, **log)
        return e.message
    except Exception as e:
        _log_read(sys_label, table, where, error=str(e), **log)
        return f"❌ Ошибка чтения {table} ({_conn_label(sys_label)}):\n{e}"


def _read_inline(system, table, where, options, names, requested, log) -> str:
    deadline = time.monotonic() + LIMITS[system]["timeout"]
    conn = get_sap_connection(system)
    try:
        layout, rows = _call_read(conn, system, table, options, names, requested + 1, 0, deadline)
    finally:
        conn.close()
    more = len(rows) > requested
    rows = rows[:requested]
    _log_read(system, table, where, rows=len(rows), more=more or None, **log)
    if not rows:
        return f"❌ Данные не найдены в {table} ({system.upper()})"
    headers = [name for name, *_ in layout]
    text = f"📋 **Данные из {table} ({system.upper()})**\n" + _render_table(headers, rows)
    if more:
        text += (f"… Показано строк: {requested}, в SAP есть ещё. Для полного набора — output='file', "
                 "для количеств и сумм — aggregate_table_data.\n")
    return text


def _read_to_file(system, table, where, names, requested, log) -> str:
    folder = _export_dir()
    _cleanup_exports(folder)
    info = _field_info(system, table)
    columns = names or list(info)
    _check_fields_exist(info, columns, table)
    # больше одной страницы — читаем по диапазонам ключа; поля ключа идут первыми
    keys, full_key = _range_keys(info) if requested > PAGE_ROWS else ([], False)
    read_names = list(dict.fromkeys(keys + columns))
    out_idx = [read_names.index(c) for c in columns]

    path = _export_path(folder, system, table)
    part = path + ".part"
    preview, written = [], 0
    deadline = time.monotonic() + LIMITS[system]["timeout"]
    try:
        with open(part, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
            writer.writerow(columns)

            def on_page(rows):
                nonlocal written
                for row in rows:
                    out = [row[i] for i in out_idx]
                    writer.writerow(out)
                    if len(preview) < FILE_PREVIEW_ROWS:
                        preview.append(out)
                written += len(rows)

            conn = get_sap_connection(system)
            try:
                _, more = _scan(conn, system, table, where, read_names, requested,
                                deadline, keys, full_key, on_page)
            finally:
                conn.close()
        os.replace(part, path)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    log["file"] = path
    _log_read(system, table, where, rows=written, more=more or None, **log)
    text = (f"📁 **Выгрузка {table} ({system.upper()})**\n"
            f"Файл: {path}\n"
            f"Строк: {written}" + (" — ⚠️ упёрлись в лимит max_rows, в SAP есть ещё строки" if more else "")
            + "\n"
            f"Колонки ({len(columns)}): {', '.join(columns)}\n"
            "Формат: CSV, UTF-8 с BOM, разделитель «;», числа со знаком слева и точкой (-8646.91).\n")
    if preview:
        text += f"Первые {len(preview)} строк:\n" + _render_table(columns, preview)
    else:
        text += "Данные по условию не найдены (файл содержит только заголовок).\n"
    return text


def _parse_measures(value: str) -> list:
    result = []
    for item in (value or "count").split(","):
        raw = item.strip()
        if not raw:
            continue
        op, _, field = raw.partition(":")
        op, field = op.strip().lower(), field.strip().upper() or None
        if (op not in ("count", "sum", "min", "max") or (op == "count") != (field is None)
                or (field and not _NAME_RE.match(field))):
            raise ToolError(f"⚠️ Некорректная мера {raw!r}. Допустимо: count, sum:ПОЛЕ, min:ПОЛЕ, max:ПОЛЕ.",
                            "invalid measures")
        measure = (op, field)
        if measure not in result:
            result.append(measure)
    if not result:
        result = [("count", None)]
    return result


def aggregate_table_data(table_name: str, system: str = "sandbox", where_clause: str = "",
                         group_by: str = "", measures: str = "count") -> str:
    """Подсчёт на стороне сервера (см. описание инструмента ниже)."""
    table = str(table_name).strip().upper()
    where = where_clause or ""
    log = {"mode": "aggregate", "group_by": group_by or "", "measures": measures or "count"}
    sys_label = str(system).lower()
    try:
        system = _check_data_system(system, table)
        sys_label = system
        groups = _parse_names(group_by, "group_by")
        if len(groups) > MAX_GROUP_BY:
            raise ToolError(f"⚠️ В group_by не больше {MAX_GROUP_BY} полей.", "too many group_by")
        meas = _parse_measures(measures)
        _check_policy(system, table, where)
        info = _field_info(system, table)
        meas_fields = [f for _, f in meas if f]
        _check_fields_exist(info, groups + meas_fields, table)
        numeric = {n for n in groups + meas_fields if info[n].get("DATATYPE") in NUMERIC_DDIC}
        for op, f in meas:
            if op == "sum" and f not in numeric:
                raise ToolError(f"⚠️ sum:{f} — поле типа {info[f].get('DATATYPE')}, суммировать можно "
                                "только числовые поля (CURR, QUAN, DEC, INT, FLTP).", "sum on non-numeric")

        # Ссылки на валюту/ЕИ: если поля нет в group_by — считаем, но проверяем смешение
        refs, notes = {}, []
        for f in dict.fromkeys(f for op, f in meas if op != "count"):
            if info[f].get("DATATYPE") not in ("CURR", "QUAN"):
                continue
            ref_tab, ref_field = (info[f].get("REFTABLE") or "").upper(), (info[f].get("REFFIELD") or "").upper()
            if not ref_field or ref_field in groups:
                continue
            if ref_tab in ("", table) and ref_field in info:
                refs.setdefault(ref_field, set()).add(f)
            else:
                notes.append(f"⚠️ {f} ссылается на {ref_tab}-{ref_field} (другая таблица): смешение "
                             "валют/ЕИ проверить нельзя — при необходимости добавь поле валюты в group_by.")

        lim = LIMITS[system]
        limit = lim["scan"]
        keys, full_key = _range_keys(info) if limit > PAGE_ROWS else ([], False)
        read_names = list(dict.fromkeys(keys + groups + meas_fields + list(refs)))
        if not read_names:  # только count без группировки: читаем одно узкое поле
            read_names = [min(info, key=lambda n: int(info[n].get("LENG") or 999))]
        idx = {n: i for i, n in enumerate(read_names)}

        agg = {}

        def on_page(rows):
            for row in rows:
                g = tuple(row[idx[n]] for n in groups)
                st = agg.get(g)
                if st is None:
                    st = agg[g] = {"count": 0, "val": {}, "refs": {r: set() for r in refs}}
                st["count"] += 1
                for op, f in meas:
                    if op == "count":
                        continue
                    raw = row[idx[f]]
                    val = _parse_number(raw) if f in numeric else raw
                    cur = st["val"].get((op, f))
                    if op == "sum":
                        st["val"][(op, f)] = (cur or Decimal(0)) + val
                    elif cur is None or (op == "min" and val < cur) or (op == "max" and val > cur):
                        st["val"][(op, f)] = val
                for r in refs:
                    st["refs"][r].add(row[idx[r]])

        deadline = time.monotonic() + lim["timeout"]
        conn = get_sap_connection(system)
        try:
            scanned, more = _scan(conn, system, table, where, read_names, limit, deadline,
                                  keys, full_key, on_page)
        finally:
            conn.close()

        log.update(scanned=scanned, groups=len(agg), more=more or None)
        _log_read(system, table, where, rows=min(len(agg), MAX_GROUPS_SHOWN), **log)

        ordered = sorted(agg.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
        headers = groups + [op if op == "count" else f"{op}:{f}" for op, f in meas]
        lines = []
        for g, st in ordered[:MAX_GROUPS_SHOWN]:
            cells = list(g)
            for op, f in meas:
                if op == "count":
                    cells.append(str(st["count"]))
                else:
                    v = st["val"].get((op, f))
                    cells.append(_fmt_number(v) if isinstance(v, Decimal) else str(v if v is not None else ""))
            lines.append(cells)

        for r, measures_using in refs.items():
            mixed = [g for g, st in ordered if len(st["refs"][r]) > 1]
            if mixed:
                sample = "; ".join(
                    (" / ".join(g) or "все строки") + ": " + ", ".join(sorted(agg[g]["refs"][r]))
                    for g in mixed[:5])
                notes.append(f"⚠️ {', '.join(sorted(measures_using))}: в {len(mixed)} группах сложены значения "
                             f"с разными {r} ({sample}). Добавь {r} в group_by, чтобы не смешивать.")
            else:
                notes.append(f"ℹ️ {r} нет в group_by, но внутри каждой группы {r} одна — суммы корректны.")

        text = f"📊 **Подсчёт {table} ({system.upper()})**\n"
        text += f"Просмотрено строк: {scanned}; групп: {len(agg)}"
        if len(agg) > MAX_GROUPS_SHOWN:
            text += f" (показаны первые {MAX_GROUPS_SHOWN} по убыванию count)"
        text += "\n"
        if more:
            text += (f"⚠️ Упёрлись в лимит просмотра {limit} строк: итоги НЕПОЛНЫЕ. "
                     "Сузь where_clause.\n")
        text += "".join(n + "\n" for n in notes)
        if not agg:
            return text + "Строк по условию не найдено.\n"
        return text + _render_table(headers, lines)
    except ToolError as e:
        _log_read(sys_label, table, where, error=e.code, **log)
        return e.message
    except Exception as e:
        _log_read(sys_label, table, where, error=str(e), **log)
        return f"❌ Ошибка подсчёта {table} ({_conn_label(sys_label)}):\n{e}"


_MASK_RE = re.compile(r"^[A-Z0-9_/%]+$")
_TEXT_LANGS = ("R", "E")  # внутренние коды языков SAP: русский, английский


def search_tables(pattern: str, system: str = "dev", max_results: int = 10) -> str:
    """Ищет таблицы SAP по маске (только DEV): имена из DD02L, описания из DD02T."""
    mask = str(pattern or "").strip().upper().replace("*", "%")
    try:
        if not _MASK_RE.match(mask):
            raise ToolError("⚠️ Маска может содержать только буквы, цифры, '_', '/' и '*' (например 'ZMM*').",
                            "invalid pattern")
        limit = _to_int(max_results, "max_results")
        if limit > SEARCH_MAX_RESULTS:
            raise ToolError(f"⚠️ max_results не больше {SEARCH_MAX_RESULTS}. Сузь маску.", "limit exceeded")
        conn = get_sap_connection("dev")
        try:
            # В DD02L описаний нет (DDTEXT лежит в DD02T) — читаем двумя запросами.
            found = conn.call("RFC_READ_TABLE", QUERY_TABLE="DD02L", DELIMITER="|",
                              OPTIONS=_split_options(f"TABNAME LIKE '{mask}' AND AS4LOCAL = 'A'"),
                              FIELDS=[{"FIELDNAME": "TABNAME"}, {"FIELDNAME": "TABCLASS"}],
                              ROWCOUNT=limit)
            tables = []
            for row in found.get("DATA", []):
                parts = [p.strip() for p in row["WA"].split("|")]
                if parts and parts[0]:
                    tables.append((parts[0], parts[1] if len(parts) > 1 else ""))
            texts = {}
            names = [name for name, _ in tables]
            try:
                for i in range(0, len(names), 40):
                    chunk = ", ".join(f"'{n}'" for n in names[i:i + 40])
                    langs = ", ".join(f"'{l}'" for l in _TEXT_LANGS)
                    got = conn.call("RFC_READ_TABLE", QUERY_TABLE="DD02T", DELIMITER="|",
                                    OPTIONS=_split_options(f"TABNAME IN ( {chunk} ) AND AS4LOCAL = 'A' "
                                                           f"AND DDLANGUAGE IN ( {langs} )"),
                                    FIELDS=[{"FIELDNAME": "TABNAME"}, {"FIELDNAME": "DDLANGUAGE"},
                                            {"FIELDNAME": "DDTEXT"}])
                    for row in got.get("DATA", []):
                        parts = row["WA"].split("|", 2)
                        if len(parts) < 3:
                            continue
                        name, lang, text = parts[0].strip(), parts[1].strip(), parts[2].strip()
                        best = texts.get(name)
                        if text and (best is None or _TEXT_LANGS.index(lang) < _TEXT_LANGS.index(best[0])):
                            texts[name] = (lang, text)
            except Exception:
                texts = {}  # описания не критичны: имена таблиц уже найдены
        finally:
            conn.close()
    except ToolError as e:
        return e.message
    except Exception as e:
        return f"❌ Ошибка поиска таблиц ({_conn_label('dev')}):\n{e}"
    if not tables:
        return f"🔍 Таблицы по маске {mask} (DEV) не найдены."
    output = f"🔍 **Таблицы по маске {mask} (DEV)**: {len(tables)}"
    output += f" (достигнут max_results={limit}, возможно есть ещё)\n" if len(tables) >= limit else "\n"
    for name, tabclass in tables:
        text = texts.get(name, ("", ""))[1]
        output += f"- **{name}** [{tabclass}]" + (f": {text}" if text else "") + "\n"
    return output


# --- Описания инструментов (то, что видит агент) ---
_P, _S = LIMITS["prod"], LIMITS["sandbox"]
_LIMITS_TEXT = (
    f"Лимиты (Prod / Sandbox): inline — {_P['inline']} / {_S['inline']} строк, "
    f"а если в fields не больше {NARROW_MAX_FIELDS} полей — {_P['inline_narrow']} / {_S['inline_narrow']}; "
    f"file — {_P['file']} / {_S['file']} строк; "
    f"aggregate — просмотр до {_P['scan']} / {_S['scan']} строк. "
    f"Тайм-аут запроса {_P['timeout']} / {_S['timeout']} с. "
    "Превышение лимита — ошибка, а не обрезание. "
    f"DEV (только таблицы репозитория): inline — {LIMITS['dev']['inline']} / {LIMITS['dev']['inline_narrow']}, "
    f"file — {LIMITS['dev']['file']}, aggregate — {LIMITS['dev']['scan']}, тайм-аут {LIMITS['dev']['timeout']} с."
)
_COMMON_TEXT = (
    "system: 'sandbox' — основной источник; 'prod' — только чтение, where_clause обязателен; "
    "'dev' — бизнес-данных нет, читаются только таблицы репозитория ("
    + ", ".join(sorted(DEV_REPO_TABLES)) + "), where_clause обязателен. "
    "Таблицы USR*, PA####, BNKA, KNBK, LFBK, PASSWORD, T002* заблокированы везде. "
    f"where_clause — ABAP Open SQL, литералы в одинарных кавычках, до {WHERE_MAX_LEN} символов. "
    "Поля проверяй заранее через get_table_structure (system='dev'). "
    "Каждый вызов пишется в журнал reads.log."
)

mcp.tool(description="Структура таблицы SAP (поля, типы, длины) через DDIF_FIELDINFO_GET. "
                     "system: 'dev' (основной) или 'prod' (только для сверки структур).")(get_table_structure)

mcp.tool(description=(
    "Читает строки таблицы SAP через RFC_READ_TABLE.\n"
    "output='inline' (по умолчанию) — строки в ответе; для просмотра примеров и точечных записей. "
    "Ответ занимает контекст: указывай только нужные fields.\n"
    "output='file' — строки пишутся в CSV (UTF-8 с BOM, разделитель ';') на сервере; в ответе путь, число строк, "
    "колонки и первые 10 строк. Для больших выборок (сотни и тысячи строк) — данные не попадают в контекст. "
    "max_rows для file по умолчанию — лимит файла.\n"
    "Для количеств, сумм, min/max и проверок вида «у всех ли записей…» используй aggregate_table_data, а не чтение строк.\n"
    "Ширина строки результата — не больше 512 символов: при ошибке уменьши число fields.\n"
    "max_rows — целое число > 0.\n" + _LIMITS_TEXT + "\n" + _COMMON_TEXT
))(read_table_data)

mcp.tool(description=(
    "Подсчёт по таблице SAP без выгрузки строк: группировка и меры считаются на сервере MCP "
    "(постраничное чтение только нужных полей, изменений в SAP нет).\n"
    f"group_by — до {MAX_GROUP_BY} полей через запятую (пусто — один итог). "
    "measures — через запятую: count, sum:ПОЛЕ, min:ПОЛЕ, max:ПОЛЕ (например 'count,sum:BEZNK').\n"
    f"Ответ: до {MAX_GROUPS_SHOWN} групп по убыванию count, сколько строк просмотрено, признак «упёрлись в лимит». "
    "Суммы SAP с минусом справа (8646.91-) разбираются правильно. Если поле суммы ссылается на валюту/ЕИ "
    "(WAERS, MEINS…), которых нет в group_by, — сумма считается, но в ответе предупреждение о группах со смешением. "
    "Используй для проверок «у всех ли записей…», «сколько документов каждого вида», итогов по поставщикам и т.п.\n"
    + _LIMITS_TEXT + "\n" + _COMMON_TEXT
))(aggregate_table_data)

mcp.tool(description="Ищет таблицы SAP по маске имени (например 'ZMM*'), только в DEV. "
                     "Возвращает имя, класс таблицы и описание. "
                     f"max_results — до {SEARCH_MAX_RESULTS}.")(search_tables)


if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8000"))
    print("🚀 SAP RFC MCP Server (FastMCP, Streamable HTTP only)")
    print(f"📡 Эндпоинт: http://{host}:{port}/mcp/")
    print(f"🔌 Системы: {', '.join(sorted(SYSTEMS))}")
    print(f"📁 Выгрузки: {EXPORT_DIR} (хранятся {EXPORT_KEEP_DAYS} дн.)")
    mcp.run(transport="streamable-http", host=host, port=port)
