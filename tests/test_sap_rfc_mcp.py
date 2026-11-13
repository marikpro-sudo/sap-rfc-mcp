"""Тесты sap_rfc_mcp.py без SAP: pyrfc подменяется заглушкой, RFC_READ_TABLE эмулируется.

Запуск:  python -m unittest discover -s tests -v   (из корня репозитория)
"""
import csv
import importlib
import json
import os
import random
import re
import sys
import tempfile
import time
import types
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="sap_rfc_test_")

# --- Окружение до импорта модуля ---
for system in ("DEV", "SANDBOX", "PROD"):
    for key in ("ASHOST", "SYSNR", "CLIENT", "USER", "PASSWORD"):
        os.environ[f"SAP_{key}_{system}"] = system.lower() if key == "ASHOST" else "x"
os.environ.update({
    "SAP_PAGE_ROWS": "1000",
    "SAP_LIMIT_SCAN_SANDBOX": "8000",
    "SAP_LIMIT_FILE_SANDBOX": "7000",
    "SAP_EXPORT_DIR": os.path.join(TMP, "exports"),
    "SAP_EXPORT_KEEP_DAYS": "30",
})


# --- Эмуляция SAP ---
FIELDS = {  # имя: (DATATYPE, INTTYPE, LENG, OUTPUTLEN, KEY, REFTABLE, REFFIELD)
    "MANDT": ("CLNT", "C", 3, 3, "X", "", ""),
    "BELNR": ("CHAR", "C", 10, 10, "X", "", ""),
    "GJAHR": ("NUMC", "N", 4, 4, "X", "", ""),
    "LIFNR": ("CHAR", "C", 10, 10, "", "", ""),
    "WAERS": ("CUKY", "C", 5, 5, "", "", ""),
    "BEZNK": ("CURR", "P", 13, 16, "", "RBKP", "WAERS"),
    "BLART": ("CHAR", "C", 2, 2, "", "", ""),
    "TXT": ("CHAR", "C", 300, 300, "", "", ""),
    "DESCR": ("CHAR", "C", 300, 300, "", "", ""),
}


def sap_amount(value: Decimal) -> str:
    """Как SAP: минус справа, выравнивание вправо."""
    text = f"{abs(value):.2f}" + ("-" if value < 0 else "")
    return text.rjust(16)


def make_rows(n, seed=1):
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        cur = rnd.choice(["KZT", "KZT", "USD"])
        amt = Decimal(rnd.randint(-50000, 200000)) / 100
        txt = rnd.choice(["обычный", 'кавычка " внутри', "точка; с запятой", "перенос\nстроки", ""])
        rows.append({"MANDT": "303", "BELNR": f"{5100000000 + i:010d}", "GJAHR": "2026",
                     "LIFNR": f"{rnd.randint(1, 30):010d}", "WAERS": cur, "BEZNK": amt,
                     "BLART": rnd.choice(["RE", "ST", "KR"]), "TXT": txt, "DESCR": ""})
    return rows


class FakeSAP:
    rows = make_rows(6000)
    shuffle_pages = False
    delay = 0.0
    calls = []


_TOK = re.compile(r"'(?:[^']|'')*'|<>|>=|<=|[()=<>,]|[A-Za-z0-9_/.\-]+")


def where_matcher(where):
    """Мини-интерпретатор Open SQL WHERE для заглушки: =, <>, <, <=, >, >=, AND, OR, NOT, скобки."""
    if not where.strip():
        return lambda r: True
    out = []
    for t in _TOK.findall(where):
        if t.startswith("'"):
            out.append(repr(t[1:-1].replace("''", "'")))
        elif t.upper() in ("AND", "OR", "NOT"):
            out.append(t.lower())
        elif t == "=":
            out.append("==")
        elif t == "<>":
            out.append("!=")
        elif t in ("<", "<=", ">", ">=", "(", ")"):
            out.append(t)
        elif re.fullmatch(r"-?\d+(\.\d+)?", t):
            out.append(f"Decimal('{t}')")
        else:
            out.append(f"r[{t!r}]")
    code = compile(" ".join(out), "<where>", "eval")
    return lambda r: eval(code, {"Decimal": Decimal}, {"r": r})


class FakeConnection:
    def __init__(self, **cfg):
        self.cfg = cfg

    def close(self):
        pass

    def call(self, func, options=None, **p):
        FakeSAP.calls.append((func, p, options))
        if FakeSAP.delay:
            time.sleep(FakeSAP.delay)
            raise RuntimeError("RFC_CANCELED: call cancelled by timeout")
        if func == "DDIF_FIELDINFO_GET":
            return {"DFIES_TAB": [
                {"FIELDNAME": n, "DATATYPE": d, "INTTYPE": it, "LENG": f"{l:06d}", "OUTPUTLEN": f"{o:06d}",
                 "KEYFLAG": k, "REFTABLE": rt, "REFFIELD": rf, "FIELDTEXT": n}
                for n, (d, it, l, o, k, rt, rf) in FIELDS.items()]}
        assert func == "RFC_READ_TABLE"
        if p.get("QUERY_TABLE") in ("DD02L", "DD02T"):
            return fake_ddic(p)
        names = [f["FIELDNAME"] for f in p["FIELDS"]] or list(FIELDS)
        delim = p.get("DELIMITER", "")
        layout, off = [], 0
        for n in names:
            width = FIELDS[n][3]
            layout.append({"FIELDNAME": n, "OFFSET": f"{off:06d}", "LENGTH": f"{width:06d}", "TYPE": FIELDS[n][1]})
            off += width + (1 if delim.strip() else 0)
        if off > 512:
            raise RuntimeError("5 (rc=5): key=DATA_BUFFER_EXCEEDED, message= Number:000")
        match = where_matcher(" ".join(o["TEXT"] for o in p.get("OPTIONS", [])))
        data = [r for r in FakeSAP.rows if match(r)]
        if FakeSAP.shuffle_pages:  # как HANA: порядок строк без ORDER BY меняется от вызова к вызову
            random.Random(len(FakeSAP.calls)).shuffle(data)
        skip, count = p.get("ROWSKIPS", 0), p.get("ROWCOUNT", 0)
        data = data[skip: skip + count if count else None]
        out = []
        for r in data:
            parts = []
            for n in names:
                v = r[n]
                v = sap_amount(v) if n == "BEZNK" else str(v).replace("\n", " ").ljust(FIELDS[n][3])
                parts.append(v)
            out.append({"WA": (delim if delim.strip() else "").join(parts).rstrip()})
        return {"FIELDS": layout, "DATA": out}


DDIC_TABLES = {"ZMM_A": "TRANSP", "ZMM_B": "INTTAB", "ZSD_C": "TRANSP"}
DDIC_TEXTS = [("ZMM_A", "E", "Table A"), ("ZMM_A", "R", "Таблица А"), ("ZMM_B", "E", "Structure | B")]


def fake_ddic(p):
    """DD02L / DD02T для search_tables. В DD02L нет DDTEXT — как в настоящем SAP."""
    names = [f["FIELDNAME"] for f in p["FIELDS"]]
    where = " ".join(o["TEXT"] for o in p.get("OPTIONS", []))
    if p["QUERY_TABLE"] == "DD02L":
        if "DDTEXT" in names:
            raise RuntimeError("5 (rc=5): key=FIELD_NOT_VALID, message= Number:000")
        prefix = re.search(r"TABNAME LIKE '([^']*)'", where).group(1).rstrip("%")
        rows = [{"WA": f"{n}|{c}"} for n, c in sorted(DDIC_TABLES.items()) if n.startswith(prefix)]
        count = p.get("ROWCOUNT", 0)
        return {"DATA": rows[:count] if count else rows}
    wanted = set(re.findall(r"'([^']+)'", where.split("AND")[0]))
    return {"DATA": [{"WA": f"{n}|{l}|{x}"} for n, l, x in DDIC_TEXTS if n in wanted]}


sys.modules["pyrfc"] = types.SimpleNamespace(Connection=FakeConnection)
sys.path.insert(0, ROOT)
srv = importlib.import_module("sap_rfc_mcp")
srv.LOG_DIR = os.path.join(TMP, "logs")
srv.READ_LOG = os.path.join(srv.LOG_DIR, "reads.log")


def last_log():
    with open(srv.READ_LOG, encoding="utf-8") as f:
        return json.loads(f.read().strip().splitlines()[-1])


class Base(unittest.TestCase):
    def setUp(self):
        FakeSAP.rows = make_rows(6000)
        FakeSAP.shuffle_pages = False
        FakeSAP.delay = 0.0
        FakeSAP.calls = []


class WhereTests(Base):
    def test_long_where_split(self):
        w = ("GJAHR = '2026' AND BLART IN ('RE', 'ST', 'KR', 'KG') AND WAERS <> 'XXX' AND "
             "BELNR <> '0000000000' AND BUKRS IN ('1000', '1002', '1003') AND RBSTAT <> 'Z' AND BEZNK >= 0")
        self.assertGreater(len(w), 150)
        lines = [r["TEXT"] for r in srv._split_options(w)]
        self.assertTrue(all(len(x) <= 72 for x in lines))
        self.assertEqual(" ".join(lines).replace(", ", ","), w.replace(", ", ","))

    def test_literal_with_spaces_not_broken(self):
        w = "TXT = '" + "a b " * 15 + "' AND " + "BELNR = '1' AND " * 5 + "GJAHR = '2026'"
        for line in srv._split_options(w):
            self.assertEqual(line["TEXT"].count("'") % 2, 0)

    def test_where_too_long(self):
        res = srv.read_table_data("RBKP", "sandbox", 5, "GJAHR = '2026' AND " * 60 + "BLART = 'RE'", "BELNR")
        self.assertIn("больше предела 1000", res)
        self.assertEqual(last_log()["error"], "where too long")


class NumberTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(srv._parse_number("8646.91-"), Decimal("-8646.91"))
        self.assertEqual(srv._parse_number("  1,234.50 "), Decimal("1234.50"))
        self.assertEqual(srv._parse_number("12,5"), Decimal("12.5"))
        self.assertEqual(srv._parse_number(""), 0)
        self.assertEqual(srv._parse_number("1.0000000000000000E+03"), Decimal("1000"))
        self.assertEqual(srv._fmt_number(Decimal("-0.00")), "0.00")


class PolicyTests(Base):
    def test_blocked_all_modes_all_systems(self):
        for table in ("USR02", "PA0008", "BNKA", "T002T", "PASSWORD"):
            for system in ("sandbox", "prod"):
                self.assertIn("заблокирована", srv.read_table_data(table, system, 5, "X = '1'"))
                self.assertIn("заблокирована", srv.read_table_data(table, system, 5, "X = '1'", output="file"))
                self.assertIn("заблокирована", srv.aggregate_table_data(table, system, "X = '1'"))
        self.assertEqual(last_log()["error"], "blocked")
        self.assertEqual(last_log()["mode"], "aggregate")
        self.assertFalse(FakeSAP.calls, "в SAP не должно уйти ни одного вызова")

    def test_prod_requires_where_all_modes(self):
        self.assertIn("обязателен where_clause", srv.read_table_data("RBKP", "prod", 5))
        self.assertIn("обязателен where_clause", srv.read_table_data("RBKP", "prod", output="file"))
        self.assertIn("обязателен where_clause", srv.aggregate_table_data("RBKP", "prod"))
        self.assertEqual(last_log()["error"], "where required")

    def test_dev_rejected(self):
        self.assertIn("'sandbox' или 'prod'", srv.read_table_data("RBKP", "dev", 5))
        self.assertIn("'sandbox' или 'prod'", srv.aggregate_table_data("RBKP", "dev"))
        self.assertIn("'sandbox' или 'prod'", srv.read_table_data("RBKP", "dev", 5, "GJAHR = '2026'"))
        self.assertFalse(FakeSAP.calls, "в SAP не должно уйти ни одного вызова")

    def test_dev_repository_tables(self):
        for table in ("E070", "E071", "TADIR", "WBCROSSGT", "DWINACTIV"):
            out = srv.read_table_data(table, "dev", 5, "GJAHR = '2026'", "BELNR,GJAHR")
            self.assertNotIn("'sandbox' или 'prod'", out)
            self.assertIn("5100000000", out)
        self.assertEqual(last_log()["system"], "dev")
        self.assertIn("Просмотрено строк", srv.aggregate_table_data("E071", "dev", "GJAHR = '2026'", "BLART"))

    def test_dev_requires_where_and_keeps_blocklist(self):
        self.assertIn("обязателен where_clause", srv.read_table_data("E071", "dev", 5))
        self.assertIn("обязателен where_clause", srv.read_table_data("E071", "dev", output="file"))
        self.assertIn("обязателен where_clause", srv.aggregate_table_data("TADIR", "dev"))
        self.assertIn("'sandbox' или 'prod'", srv.read_table_data("USR02", "dev", 5, "X = '1'"))
        self.assertFalse(FakeSAP.calls)

    def test_dev_limits(self):
        self.assertIn("больше лимита", srv.read_table_data("E071", "dev", 5000, "GJAHR = '2026'", "BELNR"))
        self.assertFalse(FakeSAP.calls)


class SearchTests(Base):
    def test_search_reads_texts_from_dd02t(self):
        out = srv.search_tables("zmm*")
        self.assertIn("**ZMM_A** [TRANSP]: Таблица А", out)      # русский текст в приоритете
        self.assertIn("**ZMM_B** [INTTAB]: Structure | B", out)  # разделитель внутри текста не ломает разбор
        self.assertNotIn("ZSD_C", out)
        self.assertNotIn("FIELD_NOT_VALID", out)

    def test_search_limit_and_not_found(self):
        self.assertIn("достигнут max_results=1", srv.search_tables("Z*", max_results=1))
        self.assertIn("не найдены", srv.search_tables("YY*"))

    def test_search_rejects_bad_pattern_without_call(self):
        self.assertIn("Маска может содержать", srv.search_tables("Z' OR TABNAME LIKE '%"))
        self.assertIn("max_results", srv.search_tables("Z*", max_results=100000))
        self.assertIn("max_results", srv.search_tables("Z*", max_results=0))
        self.assertFalse(FakeSAP.calls)


class InlineTests(Base):
    def test_limits(self):
        self.assertIn("больше лимита ответа", srv.read_table_data("RBKP", "sandbox", 201, "", ""))
        self.assertIn("больше лимита ответа", srv.read_table_data("RBKP", "sandbox", 201, "", "A,B,C,D,E,F,G"))
        self.assertIn("больше лимита ответа", srv.read_table_data("RBKP", "sandbox", 1001, "", "BELNR"))
        self.assertIn("больше лимита ответа", srv.read_table_data("RBKP", "prod", 51, "GJAHR = '2026'", ""))
        self.assertIn("больше лимита ответа", srv.read_table_data("RBKP", "prod", 501, "GJAHR = '2026'", "BELNR"))
        self.assertEqual(last_log()["error"], "limit exceeded: inline 501")
        res = srv.read_table_data("RBKP", "sandbox", 800, "", "BELNR,GJAHR,LIFNR,WAERS")
        self.assertEqual(res.count("\n"), 3 + 800 + 1)  # название, заголовок, черта, строки, «есть ещё»
        self.assertIn("есть ещё", res)
        self.assertEqual(last_log()["rows"], 800)

    def test_max_rows_types(self):
        self.assertIn("5100000002", srv.read_table_data("RBKP", "sandbox", "3", "", "BELNR"))
        self.assertIn("целым числом", srv.read_table_data("RBKP", "sandbox", "abc", "", "BELNR"))
        self.assertIn("больше 0", srv.read_table_data("RBKP", "sandbox", 0, "", "BELNR"))
        self.assertIn("больше 0", srv.read_table_data("RBKP", "sandbox", -5, "", "BELNR"))

    def test_amount_and_parsing(self):
        FakeSAP.rows[0]["BEZNK"] = Decimal("-8646.91")
        FakeSAP.rows[0]["TXT"] = "a|b"
        res = srv.read_table_data("RBKP", "sandbox", 1, "", "BELNR,BEZNK,TXT,WAERS")
        self.assertIn("5100000000 | -8646.91 | a|b | ", res)

    def test_width(self):
        res = srv.read_table_data("RBKP", "sandbox", 5, "", "")
        self.assertIn("шире 512", res)
        self.assertIn("все поля", res)
        self.assertIn("TXT (300)", res)
        self.assertEqual(last_log()["error"], "DATA_BUFFER_EXCEEDED")

    def test_timeout(self):
        srv.LIMITS["sandbox"]["timeout"] = 1
        FakeSAP.delay = 1.1
        try:
            res = srv.read_table_data("RBKP", "sandbox", 5, "", "BELNR")
        finally:
            srv.LIMITS["sandbox"]["timeout"] = 300
        self.assertIn("тайм-аут", res)
        self.assertEqual(last_log()["error"], "timeout")


class FileTests(Base):
    def test_file_5000(self):
        res = srv.read_table_data("RBKP", "sandbox", 5000, "GJAHR = '2026'", "BELNR,GJAHR,BEZNK,WAERS,TXT",
                                  output="file")
        path = [line[6:] for line in res.splitlines() if line.startswith("Файл: ")][0]
        with open(path, "rb") as f:
            self.assertTrue(f.read(3) == b"\xef\xbb\xbf")
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f, delimiter=";"))
        self.assertEqual(rows[0], ["BELNR", "GJAHR", "BEZNK", "WAERS", "TXT"])
        self.assertEqual(len(rows) - 1, 5000)
        exp = FakeSAP.rows[:5000]
        self.assertEqual(sum(Decimal(r[2]) for r in rows[1:]), sum(r["BEZNK"] for r in exp))
        self.assertIn('кавычка " внутри', [r[4] for r in rows[1:]])
        self.assertIn("точка; с запятой", [r[4] for r in rows[1:]])
        self.assertIn("Строк: 5000 — ⚠️ упёрлись", res)  # в SAP 6000 строк
        self.assertEqual(res.split("Первые 10 строк:\n")[1].count("\n"), 12)
        log = last_log()
        self.assertEqual((log["mode"], log["rows"], log["file"]), ("file", 5000, path))
        self.assertRegex(os.path.basename(path), r"^\d{8}_\d{6}_sandbox_RBKP(_\d+)?\.csv$")
        self.assertFalse([n for n in os.listdir(os.path.dirname(path)) if n.endswith(".part")])
        # 5000 > страницы 1000: чтение по диапазонам ключа (BELNR, GJAHR идут первыми, в файл не попадают)
        reads = [c for c in FakeSAP.calls if c[0] == "RFC_READ_TABLE"]
        self.assertGreater(len(reads), 5)
        self.assertEqual([f["FIELDNAME"] for f in reads[0][1]["FIELDS"]][:2], ["BELNR", "GJAHR"])
        self.assertTrue(all(c[1]["ROWSKIPS"] == 0 for c in reads))
        self.assertEqual([r[0] for r in rows[1:]], sorted(r[0] for r in rows[1:]))  # по возрастанию ключа

    def test_file_all_rows_no_flag(self):
        res = srv.read_table_data("RBKP", "sandbox", None, "", "BELNR,BLART", output="file")
        self.assertIn("Строк: 6000\n", res)

    def test_file_limit(self):
        self.assertIn("лимита выгрузки в файл", srv.read_table_data("RBKP", "sandbox", 7001, "", "BELNR",
                                                                     output="file"))

    def test_file_unstable_order(self):
        FakeSAP.shuffle_pages = True
        res = srv.read_table_data("RBKP", "sandbox", None, "GJAHR = '2026'", "BELNR,BLART", output="file")
        self.assertIn("Строк: 6000\n", res)
        path = [line[6:] for line in res.splitlines() if line.startswith("Файл: ")][0]
        with open(path, encoding="utf-8-sig", newline="") as f:
            got = [r[0] for r in list(csv.reader(f, delimiter=";"))[1:]]
        self.assertEqual(sorted(got), sorted(r["BELNR"] for r in FakeSAP.rows))
        self.assertEqual(len(set(got)), 6000)

    def test_file_error_leaves_no_part(self):
        res = srv.read_table_data("RBKP", "sandbox", 5000, "", "BELNR,TXT,DESCR", output="file")
        self.assertIn("шире 512", res)
        folder = os.environ["SAP_EXPORT_DIR"]
        self.assertFalse([n for n in os.listdir(folder) if n.endswith(".part")])

    def test_cleanup(self):
        folder = os.environ["SAP_EXPORT_DIR"]
        os.makedirs(folder, exist_ok=True)
        old = os.path.join(folder, "old.csv")
        keep = os.path.join(folder, "notes.txt")
        for p in (old, keep):
            open(p, "w").close()
            os.utime(p, (time.time() - 31 * 86400,) * 2)
        srv.read_table_data("RBKP", "sandbox", 5, "", "BELNR", output="file")
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(keep))

    def test_sync_folder_rejected(self):
        saved = srv.EXPORT_DIR
        srv.EXPORT_DIR = r"C:\Users\Марат\OneDrive\exports"
        try:
            self.assertIn("синхронизируем", srv.read_table_data("RBKP", "sandbox", 5, "", "BELNR", output="file"))
        finally:
            srv.EXPORT_DIR = saved


class AggregateTests(Base):
    def expected(self, rows, keys):
        out = {}
        for r in rows:
            g = tuple(r[k] for k in keys)
            c, s = out.get(g, (0, Decimal(0)))
            out[g] = (c + 1, s + r["BEZNK"])
        return out

    def parse(self, res, ngroups):
        lines = res.split("-" * 80 + "\n")[1].strip().splitlines()
        return {tuple(x.split(" | ")[:ngroups]): x.split(" | ")[ngroups:] for x in lines}

    def test_group_count_sum(self):
        res = srv.aggregate_table_data("RBKP", "sandbox", "GJAHR = '2026' AND BEZNK <> 0", "LIFNR, WAERS",
                                       "count,sum:BEZNK,min:BEZNK,max:BELNR")
        got = self.parse(res, 2)
        exp = self.expected(FakeSAP.rows, ["LIFNR", "WAERS"])
        self.assertEqual(len(got), len(exp))
        for g, (c, s) in exp.items():
            self.assertEqual(int(got[g][0]), c)
            self.assertEqual(Decimal(got[g][1]), s)
        self.assertIn("Просмотрено строк: 6000", res)
        self.assertNotIn("лимит просмотра", res)
        self.assertNotIn("разными WAERS", res)
        counts = [int(v[0]) for v in got.values()]
        self.assertEqual(counts, sorted(counts, reverse=True))
        log = last_log()
        self.assertEqual((log["mode"], log["scanned"], log["groups"]), ("aggregate", 6000, len(exp)))

    def test_mixed_currency_warning(self):
        res = srv.aggregate_table_data("RBKP", "sandbox", "", "BLART", "count,sum:BEZNK")
        self.assertIn("разными WAERS", res)
        self.assertIn("Добавь WAERS в group_by", res)

    def test_single_currency_ok(self):
        for r in FakeSAP.rows:
            r["WAERS"] = "KZT"
        res = srv.aggregate_table_data("RBKP", "sandbox", "", "BLART", "sum:BEZNK")
        self.assertIn("внутри каждой группы WAERS одна", res)

    def test_total_count_only(self):
        res = srv.aggregate_table_data("RBKP", "sandbox", "", "", "count")
        self.assertIn("count\n", res)
        self.assertIn("\n6000\n", res)

    def test_scan_limit(self):
        FakeSAP.rows = make_rows(9000)
        res = srv.aggregate_table_data("RBKP", "sandbox", "", "BLART", "count")
        self.assertIn("Упёрлись в лимит просмотра 8000", res)
        self.assertEqual(last_log()["scanned"], 8000)

    def test_unstable_order_exact(self):
        FakeSAP.shuffle_pages = True
        res = srv.aggregate_table_data("RBKP", "sandbox", "BEZNK <> 0", "BLART,WAERS", "count,sum:BEZNK")
        got = self.parse(res, 2)
        exp = self.expected([r for r in FakeSAP.rows if r["BEZNK"] != 0], ["BLART", "WAERS"])
        self.assertEqual({g: (int(v[0]), Decimal(v[1])) for g, v in got.items()}, exp)

    def test_or_in_where_is_parenthesized(self):
        res = srv.aggregate_table_data("RBKP", "sandbox", "BLART = 'RE' OR BLART = 'ST'", "BLART", "count")
        got = self.parse(res, 1)
        exp = self.expected([r for r in FakeSAP.rows if r["BLART"] in ("RE", "ST")], ["BLART"])
        self.assertEqual({g: int(v[0]) for g, v in got.items()}, {g: c for g, (c, _) in exp.items()})

    def test_scan_limit_exact_no_flag(self):
        FakeSAP.rows = make_rows(8000)
        res = srv.aggregate_table_data("RBKP", "sandbox", "", "BLART", "count")
        self.assertNotIn("лимит просмотра", res)
        self.assertIn("Просмотрено строк: 8000", res)

    def test_validation(self):
        self.assertIn("не больше 3", srv.aggregate_table_data("RBKP", "sandbox", "", "A,B,C,D"))
        self.assertIn("Некорректная мера", srv.aggregate_table_data("RBKP", "sandbox", "", "", "avg:BEZNK"))
        self.assertIn("Некорректная мера", srv.aggregate_table_data("RBKP", "sandbox", "", "", "sum"))
        self.assertIn("нет полей: NOPE", srv.aggregate_table_data("RBKP", "sandbox", "", "NOPE"))
        self.assertIn("суммировать можно", srv.aggregate_table_data("RBKP", "sandbox", "", "", "sum:LIFNR"))


class RangeTests(unittest.TestCase):
    def test_tuple_cmp(self):
        self.assertEqual(srv._tuple_cmp(["A", "B"], ("1", "x'y"), ">="),
                         "( A > '1' OR ( A = '1' AND B >= 'x''y' ) )")
        self.assertEqual(srv._range_where("X = '1' OR Y = '2'", ["A"], ("5",), ("9",)),
                         "( X = '1' OR Y = '2' ) AND A >= '5' AND A < '9'")


class RegistrationTests(unittest.TestCase):
    def test_tools(self):
        import asyncio
        from fastmcp import Client

        async def run():
            async with Client(srv.mcp) as c:
                return await c.list_tools()

        tools = {t.name: t for t in asyncio.run(run())}
        self.assertEqual(set(tools), {"get_table_structure", "read_table_data", "aggregate_table_data",
                                      "search_tables"})
        self.assertIn("output='file'", tools["read_table_data"].description)
        self.assertIn("500 / 1000", tools["read_table_data"].description)
        self.assertIn("output", (getattr(tools["read_table_data"], "input_schema", None) or tools["read_table_data"].inputSchema)["properties"])
        self.assertIn("measures", (getattr(tools["aggregate_table_data"], "input_schema", None) or tools["aggregate_table_data"].inputSchema)["properties"])


if __name__ == "__main__":
    unittest.main()
