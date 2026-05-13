import os
import io
import re
import base64
import requests
import threading
import datetime
import asyncio
import smtplib
import psycopg2
import psycopg2.extras
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))

SYSTEM_PROMPT = """Eres FinBot, asistente financiero personal inteligente.

REGLAS CRITICAS DE FORMATO:
Cuando detectes una accion, DEBES incluir el token EXACTO en tu respuesta.
El token va al INICIO de tu mensaje, SIN markdown, SIN negritas, SIN asteriscos.
Despues del token puedes agregar texto explicativo normal.

TOKENS (formato exacto, sin asteriscos ni negritas):
- "gaste X en Y" -> primera linea: GASTO_REGISTRADO|monto|categoria|descripcion
- "gane X" -> primera linea: INGRESO_REGISTRADO|monto|descripcion
- "presupuesto X Y" -> primera linea: PRESUPUESTO_DEFINIDO|categoria|monto
- "mi correo es X" -> primera linea: EMAIL_CONFIGURADO|correo

EJEMPLO CORRECTO de respuesta para "gaste 5000 en jabon":
GASTO_REGISTRADO|5000|Compras|jabon

¡Buen registro! Gasto pequeño y necesario. 💪

EJEMPLO INCORRECTO (NO HAGAS ESTO):
✅ **GASTO REGISTRADO**
GASTO_REGISTRADO|5000|Compras|jabon

Para todo lo demas responde en espanol, amable, conciso, sin juicios.
Usa $ y formato claro para numeros."""

# ─────────────────────────────────────────
# BASE DE DATOS
# ─────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS gastos (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, monto NUMERIC NOT NULL, categoria TEXT NOT NULL, descripcion TEXT, fecha DATE NOT NULL);")
            cur.execute("CREATE TABLE IF NOT EXISTS ingresos (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, monto NUMERIC NOT NULL, descripcion TEXT, fecha DATE NOT NULL);")
            cur.execute("CREATE TABLE IF NOT EXISTS metas (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, nombre TEXT NOT NULL, objetivo NUMERIC NOT NULL);")
            cur.execute("CREATE TABLE IF NOT EXISTS historial (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW());")
            cur.execute("CREATE TABLE IF NOT EXISTS presupuestos (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, categoria TEXT NOT NULL, limite NUMERIC NOT NULL, UNIQUE(user_id, categoria));")
            cur.execute("CREATE TABLE IF NOT EXISTS usuarios (user_id BIGINT PRIMARY KEY, timezone TEXT DEFAULT 'America/Bogota', email TEXT, last_backup DATE, created_at TIMESTAMP DEFAULT NOW());")
            try:
                cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS email TEXT;")
                cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS last_backup DATE;")
            except: pass
        conn.commit()
    print("Base de datos inicializada.")

def registrar_usuario(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO usuarios (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
        conn.commit()

def get_todos_usuarios():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM usuarios")
            return [r[0] for r in cur.fetchall()]

def guardar_email_usuario(user_id, email):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET email=%s WHERE user_id=%s", (email, user_id))
        conn.commit()

def get_email_usuario(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM usuarios WHERE user_id=%s", (user_id,))
            r = cur.fetchone()
            return r[0] if r else None

def registrar_gasto(user_id, monto, categoria, descripcion):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO gastos (user_id, monto, categoria, descripcion, fecha) VALUES (%s, %s, %s, %s, %s)",
                (user_id, monto, categoria.strip(), descripcion.strip(), datetime.date.today()))
        conn.commit()

def registrar_ingreso(user_id, monto, descripcion):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO ingresos (user_id, monto, descripcion, fecha) VALUES (%s, %s, %s, %s)",
                (user_id, monto, descripcion.strip(), datetime.date.today()))
        conn.commit()

def get_resumen(user_id):
    mes = datetime.date.today().replace(day=1)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT COALESCE(SUM(monto),0) FROM gastos WHERE user_id=%s AND fecha >= %s", (user_id, mes))
            tg = float(cur.fetchone()[0])
            cur.execute("SELECT COALESCE(SUM(monto),0) FROM ingresos WHERE user_id=%s AND fecha >= %s", (user_id, mes))
            ti = float(cur.fetchone()[0])
            cur.execute("SELECT categoria, SUM(monto) as total FROM gastos WHERE user_id=%s AND fecha >= %s GROUP BY categoria ORDER BY total DESC", (user_id, mes))
            por_cat = {r["categoria"]: float(r["total"]) for r in cur.fetchall()}
    return tg, ti, ti - tg, por_cat

def get_resumen_dia(user_id, fecha):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT COALESCE(SUM(monto),0) FROM gastos WHERE user_id=%s AND fecha=%s", (user_id, fecha))
            tg = float(cur.fetchone()[0])
            cur.execute("SELECT COALESCE(SUM(monto),0) FROM ingresos WHERE user_id=%s AND fecha=%s", (user_id, fecha))
            ti = float(cur.fetchone()[0])
            cur.execute("SELECT categoria, SUM(monto) as total FROM gastos WHERE user_id=%s AND fecha=%s GROUP BY categoria ORDER BY total DESC", (user_id, fecha))
            por_cat = {r["categoria"]: float(r["total"]) for r in cur.fetchall()}
    return tg, ti, por_cat

def get_resumen_semana(user_id):
    hoy = datetime.date.today()
    inicio = hoy - datetime.timedelta(days=6)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT COALESCE(SUM(monto),0) FROM gastos WHERE user_id=%s AND fecha >= %s AND fecha <= %s", (user_id, inicio, hoy))
            tg = float(cur.fetchone()[0])
            cur.execute("SELECT COALESCE(SUM(monto),0) FROM ingresos WHERE user_id=%s AND fecha >= %s AND fecha <= %s", (user_id, inicio, hoy))
            ti = float(cur.fetchone()[0])
            cur.execute("SELECT categoria, SUM(monto) as total FROM gastos WHERE user_id=%s AND fecha >= %s AND fecha <= %s GROUP BY categoria ORDER BY total DESC", (user_id, inicio, hoy))
            por_cat = {r["categoria"]: float(r["total"]) for r in cur.fetchall()}
    return tg, ti, ti - tg, por_cat

def tuvo_actividad_hoy(user_id):
    hoy = datetime.date.today()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM gastos WHERE user_id=%s AND fecha=%s", (user_id, hoy))
            g = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM ingresos WHERE user_id=%s AND fecha=%s", (user_id, hoy))
            i = cur.fetchone()[0]
    return (g + i) > 0

def get_metas(user_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT nombre, objetivo FROM metas WHERE user_id=%s", (user_id,))
            return cur.fetchall()

def get_presupuestos(user_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT categoria, limite FROM presupuestos WHERE user_id=%s", (user_id,))
            return {r["categoria"]: float(r["limite"]) for r in cur.fetchall()}

def guardar_presupuesto(user_id, categoria, limite):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO presupuestos (user_id, categoria, limite) VALUES (%s, %s, %s) ON CONFLICT (user_id, categoria) DO UPDATE SET limite = EXCLUDED.limite",
                (user_id, categoria.strip(), limite))
        conn.commit()

def get_historial(user_id, limit=20):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT role, content FROM historial WHERE user_id=%s ORDER BY created_at DESC LIMIT %s", (user_id, limit))
            return [{"role": r["role"], "content": r["content"]} for r in reversed(cur.fetchall())]

def guardar_historial(user_id, role, content):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO historial (user_id, role, content) VALUES (%s, %s, %s)", (user_id, role, content))
            cur.execute("DELETE FROM historial WHERE id IN (SELECT id FROM historial WHERE user_id=%s ORDER BY created_at DESC OFFSET 30)", (user_id,))
        conn.commit()

# ─────────────────────────────────────────
# NUEVAS FUNCIONES DE ANALISIS
# ─────────────────────────────────────────

def get_resumen_mes(user_id, year, month):
    inicio = datetime.date(year, month, 1)
    if month == 12:
        fin = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        fin = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT COALESCE(SUM(monto),0) FROM gastos WHERE user_id=%s AND fecha >= %s AND fecha <= %s", (user_id, inicio, fin))
            tg = float(cur.fetchone()[0])
            cur.execute("SELECT COALESCE(SUM(monto),0) FROM ingresos WHERE user_id=%s AND fecha >= %s AND fecha <= %s", (user_id, inicio, fin))
            ti = float(cur.fetchone()[0])
            cur.execute("SELECT categoria, SUM(monto) as total FROM gastos WHERE user_id=%s AND fecha >= %s AND fecha <= %s GROUP BY categoria", (user_id, inicio, fin))
            por_cat = {r["categoria"]: float(r["total"]) for r in cur.fetchall()}
    return tg, ti, por_cat

def comparar_meses(user_id):
    hoy = datetime.date.today()
    if hoy.month == 1:
        mes_ant, year_ant = 12, hoy.year - 1
    else:
        mes_ant, year_ant = hoy.month - 1, hoy.year

    ga, ia, ca = get_resumen_mes(user_id, hoy.year, hoy.month)
    gb, ib, cb = get_resumen_mes(user_id, year_ant, mes_ant)

    meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    texto = f"📊 *Comparativa*\n━━━━━━━━━━━━━━━\n📅 {meses[hoy.month-1]} vs {meses[mes_ant-1]}\n\n"

    if gb > 0:
        diff = ((ga - gb) / gb) * 100
        emoji = "📈" if diff > 0 else "📉"
        texto += f"💸 Gastos: ${ga:,.0f} vs ${gb:,.0f}\n   {emoji} {abs(diff):.1f}% {'mas' if diff > 0 else 'menos'}\n\n"
    else:
        texto += f"💸 Gastos: ${ga:,.0f}\n   (sin datos del mes anterior)\n\n"

    if ib > 0:
        diff_i = ((ia - ib) / ib) * 100
        emoji_i = "📈" if diff_i > 0 else "📉"
        texto += f"💰 Ingresos: ${ia:,.0f} vs ${ib:,.0f}\n   {emoji_i} {abs(diff_i):.1f}% {'mas' if diff_i > 0 else 'menos'}\n\n"
    else:
        texto += f"💰 Ingresos: ${ia:,.0f}\n   (sin datos del mes anterior)\n\n"

    if cb:
        texto += "📂 *Cambios por categoria:*\n"
        for cat in set(list(ca.keys()) + list(cb.keys())):
            act, ant = ca.get(cat, 0), cb.get(cat, 0)
            if ant > 0:
                d = ((act - ant) / ant) * 100
                e = "📈" if d > 0 else "📉"
                texto += f"  • {cat}: {e} {abs(d):.0f}% (${act:,.0f})\n"
            elif act > 0:
                texto += f"  • {cat}: 🆕 (${act:,.0f})\n"

    return texto

def predecir_gastos_mes(user_id):
    hoy = datetime.date.today()
    inicio = hoy.replace(day=1)
    dias_tx = (hoy - inicio).days + 1
    if hoy.month == 12:
        prox = datetime.date(hoy.year + 1, 1, 1)
    else:
        prox = datetime.date(hoy.year, hoy.month + 1, 1)
    dias_tot = (prox - inicio).days

    tg, ti, balance, por_cat = get_resumen(user_id)
    if dias_tx == 0 or tg == 0:
        return None

    prom_diario = tg / dias_tx
    proy = prom_diario * dias_tot
    dias_rest = dias_tot - dias_tx

    texto = f"🔮 *Prediccion del mes*\n━━━━━━━━━━━━━━━\n\n"
    texto += f"📅 Llevas {dias_tx} de {dias_tot} dias\n\n"
    texto += f"💸 Gastado: ${tg:,.0f}\n"
    texto += f"📊 Promedio diario: ${prom_diario:,.0f}\n\n"
    texto += f"🔮 *Proyeccion fin de mes:* ${proy:,.0f}\n"
    texto += f"⏳ Dias restantes: {dias_rest}\n"
    texto += f"💰 Gastaras aprox: ${prom_diario * dias_rest:,.0f} mas"
    return texto

def dia_mas_costoso(user_id):
    hoy = datetime.date.today()
    hace_30 = hoy - datetime.timedelta(days=30)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT EXTRACT(DOW FROM fecha) as dow, SUM(monto) as total, COUNT(*) as cnt FROM gastos WHERE user_id=%s AND fecha >= %s GROUP BY dow ORDER BY total DESC", (user_id, hace_30))
            rows = cur.fetchall()
    if not rows:
        return None

    dias = ["Domingo", "Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado"]
    texto = f"📅 *Tus dias de mayor gasto* (30 dias)\n━━━━━━━━━━━━━━━\n\n"
    for r in rows[:7]:
        d, t, c = int(r[0]), float(r[1]), int(r[2])
        prom = t / c if c > 0 else 0
        texto += f"📌 {dias[d]}: ${t:,.0f}\n   {c} gastos, prom ${prom:,.0f}\n\n"
    texto += f"💡 *Insight:* Los {dias[int(rows[0][0])]}s son tus dias mas caros."
    return texto

def get_categorias_frecuentes(user_id, limit=5):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT categoria, COUNT(*) as cnt, SUM(monto) as total FROM gastos WHERE user_id=%s GROUP BY categoria ORDER BY cnt DESC LIMIT %s", (user_id, limit))
            return [(r[0], int(r[1]), float(r[2])) for r in cur.fetchall()]

# ─────────────────────────────────────────
# FOTOS DE RECIBOS CON CLAUDE VISION
# ─────────────────────────────────────────

async def analizar_recibo(photo_bytes):
    img_b64 = base64.b64encode(photo_bytes).decode('utf-8')
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 500,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                        {"type": "text", "text": "Analiza este recibo. Extrae el TOTAL pagado y descripcion. Responde SOLO: MONTO|DESCRIPCION|CATEGORIA. Ejemplo: 25000|Almuerzo|Comida. Si no se lee responde: ERROR|no_legible"}
                    ]
                }]
            },
            timeout=60
        )
        return resp.json()["content"][0]["text"]
    except Exception as e:
        print(f"Error recibo: {e}")
        return None

# ─────────────────────────────────────────
# BACKUP POR EMAIL
# ─────────────────────────────────────────

def enviar_email_backup(destinatario, archivo_bytes, mes_nombre, user_id):
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        return False, "Email del servidor no configurado"
    try:
        msg = MIMEMultipart()
        msg["From"] = EMAIL_SENDER
        msg["To"] = destinatario
        msg["Subject"] = f"FinBot - Backup mensual {mes_nombre}"
        msg.attach(MIMEText(f"Hola!\n\nAqui esta tu backup de FinBot de {mes_nombre}.\n\nSaludos,\nFinBot 🤖", "plain"))

        archivo_bytes.seek(0)
        adj = MIMEBase("application", "octet-stream")
        adj.set_payload(archivo_bytes.read())
        encoders.encode_base64(adj)
        adj.add_header("Content-Disposition", f"attachment; filename=FinBot_{mes_nombre.replace(' ', '_')}.xlsx")
        msg.attach(adj)

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE usuarios SET last_backup=%s WHERE user_id=%s", (datetime.date.today(), user_id))
            conn.commit()
        return True, "Enviado"
    except Exception as e:
        print(f"Error email: {e}")
        return False, str(e)

# ─────────────────────────────────────────
# ALERTAS DE PRESUPUESTO
# ─────────────────────────────────────────

def verificar_alerta_presupuesto(user_id, categoria):
    pres = get_presupuestos(user_id)
    if categoria not in pres:
        return None
    limite = pres[categoria]
    mes = datetime.date.today().replace(day=1)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(monto),0) FROM gastos WHERE user_id=%s AND categoria=%s AND fecha >= %s", (user_id, categoria, mes))
            gastado = float(cur.fetchone()[0])
    pct = (gastado / limite) * 100 if limite > 0 else 0
    if pct >= 100:
        return f"🚨 Presupuesto SUPERADO en {categoria}!\nLimite: ${limite:,.0f}\nGastado: ${gastado:,.0f} ({pct:.0f}%)"
    elif pct >= 80:
        return f"⚠️ Llevas el {pct:.0f}% del presupuesto en {categoria}\nLimite: ${limite:,.0f} | Gastado: ${gastado:,.0f}\nTe quedan ${limite - gastado:,.0f}"
    return None

# ─────────────────────────────────────────
# EXPORTAR A EXCEL
# ─────────────────────────────────────────

def generar_excel(user_id):
    mes = datetime.date.today().replace(day=1)
    mes_nombre = datetime.datetime.now().strftime("%B %Y")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT fecha, categoria, descripcion, monto FROM gastos WHERE user_id=%s AND fecha >= %s ORDER BY fecha", (user_id, mes))
            gastos = cur.fetchall()
            cur.execute("SELECT fecha, descripcion, monto FROM ingresos WHERE user_id=%s AND fecha >= %s ORDER BY fecha", (user_id, mes))
            ingresos = cur.fetchall()

    wb = openpyxl.Workbook()
    hf = PatternFill("solid", fgColor="1a1a2e")
    hfont = Font(bold=True, color="FFFFFF")
    alt = PatternFill("solid", fgColor="F2F2F2")

    ws = wb.active
    ws.title = "Gastos"
    for col, h in enumerate(["Fecha", "Categoria", "Descripcion", "Monto"], 1):
        c = ws.cell(row=1, column=col, value=h)
        c.font = hfont; c.fill = hf; c.alignment = Alignment(horizontal="center")
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 28
    ws.column_dimensions["D"].width = 16
    tg = 0
    for i, g in enumerate(gastos, 2):
        ws.cell(row=i, column=1, value=str(g["fecha"]))
        ws.cell(row=i, column=2, value=g["categoria"])
        ws.cell(row=i, column=3, value=g["descripcion"])
        c = ws.cell(row=i, column=4, value=float(g["monto"]))
        c.number_format = '$#,##0'
        tg += float(g["monto"])
        if i % 2 == 0:
            for col in range(1, 5):
                ws.cell(row=i, column=col).fill = alt
    ft = len(gastos) + 2
    ws.cell(row=ft, column=3, value="TOTAL").font = Font(bold=True)
    t = ws.cell(row=ft, column=4, value=tg)
    t.font = Font(bold=True, color="E94560")
    t.number_format = '$#,##0'

    ws2 = wb.create_sheet("Ingresos")
    for col, h in enumerate(["Fecha", "Descripcion", "Monto"], 1):
        c = ws2.cell(row=1, column=col, value=h)
        c.font = hfont; c.fill = hf; c.alignment = Alignment(horizontal="center")
    ws2.column_dimensions["A"].width = 14
    ws2.column_dimensions["B"].width = 28
    ws2.column_dimensions["C"].width = 16
    ti = 0
    for i, ing in enumerate(ingresos, 2):
        ws2.cell(row=i, column=1, value=str(ing["fecha"]))
        ws2.cell(row=i, column=2, value=ing["descripcion"])
        c = ws2.cell(row=i, column=3, value=float(ing["monto"]))
        c.number_format = '$#,##0'
        ti += float(ing["monto"])
        if i % 2 == 0:
            for col in range(1, 4):
                ws2.cell(row=i, column=col).fill = alt
    fti = len(ingresos) + 2
    ws2.cell(row=fti, column=2, value="TOTAL").font = Font(bold=True)
    t = ws2.cell(row=fti, column=3, value=ti)
    t.font = Font(bold=True, color="06D6A0")
    t.number_format = '$#,##0'

    ws3 = wb.create_sheet("Resumen")
    ws3.column_dimensions["A"].width = 22
    ws3.column_dimensions["B"].width = 18
    ws3["A1"] = f"Resumen - {mes_nombre}"
    ws3["A1"].font = Font(bold=True, size=14)
    bal = ti - tg
    for row, (lbl, val) in enumerate([("Total Ingresos", ti), ("Total Gastos", tg), ("Balance", bal)], 3):
        ws3.cell(row=row, column=1, value=lbl).font = Font(bold=True)
        v = ws3.cell(row=row, column=2, value=val)
        v.number_format = '$#,##0'
        if lbl == "Balance":
            v.font = Font(bold=True, color="06D6A0" if bal >= 0 else "E94560")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf, mes_nombre

# ─────────────────────────────────────────
# GRAFICA
# ─────────────────────────────────────────

def generar_grafica(user_id):
    tg, ti, bal, por_cat = get_resumen(user_id)
    mes = datetime.datetime.now().strftime("%B %Y")
    fig = plt.figure(figsize=(10, 12), facecolor='#1a1a2e')
    fig.suptitle(f'Resumen Financiero - {mes}', fontsize=16, fontweight='bold', color='white', y=0.98)

    if not por_cat and ti == 0:
        ax = fig.add_subplot(111)
        ax.set_facecolor('#1a1a2e')
        ax.text(0.5, 0.5, 'Sin datos aun', ha='center', va='center', fontsize=14, color='white', transform=ax.transAxes)
        ax.axis('off')
    else:
        gs = fig.add_gridspec(2, 2, hspace=0.4, wspace=0.3)
        ax1 = fig.add_subplot(gs[0, :])
        ax1.set_facecolor('#16213e')
        if por_cat:
            colores = ['#e94560','#0f3460','#533483','#06d6a0','#ffd166','#ef476f','#118ab2','#073b4c','#26547c','#ffb703']
            _, _, autotexts = ax1.pie(por_cat.values(), labels=por_cat.keys(), colors=colores[:len(por_cat)], autopct='%1.1f%%', startangle=90, textprops={'color': 'white', 'fontsize': 9})
            for at in autotexts:
                at.set_color('white'); at.set_fontsize(8)
            ax1.set_title('Gastos por Categoria', color='white', fontsize=12, pad=10)

        ax2 = fig.add_subplot(gs[1, 0])
        ax2.set_facecolor('#16213e')
        vals = [ti, tg]
        bars = ax2.bar(['Ingresos', 'Gastos'], vals, color=['#06d6a0', '#e94560'], width=0.5)
        for bar, val in zip(bars, vals):
            ax2.text(bar.get_x() + bar.get_width()/2., bar.get_height() + max(vals)*0.02, f'${val:,.0f}', ha='center', va='bottom', color='white', fontsize=9, fontweight='bold')
        ax2.tick_params(colors='white')
        for s in ['top','right']: ax2.spines[s].set_visible(False)
        for s in ['bottom','left']: ax2.spines[s].set_color('#444')
        ax2.set_title('Ingresos vs Gastos', color='white', fontsize=11, pad=10)

        ax3 = fig.add_subplot(gs[1, 1])
        ax3.set_facecolor('#16213e')
        cbc = '#06d6a0' if bal >= 0 else '#e94560'
        ax3.text(0.5, 0.6, 'Balance', ha='center', va='center', fontsize=13, color='white', fontweight='bold', transform=ax3.transAxes)
        ax3.text(0.5, 0.35, f'${abs(bal):,.0f}', ha='center', va='center', fontsize=18, color=cbc, fontweight='bold', transform=ax3.transAxes)
        ax3.axis('off')

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=120, bbox_inches='tight', facecolor='#1a1a2e')
    buf.seek(0)
    plt.close()
    return buf

# ─────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────

async def enviar_resumen_diario(bot: Bot):
    hoy = datetime.date.today()
    dia_sem = hoy.weekday()
    usuarios = get_todos_usuarios()
    for user_id in usuarios:
        try:
            tg, ti, por_cat = get_resumen_dia(user_id, hoy)
            if dia_sem == 6:
                tgs, tis, bal, pcs = get_resumen_semana(user_id)
                texto = "📅 Resumen semanal\n━━━━━━━━━━━━━━━\n"
                texto += f"💰 Ingresos: ${tis:,.0f}\n"
                texto += f"💸 Gastos:   ${tgs:,.0f}\n"
                texto += f"{'📈' if bal >= 0 else '📉'} Balance:  ${bal:,.0f}\n"
                if pcs:
                    texto += "\n📂 Por categoria:\n"
                    for c, m in sorted(pcs.items(), key=lambda x: x[1], reverse=True):
                        texto += f"  • {c}: ${m:,.0f}\n"
                await bot.send_message(chat_id=user_id, text=texto)

            actividad = tuvo_actividad_hoy(user_id)
            if not actividad:
                await bot.send_message(chat_id=user_id, text="🔔 Recordatorio de FinBot\n\nNo registraste ningun movimiento hoy.\n¿Gastaste o ganaste algo? 📊")
            else:
                texto = f"📊 Resumen de hoy ({hoy.strftime('%d/%m/%Y')})\n━━━━━━━━━━━━━━━\n"
                if ti > 0: texto += f"💰 Ingresos: ${ti:,.0f}\n"
                if tg > 0:
                    texto += f"💸 Gastos: ${tg:,.0f}\n"
                    for c, m in sorted(por_cat.items(), key=lambda x: x[1], reverse=True):
                        texto += f"  • {c}: ${m:,.0f}\n"
                bd = ti - tg
                texto += f"\n{'📈' if bd >= 0 else '📉'} Balance: ${bd:,.0f}"
                await bot.send_message(chat_id=user_id, text=texto)
        except Exception as e:
            print(f"Error resumen {user_id}: {e}")

async def enviar_backup_mensual(bot: Bot):
    hoy = datetime.date.today()
    if hoy.day != 1:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, email FROM usuarios WHERE email IS NOT NULL AND email != ''")
            usuarios = cur.fetchall()
    for user_id, email in usuarios:
        try:
            buf, mes_nombre = generar_excel(user_id)
            ok, msg = enviar_email_backup(email, buf, mes_nombre, user_id)
            if ok:
                await bot.send_message(chat_id=user_id, text=f"📧 Backup mensual enviado a {email}")
        except Exception as e:
            print(f"Error backup {user_id}: {e}")

async def scheduler(bot: Bot):
    enviado = None
    backup_mes = None
    while True:
        try:
            ahora = datetime.datetime.utcnow() - datetime.timedelta(hours=5)
            hoy = ahora.date()
            if ahora.hour == 20 and ahora.minute < 10:
                if enviado != hoy:
                    enviado = hoy
                    print(f"Enviando resumenes - {hoy}")
                    await enviar_resumen_diario(bot)
            if hoy.day == 1 and ahora.hour == 9 and ahora.minute < 10:
                if backup_mes != hoy.month:
                    backup_mes = hoy.month
                    print(f"Enviando backups - {hoy}")
                    await enviar_backup_mensual(bot)
        except Exception as e:
            print(f"Error scheduler: {e}")
        await asyncio.sleep(60)

# ─────────────────────────────────────────
# SERVIDOR WEB
# ─────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# ─────────────────────────────────────────
# HANDLERS DE TELEGRAM
# ─────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    registrar_usuario(user_id)
    kb = [
        [InlineKeyboardButton("💸 Gasto", callback_data="menu_gasto"),
         InlineKeyboardButton("💰 Ingreso", callback_data="menu_ingreso")],
        [InlineKeyboardButton("📊 Resumen", callback_data="menu_resumen"),
         InlineKeyboardButton("📈 Grafica", callback_data="menu_grafica")],
        [InlineKeyboardButton("🔮 Prediccion", callback_data="menu_prediccion"),
         InlineKeyboardButton("📅 Comparar meses", callback_data="menu_comparar")],
        [InlineKeyboardButton("📆 Dias caros", callback_data="menu_dias"),
         InlineKeyboardButton("💡 Consejo", callback_data="menu_consejo")],
        [InlineKeyboardButton("🎯 Metas", callback_data="menu_metas"),
         InlineKeyboardButton("🔔 Presupuestos", callback_data="menu_presupuestos")],
        [InlineKeyboardButton("📥 Excel", callback_data="menu_excel"),
         InlineKeyboardButton("📧 Mi correo", callback_data="menu_email")],
    ]
    await update.message.reply_text(
        "Hola! Soy FinBot 🤖\n\n"
        "✨ *Nuevas funciones:*\n"
        "• 📸 Foto de recibo = registro automatico\n"
        "• 🔮 Prediccion de gastos\n"
        "• 📅 Comparativa entre meses\n"
        "• 📆 Dias mas caros\n"
        "• 📧 Backup mensual por email\n\n"
        "Escribe naturalmente o usa los botones:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "menu_gasto":
        cats = get_categorias_frecuentes(user_id, 5)
        texto = "Escribe: \"Gaste 50000 en comida\"\n\n📸 *Tip:* envia foto del recibo!"
        if cats:
            texto += "\n\n🏷️ *Tus categorias frecuentes:*\n"
            for c, n, t in cats:
                texto += f"  • {c} ({n}x)\n"
        await query.message.reply_text(texto, parse_mode="Markdown")
    elif query.data == "menu_ingreso":
        await query.message.reply_text("Escribe: \"Gane 2000000\"")
    elif query.data == "menu_resumen":
        await mostrar_resumen(query.message, user_id)
    elif query.data == "menu_grafica":
        await query.message.reply_text("Generando...")
        buf = generar_grafica(user_id)
        await query.message.reply_photo(photo=buf, caption="Tu resumen del mes")
    elif query.data == "menu_prediccion":
        p = predecir_gastos_mes(user_id)
        await query.message.reply_text(p if p else "No tengo suficientes datos aun.", parse_mode="Markdown" if p else None)
    elif query.data == "menu_comparar":
        await query.message.reply_text(comparar_meses(user_id), parse_mode="Markdown")
    elif query.data == "menu_dias":
        d = dia_mas_costoso(user_id)
        await query.message.reply_text(d if d else "No tengo suficientes datos.", parse_mode="Markdown" if d else None)
    elif query.data == "menu_metas":
        metas = get_metas(user_id)
        if not metas:
            await query.message.reply_text("Sin metas. Escribe: \"Ahorrar 500000 para vacaciones\"")
        else:
            texto = "Metas:\n\n"
            for m in metas:
                texto += f"🎯 {m['nombre']}: ${float(m['objetivo']):,.0f}\n"
            await query.message.reply_text(texto)
    elif query.data == "menu_consejo":
        tg, ti, bal, pc = get_resumen(user_id)
        msg = await query.message.reply_text("Analizando...")
        c = await pedir_consejo_ia(user_id, tg, ti, bal, pc)
        await msg.edit_text(c)
    elif query.data == "menu_presupuestos":
        pres = get_presupuestos(user_id)
        _, _, _, pc = get_resumen(user_id)
        if not pres:
            await query.message.reply_text("Sin presupuestos. Escribe: \"Presupuesto comida 300000\"")
        else:
            texto = "🔔 Presupuestos:\n\n"
            for c, l in pres.items():
                g = pc.get(c, 0)
                p = (g / l * 100) if l > 0 else 0
                e = "🚨" if p >= 100 else "⚠️" if p >= 80 else "✅"
                texto += f"{e} {c}\n   ${l:,.0f} | gastado ${g:,.0f} ({p:.0f}%)\n\n"
            await query.message.reply_text(texto)
    elif query.data == "menu_excel":
        await query.message.reply_text("Generando Excel...")
        buf, mn = generar_excel(user_id)
        await query.message.reply_document(document=buf, filename=f"FinBot_{mn.replace(' ', '_')}.xlsx", caption=f"📊 {mn}")
    elif query.data == "menu_email":
        email = get_email_usuario(user_id)
        if email:
            await query.message.reply_text(f"📧 Correo: *{email}*\n\nBackup automatico cada dia 1.\n\nPara cambiarlo: \"mi correo es nuevo@email.com\"", parse_mode="Markdown")
        else:
            await query.message.reply_text("Sin correo configurado.\n\nPara backup mensual escribe:\n\"mi correo es tucorreo@gmail.com\"")

async def mostrar_resumen(message, user_id):
    tg, ti, bal, pc = get_resumen(user_id)
    mes = datetime.datetime.now().strftime("%B %Y")
    texto = f"📊 Resumen {mes}\n━━━━━━━━━━━━━━━\n"
    texto += f"💰 Ingresos: ${ti:,.0f}\n💸 Gastos: ${tg:,.0f}\n"
    texto += f"{'📈' if bal >= 0 else '📉'} Balance: ${bal:,.0f}\n"
    if pc:
        texto += "\n📂 Por categoria:\n"
        for c, m in sorted(pc.items(), key=lambda x: x[1], reverse=True):
            texto += f"  • {c}: ${m:,.0f}\n"
    if not pc and ti == 0:
        texto += "\nAun no tienes registros."
    kb = [[InlineKeyboardButton("📈 Grafica", callback_data="menu_grafica"), InlineKeyboardButton("📥 Excel", callback_data="menu_excel")]]
    await message.reply_text(texto, reply_markup=InlineKeyboardMarkup(kb))

async def pedir_consejo_ia(user_id, tg, ti, bal, pc):
    ctx = f"Datos: ingresos={ti}, gastos={tg}, balance={bal}, por_cat={pc}. Da un consejo corto (max 3 oraciones)."
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300, "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": ctx}]},
            timeout=30
        )
        return resp.json()["content"][0]["text"]
    except:
        return "Registra tus gastos regularmente para recibir consejos."

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    registrar_usuario(user_id)
    msg = await update.message.reply_text("📸 Analizando recibo...")
    try:
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        resultado = await analizar_recibo(bytes(photo_bytes))
        if not resultado or resultado.startswith("ERROR"):
            await msg.edit_text("❌ No pude leer el recibo. Intenta otra foto o registralo manual.")
            return
        parts = resultado.strip().split("|")
        if len(parts) >= 3:
            monto = float(parts[0].replace(",", "").replace("$", "").strip())
            desc = parts[1].strip()
            cat = parts[2].strip()
            registrar_gasto(user_id, monto, cat, desc)
            await msg.edit_text(f"✅ Gasto desde foto\n💸 ${monto:,.0f}\n📂 {cat}\n📝 {desc}")
            alerta = verificar_alerta_presupuesto(user_id, cat)
            if alerta:
                await update.message.reply_text(alerta)
        else:
            await msg.edit_text(f"❌ Formato no reconocido: {resultado}")
    except Exception as e:
        print(f"Error photo: {e}")
        await msg.edit_text("❌ Error procesando la foto.")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎤 Notas de voz aun en beta.\n\nPor ahora escribe el gasto o envia foto del recibo 📸")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    registrar_usuario(user_id)

    if user_text.startswith("/gasto"):
        parts = user_text.split()
        if len(parts) >= 2:
            try:
                monto = float(parts[1].replace(",", ""))
                cat = " ".join(parts[2:]) if len(parts) > 2 else "General"
                registrar_gasto(user_id, monto, cat, cat)
                await update.message.reply_text(f"✅ Gasto: ${monto:,.0f} en {cat}")
                alerta = verificar_alerta_presupuesto(user_id, cat)
                if alerta:
                    await update.message.reply_text(alerta)
                return
            except: pass

    if user_text.startswith("/ingreso"):
        parts = user_text.split()
        if len(parts) >= 2:
            try:
                monto = float(parts[1].replace(",", ""))
                desc = " ".join(parts[2:]) if len(parts) > 2 else "Ingreso"
                registrar_ingreso(user_id, monto, desc)
                await update.message.reply_text(f"✅ Ingreso: ${monto:,.0f}")
                return
            except: pass

    if user_text.lower() in ["/resumen", "resumen", "balance"]:
        await mostrar_resumen(update.message, user_id)
        return
    if user_text.lower() in ["/grafica", "grafica"]:
        await update.message.reply_text("Generando...")
        buf = generar_grafica(user_id)
        await update.message.reply_photo(photo=buf, caption="Tu resumen")
        return
    if user_text.lower() in ["/excel", "excel"]:
        await update.message.reply_text("Generando Excel...")
        buf, mn = generar_excel(user_id)
        await update.message.reply_document(document=buf, filename=f"FinBot_{mn.replace(' ', '_')}.xlsx", caption=f"📊 {mn}")
        return
    if user_text.lower() in ["/prediccion", "prediccion"]:
        p = predecir_gastos_mes(user_id)
        await update.message.reply_text(p if p else "No tengo datos suficientes.", parse_mode="Markdown" if p else None)
        return
    if user_text.lower() in ["/comparar", "comparar"]:
        await update.message.reply_text(comparar_meses(user_id), parse_mode="Markdown")
        return

    historial = get_historial(user_id)
    historial.append({"role": "user", "content": user_text})
    guardar_historial(user_id, "user", user_text)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1000, "system": SYSTEM_PROMPT, "messages": historial},
            timeout=30
        )
        reply = resp.json()["content"][0]["text"]
    except Exception as e:
        print(f"Error API: {e}")
        await update.message.reply_text("Error, intenta de nuevo.")
        return

    # Buscar tokens DENTRO del texto (no solo al inicio) usando regex
    # Esto soluciona el caso cuando la IA agrega markdown o texto antes del token

    # GASTO_REGISTRADO|monto|categoria|descripcion
    match_gasto = re.search(r'GASTO_REGISTRADO\|([^|]+)\|([^|]+)\|([^\n]+)', reply)
    if match_gasto:
        try:
            m = float(match_gasto.group(1).replace(",", "").replace("$", "").strip())
            c = match_gasto.group(2).strip()
            d = match_gasto.group(3).strip()
            registrar_gasto(user_id, m, c, d)
            # Limpiar el token del reply visible al usuario
            reply_limpio = re.sub(r'GASTO_REGISTRADO\|[^\n]+\n?', '', reply).strip()
            if reply_limpio:
                await update.message.reply_text(f"✅ Gasto: ${m:,.0f}\n📂 {c}\n📝 {d}\n\n{reply_limpio}")
            else:
                await update.message.reply_text(f"✅ Gasto: ${m:,.0f}\n📂 {c}\n📝 {d}")
            alerta = verificar_alerta_presupuesto(user_id, c)
            if alerta:
                await update.message.reply_text(alerta)
            guardar_historial(user_id, "assistant", reply)
            return
        except Exception as e:
            print(f"Error parsing gasto: {e}")

    # INGRESO_REGISTRADO|monto|descripcion
    match_ingreso = re.search(r'INGRESO_REGISTRADO\|([^|]+)\|([^\n]+)', reply)
    if match_ingreso:
        try:
            m = float(match_ingreso.group(1).replace(",", "").replace("$", "").strip())
            d = match_ingreso.group(2).strip()
            registrar_ingreso(user_id, m, d)
            reply_limpio = re.sub(r'INGRESO_REGISTRADO\|[^\n]+\n?', '', reply).strip()
            if reply_limpio:
                await update.message.reply_text(f"✅ Ingreso: ${m:,.0f}\n📝 {d}\n\n{reply_limpio}")
            else:
                await update.message.reply_text(f"✅ Ingreso: ${m:,.0f}\n📝 {d}")
            guardar_historial(user_id, "assistant", reply)
            return
        except Exception as e:
            print(f"Error parsing ingreso: {e}")

    # PRESUPUESTO_DEFINIDO|categoria|monto
    match_pres = re.search(r'PRESUPUESTO_DEFINIDO\|([^|]+)\|([^\n]+)', reply)
    if match_pres:
        try:
            c = match_pres.group(1).strip()
            l = float(match_pres.group(2).replace(",", "").replace("$", "").strip())
            guardar_presupuesto(user_id, c, l)
            await update.message.reply_text(f"✅ Presupuesto: {c} ${l:,.0f}/mes")
            guardar_historial(user_id, "assistant", reply)
            return
        except Exception as e:
            print(f"Error parsing presupuesto: {e}")

    # EMAIL_CONFIGURADO|correo
    match_email = re.search(r'EMAIL_CONFIGURADO\|([^\s\n]+)', reply)
    if match_email:
        try:
            email = match_email.group(1).strip()
            if "@" in email and "." in email:
                guardar_email_usuario(user_id, email)
                await update.message.reply_text(f"✅ Correo: {email}\n\n📧 Backup automatico el dia 1 de cada mes.")
                guardar_historial(user_id, "assistant", reply)
                return
        except Exception as e:
            print(f"Error parsing email: {e}")

    guardar_historial(user_id, "assistant", reply)
    await update.message.reply_text(reply)

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

async def post_init(application):
    asyncio.create_task(scheduler(application.bot))

def main():
    init_db()
    threading.Thread(target=run_web_server, daemon=True).start()
    print("Web server activo")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("resumen", lambda u, c: mostrar_resumen(u.message, u.effective_user.id)))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("FinBot Pro iniciado!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
