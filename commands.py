# ================================================================
#   COMMANDS.PY - Instagram Bot v6.0
#   ✏️  SIRF IS FILE ME COMMANDS EDIT KARO
# ================================================================
#
#  📌 COMMANDS LIST:
#  PUBLIC:   /ping /uptime /help /stats /id
#  ADMIN:    /spam /stopspam
#            /nick @user name    → single nick
#            /allnick prefix     → sab ka nick
#            /kicknick           → sab ka nick reset
#            /locknick prefix    → nick lock (auto-reset karta rahe)
#            /unlocknick         → nick unlock
#            /groupname NewName  → group ka naam change
#            /lockname Name      → group name lock
#            /unlockname         → group name unlock
#            /setimage URL       → image set (URL wali)
#            /sendimage          → set image bhejta hai group me
#            /yt VideoTitle      → YouTube video link bhejta hai
#            /autoreply on/off   → auto reply on/off
#            /setreply m1|m2|m3  → auto reply messages set karo
#
#  AUTO REPLY:
#   - Jab admin online nahi hota, koi bhi message kare
#   - Bot @username ke saath random reply bhejta hai
#
# ================================================================

import time
import random
import urllib.request
import urllib.parse
import re

# ─────────────────────────────────────────────────────────────
#  YAHAN APNI PRESET IMAGE URL DAALO
#  (Command /sendimage se yahi image group me jayegi)
# ─────────────────────────────────────────────────────────────
DEFAULT_IMAGE_URL = "https://i.imgur.com/your_image.jpg"   # apni image URL yahan daalo

# ─────────────────────────────────────────────────────────────
#  AUTO REPLY MESSAGES (random se ek jayega)
#  Jab koi non-admin message kare tab
# ─────────────────────────────────────────────────────────────
DEFAULT_AUTO_REPLIES = [
    "Admin abhi available nahi hain! Thodi der baad try karo 🙏",
    "Bot hai, admin nahi! Message record ho gaya ✅",
    "Koi nahi hai abhi, wait karo bhai! ⏳",
    "Admin ka kaam chal raha hai, baad mein baat karte hain 😅",
]

# ─────────────────────────────────────────────────────────────
#  RUNTIME STATE  (in-memory, restart pe reset ho jata hai)
# ─────────────────────────────────────────────────────────────
_RUNTIME = {
    "nick_locks":     {},   # gid -> {user_pk: target_nick}
    "name_locks":     {},   # gid -> target_name
    "image_urls":     {},   # gid -> url
    "auto_reply":     {},   # gid -> bool
    "reply_msgs":     {},   # gid -> [msg1, msg2, ...]
    "reply_cooldown": {},   # gid -> {user_pk: timestamp}
}


# ─────────────────────────────────────────────────────────────
def get_all_commands():
    return (
        "📋 PUBLIC COMMANDS:\n"
        "/ping  /uptime  /help  /stats  /id\n\n"
        "👑 ADMIN COMMANDS:\n"
        "/spam @user msg   → Spam shuru\n"
        "/stopspam         → Spam band\n"
        "/nick @user Name  → Single nick\n"
        "/allnick prefix   → Sab ka nick\n"
        "/kicknick         → Sab ka nick reset\n"
        "/locknick prefix  → Nick auto-lock ON\n"
        "/unlocknick       → Nick auto-lock OFF\n"
        "/groupname Name   → Group naam change\n"
        "/lockname Name    → Group naam lock ON\n"
        "/unlockname       → Group naam lock OFF\n"
        "/setimage URL     → Image set karo\n"
        "/sendimage        → Set image bhejo\n"
        "/yt VideoTitle    → YouTube video bhejo\n"
        "/autoreply on/off → Auto reply toggle\n"
        "/setreply m1|m2   → Auto reply msgs set"
    )


# ─────────────────────────────────────────────────────────────
def process_command(text, msg_obj, sender, thread, gid,
                    client, bot_config, stats, uptime_fn, admin_ids, log_fn):
    """Main command processor. Returns True agar command handle hua."""
    raw_text       = (msg_obj.text or "").strip()
    text_lower     = raw_text.lower()
    sender_username = (sender.username or "").lower() if hasattr(sender, 'username') else ""

    is_admin = sender_username in [a.lower().replace("@", "") for a in admin_ids] if admin_ids else False

    def bump():
        stats['commands_used'] = stats.get('commands_used', 0) + 1

    # ══════════════════════════════════
    #  PUBLIC COMMANDS
    # ══════════════════════════════════

    if text_lower in ['/ping', '!ping']:
        _send(client, gid,
              f"🏓 Pong! Bot ALIVE!\n"
              f"⏱ Uptime: {uptime_fn()}\n"
              f"📨 Msgs sent: {stats.get('messages_sent', 0)}")
        bump(); log_fn(f"CMD /ping by @{sender_username}"); return True

    elif text_lower in ['/uptime', '!uptime']:
        _send(client, gid, f"⏱ Uptime: {uptime_fn()}\n✅ Bot chal raha hai!")
        bump(); log_fn(f"CMD /uptime by @{sender_username}"); return True

    elif text_lower in ['/help', '!help']:
        _send(client, gid, get_all_commands())
        bump(); log_fn(f"CMD /help by @{sender_username}"); return True

    elif text_lower in ['/stats', '!stats']:
        _send(client, gid,
              f"📊 BOT STATS\n━━━━━━━━━━━━\n"
              f"⏱ Uptime: {uptime_fn()}\n"
              f"👋 Welcomed: {stats.get('total_welcomed', 0)}\n"
              f"📨 Msgs Sent: {stats.get('messages_sent', 0)}\n"
              f"⌨️ Commands: {stats.get('commands_used', 0)}\n"
              f"🚀 Status: Active ✅")
        bump(); log_fn(f"CMD /stats by @{sender_username}"); return True

    elif text_lower in ['/id', '!id']:
        _send(client, gid, f"🆔 Group ID:\n{gid}")
        bump(); log_fn(f"CMD /id by @{sender_username}"); return True

    # ══════════════════════════════════
    #  ADMIN CHECK
    # ══════════════════════════════════

    ADMIN_PREFIXES = [
        '/spam', '/stopspam', '/nick', '/allnick', '/kicknick',
        '/locknick', '/unlocknick', '/groupname', '/lockname', '/unlockname',
        '/setimage', '/sendimage', '/yt', '/autoreply', '/setreply'
    ]
    if any(text_lower.startswith(p) for p in ADMIN_PREFIXES) and not is_admin:
        _send(client, gid, f"❌ @{sender_username} ye Admin-only command hai!")
        return True

    # ══════════════════════════════════
    #  SPAM
    # ══════════════════════════════════

    elif text_lower.startswith('/spam ') and is_admin:
        parts = raw_text.split(" ", 2)
        if len(parts) == 3:
            bot_config["target_spam"][gid] = {"username": parts[1].replace("@", ""), "message": parts[2]}
            bot_config["spam_active"][gid] = True
            _send(client, gid, f"🔥 Spam ON!\n🎯 Target: @{parts[1].replace('@', '')}")
            bump(); log_fn(f"SPAM ON target @{parts[1]} by @{sender_username}")
        else:
            _send(client, gid, "❌ Format: /spam @username message")
        return True

    elif text_lower in ['/stopspam', '!stopspam'] and is_admin:
        bot_config["spam_active"][gid] = False
        _send(client, gid, "🛑 Spam STOPPED!")
        bump(); log_fn(f"SPAM OFF by @{sender_username}"); return True

    # ══════════════════════════════════
    #  NICK — SINGLE
    # ══════════════════════════════════

    elif text_lower.startswith('/nick ') and is_admin:
        parts = raw_text.split(" ", 2)
        if len(parts) < 3:
            _send(client, gid, "❌ Format: /nick @username NickName"); return True
        target_u = parts[1].replace("@", "").lower()
        new_nick = parts[2].strip()
        user_obj = _find_user(thread, target_u)
        if not user_obj:
            _send(client, gid, f"❌ @{target_u} group me nahi mila!"); return True
        try:
            client.direct_thread_update_user_title(gid, user_obj.pk, new_nick)
            _send(client, gid, f"✅ @{target_u} ka nick → '{new_nick}'")
            log_fn(f"NICK: @{target_u} → '{new_nick}'")
        except Exception as e:
            _send(client, gid, f"⚠️ Nick set nahi hua: {str(e)[:40]}")
        bump(); return True

    # ══════════════════════════════════
    #  NICK — ALL MEMBERS
    # ══════════════════════════════════

    elif text_lower.startswith('/allnick') and is_admin:
        parts = raw_text.split(" ", 1)
        prefix = parts[1].strip() if len(parts) > 1 else "⭐"
        ok, fail = 0, 0
        _send(client, gid, f"⏳ Sab ka nick change ho raha hai...\n🏷 Prefix: {prefix}")
        for u in thread.users:
            if not _valid_user(u, client): continue
            try:
                nn = f"{prefix} {u.full_name or u.username}"
                client.direct_thread_update_user_title(gid, u.pk, nn)
                ok += 1
                log_fn(f"ALLNICK: @{u.username} → '{nn}'")
                time.sleep(random.uniform(2, 4))
            except Exception as e:
                fail += 1
                log_fn(f"ALLNICK FAIL @{u.username}: {str(e)[:30]}")
        _send(client, gid, f"✅ Done!\n✔️ Success: {ok}\n❌ Failed: {fail}")
        bump(); return True

    # ══════════════════════════════════
    #  NICK RESET
    # ══════════════════════════════════

    elif text_lower in ['/kicknick', '!kicknick'] and is_admin:
        ok = 0
        _send(client, gid, "⏳ Sab ka nick reset ho raha hai...")
        for u in thread.users:
            if not _valid_user(u, client): continue
            try:
                client.direct_thread_update_user_title(gid, u.pk, "")
                ok += 1
                time.sleep(random.uniform(1, 3))
            except:
                pass
        _RUNTIME["nick_locks"].pop(gid, None)
        _send(client, gid, f"✅ {ok} members ka nick reset!")
        bump(); log_fn(f"KICKNICK by @{sender_username}"); return True

    # ══════════════════════════════════
    #  NICK LOCK
    # ══════════════════════════════════

    elif text_lower.startswith('/locknick') and is_admin:
        parts = raw_text.split(" ", 1)
        prefix = parts[1].strip() if len(parts) > 1 else "🔒"
        lock_map = {}
        _send(client, gid, f"🔒 Nick LOCK ON!\n🏷 Prefix: {prefix}\n⏳ Setting up...")
        for u in thread.users:
            if not _valid_user(u, client): continue
            nn = f"{prefix} {u.full_name or u.username}"
            lock_map[u.pk] = nn
            try:
                client.direct_thread_update_user_title(gid, u.pk, nn)
                time.sleep(random.uniform(1, 3))
            except:
                pass
        _RUNTIME["nick_locks"][gid] = lock_map
        _send(client, gid,
              f"✅ Nick LOCKED for {len(lock_map)} members!\n"
              f"Koi change kare toh bot wapas set karega 🔒")
        bump(); log_fn(f"NICK LOCK ON prefix='{prefix}' by @{sender_username}"); return True

    elif text_lower in ['/unlocknick', '!unlocknick'] and is_admin:
        _RUNTIME["nick_locks"].pop(gid, None)
        _send(client, gid, "🔓 Nick LOCK OFF!")
        bump(); log_fn(f"NICK LOCK OFF by @{sender_username}"); return True

    # ══════════════════════════════════
    #  GROUP NAME CHANGE
    # ══════════════════════════════════

    elif text_lower.startswith('/groupname ') and is_admin:
        parts = raw_text.split(" ", 1)
        new_name = parts[1].strip() if len(parts) > 1 else ""
        if not new_name:
            _send(client, gid, "❌ Format: /groupname NewGroupName"); return True
        try:
            client.direct_thread_update_title(gid, new_name)
            _send(client, gid, f"✅ Group naam change ho gaya!\n📝 Naya naam: {new_name}")
            log_fn(f"GROUP NAME: '{new_name}' by @{sender_username}")
        except Exception as e:
            _send(client, gid, f"⚠️ Naam change nahi hua: {str(e)[:50]}")
        bump(); return True

    # ══════════════════════════════════
    #  GROUP NAME LOCK
    # ══════════════════════════════════

    elif text_lower.startswith('/lockname ') and is_admin:
        parts = raw_text.split(" ", 1)
        lock_n = parts[1].strip() if len(parts) > 1 else ""
        if not lock_n:
            _send(client, gid, "❌ Format: /lockname GroupName"); return True
        try:
            client.direct_thread_update_title(gid, lock_n)
        except:
            pass
        _RUNTIME["name_locks"][gid] = lock_n
        _send(client, gid,
              f"🔒 Group naam LOCKED!\n"
              f"📝 Locked naam: {lock_n}\n"
              f"Koi badlega toh bot wapas set karega.")
        bump(); log_fn(f"NAME LOCK: '{lock_n}' by @{sender_username}"); return True

    elif text_lower in ['/unlockname', '!unlockname'] and is_admin:
        _RUNTIME["name_locks"].pop(gid, None)
        _send(client, gid, "🔓 Group naam LOCK OFF!")
        bump(); log_fn(f"NAME LOCK OFF by @{sender_username}"); return True

    # ══════════════════════════════════
    #  IMAGE SET + SEND
    # ══════════════════════════════════

    elif text_lower.startswith('/setimage ') and is_admin:
        parts = raw_text.split(" ", 1)
        url = parts[1].strip() if len(parts) > 1 else ""
        if not url.startswith("http"):
            _send(client, gid, "❌ Format: /setimage https://your-image-url.jpg"); return True
        _RUNTIME["image_urls"][gid] = url
        _send(client, gid,
              f"✅ Image set ho gaya!\n"
              f"🖼 URL saved\n"
              f"/sendimage se group me bhejo")
        bump(); log_fn(f"IMAGE SET by @{sender_username}"); return True

    elif text_lower in ['/sendimage', '!sendimage'] and is_admin:
        url = _RUNTIME["image_urls"].get(gid, DEFAULT_IMAGE_URL)
        if "your_image" in url:
            _send(client, gid, "❌ Pehle /setimage URL se image set karo!"); return True
        try:
            import tempfile, os
            suffix = ".jpg"
            if ".png" in url.lower():
                suffix = ".png"
            elif ".gif" in url.lower():
                suffix = ".gif"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0'}
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                with open(tmp_path, 'wb') as f:
                    f.write(resp.read())
            client.direct_send_photo(tmp_path, thread_ids=[gid])
            os.unlink(tmp_path)
            stats['messages_sent'] = stats.get('messages_sent', 0) + 1
            log_fn(f"IMAGE SENT by @{sender_username}")
            bump()
        except Exception as e:
            _send(client, gid,
                  f"⚠️ Image send nahi hua!\n"
                  f"Error: {str(e)[:60]}\n"
                  f"💡 Direct image URL use karo (imgur, etc)")
        return True

    # ══════════════════════════════════
    #  YOUTUBE VIDEO SEND
    # ══════════════════════════════════

    elif text_lower.startswith('/yt ') and is_admin:
        parts = raw_text.split(" ", 1)
        query = parts[1].strip() if len(parts) > 1 else ""
        if not query:
            _send(client, gid, "❌ Format: /yt VideoTitle"); return True
        _send(client, gid, f"🔍 YouTube pe '{query}' search ho raha hai...")
        try:
            video_id, title = _yt_search(query)
            if video_id:
                msg = (
                    f"🎬 YouTube Result:\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📺 {title}\n"
                    f"▶️ https://youtu.be/{video_id}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"🔗 https://www.youtube.com/watch?v={video_id}"
                )
                _send(client, gid, msg)
                log_fn(f"YT: '{title}' sent by @{sender_username}")
            else:
                _send(client, gid, f"❌ '{query}' YouTube pe nahi mila!")
        except Exception as e:
            _send(client, gid, f"⚠️ YouTube search error: {str(e)[:50]}")
        bump(); return True

    # ══════════════════════════════════
    #  AUTO REPLY — ON / OFF
    # ══════════════════════════════════

    elif text_lower.startswith('/autoreply') and is_admin:
        parts = raw_text.split(" ", 1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        if arg == "on":
            _RUNTIME["auto_reply"][gid] = True
            msgs = _RUNTIME["reply_msgs"].get(gid, DEFAULT_AUTO_REPLIES)
            _send(client, gid,
                  f"✅ Auto Reply ON!\n"
                  f"📝 Messages set: {len(msgs)}\n"
                  f"Change karne ke liye: /setreply msg1|msg2|msg3")
        elif arg == "off":
            _RUNTIME["auto_reply"][gid] = False
            _send(client, gid, "🔕 Auto Reply OFF!")
        else:
            state = "ON ✅" if _RUNTIME["auto_reply"].get(gid) else "OFF 🔕"
            _send(client, gid, f"Auto Reply abhi: {state}\n/autoreply on  ya  /autoreply off")
        bump(); log_fn(f"AUTOREPLY {arg} by @{sender_username}"); return True

    elif text_lower.startswith('/setreply') and is_admin:
        parts = raw_text.split(" ", 1)
        if len(parts) < 2:
            _send(client, gid, "❌ Format: /setreply msg1|msg2|msg3"); return True
        msgs = [m.strip() for m in parts[1].split("|") if m.strip()]
        if not msgs:
            _send(client, gid, "❌ Kam se kam 1 message chahiye!"); return True
        _RUNTIME["reply_msgs"][gid] = msgs
        preview = "\n".join([f"{i+1}. {m}" for i, m in enumerate(msgs[:5])])
        _send(client, gid,
              f"✅ Auto Reply messages set!\n"
              f"📝 Total: {len(msgs)}\n\n{preview}")
        bump(); log_fn(f"SETREPLY: {len(msgs)} msgs by @{sender_username}"); return True

    return False  # No command matched


# ─────────────────────────────────────────────────────────────
#  AUTO REPLY HANDLER
# ─────────────────────────────────────────────────────────────
def handle_auto_reply(msg_obj, sender, thread, gid, client, stats, admin_ids, log_fn):
    """Jab koi non-admin message kare aur auto reply ON ho."""
    if not _RUNTIME["auto_reply"].get(gid, False):
        return

    sender_username = (sender.username or "").lower() if hasattr(sender, 'username') else ""
    is_admin = sender_username in [a.lower().replace("@", "") for a in admin_ids] if admin_ids else False

    if is_admin:
        return

    # 2 minute cooldown per user
    now = time.time()
    cooldowns = _RUNTIME["reply_cooldown"].setdefault(gid, {})
    if now - cooldowns.get(msg_obj.user_id, 0) < 120:
        return

    msgs = _RUNTIME["reply_msgs"].get(gid, DEFAULT_AUTO_REPLIES)
    chosen = random.choice(msgs)
    reply = f"@{sender_username} {chosen}" if sender_username else chosen

    try:
        client.direct_send(reply, thread_ids=[gid])
        stats['messages_sent'] = stats.get('messages_sent', 0) + 1
        cooldowns[msg_obj.user_id] = now
        log_fn(f"AUTO REPLY → @{sender_username}: '{chosen[:40]}'")
    except Exception as e:
        log_fn(f"AUTO REPLY FAIL: {str(e)[:40]}")


# ─────────────────────────────────────────────────────────────
#  NICK LOCK ENFORCEMENT
# ─────────────────────────────────────────────────────────────
def enforce_nick_locks(gid, thread, client, log_fn):
    """Agar nick lock ON hai toh kisi ne change kiya toh wapas set karo."""
    lock_map = _RUNTIME["nick_locks"].get(gid)
    if not lock_map:
        return
    for u in thread.users:
        if not hasattr(u, 'pk'):
            continue
        target_nick = lock_map.get(u.pk)
        if not target_nick:
            continue
        try:
            current = getattr(u, 'title', None) or ""
            if current != target_nick:
                client.direct_thread_update_user_title(gid, u.pk, target_nick)
                log_fn(f"NICK RE-LOCK: @{getattr(u, 'username', '?')} → '{target_nick}'")
                time.sleep(random.uniform(1, 2))
        except:
            pass


# ─────────────────────────────────────────────────────────────
#  GROUP NAME LOCK ENFORCEMENT
# ─────────────────────────────────────────────────────────────
def enforce_name_lock(gid, thread, client, log_fn):
    """Agar group name lock ON hai toh wapas set karo."""
    locked_name = _RUNTIME["name_locks"].get(gid)
    if not locked_name:
        return
    try:
        current_name = getattr(thread, 'thread_title', None) or ""
        if current_name and current_name != locked_name:
            client.direct_thread_update_title(gid, locked_name)
            log_fn(f"NAME RE-LOCK: '{current_name}' → '{locked_name}'")
    except:
        pass


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────
def _send(client, gid, message):
    try:
        client.direct_send(message, thread_ids=[gid])
    except Exception as e:
        print(f"[SEND ERROR] {str(e)[:60]}")


def _find_user(thread, username_lower):
    for u in thread.users:
        if hasattr(u, 'username') and u.username.lower() == username_lower:
            return u
    return None


def _valid_user(u, client):
    if not hasattr(u, 'username') or not u.username:
        return False
    if u.pk == client.user_id:
        return False
    return True


def _yt_search(query):
    """YouTube search without API key. Returns (video_id, title)."""
    try:
        q = urllib.parse.quote_plus(query)
        url = f"https://www.youtube.com/results?search_query={q}"
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode('utf-8', errors='ignore')

        ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
        titles = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"', html)

        if ids and titles:
            return ids[0], titles[0]
        elif ids:
            return ids[0], f"YouTube Video"
    except Exception as e:
        print(f"[YT SEARCH ERROR] {str(e)[:60]}")
    return None, None
