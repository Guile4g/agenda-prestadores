import os, json, csv, io
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, send_file, abort, Response
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

APP_TITLE = "Agenda de Prestadores"
DATE_FMT = "%d/%m/%Y"
TIME_FMT = "%H:%M"

CSV_PATH = Path(os.getenv("CSV_PATH", "servicos_web.csv")).resolve()

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
    return s

def dmy_to_iso(dmy: str) -> str:
    try:
        d, m, a = dmy.split("/")
        return f"{a}-{m.zfill(2)}-{d.zfill(2)}"
    except Exception:
        return ""

def parse_dmy_opt(s: str):
    try:
        return datetime.strptime(s, "%d/%m/%Y")
    except Exception:
        return None

# ---------- dados ----------
def read_all():
    rows = []
    if CSV_PATH.exists():
        with CSV_PATH.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                r["data"] = fmt_date(r.get("data",""))
                r["hora"] = fmt_time(r.get("hora",""))
                rows.append(r)
    return rows

def write_all(rows):
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["loja","empresa","funcionario","data","hora","prazo","proxima_data","observacoes"])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

# ---------- acesso ----------
def check_access(loja, pin):
    if pin == ADMIN_PIN and ADMIN_PIN:
        return "admin", None
    if loja and pin and LOJA_PINS.get(loja) == pin:
        return "loja", loja
    if loja:
        return "denied", None
    return "open", None

# ---------- cálculo próxima data ----------
def calcular_proxima(data_servico: str, hora_servico: str, prazo: str, personalizado: str):
    try:
        base_dt = datetime.strptime(f"{data_servico} {hora_servico}", f"{DATE_FMT} {TIME_FMT}")
    except Exception:
        return ""

    if prazo == "15 dias":
        return (base_dt + timedelta(days=15)).strftime(DATE_FMT)
    elif prazo == "1 mês":
        return add_months(base_dt, 1).strftime(DATE_FMT)
    elif prazo == "2 meses":
        return add_months(base_dt, 2).strftime(DATE_FMT)
    elif prazo == "3 meses":
        return add_months(base_dt, 3).strftime(DATE_FMT)
    elif prazo == "6 meses":
        return add_months(base_dt, 6).strftime(DATE_FMT)
    elif prazo == "12 meses":
        return add_months(base_dt, 12).strftime(DATE_FMT)
    elif prazo == "personalizado" and personalizado.isdigit():
        return add_months(base_dt, int(personalizado)).strftime(DATE_FMT)
    return ""

# ---------- rotas ----------
@app.route("/", methods=["GET","POST"])
def index():
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()
    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode == "denied":
        return abort(403, description="Acesso negado")

    filtro_inicio = request.args.get("inicio","").strip()
    filtro_fim = request.args.get("fim","").strip()

    if request.method == "POST":
        loja = loja_autorizada or request.form.get("loja","").strip()
        empresa = request.form.get("empresa","").strip()
        funcionario = request.form.get("funcionario","").strip()
        data = fmt_date(request.form.get("data",""))
        hora = fmt_time(request.form.get("hora",""))
        prazo = request.form.get("prazo","")
        personalizado = request.form.get("personalizado","")
        observacoes = request.form.get("observacoes","").strip()

        proxima = calcular_proxima(data, hora, prazo, personalizado)

        rows = read_all()
        rows.append({
            "loja": loja, "empresa": empresa, "funcionario": funcionario,
            "data": data, "hora": hora, "prazo": prazo if prazo != "personalizado" else f"{personalizado} meses",
            "proxima_data": proxima, "observacoes": observacoes
        })
        write_all(rows)
        return redirect(url_for("index", loja=loja_lock, pin=pin, inicio=filtro_inicio, fim=filtro_fim))

    rows = read_all()
    if loja_autorizada:
        rows = [r for r in rows if r.get("loja","") == loja_autorizada]

    # filtro por período
    if filtro_inicio and filtro_fim:
        try:
            di = datetime.strptime(filtro_inicio, "%Y-%m-%d")
            df = datetime.strptime(filtro_fim, "%Y-%m-%d")
            rows = [r for r in rows if parse_dmy_opt(r.get("data","")) and di <= parse_dmy_opt(r.get("data","")) <= df]
        except:
            pass

    return render_template("index.html", registros=rows, count=len(rows),
                           loja_lock=loja_autorizada, mode=mode, pin=pin,
                           filtro_inicio=filtro_inicio, filtro_fim=filtro_fim)

# ---------- exportações ----------
@app.route("/download/csv")
def download_csv():
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()
    inicio = request.args.get("inicio","").strip()
    fim = request.args.get("fim","").strip()

    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode == "denied":
        return abort(403)

    rows = read_all()
    if loja_autorizada:
        rows = [r for r in rows if r.get("loja","") == loja_autorizada]

    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(["loja","empresa","funcionario","data","hora","prazo","proxima_data","observacoes"])
    for r in rows:
        writer.writerow([r["loja"], r["empresa"], r["funcionario"], r["data"], r["hora"], r["prazo"], r["proxima_data"], r.get("observacoes","")])
    output = si.getvalue().encode("utf-8")
    return Response(
        output,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment;filename=relatorio.csv"}
    )

@app.route("/download/pdf")
def download_pdf():
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()

    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode == "denied":
        return abort(403)

    rows = read_all()
    if loja_autorizada:
        rows = [r for r in rows if r.get("loja","") == loja_autorizada]

    pdf_file = "relatorio.pdf"
    doc = SimpleDocTemplate(pdf_file, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []
    elements.append(Paragraph("Relatório - Agenda de Prestadores", styles["Heading1"]))
    elements.append(Spacer(1,12))

    data = [["Loja","Empresa","Funcionário","Data","Hora","Prazo","Próxima Data","Observações"]]
    for r in rows:
        data.append([r["loja"], r["empresa"], r["funcionario"], r["data"], r["hora"], r["prazo"], r["proxima_data"], r.get("observacoes","")])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.grey),
        ("TEXTCOLOR",(0,0),(-1,0),colors.whitesmoke),
        ("ALIGN",(0,0),(-1,-1),"CENTER"),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("BOTTOMPADDING",(0,0),(-1,0),12),
        ("BACKGROUND",(0,1),(-1,-1),colors.whitesmoke),
        ("GRID",(0,0),(-1,-1),1,colors.black),
    ]))
    elements.append(table)
    doc.build(elements)

    return send_file(pdf_file, as_attachment=True)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
