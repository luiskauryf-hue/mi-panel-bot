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
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # opcional: URL pública del servicio (para el botón de la mini app)

def public_base_url():
    if PUBLIC_URL: return PUBLIC_URL
    try:
        if request and request.host_url: return request.host_url.rstrip("/")
    except Exception: pass
    return "https://mi-panel-bot.onrender.com"
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
    payload = Column(Text, default="")

Base.metadata.create_all(engine)
# Migracion segura para bases ya creadas (Postgres/SQLite): agrega payload si falta.
try:
    from sqlalchemy import text as _sql_text
    with engine.begin() as _conn:
        _conn.execute(_sql_text("ALTER TABLE pending_inputs ADD COLUMN payload TEXT DEFAULT ''"))
except Exception:
    pass

app = Flask(__name__)

LOGIN_HTML = '<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Panel v11 — Acceso</title>\n<style>body{font-family:system-ui;background:#0f172a;color:#e2e8f0;display:grid;place-items:center;min-height:100vh;margin:0}.card{background:#1e293b;padding:24px;border-radius:12px;width:min(92vw,360px)}input,button{width:100%;padding:10px;border-radius:8px;border:1px solid #475569;margin-top:8px;box-sizing:border-box}button{background:#059669;color:#fff;border:0;font-weight:700}.err{color:#fda4af}</style></head>\n<body><form class="card" method="post"><h1>Panel SEBASTIÁN MEGA v11</h1>{% if error %}<p class="err">{{error}}</p>{% endif %}<label>Contraseña del panel<input type="password" name="password" required autofocus></label><button>Entrar</button></form></body></html>\n'
INDEX_HTML = '<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Panel v11</title>\n<style>body{font-family:system-ui;background:#020617;color:#e2e8f0;margin:0}.wrap{max-width:1100px;margin:auto;padding:16px}.card{background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:16px;margin:12px 0}input,select,textarea,button{padding:8px;border-radius:8px;border:1px solid #334155;background:#1e293b;color:#e2e8f0}button{background:#059669;color:#fff;border:0;font-weight:700;cursor:pointer}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}.warn{background:#451a03;border:1px solid #f59e0b;padding:12px;border-radius:10px;word-break:break-all}code{background:#1e293b;padding:2px 6px;border-radius:6px}</style></head>\n<body><div class="wrap"><h1>Panel SEBASTIÁN MEGA v11</h1><p>Hora RD: {{rd}} · Backend único · Cola por perfil</p>\n<form method="post" action="/logout"><button>Salir</button></form>\n{% if new_key %}<div class="warn"><b>Clave API nueva para {{new_codigo}} (cópiala ahora, no se vuelve a mostrar):</b><br><code>{{new_key}}</code><br>Pégala en el popup de la extensión v11 de ese perfil Dolphin.</div>{% endif %}\n<div class="card"><h2>+ Registrar perfil</h2><form method="post" action="/profiles"><input type="hidden" name="csrf" value="{{csrf}}"><div class="grid">\n<input name="dolphin_id" placeholder="ID Dolphin" required><input name="codigo" placeholder="Código (ej. Perfil-3)" required>\n<input name="modelo" placeholder="Modelo"><input name="telefono" placeholder="Teléfono"><input name="ubicacion" placeholder="Ubicación">\n<input name="foto_url" placeholder="URL foto"><input name="enlace_anuncio" placeholder="Enlace anuncio">\n<input name="interval_min" type="number" value="16" min="1"><label>Pausa RD <input name="pause_at_rd" type="time"></label><label>Reanuda RD <input name="resume_at_rd" type="time"></label>\n<select name="is_rental"><option value="0">Propio</option><option value="1">Renta</option></select>\n<input name="client_name" placeholder="Cliente (renta)"><input name="client_chat_id" placeholder="Chat ID cliente"><label>Vence renta <input name="rental_expires_at" type="datetime-local"></label>\n<textarea name="texto" placeholder="Texto del anuncio"></textarea></div><button>Guardar y generar clave</button></form></div>\n<div class="card"><h2>⏰ Horario para varios perfiles (RD)</h2><form method="post" action="/schedules/apply"><input type="hidden" name="csrf" value="{{csrf}}"><div class="grid"><label><input type="checkbox" name="all_profiles" value="1"> 🌐 Todos los perfiles</label>{% for p in profiles %}<label><input type="checkbox" name="profile_ids" value="{{p.id}}"> {{p.codigo}}</label>{% endfor %}<label>Apagado RD <input name="pause_at_rd" type="time"></label><label>Encendido RD <input name="resume_at_rd" type="time"></label><label><input type="checkbox" name="clear_schedule" value="1"> ❌ Quitar horario a los marcados</label></div><button>Aplicar horario a marcados</button></form></div><div class="card"><h2>Grupos</h2><form method="post" action="/groups"><input type="hidden" name="csrf" value="{{csrf}}"><input name="nombre" placeholder="Nombre del grupo" required><button>Crear grupo</button></form>\n<p>{% for g in groups %}<code>{{g.nombre}}</code> {% endfor %}</p></div>\n{% for p in profiles %}<div class="card"><h3>{{p.codigo}} <small>({{p.modelo or \'Sin nombre\'}})</small> — {{\'🔴 PAUSADA\' if p.paused else \'🟢 ACTIVA\'}}{% if p.is_rental %} · RENTA{% if p.rental_expired %} VENCIDA{% endif %}{% endif %}</h3>\n<p>Dolphin: <code>{{p.dolphin_id}}</code> · Tel: {{p.telefono or \'N/A\'}} · Ubicación: {{p.ubicacion or \'N/A\'}}<br>\nIntervalo: {{p.interval_min}} min · Pausa RD: {{p.pause_at_rd or \'—\'}} · Reanuda RD: {{p.resume_at_rd or \'—\'}}<br>\nÚltimo bump: {{p.last_bump_at_utc or \'Sin actividad\'}} · Bumps hoy: {{p.bumps_today}} · Total: {{p.bumps_total}} · Última señal: {{p.last_seen_at or \'nunca\'}}</p>\n<form method="post" action="/profiles/{{p.id}}/toggle" style="display:inline"><input type="hidden" name="csrf" value="{{csrf}}"><button>{{\'Reanudar\' if p.paused else \'Pausar\'}}</button></form>\n<form method="post" action="/profiles/{{p.id}}/photo" style="display:inline"><input type="hidden" name="csrf" value="{{csrf}}"><button>📸 Foto</button></form><form method="post" action="/profiles/{{p.id}}/regen-key" style="display:inline"><input type="hidden" name="csrf" value="{{csrf}}"><button>Regenerar clave API</button></form>\n<form method="post" action="/profiles/{{p.id}}/delete" style="display:inline" onsubmit="return confirm(\'¿Eliminar este perfil?\')"><input type="hidden" name="csrf" value="{{csrf}}"><button style="background:#be123c">Borrar</button></form>\n<details><summary>Editar</summary><form method="post" action="/profiles/{{p.id}}/edit"><input type="hidden" name="csrf" value="{{csrf}}"><div class="grid">\n<input name="modelo" value="{{p.modelo or \'\'}}" placeholder="Modelo"><input name="telefono" value="{{p.telefono or \'\'}}" placeholder="Teléfono">\n<input name="ubicacion" value="{{p.ubicacion or \'\'}}" placeholder="Ubicación"><input name="interval_min" type="number" value="{{p.interval_min}}">\n<label>Pausa RD <input name="pause_at_rd" type="time" value="{{p.pause_at_rd or \'\'}}"></label><label>Reanuda RD <input name="resume_at_rd" type="time" value="{{p.resume_at_rd or \'\'}}"></label>\n<select name="is_rental"><option value="0" {% if not p.is_rental %}selected{% endif %}>Propio</option><option value="1" {% if p.is_rental %}selected{% endif %}>Renta</option></select>\n<input name="client_name" value="{{p.client_name or \'\'}}" placeholder="Cliente"><input name="client_chat_id" value="{{p.client_chat_id or \'\'}}" placeholder="Chat ID cliente">\n<label>Vence <input name="rental_expires_at" type="datetime-local"></label><input name="foto_url" value="{{p.foto_url or \'\'}}" placeholder="URL foto de la modelo"><input name="enlace_anuncio" value="{{p.enlace_anuncio or \'\'}}" placeholder="Enlace del anuncio"><textarea name="texto">{{p.texto or \'\'}}</textarea></div><button>Guardar cambios</button></form></details></div>\n{% else %}<p>Sin perfiles todavía.</p>{% endfor %}\n</div></body></html>\n'

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
        if msg.kind == "photo" and not r.ok and r.status_code != 429:
            # Respaldo: si la foto falla, manda el texto para que no se pierda el reporte.
            try:
                payload = {"chat_id": msg.chat_id, "text": msg.text[:4096], "parse_mode": "HTML"}
                if msg.reply_markup: payload["reply_markup"] = json.loads(msg.reply_markup)
                r2 = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=TG_TIMEOUT)
                if r2.ok: return True, 0, "foto falló, texto enviado"
            except Exception: pass
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
        [{"text": "📸 Foto", "callback_data": f"adm:photo:{p.id}"}],
    ]}
def kb_global():
    return {"keyboard": [[{"text": "📊 Ver Estados (🟢/🔴)"}, {"text": "📸 Foto de Todos"}], [{"text": "⏸️ Pausar todas"}, {"text": "▶️ Reanudar todas"}], [{"text": "⏰ Horarios"}]], "resize_keyboard": True, "is_persistent": True}
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
        kb = {"inline_keyboard": [[{"text": "📋 Abrir mi anuncio", "web_app": {"url": public_base_url() + "/renta/app"}}], [{"text": "💚 Pagar servicio / Renovar", "callback_data": f"sop_pagar_{p.id}"}], [{"text": "🆘 Ayuda / Soporte", "callback_data": f"renta_help_{p.id}"}]]}
        enqueue_telegram(sess, chat_id, "🔒 Tu renta venció. Toca <b>Pagar servicio</b> para renovar o escribe a soporte.", kb); return
    now = time.time(); left = 86400 - (now - float(p.last_text_edit_at or 0))
    btn_edit = "✏️ Editar Texto (Disponible)" if left <= 0 else f"🔒 Editar Texto ({int(left//3600)}h {int((left%3600)//60)}m)"
    vence = p.rental_expires_at.astimezone(RD_TZ).strftime("%A %d/%m/%Y %I:%M %p") if p.rental_expires_at else "Consultar con soporte"
    faltan = ""
    if p.rental_expires_at:
        _exp = p.rental_expires_at if p.rental_expires_at.tzinfo else p.rental_expires_at.replace(tzinfo=timezone.utc)
        seg = int((_exp - now_utc()).total_seconds())
        if seg > 0: faltan = f" (Faltan {seg//86400}d {seg%86400//3600}h)"
    prox = p.next_bump_at_utc.astimezone(RD_TZ).strftime("%I:%M %p") if p.next_bump_at_utc else "—"
    txt = (f"📋 <b>CONTROL DE TU ANUNCIO ({esc(p.codigo)})</b>\n\n• <b>Estado:</b> {'🟢 ACTIVO' if not p.paused else '🔴 PAUSADO'}\n"
           f"• <b>Ubicación:</b> {esc(p.ubicacion or 'Sin definir')}\n• <b>⏳ Vence renta:</b> {esc(vence)}{faltan}\n"
           f"• <b>🔥 Próximo bump:</b> {prox} (RD) · <b>Bumps hoy:</b> {p.bumps_today}\n• <b>Fotos / Borrado:</b> 🔒 Bloqueados por seguridad\n\nToca un botón:")
    kb = {"inline_keyboard": [
        [{"text": "📋 Abrir mi anuncio", "web_app": {"url": public_base_url() + "/renta/app"}}],
        [{"text": "⏸️ Pausar Anuncio" if not p.paused else "▶️ Encender Anuncio", "callback_data": f"renta_toggle_{p.id}"}],
        [{"text": "📸 Ver foto de mi anuncio", "callback_data": f"renta_foto_{p.id}"}],
        [{"text": "📍 Cambiar Dirección", "callback_data": f"renta_dir_{p.id}"}],
        [{"text": btn_edit, "callback_data": f"renta_edit_{p.id}"}],
        [{"text": "💚 Pagar servicio / Renovar", "callback_data": f"sop_pagar_{p.id}"}],
        [{"text": "🆘 Ayuda / Soporte", "callback_data": f"renta_help_{p.id}"}]]}
    enqueue_telegram(sess, chat_id, txt, kb)

def set_pending(sess, p, chat_id, user_id, field):
    sess.query(PendingInput).filter_by(profile_id=p.id, chat_id=str(chat_id)).delete()
    sess.add(PendingInput(profile_id=p.id, chat_id=str(chat_id), user_id=str(user_id or ""), field=field, expires_at=time.time()+600)); sess.commit()

# ---------- Horarios flexibles: uno / varios marcados / todos ----------
def _pend_payload(pend):
    try: return json.loads(pend.payload or "{}")
    except Exception: return {}

def _set_sched_pending(sess, chat_id, user_id, field, payload, anchor_profile_id):
    sess.query(PendingInput).filter_by(chat_id=str(chat_id)).filter(PendingInput.field.like("sched%")).delete()
    pend = PendingInput(profile_id=int(anchor_profile_id), chat_id=str(chat_id), user_id=str(user_id or ""), field=field, expires_at=time.time()+600, payload=json.dumps(payload))
    sess.add(pend); sess.commit(); return pend

def _sched_get_pending(sess, chat_id):
    return sess.query(PendingInput).filter_by(chat_id=str(chat_id)).filter(PendingInput.field.like("sched%")).filter(PendingInput.expires_at > time.time()).order_by(PendingInput.id.desc()).first()

def _sched_toggle_kb(profiles, marked):
    rows = []
    for pr in profiles:
        mark = "✅" if pr.id in marked else "⬜"
        rows.append([{"text": f"{mark} {pr.codigo}", "callback_data": f"adm:sched_toggle:{pr.id}"}])
    rows.append([{"text": "✅ Aplicar a marcados", "callback_data": "adm:sched_apply"}, {"text": "❌ Cancelar", "callback_data": "adm:sched_cancel"}])
    return {"inline_keyboard": rows}

def _sched_ask_pause(sess, chat_id, user_id, ids, profiles_by_id):
    names = ", ".join(profiles_by_id[i].codigo for i in ids if i in profiles_by_id)
    _set_sched_pending(sess, chat_id, user_id, "sched_pause", {"ids": ids}, ids[0])
    kb = {"inline_keyboard": [[{"text": "11:00 PM", "callback_data": "adm:sched_set_pause:23:00"}, {"text": "12:00 AM", "callback_data": "adm:sched_set_pause:00:00"}, {"text": "03:00 AM", "callback_data": "adm:sched_set_pause:03:00"}], [{"text": "❌ Sin apagado", "callback_data": "adm:sched_set_pause:"}], [{"text": "❌ Cancelar", "callback_data": "adm:sched_cancel"}]]}
    enqueue_telegram(sess, chat_id, f"⏰ <b>Hora de apagado (RD)</b> para: {esc(names)}\nElige o escribe la hora (ej. 03:30 AM)", kb)

def _sched_ask_resume(sess, chat_id, user_id, payload):
    ids = payload.get("ids", [])
    _set_sched_pending(sess, chat_id, user_id, "sched_resume", payload, ids[0] if ids else 0)
    kb = {"inline_keyboard": [[{"text": "07:00 AM", "callback_data": "adm:sched_set_resume:07:00"}, {"text": "08:00 AM", "callback_data": "adm:sched_set_resume:08:00"}, {"text": "10:00 AM", "callback_data": "adm:sched_set_resume:10:00"}], [{"text": "❌ Sin encendido", "callback_data": "adm:sched_set_resume:"}], [{"text": "❌ Cancelar", "callback_data": "adm:sched_cancel"}]]}
    enqueue_telegram(sess, chat_id, "⏰ <b>Hora de encendido (RD)</b>\nElige o escribe la hora (ej. 08:30 AM)", kb)

def _sched_finish(sess, chat_id, payload, resume_val, idem):
    ids = payload.get("ids", []); pause_val = payload.get("pause", "")
    done = []
    for pid in ids:
        pr = sess.query(Profile).filter_by(id=int(pid)).first()
        if not pr: continue
        apply_desired(sess, pr, pause_at_rd=pause_val, resume_at_rd=resume_val)
        make_command(sess, pr, "set_config", {"pause_at_rd": pr.pause_at_rd, "resume_at_rd": pr.resume_at_rd, "interval_min": pr.interval_min}, idem)
        done.append(pr.codigo)
    sess.query(PendingInput).filter_by(chat_id=str(chat_id)).filter(PendingInput.field.like("sched%")).delete()
    sess.commit()
    enqueue_telegram(sess, chat_id, f"✅ Horario aplicado a {len(done)} perfil(es): {esc(', '.join(done))}\n⏸️ Apagado: {fmt_ampm(pause_val)} | ▶️ Encendido: {fmt_ampm(resume_val)} (RD)", kb_global())

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
                elif action == "photo" and len(parts) >= 3:
                    p = sess.query(Profile).filter_by(id=int(parts[2])).first()
                    if p:
                        make_command(sess, p, "photo", {"reason": "admin"}, idem); sess.commit()
                        enqueue_telegram(sess, chat_id, f"📸 Foto solicitada a <b>{esc(p.codigo)}</b>", kb_admin_profile(p))
                elif action == "sched_menu":
                    kb = {"inline_keyboard": [[{"text": "👤 Un perfil", "callback_data": "adm:sched_one_menu"}], [{"text": "👥 Varios perfiles", "callback_data": "adm:sched_multi_start"}], [{"text": "🌐 Todos", "callback_data": "adm:sched_all_start"}], [{"text": "❌ Cancelar", "callback_data": "adm:sched_cancel"}]]}
                    enqueue_telegram(sess, chat_id, "⏰ <b>Horarios (RD)</b>\n¿A quiénes quieres aplicar el horario?", kb)
                elif action == "sched_one_menu":
                    if not profiles: enqueue_telegram(sess, chat_id, "Sin perfiles todavía.", kb_global())
                    else:
                        kb = {"inline_keyboard": [[{"text": f"{pr.codigo} (⏸️ {fmt_ampm(pr.pause_at_rd)} | ▶️ {fmt_ampm(pr.resume_at_rd)})", "callback_data": f"adm:sched_one:{pr.id}"}] for pr in profiles]}
                        enqueue_telegram(sess, chat_id, "👤 Elige el perfil:", kb)
                elif action == "sched_one" and len(parts) >= 3:
                    p = sess.query(Profile).filter_by(id=int(parts[2])).first()
                    if p:
                        kb = {"inline_keyboard": [[{"text": "⏸️ Hora de apagado", "callback_data": f"adm:menu_pause:{p.id}"}, {"text": "▶️ Hora de encendido", "callback_data": f"adm:menu_resume:{p.id}"}]]}
                        enqueue_telegram(sess, chat_id, f"⏰ <b>{esc(p.codigo)}</b> — Apagado {fmt_ampm(p.pause_at_rd)} | Encendido {fmt_ampm(p.resume_at_rd)} (RD)", kb)
                elif action == "sched_multi_start":
                    if not profiles: enqueue_telegram(sess, chat_id, "Sin perfiles todavía.", kb_global())
                    else:
                        _set_sched_pending(sess, chat_id, user_id, "sched_select", {"ids": []}, profiles[0].id)
                        enqueue_telegram(sess, chat_id, "👥 Marca los perfiles (toca para ✅/⬜) y luego <b>Aplicar a marcados</b>:", _sched_toggle_kb(profiles, []))
                elif action == "sched_toggle" and len(parts) >= 3:
                    pend = _sched_get_pending(sess, chat_id)
                    if pend:
                        payload = _pend_payload(pend); ids = payload.get("ids", []); pid = int(parts[2])
                        ids = [i for i in ids if i != pid] if pid in ids else ids + [pid]
                        pend.payload = json.dumps({"ids": ids}); pend.expires_at = time.time()+600; sess.commit()
                        enqueue_telegram(sess, chat_id, f"👥 Marcados: {len(ids)}. Toca para cambiar y luego <b>Aplicar a marcados</b>:", _sched_toggle_kb(profiles, ids))
                elif action == "sched_apply":
                    pend = _sched_get_pending(sess, chat_id)
                    ids = _pend_payload(pend).get("ids", []) if pend else []
                    if not ids: enqueue_telegram(sess, chat_id, "⚠️ Marca al menos un perfil primero.", _sched_toggle_kb(profiles, []))
                    else: _sched_ask_pause(sess, chat_id, user_id, ids, {pr.id: pr for pr in profiles})
                elif action == "sched_all_start":
                    if not profiles: enqueue_telegram(sess, chat_id, "Sin perfiles todavía.", kb_global())
                    else: _sched_ask_pause(sess, chat_id, user_id, [pr.id for pr in profiles], {pr.id: pr for pr in profiles})
                elif action == "sched_set_pause":
                    pend = _sched_get_pending(sess, chat_id)
                    if pend:
                        payload = _pend_payload(pend); payload["pause"] = ":".join(parts[2:]) if len(parts) > 2 else ""
                        _sched_ask_resume(sess, chat_id, user_id, payload)
                elif action == "sched_set_resume":
                    pend = _sched_get_pending(sess, chat_id)
                    if pend: _sched_finish(sess, chat_id, _pend_payload(pend), ":".join(parts[2:]) if len(parts) > 2 else "", idem)
                elif action == "sched_cancel":
                    sess.query(PendingInput).filter_by(chat_id=str(chat_id)).filter(PendingInput.field.like("sched%")).delete(); sess.commit()
                    enqueue_telegram(sess, chat_id, "❌ Horarios cancelado.", kb_global())
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
                    if data.startswith("sop_estafa_"):
                        enqueue_telegram(sess, chat_id, "🚨 <b>Cuidado con mensajes falsos</b>\nSi te mandan un enlace raro diciendo que es de MegaPersonals o de soporte, <b>es una estafa</b>. No pagues ni des tu clave fuera de este chat. Ante la duda, escríbenos por Ayuda.")
                        return jsonify({"ok": True})
                    motivos = {"sop_nosube_": "Mi página no está subiendo", "sop_bloqueo_": "Creo que mi post se bloqueó", "sop_editar_": "Problema editando mi página", "sop_consejos_": "Consejos para vender más", "sop_nuevo_": "Quiero rentar un anuncio nuevo más", "sop_pagar_": "Quiere pagar / renovar servicio", "sop_direccion_": "Quiere cambiar su dirección"}
                    motivo = next((v for k, v in motivos.items() if data.startswith(k)), "Solicitud de soporte")
                    enqueue_telegram(sess, chat_id, "✅ Tu solicitud fue enviada al administrador.")
                    if p2 and ADMIN_CHAT_ID: enqueue_telegram(sess, ADMIN_CHAT_ID, f"🚨 <b>ALERTA DE RENTA</b>\nMotivo: {esc(motivo)}\nCliente: {esc(p2.client_name or chat_id)}\nPerfil: <b>{esc(p2.codigo)}</b>")
                    return jsonify({"ok": True})
                if not p: return jsonify({"ok": True})
                if p.rental_expired:
                    enqueue_telegram(sess, chat_id, "🔒 Tu renta venció. Escribe a soporte para renovar."); return jsonify({"ok": True})
                if data.startswith("renta_toggle_"):
                    apply_desired(sess, p, paused=not p.paused)
                    make_command(sess, p, "pause" if p.paused else "resume", {"reason": "rental"}, idem); sess.commit(); send_rental_menu(sess, chat_id, p)
                elif data.startswith("renta_dir_"):
                    # Regla de Luis: la cliente no cambia la dirección; la hace el admin desde el panel.
                    enqueue_telegram(sess, chat_id, "📍 El cambio de dirección lo hace soporte por ti. Ya avisamos al administrador y te atiende en este chat.")
                    if ADMIN_CHAT_ID: enqueue_telegram(sess, ADMIN_CHAT_ID, f"🚨 <b>ALERTA DE RENTA</b>\nMotivo: Quiere cambiar su dirección\nCliente: {esc(p.client_name or chat_id)}\nPerfil: <b>{esc(p.codigo)}</b>")
                elif data.startswith("renta_edit_"):
                    if time.time() - float(p.last_text_edit_at or 0) < 86400:
                        left = 86400 - (time.time() - float(p.last_text_edit_at or 0))
                        enqueue_telegram(sess, chat_id, f"🔒 Edición bloqueada por 24 h. Disponible en {int(left//3600)}h {int((left%3600)//60)}m.")
                    else:
                        set_pending(sess, p, chat_id, user_id, "texto"); enqueue_telegram(sess, chat_id, "✏️ Escribe el nuevo texto (luego se bloquea 24 h):")
                elif data.startswith("renta_foto_"):
                    make_command(sess, p, "photo", {"reason": "rental"}, idem); sess.commit()
                    enqueue_telegram(sess, chat_id, "📸 Foto solicitada. En 1-2 minutos te llega la captura de tu anuncio.")
                elif data.startswith("renta_help_"):
                    kb = {"inline_keyboard": [[{"text": "💡 Consejos para vender más", "callback_data": f"sop_consejos_{p.id}"}], [{"text": "🚨 Cuidado con mensajes falsos", "callback_data": f"sop_estafa_{p.id}"}], [{"text": "🚫 Mi página no está subiendo", "callback_data": f"sop_nosube_{p.id}"}], [{"text": "🔒 Creo que mi post se bloqueó", "callback_data": f"sop_bloqueo_{p.id}"}], [{"text": "✏️ Problema editando mi página", "callback_data": f"sop_editar_{p.id}"}], [{"text": "➕ Quiero rentar un anuncio nuevo más", "callback_data": f"sop_nuevo_{p.id}"}], [{"text": "📍 Quiero cambiar mi dirección", "callback_data": f"sop_direccion_{p.id}"}], [{"text": "💚 Pagar servicio / Renovar", "callback_data": f"sop_pagar_{p.id}"}]]}
                    enqueue_telegram(sess, chat_id, "🎧 <b>Centro de Soporte</b>\n¿En qué podemos ayudarte?", kb)
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
            if pend and texto and not texto.startswith("/") and pend.field in ("sched_pause", "sched_resume"):
                hora = parse_hora(texto)
                if hora:
                    payload = _pend_payload(pend)
                    if pend.field == "sched_pause":
                        payload["pause"] = hora; _sched_ask_resume(sess, chat_id, user_id, payload)
                    else:
                        _sched_finish(sess, chat_id, payload, hora, f"tg:{uid}")
                    return jsonify({"ok": True})
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
                        # Regla de Luis: la dirección ya no se guarda desde el chat del cliente.
                        sess.delete(pend); sess.commit()
                        enqueue_telegram(sess, chat_id, "📍 El cambio de dirección lo hace soporte por ti. Escríbenos por 🆘 Ayuda / Soporte."); return jsonify({"ok": True})
                    elif pend.field == "texto" and p.client_chat_id == chat_id:
                        p.texto = texto; p.last_text_edit_at = time.time(); sess.delete(pend); sess.commit()
                        enqueue_telegram(sess, chat_id, "✅ Texto actualizado. Candado de 24 h activado 🔒"); send_rental_menu(sess, chat_id, p); return jsonify({"ok": True})
            if is_admin:
                profiles = sess.query(Profile).order_by(Profile.id).all()
                if texto == "⏰ Horarios":
                    kb = {"inline_keyboard": [[{"text": "👤 Un perfil", "callback_data": "adm:sched_one_menu"}], [{"text": "👥 Varios perfiles", "callback_data": "adm:sched_multi_start"}], [{"text": "🌐 Todos", "callback_data": "adm:sched_all_start"}], [{"text": "❌ Cancelar", "callback_data": "adm:sched_cancel"}]]}
                    enqueue_telegram(sess, chat_id, "⏰ <b>Horarios (RD)</b>\n¿A quiénes quieres aplicar el horario?", kb)
                elif texto in ("📊 Ver Estados (🟢/🔴)", "/estado"):
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

@app.route("/schedules/apply", methods=["POST"])
@login_required
def apply_schedule():
    check_csrf(); sess = db()
    if request.form.get("all_profiles") == "1":
        targets = sess.query(Profile).all()
    else:
        ids = [int(x) for x in request.form.getlist("profile_ids") if str(x).isdigit()]
        targets = sess.query(Profile).filter(Profile.id.in_(ids)).all() if ids else []
    clear = request.form.get("clear_schedule") == "1"
    pause_val = "" if clear else request.form.get("pause_at_rd", "").strip()
    resume_val = "" if clear else request.form.get("resume_at_rd", "").strip()
    for pr in targets:
        apply_desired(sess, pr, pause_at_rd=pause_val, resume_at_rd=resume_val)
        make_command(sess, pr, "set_config", {"pause_at_rd": pr.pause_at_rd, "resume_at_rd": pr.resume_at_rd, "interval_min": pr.interval_min}, f"panel:{int(time.time())}")
    sess.commit(); sess.close(); return redirect(url_for("index"))

@app.route("/profiles/<int:pid>/photo", methods=["POST"])
@login_required
def photo_profile(pid):
    check_csrf(); sess = db(); p = sess.query(Profile).filter_by(id=pid).first()
    if p: make_command(sess, p, "photo", {"reason": "panel"}, f"panel:{int(time.time())}"); sess.commit()
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
        if "foto_url" in request.form: p.foto_url = request.form.get("foto_url", "").strip()
        if "enlace_anuncio" in request.form: p.enlace_anuncio = request.form.get("enlace_anuncio", "").strip()
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

def _download_photo(url):
    try:
        r = requests.get(url, timeout=10, stream=True, headers={"User-Agent": "Mozilla/5.0"})
        ctype = (r.headers.get("Content-Type") or "").lower()
        if r.ok and ("image" in ctype or url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp"))):
            data = r.raw.read(10 * 1024 * 1024 + 1, decode_content=True)
            if 0 < len(data) <= 10 * 1024 * 1024:
                ext = ".png" if "png" in ctype else ".jpg"
                path = os.path.join(UPLOAD_DIR, f"{secrets.token_hex(12)}{ext}")
                with open(path, "wb") as fh: fh.write(data)
                return path
    except Exception:
        pass
    return ""

def _photo_from_ad_page(page_url):
    # Busca la foto principal del anuncio (og:image) en la página pública del anuncio.
    try:
        r = requests.get(page_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if not r.ok: return ""
        html = r.text[:2 * 1024 * 1024]
        import re as _re
        m = _re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', html) or _re.search(r'content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']', html)
        if m:
            from urllib.parse import urljoin
            return _download_photo(urljoin(page_url, m.group(1)))
    except Exception:
        pass
    return ""

@app.route("/api/v1/report-bump", methods=["POST"])
def api_report_bump():
    sess = db(); p = auth_profile(sess)
    if not p: sess.close(); return jsonify({"ok": False}), 401
    kind = request.form.get("kind", "bump")  # bump|photo
    if kind == "bump" and p.last_bump_at_utc is not None:
        _lb = p.last_bump_at_utc if p.last_bump_at_utc.tzinfo else p.last_bump_at_utc.replace(tzinfo=timezone.utc)
        if (now_utc() - _lb).total_seconds() < 120:
            sess.close(); return jsonify({"ok": True, "dedup": True})  # reporte repetido: no duplicar
    photo_path = ""
    f = request.files.get("captura") or request.files.get("photo")
    uploaded_path = ""
    if f and f.filename:
        ext = os.path.splitext(f.filename)[1][:10] or ".jpg"; uploaded_path = os.path.join(UPLOAD_DIR, f"{secrets.token_hex(12)}{ext}"); f.save(uploaded_path)
    # Foto limpia del anuncio (como v10), en orden: URL que manda la extensión ->
    # URL foto guardada en el perfil (panel) -> foto principal de la página del anuncio.
    for _cand in [(request.form.get("foto_url") or "").strip(), (p.foto_url or "").strip()]:
        if _cand.startswith(("http://", "https://")):
            photo_path = _download_photo(_cand)
            if photo_path: break
    if not photo_path and (p.enlace_anuncio or "").strip().startswith(("http://", "https://")):
        photo_path = _photo_from_ad_page(p.enlace_anuncio.strip())
    if not photo_path:
        photo_path = uploaded_path
    if kind == "bump":
        today = rd_date()
        if p.bumps_date != today: p.bumps_date = today; p.bumps_today = 0
        p.bumps_today += 1; p.bumps_total += 1; p.last_bump_at_utc = now_utc(); p.last_seen_at = now_utc()
        p.next_bump_at_utc = now_utc() + timedelta(minutes=int(p.interval_min or 16))
    sess.commit()
    destino = p.client_chat_id if (p.is_rental and p.client_chat_id) else ADMIN_CHAT_ID
    hora = rd_now().strftime("%I:%M:%S %p")
    prox_txt = "Detenido" if p.paused else (p.next_bump_at_utc.astimezone(RD_TZ).strftime("%I:%M %p") if p.next_bump_at_utc else "—")
    caption = (f"📸 <b>{'Bump exitoso' if kind=='bump' else 'Foto de tu anuncio'}</b>\n\n📞 <b>Número:</b> {esc(p.telefono or 'N/A')}\n🆔 <b>Código:</b> {esc(p.codigo)}\n"
               f"📍 <b>Ciudad:</b> {esc(p.ubicacion or 'N/A')}\n{'🔴' if p.paused else '🟢'} <b>Estado:</b> {'PAUSADA' if p.paused else 'ACTIVA'}\n"
               f"⏰ <b>Horario (RD):</b> Pausa {fmt_ampm(p.pause_at_rd)} | Reanuda {fmt_ampm(p.resume_at_rd)}\n"
               f"🕒 <b>Hora (RD):</b> {hora} | <b>Próx:</b> {prox_txt}\n📊 <b>Bumps hoy:</b> {p.bumps_today} | <b>Total:</b> {p.bumps_total}")
    kb = None if (p.is_rental and destino == p.client_chat_id) else kb_admin_profile(p)
    if p.is_rental and destino == p.client_chat_id:
        vence = p.rental_expires_at.astimezone(RD_TZ).strftime("%d/%m/%Y") if p.rental_expires_at else "Consultar"
        prox = p.next_bump_at_utc.astimezone(RD_TZ).strftime("%I:%M %p") if p.next_bump_at_utc else "—"
        caption += f"\n⏳ <b>Vence renta:</b> {vence}\n🔥 <b>Próximo bump:</b> {prox} (RD)"
    if kind == "photo" and not photo_path:
        caption += "\n📷 Sin imagen: no se pudo obtener la foto del anuncio ni capturar la pantalla."
    # Diagnóstico: si la foto que se manda es la captura (no la limpia), mostrar qué imágenes vio la extensión.
    _dbg = (request.form.get("debug_imgs") or "").strip()
    if _dbg and not (request.form.get("foto_url") or "").strip():
        caption += f"\n🔎 Imágenes en la página: {esc(_dbg[:350])}"
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

# ---------- Mini App de renta (Web App de Telegram, estética MegaPersonals) ----------
def validate_init_data(init_data):
    """Valida el initData de Telegram WebApp con HMAC-SHA256 (doc oficial de Telegram).
    Devuelve {"user": {...}, "auth_date": int} o None si no es válido."""
    if not init_data or not TELEGRAM_BOT_TOKEN: return None
    try:
        from urllib.parse import parse_qsl
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        recv_hash = pairs.pop("hash", "")
        if not recv_hash: return None
        dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash): return None
        auth_date = int(pairs.get("auth_date", "0") or 0)
        if auth_date and time.time() - auth_date > 7 * 86400: return None
        user = json.loads(pairs.get("user", "{}") or "{}")
        if not user.get("id"): return None
        return {"user": user, "auth_date": auth_date}
    except Exception:
        return None

def rental_from_init_data(sess):
    """Autentica al cliente por initData y devuelve (perfil, None) o (None, respuesta_error).
    Aislamiento: el perfil se busca SOLO por el user.id firmado por Telegram; nunca se
    acepta un profile_id enviado por el cliente."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        body = request.get_json(silent=True) or {}
        init_data = body.get("init_data", "") or ""
    data = validate_init_data(init_data)
    if not data:
        return None, (jsonify({"ok": False, "error": "sin_autorizacion", "message": "Ábrelo desde el bot de Telegram."}), 401)
    p = sess.query(Profile).filter_by(client_chat_id=str(data["user"]["id"]), is_rental=True).first()
    if not p:
        return None, (jsonify({"ok": False, "error": "sin_perfil", "message": "No tienes un anuncio de renta activo. Escribe a soporte."}), 403)
    return p, None

def rental_status_payload(p):
    now = time.time()
    def lock_left(ts): return max(0.0, 86400 - (now - float(ts or 0)))
    exp = p.rental_expires_at
    if exp is not None and exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
    vence = exp.astimezone(RD_TZ).strftime("%A %d/%m/%Y %I:%M %p") if exp else "Consultar con soporte"
    vence_corto = exp.astimezone(RD_TZ).strftime("%d/%m") if exp else ""
    faltan = ""
    if exp:
        seg = int((exp - now_utc()).total_seconds())
        if seg > 0: faltan = f"{seg//86400}d {seg%86400//3600}h"
    prox = p.next_bump_at_utc.astimezone(RD_TZ).strftime("%I:%M %p") if (p.next_bump_at_utc and not p.paused) else "—"
    lt = lock_left(p.last_text_edit_at)
    return {"ok": True, "codigo": p.codigo,
            "estado": "VENCIDO" if p.rental_expired else ("PAUSADO" if p.paused else "ACTIVO"),
            "paused": bool(p.paused), "rental_expired": bool(p.rental_expired),
            "ubicacion": p.ubicacion or "", "texto": p.texto or "",
            "vence": vence, "vence_corto": vence_corto, "faltan": faltan,
            "proximo_bump": prox, "bumps_hoy": p.bumps_today or 0,
            "foto_url": p.foto_url or "",
            "edit_texto_disponible": lt <= 0, "edit_texto_faltan_h": round(lt / 3600, 1) if lt > 0 else 0}

def _renta_expired_block(p):
    if p.rental_expired:
        return jsonify({"ok": False, "error": "renta_vencida", "message": "🔒 Tu renta venció. Toca PAGAR SERVICIO para renovar."}), 403
    return None

def _renta_admin_alert(sess, p, motivo):
    if ADMIN_CHAT_ID:
        enqueue_telegram(sess, ADMIN_CHAT_ID, f"🚨 <b>ALERTA DE RENTA</b>\nMotivo: {esc(motivo)}\nCliente: {esc(p.client_name or p.client_chat_id)}\nPerfil: <b>{esc(p.codigo)}</b>")

RENTA_APP_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mi anuncio — MegaPersonals</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box}body{margin:0;font-family:Arial,Helvetica,sans-serif;background:#fdf2f8}
.wrap{max-width:420px;margin:0 auto;background:#fdf2f8;min-height:100vh}
.head{background:linear-gradient(135deg,#ec4899,#a855f7);color:#fff;text-align:center;padding:14px 8px;font-size:24px;font-weight:900;letter-spacing:1px;text-shadow:1px 1px 2px rgba(0,0,0,.35)}
.body{padding:12px}
.row{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.cur{font-weight:900;color:#9d174d;font-size:16px}
.badge{background:#f59e0b;color:#fff;font-weight:900;font-size:13px;padding:5px 12px;border-radius:999px}
.foto{background:linear-gradient(160deg,#fbcfe8,#e9d5ff);border:2px dashed #ec4899;border-radius:12px;min-height:130px;display:flex;align-items:center;justify-content:center;color:#9d174d;font-weight:700;font-size:15px;text-align:center;overflow:hidden}
.foto img{width:100%;display:block}
.estado{text-align:center;color:#831843;font-size:14px;margin:8px 0;font-weight:700}
.bumpbox{background:#fff;border:1px solid #f9a8d4;border-radius:12px;text-align:center;padding:10px;margin:10px 0;color:#9d174d}
.b1{font-size:13px;font-weight:700}.b2{font-size:28px;font-weight:900}.b3{font-size:13px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.btn{border:none;border-radius:10px;padding:16px 4px;font-size:16px;font-weight:900;color:#fff;cursor:pointer;width:100%}
.btn:disabled{opacity:.45}
.red{background:#dc2626}.blue{background:#2563eb}.orange{background:#ea580c}.pink{background:#db2777}.green{background:#16a34a}.purple{background:#7c3aed}
.wide{margin-top:8px}
.foto-btn{background:#fff;color:#db2777;border:2px solid #db2777}
.row2{display:flex;gap:8px;margin-top:8px}.row2 .btn{flex:1;padding:13px 4px;font-size:14px}
.note{text-align:center;color:#9d174d;font-size:12px;margin-top:10px}
.panel{background:#fff;border:1px solid #f9a8d4;border-radius:12px;padding:12px;margin-top:12px}
.panel h3{margin:0 0 8px;color:#9d174d}
.panel label{display:block;font-size:13px;font-weight:700;color:#831843;margin-top:10px}
.panel textarea,.panel input{width:100%;padding:10px;border:1px solid #f9a8d4;border-radius:8px;font-size:15px;margin-top:4px}
.sup{background:#fdf2f8;color:#9d174d;border:1px solid #f9a8d4;padding:12px 8px;font-size:14px;margin-top:8px;text-align:left}
.msg{border-radius:10px;padding:12px;margin-top:12px;font-size:15px;text-align:center;font-weight:700}
.msg.ok{background:#dcfce7;color:#166534}.msg.err{background:#fee2e2;color:#991b1b}
.vencido{background:#fee2e2;color:#991b1b;border-radius:10px;padding:12px;text-align:center;font-weight:700;margin-bottom:10px}
.sinbot{max-width:420px;margin:40px auto;background:#fff;border:2px solid #ec4899;border-radius:16px;padding:24px;text-align:center;color:#9d174d;font-size:18px;font-weight:700}
.sinbot small{display:block;margin-top:8px;font-size:14px;font-weight:400}
</style></head>
<body>
<div id="sinbot" class="sinbot" style="display:none">📲 Ábrelo desde el bot de Telegram<small>Abre tu chat con el bot y toca «📋 Abrir mi anuncio».</small></div>
<div id="app" style="display:none"><div class="wrap">
  <div class="head">MegaPersonals</div>
  <div class="body">
    <div id="vencido" class="vencido" style="display:none">🔒 Tu renta venció. Toca <b>PAGAR SERVICIO</b> para renovar.</div>
    <div class="row"><span class="cur">CURRENT POST</span><span class="badge" id="venceBadge">VENCE</span></div>
    <div class="foto" id="fotoBox"><span id="fotoTxt">Foto de tu anuncio</span><img id="fotoImg" style="display:none" alt=""></div>
    <div class="estado" id="estadoTxt"></div>
    <div class="bumpbox"><div class="b1">PRÓXIMO BUMP</div><div class="b2" id="proxBump">—</div><div class="b3" id="bumpsHoy"></div></div>
    <div class="grid">
      <button class="btn red" id="btnEdit">EDIT POST</button>
      <button class="btn blue" id="btnNew">🔒 WRITE NEW</button>
      <button class="btn orange" id="btnRemove">🔒 REMOVE POST</button>
      <button class="btn blue" id="btnBump">BUMP TO TOP</button>
    </div>
    <button class="btn pink wide" id="btnToggle">PAUSAR ANUNCIO</button>
    <button class="btn foto-btn wide" id="btnFoto">📸 VER FOTO DE MI ANUNCIO</button>
    <div class="row2">
      <button class="btn green" id="btnPay">PAGAR SERVICIO</button>
      <button class="btn purple" id="btnHelp">AYUDA</button>
    </div>
    <div class="note">Fotos y borrado bloqueados por seguridad</div>
    <div id="editPanel" class="panel" style="display:none">
      <h3>Editar mi anuncio</h3>
      <label>Texto del anuncio <span id="lockTexto"></span></label>
      <textarea id="txtTexto" rows="4"></textarea>
      <button class="btn pink wide" id="btnSaveTexto">GUARDAR TEXTO</button>
      <label>Dirección / Ciudad (solo lectura)</label>
      <div id="dirTxt" style="padding:10px;border:1px solid #f9a8d4;border-radius:8px;font-size:15px;margin-top:4px;background:#fdf2f8"></div>
      <button class="btn purple wide" id="btnPedirDir">📍 PEDIR CAMBIO DE DIRECCIÓN</button>
      <div style="font-size:12px;color:#831843;margin-top:4px">El cambio de dirección lo hace soporte por ti. Toca el botón y te atendemos en el chat.</div>
    </div>
    <div id="helpPanel" class="panel" style="display:none">
      <h3>Centro de Soporte</h3>
      <button class="btn sup" data-motivo="consejos">💡 Consejos para vender más</button>
      <button class="btn sup" data-motivo="estafa">🚨 Cuidado con mensajes falsos</button>
      <button class="btn sup" data-motivo="nosube">🚫 Mi página no está subiendo</button>
      <button class="btn sup" data-motivo="bloqueo">🔒 Creo que mi post se bloqueó</button>
      <button class="btn sup" data-motivo="editar">✏️ Problema editando mi página</button>
      <button class="btn sup" data-motivo="nuevo">➕ Quiero rentar un anuncio nuevo más</button>
      <button class="btn sup" data-motivo="direccion">📍 Quiero cambiar mi dirección</button>
    </div>
    <div id="msg" class="msg" style="display:none"></div>
  </div>
</div></div>
<script>
var tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
var initData = tg ? (tg.initData || "") : "";
if (tg) { try { tg.ready(); tg.expand(); } catch (e) {} }
function $(id) { return document.getElementById(id); }
function showMsg(t, ok) { var m = $("msg"); m.style.display = "block"; m.textContent = t; m.className = "msg " + (ok ? "ok" : "err"); try { m.scrollIntoView({behavior: "smooth", block: "center"}); } catch (e) {} }
function api(path, body) {
  return fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Telegram-Init-Data": initData}, body: JSON.stringify(body || {})})
    .then(function (r) { return r.json().then(function (j) { return {status: r.status, j: j}; }); });
}
var ST = null;
function render(st) {
  ST = st;
  $("venceBadge").textContent = st.vence_corto ? ("VENCE: " + st.vence_corto) : "VENCE";
  if (st.foto_url) { var im = $("fotoImg"); im.src = st.foto_url; im.style.display = "block"; $("fotoTxt").style.display = "none"; }
  var estado = st.estado === "ACTIVO" ? "🟢 ACTIVO" : (st.estado === "PAUSADO" ? "🔴 PAUSADO" : "🔒 VENCIDO");
  $("estadoTxt").textContent = estado + " · " + (st.ubicacion || "Sin ubicación") + " · Vence: " + st.vence + (st.faltan ? " (Faltan " + st.faltan + ")" : "");
  $("proxBump").textContent = st.proximo_bump;
  $("bumpsHoy").textContent = "Bumps hoy: " + st.bumps_hoy + " · Te avisamos en el chat cuando suba";
  var tb = $("btnToggle");
  if (st.paused) { tb.textContent = "ENCENDER ANUNCIO"; tb.className = "btn green wide"; }
  else { tb.textContent = "PAUSAR ANUNCIO"; tb.className = "btn pink wide"; }
  $("vencido").style.display = st.rental_expired ? "block" : "none";
  ["btnEdit", "btnBump", "btnToggle", "btnFoto"].forEach(function (id) { $(id).disabled = !!st.rental_expired; });
  $("txtTexto").value = st.texto || "";
  $("dirTxt").textContent = st.ubicacion || "Sin ubicación";
  $("lockTexto").textContent = st.edit_texto_disponible ? "(disponible)" : "(🔒 disponible en " + st.edit_texto_faltan_h + " h)";
  $("btnSaveTexto").disabled = !st.edit_texto_disponible;
}
function loadStatus() {
  return api("/renta/api/status").then(function (res) {
    if (res.j && res.j.ok) render(res.j);
    else showMsg((res.j && res.j.message) || "No se pudo cargar tu anuncio.", false);
  }).catch(function () { showMsg("Sin conexión. Inténtalo otra vez.", false); });
}
function act(path, body) {
  return api(path, body).then(function (res) {
    var j = res.j || {};
    if (j.ok) { if (j.estado) render(j); else loadStatus(); showMsg(j.message || "✅ Listo.", true); }
    else showMsg(j.message || "Algo salió mal. Inténtalo otra vez.", false);
  }).catch(function () { showMsg("Sin conexión. Inténtalo otra vez.", false); });
}
$("btnToggle").onclick = function () { act(ST && ST.paused ? "/renta/api/resume" : "/renta/api/pause"); };
$("btnBump").onclick = function () { act("/renta/api/bump"); };
$("btnFoto").onclick = function () { act("/renta/api/foto"); };
$("btnPay").onclick = function () { act("/renta/api/pay"); };
$("btnNew").onclick = function () { showMsg("🔒 Bloqueado por seguridad: esta opción no está disponible. Si necesitas ayuda, toca AYUDA.", false); };
$("btnRemove").onclick = function () { showMsg("🔒 Bloqueado por seguridad: no puedes borrar tu anuncio desde aquí. Si necesitas ayuda, toca AYUDA.", false); };
$("btnEdit").onclick = function () { var p = $("editPanel"); p.style.display = p.style.display === "none" ? "block" : "none"; };
$("btnHelp").onclick = function () { var p = $("helpPanel"); p.style.display = p.style.display === "none" ? "block" : "none"; };
$("btnSaveTexto").onclick = function () { act("/renta/api/edit_text", {texto: $("txtTexto").value}); };
$("btnPedirDir").onclick = function () { act("/renta/api/support", {motivo: "direccion"}); };
Array.prototype.forEach.call(document.querySelectorAll(".sup"), function (b) {
  b.onclick = function () { act("/renta/api/support", {motivo: b.getAttribute("data-motivo")}); };
});
if (!initData) { $("sinbot").style.display = "block"; }
else { $("app").style.display = "block"; loadStatus(); }
</script>
</body></html>
"""

@app.route("/renta/app")
def renta_app():
    return RENTA_APP_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/renta/api/status", methods=["POST"])
def renta_api_status():
    sess = db(); p, err = rental_from_init_data(sess)
    if err: sess.close(); return err
    out = rental_status_payload(p); sess.close(); return jsonify(out)

@app.route("/renta/api/pause", methods=["POST"])
def renta_api_pause():
    sess = db(); p, err = rental_from_init_data(sess)
    if err: sess.close(); return err
    b = _renta_expired_block(p)
    if b: sess.close(); return b
    apply_desired(sess, p, paused=True)
    make_command(sess, p, "pause", {"reason": "rental_app"}, f"rentaapp:{int(time.time()*1000)}")
    sess.commit(); out = rental_status_payload(p); out["message"] = "⏸️ Anuncio pausado."; sess.close(); return jsonify(out)

@app.route("/renta/api/resume", methods=["POST"])
def renta_api_resume():
    sess = db(); p, err = rental_from_init_data(sess)
    if err: sess.close(); return err
    b = _renta_expired_block(p)
    if b: sess.close(); return b
    apply_desired(sess, p, paused=False)
    make_command(sess, p, "resume", {"reason": "rental_app"}, f"rentaapp:{int(time.time()*1000)}")
    sess.commit(); out = rental_status_payload(p); out["message"] = "▶️ Anuncio encendido."; sess.close(); return jsonify(out)

@app.route("/renta/api/bump", methods=["POST"])
def renta_api_bump():
    sess = db(); p, err = rental_from_init_data(sess)
    if err: sess.close(); return err
    b = _renta_expired_block(p)
    if b: sess.close(); return b
    if p.paused:
        sess.close(); return jsonify({"ok": False, "error": "pausado", "message": "Tu anuncio está pausado. Toca ENCENDER ANUNCIO primero."}), 409
    make_command(sess, p, "bump_now", {"reason": "rental_app"}, f"rentaapp:{int(time.time()*1000)}")
    sess.commit(); out = rental_status_payload(p); out["message"] = "⚡ Bump solicitado. Te avisamos en el chat cuando suba."; sess.close(); return jsonify(out)

@app.route("/renta/api/foto", methods=["POST"])
def renta_api_foto():
    sess = db(); p, err = rental_from_init_data(sess)
    if err: sess.close(); return err
    b = _renta_expired_block(p)
    if b: sess.close(); return b
    make_command(sess, p, "photo", {"reason": "rental_app"}, f"rentaapp:{int(time.time()*1000)}")
    sess.commit(); out = rental_status_payload(p); out["message"] = "📸 Foto solicitada. En 1-2 minutos te llega a este chat."; sess.close(); return jsonify(out)

@app.route("/renta/api/edit_text", methods=["POST"])
def renta_api_edit_text():
    sess = db(); p, err = rental_from_init_data(sess)
    if err: sess.close(); return err
    b = _renta_expired_block(p)
    if b: sess.close(); return b
    nuevo = str((request.get_json(silent=True) or {}).get("texto", "")).strip()
    if not nuevo:
        sess.close(); return jsonify({"ok": False, "error": "vacio", "message": "Escribe el nuevo texto."}), 400
    left = 86400 - (time.time() - float(p.last_text_edit_at or 0))
    if left > 0:
        sess.close(); return jsonify({"ok": False, "error": "candado_24h", "message": f"🔒 Edición bloqueada por 24 h. Disponible en {int(left//3600)}h {int((left%3600)//60)}m.", "faltan_h": round(left / 3600, 1)}), 429
    p.texto = nuevo; p.last_text_edit_at = time.time(); sess.commit()
    out = rental_status_payload(p); out["message"] = "✅ Texto actualizado. Candado de 24 h activado 🔒"; sess.close(); return jsonify(out)

@app.route("/renta/api/change_city", methods=["POST"])
def renta_api_change_city():
    # Regla de Luis: la cliente no cambia la dirección; lo hace el admin desde el panel.
    return jsonify({"ok": False, "error": "bloqueado", "message": "🔒 El cambio de dirección lo hace soporte. Toca AYUDA y te atendemos."}), 403

@app.route("/renta/api/pay", methods=["POST"])
def renta_api_pay():
    sess = db(); p, err = rental_from_init_data(sess)
    if err: sess.close(); return err
    _renta_admin_alert(sess, p, "Quiere pagar / renovar servicio"); sess.commit(); sess.close()
    return jsonify({"ok": True, "message": "✅ Solicitud enviada. El administrador te atiende en este chat para renovar."})

@app.route("/renta/api/write_new", methods=["POST"])
def renta_api_write_new():
    # Regla de Luis: WRITE NEW es solo estética (🔒). No crea, no borra, no alerta.
    return jsonify({"ok": False, "error": "bloqueado", "message": "🔒 Bloqueado por seguridad: esta opción no está disponible."}), 403

SOP_MOTIVOS_APP = {"consejos": "Consejos para vender más", "nosube": "Mi página no está subiendo",
                   "bloqueo": "Creo que mi post se bloqueó", "editar": "Problema editando mi página",
                   "nuevo": "Quiere rentar un anuncio nuevo más", "pagar": "Quiere pagar / renovar servicio",
                   "direccion": "Quiere cambiar su dirección"}
ESTAFA_TXT_APP = "🚨 Cuidado con mensajes falsos\nSi te mandan un enlace raro diciendo que es de MegaPersonals o de soporte, es una estafa. No pagues ni des tu clave fuera de este chat. Ante la duda, escríbenos por Ayuda."

@app.route("/renta/api/support", methods=["POST"])
def renta_api_support():
    sess = db(); p, err = rental_from_init_data(sess)
    if err: sess.close(); return err
    motivo = str((request.get_json(silent=True) or {}).get("motivo", "")).strip()
    if motivo == "estafa":
        sess.close(); return jsonify({"ok": True, "message": ESTAFA_TXT_APP})
    texto_motivo = SOP_MOTIVOS_APP.get(motivo, "Solicitud de soporte")
    _renta_admin_alert(sess, p, texto_motivo); sess.commit(); sess.close()
    return jsonify({"ok": True, "message": "✅ Tu solicitud fue enviada al administrador."})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
