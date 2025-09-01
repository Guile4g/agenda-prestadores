import os, json, csv, io
from datetime import datetime
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
    """Converte 'dd/mm/aaaa' -> 'aaaa-mm-dd' (para <input type=date>)"""
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

# ---------- filtro ----------
def apply_period_filter(rows, inicio_iso: str, fim_iso: str):
    """Filtra rows pelo intervalo [inicio, fim] (datas 'aaaa-mm-dd')."""
    if not (inicio_iso and fim_iso):
        return rows
    try:
        di = datetime.strptime(inicio_iso, "%Y-%m-%d")
        df = datetime.strptime(fim_iso, "%Y-%m-%d")
    except Exception:
        return rows
    out = []
    for r in rows:
        d = parse_dmy_opt(r.get("data_servico",""))
        if d and di <= d <= df:
            out.append(r)
    return out

# ---------- rotas ----------
@app.route("/", methods=["GET","POST"])
def index():
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()
    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode == "denied":
        return abort(403, description="Acesso negado")

    fornecedores = read_suppliers()

    # filtros (GET)
    filtro_inicio = request.args.get("inicio","").strip()
    filtro_fim = request.args.get("fim","").strip()

    if request.method == "POST":
        # cadastro
        loja = loja_autorizada or request.form.get("loja","").strip()
        empresa_sel = request.form.get("empresa_sel","").strip()
        empresa_outro = request.form.get("empresa_outro","").strip()
        empresa = empresa_outro if empresa_outro else empresa_sel

        def norm_name(x: str) -> str:
            return " ".join(part.capitalize() for part in x.split())
        empresa = norm_name(empresa)

        funcionario = request.form.get("funcionario","").strip()
        data_servico = fmt_date(request.form.get("data_servico",""))
        hora_servico = fmt_time(request.form.get("hora_servico",""))
        prazo = request.form.get("prazo","1 mês").strip()

        if empresa and empresa not in fornecedores:
            fornecedores.append(empresa)
            write_suppliers(fornecedores)

        proxima_data = ""
        try:
            base_dt = datetime.strptime(f"{data_servico} {hora_servico}", f"{DATE_FMT} {TIME_FMT}")
            nxt = add_months(base_dt, {"1 mês":1,"2 meses":2,"3 meses":3}.get(prazo,1))
            proxima_data = nxt.strftime(DATE_FMT)
        except Exception:
            proxima_data = ""

        rows = read_all()
        rows.append({
            "loja": loja, "empresa": empresa, "funcionario": funcionario,
            "data_servico": data_servico, "hora_servico": hora_servico,
            "prazo": prazo, "proxima_data": proxima_data
        })
        write_all(rows)
        # preserva filtros na volta
        return redirect(url_for("index", loja=loja_lock, pin=pin, inicio=filtro_inicio, fim=filtro_fim))

    rows = read_all()
    if loja_autorizada:
        rows = [r for r in rows if r.get("loja","") == loja_autorizada]
    rows = apply_period_filter(rows, filtro_inicio, filtro_fim)

    return render_template("index.html", title=APP_TITLE, lojas=LOJAS,
                           rows=rows, count=len(rows),
                           loja_lock=loja_autorizada, mode=mode, pin=pin,
                           fornecedores=fornecedores,
                           filtro_inicio=filtro_inicio, filtro_fim=filtro_fim)

@app.route("/delete/<int:idx>", methods=["POST"])
def delete(idx):
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()
    inicio = request.args.get("inicio","").strip()
    fim = request.args.get("fim","").strip()

    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode == "denied":
        return abort(403)

    rows = read_all()
    if 0 <= idx < len(rows):
        if loja_autorizada and rows[idx].get("loja","") != loja_autorizada:
            return abort(403)
        del rows[idx]
        write_all(rows)
    return redirect(url_for("index", loja=loja_lock, pin=pin, inicio=inicio, fim=fim))

@app.route("/edit/<int:idx>", methods=["GET","POST"])
def edit(idx):
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()
    inicio = request.args.get("inicio","").strip()
    fim = request.args.get("fim","").strip()

    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode == "denied":
        return abort(403)

    rows = read_all()
    if not (0 <= idx < len(rows)):
        return abort(404)
    row = rows[idx]

    if loja_autorizada and row.get("loja","") != loja_autorizada:
        return abort(403)

    fornecedores = read_suppliers()

    if request.method == "POST":
        loja = loja_autorizada or request.form.get("loja","").strip()
        empresa_sel = request.form.get("empresa_sel","").strip()
        empresa_outro = request.form.get("empresa_outro","").strip()
        empresa = empresa_outro if empresa_outro else empresa_sel

        def norm_name(x: str) -> str:
            return " ".join(part.capitalize() for part in x.split())
        empresa = norm_name(empresa)

        funcionario = request.form.get("funcionario","").strip()
        data_servico = fmt_date(request.form.get("data_servico",""))
        hora_servico = fmt_time(request.form.get("hora_servico",""))
        prazo = request.form.get("prazo","1 mês").strip()

        if empresa and empresa not in fornecedores:
            fornecedores.append(empresa)
            write_suppliers(fornecedores)

        proxima_data = ""
        try:
            base_dt = datetime.strptime(f"{data_servico} {hora_servico}", f"{DATE_FMT} {TIME_FMT}")
            nxt = add_months(base_dt, {"1 mês":1,"2 meses":2,"3 meses":3}.get(prazo,1))
            proxima_data = nxt.strftime(DATE_FMT)
        except Exception:
            proxima_data = ""

        rows[idx] = {
            "loja": loja, "empresa": empresa, "funcionario": funcionario,
            "data_servico": data_servico, "hora_servico": hora_servico,
            "prazo": prazo, "proxima_data": proxima_data
        }
        write_all(rows)
        return redirect(url_for("index", loja=loja_lock, pin=pin, inicio=inicio, fim=fim))

    prefilled = {
        "loja": row.get("loja",""),
        "empresa": row.get("empresa",""),
        "funcionario": row.get("funcionario",""),
        "data_iso": dmy_to_iso(row.get("data_servico","")),
        "hora": row.get("hora_servico",""),
        "prazo": row.get("prazo","1 mês"),
    }
    return render_template("edit.html",
                           title=APP_TITLE, idx=idx,
                           lojas=LOJAS, fornecedores=fornecedores,
                           loja_lock=loja_autorizada, mode=mode, pin=pin,
                           form=prefilled)

# ----- admin fornecedores -----
@app.route("/fornecedores", methods=["GET","POST"])
def fornecedores_admin():
    pin = request.args.get("pin","").strip()
    mode, _ = check_access("", pin)
    if mode != "admin":
        # <<< ESTA LINHA ESTAVA CORTADA NO SEU ARQUIVO >>>
        return abort(403, description="Somente admin")

    items = read_suppliers()

    if request.method == "POST":
        novo = request.form.get("novo","").strip()
        if novo:
            novo_norm = " ".join(p.capitalize() for p in novo.split())
            if novo_norm not in items:
                items.append(novo_norm)
                write_suppliers(items)
        return redirect(url_for("fornecedores_admin", pin=pin))

    action = request.args.get("action","")
    idx = request.args.get("idx","")
    if action == "del" and idx.isdigit():
        i = int(idx)
        if 0 <= i < len(items):
            del items[i]
            write_suppliers(items)
        return redirect(url_for("fornecedores_admin", pin=pin))

    html = ["<h2>Catálogo de Fornecedores (Admin)</h2>",
            "<form method='post'>Adicionar fornecedor:<br>",
            "<input name='novo' style='width:300px'> ",
            "<button>Adicionar</button></form><hr>",
            "<ol>"]
    for i, name in enumerate(items):
        link_del = url_for('fornecedores_admin', pin=pin, action='del', idx=i)
        html.append(f"<li>{name} &nbsp; <a href='{link_del}' onclick=\"return confirm('Remover?');\">remover</a></li>")
    html.append("</ol>")
    back = url_for('index', pin=pin)
    html.append(f"<p><a href='{back}'>Voltar</a></p>")
    return "\n".join(html)

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
    rows = apply_period_filter(rows, inicio, fim)

    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(["loja","empresa","funcionario","data_servico","hora_servico","prazo","proxima_data"])
    for r in rows:
        writer.writerow([r["loja"], r["empresa"], r["funcionario"],
                         r["data_servico"], r["hora_servico"], r["prazo"], r["proxima_data"]])
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
    inicio = request.args.get("inicio","").strip()
    fim = request.args.get("fim","").strip()

    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode == "denied":
        return abort(403)

    rows = read_all()
    if loja_autorizada:
        rows = [r for r in rows if r.get("loja","") == loja_autorizada]
    rows = apply_period_filter(rows, inicio, fim)

    pdf_file = "relatorio.pdf"
    doc = SimpleDocTemplate(pdf_file, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []
    title = "Relatório - Agenda de Prestadores"
    if inicio and fim:
        title += f" (De {inicio} até {fim})"
    elements.append(Paragraph(title, styles["Heading1"]))
    elements.append(Spacer(1,12))

    data = [["Loja","Empresa","Funcionário","Data","Hora","Prazo","Próxima Data"]]
    for r in rows:
        data.append([r["loja"], r["empresa"], r["funcionario"],
                     r["data_servico"], r["hora_servico"], r["prazo"], r["proxima_data"]])

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
