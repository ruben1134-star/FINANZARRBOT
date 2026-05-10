import os
import io
import json
import requests
import threading
import datetime
import psycopg2
import psycopg2.extras
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

SYSTEM_PROMPT = """Eres FinBot, asistente financiero personal inteligente. Ayudas a:
1. Registrar gastos e ingresos con categoria, monto y fecha
2. Definir presupuestos mensuales por categoria
3. Crear y rastrear metas de ahorro
4. Dar consejos financieros personalizados
5. Analizar patrones de gasto

COMANDOS ESPECIALES que debes reconocer:
- Si el usuario dice "gaste X en Y" o "/gasto X Y" -> registra el gasto
- Si el usuario dice "gane X" o "/ingreso X" -> registra el ingreso  
- Si pide resumen/balance -> muestra resumen claro
- Si pide consejo -> da consejo personalizado basado en sus datos
- Si el usuario quiere definir presupuesto como "presupuesto comida 300000" o "mi limite en X es Y" -> responde PRESUPUESTO_DEFINIDO|categoria|monto

Cuando registres un gasto responde SIEMPRE con este formato exacto:
GASTO_REGISTRADO|monto|categoria|descripcion

Cuando registres un ingreso responde SIEMPRE con este formato exacto:
INGRESO_REGISTRADO|monto|descripcion

Cuando el usuario defina un presupuesto responde SIEMPRE con este formato exacto:
PRESUPUESTO_DEFINIDO|categoria|monto

Para todo lo demas responde normalmente en espanol, amable, sin juicios.
Usa $ y formato claro para numeros. Se conciso en Telegram."""

# ─────────────────────────────────────────
# BASE DE DATOS
# ─────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gastos (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    monto NUMERIC NOT NULL,
                    categoria TEXT NOT NULL,
                    descripcion TEXT,
                    fecha DATE NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ingresos (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    monto NUMERIC NOT NULL,
                    descripcion TEXT,
                    fecha DATE NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS metas (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    nombre TEXT NOT NULL,
                    objetivo NUMERIC NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS historial (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS presupuestos (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    categoria TEXT NOT NULL,
                    limite NUMERIC NOT NULL,
                    UNIQUE(user_id, categoria)
                );
            """)
        conn.commit()
    print("Base de datos inicializada correctamente.")

def registrar_gasto(user_id, monto, categoria, descripcion):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO gastos (user_id, monto, categoria, descripcion, fecha) VALUES (%s, %s, %s, %s, %s)",
                (user_id, monto, categoria.strip(), descripcion.strip(), datetime.date.today())
            )
        conn.commit()

def registrar_ingreso(user_id, monto, descripcion):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ingresos (user_id, monto, descripcion, fecha) VALUES (%s, %s, %s, %s)",
                (user_id, monto, descripcion.strip(), datetime.date.today())
            )
        conn.commit()

def get_resumen(user_id):
    mes = datetime.date.today().replace(day=1)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT COALESCE(SUM(monto),0) FROM gastos WHERE user_id=%s AND fecha >= %s",
                (user_id, mes)
            )
            total_gastos = float(cur.fetchone()[0])
            cur.execute(
                "SELECT COALESCE(SUM(monto),0) FROM ingresos WHERE user_id=%s AND fecha >= %s",
                (user_id, mes)
            )
            total_ingresos = float(cur.fetchone()[0])
            cur.execute(
                "SELECT categoria, SUM(monto) as total FROM gastos WHERE user_id=%s AND fecha >= %s GROUP BY categoria ORDER BY total DESC",
                (user_id, mes)
            )
            rows = cur.fetchall()
            por_categoria = {r["categoria"]: float(r["total"]) for r in rows}
    balance = total_ingresos - total_gastos
    return total_gastos, total_ingresos, balance, por_categoria

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
            cur.execute("""
                INSERT INTO presupuestos (user_id, categoria, limite)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, categoria) DO UPDATE SET limite = EXCLUDED.limite
            """, (user_id, categoria.strip(), limite))
        conn.commit()

def get_historial(user_id, limit=20):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT role, content FROM historial WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit)
            )
            rows = cur.fetchall()
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def guardar_historial(user_id, role, content):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO historial (user_id, role, content) VALUES (%s, %s, %s)",
                (user_id, role, content)
            )
            cur.execute("""
                DELETE FROM historial WHERE id IN (
                    SELECT id FROM historial WHERE user_id=%s
                    ORDER BY created_at DESC OFFSET 30
                )
            """, (user_id,))
        conn.commit()

# ─────────────────────────────────────────
# ALERTAS DE PRESUPUESTO
# ─────────────────────────────────────────

def verificar_alerta_presupuesto(user_id, categoria):
    presupuestos = get_presupuestos(user_id)
    if categoria not in presupuestos:
        return None
    limite = presupuestos[categoria]
    mes = datetime.date.today().replace(day=1)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(monto),0) FROM gastos WHERE user_id=%s AND categoria=%s AND fecha >= %s",
                (user_id, categoria, mes)
            )
            gastado = float(cur.fetchone()[0])
    porcentaje = (gastado / limite) * 100 if limite > 0 else 0
    if porcentaje >= 100:
        return (
            f"🚨 Presupuesto SUPERADO en {categoria}!\n"
            f"Limite: ${limite:,.0f}\n"
            f"Gastado: ${gastado:,.0f} ({porcentaje:.0f}%)"
        )
    elif porcentaje >= 80:
        return (
            f"⚠️ Llevas el {porcentaje:.0f}% del presupuesto en {categoria}\n"
            f"Limite: ${limite:,.0f} | Gastado: ${gastado:,.0f}\n"
            f"Te quedan ${limite - gastado:,.0f}"
        )
    return None

# ─────────────────────────────────────────
# EXPORTAR A EXCEL
# ─────────────────────────────────────────

def generar_excel(user_id):
    mes = datetime.date.today().replace(day=1)
    mes_nombre = datetime.datetime.now().strftime("%B %Y")

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT fecha, categoria, descripcion, monto FROM gastos WHERE user_id=%s AND fecha >= %s ORDER BY fecha",
                (user_id, mes)
            )
            gastos = cur.fetchall()
            cur.execute(
                "SELECT fecha, descripcion, monto FROM ingresos WHERE user_id=%s AND fecha >= %s ORDER BY fecha",
                (user_id, mes)
            )
            ingresos = cur.fetchall()

    wb = openpyxl.Workbook()
    header_fill = PatternFill("solid", fgColor="1a1a2e")
    header_font = Font(bold=True, color="FFFFFF")
    alt_fill = PatternFill("solid", fgColor="F2F2F2")

    # Hoja Gastos
    ws_gastos = wb.active
    ws_gastos.title = "Gastos"
    for col, h in enumerate(["Fecha", "Categoria", "Descripcion", "Monto"], 1):
        cell = ws_gastos.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    ws_gastos.column_dimensions["A"].width = 14
    ws_gastos.column_dimensions["B"].width = 18
    ws_gastos.column_dimensions["C"].width = 28
    ws_gastos.column_dimensions["D"].width = 16

    total_gastos = 0
    for i, g in enumerate(gastos, 2):
        ws_gastos.cell(row=i, column=1, value=str(g["fecha"]))
        ws_gastos.cell(row=i, column=2, value=g["categoria"])
        ws_gastos.cell(row=i, column=3, value=g["descripcion"])
        c = ws_gastos.cell(row=i, column=4, value=float(g["monto"]))
        c.number_format = '$#,##0'
        total_gastos += float(g["monto"])
        if i % 2 == 0:
            for col in range(1, 5):
                ws_gastos.cell(row=i, column=col).fill = alt_fill
    fila_t = len(gastos) + 2
    ws_gastos.cell(row=fila_t, column=3, value="TOTAL").font = Font(bold=True)
    t = ws_gastos.cell(row=fila_t, column=4, value=total_gastos)
    t.font = Font(bold=True, color="E94560")
    t.number_format = '$#,##0'

    # Hoja Ingresos
    ws_ingresos = wb.create_sheet("Ingresos")
    for col, h in enumerate(["Fecha", "Descripcion", "Monto"], 1):
        cell = ws_ingresos.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    ws_ingresos.column_dimensions["A"].width = 14
    ws_ingresos.column_dimensions["B"].width = 28
    ws_ingresos.column_dimensions["C"].width = 16

    total_ingresos = 0
    for i, ing in enumerate(ingresos, 2):
        ws_ingresos.cell(row=i, column=1, value=str(ing["fecha"]))
        ws_ingresos.cell(row=i, column=2, value=ing["descripcion"])
        c = ws_ingresos.cell(row=i, column=3, value=float(ing["monto"]))
        c.number_format = '$#,##0'
        total_ingresos += float(ing["monto"])
        if i % 2 == 0:
            for col in range(1, 4):
                ws_ingresos.cell(row=i, column=col).fill = alt_fill
    fila_ti = len(ingresos) + 2
    ws_ingresos.cell(row=fila_ti, column=2, value="TOTAL").font = Font(bold=True)
    t = ws_ingresos.cell(row=fila_ti, column=3, value=total_ingresos)
    t.font = Font(bold=True, color="06D6A0")
    t.number_format = '$#,##0'

    # Hoja Resumen
    ws_resumen = wb.create_sheet("Resumen")
    ws_resumen.column_dimensions["A"].width = 22
    ws_resumen.column_dimensions["B"].width = 18
    ws_resumen["A1"] = f"Resumen Financiero - {mes_nombre}"
    ws_resumen["A1"].font = Font(bold=True, size=14)
    balance = total_ingresos - total_gastos
    for row, (label, valor) in enumerate([("Total Ingresos", total_ingresos), ("Total Gastos", total_gastos), ("Balance", balance)], 3):
        ws_resumen.cell(row=row, column=1, value=label).font = Font(bold=True)
        v = ws_resumen.cell(row=row, column=2, value=valor)
        v.number_format = '$#,##0'
        if label == "Balance":
            v.font = Font(bold=True, color="06D6A0" if balance >= 0 else "E94560")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf, mes_nombre

# ─────────────────────────────────────────
# GRAFICA
# ─────────────────────────────────────────

def generar_grafica(user_id):
    total_gastos, total_ingresos, balance, por_categoria = get_resumen(user_id)
    mes = datetime.datetime.now().strftime("%B %Y")
    fig = plt.figure(figsize=(10, 12), facecolor='#1a1a2e')
    fig.suptitle(f'Resumen Financiero - {mes}', fontsize=16, fontweight='bold', color='white', y=0.98)

    if not por_categoria and total_ingresos == 0:
        ax = fig.add_subplot(111)
        ax.set_facecolor('#1a1a2e')
        ax.text(0.5, 0.5, 'Sin datos aun\nRegistra tus gastos\ne ingresos primero!',
                ha='center', va='center', fontsize=14, color='white', transform=ax.transAxes)
        ax.axis('off')
    else:
        gs = fig.add_gridspec(2, 2, hspace=0.4, wspace=0.3)
        ax1 = fig.add_subplot(gs[0, :])
        ax1.set_facecolor('#16213e')
        if por_categoria:
            colores = ['#e94560', '#0f3460', '#533483', '#06d6a0', '#ffd166', '#ef476f', '#118ab2', '#073b4c', '#26547c', '#ffb703']
            wedges, texts, autotexts = ax1.pie(
                por_categoria.values(), labels=por_categoria.keys(),
                colors=colores[:len(por_categoria)], autopct='%1.1f%%', startangle=90,
                textprops={'color': 'white', 'fontsize': 9}
            )
            for autotext in autotexts:
                autotext.set_color('white')
                autotext.set_fontsize(8)
            ax1.set_title('Gastos por Categoria', color='white', fontsize=12, pad=10)
        else:
            ax1.text(0.5, 0.5, 'Sin gastos registrados', ha='center', va='center', color='white', fontsize=12, transform=ax1.transAxes)
            ax1.axis('off')

        ax2 = fig.add_subplot(gs[1, 0])
        ax2.set_facecolor('#16213e')
        valores = [total_ingresos, total_gastos]
        bars = ax2.bar(['Ingresos', 'Gastos'], valores, color=['#06d6a0', '#e94560'], width=0.5, edgecolor='none')
        for bar, val in zip(bars, valores):
            ax2.text(bar.get_x() + bar.get_width()/2., bar.get_height() + max(valores)*0.02,
                    f'${val:,.0f}', ha='center', va='bottom', color='white', fontsize=9, fontweight='bold')
        ax2.set_facecolor('#16213e')
        ax2.tick_params(colors='white')
        for spine in ['top', 'right']:
            ax2.spines[spine].set_visible(False)
        for spine in ['bottom', 'left']:
            ax2.spines[spine].set_color('#444')
        ax2.set_title('Ingresos vs Gastos', color='white', fontsize=11, pad=10)

        ax3 = fig.add_subplot(gs[1, 1])
        ax3.set_facecolor('#16213e')
        color_balance = '#06d6a0' if balance >= 0 else '#e94560'
        ax3.text(0.5, 0.6, f'{"↑" if balance >= 0 else "↓"} Balance', ha='center', va='center',
                fontsize=13, color='white', fontweight='bold', transform=ax3.transAxes)
        ax3.text(0.5, 0.35, f'${abs(balance):,.0f}', ha='center', va='center',
                fontsize=18, color=color_balance, fontweight='bold', transform=ax3.transAxes)
        ax3.text(0.5, 0.15, 'Positivo' if balance >= 0 else 'Negativo',
                ha='center', va='center', fontsize=11, color=color_balance, transform=ax3.transAxes)
        ax3.axis('off')
        ax3.set_title('Balance del Mes', color='white', fontsize=11, pad=10)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=120, bbox_inches='tight', facecolor='#1a1a2e', edgecolor='none')
    buf.seek(0)
    plt.close()
    return buf

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
    httpd = HTTPServer(("0.0.0.0", port), HealthHandler)
    httpd.serve_forever()

# ─────────────────────────────────────────
# HANDLERS DE TELEGRAM
# ─────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💸 Registrar gasto", callback_data="menu_gasto"),
         InlineKeyboardButton("💰 Registrar ingreso", callback_data="menu_ingreso")],
        [InlineKeyboardButton("📊 Ver resumen", callback_data="menu_resumen"),
         InlineKeyboardButton("📈 Ver grafica", callback_data="menu_grafica")],
        [InlineKeyboardButton("🎯 Metas de ahorro", callback_data="menu_metas"),
         InlineKeyboardButton("💡 Consejo del dia", callback_data="menu_consejo")],
        [InlineKeyboardButton("🔔 Mis presupuestos", callback_data="menu_presupuestos"),
         InlineKeyboardButton("📥 Exportar Excel", callback_data="menu_excel")],
    ]
    await update.message.reply_text(
        "Hola! Soy FinBot, tu asistente financiero personal.\n\n"
        "• Registrar gastos e ingresos\n"
        "• Ver resumen mensual con grafica\n"
        "• Alertas cuando superes tu presupuesto\n"
        "• Exportar historial a Excel\n\n"
        "Escribe naturalmente o usa los botones:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "menu_gasto":
        await query.message.reply_text("Escribe:\n\"Gaste 50000 en comida\"\no: /gasto 50000 comida")
    elif query.data == "menu_ingreso":
        await query.message.reply_text("Escribe:\n\"Gane 2000000\"\no: /ingreso 2000000")
    elif query.data == "menu_resumen":
        await mostrar_resumen(query.message, user_id)
    elif query.data == "menu_grafica":
        await query.message.reply_text("Generando tu grafica...")
        buf = generar_grafica(user_id)
        await query.message.reply_photo(photo=buf, caption="Tu resumen financiero del mes")
    elif query.data == "menu_metas":
        metas = get_metas(user_id)
        if not metas:
            await query.message.reply_text("No tienes metas aun.\nEscribe: \"Quiero ahorrar 500000 para vacaciones\"")
        else:
            texto = "Tus metas de ahorro:\n\n"
            for m in metas:
                texto += f"🎯 {m['nombre']}: ${float(m['objetivo']):,.0f}\n"
            await query.message.reply_text(texto)
    elif query.data == "menu_consejo":
        total_gastos, total_ingresos, balance, por_categoria = get_resumen(user_id)
        msg = await query.message.reply_text("Analizando tus finanzas...")
        consejo = await pedir_consejo_ia(user_id, total_gastos, total_ingresos, balance, por_categoria)
        await msg.edit_text(consejo)
    elif query.data == "menu_presupuestos":
        presupuestos = get_presupuestos(user_id)
        _, _, _, por_categoria = get_resumen(user_id)
        if not presupuestos:
            await query.message.reply_text(
                "No tienes presupuestos definidos.\n\n"
                "Escribe algo como:\n"
                "\"Presupuesto comida 300000\"\n"
                "\"Mi limite en transporte es 150000\""
            )
        else:
            texto = "🔔 Tus presupuestos del mes:\n\n"
            for cat, limite in presupuestos.items():
                gastado = por_categoria.get(cat, 0)
                porcentaje = (gastado / limite * 100) if limite > 0 else 0
                emoji = "🚨" if porcentaje >= 100 else "⚠️" if porcentaje >= 80 else "✅"
                texto += f"{emoji} {cat}\n   Limite: ${limite:,.0f} | Gastado: ${gastado:,.0f} ({porcentaje:.0f}%)\n\n"
            await query.message.reply_text(texto)
    elif query.data == "menu_excel":
        await query.message.reply_text("Generando tu archivo Excel...")
        buf, mes_nombre = generar_excel(user_id)
        await query.message.reply_document(
            document=buf,
            filename=f"FinBot_{mes_nombre.replace(' ', '_')}.xlsx",
            caption=f"📊 Tu reporte financiero de {mes_nombre}"
        )

async def mostrar_resumen(message, user_id):
    total_gastos, total_ingresos, balance, por_categoria = get_resumen(user_id)
    mes = datetime.datetime.now().strftime("%B %Y")
    texto = f"📊 Resumen de {mes}\n━━━━━━━━━━━━━━━\n"
    texto += f"💰 Ingresos: ${total_ingresos:,.0f}\n"
    texto += f"💸 Gastos:   ${total_gastos:,.0f}\n"
    texto += f"{'📈' if balance >= 0 else '📉'} Balance:  ${balance:,.0f}\n"
    if por_categoria:
        texto += "\n📂 Por categoria:\n"
        for cat, monto in sorted(por_categoria.items(), key=lambda x: x[1], reverse=True):
            texto += f"  • {cat}: ${monto:,.0f}\n"
    if not por_categoria and total_ingresos == 0:
        texto += "\nAun no tienes registros este mes."
    keyboard = [[InlineKeyboardButton("📈 Grafica", callback_data="menu_grafica"),
                 InlineKeyboardButton("📥 Excel", callback_data="menu_excel")]]
    await message.reply_text(texto, reply_markup=InlineKeyboardMarkup(keyboard))

async def pedir_consejo_ia(user_id, total_gastos, total_ingresos, balance, por_categoria):
    contexto = (f"Datos del mes: ingresos={total_ingresos}, gastos={total_gastos}, balance={balance}, "
                f"por categoria={por_categoria}. Da un consejo financiero corto (max 3 oraciones).")
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300, "system": SYSTEM_PROMPT,
                  "messages": [{"role": "user", "content": contexto}]},
            timeout=30
        )
        return resp.json()["content"][0]["text"]
    except Exception as e:
        print(f"Error consejo IA: {e}")
        return "Registra tus gastos e ingresos regularmente para recibir consejos personalizados!"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text

    if user_text.startswith("/gasto"):
        parts = user_text.split()
        if len(parts) >= 2:
            try:
                monto = float(parts[1].replace(",", ""))
                cat = " ".join(parts[2:]) if len(parts) > 2 else "General"
                registrar_gasto(user_id, monto, cat, cat)
                await update.message.reply_text(f"✅ Gasto registrado\n💸 ${monto:,.0f} en {cat}")
                alerta = verificar_alerta_presupuesto(user_id, cat)
                if alerta:
                    await update.message.reply_text(alerta)
                return
            except Exception as e:
                print(f"Error gasto rapido: {e}")

    if user_text.startswith("/ingreso"):
        parts = user_text.split()
        if len(parts) >= 2:
            try:
                monto = float(parts[1].replace(",", ""))
                desc = " ".join(parts[2:]) if len(parts) > 2 else "Ingreso"
                registrar_ingreso(user_id, monto, desc)
                await update.message.reply_text(f"✅ Ingreso registrado\n💰 ${monto:,.0f}")
                return
            except Exception as e:
                print(f"Error ingreso rapido: {e}")

    if user_text.lower() in ["/resumen", "resumen", "balance"]:
        await mostrar_resumen(update.message, user_id)
        return

    if user_text.lower() in ["/grafica", "grafica", "grafico"]:
        await update.message.reply_text("Generando tu grafica...")
        buf = generar_grafica(user_id)
        await update.message.reply_photo(photo=buf, caption="Tu resumen financiero del mes")
        return

    if user_text.lower() in ["/excel", "exportar", "excel"]:
        await update.message.reply_text("Generando tu archivo Excel...")
        buf, mes_nombre = generar_excel(user_id)
        await update.message.reply_document(
            document=buf,
            filename=f"FinBot_{mes_nombre.replace(' ', '_')}.xlsx",
            caption=f"📊 Tu reporte financiero de {mes_nombre}"
        )
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
        await update.message.reply_text("Hubo un error, intenta de nuevo.")
        return

    if reply.startswith("GASTO_REGISTRADO|"):
        parts = reply.split("|")
        if len(parts) >= 4:
            try:
                monto = float(parts[1])
                categoria = parts[2]
                descripcion = parts[3]
                registrar_gasto(user_id, monto, categoria, descripcion)
                await update.message.reply_text(
                    f"✅ Gasto registrado\n💸 ${monto:,.0f}\n📂 {categoria}\n📝 {descripcion}")
                alerta = verificar_alerta_presupuesto(user_id, categoria)
                if alerta:
                    await update.message.reply_text(alerta)
                guardar_historial(user_id, "assistant", reply)
                return
            except Exception as e:
                print(f"Error parsing gasto: {e}")

    if reply.startswith("INGRESO_REGISTRADO|"):
        parts = reply.split("|")
        if len(parts) >= 3:
            try:
                monto = float(parts[1])
                descripcion = parts[2]
                registrar_ingreso(user_id, monto, descripcion)
                await update.message.reply_text(f"✅ Ingreso registrado\n💰 ${monto:,.0f}\n📝 {descripcion}")
                guardar_historial(user_id, "assistant", reply)
                return
            except Exception as e:
                print(f"Error parsing ingreso: {e}")

    if reply.startswith("PRESUPUESTO_DEFINIDO|"):
        parts = reply.split("|")
        if len(parts) >= 3:
            try:
                categoria = parts[1].strip()
                limite = float(parts[2].strip())
                guardar_presupuesto(user_id, categoria, limite)
                await update.message.reply_text(
                    f"✅ Presupuesto guardado\n🔔 {categoria}: ${limite:,.0f}/mes\n"
                    f"Te avisare al llegar al 80% y al 100%")
                guardar_historial(user_id, "assistant", reply)
                return
            except Exception as e:
                print(f"Error parsing presupuesto: {e}")

    guardar_historial(user_id, "assistant", reply)
    await update.message.reply_text(reply)

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def main():
    init_db()
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    print("Servidor web activo")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("resumen", lambda u, c: mostrar_resumen(u.message, u.effective_user.id)))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("FinBot iniciado con alertas y Excel!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
