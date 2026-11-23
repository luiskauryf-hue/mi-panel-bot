import os
import sqlite3
from datetime import datetime
import requests
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# CONFIGURACIÓN DE TU BOT DE TELEGRAM
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8672312818:AAE7QGCriKB_huWbkY0rnea9jeN0QRAMB-c")
TELEGRAM_CHAT_ID = "-1003829850200"
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
            ultimo_bump TEXT DEFAULT 'Sin actividad'
        )
    """)
    conn.commit()
    conn.close()

init_db()

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

    if dolphin_id and codigo:
        conn = get_db()
        try:
            conn.execute("""
                INSERT INTO perfiles (dolphin_id, codigo, modelo, telefono, ubicacion, texto, foto_url, enlace_anuncio)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (dolphin_id, codigo, modelo, telefono, ubicacion, texto, foto_url, enlace_anuncio))
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
        "estado": perfil["estado"]
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
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
        resp = requests.post(url, data=data, files=files)
    else:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "Markdown"})

    return jsonify({"ok": True, "telegram_status": resp.status_code})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))