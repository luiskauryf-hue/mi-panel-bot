import os
import time
import sqlite3
from datetime import datetime
import requests
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# CONFIGURACIÓN DE TU BOT DE TELEGRAM
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8672312818:AAE7QGCriKB_huWbkY0rnea9jeN0QRAMB-c")
ADMIN_CHAT_ID = "-1003829850200"
DB_PATH = "panel_kaury.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS perfiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dolphin_id TEXT UNIQUE NOT NULL,
            codigo TEXT NOT NULL,
            modelo TEXT,
            telefono TEXT,
            ubicacion TEXT,
            texto TEXT,
            foto_url TEXT,
            enlace_anuncio TEXT,
            estado TEXT DEFAULT 'ACTIVA',
            bumps_hoy INTEGER DEFAULT 0,
            bumps_total INTEGER DEFAULT 0,
            ultimo_bump TEXT DEFAULT 'Sin actividad',
            es_renta INTEGER DEFAULT 0,
            cliente_chat_id TEXT DEFAULT '',
            cliente_nombre TEXT DEFAULT '',
            vence_renta TEXT DEFAULT '',
            ultima_edicion_cliente REAL DEFAULT 0,
            esperando_input TEXT DEFAULT ''
        )
    """)
    # Migración automática segura por si la tabla ya existía en Render
    columnas_nuevas = [
        ("es_renta", "INTEGER DEFAULT 0"),
        ("cliente_chat_id", "TEXT DEFAULT ''"),
        ("cliente_nombre", "TEXT DEFAULT ''"),
        ("vence_renta", "TEXT DEFAULT ''"),
        ("ultima_edicion_cliente", "REAL DEFAULT 0"),
        ("esperando_input", "TEXT DEFAULT ''")
    ]
    for col, tipo in columnas_nuevas:
        try:
            conn.execute(f"ALTER TABLE perfiles ADD COLUMN {col} {tipo}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()

init_db()

# --- FUNCIONES DE TELEGRAM PARA CLIENTES DE RENTA ---

def enviar_telegram(chat_id, texto, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload)

def enviar_menu_renta(chat_id, perfil):
    estado_txt = "🟢 ACTIVO (Subiendo)" if perfil["estado"] == "ACTIVA" else "🔴 PAUSADO"
    btn_estado = "⏸️ Pausar Anuncio" if perfil["estado"] == "ACTIVA" else "▶️ Encender Anuncio"

    ahora = time.time()
    ultima_ed = float(perfil["ultima_edicion_cliente"] or 0)
    segundos_pasados = ahora - ultima_ed

    if segundos_pasados >= 86400:
        btn_editar = "✏️ Editar Texto (Disponible)"
    else:
        horas = int((86400 - segundos_pasados) // 3600)
        minutos = int(((86400 - segundos_pasados) % 3600) // 60)
        btn_editar = f"🔒 Editar Texto ({horas}h {minutos}m)"

    vence = perfil["vence_renta"] or "Consultar con soporte"
    texto = (
        f"📋 *CONTROL DE TU ANUNCIO ({perfil['codigo']})*\n\n"
        f"• *Estado:* {estado_txt}\n"
        f"• *Ubicación:* {perfil['ubicacion'] or 'Sin definir'}\n"
        f"• *Vence renta:* {vence}\n"
        f"• *Fotos / Borrado:* 🔒 Bloqueados por seguridad\n\n"
        f"Toca un botón para gestionar tu perfil:"
    )

    teclado = {
        "inline_keyboard": [
            [{"text": btn_estado, "callback_data": f"renta_toggle_{perfil['id']}"}],
            [{"text": "📍 Cambiar Dirección", "callback_data": f"renta_dir_{perfil['id']}"}],
            [{"text": btn_editar, "callback_data": f"renta_edit_{perfil['id']}"}],
            [{"text": "🆘 Ayuda / Soporte", "callback_data": f"renta_help_{perfil['id']}"}]
        ]
    }
    enviar_telegram(chat_id, texto, teclado)

# --- WEBHOOK DE TELEGRAM (RECIBE BOTONES Y MENSAJES DE LOS GRUPOS) ---

@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    conn = get_db()

    # 1. Cuando el cliente toca un botón en Telegram
    if "callback_query" in update:
        cb = update["callback_query"]
        chat_id = str(cb["message"]["chat"]["id"])
        data = cb.get("data", "")
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb["id"]})

        if data.startswith("renta_toggle_"):
            pid = int(data.split("_")[-1])
            perfil = conn.execute("SELECT * FROM perfiles WHERE id = ? AND cliente_chat_id = ?", (pid, chat_id)).fetchone()
            if perfil:
                nuevo = "PAUSADA" if perfil["estado"] == "ACTIVA" else "ACTIVA"
                conn.execute("UPDATE perfiles SET estado = ? WHERE id = ?", (nuevo, pid))
                conn.commit()
                perfil = conn.execute("SELECT * FROM perfiles WHERE id = ?", (pid,)).fetchone()
                enviar_menu_renta(chat_id, perfil)

        elif data.startswith("renta_dir_"):
            pid = int(data.split("_")[-1])
            conn.execute("UPDATE perfiles SET esperando_input = 'direccion' WHERE id = ? AND cliente_chat_id = ?", (pid, chat_id))
            conn.commit()
            enviar_telegram(chat_id, "📍 Escribe ahora la *nueva dirección o zona* dentro de tu ciudad:")

        elif data.startswith("renta_edit_"):
            pid = int(data.split("_")[-1])
            perfil = conn.execute("SELECT * FROM perfiles WHERE id = ? AND cliente_chat_id = ?", (pid, chat_id)).fetchone()
            if perfil:
                ahora = time.time()
                ultima_ed = float(perfil["ultima_edicion_cliente"] or 0)
                if (ahora - ultima_ed) < 86400:
                    restante = 86400 - (ahora - ultima_ed)
                    horas = int(restante // 3600)
                    minutos = int((restante % 3600) // 60)
                    enviar_telegram(chat_id, f"🔒 *Edición bloqueada.* Solo puedes editar el texto una vez cada 24 horas.\nDisponible de nuevo en *{horas}h {minutos}m*.")
                else:
                    conn.execute("UPDATE perfiles SET esperando_input = 'texto' WHERE id = ?", (pid,))
                    conn.commit()
                    enviar_telegram(chat_id, "✏️ Escribe ahora el *nuevo texto* para tu anuncio (luego se bloqueará por 24 horas):")

        elif data.startswith("renta_help_"):
            pid = int(data.split("_")[-1])
            teclado_ayuda = {
                "inline_keyboard": [
                    [{"text": "⚠️ Mi página no está subiendo", "callback_data": f"sop_nosube_{pid}"}],
                    [{"text": "➕ Quiero rentar otro anuncio", "callback_data": f"sop_nuevo_{pid}"}]
                ]
            }
            enviar_telegram(chat_id, "🎧 *Centro de Soporte*\nSelecciona qué necesitas:", teclado_ayuda)

        elif data.startswith("sop_"):
            partes = data.split("_")
            tipo = partes[1]
            pid = int(partes[2])
            perfil = conn.execute("SELECT * FROM perfiles WHERE id = ?", (pid,)).fetchone()
            motivo = "Mi página no está subiendo" if tipo == "nosube" else "Quiere rentar otro anuncio"
            enviar_telegram(chat_id, "✅ Tu solicitud fue enviada al administrador. Te responderemos en breve.")
            if perfil:
                enviar_telegram(ADMIN_CHAT_ID, f"🚨 *ALERTA DE RENTA*\n• Cliente: {perfil['cliente_nombre'] or chat_id}\n• Perfil: `{perfil['codigo']}` ({perfil['dolphin_id']})\n• Motivo: *{motivo}*")

    # 2. Cuando el cliente envía un mensaje de texto en su grupo
    elif "message" in update:
        msg = update["message"]
        chat_id = str(msg["chat"]["id"])
        texto_msg = msg.get("text", "").strip()

        # Comando rápido /id para saber el ID de cualquier grupo nuevo
        if texto_msg == "/id":
            enviar_telegram(chat_id, f"🆔 El ID de este grupo es:\n`{chat_id}`")
            conn.close()
            return jsonify({"ok": True})

        perfil = conn.execute("SELECT * FROM perfiles WHERE cliente_chat_id = ? AND es_renta = 1", (chat_id,)).fetchone()
        if perfil:
            if texto_msg.lower() in ["/menu", "menu", "/start"]:
                conn.execute("UPDATE perfiles SET esperando_input = '' WHERE id = ?", (perfil["id"],))
                conn.commit()
                enviar_menu_renta(chat_id, perfil)
            elif perfil["esperando_input"] == "direccion" and texto_msg:
                conn.execute("UPDATE perfiles SET ubicacion = ?, esperando_input = '' WHERE id = ?", (texto_msg, perfil["id"]))
                conn.commit()
                enviar_telegram(chat_id, f"✅ Dirección actualizada a: *{texto_msg}*")
                perfil = conn.execute("SELECT * FROM perfiles WHERE id = ?", (perfil["id"],)).fetchone()
                enviar_menu_renta(chat_id, perfil)
            elif perfil["esperando_input"] == "texto" and texto_msg:
                conn.execute("UPDATE perfiles SET texto = ?, ultima_edicion_cliente = ?, esperando_input = '' WHERE id = ?", (texto_msg, time.time(), perfil["id"]))
                conn.commit()
                enviar_telegram(chat_id, "✅ Texto actualizado. El candado de 24 horas 🔒 se ha activado.")
                perfil = conn.execute("SELECT * FROM perfiles WHERE id = ?", (perfil["id"],)).fetchone()
                enviar_menu_renta(chat_id, perfil)

    conn.close()
    return jsonify({"ok": True})

# --- VISTAS DEL PANEL WEB (PC Y IPHONE) ---

@app.route("/")
def index():
    conn = get_db()
    perfiles = conn.execute("SELECT * FROM perfiles ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("index.html", perfiles=perfiles)

@app.route("/agregar", methods=["POST"])
def agregar_perfil():
    dolphin_id = request.form.get("dolphin_id", "").strip()
    codigo = request.form.get("codigo", "").strip()
    modelo = request.form.get("modelo", "").strip()
    telefono = request.form.get("telefono", "").strip()
    ubicacion = request.form.get("ubicacion", "").strip()
    texto = request.form.get("texto", "").strip()
    foto_url = request.form.get("foto_url", "").strip()
    enlace_anuncio = request.form.get("enlace_anuncio", "").strip()
    es_renta = 1 if request.form.get("es_renta") == "1" else 0
    cliente_chat_id = request.form.get("cliente_chat_id", "").strip()
    cliente_nombre = request.form.get("cliente_nombre", "").strip()
    vence_renta = request.form.get("vence_renta", "").strip()

    if dolphin_id and codigo:
        conn = get_db()
        try:
            conn.execute("""
                INSERT INTO perfiles (
                    dolphin_id, codigo, modelo, telefono, ubicacion, texto, foto_url,
                    enlace_anuncio, es_renta, cliente_chat_id, cliente_nombre, vence_renta
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (dolphin_id, codigo, modelo, telefono, ubicacion, texto, foto_url, enlace_anuncio, es_renta, cliente_chat_id, cliente_nombre, vence_renta))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        finally:
            conn.close()
    return redirect(url_for("index"))

@app.route("/toggle_estado/<int:id>")
def toggle_estado(id):
    conn = get_db()
    perfil = conn.execute("SELECT estado FROM perfiles WHERE id = ?", (id,)).fetchone()
    if perfil:
        nuevo_estado = "PAUSADA" if perfil["estado"] == "ACTIVA" else "ACTIVA"
        conn.execute("UPDATE perfiles SET estado = ? WHERE id = ?", (nuevo_estado, id))
        conn.commit()
    conn.close()
    return redirect(url_for("index"))

@app.route("/eliminar/<int:id>")
def eliminar_perfil(id):
    conn = get_db()
    conn.execute("DELETE FROM perfiles WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

# --- RUTAS API PARA LA EXTENSIÓN DE DOLPHIN ANTY ---

@app.route("/api/perfil/<dolphin_id>", methods=["GET"])
def api_obtener_perfil(dolphin_id):
    conn = get_db()
    perfil = conn.execute("SELECT * FROM perfiles WHERE dolphin_id = ?", (dolphin_id,)).fetchone()
    conn.close()

    if not perfil:
        return jsonify({"ok": False, "error": "ID de Dolphin no registrado en el panel"}), 404

    return jsonify({
        "ok": True,
        "dolphin_id": perfil["dolphin_id"],
        "codigo": perfil["codigo"],
        "modelo": perfil["modelo"],
        "telefono": perfil["telefono"],
        "ubicacion": perfil["ubicacion"],
        "texto": perfil["texto"],
        "foto_url": perfil["foto_url"],
        "estado": perfil["estado"],
        "es_renta": bool(perfil["es_renta"])
    })

@app.route("/api/reportar_bump", methods=["POST"])
def api_reportar_bump():
    dolphin_id = request.form.get("dolphin_id")
    imagen = request.files.get("captura")

    conn = get_db()
    perfil = conn.execute("SELECT * FROM perfiles WHERE dolphin_id = ?", (dolphin_id,)).fetchone()

    if not perfil:
        conn.close()
        return jsonify({"ok": False, "error": "Perfil no encontrado"}), 404

    ahora = datetime.now().strftime("%d/%m/%Y %I:%M:%S %p")
    nuevos_hoy = perfil["bumps_hoy"] + 1
    nuevos_total = perfil["bumps_total"] + 1

    conn.execute("""
        UPDATE perfiles
        SET bumps_hoy = ?, bumps_total = ?, ultimo_bump = ?
        WHERE dolphin_id = ?
    """, (nuevos_hoy, nuevos_total, ahora, dolphin_id))
    conn.commit()
    conn.close()

    # Si es perfil de renta, manda la captura al grupo privado del cliente; si es propio, al tuyo
    destino_chat = perfil["cliente_chat_id"] if (perfil["es_renta"] and perfil["cliente_chat_id"]) else ADMIN_CHAT_ID

    caption = (
        f"🔥 *PUBLICACIÓN / BUMP CONFIRMADO*\n\n"
        f"👤 *Modelo:* {perfil['modelo'] or 'N/A'}\n"
        f"🆔 *Código:* {perfil['codigo']} | *Dolphin ID:* `{dolphin_id}`\n"
        f"📞 *Teléfono:* {perfil['telefono'] or 'N/A'}\n"
        f"📍 *Ubicación:* {perfil['ubicacion'] or 'N/A'}\n"
        f"🟢 *Estado:* {perfil['estado']}\n"
        f"🕒 *Hora:* {ahora}\n"
        f"📊 *Bumps hoy:* {nuevos_hoy} | *Total:* {nuevos_total}"
    )

    if imagen:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        files = {"photo": (imagen.filename, imagen.stream, imagen.mimetype)}
        data = {"chat_id": destino_chat, "caption": caption, "parse_mode": "Markdown"}
        resp = requests.post(url, data=data, files=files)
    else:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={"chat_id": destino_chat, "text": caption, "parse_mode": "Markdown"})

    return jsonify({"ok": True, "telegram_status": resp.status_code})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
