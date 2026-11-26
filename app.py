# SEBASTIÁN MEGA v11 — Backend único
# Un solo dueño del bot de Telegram (webhook), cola por perfil, estado deseado persistente.
# La extensión NO contiene el token: se autentica con una api_key por perfil.
import os, json, time, hmac, hashlib, secrets, threading, html
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import requests
from flask import Flask, request, jsonify, session, redirect, url_for, render_template, render_template_string, abort
from jinja2 import TemplateNotFound
from flask_cors import CORS
from sqlalchemy import create_engine, Column, Integer, String, Boolean, Float, DateTime, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

RD_TZ = ZoneInfo("America/Santo_Domingo")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///panel_v11.db")
# Render Postgres suele entregar postgres:// -> SQLAlchemy 2 necesita postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", TELEGRAM_WEBHOOK_SECRET)
OUTBOUND_ENABLED = os.environ.get("OUTBOUND_ENABLED", "1") == "1"
TG_TIMEOUT = 10
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()

def now_utc():
    return datetime.now(timezone.utc)
def rd_now():
    return now_utc().astimezone(RD_TZ)
def rd_hhmm():
    return rd_now().strftime("%H:%M")
def rd_date():
    return rd_now().strftime("%Y-%m-%d")
def hash_key(key: str) -> str:
    return hashlib.sha256((key or "").encode()).hexdigest()
def esc(s):
    return html.escape(str(s or ""), quote=False)

class Profile(Base):
    __tablename__ = "profiles"
    id = Column(Integer, primary_key=True)
    dolphin_id = Column(String, unique=True, nullable=False, index=True)
    codigo = Column(String, nullable=False)
    modelo = Column(String, default=""); telefono = Column(String, default="")
    ubicacion = Column(String, default=""); texto = Column(Text, default="")
    foto_url = Column(String, default=""); enlace_anuncio = Column(String, default="")
    paused = Column(Boolean, default=False); schedule_paused = Column(Boolean, default=False)
    interval_min = Column(Integer, default=16)
    pause_at_rd = Column(String, default=""); resume_at_rd = Column(String, default="")
    next_bump_at_utc = Column(DateTime(timezone=True), nullable=True)
    last_bump_at_utc = Column(DateTime(timezone=True), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    bumps_today = Column(Integer, default=0); bumps_date = Column(String, default="")
    bumps_total = Column(Integer, default=0)
    is_rental = Column(Boolean, default=False)
    client_chat_id = Column(String, default=""); client_name = Column(String, default="")
    rental_expires_at = Column(DateTime(timezone=True), nullable=True)
    rental_expired = Column(Boolean, default=False)
    last_text_edit_at = Column(Float, default=0); last_location_edit_at = Column(Float, default=0)
    api_key_hash = Column(String, default="")
    created_at = Column(DateTime(timezone=True), default=now_utc)

class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True); nombre = Column(String, unique=True, nullable=False)

class ProfileGroup(Base):
    __tablename__ = "profile_groups"
    id = Column(Integer, primary_key=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    __table_args__ = (UniqueConstraint("profile_id", "group_id"),)

class Command(Base):
    __tablename__ = "commands"
    id = Column(Integer, primary_key=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False, index=True)
    type = Column(String, nullable=False)  # pause|resume|bump_now|photo|set_config
    payload = Column(Text, default="{}")
    idempotency_key = Column(String, unique=True, nullable=False)
    status = Column(String, default="pending")  # pending|acked|failed
    attempts = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    acked_at = Column(DateTime(timezone=True), nullable=True); ack_detail = Column(Text, default="")

class TelegramUpdate(Base):
    __tablename__ = "telegram_updates"
    update_id = Column(Integer, primary_key=True); received_at = Column(DateTime(timezone=True), default=now_utc)

class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, nullable=False); kind = Column(String, default="message")
    text = Column(Text, default=""); photo_path = Column(String, default="")
    reply_markup = Column(Text, default="")
    status = Column(String, default="pending"); attempts = Column(Integer, default=0)
    not_before = Column(Float, default=0); created_at = Column(DateTime(timezone=True), default=now_utc)

class PendingInput(Base):
    __tablename__ = "pending_inputs"
    id = Column(Integer, primary_key=True)
    profile_id = Column(Integer, nullable=False); chat_id = Column(String, nullable=False)
    user_id = Column(String, default=""); field = Column(String, nullable=False)
    expires_at = Column(Float, nullable=False)

Base.metadata.create_all(engine)

app = Flask(__name__)

LOGIN_HTML = '<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Panel v11 — Acceso</title>\n<style>body{font-family:system-ui;background:#0f172a;color:#e2e8f0;display:grid;place-items:center;min-height:100vh;margin:0}.card{background:#1e293b;padding:24px;border-radius:12px;width:min(92vw,360px)}input,button{width:100%;padding:10px;border-radius:8px;border:1px solid #475569;margin-top:8px;box-sizing:border-box}button{background:#059669;color:#fff;border:0;font-weight:700}.err{color:#fda4af}</style></head>\n<body><form class="card" method="post"><h1>Panel SEBASTIÁN MEGA v11</h1>{% if error %}<p class="err">{{error}}</p>{% endif %}<label>Contraseña del panel<input type="password" name="password" required autofocus></label><button>Entrar</button></form></body></html>\n'
INDEX_HTML = '<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Panel v11</title>\n<style>body{font-family:system-ui;background:#020617;color:#e2e8f0;margin:0}.wrap{max-width:1100px;margin:auto;padding:16px}.card{background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:16px;margin:12px 0}input,select,textarea,button{padding:8px;border-radius:8px;border:1px solid #334155;background:#1e293b;color:#e2e8f0}button{background:#059669;color:#fff;border:0;font-weight:700;cursor:pointer}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}.warn{background:#451a03;border:1px solid #f59e0b;padding:12px;border-radius:10px;word-break:break-all}code{background:#1e293b;padding:2px 6px;border-radius:6px}</style></head>\n<body><div class="wrap"><h1>Panel SEBASTIÁN MEGA v11</h1><p>Hora RD: {{rd}} · Backend único · Cola por perfil</p>\n<form method="post" action="/logout"><button>Salir</button></form>\n{% if new_key %}<div class="warn"><b>Clave API nueva para {{new_codigo}} (cópiala ahora, no se vuelve a mostrar):</b><br><code>{{new_key}}</code><br>Pégala en el popup de la extensión v11 de ese perfil Dolphin.</div>{% endif %}\n<div class="card"><h2>+ Registrar perfil</h2><form method="post" action="/profiles"><input type="hidden" name="csrf" value="{{csrf}}"><div class="grid">\n<input name="dolphin_id" placeholder="ID Dolphin" required><input name="codigo" placeholder="Código (ej. Perfil-3)" required>\n<input name="modelo" placeholder="Modelo"><input name="telefono" placeholder="Teléfono"><input name="ubicacion" placeholder="Ubicación">\n<input name="foto_url" placeholder="URL foto"><input name="enlace_anuncio" placeholder="Enlace anuncio">\n<input name="interval_min" type="number" value="16" min="1"><label>Pausa RD <input name="pause_at_rd" type="time"></label><label>Reanuda RD <input name="resume_at_rd" type="time"></label>\n<select name="is_rental"><option value="0">Propio</option><option value="1">Renta</option></select>\n<input name="client_name" placeholder="Cliente (renta)"><input name="client_chat_id" placeholder="Chat ID cliente"><label>Vence renta <input name="rental_expires_at" type="datetime-local"></label>\n<textarea name="texto" placeholder="Texto del anuncio"></textarea></div><button>Guardar y generar clave</button></form></div>\n<div class="card"><h2>Grupos</h2><form method="post" action="/groups"><input type="hidden" name="csrf" value="{{csrf}}"><input name="nombre" placeholder="Nombre del grupo" required><button>Crear grupo</button></form>\n<p>{% for g in groups %}<code>{{g.nombre}}</code> {% endfor %}</p></div>\n{% for p in profiles %}<div class="card"><h3>{{p.codigo}} <small>({{p.modelo or \'Sin nombre\'}})</small> — {{\'🔴 PAUSADA\' if p.paused else \'🟢 ACTIVA\'}}{% if p.is_rental %} · RENTA{% if p.rental_expired %} VENCIDA{% endif %}{% endif %}</h3>\n<p>Dolphin: <code>{{p.dolphin_id}}</code> · Tel: {{p.telefono or \'N/A\'}} · Ubicación: {{p.ubicacion or \'N/A\'}}<br>\nIntervalo: {{p.interval_min}} min · Pausa RD: {{p.pause_at_rd or \'—\'}} · Reanuda RD: {{p.resume_at_rd or \'—\'}}<br>\nÚltimo bump: {{p.last_bump_at_utc or \'Sin actividad\'}} · Bumps hoy: {{p.bumps_today}} · Total: {{p.bumps_total}} · Última señal: {{p.last_seen_at or \'nunca\'}}</p>\n<form method="post" action="/profiles/{{p.id}}/toggle" style="display:inline"><input type="hidden" name="csrf" value="{{csrf}}"><button>{{\'Reanudar\' if p.paused else \'Pausar\'}}</button></form>\n<form method="post" action="/profiles/{{p.id}}/regen-key" style="display:inline"><input type="hidden" name="csrf" value="{{csrf}}"><button>Regenerar clave API</button></form>\n<form method="post" action="/profiles/{{p.id}}/delete" style="display:inline" onsubmit="return confirm(\'¿Eliminar este perfil?\')"><input type="hidden" name="csrf" value="{{csrf}}"><button style="background:#be123c">Borrar</button></form>\n<details><summary>Editar</summary><form method="post" action="/profiles/{{p.id}}/edit"><input type="hidden" name="csrf" value="{{csrf}}"><div class="grid">\n<input name="modelo" value="{{p.modelo or \'\'}}" placeholder="Modelo"><input name="telefono" value="{{p.telefono or \'\'}}" placeholder="Teléfono">\n<input name="ubicacion" value="{{p.ubicacion or \'\'}}" placeholder="Ubicación"><input name="interval_min" type="number" value="{{p.interval_min}}">\n<label>Pausa RD <input name="pause_at_rd" type="time" value="{{p.pause_at_rd or \'\'}}"></label><label>Reanuda RD <input name="resume_at_rd" type="time" value="{{p.resume_at_rd or \'\'}}"></label>\n<select name="is_rental"><option value="0" {% if not p.is_rental %}selected{% endif %}>Propio</option><option value="1" {% if p.is_rental %}selected{% endif %}>Renta</option></select>\n<input name="client_name" value="{{p.client_name or \'\'}}" placeholder="Cliente"><input name="client_chat_id" value="{{p.client_chat_id or \'\'}}" placeholder="Chat ID cliente">\n<label>Vence <input name="rental_expires_at" type="datetime-local"></label><textarea name="texto">{{p.texto or \'\'}}</textarea></div><button>Guardar cambios</button></form></details></div>\n{% else %}<p>Sin perfiles todavía.</p>{% endfor %}\n</div></body></html>\n'

def render_page(name, **ctx):
    # Siempre usar las pantallas v11 de adentro del archivo.
    # La carpeta templates del repo puede tener la pantalla vieja (form /agregar).
    return render_template_string(LOGIN_HTML if name=='login.html' else INDEX_HTML, **ctx)

app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
CORS(app, resources={r"/api/*": {"origins": "*"}})

def db():
    return SessionLocal()

# ---------- Telegram saliente: una sola cola con rate-limit ----------
def enqueue_telegram(sess, chat_id, text="", reply_markup=None, photo_path="", caption=""):
    if not chat_id:
        return
    kind = "photo" if photo_path else "message"
    body = caption if kind == "photo" else text
    sess.add(OutboundMessage(chat_id=str(chat_id), kind=kind, text=body, photo_path=photo_path or "",
                             reply_markup=json.dumps(reply_markup) if reply_markup else ""))
    sess.commit()

def _tg_send_once(msg):
    if not TELEGRAM_BOT_TOKEN:
        return False, 0, "sin token"
    try:
        if msg.kind == "photo" and msg.photo_path and os.path.exists(msg.photo_path):
            with open(msg.photo_path, "rb") as f:
                data = {"chat_id": msg.chat_id, "caption": msg.text[:1024], "parse_mode": "HTML"}
                if msg.reply_markup: data["reply_markup"] = msg.reply_markup
                r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto", data=data, files={"photo": f}, timeout=TG_TIMEOUT)
        else:
            payload = {"chat_id": msg.chat_id, "text": msg.text[:4096], "parse_mode": "HTML"}
            if msg.reply_markup: payload["reply_markup"] = json.loads(msg.reply_markup)
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=TG_TIMEOUT)
        if r.status_code == 429:
            retry = 5
            try: retry = int(r.json().get("parameters", {}).get("retry_after", 5))
            except Exception: pass
            return False, retry, "429"
        return r.ok, 0, r.text[:200]
    except Exception as e:
        return False, 5, str(e)[:200]

def outbound_worker():
    while True:
        try:
            sess = db()
            msg = sess.query(OutboundMessage).filter_by(status="pending").filter(OutboundMessage.not_before <= time.time()).order_by(OutboundMessage.id).first()
            if not msg:
                sess.close(); time.sleep(0.5); continue
            ok, retry_after, detail = _tg_send_once(msg)
            msg.attempts += 1
            if ok:
                msg.status = "sent"
                if msg.photo_path and os.path.exists(msg.photo_path):
                    try: os.remove(msg.photo_path)
                    except Exception: pass
            elif retry_after:
                msg.not_before = time.time() + retry_after
                if msg.attempts >= 8: msg.status = "failed"
            else:
                msg.not_before = time.time() + 5
                if msg.attempts >= 5: msg.status = "failed"
            sess.commit(); sess.close()
            time.sleep(1.1)  # escalonado global 1-2 s entre envíos
        except Exception:
            time.sleep(2)

if OUTBOUND_ENABLED and os.environ.get("FLASK_ENV") != "testing":
    threading.Thread(target=outbound_worker, daemon=True).start()

# ---------- Estado deseado / comandos ----------
def make_command(sess, profile, ctype, payload, idem_prefix):
    key = f"{idem_prefix}:{profile.id}:{ctype}"
    existing = sess.query(Command).filter_by(idempotency_key=key).first()
    if existing: return existing
    cmd = Command(profile_id=profile.id, type=ctype, payload=json.dumps(payload or {}), idempotency_key=key)
    sess.add(cmd); return cmd

def apply_desired(sess, profile, paused=None, interval_min=None, pause_at_rd=None, resume_at_rd=None):
    if paused is not None:
        profile.paused = bool(paused)
        if not paused and not profile.next_bump_at_utc:
            profile.next_bump_at_utc = now_utc() + timedelta(minutes=int(profile.interval_min or 16))
    if interval_min is not None: profile.interval_min = max(1, int(interval_min))
    if pause_at_rd is not None: profile.pause_at_rd = pause_at_rd or ""
    if resume_at_rd is not None: profile.resume_at_rd = resume_at_rd or ""

def fan_out(sess, profiles, ctype, payload, idem_prefix, paused=None):
    cmds = []
    for i, p in enumerate(profiles):
        pl = dict(payload or {}); pl.setdefault("delay_seconds", i * 3)
        if paused is not None: apply_desired(sess, p, paused=paused)
        cmds.append(make_command(sess, p, ctype, pl, idem_prefix))
    sess.commit(); return cmds

def schedule_should_pause(profile, t=None):
    t = t or rd_hhmm(); pa = profile.pause_at_rd or ""; ra = profile.resume_at_rd or ""
    if pa and ra:
        return (pa <= t < ra) if pa < ra else (t >= pa or t < ra)
    if pa: return t >= pa
    if ra: return t < ra
    return False

def tick(sess):
    changed = []
    for p in sess.query(Profile).all():
        # Vencimiento de renta
        exp = p.rental_expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if p.is_rental and exp is not None and not p.rental_expired and now_utc() >= exp:
            p.rental_expired = True; p.paused = True
            make_command(sess, p, "pause", {"reason": "rental_expired"}, f"expire:{p.id}:{int(p.rental_expires_at.timestamp())}")
            if p.client_chat_id: enqueue_telegram(sess, p.client_chat_id, "🔒 Tu renta venció y el anuncio quedó pausado. Escribe a soporte para renovar.")
            if ADMIN_CHAT_ID: enqueue_telegram(sess, ADMIN_CHAT_ID, f"🔒 Renta vencida: <b>{esc(p.codigo)}</b> pausado automáticamente.")
            changed.append(p.codigo); continue
        # Horario RD por ventana (no minuto exacto)
        should = schedule_should_pause(p)
        if should and not p.paused:
            p.paused = True; p.schedule_paused = True
            make_command(sess, p, "pause", {"reason": "schedule_rd"}, f"sched-pause:{p.id}:{rd_date()}:{p.pause_at_rd}")
            changed.append(p.codigo)
        elif not should and p.schedule_paused:
            p.paused = False; p.schedule_paused = False
            p.next_bump_at_utc = now_utc() + timedelta(minutes=1)
            make_command(sess, p, "resume", {"reason": "schedule_rd"}, f"sched-resume:{p.id}:{rd_date()}:{p.resume_at_rd}")
            changed.append(p.codigo)
        # Bump vencido y perfil vivo (<5 min)
        if (not p.paused) and p.next_bump_at_utc and p.last_seen_at:
            nbt = p.next_bump_at_utc if p.next_bump_at_utc.tzinfo else p.next_bump_at_utc.replace(tzinfo=timezone.utc)
            lst = p.last_seen_at if p.last_seen_at.tzinfo else p.last_seen_at.replace(tzinfo=timezone.utc)
            if now_utc() >= nbt and (now_utc() - lst).total_seconds() < 300:
                pending = sess.query(Command).filter_by(profile_id=p.id, type="bump_now", status="pending").first()
                if not pending:
                    make_command(sess, p, "bump_now", {"reason": "due"}, f"bump-due:{p.id}:{int(nbt.timestamp())}")
    sess.commit(); return changed

def tick_worker():
    while True:
        try:
            sess = db(); tick(sess); sess.close()
        except Exception: pass
        time.sleep(60)
if os.environ.get("FLASK_ENV") != "testing":
    threading.Thread(target=tick_worker, daemon=True).start()

# ---------- Teclados ----------
def kb_admin_profile(p):
    return {"inline_keyboard": [
        [{"text": "⏸️ Pausar", "callback_data": f"adm:pause:{p.id}"}, {"text": "▶️ Reanudar", "callback_data": f"adm:resume:{p.id}"}],
        [{"text": "⏸️ Pausar todas", "callback_data": "adm:pause_all"}, {"text": "▶️ Reanudar todas", "callback_data": "adm:resume_all"}],
        [{"text": "⏰ Programar pausa", "callback_data": f"adm:menu_pause:{p.id}"}, {"text": "⏰ Programar reanudar", "callback_data": f"adm:menu_resume:{p.id}"}],
        [{"text": "🕒 Último bump", "callback_data": f"adm:last_bump:{p.id}"}, {"text": "⚡ Bump ahora", "callback_data": f"adm:bump:{p.id}"}],
        [{"text": "⏱️ Minutos", "callback_data": f"adm:menu_interval:{p.id}"}, {"text": "📊 Ver todos", "callback_data": "adm:status"}],
    ]}
def kb_global():
    return {"keyboard": [[{"text": "📊 Ver Estados (🟢/🔴)"}, {"text": "📸 Foto de Todos"}], [{"text": "⏸️ Pausar todas"}, {"text": "▶️ Reanudar todas"}]], "resize_keyboard": True, "is_persistent": True}
def fmt_ampm(hhmm):
    if not hhmm or ":" not in hhmm: return "No"
    h, m = map(int, hhmm.split(":")); ap = "PM" if h >= 12 else "AM"; h = h % 12 or 12
    return f"{h:02d}:{m:02d} {ap}"
def profile_status_line(p):
    alive = bool(p.last_seen_at and (now_utc() - (p.last_seen_at if p.last_seen_at.tzinfo else p.last_seen_at.replace(tzinfo=timezone.utc))).total_seconds() < 300)
    icon = "🟢" if (not p.paused and alive) else ("🔴" if p.paused else "⚪")
    nxt = ""
    if p.next_bump_at_utc and not p.paused:
        nbt = p.next_bump_at_utc if p.next_bump_at_utc.tzinfo else p.next_bump_at_utc.replace(tzinfo=timezone.utc)
        secs = max(0, int((nbt - now_utc()).total_seconds())); nxt = f"Próx: {secs//60}m {secs%60}s"
    return f"{icon} <b>{esc(p.codigo)}</b> — {'PAUSADA' if p.paused else 'Activa'} {nxt} | Pausa {fmt_ampm(p.pause_at_rd)} Reanuda {fmt_ampm(p.resume_at_rd)} | {'en línea' if alive else 'sin señal'}"

def send_rental_menu(sess, chat_id, p):
    if p.rental_expired:
        enqueue_telegram(sess, chat_id, "🔒 Tu renta venció. Escribe a soporte para renovar."); return
    now = time.time(); left = 86400 - (now - float(p.last_text_edit_at or 0))
    btn_edit = "✏️ Editar Texto (Disponible)" if left <= 0 else f"🔒 Editar Texto ({int(left//3600)}h {int((left%3600)//60)}m)"
    vence = p.rental_expires_at.astimezone(RD_TZ).strftime("%d/%m/%Y %I:%M %p") if p.rental_expires_at else "Consultar con soporte"
    txt = (f"📋 <b>CONTROL DE TU ANUNCIO ({esc(p.codigo)})</b>\n\n• <b>Estado:</b> {'🟢 ACTIVO' if not p.paused else '🔴 PAUSADO'}\n"
           f"• <b>Ubicación:</b> {esc(p.ubicacion or 'Sin definir')}\n• <b>Vence renta:</b> {esc(vence)}\n• <b>Fotos / Borrado:</b> 🔒 Bloqueados por seguridad\n\nToca un botón:")
    kb = {"inline_keyboard": [
        [{"text": "⏸️ Pausar Anuncio" if not p.paused else "▶️ Encender Anuncio", "callback_data": f"renta_toggle_{p.id}"}],
        [{"text": "📍 Cambiar Dirección", "callback_data": f"renta_dir_{p.id}"}],
        [{"text": btn_edit, "callback_data": f"renta_edit_{p.id}"}],
        [{"text": "🆘 Ayuda / Soporte", "callback_data": f"renta_help_{p.id}"}]]}
    enqueue_telegram(sess, chat_id, txt, kb)

def set_pending(sess, p, chat_id, user_id, field):
    sess.query(PendingInput).filter_by(profile_id=p.id, chat_id=str(chat_id)).delete()
    sess.add(PendingInput(profile_id=p.id, chat_id=str(chat_id), user_id=str(user_id or ""), field=field, expires_at=time.time()+600)); sess.commit()

def parse_hora(texto):
    t = texto.strip().upper(); import re
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)?$", t)
    if not m: return None
    h = int(m.group(1)); mm = m.group(2); mer = m.group(3)
    if mer == "PM" and h < 12: h += 12
    if mer == "AM" and h == 12: h = 0
    if h > 23 or int(mm) > 59: return None
    return f"{h:02d}:{mm}"

# ---------- Webhook único (admin + renta) ----------
@app.route("/telegram/webhook", methods=["POST"])
@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    if TELEGRAM_WEBHOOK_SECRET:
        if not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), TELEGRAM_WEBHOOK_SECRET):
            abort(403)
    update = request.get_json(silent=True) or {}
    sess = db()
    try:
        uid = update.get("update_id")
        if uid is not None:
            if sess.query(TelegramUpdate).filter_by(update_id=int(uid)).first():
                return jsonify({"ok": True, "dup": True})
            sess.add(TelegramUpdate(update_id=int(uid))); sess.commit()
        # Callbacks
        if "callback_query" in update:
            cb = update["callback_query"]; chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
            user_id = str(cb.get("from", {}).get("id", "")); data = cb.get("data", "") or ""
            # answerCallbackQuery directo (rápido, no por cola)
            if TELEGRAM_BOT_TOKEN:
                try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb.get("id")}, timeout=TG_TIMEOUT)
                except Exception: pass
            is_admin = bool(ADMIN_CHAT_ID and chat_id == str(ADMIN_CHAT_ID))
            idem = f"tg:{uid}"
            # --- Admin ---
            if is_admin and data.startswith("adm:"):
                parts = data.split(":"); action = parts[1] if len(parts) > 1 else ""
                profiles = sess.query(Profile).order_by(Profile.id).all()
                if action == "pause_all":
                    fan_out(sess, profiles, "pause", {"reason": "admin"}, idem, paused=True)
                    enqueue_telegram(sess, chat_id, f"🔴 Pausar todas: {len(profiles)} órdenes creadas. Cada perfil confirmará por heartbeat.", kb_global())
                elif action == "resume_all":
                    fan_out(sess, profiles, "resume", {"reason": "admin"}, idem, paused=False)
                    enqueue_telegram(sess, chat_id, f"🟢 Reanudar todas: {len(profiles)} órdenes escalonadas creadas.", kb_global())
                elif action == "status":
                    lines = [profile_status_line(p) for p in profiles] or ["Sin perfiles"]
                    enqueue_telegram(sess, chat_id, "📊 <b>ESTADO GENERAL</b>\n" + "\n".join(lines), kb_global())
                elif action in ("pause", "resume", "bump", "last_bump", "menu_pause", "menu_resume", "menu_interval") and len(parts) >= 3:
                    p = sess.query(Profile).filter_by(id=int(parts[2])).first()
                    if p:
                        if action == "pause":
                            apply_desired(sess, p, paused=True); make_command(sess, p, "pause", {}, idem); sess.commit()
                            enqueue_telegram(sess, chat_id, f"🔴 <b>{esc(p.codigo)}</b> — PAUSADO", kb_admin_profile(p))
                        elif action == "resume":
                            apply_desired(sess, p, paused=False); make_command(sess, p, "resume", {}, idem); sess.commit()
                            enqueue_telegram(sess, chat_id, f"🟢 <b>{esc(p.codigo)}</b> — ACTIVO", kb_admin_profile(p))
                        elif action == "bump":
                            make_command(sess, p, "bump_now", {}, idem); sess.commit()
                            enqueue_telegram(sess, chat_id, f"⚡ Bump solicitado a <b>{esc(p.codigo)}</b>", kb_admin_profile(p))
                        elif action == "last_bump":
                            lb = p.last_bump_at_utc.astimezone(RD_TZ).strftime("%I:%M:%S %p") if p.last_bump_at_utc else "Sin registro"
                            enqueue_telegram(sess, chat_id, f"🕒 <b>{esc(p.codigo)}</b>\nÚltimo: {lb} (RD)\nBumps hoy: {p.bumps_today}", kb_admin_profile(p))
                        elif action == "menu_pause":
                            set_pending(sess, p, chat_id, user_id, "pause_at_rd")
                            kb = {"inline_keyboard": [[{"text": "11:00 PM", "callback_data": f"adm:set_pause:{p.id}:23:00"}, {"text": "12:00 AM", "callback_data": f"adm:set_pause:{p.id}:00:00"}, {"text": "03:00 AM", "callback_data": f"adm:set_pause:{p.id}:03:00"}], [{"text": "❌ Quitar", "callback_data": f"adm:set_pause:{p.id}:"}]]}
                            enqueue_telegram(sess, chat_id, f"⏰ Pausa (RD) para <b>{esc(p.codigo)}</b>: elige o escribe la hora (ej. 03:30 AM)", kb)
                        elif action == "menu_resume":
                            set_pending(sess, p, chat_id, user_id, "resume_at_rd")
                            kb = {"inline_keyboard": [[{"text": "07:00 AM", "callback_data": f"adm:set_resume:{p.id}:07:00"}, {"text": "08:00 AM", "callback_data": f"adm:set_resume:{p.id}:08:00"}, {"text": "10:00 AM", "callback_data": f"adm:set_resume:{p.id}:10:00"}], [{"text": "❌ Quitar", "callback_data": f"adm:set_resume:{p.id}:"}]]}
                            enqueue_telegram(sess, chat_id, f"⏰ Reanudar (RD) para <b>{esc(p.codigo)}</b>: elige o escribe la hora (ej. 08:30 AM)", kb)
                        elif action == "menu_interval":
                            set_pending(sess, p, chat_id, user_id, "interval_min")
                            kb = {"inline_keyboard": [[{"text": "15 min", "callback_data": f"adm:set_interval:{p.id}:15"}, {"text": "16 min", "callback_data": f"adm:set_interval:{p.id}:16"}, {"text": "20 min", "callback_data": f"adm:set_interval:{p.id}:20"}, {"text": "30 min", "callback_data": f"adm:set_interval:{p.id}:30"}]]}
                            enqueue_telegram(sess, chat_id, f"⏱️ Minutos entre bumps para <b>{esc(p.codigo)}</b>:", kb)
                elif action in ("set_pause", "set_resume", "set_interval") and len(parts) >= 3:
                    p = sess.query(Profile).filter_by(id=int(parts[2])).first()
                    if p:
                        val = ":".join(parts[3:]) if len(parts) > 3 else ""
                        if action == "set_pause": apply_desired(sess, p, pause_at_rd=val)
                        elif action == "set_resume": apply_desired(sess, p, resume_at_rd=val)
                        else: apply_desired(sess, p, interval_min=int(val or 16))
                        make_command(sess, p, "set_config", {"pause_at_rd": p.pause_at_rd, "resume_at_rd": p.resume_at_rd, "interval_min": p.interval_min}, idem); sess.commit()
                        enqueue_telegram(sess, chat_id, f"✅ <b>{esc(p.codigo)}</b> actualizado. Pausa {fmt_ampm(p.pause_at_rd)} | Reanuda {fmt_ampm(p.resume_at_rd)} | {p.interval_min} min", kb_admin_profile(p))
                return jsonify({"ok": True})
            # --- Renta (formato compatible v10) ---
            if data.startswith("renta_") or data.startswith("sop_"):
                try: pid = int(data.rsplit("_", 1)[-1])
                except Exception: return jsonify({"ok": True})
                p = sess.query(Profile).filter_by(id=pid, client_chat_id=chat_id, is_rental=True).first()
                if data.startswith("sop_"):
                    p2 = sess.query(Profile).filter_by(id=pid).first()
                    enqueue_telegram(sess, chat_id, "✅ Tu solicitud fue enviada al administrador.")
                    if p2 and ADMIN_CHAT_ID: enqueue_telegram(sess, ADMIN_CHAT_ID, f"🚨 <b>ALERTA DE RENTA</b>\nCliente: {esc(p2.client_name or chat_id)}\nPerfil: <b>{esc(p2.codigo)}</b>")
                    return jsonify({"ok": True})
                if not p: return jsonify({"ok": True})
                if p.rental_expired:
                    enqueue_telegram(sess, chat_id, "🔒 Tu renta venció. Escribe a soporte para renovar."); return jsonify({"ok": True})
                if data.startswith("renta_toggle_"):
                    apply_desired(sess, p, paused=not p.paused)
                    make_command(sess, p, "pause" if p.paused else "resume", {"reason": "rental"}, idem); sess.commit(); send_rental_menu(sess, chat_id, p)
                elif data.startswith("renta_dir_"):
                    if time.time() - float(p.last_location_edit_at or 0) < 86400:
                        left = 86400 - (time.time() - float(p.last_location_edit_at or 0))
                        enqueue_telegram(sess, chat_id, f"🔒 Solo puedes cambiar la ubicación una vez cada 24 h. Disponible en {int(left//3600)}h {int((left%3600)//60)}m.")
                    else:
                        set_pending(sess, p, chat_id, user_id, "ubicacion"); enqueue_telegram(sess, chat_id, "📍 Escribe la nueva dirección o zona:")
                elif data.startswith("renta_edit_"):
                    if time.time() - float(p.last_text_edit_at or 0) < 86400:
                        left = 86400 - (time.time() - float(p.last_text_edit_at or 0))
                        enqueue_telegram(sess, chat_id, f"🔒 Edición bloqueada por 24 h. Disponible en {int(left//3600)}h {int((left%3600)//60)}m.")
                    else:
                        set_pending(sess, p, chat_id, user_id, "texto"); enqueue_telegram(sess, chat_id, "✏️ Escribe el nuevo texto (luego se bloquea 24 h):")
                elif data.startswith("renta_help_"):
                    kb = {"inline_keyboard": [[{"text": "⚠️ Mi página no está subiendo", "callback_data": f"sop_nosube_{p.id}"}], [{"text": "➕ Quiero rentar otro anuncio", "callback_data": f"sop_nuevo_{p.id}"}]]}
                    enqueue_telegram(sess, chat_id, "🎧 <b>Centro de Soporte</b>\nSelecciona qué necesitas:", kb)
                return jsonify({"ok": True})
            return jsonify({"ok": True})
        # Mensajes de texto
        if "message" in update:
            msg = update["message"]; chat_id = str(msg.get("chat", {}).get("id", "")); user_id = str(msg.get("from", {}).get("id", ""))
            texto = (msg.get("text") or "").strip(); is_admin = bool(ADMIN_CHAT_ID and chat_id == str(ADMIN_CHAT_ID))
            if texto == "/id":
                enqueue_telegram(sess, chat_id, f"🆔 El ID de este grupo es:\n<code>{esc(chat_id)}</code>"); return jsonify({"ok": True})
            # Pendiente (admin horarios o renta dirección/texto)
            pend = sess.query(PendingInput).filter_by(chat_id=chat_id).filter(PendingInput.expires_at > time.time()).order_by(PendingInput.id.desc()).first()
            if pend and texto and not texto.startswith("/"):
                p = sess.query(Profile).filter_by(id=pend.profile_id).first()
                if p:
                    if pend.field in ("pause_at_rd", "resume_at_rd"):
                        hora = parse_hora(texto)
                        if hora:
                            apply_desired(sess, p, **{pend.field: hora}); make_command(sess, p, "set_config", {pend.field: hora}, f"tg:{uid}"); sess.commit()
                            sess.delete(pend); sess.commit(); enqueue_telegram(sess, chat_id, f"✅ <b>{esc(p.codigo)}</b> actualizado a {fmt_ampm(hora)} (RD)", kb_admin_profile(p)); return jsonify({"ok": True})
                    elif pend.field == "interval_min" and texto.isdigit():
                        apply_desired(sess, p, interval_min=int(texto)); make_command(sess, p, "set_config", {"interval_min": int(texto)}, f"tg:{uid}"); sess.commit()
                        sess.delete(pend); sess.commit(); enqueue_telegram(sess, chat_id, f"✅ Intervalo de <b>{esc(p.codigo)}</b>: {texto} min", kb_admin_profile(p)); return jsonify({"ok": True})
                    elif pend.field == "ubicacion" and p.client_chat_id == chat_id:
                        p.ubicacion = texto; p.last_location_edit_at = time.time(); sess.delete(pend); sess.commit()
                        enqueue_telegram(sess, chat_id, f"✅ Dirección actualizada a: <b>{esc(texto)}</b>"); send_rental_menu(sess, chat_id, p); return jsonify({"ok": True})
                    elif pend.field == "texto" and p.client_chat_id == chat_id:
                        p.texto = texto; p.last_text_edit_at = time.time(); sess.delete(pend); sess.commit()
                        enqueue_telegram(sess, chat_id, "✅ Texto actualizado. Candado de 24 h activado 🔒"); send_rental_menu(sess, chat_id, p); return jsonify({"ok": True})
            if is_admin:
                profiles = sess.query(Profile).order_by(Profile.id).all()
                if texto in ("📊 Ver Estados (🟢/🔴)", "/estado"):
                    enqueue_telegram(sess, chat_id, "📊 <b>ESTADO GENERAL</b>\n" + "\n".join(profile_status_line(p) for p in profiles), kb_global())
                elif texto == "📸 Foto de Todos":
                    fan_out(sess, profiles, "photo", {"reason": "admin"}, f"tg:{uid}"); enqueue_telegram(sess, chat_id, f"📸 Foto solicitada a {len(profiles)} perfiles (escalonada).", kb_global())
                elif texto == "⏸️ Pausar todas":
                    fan_out(sess, profiles, "pause", {"reason": "admin"}, f"tg:{uid}", paused=True); enqueue_telegram(sess, chat_id, f"🔴 Pausar todas: {len(profiles)} órdenes creadas.", kb_global())
                elif texto == "▶️ Reanudar todas":
                    fan_out(sess, profiles, "resume", {"reason": "admin"}, f"tg:{uid}", paused=False); enqueue_telegram(sess, chat_id, f"🟢 Reanudar todas: {len(profiles)} órdenes creadas.", kb_global())
                elif texto.split()[0].lower() in ("/panel", "/menu", "/start") if texto else False:
                    enqueue_telegram(sess, chat_id, "🎛️ <b>PANEL — SEBASTIÁN MEGA v11</b>\nUsa los botones o toca un perfil en una foto.", kb_global())
                return jsonify({"ok": True})
            # Renta
            p = sess.query(Profile).filter_by(client_chat_id=chat_id, is_rental=True).first()
            if p and texto.lower() in ("/menu", "menu", "/start"):
                send_rental_menu(sess, chat_id, p)
            return jsonify({"ok": True})
        return jsonify({"ok": True})
    finally:
        sess.close()

# ---------- Panel web (con login, cambios por POST) ----------
def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def w(*a, **k):
        if not session.get("admin"): return redirect(url_for("login"))
        return fn(*a, **k)
    return w
def csrf_token():
    if "csrf" not in session: session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]
def check_csrf():
    if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")): abort(403)

@app.route("/health")
def health(): return jsonify({"ok": True, "service": "sebastian-mega-v11", "rd": rd_now().isoformat()})

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if PANEL_PASSWORD and hmac.compare_digest(request.form.get("password", ""), PANEL_PASSWORD):
            session["admin"] = True; return redirect(url_for("index"))
        return render_page("login.html", error="Contraseña incorrecta"), 401
    return render_page("login.html", error="")

@app.route("/logout", methods=["POST"])
def logout(): session.clear(); return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    sess = db(); profiles = sess.query(Profile).order_by(Profile.id.desc()).all(); groups = sess.query(Group).all()
    new_key = session.pop("new_api_key", ""); new_codigo = session.pop("new_codigo", ""); sess.close()
    return render_page("index.html", profiles=profiles, groups=groups, csrf=csrf_token(), new_key=new_key, new_codigo=new_codigo, rd=rd_now().strftime("%I:%M %p"))

@app.route("/profiles", methods=["POST"])
@app.route("/agregar", methods=["POST"])
@login_required
def create_profile():
    check_csrf(); sess = db()
    api_key = secrets.token_urlsafe(32)
    vence_raw = request.form.get("rental_expires_at", "").strip()
    vence = None
    if vence_raw:
        try: vence = datetime.fromisoformat(vence_raw).replace(tzinfo=RD_TZ).astimezone(timezone.utc)
        except Exception: vence = None
    p = Profile(dolphin_id=request.form.get("dolphin_id", "").strip(), codigo=request.form.get("codigo", "").strip() or "Perfil",
                modelo=request.form.get("modelo", "").strip(), telefono=request.form.get("telefono", "").strip(),
                ubicacion=request.form.get("ubicacion", "").strip(), texto=request.form.get("texto", "").strip(),
                foto_url=request.form.get("foto_url", "").strip(), enlace_anuncio=request.form.get("enlace_anuncio", "").strip(),
                interval_min=int(request.form.get("interval_min") or 16), pause_at_rd=request.form.get("pause_at_rd", "").strip(),
                resume_at_rd=request.form.get("resume_at_rd", "").strip(), is_rental=(request.form.get("is_rental") or request.form.get("es_renta")) == "1",
                client_chat_id=(request.form.get("client_chat_id") or request.form.get("cliente_chat_id") or "").strip(), client_name=(request.form.get("client_name") or request.form.get("cliente_nombre") or "").strip(),
                rental_expires_at=vence, api_key_hash=hash_key(api_key), next_bump_at_utc=now_utc() + timedelta(minutes=16))
    if p.dolphin_id:
        sess.add(p)
        try: sess.commit(); session["new_api_key"] = api_key; session["new_codigo"] = p.codigo
        except Exception: sess.rollback()
    sess.close(); return redirect(url_for("index"))

@app.route("/profiles/<int:pid>/toggle", methods=["POST"])
@login_required
def toggle_profile(pid):
    check_csrf(); sess = db(); p = sess.query(Profile).filter_by(id=pid).first()
    if p:
        apply_desired(sess, p, paused=not p.paused)
        make_command(sess, p, "pause" if p.paused else "resume", {"reason": "panel"}, f"panel:{pid}:{int(time.time())}"); sess.commit()
    sess.close(); return redirect(url_for("index"))

@app.route("/profiles/<int:pid>/delete", methods=["POST"])
@login_required
def delete_profile(pid):
    check_csrf(); sess = db(); p = sess.query(Profile).filter_by(id=pid).first()
    if p: sess.query(Command).filter_by(profile_id=pid).delete(); sess.query(ProfileGroup).filter_by(profile_id=pid).delete(); sess.delete(p); sess.commit()
    sess.close(); return redirect(url_for("index"))

@app.route("/profiles/<int:pid>/edit", methods=["POST"])
@login_required
def edit_profile(pid):
    check_csrf(); sess = db(); p = sess.query(Profile).filter_by(id=pid).first()
    if p:
        p.ubicacion = request.form.get("ubicacion", p.ubicacion); p.texto = request.form.get("texto", p.texto)
        p.telefono = request.form.get("telefono", p.telefono); p.modelo = request.form.get("modelo", p.modelo)
        apply_desired(sess, p, interval_min=int(request.form.get("interval_min") or p.interval_min),
                      pause_at_rd=request.form.get("pause_at_rd", p.pause_at_rd), resume_at_rd=request.form.get("resume_at_rd", p.resume_at_rd))
        vence_raw = request.form.get("rental_expires_at", "").strip()
        if vence_raw:
            try: p.rental_expires_at = datetime.fromisoformat(vence_raw).replace(tzinfo=RD_TZ).astimezone(timezone.utc); p.rental_expired = False
            except Exception: pass
        p.is_rental = request.form.get("is_rental") == "1"; p.client_chat_id = request.form.get("client_chat_id", p.client_chat_id); p.client_name = request.form.get("client_name", p.client_name)
        make_command(sess, p, "set_config", {"pause_at_rd": p.pause_at_rd, "resume_at_rd": p.resume_at_rd, "interval_min": p.interval_min}, f"panel-edit:{pid}:{int(time.time())}"); sess.commit()
    sess.close(); return redirect(url_for("index"))

@app.route("/profiles/<int:pid>/regen-key", methods=["POST"])
@login_required
def regen_key(pid):
    check_csrf(); sess = db(); p = sess.query(Profile).filter_by(id=pid).first()
    if p:
        api_key = secrets.token_urlsafe(32); p.api_key_hash = hash_key(api_key); sess.commit()
        session["new_api_key"] = api_key; session["new_codigo"] = p.codigo
    sess.close(); return redirect(url_for("index"))

@app.route("/groups", methods=["POST"])
@login_required
def create_group():
    check_csrf(); sess = db(); nombre = request.form.get("nombre", "").strip()
    if nombre:
        try: sess.add(Group(nombre=nombre)); sess.commit()
        except Exception: sess.rollback()
    sess.close(); return redirect(url_for("index"))

@app.route("/internal/tick", methods=["POST"])
def internal_tick():
    if INTERNAL_SECRET and not hmac.compare_digest(request.headers.get("X-Internal-Secret", ""), INTERNAL_SECRET): abort(403)
    sess = db(); changed = tick(sess); sess.close(); return jsonify({"ok": True, "changed": changed})

# ---------- API extensión v11 ----------
def auth_profile(sess):
    dolphin_id = request.headers.get("X-Dolphin-Id") or (request.get_json(silent=True) or {}).get("dolphin_id") or request.form.get("dolphin_id") or ""
    key = request.headers.get("X-Profile-Key") or ""
    if not dolphin_id or not key: return None
    p = sess.query(Profile).filter_by(dolphin_id=str(dolphin_id)).first()
    if not p or not p.api_key_hash or not hmac.compare_digest(hash_key(key), p.api_key_hash): return None
    return p

def desired_payload(p):
    return {"codigo": p.codigo, "paused": bool(p.paused), "interval_min": p.interval_min, "pause_at_rd": p.pause_at_rd,
            "resume_at_rd": p.resume_at_rd, "next_bump_at_utc": p.next_bump_at_utc.isoformat() if p.next_bump_at_utc else None,
            "ubicacion": p.ubicacion, "texto": p.texto, "telefono": p.telefono, "is_rental": bool(p.is_rental), "rental_expired": bool(p.rental_expired)}

@app.route("/api/v1/heartbeat", methods=["POST"])
def api_heartbeat():
    sess = db(); p = auth_profile(sess)
    if not p: sess.close(); return jsonify({"ok": False, "error": "auth"}), 401
    tick(sess)
    p.last_seen_at = now_utc()
    cmds = sess.query(Command).filter_by(profile_id=p.id, status="pending").order_by(Command.id).limit(20).all()
    out = []
    for c in cmds:
        c.attempts += 1; out.append({"id": c.id, "type": c.type, "payload": json.loads(c.payload or "{}"), "idempotency_key": c.idempotency_key})
    sess.commit(); payload = {"ok": True, "desired": desired_payload(p), "commands": out, "server_rd": rd_now().isoformat()}
    sess.close(); return jsonify(payload)

@app.route("/api/v1/commands/<int:cid>/ack", methods=["POST"])
def api_ack(cid):
    sess = db(); p = auth_profile(sess)
    if not p: sess.close(); return jsonify({"ok": False}), 401
    c = sess.query(Command).filter_by(id=cid, profile_id=p.id).first()
    if not c: sess.close(); return jsonify({"ok": False}), 404
    body = request.get_json(silent=True) or {}
    c.status = "acked" if body.get("ok", True) else "failed"; c.acked_at = now_utc(); c.ack_detail = str(body.get("detail", ""))[:500]
    sess.commit(); sess.close(); return jsonify({"ok": True})

@app.route("/api/v1/set-paused", methods=["POST"])
def api_set_paused():
    sess = db(); p = auth_profile(sess)
    if not p: sess.close(); return jsonify({"ok": False}), 401
    body = request.get_json(silent=True) or {}; apply_desired(sess, p, paused=bool(body.get("paused"))); sess.commit()
    out = desired_payload(p); sess.close(); return jsonify({"ok": True, "desired": out})

@app.route("/api/v1/report-bump", methods=["POST"])
def api_report_bump():
    sess = db(); p = auth_profile(sess)
    if not p: sess.close(); return jsonify({"ok": False}), 401
    kind = request.form.get("kind", "bump")  # bump|photo
    photo_path = ""
    f = request.files.get("captura") or request.files.get("photo")
    if f and f.filename:
        ext = os.path.splitext(f.filename)[1][:10] or ".jpg"; photo_path = os.path.join(UPLOAD_DIR, f"{secrets.token_hex(12)}{ext}"); f.save(photo_path)
    if kind == "bump":
        today = rd_date()
        if p.bumps_date != today: p.bumps_date = today; p.bumps_today = 0
        p.bumps_today += 1; p.bumps_total += 1; p.last_bump_at_utc = now_utc(); p.last_seen_at = now_utc()
        p.next_bump_at_utc = now_utc() + timedelta(minutes=int(p.interval_min or 16))
    sess.commit()
    destino = p.client_chat_id if (p.is_rental and p.client_chat_id) else ADMIN_CHAT_ID
    hora = rd_now().strftime("%I:%M:%S %p")
    caption = (f"🔥 <b>{'BUMP CONFIRMADO' if kind=='bump' else 'FOTO'}</b>\n\n👤 <b>Modelo:</b> {esc(p.modelo or 'N/A')}\n🆔 <b>Código:</b> {esc(p.codigo)}\n"
               f"📞 <b>Teléfono:</b> {esc(p.telefono or 'N/A')}\n📍 <b>Ubicación:</b> {esc(p.ubicacion or 'N/A')}\n🟢 <b>Estado:</b> {'PAUSADA' if p.paused else 'ACTIVA'}\n"
               f"🕒 <b>Hora (RD):</b> {hora}\n📊 <b>Bumps hoy:</b> {p.bumps_today} | <b>Total:</b> {p.bumps_total}")
    kb = None if (p.is_rental and destino == p.client_chat_id) else kb_admin_profile(p)
    enqueue_telegram(sess, destino, text=caption, reply_markup=kb, photo_path=photo_path, caption=caption)
    sess.close(); return jsonify({"ok": True})

# Compatibilidad mínima con rutas v10 (sin auth -> ahora exigen clave)
@app.route("/api/perfil/<dolphin_id>", methods=["GET"])
def api_perfil_legacy(dolphin_id):
    sess = db(); request.environ["HTTP_X_DOLPHIN_ID"] = dolphin_id
    # reutiliza auth por header X-Profile-Key
    key = request.headers.get("X-Profile-Key", ""); p = sess.query(Profile).filter_by(dolphin_id=dolphin_id).first()
    if not p or not key or not hmac.compare_digest(hash_key(key), p.api_key_hash or ""): sess.close(); return jsonify({"ok": False, "error": "auth"}), 401
    out = {"ok": True, **desired_payload(p), "dolphin_id": p.dolphin_id, "modelo": p.modelo, "foto_url": p.foto_url, "estado": "PAUSADA" if p.paused else "ACTIVA"}
    sess.close(); return jsonify(out)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
