import os, json, csv
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, send_file, abort, make_response
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

APP_TITLE = "Agenda de Prestadores"
DATE_FMT = "%d/%m/%Y"
TIME_FMT = "%H:%M"

CSV_PATH = Path(os.getenv("CSV_PATH", "servicos_web.csv")).resolve()
SUPPLIERS_CSV = Path(os.getenv("SUPPLIERS_CSV", "fornecedores.csv")).resolve()

LOJAS = [
    "4g Comércio de Alimentos e Bebidas Ltda",
    "Tenro Plaza Niterói Ltda",
    "Gdm Cafés e Bolos Ltda",
    "Tenro Café Américas",
]

def _default_pins():
    return {
        "4g Comércio de Alimentos e Bebidas Ltda": "1111",
        "Tenro Plaza Niterói Ltda": "2222",
        "Gdm Cafés e Bolos Ltda": "3333",
        "Tenro Café Américas": "4444",
    }

ADMIN_PIN = os.getenv("ADMIN_PIN", "9999")
try:
    LOJA_PINS = json.loads(os.getenv("LOJA_PINS_JSON", "")) or _default_pins()
except Exception:
    LOJA_PINS = _default_pins()

app = Flask(__name__)

# ---------- util ----------
def add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    days_in_month = [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
                     31,30,31,30,31,31,30,31,30,31][month-1]
    day = min(dt.day, days_in_month)
    return dt.replace(year=year, month=month, day=day)

def fmt_date(s: str) -> str:
    s = (s or "").strip()
    if not s: return ""
    if "-" in s and len(s) == 10:  # yyyy-mm-dd -> dd/mm/aaaa
        a, m, d = s.split("-")
        return f"{d}/{m}/{a}"
    if "/" in s and len(s) == 10:  # dd/mm/aaaa
        d, m, a = s.split("/")
        return f"{d.zfill(2)}/{m.zfill(2)}/{a}"
    if s.isdigit() and len(s) == 8:  # ddmmyyyy
        d, m, a = s[:2], s[2:4], s[4:]
        return f"{d}/{m}/{a}"
    return s

def fmt_time(s: str) -> str:
    s = (s or "").strip()
    if not s: return ""
    if ":" in s:
        hh, mm = (s.split(":") + ["00"])[:2]
        return f"{hh.zfill(2)}:{mm.zfill(2)}"
    if s.isdigit() and len(s) in (3,4):
        s = s.zfill(4)
        return f"{s[:2]}:{s[2:]}"
    if s.isdigit() and len(s) in (1,2):
        return f"{s.zfill(2)}:00"
    return s

def dmy_to_iso(dmy: str) -> str:
    """Converte 'dd/mm/aaaa' -> 'aaaa-mm-dd' (para preencher <input type=date>)"""
    try:
        d, m, a = dmy.split("/")
        return f"{a}-{m.zfill(2)}-{d.zfill(2)}"
    except Exception:
        return ""

def read_all():
    rows = []
    if CSV_PATH.exists():
        with CSV_PATH.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                r["data_servico"] = fmt_date(r.get("data_servico",""))
                r["hora_servico"] = fmt_time(r.get("hora_servico",""))
                if not r.get("proxima_data"):
                    try:
                        base_dt = datetime.strptime(
                            f'{r["data_servico"]} {r["hora_servico"]}', f"{DATE_FMT} {TIME_FMT}"
                        )
                        nxt = add_months(base_dt, {"1 mês":1,"2 meses":2,"3 meses":3}.get(r.get("prazo","1 mês"),1))
                        r["proxima_data"] = nxt.strftime(DATE_FMT)
                    except Exception:
                        r["proxima_data"] = r.get("proxima_data","")
                rows.append(r)
    return rows

def write_all(rows):
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["loja","empresa","funcionario","data_servico","hora_servico","prazo","proxima_data"])
        writer.writeheader()
        for r in rows:
            r = r.copy()
            r["data_servico"] = fmt_date(r.get("data_servico",""))
            r["hora_servico"] = fmt_time(r.get("hora_servico",""))
            writer.writerow(r)

# ---------- catálogo de fornecedores ----------
def read_suppliers():
    items = []
    if SUPPLIERS_CSV.exists():
        with SUPPLIERS_CSV.open("r", newline="", encoding="utf-8") as f:
            rd = csv.reader(f)
            for row in rd:
                if row:
                    items.append(row[0].strip())
    else:
        items = []
        write_suppliers(items)
    seen = set(); uniq = []
    for s in items:
        k = s.strip()
        if k and k.lower() not in seen:
            seen.add(k.lower()); uniq.append(k)
    return uniq

def write_suppliers(items):
    SUPPLIERS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with SUPPLIERS_CSV.open("w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        for it in items:
            wr.writerow([it.strip()])

# ---------- acesso ----------
def check_access(loja, pin):
    if pin == ADMIN_PIN and ADMIN_PIN:
        return "admin", None
    if loja and pin and LOJA_PINS.get(loja) == pin:
        return "loja", loja
    if loja:
        return "denied", None
    return "open", None

# ---------- rotas ----------
@app.route("/", methods=["GET","POST"])
def index():
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()
    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode == "denied":
        return abort(403, description="Acesso negado")

    fornecedores = read_suppliers()

    # --- filtros ---
    filtro_inicio = request.args.get("inicio","").strip()
    filtro_fim = request.args.get("fim","").strip()

    rows = read_all()
    if loja_autorizada:
        rows = [r for r in rows if r.get("loja","") == loja_autorizada]

    # aplicar filtro por período
    if filtro_inicio and filtro_fim:
        try:
            di = datetime.strptime(filtro_inicio, "%Y-%m-%d")
            df = datetime.strptime(filtro_fim, "%Y-%m-%d")
            def parse_dmy(s):
                try:
                    return datetime.strptime(s, "%d/%m/%Y")
                except: return None
            rows = [r for r in rows if (d:=parse_dmy(r.get("data_servico",""))) and di <= d <= df]
        except: pass

    return render_template("index.html", title=APP_TITLE, lojas=LOJAS,
                           rows=rows, count=len(rows),
                           loja_lock=loja_autorizada, mode=mode, pin=pin,
                           fornecedores=fornecedores,
                           filtro_inicio=filtro_inicio, filtro_fim=filtro_fim)

# ---------- exportar PDF ----------
@app.route("/download/pdf")
def download_pdf():
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()
    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode == "denied":
        return abort(403)

    filtro_inicio = request.args.get("inicio","").strip()
    filtro_fim = request.args.get("fim","").strip()

    rows = read_all()
    if loja_autorizada:
        rows = [r for r in rows if r.get("loja","") == loja_autorizada]

    if filtro_inicio and filtro_fim:
        try:
            di = datetime.strptime(filtro_inicio, "%Y-%m-%d")
            df = datetime.strptime(filtro_fim, "%Y-%m-%d")
            def parse_dmy(s):
                try: return datetime.strptime(s, "%d/%m/%Y")
                except: return None
            rows = [r for r in rows if (d:=parse_dmy(r.get("data_servico",""))) and di <= d <= df]
        except: pass

    # gerar PDF
    buf = []
    pdf_file = "relatorio.pdf"
    doc = SimpleDocTemplate(pdf_file, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph("Relatório - Agenda de Prestadores", styles["Heading1"]))
    elements.append(Spacer(1,12))

    data = [["Loja","Empresa","Funcionário","Data","Hora","Prazo","Próxima Data"]]
    for r in rows:
        data.append([r["loja"],r["empresa"],r["funcionario"],r["data_servico"],
                     r["hora_servico"],r["prazo"],r["proxima_data"]])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.grey),
        ("TEXTCOLOR",(0,0),(-1,0),colors.whitesmoke),
        ("ALIGN",(0,0),(-1,-1),"CENTER"),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("BOTTOMPADDING",(0,0),(-1,0),12),
        ("BACKGROUND",(0,1),(-1,-1),colors.beige),
        ("GRID",(0,0),(-1,-1),1,colors.black),
    ]))

    elements.append(table)
    doc.build(elements)

    return send_file(pdf_file, as_attachment=True)

