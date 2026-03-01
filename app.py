import os
import threading
import time
import random
import uuid
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired, RateLimitError, ClientError, ClientForbiddenError,
    ClientNotFoundError, ChallengeRequired, PleaseWaitFewMinutes
)
from commands import (
    process_command,
    handle_auto_reply,
    enforce_nick_locks,
    enforce_name_lock,
)

app = Flask(__name__)

# ───────────────────────────────────────────
#  GLOBAL STATE
# ───────────────────────────────────────────
BOT_THREAD    = None
STOP_EVENT    = threading.Event()
LOGS          = []
START_TIME    = None
CLIENT        = None
SESSION_TOKEN = None
LOGIN_SUCCESS = False
TASK_ID       = None

STATS = {
    "total_welcomed": 0,
    "today_welcomed": 0,
    "messages_sent":  0,
    "commands_used":  0,
    "last_reset":     str(datetime.now().date()),
}

BOT_CONFIG = {
    "target_spam": {},
    "spam_active": {},
}


# ───────────────────────────────────────────
#  HELPERS
# ───────────────────────────────────────────
def uptime():
    if not START_TIME:
        return "00:00:00"
    delta = datetime.now() - START_TIME
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def log(msg, level="INFO"):
    ts = datetime.now().strftime('%H:%M:%S')
    lm = f"[{ts}] {msg}"
    LOGS.append({"text": lm, "level": level, "ts": ts})
    if len(LOGS) > 600:
        LOGS[:] = LOGS[-600:]
    print(lm)

def safe_send(client, gid, message):
    try:
        client.direct_send(message, thread_ids=[gid])
        STATS["messages_sent"] += 1
        return True
    except Exception as e:
        log(f"Send error: {str(e)[:50]}", "WARN")
        return False


# ───────────────────────────────────────────
#  CLIENT / SESSION
# ───────────────────────────────────────────
def create_stable_client():
    cl = Client()
    cl.delay_range = [8, 15]
    cl.request_timeout = 90
    cl.max_retries = 1
    cl.set_user_agent(
        "Instagram 380.0.0.28.104 Android (35/14; 600dpi; 1440x3360; "
        "samsung; SM-S936B; dm5q; exynos2500; en_IN; 380000028)"
    )
    return cl

def safe_login(cl, token, max_retries=3):
    global LOGIN_SUCCESS, SESSION_TOKEN
    for attempt in range(max_retries):
        try:
            log(f"Login attempt {attempt+1}/{max_retries}")
            cl.login_by_sessionid(token)
            account = cl.account_info()
            if account and hasattr(account, 'username') and account.username:
                log(f"Login SUCCESS: @{account.username}", "OK")
                LOGIN_SUCCESS = True
                SESSION_TOKEN = token
                time.sleep(3)
                return True, account.username
        except Exception as e:
            err = str(e).lower()
            if "session" in err or "login required" in err:
                log("Session expired!", "ERR"); return False, None
            elif "rate limit" in err:
                log("Rate limited – 60s wait", "WARN"); time.sleep(60)
            elif "challenge" in err:
                log("Challenge required", "ERR"); time.sleep(30)
            else:
                log(f"Login error: {str(e)[:50]}", "WARN")
                time.sleep(15 * (attempt + 1))
    return False, None

def session_health_check():
    global CLIENT, LOGIN_SUCCESS
    try:
        if CLIENT:
            CLIENT.account_info()
            return True
    except:
        pass
    LOGIN_SUCCESS = False
    return False

def refresh_session(token):
    global CLIENT, LOGIN_SUCCESS
    log("Auto session refresh...", "WARN")
    nc = create_stable_client()
    ok, _ = safe_login(nc, token)
    if ok:
        CLIENT = nc
        return True
    return False


# ───────────────────────────────────────────
#  MAIN BOT LOOP
# ───────────────────────────────────────────
def run_bot(session_token, welcome_msgs, gids, delay, poll, use_nick, enable_cmds, admin_ids):
    global START_TIME, CLIENT, LOGIN_SUCCESS, TASK_ID

    START_TIME = datetime.now()
    TASK_ID = f"TASK-{str(uuid.uuid4()).upper()[:8]}"
    consecutive_errors = 0
    max_errors = 12

    log(f"Bot v6.0 STARTING | Task: {TASK_ID}", "OK")
    log(f"Groups: {len(gids)} | Admins: {len(admin_ids)} | Commands: {enable_cmds}", "INFO")

    CLIENT = create_stable_client()
    ok, username = safe_login(CLIENT, session_token)
    if not ok:
        log("Login FAILED – Bot stopped", "ERR"); return

    known_members = {gid: set() for gid in gids}
    last_msg_id   = {gid: None  for gid in gids}

    log("Initializing groups...", "INFO")
    for i, gid in enumerate(gids):
        try:
            time.sleep(10)
            thread = CLIENT.direct_thread(gid)
            known_members[gid] = {u.pk for u in thread.users}
            if thread.messages:
                last_msg_id[gid] = thread.messages[0].id
            BOT_CONFIG["spam_active"][gid] = False
            log(f"Group {i+1}/{len(gids)} ready: {gid[:14]}...", "OK")
        except Exception as e:
            log(f"Group init error: {str(e)[:40]}", "WARN")

    log(f"Bot LIVE | Task: {TASK_ID} | @{username}", "OK")

    while not STOP_EVENT.is_set():
        for gid in gids:
            if STOP_EVENT.is_set():
                break

            try:
                if not session_health_check():
                    if refresh_session(SESSION_TOKEN):
                        consecutive_errors = 0
                    else:
                        log("Session recovery FAILED", "ERR"); return

                time.sleep(random.uniform(12, 20))
                thread = CLIENT.direct_thread(gid)
                consecutive_errors = 0

                # ── ENFORCE LOCKS (nick + name) ──
                try:
                    enforce_nick_locks(gid, thread, CLIENT, log)
                    enforce_name_lock(gid, thread, CLIENT, log)
                except Exception as e:
                    log(f"Lock enforce error: {str(e)[:40]}", "WARN")

                # ── COMMANDS + AUTO REPLY ──
                if enable_cmds:
                    new_msgs = []
                    if last_msg_id[gid] and thread.messages:
                        for msg in thread.messages[:10]:
                            if msg.id == last_msg_id[gid]:
                                break
                            new_msgs.append(msg)

                    for msg_obj in reversed(new_msgs[:5]):
                        try:
                            if not msg_obj or msg_obj.user_id == CLIENT.user_id:
                                continue
                            sender = next((u for u in thread.users if u.pk == msg_obj.user_id), None)
                            if not sender or not hasattr(sender, 'username'):
                                continue

                            handled = process_command(
                                text       = (msg_obj.text or "").strip().lower(),
                                msg_obj    = msg_obj,
                                sender     = sender,
                                thread     = thread,
                                gid        = gid,
                                client     = CLIENT,
                                bot_config = BOT_CONFIG,
                                stats      = STATS,
                                uptime_fn  = uptime,
                                admin_ids  = admin_ids,
                                log_fn     = log,
                            )

                            # If not a command, try auto reply
                            if not handled:
                                handle_auto_reply(
                                    msg_obj   = msg_obj,
                                    sender    = sender,
                                    thread    = thread,
                                    gid       = gid,
                                    client    = CLIENT,
                                    stats     = STATS,
                                    admin_ids = admin_ids,
                                    log_fn    = log,
                                )

                        except Exception as e:
                            log(f"Msg process error: {str(e)[:40]}", "WARN")

                    if thread.messages:
                        last_msg_id[gid] = thread.messages[0].id

                # ── SPAM ──
                if BOT_CONFIG["spam_active"].get(gid):
                    target = BOT_CONFIG["target_spam"].get(gid)
                    if target:
                        try:
                            safe_send(CLIENT, gid, f"@{target['username']} {target['message']}")
                            time.sleep(4)
                        except:
                            pass

                # ── WELCOME NEW USERS ──
                current_members = {u.pk for u in thread.users}
                new_users = current_members - known_members[gid]

                for user in thread.users:
                    if user.pk in new_users and hasattr(user, 'username') and user.username:
                        try:
                            wm = random.choice(welcome_msgs)
                            msg = f"@{user.username} {wm}" if use_nick else wm
                            if safe_send(CLIENT, gid, msg):
                                STATS["total_welcomed"] += 1
                                STATS["today_welcomed"] += 1
                                log(f"WELCOME: @{user.username}", "OK")
                            time.sleep(delay * 2)
                            break
                        except:
                            break

                known_members[gid] = current_members

            except RateLimitError:
                consecutive_errors += 1
                log("Rate limit! 2min cooldown", "WARN")
                time.sleep(120)
            except Exception as e:
                consecutive_errors += 1
                log(f"Loop error: {str(e)[:50]}", "WARN")
                time.sleep(15)

        if consecutive_errors > max_errors:
            log("Emergency session restart...", "WARN")
            if not refresh_session(SESSION_TOKEN):
                break

        time.sleep(poll + random.uniform(3, 7))

    log("Bot STOPPED", "INFO")


# ───────────────────────────────────────────
#  FLASK ROUTES
# ───────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(PAGE_HTML)

@app.route("/start", methods=["POST"])
def start():
    global BOT_THREAD, STOP_EVENT
    if BOT_THREAD and BOT_THREAD.is_alive():
        return jsonify({"ok": False, "message": "Bot already running hai!"})
    try:
        token   = request.form.get("session", "").strip()
        welcome = [x.strip() for x in request.form.get("welcome", "").splitlines() if x.strip()]
        gids    = [x.strip() for x in request.form.get("group_ids", "").split(",") if x.strip()]
        admins  = [x.strip().replace("@", "") for x in request.form.get("admin_ids", "").split(",") if x.strip()]

        if not all([token, welcome, gids]):
            return jsonify({"ok": False, "message": "Session Token, Welcome Message aur Group ID required hai!"})

        STOP_EVENT.clear()
        STATS.update({"total_welcomed": 0, "today_welcomed": 0, "messages_sent": 0, "commands_used": 0})
        LOGS.clear()

        BOT_THREAD = threading.Thread(
            target=run_bot,
            args=(token, welcome, gids,
                  int(request.form.get("delay", 5)),
                  int(request.form.get("poll", 25)),
                  request.form.get("use_custom_name") == "yes",
                  request.form.get("enable_commands") == "yes",
                  admins),
            daemon=True
        )
        BOT_THREAD.start()
        return jsonify({"ok": True, "message": "Bot start ho gaya! 🚀"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Error: {str(e)}"})

@app.route("/stop", methods=["POST"])
def stop():
    global STOP_EVENT, CLIENT
    STOP_EVENT.set()
    CLIENT = None
    if BOT_THREAD:
        BOT_THREAD.join(timeout=5)
    log("Bot STOPPED by user", "INFO")
    return jsonify({"ok": True, "message": "Bot band ho gaya! 🛑"})

@app.route("/logs")
def logs_route():
    raw = [l["text"] for l in LOGS[-250:]]
    return jsonify({"logs": raw, "uptime": uptime(),
                    "status": "running" if BOT_THREAD and BOT_THREAD.is_alive() else "stopped"})

@app.route("/clear_logs", methods=["POST"])
def clear_logs_route():
    LOGS.clear()
    log("Logs cleared", "INFO")
    return jsonify({"ok": True})

@app.route("/stats")
def stats_route():
    return jsonify({
        "uptime":         uptime(),
        "status":         "running" if BOT_THREAD and BOT_THREAD.is_alive() else "stopped",
        "total_welcomed": STATS["total_welcomed"],
        "today_welcomed": STATS["today_welcomed"],
        "messages_sent":  STATS["messages_sent"],
        "commands_used":  STATS["commands_used"],
        "task_id":        TASK_ID or "—",
        "login":          LOGIN_SUCCESS,
    })

@app.route("/status_check")
def status_check():
    return jsonify({
        "alive":     BOT_THREAD is not None and BOT_THREAD.is_alive(),
        "task_id":   TASK_ID or "",
        "uptime":    uptime(),
        "msgs_sent": STATS["messages_sent"],
        "cmds_used": STATS["commands_used"],
        "welcomed":  STATS["total_welcomed"],
    })


# ───────────────────────────────────────────
#  PREMIUM HTML PAGE
# ───────────────────────────────────────────
PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Instagram Bot v6.0 — Premium Panel</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
  :root{
    --bg:#080810;--surface:#0f0f1a;--card:#16162a;--border:#252540;
    --accent:#7c3aed;--accent2:#06b6d4;--green:#10b981;--red:#ef4444;
    --yellow:#f59e0b;--orange:#f97316;--text:#e2e8f0;--muted:#64748b;--radius:16px;
  }
  *{margin:0;padding:0;box-sizing:border-box;}
  body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;}
  ::-webkit-scrollbar{width:5px;height:5px;}
  ::-webkit-scrollbar-track{background:transparent;}
  ::-webkit-scrollbar-thumb{background:var(--border);border-radius:5px;}

  .header{
    background:linear-gradient(135deg,#0d0520 0%,#050d20 100%);
    border-bottom:1px solid var(--border);padding:18px 36px;
    display:flex;align-items:center;justify-content:space-between;
    position:sticky;top:0;z-index:100;
  }
  .logo{display:flex;align-items:center;gap:14px;}
  .logo-icon{
    width:46px;height:46px;border-radius:13px;
    background:linear-gradient(135deg,var(--accent),var(--accent2));
    display:grid;place-items:center;font-size:1.4rem;
    box-shadow:0 0 24px rgba(124,58,237,.5);
  }
  .logo-text h1{font-size:1.3rem;font-weight:800;background:linear-gradient(90deg,#a78bfa,#67e8f9);-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
  .logo-text p{font-size:.72rem;color:var(--muted);}

  .status-pill{display:flex;align-items:center;gap:8px;background:var(--card);border:1px solid var(--border);border-radius:40px;padding:8px 18px;font-weight:600;font-size:.85rem;transition:all .3s;}
  .status-pill.running{border-color:var(--green);color:var(--green);}
  .status-pill.stopped{border-color:var(--red);color:var(--red);}
  .dot{width:9px;height:9px;border-radius:50%;background:currentColor;animation:pulse 2s infinite;}
  @keyframes pulse{0%,100%{opacity:1;transform:scale(1);}50%{opacity:.4;transform:scale(.8);}}

  .status-box{
    display:flex;align-items:center;gap:0;
    background:linear-gradient(135deg,rgba(124,58,237,.07),rgba(6,182,212,.04));
    border:1px solid var(--border);border-radius:var(--radius);
    margin:24px 36px 0;overflow:hidden;
  }
  .sb-item{flex:1;padding:16px 20px;text-align:center;border-right:1px solid var(--border);}
  .sb-item:last-child{border-right:none;}
  .sb-label{font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:5px;}
  .sb-val{font-size:1.3rem;font-weight:800;color:var(--text);}
  .task-badge{
    display:flex;align-items:center;gap:8px;padding:14px 22px;
    background:rgba(124,58,237,.12);border-right:1px solid var(--border);
    font-family:monospace;font-size:.88rem;color:#a78bfa;white-space:nowrap;
  }

  .main{display:grid;grid-template-columns:1fr 400px;gap:0;min-height:calc(100vh - 140px);}
  .left-panel{padding:24px 36px;border-right:1px solid var(--border);}
  .right-panel{padding:24px 28px;background:rgba(0,0,0,.25);}

  .section-title{font-size:.68rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;display:flex;align-items:center;gap:8px;}
  .section-title::after{content:'';flex:1;height:1px;background:var(--border);}

  .form-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:18px;}
  .form-group{display:flex;flex-direction:column;gap:7px;}
  .form-group.full{grid-column:1/-1;}
  label.field-label{font-size:.78rem;font-weight:600;color:#94a3b8;display:flex;align-items:center;gap:6px;}
  label.field-label .req{color:var(--red);}
  input,textarea{
    background:var(--card);border:1.5px solid var(--border);color:var(--text);
    border-radius:11px;padding:12px 15px;font-size:.9rem;font-family:'Inter',sans-serif;transition:all .25s;
  }
  input:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(124,58,237,.12);}
  textarea{resize:vertical;min-height:100px;}

  .toggles{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px;}
  .toggle-row{
    display:flex;align-items:center;justify-content:space-between;
    background:var(--card);border:1.5px solid var(--border);border-radius:11px;padding:13px 16px;transition:all .25s;
  }
  .toggle-row:hover{border-color:var(--accent);}
  .toggle-label{font-size:.85rem;font-weight:600;display:flex;align-items:center;gap:9px;color:#94a3b8;}
  .switch{position:relative;width:42px;height:23px;flex-shrink:0;}
  .switch input{opacity:0;width:0;height:0;}
  .slider{position:absolute;inset:0;background:#1e1e3a;border-radius:23px;cursor:pointer;transition:.3s;}
  .slider:before{content:'';position:absolute;height:17px;width:17px;left:3px;bottom:3px;background:#4a5568;border-radius:50%;transition:.3s;}
  input:checked+.slider{background:var(--accent);}
  input:checked+.slider:before{transform:translateX(19px);background:white;}

  .btn-row{display:flex;gap:12px;margin-bottom:20px;}
  .btn{flex:1;padding:13px 18px;border:none;border-radius:11px;font-size:.92rem;font-weight:700;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:9px;transition:all .25s;letter-spacing:.3px;}
  .btn-start{background:linear-gradient(135deg,#059669,#10b981);color:white;box-shadow:0 6px 18px rgba(16,185,129,.3);}
  .btn-stop{background:linear-gradient(135deg,#dc2626,#ef4444);color:white;box-shadow:0 6px 18px rgba(239,68,68,.3);}
  .btn:hover:not(:disabled){transform:translateY(-2px);filter:brightness(1.1);}
  .btn:disabled{opacity:.45;cursor:not-allowed;}

  /* COMMAND TABLE */
  .cmd-table{width:100%;border-collapse:collapse;font-size:.8rem;margin-bottom:6px;}
  .cmd-table tr{border-bottom:1px solid rgba(255,255,255,.04);}
  .cmd-table tr:last-child{border-bottom:none;}
  .cmd-table td{padding:7px 8px;}
  .cmd-table td:first-child{font-family:monospace;color:#a78bfa;font-weight:700;white-space:nowrap;}
  .cmd-table td:last-child{text-align:right;}
  .cmd-table td:nth-child(2){color:#94a3b8;padding-left:12px;}
  .badge{font-size:.62rem;padding:2px 8px;border-radius:20px;font-weight:700;letter-spacing:.5px;}
  .badge-admin{background:rgba(245,158,11,.18);color:var(--yellow);}
  .badge-pub{background:rgba(16,185,129,.18);color:var(--green);}
  .badge-new{background:rgba(249,115,22,.18);color:var(--orange);}

  .cmd-section{background:var(--card);border:1.5px solid var(--border);border-radius:13px;padding:16px;margin-bottom:18px;}
  .cmd-section-title{font-size:.72rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;display:flex;align-items:center;gap:7px;}

  /* STATS */
  .stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px;}
  .stat-card{background:var(--card);border:1.5px solid var(--border);border-radius:13px;padding:18px;text-align:center;position:relative;overflow:hidden;transition:all .3s;}
  .stat-card::after{content:'';position:absolute;inset:0;background:linear-gradient(135deg,rgba(124,58,237,.06),transparent);opacity:0;transition:.3s;}
  .stat-card:hover::after{opacity:1;}
  .stat-icon{font-size:1.5rem;margin-bottom:7px;}
  .stat-num{font-size:1.9rem;font-weight:800;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
  .stat-label{font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-top:3px;}

  /* LOGS */
  #logs{
    background:#04040e;border:1px solid var(--border);border-radius:13px;
    padding:16px;height:340px;overflow-y:auto;font-family:'Fira Code','Courier New',monospace;
    font-size:.78rem;line-height:1.75;white-space:pre-wrap;
  }
  .log-ok{color:#34d399;}.log-err{color:#f87171;}.log-warn{color:#fbbf24;}.log-info{color:#7dd3fc;}

  #uptimeDisplay{font-family:monospace;font-weight:700;color:var(--accent2);font-size:1rem;}

  .alert{
    position:fixed;top:20px;right:20px;z-index:9999;
    background:var(--card);border:1.5px solid var(--border);border-radius:13px;
    padding:14px 20px;display:flex;align-items:center;gap:10px;
    max-width:340px;font-size:.88rem;font-weight:600;
    transform:translateX(400px);transition:transform .4s cubic-bezier(.34,1.56,.64,1);
    box-shadow:0 20px 40px rgba(0,0,0,.6);
  }
  .alert.show{transform:translateX(0);}
  .alert.ok{border-color:var(--green);color:var(--green);}
  .alert.fail{border-color:var(--red);color:var(--red);}

  .spinner{width:17px;height:17px;border:2.5px solid rgba(255,255,255,.2);border-top-color:white;border-radius:50%;animation:spin .7s linear infinite;display:none;}
  @keyframes spin{to{transform:rotate(360deg);}}
  .btn-sm{padding:6px 12px;border:1px solid var(--border);background:transparent;color:var(--muted);border-radius:8px;font-size:.75rem;cursor:pointer;transition:all .2s;font-weight:600;}
  .btn-sm:hover{border-color:var(--accent);color:var(--accent);}

  @media(max-width:900px){.main{grid-template-columns:1fr;}.form-grid{grid-template-columns:1fr;}.toggles{grid-template-columns:1fr;}.header{padding:14px 18px;}.status-box{margin:16px 18px 0;flex-wrap:wrap;}.left-panel,.right-panel{padding:18px;}}
</style>
</head>
<body>

<div class="header">
  <div class="logo">
    <div class="logo-icon">🤖</div>
    <div class="logo-text">
      <h1>Instagram Bot v6.0</h1>
      <p>Premium Control Panel — All Commands Unlocked</p>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:16px;">
    <span id="uptimeDisplay">00:00:00</span>
    <div class="status-pill stopped" id="statusPill">
      <div class="dot"></div>
      <span id="statusText">Stopped</span>
    </div>
  </div>
</div>

<!-- STATUS BOX -->
<div class="status-box">
  <div class="task-badge">
    <i class="fas fa-fingerprint"></i>
    <span id="taskIdVal">Bot start nahi hua</span>
  </div>
  <div class="sb-item">
    <div class="sb-label">Messages Sent</div>
    <div class="sb-val" id="sbMsgs">0</div>
  </div>
  <div class="sb-item">
    <div class="sb-label">Commands Used</div>
    <div class="sb-val" id="sbCmds">0</div>
  </div>
  <div class="sb-item">
    <div class="sb-label">Welcomed</div>
    <div class="sb-val" id="sbWel">0</div>
  </div>
  <div class="sb-item" style="border-right:none;">
    <div class="sb-label">Uptime</div>
    <div class="sb-val" id="sbUptime">00:00:00</div>
  </div>
</div>

<!-- MAIN -->
<div class="main">

  <!-- LEFT: CONFIG -->
  <div class="left-panel">
    <div class="section-title"><i class="fas fa-sliders"></i> Bot Configuration</div>
    <form id="botForm">
      <div class="form-grid">
        <div class="form-group">
          <label class="field-label"><i class="fas fa-key"></i> Session Token <span class="req">*</span></label>
          <input type="password" name="session" placeholder="Instagram session ID" required>
        </div>
        <div class="form-group">
          <label class="field-label"><i class="fas fa-hashtag"></i> Group IDs <span class="req">*</span></label>
          <input type="text" name="group_ids" placeholder="id1,id2,id3" required>
        </div>
        <div class="form-group">
          <label class="field-label"><i class="fas fa-user-shield"></i> Admin Usernames</label>
          <input type="text" name="admin_ids" placeholder="admin1,admin2 (without @)">
        </div>
        <div class="form-group" style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
          <div>
            <label class="field-label"><i class="fas fa-hourglass"></i> Delay (s)</label>
            <input type="number" name="delay" value="5" min="3" max="15">
          </div>
          <div>
            <label class="field-label"><i class="fas fa-sync"></i> Poll (s)</label>
            <input type="number" name="poll" value="25" min="20" max="60">
          </div>
        </div>
        <div class="form-group full">
          <label class="field-label"><i class="fas fa-comment-dots"></i> Welcome Messages <span class="req">*</span> <small style="color:var(--muted);font-weight:400;">(1 line = 1 msg, random jayega)</small></label>
          <textarea name="welcome" placeholder="Ek line me ek message likho">Welcome bro! 🔥
Have fun! 🎉
Enjoy group! 😊</textarea>
        </div>
      </div>

      <div class="toggles">
        <label class="toggle-row">
          <span class="toggle-label"><i class="fas fa-at" style="color:#a78bfa"></i> @Mention on Welcome</span>
          <label class="switch"><input type="checkbox" name="use_custom_name" value="yes" id="ucn" checked><span class="slider"></span></label>
        </label>
        <label class="toggle-row">
          <span class="toggle-label"><i class="fas fa-terminal" style="color:#a78bfa"></i> Enable Commands</span>
          <label class="switch"><input type="checkbox" name="enable_commands" value="yes" id="ecmd" checked><span class="slider"></span></label>
        </label>
      </div>

      <div class="btn-row">
        <button type="button" class="btn btn-start" id="startBtn" onclick="startBot()">
          <div class="spinner" id="startSpinner"></div>
          <i class="fas fa-play" id="startIcon"></i>
          <span>Start Bot</span>
        </button>
        <button type="button" class="btn btn-stop" onclick="stopBot()">
          <i class="fas fa-stop"></i><span>Stop Bot</span>
        </button>
      </div>
    </form>

    <!-- COMMAND REFERENCE TABLE -->
    <div class="cmd-section">
      <div class="cmd-section-title"><i class="fas fa-terminal" style="color:#a78bfa"></i> Public Commands</div>
      <table class="cmd-table">
        <tr><td>/ping</td><td>Bot status check</td><td><span class="badge badge-pub">PUBLIC</span></td></tr>
        <tr><td>/uptime</td><td>Running time</td><td><span class="badge badge-pub">PUBLIC</span></td></tr>
        <tr><td>/stats</td><td>Bot statistics</td><td><span class="badge badge-pub">PUBLIC</span></td></tr>
        <tr><td>/help</td><td>Commands list</td><td><span class="badge badge-pub">PUBLIC</span></td></tr>
        <tr><td>/id</td><td>Group ID</td><td><span class="badge badge-pub">PUBLIC</span></td></tr>
      </table>
    </div>

    <div class="cmd-section">
      <div class="cmd-section-title"><i class="fas fa-crown" style="color:#f59e0b"></i> Admin — Nick Name</div>
      <table class="cmd-table">
        <tr><td>/nick @user Name</td><td>Single member nick</td><td><span class="badge badge-admin">ADMIN</span></td></tr>
        <tr><td>/allnick prefix</td><td>Sab ka nick (prefix+naam)</td><td><span class="badge badge-admin">ADMIN</span></td></tr>
        <tr><td>/kicknick</td><td>Sab ka nick reset</td><td><span class="badge badge-admin">ADMIN</span></td></tr>
        <tr><td>/locknick prefix</td><td>Nick auto-lock ON 🔒</td><td><span class="badge badge-new">NEW</span></td></tr>
        <tr><td>/unlocknick</td><td>Nick auto-lock OFF 🔓</td><td><span class="badge badge-new">NEW</span></td></tr>
      </table>
    </div>

    <div class="cmd-section">
      <div class="cmd-section-title"><i class="fas fa-pen" style="color:#f59e0b"></i> Admin — Group Name</div>
      <table class="cmd-table">
        <tr><td>/groupname Name</td><td>Group naam change karo</td><td><span class="badge badge-new">NEW</span></td></tr>
        <tr><td>/lockname Name</td><td>Group naam lock ON 🔒</td><td><span class="badge badge-new">NEW</span></td></tr>
        <tr><td>/unlockname</td><td>Group naam lock OFF 🔓</td><td><span class="badge badge-new">NEW</span></td></tr>
      </table>
    </div>

    <div class="cmd-section">
      <div class="cmd-section-title"><i class="fas fa-photo-film" style="color:#f59e0b"></i> Admin — Media & Utilities</div>
      <table class="cmd-table">
        <tr><td>/setimage URL</td><td>Image URL set karo</td><td><span class="badge badge-new">NEW</span></td></tr>
        <tr><td>/sendimage</td><td>Set image group me bhejo</td><td><span class="badge badge-new">NEW</span></td></tr>
        <tr><td>/yt VideoTitle</td><td>YouTube video search+send</td><td><span class="badge badge-new">NEW</span></td></tr>
        <tr><td>/spam @user msg</td><td>Spam shuru karo</td><td><span class="badge badge-admin">ADMIN</span></td></tr>
        <tr><td>/stopspam</td><td>Spam band karo</td><td><span class="badge badge-admin">ADMIN</span></td></tr>
      </table>
    </div>

    <div class="cmd-section">
      <div class="cmd-section-title"><i class="fas fa-robot" style="color:#f59e0b"></i> Admin — Auto Reply</div>
      <table class="cmd-table">
        <tr><td>/autoreply on</td><td>Auto reply ON karo</td><td><span class="badge badge-new">NEW</span></td></tr>
        <tr><td>/autoreply off</td><td>Auto reply OFF karo</td><td><span class="badge badge-new">NEW</span></td></tr>
        <tr><td>/setreply m1|m2|m3</td><td>Reply messages set karo</td><td><span class="badge badge-new">NEW</span></td></tr>
      </table>
      <div style="margin-top:10px;font-size:.75rem;color:var(--muted);background:rgba(124,58,237,.08);padding:10px;border-radius:8px;">
        💡 Auto Reply: Jab admin nahi hota, koi bhi message kare — bot @username ke saath random reply bhejta hai. 2 min cooldown per user.
      </div>
    </div>

  </div>

  <!-- RIGHT: STATS + LOGS -->
  <div class="right-panel">
    <div class="section-title"><i class="fas fa-chart-line"></i> Live Statistics</div>
    <div class="stats-grid">
      <div class="stat-card"><div class="stat-icon">👋</div><div class="stat-num" id="totalWelcomed">0</div><div class="stat-label">Total Welcomed</div></div>
      <div class="stat-card"><div class="stat-icon">📅</div><div class="stat-num" id="todayWelcomed">0</div><div class="stat-label">Today</div></div>
      <div class="stat-card"><div class="stat-icon">📨</div><div class="stat-num" id="messagesSent">0</div><div class="stat-label">Msgs Sent</div></div>
      <div class="stat-card"><div class="stat-icon">⌨️</div><div class="stat-num" id="commandsUsed">0</div><div class="stat-label">Commands</div></div>
    </div>

    <div class="section-title" style="margin-top:6px;"><i class="fas fa-stream"></i> Live Console</div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
      <span style="font-size:.72rem;color:var(--muted);">Auto-refresh 3s</span>
      <div style="display:flex;gap:8px;">
        <button class="btn-sm" onclick="scrollLogsToBottom()"><i class="fas fa-arrow-down"></i></button>
        <button class="btn-sm" onclick="clearLogs()"><i class="fas fa-trash"></i> Clear</button>
      </div>
    </div>
    <div id="logs">Waiting for bot to start...</div>
  </div>
</div>

<!-- ALERT -->
<div class="alert" id="alertBox">
  <i class="fas fa-info-circle" id="alertIcon"></i>
  <span id="alertMsg">—</span>
</div>

<script>
function showAlert(msg, ok=true){
  const box=document.getElementById('alertBox');
  document.getElementById('alertMsg').textContent=msg;
  box.className='alert show '+(ok?'ok':'fail');
  document.getElementById('alertIcon').className='fas '+(ok?'fa-check-circle':'fa-times-circle');
  setTimeout(()=>box.className='alert',3500);
}
async function startBot(){
  const btn=document.getElementById('startBtn');
  const sp=document.getElementById('startSpinner');
  const ic=document.getElementById('startIcon');
  btn.disabled=true; sp.style.display='block'; ic.style.display='none';
  try{
    const fd=new FormData(document.getElementById('botForm'));
    const r=await fetch('/start',{method:'POST',body:fd});
    const d=await r.json();
    showAlert(d.message,d.ok);
    if(d.ok){updateStatus();updateLogs();}
  }catch(e){showAlert('Connection error!',false);}
  finally{btn.disabled=false;sp.style.display='none';ic.style.display='inline';}
}
async function stopBot(){
  try{
    const r=await fetch('/stop',{method:'POST'});
    const d=await r.json();
    showAlert(d.message,d.ok);
    updateStatus();
  }catch(e){showAlert('Error!',false);}
}
async function clearLogs(){
  await fetch('/clear_logs',{method:'POST'});
  document.getElementById('logs').textContent='Logs cleared!';
}
function scrollLogsToBottom(){
  const el=document.getElementById('logs');
  el.scrollTop=el.scrollHeight;
}
function esc(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function colorLog(line){
  if(/✅|OK|SUCCESS|WELCOME|LIVE|START/.test(line)) return `<span class="log-ok">${esc(line)}</span>`;
  if(/❌|ERR|FAIL|failed/.test(line)) return `<span class="log-err">${esc(line)}</span>`;
  if(/⚠️|WARN|Rate|wait|restart/.test(line)) return `<span class="log-warn">${esc(line)}</span>`;
  return `<span class="log-info">${esc(line)}</span>`;
}
async function updateLogs(){
  try{
    const r=await fetch('/logs');
    const d=await r.json();
    const el=document.getElementById('logs');
    const atBottom=el.scrollTop+el.clientHeight>=el.scrollHeight-30;
    el.innerHTML=d.logs.map(colorLog).join('\\n');
    if(atBottom) el.scrollTop=el.scrollHeight;
  }catch(e){}
}
async function updateStatus(){
  try{
    const r=await fetch('/status_check');
    const d=await r.json();
    const pill=document.getElementById('statusPill');
    document.getElementById('uptimeDisplay').textContent=d.uptime||'00:00:00';
    document.getElementById('sbUptime').textContent=d.uptime||'00:00:00';
    document.getElementById('sbMsgs').textContent=d.msgs_sent||0;
    document.getElementById('sbCmds').textContent=d.cmds_used||0;
    document.getElementById('sbWel').textContent=d.welcomed||0;
    if(d.task_id) document.getElementById('taskIdVal').textContent=d.task_id;
    pill.className='status-pill '+(d.alive?'running':'stopped');
    document.getElementById('statusText').textContent=d.alive?'Running':'Stopped';
    const sr=await fetch('/stats');
    const sd=await sr.json();
    document.getElementById('totalWelcomed').textContent=sd.total_welcomed||0;
    document.getElementById('todayWelcomed').textContent=sd.today_welcomed||0;
    document.getElementById('messagesSent').textContent=sd.messages_sent||0;
    document.getElementById('commandsUsed').textContent=sd.commands_used||0;
  }catch(e){}
}
setInterval(updateStatus,3000);
setInterval(updateLogs,3000);
updateStatus();updateLogs();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log("Instagram Bot v6.0 PREMIUM Starting...", "OK")
    log("commands.py se all commands load!", "OK")
    log("Nick Lock + Name Lock + Image + YT + AutoReply READY!", "OK")
    app.run(host="0.0.0.0", port=port, debug=False)
