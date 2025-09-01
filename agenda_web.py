import os, json, csv
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, send_file, abort

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

def add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    days_in_month = [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
                     31,30,31,30,31,31,30,31,30,31][month-1]
    day = min(dt.day, days_in_month)
    return dt.replace(year=year, month=month, day=day)

# --------- FORMATADORES (aceitam 01092025, 2025-09-01, 14:30, 1430 etc.) ----------
def fmt_date(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if "-" in s and len(s) == 10:  # yyyy-mm-dd
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
    if not s:
        return ""
    if ":" in s:
        hh, mm = (s.split(":") + ["00"])[:2]
        return f"{hh.zfill(2)}:{mm.zfill(2)}"
    if s.isdigit() and len(s) in (3,4):  # 930 -> 09:30 / 1430 -> 14:30
        s = s.zfill(4)
        return f"{s[:2]}:{s[2:]}"
    if s.isdigit() and len(s) in (1,2):  # 9 -> 09:00
        return f"{s.zfill(2)}:00"
    return s
# -----------------------------------------------------------------------------------

def read_all():
    rows = []
    if CSV_PATH.exists():
        with CSV_PATH.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                # normaliza para exibir SEMPRE bonito
                r["data_servico"] = fmt_date(r.get("data_servico",""))
                r["hora_servico"] = fmt_time(r.get("hora_servico",""))
                # se proxima_data estiver vazia, tenta calcular
                if not r.get("proxima_data"):
                    try:
                        base_dt = datetime.strptime(
                            f'{r["data_servico"]} {r["hora_servico"]}',
                            f"{DATE_FMT} {TIME_FMT}"
                        )
                        meses_map = {"1 mês":1, "2 meses":2, "3 meses":3}
                        nxt = add_months(base_dt, meses_map.get(r.get("prazo","1 mês"),1))
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
            # garante que salvamos formatado
            r = r.copy()
            r["data_servico"] = fmt_date(r.get("data_servico",""))
            r["hora_servico"] = fmt_time(r.get("hora_servico",""))
            writer.writerow(r)

app = Flask(__name__)

def check_access(loja, pin):
    if pin == ADMIN_PIN and ADMIN_PIN:
        return "admin", None
    if loja and pin and LOJA_PINS.get(loja) == pin:
        return "loja", loja
    if loja:
        return "denied", None
    return "open", None

@app.route("/", methods=["GET","POST"])
def index():
    loja_lock = request.args.get("loja","").strip()
    pin = request.args.get("pin","").strip()
    mode, loja_autorizada = check_access(loja_lock, pin)
    if mode == "denied":
        return abort(403, description="Acesso negado")

    if request.method == "POST":
        loja = loja_autorizada or request.form.get("loja","").strip()
        empresa = request.form.get("empresa","").strip()
        funcionario = request.form.get("funcionario","").strip()
        data_servico = fmt_date(request.form.get("data_servico",""))
        hora_servico = fmt_time(request.form.get("hora_servico",""))
        prazo = request.form.get("prazo","1 mês").strip()

        # calcula próxima data
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
        return redirect(url_for("index", loja=loja_lock, pin=pin))

    rows = read_all()
    # se estiver em modo "loja", filtra visualização
    loja_aut = loja_autorizada
    if loja_aut:
        rows = [r for r in rows if r.get("loja","") == loja_aut]

    return render_template("index.html", title=APP_TITLE, lojas=LOJAS,
                           rows=rows, count=len(rows),
                           loja_lock=loja_autorizada, mode=mode, pin=pin)

@app.route("/download/csv")
def download_csv():
    if not CSV_PATH.exists():
        write_all([])
    return send_file(str(CSV_PATH), as_attachment=True, download_name="servicos_web.csv")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
