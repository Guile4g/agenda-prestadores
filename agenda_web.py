import os, json, csv, io
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, send_file, abort, Response

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

APP_TITLE = "Agenda de Prestadores"
DATE_FMT = "%d/%m/%Y"
TIME_FMT = "%H:%M"

# Arquivos de dados
CSV_PATH = Path(os.getenv("CSV_PATH", "servicos_web.csv")).resolve()
SUPPLIERS_CSV = Path(os.getenv("SUPPLIERS_CSV", "fornecedores.csv")).resolve()

# Lojas e PINs
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

# -------------------- Utils --------------------
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
    if "-" in s and len(s) == 10:  # aaaa-mm-dd -> dd/mm/aaaa
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
    # 'dd/mm/aaaa' -> 'aaaa-mm-dd'
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

app.jinja_env.filters['dmy_to_iso'] = dmy_to_iso

# -------------------- Dados --------------------
def read_all():
    rows = []
    if CSV_PATH.exists():
        with CSV_PATH.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                # normaliza formatos
                r["data"] = fmt_date(r.get("data",""))
                r["hora"] = fmt_time(r.get("hora",""))
                rows.append(r)
    return rows

def write_all(rows):
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "loja","empresa","funcionario","data","hora",
            "prazo","proxima_data","observacoes"
        ])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

# -------------------- Fornecedores (somente admin) --------------------
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
    # remove duplicatas (case-insensitive)
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

# -------------------- Acesso --------------------
def check_access(loja, pin):
    """Retorna (mode, loja_autorizada)
       mode: 'admin' | 'loja' | 'denied' | 'open'
    """
    if pin == ADMIN_PIN and ADMIN_PIN:
        return "admin", None
    if loja and pin and LOJA_PINS.get(loja) == pin:
        return "loja", loja
    if loja:
        return "denied", None
    return "open", None

# -------------------- Filtro por período --------------------
def apply_period_filter(rows, inicio_iso: str, fim_iso: str):
    if not (inicio_iso and fim_iso):
        return rows
    try:
        di = datetime.strptime(inicio_iso, "%Y-%m-%d")
        df = datetime.strptime(fim_iso, "%Y-%m-%d")
    except Exception:
        return rows
    out = []
    for r in rows:
        d = parse_dmy_opt(r.get("data",""))
        if d and di <= d <= df:
            out.append(r)
    return out

# -------------------- Cálculo próxima data --------------------
def calcular_proxima(data_servico: str, hora_servico: str, prazo: str, personalizado_meses: str):
    try:
        base_dt = datetime.strptime(f"{data_servico} {hora_servico}", f"{DATE_FMT} {TIME_FMT}")
    except Exception:
        return ""

    if prazo == "15 dias":
        return (base_dt + timedelta(days=15)).strftime(DATE_FMT)
    if prazo == "1 mês":
        return add_months(base_dt, 1).strftime(DATE_FMT)
    if prazo == "2 meses":
        return add_months(base_dt, 2).strftime(DATE_FMT)
    if prazo == "3 meses":
        return add_months(base_dt, 3).strftime(DATE_FMT)
    if prazo == "6 meses":
        return add_months(base_dt, 6).strftime(DATE_FMT)
    if prazo == "12 meses":
        return add_months(base_dt, 12).strftime(DATE_FMT)
    if prazo == "personalizado" and personalizado_meses.isdigit():
        return add_months(base_dt, int(personalizado_meses)).strftime(DATE_FMT)
    return ""

# -------------------- Rotas --------------------
@app.route("/", methods=["GET","POST"])
def index():
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()
    mode, loja_autorizada = check_access(loja_lock, pin)

    # Bloqueia acesso “aberto” para evitar ver todas as lojas sem PIN
    if mode == "open":
        return abort(403, description="Acesso restrito. Use o link com ?loja=...&pin=... ou ?pin=9999 (admin).")
    if mode == "denied":
        return abort(403, description="PIN inválido para a loja informada.")

    # fornecedores: apenas admin pode cadastrar/editar; lojas só escolhem
    fornecedores = read_suppliers()

    # filtros (GET)
    filtro_inicio = request.args.get("inicio","").strip()
    filtro_fim = request.args.get("fim","").strip()

    if request.method == "POST":
        # loja travada: se 'loja' vier do form e não bate com loja_autorizada, rejeita
        loja_form = request.form.get("loja","").strip()
        loja_final = loja_autorizada or loja_form
        if mode == "loja":
            # forçar que a loja do registro seja exatamente a autorizada
            loja_final = loja_autorizada

        empresa = request.form.get("empresa","").strip()
        funcionario = request.form.get("funcionario","").strip()
        data = fmt_date(request.form.get("data",""))
        hora = fmt_time(request.form.get("hora",""))
        prazo = request.form.get("prazo","").strip()
        personalizado = request.form.get("personalizado","").strip()
        observacoes = request.form.get("observacoes","").strip()

        # Impede que loja tente cadastrar fornecedor novo: só pode escolher da lista
        if empresa not in fornecedores:
            return abort(400, description="Fornecedor inválido. Peça ao gestor para cadastrar em /fornecedores?pin=9999")

        proxima = calcular_proxima(data, hora, prazo, personalizado)

        rows = read_all()
        rows.append({
            "loja": loja_final, "empresa": empresa, "funcionario": funcionario,
            "data": data, "hora": hora,
            "prazo": (prazo if prazo != "personalizado" else f"{personalizado} meses"),
            "proxima_data": proxima,
            "observacoes": observacoes
        })
        write_all(rows)
        return redirect(url_for("index", loja=loja_lock, pin=pin, inicio=filtro_inicio, fim=filtro_fim))

    # GET
    rows = read_all()

    # escopo por loja
    if mode == "loja":
        rows = [r for r in rows if r.get("loja","") == loja_autorizada]

    # filtro por período
    rows = apply_period_filter(rows, filtro_inicio, filtro_fim)

    return render_template("index.html",
                           title=APP_TITLE,
                           lojas=LOJAS,
                           fornecedores=fornecedores,
                           registros=rows,
                           count=len(rows),
                           loja_lock=(loja_autorizada if mode=="loja" else None),
                           mode=mode, pin=pin,
                           filtro_inicio=filtro_inicio, filtro_fim=filtro_fim)

@app.route("/edit/<int:id>", methods=["GET","POST"])
def edit(id):
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()
    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode == "open":
        return abort(403)
    if mode == "denied":
        return abort(403)

    fornecedores = read_suppliers()

    rows = read_all()
    if id < 0 or id >= len(rows):
        return abort(404)
    reg = rows[id]

    # loja só pode editar seus próprios registros
    if mode == "loja" and reg.get("loja","") != loja_autorizada:
        return abort(403)

    if request.method == "POST":
        # Empresa deve ser da lista (somente admin pode gerenciar catálogo em /fornecedores)
        empresa = request.form.get("empresa","").strip()
        if empresa not in fornecedores:
            return abort(400, description="Fornecedor inválido. Solicite ao gestor incluir o fornecedor.")
        reg["empresa"] = empresa

        reg["funcionario"] = request.form.get("funcionario","").strip()
        reg["data"] = fmt_date(request.form.get("data",""))
        reg["hora"] = fmt_time(request.form.get("hora",""))
        prazo = request.form.get("prazo","").strip()
        personalizado = request.form.get("personalizado","").strip()
        reg["observacoes"] = request.form.get("observacoes","").strip()
        reg["prazo"] = (prazo if prazo != "personalizado" else f"{personalizado} meses")
        reg["proxima_data"] = calcular_proxima(reg["data"], reg["hora"], prazo, personalizado)

        rows[id] = reg
        write_all(rows)
        return redirect(url_for("index", loja=loja_lock, pin=pin))

    return render_template("edit.html", registro=reg, id=id, fornecedores=fornecedores)

@app.route("/delete/<int:id>")
def delete(id):
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()
    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode == "open":
        return abort(403)
    if mode == "denied":
        return abort(403)

    rows = read_all()
    if id < 0 or id >= len(rows):
        return abort(404)

    reg = rows[id]
    if mode == "loja" and reg.get("loja","") != loja_autorizada:
        return abort(403)

    del rows[id]
    write_all(rows)
    return redirect(url_for("index", loja=loja_lock, pin=pin))

# --------- Administração de fornecedores (somente admin) ----------
@app.route("/fornecedores", methods=["GET","POST"])
def fornecedores_admin():
    pin = request.args.get("pin","").strip()
    mode, _ = check_access("", pin)
    if mode != "admin":
        return abort(403, description="Somente admin")

    items = read_suppliers()

    if request.method == "POST":
        novo = (request.form.get("novo","") or "").strip()
        if novo:
            # normaliza um pouco (Capitalize palavras)
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

# -------------------- Exportações --------------------
@app.route("/download/csv")
def download_csv():
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()
    inicio = request.args.get("inicio","").strip()
    fim = request.args.get("fim","").strip()

    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode in ("open","denied"):
        return abort(403)

    rows = read_all()
    if mode == "loja":
        rows = [r for r in rows if r.get("loja","") == loja_autorizada]
    rows = apply_period_filter(rows, inicio, fim)

    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(["loja","empresa","funcionario","data","hora","prazo","proxima_data","observacoes"])
    for r in rows:
        writer.writerow([r.get("loja",""), r.get("empresa",""), r.get("funcionario",""),
                         r.get("data",""), r.get("hora",""), r.get("prazo",""),
                         r.get("proxima_data",""), r.get("observacoes","")])
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
    if mode in ("open","denied"):
        return abort(403)

    rows = read_all()
    if mode == "loja":
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

    data = [["Loja","Empresa","Funcionário","Data","Hora","Prazo","Próxima Data","Observações"]]
    for r in rows:
        data.append([r.get("loja",""), r.get("empresa",""), r.get("funcionario",""),
                     r.get("data",""), r.get("hora",""), r.get("prazo",""),
                     r.get("proxima_data",""), r.get("observacoes","")])

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
