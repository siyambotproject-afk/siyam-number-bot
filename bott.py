import asyncio
import io
import re
import json
import html
import os
import httpx
import random
import string
import time
import struct
import hmac
import hashlib
import base64
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from telegram.request import HTTPXRequest

# Premium copy button (PTB 21+)
try:
    from telegram import CopyTextButton
    HAS_COPY_BTN = True
except ImportError:
    HAS_COPY_BTN = False

# ==================== FIREBASE-BACKED STORAGE (optional, secrets via env) ====================
# All the load_X()/save_X() functions below (users, settings, stats, bans,
# referrals, withdraws, ranges, provider stats) funnel through this one
# generic layer. When Firebase is configured (USE_FIREBASE=true + valid
# credentials), every one of them transparently reads/writes a Firestore
# document instead of a local JSON file — same dict-shaped data, same
# call signatures, nothing else in the bot needs to change. If Firebase
# isn't configured, or firebase-admin isn't installed, or a Firestore call
# fails for any reason, it falls back to the local JSON file automatically.

USE_FIREBASE = os.environ.get("USE_FIREBASE", "").strip().lower() in ("1", "true", "yes")

try:
    import firebase_admin
    from firebase_admin import credentials as _fb_credentials, firestore as _fb_firestore
    _FIREBASE_SDK_AVAILABLE = True
except ImportError:
    _FIREBASE_SDK_AVAILABLE = False

_firestore_db = None
_firestore_init_failed = False
FIRESTORE_COLLECTION = "bot_state"

def _init_firestore():
    """Lazily initializes the Firestore client from env-provided credentials.
    Returns the client, or None if Firebase isn't usable (missing package,
    missing/invalid credentials, disabled) — callers must handle None by
    falling back to local files."""
    global _firestore_db, _firestore_init_failed
    if _firestore_db is not None:
        return _firestore_db
    if _firestore_init_failed or not USE_FIREBASE or not _FIREBASE_SDK_AVAILABLE:
        return None
    cred_json = (os.environ.get("FIREBASE_CREDENTIALS_JSON", "") or os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")).strip()
    cred_path = os.environ.get("FIREBASE_CREDENTIALS_PATH", "").strip()
    try:
        if cred_json:
            cred_dict = json.loads(cred_json)
            cred = _fb_credentials.Certificate(cred_dict)
        elif cred_path:
            cred = _fb_credentials.Certificate(cred_path)
        else:
            print("[firebase] USE_FIREBASE is on but no FIREBASE_CREDENTIALS_JSON/PATH is set — falling back to local files.")
            _firestore_init_failed = True
            return None
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        _firestore_db = _fb_firestore.client()
        print("[firebase] Firestore initialized — bot data will be stored in Firebase.")
        return _firestore_db
    except Exception as e:
        print(f"[firebase] initialization failed, falling back to local files: {e}")
        _firestore_init_failed = True
        return None

def _storage_key(filename):
    """Turns a local filename like 'users.json' into a Firestore doc id."""
    return os.path.splitext(os.path.basename(str(filename)))[0]

def _generic_load(filename, default):
    """Reads one logical data store (e.g. users, settings, stats) — from
    Firestore if configured and reachable, otherwise from its local JSON
    file. `default` is returned (and NOT persisted) if nothing exists yet."""
    db = _init_firestore()
    if db is not None:
        try:
            doc = db.collection(FIRESTORE_COLLECTION).document(_storage_key(filename)).get()
            if doc.exists:
                payload = doc.to_dict().get("payload", default)
                return payload
            return default
        except Exception as e:
            print(f"[firebase] load '{filename}' failed, falling back to local file: {e}")
    # Local-file fallback (identical to the bot's original behavior)
    if not os.path.exists(filename):
        try:
            with open(filename, "w") as f:
                json.dump(default, f)
        except Exception:
            pass
        return default
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except Exception:
        return default

def _generic_save(filename, data):
    """Writes one logical data store — to Firestore if configured and
    reachable, otherwise to its local JSON file. Always also keeps trying
    the intended backend on every call (no silent permanent downgrade)."""
    db = _init_firestore()
    if db is not None:
        try:
            db.collection(FIRESTORE_COLLECTION).document(_storage_key(filename)).set({"payload": data})
            return
        except Exception as e:
            print(f"[firebase] save '{filename}' failed, falling back to local file: {e}")
    try:
        with open(filename, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"[storage] local save '{filename}' failed: {e}")

# ==================== CONFIG SECTION ====================
# SECURITY: secrets (bot token, Firebase credentials) are read from
# environment variables — never hardcoded here. Set them on your host
# (BOTIFYHOST env settings, a .env loader, systemd EnvironmentFile, etc.):
#   BOT_TOKEN                  — your Telegram bot token from @BotFather
#   USE_FIREBASE               — "true" to store all bot data in Firebase
#                                  Firestore instead of local JSON files
#   FIREBASE_CREDENTIALS_JSON  — the ENTIRE contents of your Firebase
#                                  service-account JSON key, as one string
#   FIREBASE_CREDENTIALS_PATH  — (alternative to the above) a file path on
#                                  the host pointing to that JSON key file
#
# If BOT_TOKEN isn't set, the bot refuses to start (no fallback — never
# ship a real token in code). If Firebase isn't configured/available, data
# storage transparently falls back to local JSON files, exactly as before.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
USER_DATA_FILE = "users.json"
PAID_SMS_FILE = "paid_sms.json"
STATS_FILE = "user_stats.json"
PROVIDER_STATS_FILE = "provider_stats.json"
REFERRAL_DATA_FILE = "referral_data.json"
BANNED_USERS_FILE = "banned_users.json"
WITHDRAW_DATA_FILE = "withdraw_requests.json"
ACTIVITY_LOGS_FILE = "activity_logs.json"
DATA_RANGE_FILE = "datarange.json"
SETTINGS_FILE = "settings.json"

BOT_USERNAME = None  # set at runtime via post_init

# ==================== MULTIPLE ADMINS CONFIGURATION ====================
ADMINS = [7753282667]

OTP_GROUP_ID = -1003941189680

# ==================== PREMIUM EMOJI MAPPING ====================

EMOJI_ID_MAP = {
    "telegram": "5271801931814165886", "instagram": "5269682734820777950",
    "facebook": "5269427536453984598", "tiktok": "5271527792641595125",
    "x": "5269500885905468781",        "whatsapp": "5271536803482981220",
    "up": "5244837092042750681",        "down": "5246762912428603768",
    "add": "5397916757333654639",       "setting": "5341715473882955310",
    "1st": "5440539497383087970",       "2st": "5447203607294265305",
    "3rd": "5453902265922376865",       "free": "5406756500108501710",
    "msg": "5253742260054409879",       "link": "5271604874419647061",
    "status": "5231200819986047254",    "home": "5416041192905265756",
    "gift_box": "5970074171449808121",  "delete": "5422557736330106570",
    "number_change": "5267295703666824255", "refer_btn": "5420396762189831222",
    "get_number_btn": "5375338737028841420", "cross": "5420130255174145507",
    "stop": "5956074558044770726",      "ban": "5420323339723881652",
    "loading": "5386367538735104399",   "profile": "5352861489541714456",
    "done": "6298670698948724690",      "otp_success": "5190781475468915802",
    "nagad": "5352985330628730418",     "bkash": "5348469219761626211",
    "rocket": "5346042941196507141",    "binance": "5348212415077064131",
    "live": "5355102594886833928",      "developer": "5267294466716244344",
    "channel": "6215074610845585917",   "copy": "5429483843541284898",
    "admin": "5350396951407895212",     "waiting": "6217721388736712699",
    "back": "5267490665117275176",      "leader_board": "5280769763398671636",
    "money": "6233367447789899509",     "discord": "5807892405306791778",
    "custom_range": "5231012545799666522", "paypal": "5107533253946901363",
    "imo": "5337155807752524558",       "hi": "5353027129250453493",
    "off": "5352974971167611327",       "diamond": "5251562950698759162",
    "broadcast": "5251671501702196837", "key": "5296369303661067030",
    "bot_logo": "4943094697238201446",  "tk": "5201873447554145566",
    "roket": "5188481279963715781",     "all_done": "5859265295113261399",
    "yes": "5875017993909440887",       "no": "5875005555684152921",
    "fire_love": "5864093929275658617",
}

FALLBACK_EMOJI_MAP = {
    "telegram": "\u2708\ufe0f", "instagram": "\U0001f4f7", "facebook": "\U0001f535",
    "tiktok": "\U0001f3b5", "x": "\u274c", "whatsapp": "\U0001f4ac",
    "up": "\U0001f4c8", "down": "\U0001f4c9", "add": "\u2795", "setting": "\u2699\ufe0f",
    "1st": "\U0001f947", "2st": "\U0001f948", "3rd": "\U0001f949", "free": "\U0001f193",
    "msg": "\U0001f4ac", "link": "\U0001f517", "status": "\U0001f4ca", "home": "\U0001f3e0",
    "gift_box": "\U0001f381", "delete": "\U0001f5d1\ufe0f", "number_change": "\U0001f504",
    "get_number_btn": "\U0001f4de", "cross": "\u274c", "stop": "\U0001f6d1",
    "ban": "\U0001f6ab", "loading": "\u23f3", "profile": "\U0001f464", "done": "\u2705",
    "otp_success": "\u2b50", "nagad": "\U0001f7e0", "bkash": "\U0001f496",
    "rocket": "\U0001f680", "binance": "\U0001f7e1", "live": "\U0001f7e2",
    "developer": "\U0001f468\u200d\U0001f4bb", "channel": "\U0001f4e2",
    "copy": "\U0001f4cb", "admin": "\U0001f451", "waiting": "\u23f3", "back": "\U0001f519",
    "leader_board": "\U0001f3c6", "custom_range": "\U0001f3af", "refer_btn": "\U0001f517",
    "paypal": "\U0001f4b3", "imo": "\U0001f4ac", "hi": "\U0001f44b", "off": "\U0001f6d1",
    "diamond": "\U0001f48e", "broadcast": "\U0001f4e2", "key": "\U0001f511", "tk": "\u09f3",
    "roket": "\U0001f680", "all_done": "\u2705", "yes": "\u2714\ufe0f", "no": "\u274c",
    "fire_love": "\u2764\ufe0f",
}

PREMIUM_FLAGS = {
    "\U0001f1fa\U0001f1f8": "5913463998522592692",
    "\U0001f1fa\U0001f1e6": "5911406692007941050",
    "\U0001f1f5\U0001f1f1": "5913550391789752571",
    "\U0001f1f0\U0001f1ff": "5913724621433082323",
    "\U0001f1e6\U0001f1ff": "5911197578640233518",
    "\U0001f1ea\U0001f1fa": "5911106310585193018",
    "\U0001f1e6\U0001f1f2": "5913272455866093666",
    "\U0001f1f7\U0001f1fa": "5913274246867456342",
    "\U0001f1fa\U0001f1ff": "5911051846104912282",
    "\U0001f1e9\U0001f1ea": "5911096835887337583",
    "\U0001f1ef\U0001f1f5": "5913293711659241040",
    "\U0001f1f9\U0001f1f7": "5910995113881901195",
    "\U0001f1e7\U0001f1fe": "5911011185649521599",
    "\U0001f1ec\U0001f1e7": "5913443365499703513",
    "\U0001f1ee\U0001f1f3": "5913754823643107921",
    "\U0001f1e7\U0001f1f7": "5911148568768418614",
    "\U0001f1fe\U0001f1ea": "5913346492512341993",
    "\U0001f1fb\U0001f1f3": "5913428887164949581",
    "\U0001f1e6\U0001f1ea": "5913726554168365343",
    "\U0001f1fa\U0001f1ec": "5913488939397681980",
    "\U0001f1f9\U0001f1f2": "5913315521503170180",
    "\U0001f1f9\U0001f1f3": "5911332947419468671",
    "\U0001f1f9\U0001f1ed": "5913617968805187987",
    "\U0001f1f9\U0001f1ff": "5911418949844603556",
    "\U0001f1f8\U0001f1ea": "5911156510162949403",
    "\U0001f1f8\U0001f1e9": "5911387497799094470",
    "\U0001f1ea\U0001f1f8": "5911193287967904547",
    "\U0001f1f1\U0001f1f0": "5911293163137406640",
    "\U0001f1ff\U0001f1e6": "5911203119148044594",
    "\U0001f1f8\U0001f1ec": "5911531460808051849",
    "\U0001f1f7\U0001f1f8": "5913592598433369871",
    "\U0001f1f8\U0001f1f3": "5910995302860461643",
    "\U0001f1f6\U0001f1e6": "5911260864983339619",
    "\U0001f1f5\U0001f1f9": "5911023653939581472",
    "\U0001f1f5\U0001f1ed": "5911268638874145162",
    "\U0001f1f5\U0001f1ea": "5911207993935925780",
    "\U0001f1f5\U0001f1f0": "5913705895375672082",
    "\U0001f1f4\U0001f1f2": "5913570801474343473",
    "\U0001f1f3\U0001f1f4": "5913617397574537046",
    "\U0001f1f3\U0001f1ec": "5911143844304393105",
    "\U0001f1f3\U0001f1ff": "5913640044937089340",
    "\U0001f1f3\U0001f1f1": "5913367645226275100",
    "\U0001f1f3\U0001f1f5": "5913496520014958723",
    "\U0001f1f2\U0001f1e6": "5911482111633658301",
    "\U0001f1f2\U0001f1f3": "5911041383564580038",
    "\U0001f1f2\U0001f1e9": "5913456847402045950",
    "\U0001f1f2\U0001f1fd": "5913687302462246518",
    "\U0001f1f2\U0001f1fe": "5913654360063087453",
    "\U0001f1f0\U0001f1ea": "5911154710571651231",
    "\U0001f1f1\U0001f1f9": "5911172315642597775",
    "\U0001f1f1\U0001f1fb": "5913738489882480243",
    "\U0001f1f1\U0001f1e7": "5911504273664905447",
    "\U0001f1ee\U0001f1e9": "5913479361620611038",
    "\U0001f1ee\U0001f1f7": "5911308891307643032",
    "\U0001f1ee\U0001f1f6": "5911382442622587735",
    "\U0001f1ee\U0001f1f1": "5911471936856134692",
    "\U0001f1ee\U0001f1f9": "5913688444923547525",
    "\U0001f1ed\U0001f1fa": "5913767635530551104",
    "\U0001f1ec\U0001f1f7": "5911210399117611448",
    "\U0001f1ec\U0001f1ed": "5913391155877252952",
    "\U0001f1ec\U0001f1ea": "5913434771270144023",
    "\U0001f1eb\U0001f1f7": "5913605586414473124",
    "\U0001f1eb\U0001f1ee": "5911041344909873378",
    "\U0001f1ea\U0001f1f9": "5911078333168227043",
    "\U0001f1ea\U0001f1ec": "5913694831539916769",
    "\U0001f1e9\U0001f1f0": "5911206009661034712",
    "\U0001f1e8\U0001f1fe": "5911023550860366409",
    "\U0001f1ed\U0001f1f7": "5913692684056269311",
    "\U0001f1e8\U0001f1f3": "5913779335021466780",
    "\U0001f1e7\U0001f1e9": "5911365056594973179",
    "\U0001f1e7\U0001f1ea": "5913529642802745141",
    "\U0001f1e6\U0001f1fa": "5913632326880858455",
    "\U0001f1e6\U0001f1f9": "5911338831524664592",
    "\U0001f1e6\U0001f1f7": "5913573356979884082",
    "\U0001f1f0\U0001f1f7": "5913371673905598425",
    "\U0001f1f8\U0001f1e6": "4985897134424328239",
    "\U0001f1e8\U0001f1f4": "5913773060074246009",
    "\U0001f1e8\U0001f1f1": "5911470957603592832",
    "\U0001f1ff\U0001f1f2": "5913564754160389778",
}

def get_tg_emoji(key, default_char=""):
    emoji_id = EMOJI_ID_MAP.get(key)
    fallback = default_char or FALLBACK_EMOJI_MAP.get(key, "\u2b50")
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback

def get_country_tg_flag(flag_emoji):
    emoji_id = PREMIUM_FLAGS.get(flag_emoji)
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{flag_emoji}</tg-emoji>'
    return flag_emoji

def _strip_tg_emoji_tags(html_text):
    """Removes <tg-emoji emoji-id="...">X</tg-emoji> wrappers, keeping just
    the plain fallback character X. Used as a safety-net retry: if Telegram
    ever rejects a message because a custom-emoji id is invalid/unavailable,
    we resend the same message in plain-unicode form instead of losing it."""
    return re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', html_text)

async def send_with_emoji_fallback(send_coro_factory, html_text):
    """Calls send_coro_factory(text) with the given HTML text; if it fails
    (e.g. an invalid/unavailable custom emoji id, or any other send error),
    retries once with all <tg-emoji> tags stripped down to their plain
    fallback characters, so a single bad emoji id never silently drops
    the message. Both the first and second failure are logged so a
    send that keeps failing (e.g. bad reply_markup, chat not found,
    bot blocked) is never silent."""
    try:
        return await send_coro_factory(html_text)
    except Exception as e:
        plain = _strip_tg_emoji_tags(html_text)
        if plain != html_text:
            print(f"[emoji-fallback] first send failed, retrying without custom emoji: {e}")
            try:
                return await send_coro_factory(plain)
            except Exception as e2:
                print(f"[emoji-fallback] send failed even without custom emoji: {e2}")
        else:
            print(f"[emoji-fallback] send failed (no tg-emoji to strip): {e}")
    return None

# ==================== SYSTEM DYNAMIC SETTINGS ====================

_settings_cache = None

def load_settings():
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache
    default = {
        "active_panel": "zenex",
        "zenex_api_key": "ZNX_IQ52ED851U09ZAZL062U26GL",
        "zenex_base_url": "https://api.zenexnetwork.com",
        "panel_url": "https://t.me/Zenex_Number_bot?start=8346777366",
        "allowed_services": ["Instagram","Facebook","WhatsApp","TikTok","Telegram","Discord","PayPal","Imo"],
        "otp_group_url": "https://t.me/+31eV11IT7WQzMjI9",
        "channel_url": "https://t.me/MinoXofficial0",
        "support_username": "@support",
        "maintenance_mode": False,
        "cooldown_time": 1.0,
        "min_withdraw": 50.0,
        "otp_bonus": 0.20,
        "referral_bonus": 0.0,
        "admins": ADMINS.copy(),
        "owners": ADMINS.copy(),
        "otp_group_chat_id": None,
        "range_broadcast_chat_id": None,
        "manual_top_hit_ranges": [],
        "force_join_channel": None,
        "force_join_channels": [],
        "developer_link": "https://t.me/Davil_Raju",
        "support_link": "https://t.me/DEM_Support_Chat",
        "otp_group_btn_label": "📢 OTP GROUP",
        "panel_btn_label": "📱 NUMBER PANEL",
        "channel_btn_label": "📢 MAIN CHANNEL",
        "support_btn_label": "💬 SUPPORT",
        "developer_btn_label": "👨‍💻 DEVELOPER BY",
        "manual_services": [],
        "active_provider": "zenex",
        "enabled_providers": [],
        "providers": {
            "zenex": {
                "name": "Zenex",
                "base_url": "https://api.zenexnetwork.com",
                "api_key": "ZNX_IQ52ED851U09ZAZL062U26GL",
                "auth_type": "header",          # "header" or "query"
                "auth_key": "mapikey",
                "getnum_method": "POST",
                "getnum_path": "/v1/getnum",
                "getnum_range_field": "range",
                "getnum_extra_body": {"is_national": False, "remove_plus": False},
                "number_fields": ["full_number", "no_plus_number", "copy", "number"],
                "ranges_path": "/v1/active-ranges",
                "otp_path": "/v1/numsuccess/info",
            }
        },
    }
    data = _generic_load(SETTINGS_FILE, None)
    if data is None:
        _generic_save(SETTINGS_FILE, default)
        _settings_cache = default
        return default
    try:
        updated = False
        for k, v in default.items():
            if k not in data:
                data[k] = v; updated = True
        # One-time migration: fold the old single force_join_channel into the
        # new multi-channel list, so existing setups keep working.
        legacy = data.get("force_join_channel")
        if legacy and legacy not in (data.get("force_join_channels") or []):
            data.setdefault("force_join_channels", []).append(legacy)
            updated = True
        if updated:
            _generic_save(SETTINGS_FILE, data)
        _settings_cache = data
        return data
    except Exception:
        _settings_cache = default
        return default

def save_settings(settings):
    global _settings_cache
    _settings_cache = settings
    _generic_save(SETTINGS_FILE, settings)

def clean_base_url(url):
    url = str(url).strip().rstrip('/')
    url = re.sub(r'(/v1|/api|/api/v1)$', '', url)
    return url.rstrip('/')

def get_active_provider():
    """Returns the config dict of the currently active provider (any panel: Zenex, miahsms, etc.).
    Used as the single-panel fallback when multi-panel mode has 0-1 providers enabled."""
    s = load_settings()
    providers = s.get("providers", {})
    active_id = s.get("active_provider", "zenex")
    if active_id in providers:
        return providers[active_id]
    if providers:
        return next(iter(providers.values()))
    return {
        "name": "None", "base_url": "", "api_key": "",
        "auth_type": "header", "auth_key": "mapikey",
        "getnum_method": "POST", "getnum_path": "/v1/getnum",
        "getnum_range_field": "range", "getnum_extra_body": {},
        "number_fields": ["number"], "ranges_path": "", "otp_path": "",
    }

# ==================== MULTI-PANEL MODE ====================
# When 2+ providers are in settings["enabled_providers"], the bot rotates
# between them for GET NUMBER (round-robin, with automatic fallback to the
# next panel if one is out of stock/down) and polls ALL of them for
# incoming OTPs — so numbers can come from different panels each time,
# different users can land on different panels, and OTPs are still caught
# regardless of which panel issued the number. With 0 or 1 enabled, it
# behaves exactly like single-panel mode (active_provider), unchanged.

_provider_round_robin = {"i": 0}

def get_enabled_provider_ids():
    """Returns the ordered list of provider ids currently in rotation.
    Falls back to the single active_provider (or the first provider that
    exists) when multi-panel mode isn't configured — this is what keeps
    single-panel operation working exactly as before."""
    s = load_settings()
    providers = s.get("providers", {})
    enabled = [pid for pid in (s.get("enabled_providers") or []) if pid in providers]
    if enabled:
        return enabled
    active = s.get("active_provider")
    if active and active in providers:
        return [active]
    return list(providers.keys())

def get_provider_by_id(pid):
    return load_settings().get("providers", {}).get(pid)

def get_rotated_provider_ids():
    """Same set of enabled providers, but rotated by one step each call —
    this is what spreads GET NUMBER requests across panels over time."""
    ids = get_enabled_provider_ids()
    if not ids:
        return []
    start = _provider_round_robin["i"] % len(ids)
    _provider_round_robin["i"] += 1
    return ids[start:] + ids[:start]

def _provider_base(provider):
    return str(provider.get("base_url", "")).strip().rstrip("/")

def _provider_auth(provider, params, headers):
    """Applies auth to either query params or headers, based on provider config."""
    api_key = str(provider.get("api_key", "")).strip()
    auth_key = provider.get("auth_key", "mapikey")
    if provider.get("auth_type", "header") == "query":
        params[auth_key] = api_key
    else:
        headers[auth_key] = api_key
    return params, headers

def _dig(obj, *field_names):
    """Try a list of possible field/dot-path names on a dict and return the first hit."""
    for f in field_names:
        cur = obj
        ok = True
        for part in f.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None

def _deep_find_number(payload, number_fields):
    """Recursively search any nested dict/list for a number field, in case a
    provider's response is shaped differently than expected. This NEVER
    overrides a direct match from _dig() — it's only a fallback so an
    unusual response shape doesn't cause a false 'no number' failure."""
    if isinstance(payload, dict):
        for key in number_fields:
            value = payload.get(key)
            if isinstance(value, (str, int, float)):
                normalized = normalize_number(value)
                if len(normalized) >= 7:
                    return value
        for value in payload.values():
            found = _deep_find_number(value, number_fields)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _deep_find_number(value, number_fields)
            if found:
                return found
    return None

def _deep_find_ranges(payload):
    """Flexible parser for an 'active ranges' style response — tries every
    known key/nesting shape before giving up. Falls back gracefully so a
    provider's slightly different JSON shape still works."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("active_ranges", "activeRanges", "available_ranges",
                "availableRanges", "ranges", "range_list", "services"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    for key in ("data", "result", "response", "payload"):
        value = payload.get(key)
        if isinstance(value, (dict, list)):
            nested = _deep_find_ranges(value)
            if nested is not None:
                return nested
    # A few panels return {"WhatsApp": ["880XXX", ...], "Telegram": [...]}
    grouped = []
    for service, values in payload.items():
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("service", service)
            else:
                item = {"service": service, "range": value}
            grouped.append(item)
    return grouped or None

def load_provider_stats():
    return _generic_load(PROVIDER_STATS_FILE, {})

def save_provider_stats(stats):
    _generic_save(PROVIDER_STATS_FILE, stats)

def record_provider_event(provider_id, kind):
    """Admin-only usage tracking: which panel is issuing numbers / getting
    OTPs, and how often. kind is 'number' or 'otp'. Safe to call with a
    missing/unknown provider_id (older data before this tracking existed) —
    just does nothing in that case."""
    if not provider_id:
        return
    stats = load_provider_stats()
    today = datetime.now().strftime("%Y-%m-%d")
    entry = stats.setdefault(provider_id, {
        "numbers_issued": 0, "otps_received": 0,
        "today_date": today, "today_numbers": 0, "today_otps": 0,
        "last_used": None,
    })
    if entry.get("today_date") != today:
        entry["today_date"] = today
        entry["today_numbers"] = 0
        entry["today_otps"] = 0
    if kind == "number":
        entry["numbers_issued"] = entry.get("numbers_issued", 0) + 1
        entry["today_numbers"] = entry.get("today_numbers", 0) + 1
    elif kind == "otp":
        entry["otps_received"] = entry.get("otps_received", 0) + 1
        entry["today_otps"] = entry.get("today_otps", 0) + 1
    entry["last_used"] = datetime.now().isoformat()
    save_provider_stats(stats)

async def register_active_number(uid, number, range_text, app_name=None, provider_id=None):
    """Reserve a number once so two concurrent requests can never both claim
    ownership of the same number (fixes a race condition where the API
    occasionally hands out a number that's already being processed)."""
    clean_num = normalize_number(number)
    if len(clean_num) < 7:
        return None
    async with number_assignment_lock:
        if clean_num in active_numbers:
            return None
        active_numbers[clean_num] = {
            "uid": uid, "range": str(range_text).strip().upper(),
            "app": app_name or "", "timestamp": datetime.now(),
            "provider_id": provider_id or "",
        }
        save_number_range_info(uid, clean_num, range_text)
    record_range_hit(range_text, app_name)
    record_provider_event(provider_id, "number")
    return clean_num

# ==================== WELCOME MESSAGE CONFIGURATION ====================
WELCOME_MESSAGE = """✨ 𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 RJ RONY TEST BOT🚀 ✨ 
━━━━━━━━━━━━━━━━━━━━━━
🚀 Enjoy Premium Quality Service 🚀"""

# ==================== OTP RATE CONFIGURATION ====================
OTP_RATE = 0.20

# ==================== REFERRAL / WITHDRAW CONFIGURATION ====================
REFERRAL_PRICE = 0
MIN_WITHDRAW = 50
MAX_WITHDRAW = 10000

# ==================== SUPPORT & DEVELOPER LINKS ====================
SUPPORT_LINK = "https://t.me/DEM_Support_Chat"      # আপনার সাপোর্ট লিংক দিন
DEVELOPER_LINK = "https://t.me/Davil_Raju"          # আপনার ডেভেলপার লিংক দিন
DEVELOPER_NAME = "Davil Raju"                         # ডেভেলপারের নাম

request_queue = asyncio.Queue()
MAX_WORKERS = 5000

client_async = httpx.AsyncClient(
    http2=True,
    timeout=httpx.Timeout(4.0, connect=0.8, read=3.2),
    limits=httpx.Limits(max_connections=5000, max_keepalive_connections=2500),
    trust_env=False,
)

active_numbers = {}
last_range = {}
CHECK_INTERVAL = 1.5
number_assignment_lock = asyncio.Lock()

# ==================== TOP HIT RANGES TRACKING (local, always works) ====================
range_hit_log = []              # list of (datetime, range_text, app_name)
RANGE_HIT_WINDOW_MINUTES = 30

def record_range_hit(range_text, app_name=None):
    if not range_text:
        return
    range_hit_log.append((datetime.now(), str(range_text).strip().upper(), app_name or ""))
    # keep the log from growing forever — prune anything older than 4x the window
    if len(range_hit_log) > 5000:
        cutoff = datetime.now() - timedelta(minutes=RANGE_HIT_WINDOW_MINUTES * 4)
        range_hit_log[:] = [x for x in range_hit_log if x[0] > cutoff]

def get_top_hit_ranges(limit=10, minutes=RANGE_HIT_WINDOW_MINUTES):
    cutoff = datetime.now() - timedelta(minutes=minutes)
    counts, apps = {}, {}
    for ts, rng, app in range_hit_log:
        if ts >= cutoff and rng:
            counts[rng] = counts.get(rng, 0) + 1
            if app:
                apps[rng] = app

    # Merge in admin-added manual entries — these show alongside the
    # auto-tracked (real usage) hits, so admins can seed the list too.
    for entry in load_settings().get("manual_top_hit_ranges", []):
        rng = str(entry.get("range", "")).strip().upper()
        if not rng:
            continue
        hits = int(entry.get("hits", 0) or 0)
        counts[rng] = counts.get(rng, 0) + hits
        app = entry.get("app")
        if app:
            apps[rng] = app

    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [(rng, cnt, apps.get(rng, "")) for rng, cnt in top]

# ==================== ZENEXA RANGES CACHE ====================
_ranges_cache = {"data": None, "updated_at": 0.0, "fetching": False}

def get_app_emoji_id(app_name):
    n = app_name.lower()
    # Ordered: longer/more-specific names first to avoid partial-match errors
    SERVICE_EMOJI_MAP = [
        ("whatsapp business", "5336814486701514414"),
        ("whatsapp",          "5334759662677957452"),
        ("telegram",          "5337010556253543833"),
        ("facebook",          "5334807341109908955"),
        ("instagram",         "5334868205091459431"),
        ("amazon prime",      "6111801057061374810"),
        ("amazon",            "4995019580536524226"),
        ("imo",               "5337155807752524558"),
        ("apple",             "5334637951894722661"),
        ("google",            "5335010201005231986"),
        ("microsoft",         "5334880948259427772"),
        ("teams",             "5334590977837403844"),
        ("tiktok",            "5339213256001102461"),
        ("bkash",             "5348469219761626211"),
        ("bybit",             "5348372939479751825"),
        ("binance",           "5348212415077064131"),
        ("melbet",            "5337102391244263212"),
        ("snapchat",          "5359441366554255082"),
        ("uber",              "5298715455316303708"),
        ("paypal",            "5776103539872896061"),
        ("discord",           "5116246243646898866"),
        ("viber",             "5463060437572528782"),
        ("linkedin",          "6224222994265279792"),
        ("line",              "5399818044866327279"),
        ("wechat",            "5782757599560602950"),
        ("twitter",           "5215726959056662534"),
        ("reddit",            "4992421103847604984"),
        ("pinterest",         "5346103513120258857"),
        ("twitch",            "5233333563306301418"),
        ("zoom",              "5881799193219043268"),
        ("signal",            "5293998404404272267"),
        ("slack",             "4994972469040251302"),
        ("skype",             "4992613535562334989"),
        ("netflix",           "6255738712664050133"),
        ("spotify",           "5411392711146095115"),
        ("hoichoi",           "6104822598493801746"),
        ("daraz",             "5336879280578138635"),
        ("foodpanda",         "5336879280578138635"),
        ("pathao",            "5336879280578138635"),
        ("aliexpress",        "5336879280578138635"),
        ("shopee",            "5336879280578138635"),
        ("payoneer",          "5336879280578138635"),
        ("wise",              "5336879280578138635"),
        ("chatgpt",           "5296516998996445955"),
        ("notion",            "5336879280578138635"),
        ("github",            "5417836094098007862"),
        ("canva",             "5111661409008092227"),
        ("figma",             "5336879280578138635"),
        ("upwork",            "5336879280578138635"),
        ("fiverr",            "5336879280578138635"),
        ("yahoo",             "5336879280578138635"),
        ("dropbox",           "5336879280578138635"),
        ("coursera",          "5336879280578138635"),
        ("duolingo",          "5336879280578138635"),
        ("rocket",            "5346042941196507141"),
    ]
    for k, v in SERVICE_EMOJI_MAP:
        if k in n:
            return v
    return "5861680977994060034"

def get_platform_icon(platform_name):
    return f'<tg-emoji emoji-id="{get_app_emoji_id(platform_name)}">📞</tg-emoji>'

def get_clean_app_name(raw):
    n = raw.lower().strip()
    for k, v in [("facebook","Facebook"),("instagram","Instagram"),("whatsapp","WhatsApp"),
                  ("tiktok","TikTok"),("paypal","PayPal"),("telegram","Telegram"),
                  ("discord","Discord"),("imo","Imo")]:
        if k in n: return v
    return raw.strip().capitalize()

def make_bold_text(text):
    out = []
    for ch in str(text):
        o = ord(ch)
        if 65 <= o <= 90:    out.append(chr(o - 65 + 0x1D5D4))
        elif 97 <= o <= 122: out.append(chr(o - 97 + 0x1D5EE))
        elif 48 <= o <= 57:  out.append(chr(o - 48 + 0x1D7EC))
        else: out.append(ch)
    return "".join(out)

async def _fetch_raw_range_entries_for(provider):
    """Fetches and parses ONE provider's live-ranges endpoint into a flat
    list of {"range":..., "service":...} entries, with NO allowed-list
    filtering applied. Returns (entries, error)."""
    base = _provider_base(provider)
    ranges_path = provider.get("ranges_path", "")
    if not base or not ranges_path:
        return [], None  # provider doesn't support live ranges

    url = base + ranges_path
    params, headers = _provider_auth(provider, {}, {})
    ranges_list = None
    for attempt in range(2):
        try:
            r = await client_async.get(url, params=params, headers=headers,
                                        timeout=httpx.Timeout(5.0, connect=1.5))
            if r.status_code != 200:
                print(f"[active-ranges] provider={provider.get('name')} status={r.status_code} body={r.text[:300]}")
                ranges_list = None
            else:
                data = r.json()
                ranges_list = _deep_find_ranges(data)
            if ranges_list is not None: break
        except Exception as e:
            print(f"[active-ranges] provider={provider.get('name')} exception: {e}")
            if attempt == 0: await asyncio.sleep(0.2)
    if ranges_list is None: return None, "Server unreachable or invalid API key."
    if not ranges_list:     return [], None

    entries = []
    for obj in ranges_list:
        if isinstance(obj, str):
            entries.append({"range": obj, "service": "Unknown"})
        elif isinstance(obj, dict):
            service = (obj.get("service") or obj.get("app") or obj.get("application")
                       or obj.get("service_name") or obj.get("sid") or "Unknown")
            if isinstance(obj.get("ranges"), list):
                # e.g. {"sid": "WhatsApp", "ranges": ["22507XXX", "23762XXX"], ...}
                for r in obj["ranges"]:
                    entries.append({"range": r, "service": service})
            else:
                rng = obj.get("range") or obj.get("prefix") or obj.get("range_text") or obj.get("value") or ""
                entries.append({"range": rng, "service": service})
    return entries, None

async def _fetch_raw_range_entries():
    """Backward-compatible single-provider wrapper (active_provider only)."""
    return await _fetch_raw_range_entries_for(get_active_provider())

async def _fetch_raw_range_entries_all_enabled():
    """Multi-panel aware: merges live-range entries from EVERY enabled
    provider into one list, tagging each entry with which panel it came
    from. With only one provider enabled, this is equivalent to the
    single-provider version."""
    all_entries = []
    any_ok = False
    last_err = None
    for pid in get_enabled_provider_ids():
        provider = get_provider_by_id(pid)
        if not provider:
            continue
        entries, err = await _fetch_raw_range_entries_for(provider)
        if err:
            last_err = err
            continue
        any_ok = True
        for e in entries:
            e["_provider_id"] = pid
        all_entries.extend(entries)
    if not any_ok and last_err:
        return None, last_err
    return all_entries, None

async def sync_provider_services():
    """Admin-triggered: fetch live ranges fresh (bypassing cache) from ALL
    enabled panels and auto-add any service name found that isn't in the
    allowed_services list yet, so it shows up in GET NUMBER immediately."""
    entries, err = await _fetch_raw_range_entries_all_enabled()
    if err:
        return None, None, err
    found_services = set()
    for entry in entries:
        rng = str(entry.get("range", "")).strip().upper()
        if not rng or not re.search(r"\d", rng):
            continue
        clean = get_clean_app_name(str(entry.get("service", "Unknown")))
        if clean and clean != "Unknown":
            found_services.add(clean)

    s = load_settings()
    allowed = s.get("allowed_services", [])
    allowed_lower = {a.lower() for a in allowed}
    newly_added = sorted(sv for sv in found_services if sv.lower() not in allowed_lower)
    if newly_added:
        s["allowed_services"] = allowed + newly_added
        save_settings(s)

    # Force an immediate cache refresh so GET NUMBER reflects this right away
    top, _ = await fetch_top55_ranges_by_app()
    if top:
        _ranges_cache["data"] = top
        _ranges_cache["updated_at"] = time.monotonic()

    return sorted(found_services), newly_added, None

async def fetch_top55_ranges_by_app():
    settings = load_settings()
    allowed = {s.lower() for s in settings.get("allowed_services",
        ["Instagram","Facebook","WhatsApp","TikTok","Telegram","Discord","PayPal","Imo"])}

    entries, err = await _fetch_raw_range_entries_all_enabled()
    if err: return None, err
    if not entries: return {}, None

    top = {}
    for entry in entries:
        rng = str(entry.get("range", "")).strip().upper()
        service = entry.get("service", "Unknown")
        if not rng or not re.search(r"\d", rng):
            continue
        clean = get_clean_app_name(str(service))
        if clean.lower() not in allowed and clean != "Unknown": continue
        if clean not in top: top[clean] = {"icon": get_platform_icon(clean), "ranges": []}
        if rng not in top[clean]["ranges"]:
            top[clean]["ranges"].append(rng)
    return dict(sorted(top.items(), key=lambda x: len(x[1]["ranges"]), reverse=True)), None

def build_app_buttons_from_cache(top):
    btns, row, ci = [], [], 0
    clrs = ["primary", "success", "danger"]
    for app_name in top:
        row.append(InlineKeyboardButton(make_bold_text(app_name),
            callback_data=f"sel_app_{app_name}",
            api_kwargs={"icon_custom_emoji_id": get_app_emoji_id(app_name), "style": clrs[ci % 3]}))
        ci += 1
        if len(row) == 2: btns.append(row); row = []
    if row: btns.append(row)
    return btns

async def _bg_refresh_ranges():
    global _ranges_cache
    while True:
        try:
            if not _ranges_cache["fetching"]:
                _ranges_cache["fetching"] = True
                try:
                    data, _ = await fetch_top55_ranges_by_app()
                    if data:
                        _ranges_cache["data"] = data
                        _ranges_cache["updated_at"] = time.monotonic()
                except Exception: pass
                finally: _ranges_cache["fetching"] = False
        except Exception: pass
        await asyncio.sleep(200)

# ==================== MASTER RANGE LIST BROADCAST (like Zenex's own site) ====================

async def range_broadcast_loop(app):
    """Periodically posts the current active-ranges list (Master Range List)
    to an admin-configured group — mirrors the Zenex website's own live
    range feed. Multi-panel aware: pulls merged ranges from every enabled
    provider via fetch_top55_ranges_by_app()."""
    while True:
        try:
            s = load_settings()
            chat_id = s.get("range_broadcast_chat_id")
            if chat_id:
                top = _ranges_cache.get("data")
                if not top:
                    top, _ = await fetch_top55_ranges_by_app()
                if top:
                    blocks = []
                    total_ranges = 0
                    for app_name, info in list(top.items())[:15]:
                        ranges = info.get("ranges", [])
                        if not ranges:
                            continue
                        total_ranges += len(ranges)
                        shown = ranges[:6]
                        range_lines = []
                        for rng in shown:
                            prefix = re.sub(r'[xX]+$', '', str(rng)).strip()
                            flag, country_name = get_country_info(re.sub(r'\D', '', prefix))
                            range_lines.append(f"{flag} {html.escape(country_name)} — <code>{html.escape(rng)}</code>")
                        extra = len(ranges) - len(shown)
                        if extra > 0:
                            range_lines.append(f"<i>+{extra} more…</i>")
                        blocks.append(
                            f"<blockquote>{get_platform_icon(app_name)} <b>{html.escape(app_name)}</b> "
                            f"({len(ranges)} range{'s' if len(ranges) != 1 else ''})\n"
                            + "\n".join(range_lines) + "</blockquote>"
                        )
                    if blocks:
                        now = datetime.now().strftime("%I:%M %p")
                        msg = (
                            f"📡 <b>MASTER RANGE LIST</b>\n"
                            f"🕒 আপডেট: {now} | 📊 মোট {total_ranges} রেঞ্জ, {len(blocks)}টা সার্ভিস\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(blocks)
                        )
                        try:
                            await app.bot.send_message(chat_id, msg[:4000], parse_mode="HTML")
                        except Exception as e:
                            print(f"[range-broadcast] send failed: {e}")
        except Exception as e:
            print(f"[range-broadcast] exception: {e}")
        await asyncio.sleep(180)  # every 3 minutes

# ==================== CHECK IF USER IS ADMIN ====================

def is_admin(user_id):
    """Return True when the Telegram user is an admin or owner.

    Admin IDs may exist in the hard-coded ADMINS list and/or settings.json.
    Values are normalized to strings so JSON string/int differences cannot
    prevent the Admin Panel from opening.
    """
    try:
        uid = str(int(user_id))
    except (TypeError, ValueError):
        uid = str(user_id).strip()

    s = load_settings() or {}
    configured_admins = s.get("admins", []) or []
    configured_owners = s.get("owners", []) or []

    allowed_ids = {str(x).strip() for x in ADMINS}
    allowed_ids.update(str(x).strip() for x in configured_admins)
    allowed_ids.update(str(x).strip() for x in configured_owners)

    return uid in allowed_ids

# ==================== WITHDRAW DATA FUNCTIONS ====================

def load_withdraw_requests():
    return _generic_load(WITHDRAW_DATA_FILE, {})

def save_withdraw_requests(data):
    _generic_save(WITHDRAW_DATA_FILE, data)

def generate_payment_id():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=20))

# ==================== BANNED USERS FUNCTIONS ====================

def load_banned_users():
    return _generic_load(BANNED_USERS_FILE, [])

def save_banned_users(banned_list):
    _generic_save(BANNED_USERS_FILE, banned_list)

def is_user_banned(uid):
    banned_list = load_banned_users()
    return str(uid) in banned_list

def ban_user(uid):
    banned_list = load_banned_users()
    uid_str = str(uid)
    if uid_str not in banned_list:
        banned_list.append(uid_str)
        save_banned_users(banned_list)
        return True
    return False

def unban_user(uid):
    banned_list = load_banned_users()
    uid_str = str(uid)
    if uid_str in banned_list:
        banned_list.remove(uid_str)
        save_banned_users(banned_list)
        return True
    return False

# ==================== REFERRAL DATA FUNCTIONS ====================

def load_referral_data():
    return _generic_load(REFERRAL_DATA_FILE, {})

def save_referral_data(data):
    _generic_save(REFERRAL_DATA_FILE, data)

def update_referral_count(uid, count):
    referral_data = load_referral_data()
    uid_str = str(uid)
    if uid_str not in referral_data:
        referral_data[uid_str] = {"referral_count": 0}
    referral_data[uid_str]["referral_count"] = count
    save_referral_data(referral_data)

def get_referral_count(uid):
    referral_data = load_referral_data()
    uid_str = str(uid)
    return referral_data.get(uid_str, {}).get("referral_count", 0)

# ==================== DATA RANGE FILE ====================

def load_range_db():
    return _generic_load(DATA_RANGE_FILE, {})

def save_range_db(data):
    _generic_save(DATA_RANGE_FILE, data)

def save_number_range_info(uid, number, range_text):
    db = load_range_db()
    flag, name = get_country_info(number)
    db[normalize_number(number)] = {
        "user_id": str(uid),
        "number": f"+{normalize_number(number)}",
        "range": range_text,
        "country": f"{flag} {name}"
    }
    save_range_db(db)

# ==================== COUNTRY MAPPING SECTION ====================

def get_country_info(number):
    number = str(number).strip()

    country_map = {
        "2376": ("🇨🇲", "Cameroon"),
        "2250": ("🇨🇮", "Ivory Coast"),
        "2613": ("🇲🇬", "Madagascar"),
        "4077": ("🇷🇴", "Romania"),
        "237": ("🇨🇲", "Cameroon"),
        "225": ("🇨🇮", "Ivory Coast"),
        "261": ("🇲🇬", "Madagascar"),
        "20": ("🇪🇬", "Egypt"),
        "27": ("🇿🇦", "South Africa"),
        "234": ("🇳🇬", "Nigeria"),
        "254": ("🇰🇪", "Kenya"),
        "233": ("🇬🇭", "Ghana"),
        "212": ("🇲🇦", "Morocco"),
        "213": ("🇩🇿", "Algeria"),
        "216": ("🇹🇳", "Tunisia"),
        "218": ("🇱🇾", "Libya"),
        "249": ("🇸🇩", "Sudan"),
        "251": ("🇪🇹", "Ethiopia"),
        "252": ("🇸🇴", "Somalia"),
        "253": ("🇩🇯", "Djibouti"),
        "255": ("🇹🇿", "Tanzania"),
        "256": ("🇺🇬", "Uganda"),
        "257": ("🇧🇮", "Burundi"),
        "258": ("🇲🇿", "Mozambique"),
        "260": ("🇿🇲", "Zambia"),
        "263": ("🇿🇼", "Zimbabwe"),
        "264": ("🇳🇦", "Namibia"),
        "265": ("🇲🇼", "Malawi"),
        "266": ("🇱🇸", "Lesotho"),
        "267": ("🇧🇼", "Botswana"),
        "268": ("🇸🇿", "Swaziland"),
        "269": ("🇰🇲", "Comoros"),
        "220": ("🇬🇲", "Gambia"),
        "221": ("🇸🇳", "Senegal"),
        "222": ("🇲🇷", "Mauritania"),
        "223": ("🇲🇱", "Mali"),
        "224": ("🇬🇳", "Guinea"),
        "226": ("🇧🇫", "Burkina Faso"),
        "227": ("🇳🇪", "Niger"),
        "228": ("🇹🇬", "Togo"),
        "229": ("🇧🇯", "Benin"),
        "230": ("🇲🇺", "Mauritius"),
        "231": ("🇱🇷", "Liberia"),
        "232": ("🇸🇱", "Sierra Leone"),
        "235": ("🇹🇩", "Chad"),
        "236": ("🇨🇫", "Central African Republic"),
        "238": ("🇨🇻", "Cape Verde"),
        "239": ("🇸🇹", "Sao Tome and Principe"),
        "240": ("🇬🇶", "Equatorial Guinea"),
        "241": ("🇬🇦", "Gabon"),
        "242": ("🇨🇬", "Congo"),
        "243": ("🇨🇩", "DR Congo"),
        "244": ("🇦🇴", "Angola"),
        "245": ("🇬🇼", "Guinea-Bissau"),
        "247": ("🇸🇭", "Saint Helena"),
        "248": ("🇸🇨", "Seychelles"),
        "250": ("🇷🇼", "Rwanda"),
        "290": ("🇸🇭", "Saint Helena"),
        "291": ("🇪🇷", "Eritrea"),
        "40": ("🇷🇴", "Romania"),
        "44": ("🇬🇧", "United Kingdom"),
        "33": ("🇫🇷", "France"),
        "49": ("🇩🇪", "Germany"),
        "39": ("🇮🇹", "Italy"),
        "34": ("🇪🇸", "Spain"),
        "31": ("🇳🇱", "Netherlands"),
        "32": ("🇧🇪", "Belgium"),
        "41": ("🇨🇭", "Switzerland"),
        "43": ("🇦🇹", "Austria"),
        "46": ("🇸🇪", "Sweden"),
        "47": ("🇳🇴", "Norway"),
        "45": ("🇩🇰", "Denmark"),
        "358": ("🇫🇮", "Finland"),
        "351": ("🇵🇹", "Portugal"),
        "353": ("🇮🇪", "Ireland"),
        "36": ("🇭🇺", "Hungary"),
        "48": ("🇵🇱", "Poland"),
        "380": ("🇺🇦", "Ukraine"),
        "370": ("🇱🇹", "Lithuania"),
        "371": ("🇱🇻", "Latvia"),
        "372": ("🇪🇪", "Estonia"),
        "373": ("🇲🇩", "Moldova"),
        "374": ("🇦🇲", "Armenia"),
        "375": ("🇧🇾", "Belarus"),
        "376": ("🇦🇩", "Andorra"),
        "377": ("🇲🇨", "Monaco"),
        "381": ("🇷🇸", "Serbia"),
        "382": ("🇲🇪", "Montenegro"),
        "385": ("🇭🇷", "Croatia"),
        "386": ("🇸🇮", "Slovenia"),
        "387": ("🇧🇦", "Bosnia and Herzegovina"),
        "389": ("🇲🇰", "North Macedonia"),
        "350": ("🇬🇮", "Gibraltar"),
        "352": ("🇱🇺", "Luxembourg"),
        "354": ("🇮🇸", "Iceland"),
        "355": ("🇦🇱", "Albania"),
        "356": ("🇲🇹", "Malta"),
        "357": ("🇨🇾", "Cyprus"),
        "359": ("🇧🇬", "Bulgaria"),
        "421": ("🇸🇰", "Slovakia"),
        "420": ("🇨🇿", "Czech Republic"),
        "298": ("🇫🇴", "Faroe Islands"),
        "299": ("🇬🇱", "Greenland"),
        "1": ("🇺🇸", "United States"),
        "7": ("🇷🇺", "Russia"),
        "91": ("🇮🇳", "India"),
        "92": ("🇵🇰", "Pakistan"),
        "880": ("🇧🇩", "Bangladesh"),
        "86": ("🇨🇳", "China"),
        "81": ("🇯🇵", "Japan"),
        "82": ("🇰🇷", "South Korea"),
        "84": ("🇻🇳", "Vietnam"),
        "66": ("🇹🇭", "Thailand"),
        "62": ("🇮🇩", "Indonesia"),
        "60": ("🇲🇾", "Malaysia"),
        "65": ("🇸🇬", "Singapore"),
        "63": ("🇵🇭", "Philippines"),
        "95": ("🇲🇲", "Myanmar"),
        "94": ("🇱🇰", "Sri Lanka"),
        "977": ("🇳🇵", "Nepal"),
        "93": ("🇦🇫", "Afghanistan"),
        "98": ("🇮🇷", "Iran"),
        "90": ("🇹🇷", "Turkey"),
        "964": ("🇮🇶", "Iraq"),
        "963": ("🇸🇾", "Syria"),
        "961": ("🇱🇧", "Lebanon"),
        "962": ("🇯🇴", "Jordan"),
        "965": ("🇰🇼", "Kuwait"),
        "966": ("🇸🇦", "Saudi Arabia"),
        "967": ("🇾🇪", "Yemen"),
        "968": ("🇴🇲", "Oman"),
        "971": ("🇦🇪", "United Arab Emirates"),
        "972": ("🇮🇱", "Israel"),
        "973": ("🇧🇭", "Bahrain"),
        "974": ("🇶🇦", "Qatar"),
        "994": ("🇦🇿", "Azerbaijan"),
        "995": ("🇬🇪", "Georgia"),
        "996": ("🇰🇬", "Kyrgyzstan"),
        "992": ("🇹🇯", "Tajikistan"),
        "993": ("🇹🇲", "Turkmenistan"),
        "998": ("🇺🇿", "Uzbekistan"),
        "855": ("🇰🇭", "Cambodia"),
        "856": ("🇱🇦", "Laos"),
        "976": ("🇲🇳", "Mongolia"),
        "850": ("🇰🇵", "North Korea"),
        "55": ("🇧🇷", "Brazil"),
        "52": ("🇲🇽", "Mexico"),
        "54": ("🇦🇷", "Argentina"),
        "57": ("🇨🇴", "Colombia"),
        "51": ("🇵🇪", "Peru"),
        "58": ("🇻🇪", "Venezuela"),
        "56": ("🇨🇱", "Chile"),
        "593": ("🇪🇨", "Ecuador"),
        "591": ("🇧🇴", "Bolivia"),
        "595": ("🇵🇾", "Paraguay"),
        "598": ("🇺🇾", "Uruguay"),
        "502": ("🇬🇹", "Guatemala"),
        "503": ("🇸🇻", "El Salvador"),
        "504": ("🇭🇳", "Honduras"),
        "506": ("🇨🇷", "Costa Rica"),
        "507": ("🇵🇦", "Panama"),
        "509": ("🇭🇹", "Haiti"),
        "501": ("🇧🇿", "Belize"),
        "61": ("🇦🇺", "Australia"),
        "64": ("🇳🇿", "New Zealand"),
        "675": ("🇵🇬", "Papua New Guinea"),
        "679": ("🇫🇯", "Fiji"),
        "1246": ("🇧🇧", "Barbados"),
        "1876": ("🇯🇲", "Jamaica"),
        "53": ("🇨🇺", "Cuba"),
        "592": ("🇬🇾", "Guyana"),
    }

    clean_num = str(number).replace('+', '').replace(' ', '').replace('-', '').strip()
    sorted_prefixes = sorted(country_map.keys(), key=len, reverse=True)

    for prefix in sorted_prefixes:
        if clean_num.startswith(prefix):
            return country_map[prefix]

    return ("🌍", "Unknown")

# ==================== SERVICE DETECTION SECTION ====================

def detect_service(full_sms):
    if not full_sms:
        return "SMS SERVICE"

    sms_lower = full_sms.lower()

    service_keywords = {
        "facebook": "FACEBOOK", "fb": "FACEBOOK",
        "instagram": "INSTAGRAM", "insta": "INSTAGRAM",
        "tiktok": "TIKTOK",
        "twitter": "TWITTER", "x.com": "TWITTER",
        "snapchat": "SNAPCHAT", "snap": "SNAPCHAT",
        "whatsapp": "WHATSAPP",
        "telegram": "TELEGRAM",
        "discord": "DISCORD",
        "messenger": "MESSENGER",
        "linkedin": "LINKEDIN",
        "google": "GOOGLE", "gmail": "GOOGLE",
        "amazon": "AMAZON",
        "microsoft": "MICROSOFT", "outlook": "MICROSOFT",
        "yahoo": "YAHOO",
        "paypal": "PAYPAL",
        "binance": "BINANCE",
        "coinbase": "COINBASE",
        "spotify": "SPOTIFY",
        "netflix": "NETFLIX",
        "uber": "UBER",
        "apple": "APPLE", "icloud": "APPLE",
        "bkash": "BKASH",
        "nagad": "NAGAD",
        "stripe": "STRIPE",
        "line": "LINE",
        "wechat": "WECHAT",
        "viber": "VIBER",
        "signal": "SIGNAL",
        "pubg": "PUBG",
        "free fire": "FREE FIRE",
        "imo": "IMO",
        "twitch": "TWITCH",
        "pinterest": "PINTEREST",
        "reddit": "REDDIT",
        "zoom": "ZOOM",
        "slack": "SLACK",
        "skype": "SKYPE",
        "bybit": "BYBIT",
        "melbet": "MELBET",
        "1xbet": "1XBET",
        "aliexpress": "ALIEXPRESS",
        "shopee": "SHOPEE",
        "daraz": "DARAZ",
        "foodpanda": "FOODPANDA",
        "pathao": "PATHAO",
        "rocket": "ROCKET",
        "chatgpt": "CHATGPT",
        "openai": "CHATGPT",
    }

    for keyword, service_name in sorted(service_keywords.items(), key=lambda x: len(x[0]), reverse=True):
        if keyword in sms_lower:
            return service_name

    return "SMS SERVICE"

# ==================== KEYBOARDS SECTION ====================
# GET 2FA এবং SEARCH OTP বাটনের স্থান পরিবর্তন করা হয়েছে (swap)

from telegram import KeyboardButton, ReplyKeyboardMarkup

def make_reply_btn(text, emoji_id=None, style=None):
    """Sends the premium custom emoji via icon_custom_emoji_id (Bot API 9.4) —
    this renders as a real premium emoji on the button ONLY if the bot's
    current owner account has an active Telegram Premium subscription.
    No plain-unicode emoji is embedded in the button text itself."""
    api_kwargs = {}
    if emoji_id:
        api_kwargs['icon_custom_emoji_id'] = str(emoji_id)
    if style:
        api_kwargs['style'] = style
    return KeyboardButton(
        text=text,
        api_kwargs=api_kwargs if api_kwargs else None
    )

def main_keyboard(user_id):
    keyboard = [
        [
            make_reply_btn("GET NUMBER",     emoji_id="5375338737028841420", style="success")
        ],
        [
            make_reply_btn("SEARCH OTP",     emoji_id="5190781475468915802", style="primary"),
            make_reply_btn("GET 2FA",        emoji_id="5296369303661067030", style="danger")
        ],
        [
            make_reply_btn("BALANCE",        emoji_id="6233367447789899509", style="success"),
            make_reply_btn("PROFILE",        emoji_id="5352861489541714456", style="primary")
        ],
        [
            make_reply_btn("REFER AND EARN", emoji_id="5420396762189831222", style="primary"),
            make_reply_btn("SUPPORT",        emoji_id="5253742260054409879", style="primary")
        ],
        [
            make_reply_btn("LEADERBOARD",    emoji_id="5280769763398671636", style="danger"),
            make_reply_btn("TOP HITS",       emoji_id="5244837092042750681", style="success")
        ],
    ]

    if is_admin(user_id):
        keyboard.append([
            make_reply_btn("ADMIN PANEL", emoji_id="5350396951407895212", style="danger")
        ])

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def cancel_keyboard():
    keyboard = [[make_reply_btn("CANCEL", emoji_id="5420130255174145507", style="danger")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def admin_main_keyboard():
    keyboard = [
        [make_reply_btn("USER MANAGEMENT",      emoji_id="5193063022226086560", style="success")],
        [make_reply_btn("SYSTEM CONFIGURATION", emoji_id="5341715473882955310", style="success")],
        [make_reply_btn("BOT SETTINGS",         emoji_id="5282843764451195532", style="primary")],
        [make_reply_btn("SERVICE MANAGEMENT",   emoji_id="5375338737028841420", style="primary")],
        [make_reply_btn("BACK TO MAIN",         emoji_id="5267490665117275176", style="danger")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def user_management_keyboard():
    keyboard = [
        [make_reply_btn("SEND MESSAGE TO ALL USERS", emoji_id="5251671733630431622", style="success")],
        [make_reply_btn("ALL USERS INFO",             emoji_id="5352861489541714456", style="primary")],
        [make_reply_btn("BAN USER LIST",             emoji_id="5420323339723881652", style="primary")],
        [make_reply_btn("ALL USER BALANCE",          emoji_id="6233367447789899509", style="primary")],
        [make_reply_btn("BACK TO ADMIN",             emoji_id="5267490665117275176", style="danger")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def system_config_keyboard():
    keyboard = [
        [make_reply_btn("TODAY ALL STATUS",  emoji_id="5231200819986047254", style="success"),
         make_reply_btn("USER STATUS CHECK", emoji_id="5352861489541714456", style="success")],
        [make_reply_btn("BAN USER",          emoji_id="5420323339723881652", style="danger"),
         make_reply_btn("UNBAN USER",        emoji_id="5875017993909440887", style="primary")],
        [make_reply_btn("BAN USER LIST",     emoji_id="5956074558044770726", style="primary")],
        [make_reply_btn("REMOVE BALANCE",    emoji_id="5244837092042750681", style="danger"),
         make_reply_btn("ADD BALANCE",       emoji_id="5397916757333654639", style="success")],
        [make_reply_btn("BACK TO ADMIN",     emoji_id="5267490665117275176", style="danger")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def bot_settings_keyboard():
    keyboard = [
        [make_reply_btn("ADD ADMIN",          emoji_id="5397916757333654639", style="success"),
         make_reply_btn("REMOVE ADMIN",       emoji_id="5244837092042750681", style="danger")],
        [make_reply_btn("LINKS & CHANNELS",   emoji_id="5253742260054409879", style="primary")],
        [make_reply_btn("SET OTP BONUS",      emoji_id="6233367447789899509", style="success"),
         make_reply_btn("SET REFERRAL BONUS", emoji_id="5420396762189831222", style="success")],
        [make_reply_btn("SET OTP CHAT ID",    emoji_id="5231200819986047254", style="primary")],
        [make_reply_btn("SET RANGE BROADCAST CHAT ID", emoji_id="5231012545799666522", style="primary")],
        [make_reply_btn("MANAGE TOP HIT RANGES", emoji_id="5231012545799666522", style="success")],
        [make_reply_btn("API PROVIDER CONFIG", emoji_id="5350396951407895212", style="success")],
        [make_reply_btn("BACK TO ADMIN",      emoji_id="5267490665117275176", style="danger")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def top_hit_ranges_admin_keyboard():
    keyboard = [
        [make_reply_btn("ADD RANGE",    emoji_id="5397916757333654639", style="success")],
        [make_reply_btn("REMOVE RANGE", emoji_id="5244837092042750681", style="danger")],
        [make_reply_btn("LIST RANGES",  emoji_id="5231200819986047254", style="primary")],
        [make_reply_btn("BACK TO BOT SETTINGS", emoji_id="5267490665117275176", style="danger")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def links_config_keyboard():
    """One consolidated hub for every link and every join-required channel
    the bot uses — each button says exactly where it's used."""
    keyboard = [
        [make_reply_btn("OTP GROUP JOIN LINK",     emoji_id="5253742260054409879", style="primary")],
        [make_reply_btn("MAIN CHANNEL BUTTON LINK", emoji_id="6215074610845585917", style="primary")],
        [make_reply_btn("NUMBER PANEL BUTTON LINK", emoji_id="4943094697238201446", style="primary")],
        [make_reply_btn("DEVELOPER BUTTON LINK",   emoji_id="5267294466716244344", style="primary")],
        [make_reply_btn("SUPPORT BUTTON LINK",     emoji_id="5251671501702196837", style="primary")],
        [make_reply_btn("RENAME BUTTON LABELS",    emoji_id="5341715473882955310", style="primary")],
        [make_reply_btn("FORCE JOIN CHANNELS",     emoji_id="5282843764451195532", style="danger")],
        [make_reply_btn("VIEW ALL LINKS",          emoji_id="5231200819986047254", style="success")],
        [make_reply_btn("BACK TO BOT SETTINGS",    emoji_id="5267490665117275176", style="danger")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def rename_buttons_keyboard():
    keyboard = [
        [make_reply_btn("RENAME: OTP GROUP BTN",     emoji_id="5253742260054409879", style="primary")],
        [make_reply_btn("RENAME: NUMBER PANEL BTN",  emoji_id="4943094697238201446", style="primary")],
        [make_reply_btn("RENAME: MAIN CHANNEL BTN",  emoji_id="6215074610845585917", style="primary")],
        [make_reply_btn("RENAME: SUPPORT BTN",       emoji_id="5251671501702196837", style="primary")],
        [make_reply_btn("RENAME: DEVELOPER BTN",     emoji_id="5267294466716244344", style="primary")],
        [make_reply_btn("RENAME: DEVELOPER NAME",    emoji_id="5267294466716244344", style="primary")],
        [make_reply_btn("BACK TO LINKS & CHANNELS",  emoji_id="5267490665117275176", style="danger")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def force_join_keyboard():
    keyboard = [
        [make_reply_btn("ADD CHANNEL",    emoji_id="5397916757333654639", style="success")],
        [make_reply_btn("REMOVE CHANNEL", emoji_id="5244837092042750681", style="danger")],
        [make_reply_btn("LIST CHANNELS",  emoji_id="5231200819986047254", style="primary")],
        [make_reply_btn("BACK TO LINKS & CHANNELS", emoji_id="5267490665117275176", style="danger")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def provider_config_keyboard():
    keyboard = [
        [make_reply_btn("ZENEX SETUP",   emoji_id="5397916757333654639", style="success"),
         make_reply_btn("MIAHSMS SETUP", emoji_id="5397916757333654639", style="success")],
        [make_reply_btn("VOLTXSMS SETUP",    emoji_id="5397916757333654639", style="success"),
         make_reply_btn("STEXSMS SETUP",     emoji_id="5397916757333654639", style="success")],
        [make_reply_btn("MNITNETWORK SETUP", emoji_id="5397916757333654639", style="success")],
        [make_reply_btn("ADD PROVIDER (ADVANCED)", emoji_id="5397916757333654639", style="primary")],
        [make_reply_btn("SWITCH PROVIDER", emoji_id="5341715473882955310", style="primary"),
         make_reply_btn("EDIT PROVIDER",   emoji_id="5282843764451195532", style="primary")],
        [make_reply_btn("MULTI-PANEL MODE", emoji_id="5341715473882955310", style="danger")],
        [make_reply_btn("SYNC SERVICES",   emoji_id="5386367538735104399", style="success")],
        [make_reply_btn("VIEW PROVIDERS",  emoji_id="5231200819986047254", style="success")],
        [make_reply_btn("BACK TO BOT SETTINGS", emoji_id="5267490665117275176", style="danger")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def multi_panel_keyboard():
    keyboard = [
        [make_reply_btn("ENABLE PANEL",  emoji_id="5397916757333654639", style="success")],
        [make_reply_btn("DISABLE PANEL", emoji_id="5244837092042750681", style="danger")],
        [make_reply_btn("LIST ENABLED PANELS", emoji_id="5231200819986047254", style="primary")],
        [make_reply_btn("PANEL USAGE STATS", emoji_id="5231200819986047254", style="success")],
        [make_reply_btn("BACK TO API PROVIDER CONFIG", emoji_id="5267490665117275176", style="danger")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def service_management_keyboard():
    keyboard = [
        [make_reply_btn("ADD SERVICE",    emoji_id="5397916757333654639", style="success")],
        [make_reply_btn("REMOVE SERVICE", emoji_id="5244837092042750681", style="danger")],
        [make_reply_btn("LIST SERVICES",  emoji_id="5352861489541714456", style="primary")],
        [make_reply_btn("BACK TO ADMIN",  emoji_id="5267490665117275176", style="danger")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def withdraw_method_keyboard():
    keyboard = ReplyKeyboardMarkup([
        [make_reply_btn("BKASH",  emoji_id="5348469219761626211", style="success"),
         make_reply_btn("NAGAD",  emoji_id="5352985330628730418", style="success")],
        [make_reply_btn("ROCKET", emoji_id="5346042941196507141", style="primary"),
         make_reply_btn("BINANCE",emoji_id="5348212415077064131", style="primary")],
        [make_reply_btn("CANCEL", emoji_id="5420130255174145507", style="danger")]
    ], resize_keyboard=True)
    return keyboard

def number_result_keyboard():
    """CHANGE NUMBER + OTP GROUP side by side, with a full-width BACK button
    below (styled like LEADERBOARD). OTP group link/label are admin-editable
    via Admin Panel > BOT SETTINGS > LINKS & CHANNELS."""
    s = load_settings()
    otp_url = s.get("otp_group_url") or "https://t.me/+31eV11IT7WQzMjI9"
    otp_label = s.get("otp_group_btn_label") or "📢 OTP GROUP"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 CHANGE NUMBER", callback_data="same_range",
                api_kwargs={"icon_custom_emoji_id": "5267295703666824255", "style": "success"}),
            InlineKeyboardButton(otp_label, url=otp_url,
                api_kwargs={"icon_custom_emoji_id": "5253742260054409879", "style": "primary"})
        ],
        [InlineKeyboardButton("🔙 BACK", callback_data="back_services",
            api_kwargs={"icon_custom_emoji_id": "5267490665117275176", "style": "danger"})]
    ])

# ==================== HELPER FUNCTIONS SECTION ====================

def format_balance(balance):
    return f"{balance:.2f}"

def extract_otp(text):
    if not text or text == "No Content":
        return "N/A"
    spaced_otp = re.search(r'\b(\d{3}\s\d{3})\b', text)
    if spaced_otp:
        return spaced_otp.group(1).replace(" ", "")
    match = re.search(r'\b(\d{4,8})\b', text)
    return match.group(1) if match else "N/A"

def normalize_number(num):
    return re.sub(r'\D', '', str(num))

def mask_number(num):
    if len(num) > 6:
        return f"{num[:4]}****{num[-6:]}"
    return num

def get_date_reset_time():
    now = datetime.now()
    today_midnight = datetime(now.year, now.month, now.day, 0, 0, 0)
    return today_midnight

def is_valid_bangladesh_number(number):
    number = re.sub(r'\D', '', str(number))
    return len(number) == 11 and number.startswith('01')

def is_range_request(param):
    return 'X' in param.upper()

def is_referral_request(param):
    return param.isdigit()

# ==================== DATABASE FUNCTIONS SECTION ====================

def load_data(filename=USER_DATA_FILE):
    return _generic_load(filename, {})

def save_data(data, filename=USER_DATA_FILE):
    _generic_save(filename, data)

def get_user(uid):
    uid = str(uid)
    data = load_data()
    if uid not in data:
        data[uid] = {"user_id": uid, "balance": 0.0, "total_numbers": 0, "referral_count": 0}
        save_data(data)
    return data[uid]

def update_user_profile(uid, full_name=None, username=None):
    """Keeps the stored full_name/username current so USER MANAGEMENT can
    show more than a bare Telegram ID."""
    uid = str(uid)
    data = load_data()
    if uid not in data:
        data[uid] = {"user_id": uid, "balance": 0.0, "total_numbers": 0, "referral_count": 0}
    changed = False
    if full_name and data[uid].get("full_name") != full_name:
        data[uid]["full_name"] = full_name
        changed = True
    if username is not None and data[uid].get("username") != username:
        data[uid]["username"] = username
        changed = True
    if changed:
        save_data(data)

async def update_db_balance(uid, amount):
    uid = str(uid)
    data = load_data()
    if uid in data:
        data[uid]["balance"] = round(data[uid].get("balance", 0.0) + amount, 2)
        save_data(data)
        return data[uid]["balance"]
    return 0.0

def get_all_users():
    data = load_data(USER_DATA_FILE)
    return list(data.keys()) if data else []

def user_exists(uid):
    data = load_data(USER_DATA_FILE)
    return str(uid) in data

# ==================== STATS FUNCTIONS SECTION ====================

def load_stats():
    return _generic_load(STATS_FILE, {})

def save_stats(stats):
    _generic_save(STATS_FILE, stats)

def add_number_taken(uid, count=1):
    uid = str(uid)
    stats = load_stats()
    if uid not in stats:
        stats[uid] = {"numbers_taken": [], "otps_received": []}
    now = datetime.now().isoformat()
    for _ in range(count):
        stats[uid]["numbers_taken"].append(now)
    log_global_activity(uid, "NUMBER_TAKEN", {"count": count})
    save_stats(stats)

def add_otp_received(uid):
    uid = str(uid)
    stats = load_stats()
    if uid not in stats:
        stats[uid] = {"numbers_taken": [], "otps_received": []}
    stats[uid]["otps_received"].append(datetime.now().isoformat())
    save_stats(stats)

def get_user_stats(uid):
    uid = str(uid)
    stats = load_stats()
    user_stats = stats.get(uid, {"numbers_taken": [], "otps_received": []})

    now = datetime.now()
    today_midnight = get_date_reset_time()
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)

    numbers_taken = user_stats.get("numbers_taken", [])
    otps_received = user_stats.get("otps_received", [])

    today_numbers = sum(1 for t in numbers_taken if datetime.fromisoformat(t) >= today_midnight)
    today_otps = sum(1 for t in otps_received if datetime.fromisoformat(t) >= today_midnight)
    last24h_numbers = sum(1 for t in numbers_taken if datetime.fromisoformat(t) > last_24h)
    last24h_otps = sum(1 for t in otps_received if datetime.fromisoformat(t) > last_24h)
    last7d_numbers = sum(1 for t in numbers_taken if datetime.fromisoformat(t) > last_7d)
    last7d_otps = sum(1 for t in otps_received if datetime.fromisoformat(t) > last_7d)
    total_numbers = len(numbers_taken)
    total_otps = len(otps_received)

    return {
        "total_numbers": total_numbers, "total_otps": total_otps,
        "today_numbers": today_numbers, "today_otps": today_otps,
        "last24h_numbers": last24h_numbers, "last24h_otps": last24h_otps,
        "last7d_numbers": last7d_numbers, "last7d_otps": last7d_otps
    }

def log_global_activity(uid, action, details):
    logs = _generic_load(ACTIVITY_LOGS_FILE, [])
    now = datetime.now()
    logs.append({
        "uid": str(uid), "action": action, "details": details,
        "timestamp": now.isoformat(),
        "date": now.strftime("%d/%m/%Y"),
        "time": now.strftime("%H:%M:%S")
    })
    _generic_save(ACTIVITY_LOGS_FILE, logs)

def get_global_system_stats():
    stats = load_stats()
    now = datetime.now()
    today_midnight = datetime(now.year, now.month, now.day)
    last_7d = now - timedelta(days=7)
    total_n = total_o = today_n = today_o = seven_n = seven_o = 0
    for uid in stats:
        u = stats[uid]
        n_list = u.get("numbers_taken", [])
        o_list = u.get("otps_received", [])
        total_n += len(n_list)
        total_o += len(o_list)
        for t in n_list:
            dt = datetime.fromisoformat(t)
            if dt >= today_midnight: today_n += 1
            if dt >= last_7d: seven_n += 1
        for t in o_list:
            dt = datetime.fromisoformat(t)
            if dt >= today_midnight: today_o += 1
            if dt >= last_7d: seven_o += 1
    return today_n, today_o, seven_n, seven_o, total_n, total_o

# ==================== LEADERBOARD SECTION ====================

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_banned(uid):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return

    stats_data = load_stats()
    today_midnight = get_date_reset_time()
    user_data_all = load_data(USER_DATA_FILE)

    user_today_counts = []

    for uid_str, user_stats in stats_data.items():
        otps_received = user_stats.get("otps_received", [])
        today_count = 0
        for ts in otps_received:
            try:
                dt = datetime.fromisoformat(ts)
                if dt >= today_midnight:
                    today_count += 1
            except:
                continue
        if today_count > 0:
            name = user_data_all.get(uid_str, {}).get("full_name")
            if not name:
                name = user_data_all.get(uid_str, {}).get("username")
            if not name:
                name = f"User {uid_str}"
            user_today_counts.append((uid_str, today_count, html.escape(name)))

    user_today_counts.sort(key=lambda x: x[1], reverse=True)
    top10 = user_today_counts[:10]

    if not top10:
        msg = (
            "<b>🏆 TOP 10 OTP LEADERBOARD 🏆</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "❌ আজ পর্যন্ত কেউ OTP পায়নি।\n"
        )
    else:
        msg = (
            "<b>🏆 TOP 10 OTP RECEIVERS (TODAY) 🏆</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        for idx, (uid_str, count, name) in enumerate(top10, 1):
            if idx == 1:
                medal = "🥇"
            elif idx == 2:
                medal = "🥈"
            elif idx == 3:
                medal = "🥉"
            else:
                medal = f"{idx}️⃣"
            msg += f"{medal} <b>{name}</b>\n   🔑 <code>{count}</code> OTPs\n\n"
        msg += (
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📊 <i>প্রতিদিন রাত ১২টায় রিসেট হয়</i>"
        )

    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=main_keyboard(uid))

# ==================== TOP HIT RANGES SECTION ====================

async def top_hit_ranges_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_banned(uid):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return

    top = get_top_hit_ranges(limit=10, minutes=RANGE_HIT_WINDOW_MINUTES)

    if not top:
        msg = (
            "<b>🔥 TOP HITS (LAST 30M)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "❌ গত ৩০ মিনিটে কোনো নাম্বার নেওয়া হয়নি।\n"
        )
    else:
        msg = (
            "<b>🔥 TOP HITS (LAST 30M)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        for idx, (rng, cnt, app_name) in enumerate(top, 1):
            prefix = re.sub(r'[xX]+$', '', rng).strip()
            flag, country_name = get_country_info(re.sub(r'\D', '', prefix))
            app_txt = f" — <b>{html.escape(app_name)}</b>" if app_name else ""
            msg += (
                f"{idx}. <code>{rng}</code> {flag} {html.escape(country_name)}{app_txt}\n"
                f"   🔥 <code>{cnt}</code> Hits\n\n"
            )
        msg += (
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📊 <i>এই বটে ব্যবহার হওয়া নাম্বার থেকে হিসাব করা (৩০ মিনিটের রোলিং উইন্ডো) — "
            "provider panel সরাসরি hit-count API দেয় না, তাই পুরো নেটওয়ার্কের ডেটা না</i>"
        )

    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=main_keyboard(uid))

# ==================== 2FA CODE GENERATOR SECTION ====================

def generate_2fa_code(secret_key):
    try:
        clean_secret = secret_key.replace(" ", "").upper().strip()
        key = base64.b32decode(clean_secret, casefold=True)
        t = int(time.time()) // 30
        msg_bytes = struct.pack(">Q", t)
        h = hmac.new(key, msg_bytes, hashlib.sha1).digest()
        o = h[-1] & 0xf
        code = (struct.unpack(">I", h[o:o+4])[0] & 0x7fffffff) % 1_000_000
        return f"{code:06d}", clean_secret
    except:
        return None, None

async def get_2fa_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_banned(uid):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return
    context.user_data["mode"] = "get_2fa"
    await update.message.reply_text(
        "⚡ <b>GET 2FA CODE</b> ⚡\n\n"
        "<blockquote>🔑 ENTER YOUR 2FA SECRET KEY:</blockquote>",
        parse_mode="HTML"
    )

async def process_2fa_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    secret_key = update.message.text.strip()
    context.user_data["mode"] = None

    otp_code, clean_key = generate_2fa_code(secret_key)

    if otp_code is None:
        await update.message.reply_text(
            "❌ <b>INVALID 2FA SECRET KEY</b>\n\n⚠️ Please send a valid base32 key.",
            parse_mode="HTML",
            reply_markup=main_keyboard(uid)
        )
        return

    now = datetime.now()
    final_msg = (
        "✅ <b>2FA CODE GENERATED!</b>\n\n"
        f"<blockquote>🔑 KEY: <code>{clean_key}</code></blockquote>\n"
        f"<blockquote>🔢 CODE: <code>{otp_code}</code></blockquote>\n"
        f"<blockquote>⏳ EXPIRES IN: 30 SECONDS</blockquote>\n"
        f"📅 {now.strftime('%d %B, %Y')} | {now.strftime('%I:%M %p')}"
    )
    await update.message.reply_text(final_msg, parse_mode="HTML")


def build_manual_service_buttons():
    """Build inline buttons for manually added services."""
    s = load_settings()
    manual = s.get("manual_services", [])
    btns = []
    clrs = ["primary", "success", "danger"]
    for i, svc in enumerate(manual):
        btns.append(InlineKeyboardButton(
            make_bold_text(svc['name']),
            callback_data=f"manual_svc_{i}",
            api_kwargs={"icon_custom_emoji_id": get_app_emoji_id(svc['name']), "style": clrs[i % 3]}
        ))
    rows = [btns[j:j+2] for j in range(0, len(btns), 2)]
    return rows

async def show_app_selection(update, context):
    uid = update.effective_user.id
    if is_user_banned(uid):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return
    context.user_data.pop("top_ranges_by_app", None)

    # Build combined keyboard: provider apps + manual services
    async def _build_full_keyboard(top):
        zenex_rows = build_app_buttons_from_cache(top)
        manual_rows = build_manual_service_buttons()
        all_rows = zenex_rows + manual_rows
        if not all_rows:
            all_rows = []
        all_rows.append([InlineKeyboardButton("⚙️ CUSTOM RANGE", callback_data="custom_range",
                                               api_kwargs={"style": "danger"})])
        return all_rows

    cache_age = time.monotonic() - _ranges_cache["updated_at"]
    if _ranges_cache["data"] and cache_age < 300:
        top = _ranges_cache["data"]
        context.user_data["top_ranges_by_app"] = top
        msg = f'{get_tg_emoji("get_number_btn")} <b>SELECT APP TO GET NUMBER</b>\n━━━━━━━━━━━━━━━━━━━━━'
        kb = await _build_full_keyboard(top)
        await update.message.reply_text(msg, parse_mode="HTML",
                                         reply_markup=InlineKeyboardMarkup(kb))
        return
    status = await update.message.reply_text(
        f'{get_tg_emoji("loading")} <b>Loading ranges...</b>', parse_mode="HTML")
    top, err = await fetch_top55_ranges_by_app()
    if err or not top:
        top, err = await fetch_top55_ranges_by_app()
    if err:
        manual_rows = build_manual_service_buttons()
        if manual_rows:
            manual_rows.append([InlineKeyboardButton("⚙️ CUSTOM RANGE", callback_data="custom_range",
                                                      api_kwargs={"style": "danger"})])
            await status.edit_text(
                f'{get_tg_emoji("get_number_btn")} <b>SELECT APP TO GET NUMBER</b>\n━━━━━━━━━━━━━━━━━━━━━\n<blockquote>⚠️ Provider unavailable — showing manual services</blockquote>',
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(manual_rows))
        else:
            await status.edit_text(
                f'{get_tg_emoji("cross")} <b>Could not load ranges.</b>\n<blockquote>{err}</blockquote>',
                parse_mode="HTML")
        return
    if not top:
        # Show only manual services if provider has no live ranges / fails
        manual_rows = build_manual_service_buttons()
        if manual_rows:
            manual_rows.append([InlineKeyboardButton("⚙️ CUSTOM RANGE", callback_data="custom_range",
                                                      api_kwargs={"style": "danger"})])
            await status.edit_text(
                f'{get_tg_emoji("get_number_btn")} <b>SELECT APP TO GET NUMBER</b>\n━━━━━━━━━━━━━━━━━━━━━\n<blockquote>⚠️ Live ranges unavailable — showing manual services</blockquote>',
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(manual_rows))
        else:
            await status.edit_text(
                f'{get_tg_emoji("stop")} No active ranges returned by the API.', parse_mode="HTML")
        return
    _ranges_cache["data"] = top
    _ranges_cache["updated_at"] = time.monotonic()
    context.user_data["top_ranges_by_app"] = top
    msg = f'{get_tg_emoji("get_number_btn")} <b>SELECT APP TO GET NUMBER</b>\n━━━━━━━━━━━━━━━━━━━━━'
    kb = await _build_full_keyboard(top)
    await status.edit_text(msg, parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(kb))

# ==================== AUTO OTP MONITOR SECTION (MULTI-PROVIDER) ====================

async def _fetch_otps_from_provider(provider):
    """Polls ONE provider's otp_path and returns its list of otp dicts
    (empty list on any failure — never raises)."""
    base = _provider_base(provider)
    otp_path = provider.get("otp_path", "")
    if not base or not otp_path:
        return []
    url = base + otp_path
    params, headers = _provider_auth(provider, {}, {})
    try:
        r = await client_async.get(url, params=params, headers=headers)
        if r.status_code != 200:
            print(f"[monitor_loop] provider={provider.get('name')} status={r.status_code} body={r.text[:300]}")
            return []
        res = r.json()
    except Exception as e:
        print(f"[monitor_loop] provider={provider.get('name')} exception: {e}")
        return []

    if isinstance(res, dict):
        if "data" in res:
            d = res["data"]
            return d if isinstance(d, list) else (d.get("otps") or d.get("active") or [])
        return res.get("otps") or []
    elif isinstance(res, list):
        return res
    return []

async def monitor_loop(app):
    while True:
        try:
            settings = load_settings()

            # Multi-panel aware: poll every ENABLED provider's OTP feed, not
            # just one — a number could have been issued from any of them.
            otps = []
            for pid in get_enabled_provider_ids():
                provider = get_provider_by_id(pid)
                if provider:
                    otps.extend(await _fetch_otps_from_provider(provider))

            if not otps:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            if otps:
                paid_data = load_data(PAID_SMS_FILE)
                paid_keys_set = set(paid_data.keys())
                processed_in_session = set()

                for otp in otps:
                    num = normalize_number(otp.get("number", ""))
                    full_sms = otp.get("sms") or otp.get("message") or otp.get("otp") or "No SMS Content"
                    otp_id = str(otp.get("nid") or otp.get("otp_id", ""))
                    otp_code = extract_otp(full_sms)
                    if not otp_code or otp_code == "N/A": continue
                    sms_key = otp_id if otp_id else f"{num}_{full_sms}"

                    if (num in active_numbers and
                            sms_key not in paid_keys_set and
                            sms_key not in processed_in_session):
                        details = active_numbers[num]
                        paid_keys_set.add(sms_key)
                        processed_in_session.add(sms_key)
                        paid_data[sms_key] = {"uid": details["uid"], "otp": otp_code}

                        otp_bonus = settings.get("otp_bonus", OTP_RATE)
                        await update_db_balance(details["uid"], otp_bonus)
                        add_otp_received(details["uid"])
                        record_provider_event(details.get("provider_id"), "otp")
                        log_global_activity(details["uid"], "OTP_RECEIVED",
                                            {"number": num, "otp": otp_code, "sms": full_sms})

                        country_flag, country_name = get_country_info(num)
                        clean_num = num.replace('+', '').strip()
                        flag_tg = get_country_tg_flag(country_flag)
                        purchased_app = details.get("app") or detect_service(full_sms)
                        app_em = get_platform_icon(purchased_app)

                        if HAS_COPY_BTN:
                            try:
                                btn_copy = InlineKeyboardButton(
                                    text=otp_code,
                                    copy_text=CopyTextButton(text=otp_code),
                                    api_kwargs={"icon_custom_emoji_id": "5296369303661067030", "style": "success"})
                            except Exception:
                                btn_copy = InlineKeyboardButton(
                                    otp_code, callback_data=f"copy_text_{otp_code}",
                                    api_kwargs={"icon_custom_emoji_id": "5296369303661067030", "style": "success"})
                        else:
                            btn_copy = InlineKeyboardButton(
                                otp_code, callback_data=f"copy_text_{otp_code}",
                                api_kwargs={"icon_custom_emoji_id": "5296369303661067030", "style": "success"})

                        panel_url = settings.get("panel_url", "https://t.me/Zenex_Number_bot?start=8346777366")
                        channel_url = settings.get("channel_url", "https://t.me/MinoXofficial0")
                        developer_url = settings.get("developer_link") or DEVELOPER_LINK
                        masked = mask_number(clean_num)
                        otp_safe = html.escape(str(otp_code))
                        sms_safe = html.escape(str(full_sms))
                        country_safe = html.escape(str(country_name))
                        service_safe = html.escape(str(purchased_app))

                        def _otp_notice(number_str):
                            return (
                                f"{get_tg_emoji('otp_success')} <b>OTP RECEIVED SUCCESSFULLY</b> {get_tg_emoji('otp_success')}\n\n"
                                f"<blockquote>🌍 COUNTRY: {flag_tg} {country_safe}</blockquote>\n"
                                f"<blockquote>{app_em} SERVICE: {service_safe}</blockquote>\n"
                                f"<blockquote>{get_tg_emoji('get_number_btn')} NUMBER: <code>+{number_str}</code></blockquote>\n"
                                f"<blockquote>{get_tg_emoji('key')} OTP: <code>{otp_safe}</code></blockquote>\n"
                                f"<blockquote>{get_tg_emoji('msg')} FULL SMS: <code>{sms_safe}</code></blockquote>"
                            )

                        user_msg  = _otp_notice(clean_num)
                        group_msg = _otp_notice(masked)
                        panel_label = settings.get("panel_btn_label") or "📱 NUMBER PANEL"
                        channel_label = settings.get("channel_btn_label") or "📢 MAIN CHANNEL"
                        developer_label = settings.get("developer_btn_label") or "👨‍💻 DEVELOPER"
                        user_kb   = InlineKeyboardMarkup([[btn_copy]])
                        # 4 buttons, 4 distinct colors: COPY=success, DEVELOPER=danger,
                        # NUMBER PANEL=primary, MAIN CHANNEL=secondary (4th color —
                        # Only 3 button colors are officially valid per Telegram Bot API
                        # 9.4: primary, success, danger. "secondary" (tried earlier as an
                        # experimental 4th color) is NOT valid and could cause the whole
                        # sendMessage call to be rejected — reusing "success" here instead.
                        group_kb  = InlineKeyboardMarkup([
                            [btn_copy,
                             InlineKeyboardButton(developer_label, url=developer_url,
                                api_kwargs={"icon_custom_emoji_id": "5267294466716244344", "style": "danger"})],
                            [InlineKeyboardButton(panel_label, url=panel_url,
                                api_kwargs={"icon_custom_emoji_id": "4943094697238201446", "style": "primary"}),
                             InlineKeyboardButton(channel_label, url=channel_url,
                                api_kwargs={"icon_custom_emoji_id": "6215074610845585917", "style": "success"})]])

                        await send_with_emoji_fallback(
                            lambda t: app.bot.send_message(details["uid"], t, parse_mode="HTML", reply_markup=user_kb),
                            user_msg)
                        gid = settings.get("otp_group_chat_id") or OTP_GROUP_ID
                        await send_with_emoji_fallback(
                            lambda t: app.bot.send_message(gid, t, parse_mode="HTML", reply_markup=group_kb),
                            group_msg)
                        save_data(paid_data, PAID_SMS_FILE)

            now = datetime.now()
            for nk in [k for k, v in list(active_numbers.items())
                        if isinstance(v, dict) and "timestamp" in v
                        and (now - v["timestamp"]).total_seconds() > 3600]:
                del active_numbers[nk]
        except Exception as e:
            print(f"[monitor_loop] unexpected exception: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

# ==================== WORKER & API SECTION (MULTI-PROVIDER) ====================

async def _fetch_number_from_provider(provider, range_str):
    """Tries to allocate a number from ONE specific provider. Returns a
    result dict (with provider name attached) or None on any failure —
    never raises, so the multi-panel caller can just move on to the next
    provider in rotation."""
    try:
        base = _provider_base(provider)
        if not base:
            return None
        url = base + provider.get("getnum_path", "/v1/getnum")
        params, headers = {}, {}
        params, headers = _provider_auth(provider, params, headers)
        headers["Accept"] = "application/json"

        range_field = provider.get("getnum_range_field", "range")
        method = provider.get("getnum_method", "POST").upper()

        # Some panels (e.g. 2oo9.cloud-based ones: voltxsms/stexsms/mnitnetwork)
        # want the range WITHOUT its trailing XXX (e.g. "26134" not "26134XXX").
        range_value = range_str
        if provider.get("strip_range_xxx"):
            range_value = re.sub(r'[xX]+$', '', str(range_str)).strip()

        # Number allocation can occasionally take longer than a couple of
        # seconds on some panels, so the read timeout is generous here.
        if method == "GET":
            params[range_field] = range_value
            r = await client_async.get(url, params=params, headers=headers,
                                        timeout=httpx.Timeout(8.0, connect=2.0, read=6.0))
        else:
            body = dict(provider.get("getnum_extra_body", {}) or {})
            body[range_field] = range_value
            r = await client_async.post(url, params=params, json=body, headers=headers,
                                         timeout=httpx.Timeout(8.0, connect=2.0, read=6.0))

        if r.status_code != 200:
            print(f"[getnum] provider={provider.get('name')} status={r.status_code} body={r.text[:300]}")
            return None
        data = r.json()
        # unwrap common "data" wrapper if present
        nd = data.get("data", data) if isinstance(data, dict) else data
        number_fields = provider.get("number_fields", ["number", "phonenumber", "full_number"])
        num = _dig(nd, *number_fields) if isinstance(nd, dict) else None
        if not num and isinstance(data, dict):
            num = _dig(data, *number_fields)
        # Fallback: if the configured field names didn't match, do a deep
        # recursive search before giving up — handles panels that nest the
        # number differently than expected, without changing the primary path.
        if not num:
            num = _deep_find_number(data, list(number_fields) + [
                "full_number", "no_plus_number", "phone_number", "phone",
                "mobile", "msisdn", "number", "copy"
            ])
        if num:
            otp = _dig(nd if isinstance(nd, dict) else {}, "otp", "otp_code", "code", "verification_code")
            sms = _dig(nd if isinstance(nd, dict) else {}, "sms", "message", "full_sms", "text")
            return {"number": num, "otp_now": bool(otp), "otp": otp, "sms": sms,
                    "provider_name": provider.get("name", "")}
        print(f"[getnum] provider={provider.get('name')} no number field matched in response: {str(data)[:300]}")
    except Exception as e:
        print(f"[getnum] provider={provider.get('name')} exception: {e}")
    return None

async def fetch_number_async(range_str):
    """Multi-panel aware: tries every ENABLED provider in rotated order
    (round-robin, so repeated calls spread across panels over time) and
    returns the first successful allocation — so if one panel is out of
    stock or down, it automatically falls through to the next. With only
    one provider configured/enabled, this behaves exactly like single-panel
    mode (one attempt, same as before)."""
    for pid in get_rotated_provider_ids():
        provider = get_provider_by_id(pid)
        if not provider:
            continue
        result = await _fetch_number_from_provider(provider, range_str)
        if result:
            result["provider_id"] = pid
            return result
    return None

async def fast_allocate_number(query, context, range_text, sid):
    uid = query.from_user.id

    if is_user_banned(uid):
        await query.message.edit_text("🚫 YOU ARE BANNED 🚫")
        return

    res, clean_num = None, None
    # The API can occasionally hand back a number another concurrent request
    # just claimed — retry a couple of times instead of overwriting ownership.
    for _ in range(3):
        try:
            res = await fetch_number_async(range_text)
        except Exception as e:
            await query.message.edit_text(f"❌ Server error: {str(e)[:100]}")
            return
        if not res or not res.get("number"):
            break
        clean_num = await register_active_number(uid, res["number"], range_text, sid, res.get("provider_id"))
        if clean_num:
            break

    if not res or not res.get("number") or not clean_num:
        await query.message.edit_text(
            "❌ <b>Number পাওয়া যায়নি।</b>\n\n"
            "<blockquote>⚠️ এই range-এ এখন number নেই বা server busy।\n"
            "আরেকটি range চেষ্টা করুন।</blockquote>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 BACK", callback_data="back_services", api_kwargs={"style": "danger"})
            ]])
        )
        return

    add_number_taken(uid, 1)
    last_range[uid] = range_text

    country_flag, country_name = get_country_info(clean_num)

    if res.get("otp_now") and res.get("otp"):
        otp_safe = html.escape(str(res["otp"]))
        sms_safe  = html.escape(str(res.get("sms") or ""))
        add_otp_received(uid)
        text = (
            f"✅ <b>YOUR NUMBER</b> ✅\n\n"
            f"<blockquote>🌍 COUNTRY: <code>{country_flag} {html.escape(country_name)}</code></blockquote>\n"
            f"<blockquote>📶 RANGE: <code>{range_text}</code></blockquote>\n"
            f"<blockquote>📞 NUMBER: <code>+{clean_num}</code></blockquote>\n"
            f"<blockquote>🔑 OTP: <code>{otp_safe}</code></blockquote>"
            + (f"\n<blockquote>📩 SMS: <code>{sms_safe}</code></blockquote>" if sms_safe else "")
            + "\n\n<b>✅ OTP RECEIVED INSTANTLY!</b>"
        )
    else:
        text = (
            f"✅ <b>YOUR NUMBER</b> ✅\n\n"
            f"<blockquote>🌍 COUNTRY: <code>{country_flag} {html.escape(country_name)}</code></blockquote>\n"
            f"<blockquote>📶 RANGE: <code>{range_text}</code></blockquote>\n"
            f"<blockquote>📞 NUMBER: <code>+{clean_num}</code></blockquote>\n\n"
            f"<b>📩 SMS STATUS: ⏳ WAITING...</b>"
        )

    # ★★★ USER BUTTONS UPDATED: CHANGE NUMBER + OTP GROUP + BACK ★★★
    keyboard = number_result_keyboard()
    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        print(f"fast_allocate edit error: {e}")

async def worker():
    while True:
        task = await request_queue.get()
        try:
            if task['type'] == 'process_numbers':
                await process_numbers(task['update'], task['context'], task['range_text'], task['count'])
            elif task['type'] == 'search_otp':
                await perform_otp_search(task['update'], task['context'], task['target_num'])
            elif task['type'] == 'auto_number':
                await process_auto_number(task['update'], task['context'], task['range_text'])
        except Exception as e:
            print(f"Worker Error: {e}")
        finally:
            request_queue.task_done()

# ==================== AUTO NUMBER FROM LINK / DEEP LINK ====================

async def process_auto_number(update, context, range_text):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    if is_user_banned(uid):
        await context.bot.send_message(chat_id=chat_id, text="🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return

    status_msg = await context.bot.send_message(chat_id=chat_id, text="🔍 SEARCHING...")

    try:
        res = await fetch_number_async(range_text)
        if not res:
            await status_msg.edit_text("❌ NO NUMBERS FOUND. TRY A VALID RANGE.")
            return

        generated_num = normalize_number(res["number"]) if res else None
        if not generated_num:
            await status_msg.edit_text("❌ NO NUMBERS FOUND. TRY A VALID RANGE.")
            return

        add_number_taken(uid, 1)
        last_range[uid] = range_text
        registered = await register_active_number(uid, generated_num, range_text, provider_id=res.get("provider_id"))
        if not registered:
            # Extremely rare: number already claimed elsewhere — keep it anyway,
            # this is a single-shot deep-link flow, not a shared allocation race.
            active_numbers[generated_num] = {"uid": uid, "range": range_text, "timestamp": datetime.now(),
                                              "provider_id": res.get("provider_id", "")}
            save_number_range_info(uid, generated_num, range_text)

        country_flag, country_name = get_country_info(generated_num)

        if res.get("otp_now") and res.get("otp"):
            instant_otp = html.escape(str(res["otp"]))
            instant_sms = html.escape(str(res.get("sms") or ""))
            add_otp_received(uid)
            final_text = (
                f"✅ <b>YOUR NUMBER DETAILS</b> ✅\n\n"
                f"<blockquote>🌍 COUNTRY: <code>{country_flag} {country_name}</code></blockquote>\n"
                f"<blockquote>📶 RANGE: <code>{range_text}</code></blockquote>\n\n"
                f"<blockquote>📞 NUMBER: <code>+{generated_num}</code></blockquote>\n\n"
                f"<blockquote>🔑 OTP: <code>{instant_otp}</code></blockquote>\n"
                + (f"<blockquote>📩 SMS: <code>{instant_sms}</code></blockquote>\n" if instant_sms else "")
                + f"\n<b>✅ OTP RECEIVED INSTANTLY!</b>"
            )
        else:
            final_text = (
                f"✅ <b>YOUR NUMBER DETAILS</b> ✅\n\n"
                f"<blockquote>🌍 COUNTRY: <code>{country_flag} {country_name}</code></blockquote>\n"
                f"<blockquote>📶 RANGE: <code>{range_text}</code></blockquote>\n\n"
                f"<blockquote>📞 NUMBER: <code>+{generated_num}</code></blockquote>\n\n"
                f"<b>📩 SMS STATUS: ⏳ WAITING...</b>"
            )

        # ★★★ CHANGE NUMBER + OTP GROUP + BACK (AUTO NUMBER) ★★★
        keyboard = number_result_keyboard()
        await status_msg.edit_text(final_text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        print(f"Auto Number Error: {e}")
        await status_msg.edit_text(f"❌ Error: {str(e)}")

# ==================== USER PANEL — PROCESS NUMBERS ====================

async def process_numbers(update_or_query, context, range_text, count):
    if isinstance(update_or_query, Update) and update_or_query.callback_query:
        uid = update_or_query.callback_query.from_user.id
        chat_id = update_or_query.callback_query.message.chat_id
    else:
        uid = update_or_query.effective_user.id
        chat_id = update_or_query.effective_chat.id

    if is_user_banned(uid):
        await context.bot.send_message(chat_id=chat_id, text="🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return

    status_msg = await context.bot.send_message(chat_id=chat_id, text="🔍 SEARCHING . . .")

    try:
        add_number_taken(uid, count)
        last_range[uid] = range_text

        tasks = [fetch_number_async(range_text) for _ in range(count)]
        results = await asyncio.gather(*tasks)
        valid_results = [r for r in results if r and r.get("number")]

        if not valid_results:
            await status_msg.edit_text("❌ NO NUMBERS FOUND. TRY A VALID RANGE.")
            return

        num_entries = []
        for r in valid_results:
            clean_num = await register_active_number(uid, r["number"], range_text, provider_id=r.get("provider_id"))
            if clean_num:
                num_entries.append({
                    "num":     clean_num,
                    "otp_now": r.get("otp_now", False),
                    "otp":     r.get("otp"),
                    "sms":     r.get("sms"),
                })

        if not num_entries:
            await status_msg.edit_text("❌ NO NUMBERS FOUND. TRY A VALID RANGE.")
            return

        country_flag, country_name = get_country_info(num_entries[0]["num"])

        num_lines = []
        for entry in num_entries:
            if entry["otp_now"] and entry["otp"]:
                otp_safe = html.escape(str(entry["otp"]))
                sms_safe = html.escape(str(entry.get("sms") or ""))
                add_otp_received(uid)
                line = (
                    f"<blockquote>📞 NUMBER: <code>+{entry['num']}</code>\n"
                    f"🔑 OTP: <code>{otp_safe}</code>"
                    + (f"\n📩 SMS: <code>{sms_safe}</code>" if sms_safe else "")
                    + "</blockquote>"
                )
            else:
                line = f"<blockquote>📞 NUMBER: <code>+{entry['num']}</code></blockquote>"
            num_lines.append(line)

        num_list_text = "\n".join(num_lines)
        any_instant = any(e["otp_now"] and e["otp"] for e in num_entries)
        sms_status = "✅ OTP RECEIVED INSTANTLY!" if any_instant else "📩 SMS STATUS: ⏳ WAITING..."

        final_text = (
            f"✅ <b>YOUR NUMBER DETAILS</b> ✅\n\n"
            f"<blockquote>🌍 COUNTRY: <code>{country_flag} {country_name}</code></blockquote>\n"
            f"<blockquote>📶 RANGE: <code>{range_text}</code></blockquote>\n\n"
            f"{num_list_text}\n\n"
            f"<b>{sms_status}</b>"
        )

        # ★★★ CHANGE NUMBER + OTP GROUP + BACK (PROCESS NUMBERS) ★★★
        keyboard = number_result_keyboard()

        await status_msg.edit_text(final_text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        print(f"Process Number Error: {e}")
        await status_msg.edit_text(f"❌ System Error: {str(e)}")

async def perform_otp_search(update, context, target_num):
    uid = str(update.effective_user.id)

    if is_user_banned(int(uid)):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(int(uid)))
        return

    status_msg = await update.message.reply_text("🔍 SEARCHING IN SERVER...")

    try:
        # Multi-panel aware: the target number could have come from any
        # enabled provider, so check all of them and merge results.
        raw_otps = []
        any_provider_ok = False
        for pid in get_enabled_provider_ids():
            provider = get_provider_by_id(pid)
            if not provider:
                continue
            base = _provider_base(provider)
            otp_path = provider.get("otp_path", "")
            if not base or not otp_path:
                continue
            any_provider_ok = True
            raw_otps.extend(await _fetch_otps_from_provider(provider))
        if not any_provider_ok:
            await status_msg.edit_text("❌ Active provider has no OTP search endpoint configured.")
            await update.message.reply_text("🔙 Returning to Main Menu...", reply_markup=main_keyboard(int(uid)))
            return
        if True:
            all_otps = raw_otps
            found_otps = [o for o in all_otps if normalize_number(o.get("number", "")) == target_num]

            if not found_otps:
                error_msg = (
                    "━━━━━━━━━━━━━━━━━━\n❌ NO OTP FOUND\n━━━━━━━━━━━━━━━━━━\n\n"
                    f"📞 NUMBER:\n`+{target_num}`\n\n⏳ PLEASE TRY AGAIN LATER\n━━━━━━━━━━━━━━━━━━"
                )
                await status_msg.edit_text(error_msg, parse_mode="Markdown")
                await update.message.reply_text("🔙 RETURNING TO MAIN MENU...", reply_markup=main_keyboard(int(uid)))
            else:
                await status_msg.delete()
                paid_data = load_data(PAID_SMS_FILE)

                for o in found_otps:
                    full_sms = o.get('message') or o.get('sms') or o.get('otp') or "No Content Found"
                    otp_code = extract_otp(full_sms)
                    otp_id = str(o.get("otp_id", ""))
                    sms_key = otp_id if otp_id else f"{target_num}_{full_sms}"

                    if sms_key in paid_data:
                        payment_status = "❌ ALREADY PAID"
                    else:
                        otp_rate = load_settings().get("otp_bonus", OTP_RATE)
                        await update_db_balance(uid, otp_rate)
                        add_otp_received(uid)
                        paid_data[sms_key] = {"uid": uid, "otp": otp_code}
                        payment_status = f"💵 ADD BALANCE FOR {otp_rate:.2f} BDT"

                    save_data(paid_data, PAID_SMS_FILE)
                    country_flag, country_name = get_country_info(target_num)
                    service_name = detect_service(full_sms)

                    msg = (
                        f"✅ <b>OTP FOUND!</b>\n\n"
                        f"<blockquote>🌍 COUNTRY: <code>{country_flag} {country_name}</code></blockquote>\n"
                        f"<blockquote>📱 SERVICE: <code>{service_name}</code></blockquote>\n"
                        f"<blockquote>📞 NUMBER: <code>+{target_num}</code></blockquote>\n"
                        f"<blockquote>🔑 OTP: <code>{html.escape(otp_code)}</code></blockquote>\n\n"
                        f"<blockquote>📩 FULL SMS:\n<code>{html.escape(str(full_sms))}</code></blockquote>\n\n"
                        f"<b>{payment_status}</b>"
                    )
                    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=main_keyboard(int(uid)))
        else:
            await status_msg.edit_text("❌ SERVER RETURNED AN ERROR.")
            await update.message.reply_text("🔙 Returning to Main Menu...", reply_markup=main_keyboard(int(uid)))

    except Exception as e:
        try:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
        except:
            await update.message.reply_text(f"❌ Error: {str(e)}")
        await update.message.reply_text("🔙 Returning to Main Menu...", reply_markup=main_keyboard(int(uid)))

# ==================== REFER AND EARN SECTION ====================

async def refer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if is_user_banned(uid):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return

    user_data = get_user(uid)
    bot_info = await context.bot.get_me()

    referral_link = f"https://t.me/{bot_info.username}?start={uid}"
    successful_refers = get_referral_count(uid)
    referral_bonus = load_settings().get("referral_bonus", REFERRAL_PRICE)
    total_reward = float(successful_refers) * referral_bonus

    refer_msg = (
        f"🎁 <b>REFER AND EARN SYSTEM</b> 🎁\n\n"
        f"<blockquote>🚀 INVITE FRIENDS &amp; EARN {referral_bonus:.2f} BDT EACH! 💸</blockquote>\n\n"
        f"<b>🔗 YOUR REFERRAL LINK:</b>\n"
        f"<blockquote><code>{referral_link}</code></blockquote>\n\n"
        f"<b>📊 YOUR STATS:</b>\n"
        f"<blockquote>👥 TOTAL REFERS: {successful_refers}\n"
        f"💰 TOTAL EARNED: {format_balance(total_reward)} BDT</blockquote>\n\n"
        f"✨ <b>SHARE LINK &amp; EARN MONEY!</b> ✨"
    )

    await update.message.reply_text(
        refer_msg,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("👥 YOUR REFERRAL", callback_data=f"my_ref_{uid}", api_kwargs={"style": "primary"})
        ]])
    )

# ==================== WITHDRAW FUNCTIONS ====================

async def withdraw_method_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = update.effective_user.id

    if text == "CANCEL":
        context.user_data["withdraw_mode"] = None
        await update.message.reply_text("❌ WITHDRAW CANCELLED", reply_markup=main_keyboard(uid))
        return

    method_map = {"BKASH": "BKASH", "NAGAD": "NAGAD", "ROCKET": "ROCKET", "BINANCE": "BINANCE"}
    if text in method_map:
        balance = get_user(uid)['balance']
        context.user_data["withdraw_method"] = method_map[text]
        context.user_data["withdraw_mode"] = "amount"
        msg = (
            f"<blockquote>💸 SEND YOUR AMOUNT!\n"
            f"💵 TOTAL BALANCE: {format_balance(balance)} BDT</blockquote>\n\n"
            f"<blockquote>📉 MINIMUM WITHDRAW {MIN_WITHDRAW} BDT</blockquote>"
        )
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=cancel_keyboard())
    else:
        await update.message.reply_text("⚠️ PLEASE SELECT A VALID PAYMENT METHOD!", reply_markup=withdraw_method_keyboard())

async def withdraw_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = update.effective_user.id

    if text == "CANCEL":
        context.user_data["withdraw_mode"] = None
        await update.message.reply_text("❌ WITHDRAW CANCELLED", reply_markup=main_keyboard(uid))
        return

    try:
        amount = float(text)
    except:
        await update.message.reply_text("⚠️ PLEASE SEND A VALID AMOUNT!", reply_markup=cancel_keyboard())
        return

    balance = get_user(uid)['balance']
    if amount < MIN_WITHDRAW or amount > MAX_WITHDRAW:
        await update.message.reply_text(f"📉 MIN: {MIN_WITHDRAW} BDT | MAX: {MAX_WITHDRAW} BDT", reply_markup=cancel_keyboard())
        return
    if amount > balance:
        await update.message.reply_text("🚫 INSUFFICIENT BALANCE!", reply_markup=cancel_keyboard())
        return

    context.user_data["withdraw_amount"] = amount
    context.user_data["withdraw_mode"] = "number"
    await update.message.reply_text(
        "📞 PLEASE SEND YOUR PAYMENT NUMBER!\n\n<blockquote>🔢 EXAMPLE: 017XXXXXXXX</blockquote>",
        parse_mode="HTML", reply_markup=cancel_keyboard()
    )

async def withdraw_number_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = update.effective_user.id

    if text == "CANCEL":
        context.user_data["withdraw_mode"] = None
        await update.message.reply_text("❌ WITHDRAW CANCELLED", reply_markup=main_keyboard(uid))
        return

    if not is_valid_bangladesh_number(text):
        await update.message.reply_text("⚠️ PLEASE SEND VALID NUMBER! 017XXXXXXXX", reply_markup=cancel_keyboard())
        return

    method = context.user_data.get("withdraw_method")
    amount = context.user_data.get("withdraw_amount")
    payment_number = text
    payment_id = generate_payment_id()

    context.user_data["temp_withdraw"] = {
        "method": method, "amount": amount,
        "number": payment_number, "payment_id": payment_id
    }

    msg = (
        "✨ <b>YOUR PAYMENT DETAILS!</b> ✨\n\n"
        f"<blockquote>📝 METHOD: {method}\n"
        f"📞 NUMBER: {payment_number}\n\n"
        f"✅ CORRECT → CONFIRM\n❌ WRONG → CANCEL</blockquote>"
    )
    await update.message.reply_text(
        msg, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ CANCEL", callback_data="withdraw_cancel", api_kwargs={"style": "danger"}),
            InlineKeyboardButton("✅ CONFIRM", callback_data="withdraw_confirm", api_kwargs={"style": "success"})
        ]])
    )

async def process_withdraw_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()

    temp_data = context.user_data.get("temp_withdraw")
    if not temp_data:
        await query.message.reply_text("⚠️ SESSION EXPIRED.", reply_markup=main_keyboard(uid))
        return

    method = temp_data["method"]
    amount = temp_data["amount"]
    payment_number = temp_data["number"]
    payment_id = temp_data["payment_id"]

    new_balance = await update_db_balance(uid, -amount)
    wr = load_withdraw_requests()
    wr[str(payment_id)] = {
        "user_id": uid, "method": method, "amount": amount,
        "number": payment_number, "payment_id": payment_id,
        "status": "pending", "timestamp": datetime.now().isoformat()
    }
    save_withdraw_requests(wr)

    await query.message.edit_text(
        f"✅ <b>WITHDRAWAL REQUEST SUBMITTED</b> ✅\n\n"
        f"<blockquote>📝 METHOD: <code>{method}</code>\n"
        f"📞 NUMBER: <code>{payment_number}</code>\n"
        f"💰 AMOUNT: <code>{format_balance(amount)} BDT</code>\n"
        f"🆔 ID: <code>{payment_id}</code></blockquote>",
        parse_mode="HTML"
    )
    await context.bot.send_message(uid, "🎉 <b>WITHDRAW REQUEST SUBMITTED!</b>", parse_mode="HTML", reply_markup=main_keyboard(uid))

    admin_msg = (
        f"✅ <b>NEW WITHDRAWAL REQUEST</b>\n\n"
        f"<blockquote>🆔 USER: <code>{uid}</code>\n"
        f"📝 METHOD: <code>{method}</code>\n"
        f"📞 NUMBER: <code>{payment_number}</code>\n"
        f"💰 AMOUNT: <code>{format_balance(amount)} BDT</code>\n"
        f"🆔 ID: <code>{payment_id}</code></blockquote>"
    )
    admin_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ REJECT", callback_data=f"admin_reject_{payment_id}", api_kwargs={"style": "danger"}),
        InlineKeyboardButton("✅ APPROVE", callback_data=f"admin_approve_{payment_id}", api_kwargs={"style": "success"})
    ]])
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(admin_id, admin_msg, parse_mode="HTML", reply_markup=admin_kb)
        except Exception as e:
            print(f"Admin notify fail {admin_id}: {e}")

    context.user_data["temp_withdraw"] = None
    context.user_data["withdraw_mode"] = None

async def process_withdraw_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()
    context.user_data["temp_withdraw"] = None
    context.user_data["withdraw_mode"] = None
    await query.message.edit_text("❌ WITHDRAW CANCELLED")
    await context.bot.send_message(uid, "🔹 PLEASE USE THE BUTTONS BELOW:", reply_markup=main_keyboard(uid))

# ==================== ADMIN PANEL - WITHDRAW APPROVAL ====================

async def admin_approve_withdraw(update, context, payment_id):
    query = update.callback_query
    await query.answer()
    wr = load_withdraw_requests()
    if payment_id not in wr:
        await query.message.reply_text("⚠️ REQUEST NOT FOUND!")
        return
    rd = wr[payment_id]
    uid = rd["user_id"]
    method = rd["method"]
    amount = rd["amount"]
    payment_number = rd["number"]
    wr[payment_id]["status"] = "approved"
    save_withdraw_requests(wr)

    try:
        await context.bot.send_message(
            uid,
            f"🎉 <b>WITHDRAWAL APPROVED!</b>\n\n"
            f"<blockquote>📝 METHOD: <code>{method}</code>\n"
            f"📞 NUMBER: <code>{payment_number}</code>\n"
            f"💰 AMOUNT: <code>{format_balance(amount)} BDT</code></blockquote>",
            parse_mode="HTML"
        )
    except:
        pass
    await query.message.edit_text(f"✅ APPROVED | User: {uid} | Amount: {format_balance(amount)} BDT")

async def admin_reject_withdraw(update, context, payment_id):
    query = update.callback_query
    await query.answer()
    wr = load_withdraw_requests()
    if payment_id not in wr:
        await query.message.reply_text("⚠️ REQUEST NOT FOUND!")
        return
    rd = wr[payment_id]
    uid = rd["user_id"]
    amount = rd["amount"]
    wr[payment_id]["status"] = "rejected"
    save_withdraw_requests(wr)

    try:
        await context.bot.send_message(uid, "❌ **WITHDRAWAL REQUEST REJECTED**\n\nContact admin for more info.", parse_mode="Markdown")
    except:
        pass
    await query.message.edit_text(f"❌ REJECTED | User: {uid} | Amount: {format_balance(amount)} BDT")

# ==================== ADMIN PANEL - BALANCE MANAGEMENT ====================

async def admin_add_balance_start(update, context):
    context.user_data["add_balance_mode"] = True
    context.user_data["remove_balance_mode"] = False
    await update.message.reply_text("💰 SEND USER ID TO ADD BALANCE:")

async def admin_remove_balance_start(update, context):
    context.user_data["remove_balance_mode"] = True
    context.user_data["add_balance_mode"] = False
    await update.message.reply_text("💸 SEND USER ID TO REMOVE BALANCE:")

async def process_add_balance_user(update, context):
    uid_to_add = update.message.text.strip()
    if not uid_to_add.isdigit():
        await update.message.reply_text("❌ INVALID USER ID!")
        return
    uid_to_add_int = int(uid_to_add)
    if not user_exists(uid_to_add_int):
        await update.message.reply_text("❌ USER NOT FOUND!")
        context.user_data["add_balance_mode"] = False
        return
    context.user_data["pending_add_user"] = uid_to_add_int
    await update.message.reply_text("💵 SEND AMOUNT TO ADD:")

async def process_remove_balance_user(update, context):
    uid_to_remove = update.message.text.strip()
    if not uid_to_remove.isdigit():
        await update.message.reply_text("❌ INVALID USER ID!")
        return
    uid_to_remove_int = int(uid_to_remove)
    if not user_exists(uid_to_remove_int):
        await update.message.reply_text("❌ USER NOT FOUND!")
        context.user_data["remove_balance_mode"] = False
        return
    context.user_data["pending_remove_user"] = uid_to_remove_int
    await update.message.reply_text("💸 SEND AMOUNT TO REMOVE:")

async def process_add_balance_amount(update, context):
    try:
        amount = float(update.message.text.strip())
        if amount <= 0: raise ValueError
    except:
        await update.message.reply_text("❌ INVALID AMOUNT!")
        return
    uid = context.user_data.get("pending_add_user")
    if not uid:
        context.user_data["add_balance_mode"] = False
        await update.message.reply_text("⚠️ SESSION EXPIRED.")
        return
    old_balance = get_user(uid).get("balance", 0)
    new_balance = await update_db_balance(uid, amount)
    await update.message.reply_text(
        f"✅ **ADD BALANCE SUCCESSFUL**\n🆔 USER: `{uid}`\n"
        f"💰 ADDED: `{format_balance(amount)} BDT`\n"
        f"📈 NEW BALANCE: `{format_balance(new_balance)} BDT`",
        parse_mode="Markdown"
    )
    try:
        await context.bot.send_message(uid, f"🎉 ADMIN ADDED `{format_balance(amount)} BDT` TO YOUR ACCOUNT!\n💵 NEW BALANCE: `{format_balance(new_balance)} BDT`", parse_mode="Markdown")
    except:
        pass
    context.user_data["add_balance_mode"] = False
    context.user_data["pending_add_user"] = None

async def process_remove_balance_amount(update, context):
    try:
        amount = float(update.message.text.strip())
        if amount <= 0: raise ValueError
    except:
        await update.message.reply_text("❌ INVALID AMOUNT!")
        return
    uid = context.user_data.get("pending_remove_user")
    if not uid:
        context.user_data["remove_balance_mode"] = False
        await update.message.reply_text("⚠️ SESSION EXPIRED.")
        return
    old_balance = get_user(uid).get("balance", 0)
    if amount > old_balance:
        await update.message.reply_text(f"❌ INSUFFICIENT BALANCE! Current: {format_balance(old_balance)} BDT")
        context.user_data["remove_balance_mode"] = False
        context.user_data["pending_remove_user"] = None
        return
    new_balance = await update_db_balance(uid, -amount)
    await update.message.reply_text(
        f"✅ **REMOVE BALANCE SUCCESSFUL**\n🆔 USER: `{uid}`\n"
        f"💸 REMOVED: `{format_balance(amount)} BDT`\n"
        f"📉 NEW BALANCE: `{format_balance(new_balance)} BDT`",
        parse_mode="Markdown"
    )
    try:
        await context.bot.send_message(uid, f"⚠️ ADMIN REMOVED `{format_balance(amount)} BDT` FROM YOUR ACCOUNT!\n💵 NEW BALANCE: `{format_balance(new_balance)} BDT`", parse_mode="Markdown")
    except:
        pass
    context.user_data["remove_balance_mode"] = False
    context.user_data["pending_remove_user"] = None

# ==================== ADMIN PANEL - BAN/UNBAN ====================

async def admin_ban_user_start(update, context):
    context.user_data["admin_ban_mode"] = True
    context.user_data["admin_unban_mode"] = False
    await update.message.reply_text("🚫 SEND TELEGRAM ID TO BAN USER:")

async def admin_unban_user_start(update, context):
    context.user_data["admin_unban_mode"] = True
    context.user_data["admin_ban_mode"] = False
    await update.message.reply_text("🔓 SEND TELEGRAM ID TO UNBAN USER:")

async def process_ban_user(update, context):
    uid_to_ban = update.message.text.strip()
    if not uid_to_ban.isdigit():
        await update.message.reply_text("❌ INVALID USER ID!")
        return
    uid_to_ban_int = int(uid_to_ban)
    if not user_exists(uid_to_ban_int):
        await update.message.reply_text("❌ USER NOT FOUND!")
        context.user_data["admin_ban_mode"] = False
        return
    if is_user_banned(uid_to_ban_int):
        await update.message.reply_text("⚠️ USER IS ALREADY BANNED!")
        context.user_data["admin_ban_mode"] = False
        return
    ban_user(uid_to_ban_int)
    try:
        await context.bot.send_message(uid_to_ban_int, "🚫 **YOU HAVE BEEN BANNED**\n📞 Contact support.", parse_mode="Markdown")
    except:
        pass
    await update.message.reply_text(f"✅ USER `{uid_to_ban}` BANNED!", parse_mode="Markdown", reply_markup=system_config_keyboard())
    context.user_data["admin_ban_mode"] = False

async def process_unban_user(update, context):
    uid_to_unban = update.message.text.strip()
    if not uid_to_unban.isdigit():
        await update.message.reply_text("❌ INVALID USER ID!")
        return
    uid_to_unban_int = int(uid_to_unban)
    if not is_user_banned(uid_to_unban_int):
        await update.message.reply_text("⚠️ THIS USER IS NOT BANNED!")
        context.user_data["admin_unban_mode"] = False
        return
    unban_user(uid_to_unban_int)
    try:
        await context.bot.send_message(uid_to_unban_int, "✅ **YOU HAVE BEEN UNBANNED!** Use /start", parse_mode="Markdown")
    except:
        pass
    await update.message.reply_text(f"✅ USER `{uid_to_unban}` UNBANNED!", parse_mode="Markdown", reply_markup=system_config_keyboard())
    context.user_data["admin_unban_mode"] = False

async def show_banned_users_list(update, context):
    banned_list = load_banned_users()
    if not banned_list:
        await update.message.reply_text("📜 NO BANNED USERS.", reply_markup=system_config_keyboard())
        return
    text = "📜 **BANNED USER LIST**\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, uid in enumerate(banned_list, 1):
        text += f"{i}. `{uid}`\n"
    text += f"\n📊 Total: {len(banned_list)}"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=system_config_keyboard())

# ==================== MESSAGE HANDLER SECTION ====================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    text = update.message.text.strip()

    # Force-join gate — applies to every action for non-admins (not just
    # unmatched text), and re-checks live membership every time, so leaving
    # a channel later blocks the bot again until they rejoin.
    if text != "CANCEL" and not is_admin(uid):
        if not await check_force_join(update, context):
            return

    # Withdraw flow
    if context.user_data.get("withdraw_mode") == "select_method":
        await withdraw_method_selected(update, context)
        return
    if context.user_data.get("withdraw_mode") == "amount":
        await withdraw_amount_received(update, context)
        return
    if context.user_data.get("withdraw_mode") == "number":
        await withdraw_number_received(update, context)
        return

    # Admin balance
    if context.user_data.get("add_balance_mode") and is_admin(uid):
        if context.user_data.get("pending_add_user"):
            await process_add_balance_amount(update, context)
        else:
            await process_add_balance_user(update, context)
        return
    if context.user_data.get("remove_balance_mode") and is_admin(uid):
        if context.user_data.get("pending_remove_user"):
            await process_remove_balance_amount(update, context)
        else:
            await process_remove_balance_user(update, context)
        return

    # Admin ban/unban
    if context.user_data.get("admin_ban_mode") and is_admin(uid):
        await process_ban_user(update, context)
        return
    if context.user_data.get("admin_unban_mode") and is_admin(uid):
        await process_unban_user(update, context)
        return

    # CUSTOM RANGE — user sent a range text
    if context.user_data.get("mode") == "custom_range":
        context.user_data["mode"] = None
        range_text = text.strip().upper()
        if not re.search(r'\d', range_text):
            await update.message.reply_text(
                "❌ <b>INVALID RANGE!</b>\n\n"
                "<blockquote>সঠিক উদাহরণ: <code>234XXX</code></blockquote>",
                parse_mode="HTML",
                reply_markup=main_keyboard(uid)
            )
            return
        await request_queue.put({
            'type': 'process_numbers',
            'update': update,
            'context': context,
            'range_text': range_text,
            'count': 1
        })
        return

    # Ban check
    if not is_admin(uid) and is_user_banned(uid):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return

    # Cancel
    if text == "CANCEL":
        context.user_data.clear()
        await update.message.reply_text("❌ CANCELLED", reply_markup=main_keyboard(uid))
        return

    # Main menu buttons
    if text == "PROFILE":
        user_data = get_user(uid)
        stats = get_user_stats(uid)
        user = update.effective_user
        update_user_profile(uid, user.full_name, user.username)
        full_name = html.escape(user.full_name)
        username = html.escape(user.username or "No username")
        profile_text = (
            f"👤 <b>YOUR PROFILE</b>\n\n"
            f"<blockquote>🏷️ NAME: <b>{full_name}</b></blockquote>\n"
            f"<blockquote>🆔 USERNAME: @{username}</blockquote>\n"
            f"<blockquote>🗝️ TELEGRAM ID: <code>{uid}</code></blockquote>\n\n"
            f"<blockquote>💵 BALANCE: <b>{format_balance(user_data.get('balance', 0))} BDT</b></blockquote>\n\n"
            f"✨ <b>TODAY</b>\n"
            f"<blockquote>📱 NUMBERS: {stats['today_numbers']}\n🔑 OTPS: {stats['today_otps']}</blockquote>\n\n"
            f"🔥 <b>LAST 7 DAYS</b>\n"
            f"<blockquote>📱 NUMBERS: {stats['last7d_numbers']}\n🔑 OTPS: {stats['last7d_otps']}</blockquote>\n\n"
            f"🌐 <b>ALL TIME</b>\n"
            f"<blockquote>📱 NUMBERS: {stats['total_numbers']}\n🔑 OTPS: {stats['total_otps']}</blockquote>"
        )
        await update.message.reply_text(profile_text, parse_mode="HTML")
        return

    if text == "BALANCE":
        user_data = get_user(uid)
        balance = user_data.get('balance', 0)
        stats = get_user_stats(uid)
        s = load_settings()
        otp_rate = s.get("otp_bonus", OTP_RATE)
        referral_bonus = s.get("referral_bonus", REFERRAL_PRICE)
        min_withdraw = s.get("min_withdraw", MIN_WITHDRAW)
        referral_count = get_referral_count(uid)
        today_earned = stats['today_otps'] * otp_rate
        total_otp_earned = stats['total_otps'] * otp_rate
        total_referral_earned = referral_count * referral_bonus
        await update.message.reply_text(
            f"💰 <b>YOUR BALANCE</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<blockquote>💵 CURRENT BALANCE: <b>{format_balance(balance)} BDT</b></blockquote>\n\n"
            f"✨ <b>TODAY</b>\n"
            f"<blockquote>🔑 OTPs: {stats['today_otps']} × {otp_rate:.2f} = {today_earned:.2f} BDT</blockquote>\n\n"
            f"🌐 <b>ALL TIME EARNED</b>\n"
            f"<blockquote>🔑 From OTPs: {stats['total_otps']} × {otp_rate:.2f} = {total_otp_earned:.2f} BDT\n"
            f"👥 From Referrals: {referral_count} × {referral_bonus:.2f} = {total_referral_earned:.2f} BDT</blockquote>\n\n"
            f"📉 <i>Minimum withdraw: {format_balance(min_withdraw)} BDT</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💸 WITHDRAW", callback_data="withdraw_start", api_kwargs={"style": "primary"})
            ]])
        )
        return

    if text == "REFER AND EARN":
        await refer_command(update, context)
        return

    # SEARCH OTP
    if text == "SEARCH OTP":
        context.user_data["mode"] = "search_otp"
        await update.message.reply_text("🔍 **ENTER THE NUMBER TO SEARCH OTP:**", parse_mode="Markdown")
        return

    if context.user_data.get("mode") == "search_otp":
        context.user_data["mode"] = None
        await request_queue.put({'type': 'search_otp', 'update': update, 'context': context, 'target_num': normalize_number(text)})
        return

    # GET 2FA
    if text == "GET 2FA":
        await get_2fa_code(update, context)
        return

    # GET NUMBER
    if text == "GET NUMBER":
        await show_app_selection(update, context)
        return

    if context.user_data.get("mode") == "get_2fa":
        await process_2fa_key(update, context)
        return

    # LEADERBOARD
    if text == "LEADERBOARD":
        await leaderboard_command(update, context)
        return

    # TOP HIT RANGES
    if text == "TOP HITS":
        await top_hit_ranges_command(update, context)
        return

    # SUPPORT BUTTON HANDLER
    if text == "SUPPORT":
        _s = load_settings()
        support_text = "💬 SUPPORT 🎧\n\nCLICK THE BUTTON BELOW TO CONTACT SUPPORT 📩"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(_s.get("support_btn_label") or "💬 SUPPORT", url=_s.get("support_link") or SUPPORT_LINK, api_kwargs={"style": "primary"})],
            [InlineKeyboardButton(_s.get("developer_btn_label") or "👨‍💻 DEVELOPER BY", url=_s.get("developer_link") or DEVELOPER_LINK, api_kwargs={"style": "danger"})]
        ])
        await update.message.reply_text(support_text, reply_markup=keyboard, parse_mode="Markdown")
        return

    # Admin panel
    if text == "ADMIN PANEL" and is_admin(uid):
        context.user_data["admin_mode"] = "main"
        await update.message.reply_text(
            "⌬━━━━━━━━━━━━━━━━━━━━⌬\n   WELCOME ADMIN PANEL\n⌬━━━━━━━━━━━━━━━━━━━━⌬",
            reply_markup=admin_main_keyboard()
        )
        return

    if text == "BACK TO MAIN" and context.user_data.get("admin_mode"):
        context.user_data["admin_mode"] = None
        await update.message.reply_text("🔙 Back to main menu.", reply_markup=main_keyboard(uid))
        return

    if text == "BACK TO ADMIN":
        context.user_data["user_management_mode"] = None
        context.user_data["system_config_mode"] = None
        context.user_data["admin_mode"] = "main"
        await update.message.reply_text("🔙 Back to admin panel.", reply_markup=admin_main_keyboard())
        return

    if text == "USER MANAGEMENT" and context.user_data.get("admin_mode") == "main" and is_admin(uid):
        context.user_data["user_management_mode"] = "main"
        await update.message.reply_text("👥 User Management:", reply_markup=user_management_keyboard())
        return

    if text == "SYSTEM CONFIGURATION" and context.user_data.get("admin_mode") == "main" and is_admin(uid):
        context.user_data["system_config_mode"] = "main"
        await update.message.reply_text("⚙️ System Configuration:", reply_markup=system_config_keyboard())
        return

    if text == "TODAY ALL STATUS" and context.user_data.get("system_config_mode") == "main" and is_admin(uid):
        t_n, t_o, s_n, s_o, tot_n, tot_o = get_global_system_stats()
        msg = (
            f"📊 <b>SYSTEM STATUS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✨ <b>TODAY</b>\n📱 NUMBERS: {t_n}\n🔑 OTPS: {t_o}\n\n"
            f"🔥 <b>LAST 7 DAYS</b>\n📱 NUMBERS: {s_n}\n🔑 OTPS: {s_o}\n\n"
            f"🌐 <b>ALL TIME</b>\n📱 NUMBERS: {tot_n}\n🔑 OTPS: {tot_o}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")
        return

    if text == "USER STATUS CHECK" and is_admin(uid):
        context.user_data["mode"] = "input_user_id"
        await update.message.reply_text("🔍 ENTER TELEGRAM ID:", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("mode") == "input_user_id" and is_admin(uid):
        target_uid = text.strip()
        if not target_uid.isdigit():
            await update.message.reply_text("❌ INVALID ID!")
            return
        context.user_data["mode"] = None
        stats = get_user_stats(target_uid)
        user_info = load_data(USER_DATA_FILE).get(target_uid, {})
        name = html.escape(user_info.get("full_name") or "N/A")
        username = html.escape(f"@{user_info['username']}" if user_info.get("username") else "no-username")
        balance = user_info.get("balance", 0)
        referral_count = get_referral_count(target_uid)
        is_banned = is_user_banned(target_uid)
        banned_tag = " 🚫 BANNED" if is_banned else ""
        msg = (
            f"👤 <b>USER STATUS</b> — <code>{target_uid}</code>{banned_tag}\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<blockquote>🏷️ NAME: {name}\n🆔 USERNAME: {username}\n💵 BALANCE: {balance:.2f} BDT\n"
            f"👥 REFERRALS: {referral_count}</blockquote>\n\n"
            f"✨ TODAY: 📱 {stats['today_numbers']} | 🔑 {stats['today_otps']}\n"
            f"🔥 7 DAYS: 📱 {stats['last7d_numbers']} | 🔑 {stats['last7d_otps']}\n"
            f"🌐 ALL TIME: 📱 {stats['total_numbers']} | 🔑 {stats['total_otps']}"
        )
        ban_btn = (InlineKeyboardButton("✅ UNBAN", callback_data=f"admin_unban_{target_uid}", api_kwargs={"style": "success"})
                   if is_banned else
                   InlineKeyboardButton("🚫 BAN", callback_data=f"admin_ban_{target_uid}", api_kwargs={"style": "danger"}))
        await update.message.reply_text(
            msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📂 CHECK ALL DATA", callback_data=f"full_logs_{target_uid}", api_kwargs={"style": "primary"})],
                [ban_btn]
            ])
        )
        return

    if text == "ALL USERS INFO" and context.user_data.get("user_management_mode") == "main" and is_admin(uid):
        user_db = load_data(USER_DATA_FILE)
        if user_db:
            lines = []
            for i, (uid_str, u) in enumerate(user_db.items(), 1):
                name = u.get("full_name") or "N/A"
                username = f"@{u.get('username')}" if u.get("username") else "no-username"
                balance = u.get("balance", 0)
                stats = get_user_stats(uid_str)
                banned = " [BANNED]" if is_user_banned(uid_str) else ""
                lines.append(
                    f"{i}. ID: {uid_str} | {name} | {username}{banned}\n"
                    f"   Balance: {balance:.2f} BDT | Numbers: {stats['total_numbers']} | OTPs: {stats['total_otps']}"
                )
            content = f"👥 TOTAL USERS: {len(user_db)}\n\n" + "\n\n".join(lines)
            f = io.BytesIO(content.encode()); f.name = f"ALL_USERS_{len(user_db)}.txt"
            await update.message.reply_document(document=f, caption=f"👥 Total Users: {len(user_db)}", reply_markup=user_management_keyboard())
        else:
            await update.message.reply_text("No users found.", reply_markup=user_management_keyboard())
        return

    if text == "ALL USER BALANCE" and context.user_data.get("user_management_mode") == "main" and is_admin(uid):
        user_db = load_data(USER_DATA_FILE)
        if user_db:
            total_bal = sum(v.get("balance", 0) for v in user_db.values())
            lines = [f"{i}. {uid_}: {v.get('balance', 0):.2f} BDT" for i, (uid_, v) in enumerate(user_db.items(), 1)]
            content = f"💰 TOTAL BALANCE: {total_bal:.2f} BDT\n\n" + "\n".join(lines)
            f = io.BytesIO(content.encode()); f.name = f"BALANCES_{total_bal:.0f}.txt"
            await update.message.reply_document(document=f, caption=f"💵 Total Balance: {total_bal:.2f} BDT", reply_markup=user_management_keyboard())
        else:
            await update.message.reply_text("No data.", reply_markup=user_management_keyboard())
        return

    if text == "BAN USER LIST" and is_admin(uid):
        await show_banned_users_list(update, context)
        return

    if text == "BAN USER" and context.user_data.get("system_config_mode") == "main" and is_admin(uid):
        await admin_ban_user_start(update, context)
        return

    if text == "UNBAN USER" and context.user_data.get("system_config_mode") == "main" and is_admin(uid):
        await admin_unban_user_start(update, context)
        return

    if text == "ADD BALANCE" and context.user_data.get("system_config_mode") == "main" and is_admin(uid):
        await admin_add_balance_start(update, context)
        return

    if text == "REMOVE BALANCE" and context.user_data.get("system_config_mode") == "main" and is_admin(uid):
        await admin_remove_balance_start(update, context)
        return

    # ==================== FIXED BROADCAST (TEXT, PHOTO, VIDEO, FILE, ETC.) ====================
    if text == "SEND MESSAGE TO ALL USERS" and is_admin(uid):
        context.user_data["broadcast_mode"] = True
        await update.message.reply_text(
            "📢 <b>ADMIN BROADCAST SYSTEM (PRO)</b>\n\n"
            "💬 আপনি এখন যা পাঠাবেন (Text, Photo, Video, Document, Voice, Audio, Animation, Sticker) – সকল ইউজারের কাছে প্রফেশনাল হেডারসহ চলে যাবে।\n\n"
            "✨ রেঞ্জ (যেমন: 237XXX) থাকলে তা অটোমেটিক ক্লিক-টু-কপি হয়ে যাবে।", 
            parse_mode="HTML", 
            reply_markup=cancel_keyboard()
        )
        return

    if context.user_data.get("broadcast_mode") and is_admin(uid):
        context.user_data["broadcast_mode"] = False
        
        user_db = load_data(USER_DATA_FILE)
        all_uids = list(user_db.keys())
        
        if not all_uids:
            await update.message.reply_text("❌ পাঠানোর জন্য কোনো ইউজার পাওয়া যায়নি!")
            return

        success_ids, fail_ids = [], []
        status_msg = await update.message.reply_text(f"🚀 <b>ব্রডকাস্ট শুরু হয়েছে...</b>\n🎯 টার্গেট: {len(all_uids)} জন ইউজার।", parse_mode="HTML")

        def format_broadcast_caption(caption_text):
            if not caption_text:
                return "<blockquote>📢 <b>ADMIN NOTICE :</b></blockquote>"
            formatted = re.sub(r'(\d{3,}[xX]{3,})', r'<code>\1</code>', str(caption_text))
            return f"<blockquote>📢 <b>ADMIN NOTICE :</b></blockquote>\n\n{formatted}"

        for user_id_str in all_uids:
            try:
                target_id = int(user_id_str)
                
                # টেক্সট মেসেজ
                if update.message.text:
                    await context.bot.send_message(
                        chat_id=target_id, 
                        text=format_broadcast_caption(update.message.text), 
                        parse_mode="HTML"
                    )
                # ফটো
                elif update.message.photo:
                    caption = format_broadcast_caption(update.message.caption) if update.message.caption else None
                    await context.bot.send_photo(
                        chat_id=target_id,
                        photo=update.message.photo[-1].file_id,
                        caption=caption,
                        parse_mode="HTML" if caption else None
                    )
                # ভিডিও
                elif update.message.video:
                    caption = format_broadcast_caption(update.message.caption) if update.message.caption else None
                    await context.bot.send_video(
                        chat_id=target_id,
                        video=update.message.video.file_id,
                        caption=caption,
                        parse_mode="HTML" if caption else None
                    )
                # ডকুমেন্ট (যেকোনো ফাইল)
                elif update.message.document:
                    caption = format_broadcast_caption(update.message.caption) if update.message.caption else None
                    await context.bot.send_document(
                        chat_id=target_id,
                        document=update.message.document.file_id,
                        caption=caption,
                        parse_mode="HTML" if caption else None
                    )
                # অডিও
                elif update.message.audio:
                    caption = format_broadcast_caption(update.message.caption) if update.message.caption else None
                    await context.bot.send_audio(
                        chat_id=target_id,
                        audio=update.message.audio.file_id,
                        caption=caption,
                        parse_mode="HTML" if caption else None
                    )
                # ভয়েস
                elif update.message.voice:
                    caption = format_broadcast_caption(update.message.caption) if update.message.caption else None
                    await context.bot.send_voice(
                        chat_id=target_id,
                        voice=update.message.voice.file_id,
                        caption=caption,
                        parse_mode="HTML" if caption else None
                    )
                # অ্যানিমেশন (GIF)
                elif update.message.animation:
                    caption = format_broadcast_caption(update.message.caption) if update.message.caption else None
                    await context.bot.send_animation(
                        chat_id=target_id,
                        animation=update.message.animation.file_id,
                        caption=caption,
                        parse_mode="HTML" if caption else None
                    )
                # স্টিকার
                elif update.message.sticker:
                    await context.bot.send_sticker(
                        chat_id=target_id,
                        sticker=update.message.sticker.file_id
                    )
                # অন্য সব কিছু (ফরোয়ার্ড/কপি)
                else:
                    try:
                        await context.bot.copy_message(
                            chat_id=target_id,
                            from_chat_id=update.message.chat_id,
                            message_id=update.message.message_id
                        )
                    except:
                        await context.bot.send_message(
                            chat_id=target_id,
                            text="📢 <b>ADMIN NOTICE :</b>\n\nআপনার জন্য একটি নতুন বার্তা আছে, কিন্তু এটি প্রদর্শন করা সম্ভব হয়নি।",
                            parse_mode="HTML"
                        )
                success_ids.append(user_id_str)
            except Exception as e:
                print(f"Broadcast fail to {user_id_str}: {e}")
                fail_ids.append(user_id_str)
            
            await asyncio.sleep(0.05)

        report_text = (
            f"✅ <b>ADMIN NOTICE COMPLETE !</b>\n\n"
            f"📊 <b>BROADCAST REPORT:</b>\n\n"
            f"<blockquote>✅ SUCCESSFULLY SENT: {len(success_ids)} USERS !</blockquote>\n"
            f"<blockquote>❌ FAILED TO SEND: {len(fail_ids)} USERS !</blockquote>"
        )
        
        await status_msg.delete()
        await context.bot.send_message(chat_id=uid, text=report_text, parse_mode="HTML", reply_markup=main_keyboard(uid))

        random_suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if success_ids:
            s_file = io.BytesIO(("\n".join(success_ids)).encode()); s_file.name = f"SUCCESS_{random_suffix}.txt"
            await context.bot.send_document(chat_id=uid, document=s_file, caption="✅ Success User List")
        if fail_ids:
            f_file = io.BytesIO(("\n".join(fail_ids)).encode()); f_file.name = f"FAILED_{random_suffix}.txt"
            await context.bot.send_document(chat_id=uid, document=f_file, caption="❌ Failed User List")
        
        return

    # ==================== BOT SETTINGS NAVIGATION ====================
    if text == "BOT SETTINGS" and context.user_data.get("admin_mode") == "main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "main"
        context.user_data["zenex_config_mode"] = None
        await update.message.reply_text("⚙️ <b>BOT SETTINGS</b>", parse_mode="HTML", reply_markup=bot_settings_keyboard())
        return

    if text == "BACK TO BOT SETTINGS" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "main"
        context.user_data["zenex_config_mode"] = None
        await update.message.reply_text("⚙️ <b>BOT SETTINGS</b>", parse_mode="HTML", reply_markup=bot_settings_keyboard())
        return

    if text == "BACK TO LINKS & CHANNELS" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "links_main"
        await update.message.reply_text("🔗 <b>LINKS & CHANNELS</b>", parse_mode="HTML", reply_markup=links_config_keyboard())
        return

    if text == "API PROVIDER CONFIG" and context.user_data.get("bot_settings_mode") == "main" and is_admin(uid):
        context.user_data["zenex_config_mode"] = "main"
        await update.message.reply_text("📡 <b>API PROVIDER CONFIG</b>\n\n<blockquote>এখানে আপনি যেকোনো সাইটের (Zenex, miahsms বা অন্য কোনো panel) API যোগ করতে ও সুইচ করতে পারবেন।</blockquote>", parse_mode="HTML", reply_markup=provider_config_keyboard())
        return

    if text == "SERVICE MANAGEMENT" and context.user_data.get("admin_mode") == "main" and is_admin(uid):
        context.user_data["service_mgmt_mode"] = "main"
        await update.message.reply_text("🔧 <b>SERVICE MANAGEMENT</b>", parse_mode="HTML", reply_markup=service_management_keyboard())
        return

    # ==================== ADD / REMOVE ADMIN ====================
    if text == "ADD ADMIN" and context.user_data.get("bot_settings_mode") == "main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "add_admin"
        s = load_settings()
        admin_list = "\n".join(f"• <code>{a}</code>" for a in s.get("admins", []))
        await update.message.reply_text(
            f"➕ <b>ADD ADMIN</b>\n\n<blockquote>Current admins:\n{admin_list}</blockquote>\n\n"
            f"Send the <b>Telegram User ID</b> to add as admin:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "add_admin" and is_admin(uid):
        if not text.lstrip("-").isdigit():
            await update.message.reply_text("❌ Invalid ID! Send a numeric Telegram ID.")
            return
        new_admin_id = int(text)
        s = load_settings()
        admins = s.get("admins", [])
        if new_admin_id in admins:
            await update.message.reply_text("⚠️ This user is already an admin!", reply_markup=bot_settings_keyboard())
        else:
            admins.append(new_admin_id)
            s["admins"] = admins
            save_settings(s)
            await update.message.reply_text(
                f"✅ <b>ADMIN ADDED!</b>\n<blockquote>🆔 ID: <code>{new_admin_id}</code></blockquote>",
                parse_mode="HTML", reply_markup=bot_settings_keyboard())
        context.user_data["bot_settings_mode"] = "main"
        return

    if text == "REMOVE ADMIN" and context.user_data.get("bot_settings_mode") == "main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "remove_admin"
        s = load_settings()
        admin_list = "\n".join(f"• <code>{a}</code>" for a in s.get("admins", []))
        await update.message.reply_text(
            f"➖ <b>REMOVE ADMIN</b>\n\n<blockquote>Current admins:\n{admin_list}</blockquote>\n\n"
            f"Send the <b>Telegram User ID</b> to remove from admin:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "remove_admin" and is_admin(uid):
        if not text.lstrip("-").isdigit():
            await update.message.reply_text("❌ Invalid ID! Send a numeric Telegram ID.")
            return
        rem_id = int(text)
        s = load_settings()
        admins = s.get("admins", [])
        owners = s.get("owners", [])
        if rem_id in owners and rem_id == uid:
            await update.message.reply_text("❌ Cannot remove yourself (owner)!", reply_markup=bot_settings_keyboard())
        elif rem_id not in admins:
            await update.message.reply_text("⚠️ This user is not an admin!", reply_markup=bot_settings_keyboard())
        else:
            admins.remove(rem_id)
            s["admins"] = admins
            save_settings(s)
            await update.message.reply_text(
                f"✅ <b>ADMIN REMOVED!</b>\n<blockquote>🆔 ID: <code>{rem_id}</code></blockquote>",
                parse_mode="HTML", reply_markup=bot_settings_keyboard())
        context.user_data["bot_settings_mode"] = "main"
        return

    # ==================== LINKS & CHANNELS SUBMENU (everything link/channel related, one place) ====================
    if text == "LINKS & CHANNELS" and context.user_data.get("bot_settings_mode") == "main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "links_main"
        await update.message.reply_text(
            "🔗 <b>LINKS & CHANNELS</b>\n\n"
            "<blockquote>বটের যত লিংক ও জয়েন-প্রয়োজনীয় চ্যানেল আছে সব এখানে — "
            "প্রতিটা বাটনের নামেই লেখা আছে সেটা কোথায় ব্যবহার হয়।</blockquote>",
            parse_mode="HTML", reply_markup=links_config_keyboard())
        return

    if text == "VIEW ALL LINKS" and context.user_data.get("bot_settings_mode") == "links_main" and is_admin(uid):
        s = load_settings()
        channels = s.get("force_join_channels", [])
        legacy = s.get("force_join_channel")
        if legacy and legacy not in channels:
            channels = channels + [legacy]
        ch_text = "\n".join(f"  • <code>{html.escape(str(c))}</code>" for c in channels) or "  (কোনোটা যোগ করা নেই)"
        msg = (
            "🔗 <b>ALL LINKS & CHANNELS — এক নজরে</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<blockquote>📢 <b>OTP GROUP JOIN LINK</b> (বাটনের নাম: {html.escape(s.get('otp_group_btn_label','📢 OTP GROUP'))})\n"
            f"<code>{html.escape(s.get('otp_group_url','Not set'))}</code></blockquote>\n\n"
            f"<blockquote>📣 <b>MAIN CHANNEL BUTTON LINK</b> (বাটনের নাম: {html.escape(s.get('channel_btn_label','📢 MAIN CHANNEL'))})\n"
            f"<code>{html.escape(s.get('channel_url','Not set'))}</code></blockquote>\n\n"
            f"<blockquote>📱 <b>NUMBER PANEL BUTTON LINK</b> (বাটনের নাম: {html.escape(s.get('panel_btn_label','📱 NUMBER PANEL'))})\n"
            f"<code>{html.escape(s.get('panel_url','Not set'))}</code></blockquote>\n\n"
            f"<blockquote>👨‍💻 <b>DEVELOPER BUTTON LINK</b> (বাটনের নাম: {html.escape(s.get('developer_btn_label','👨‍💻 DEVELOPER BY'))})\n"
            f"<code>{html.escape(s.get('developer_link','Not set'))}</code></blockquote>\n\n"
            f"<blockquote>💬 <b>SUPPORT BUTTON LINK</b> (বাটনের নাম: {html.escape(s.get('support_btn_label','💬 SUPPORT'))})\n"
            f"<code>{html.escape(s.get('support_link','Not set'))}</code></blockquote>\n\n"
            f"<blockquote>🚪 <b>FORCE JOIN CHANNELS</b> (বট ব্যবহার করতে বাধ্যতামূলক জয়েন)\n{ch_text}</blockquote>"
        )
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=links_config_keyboard())
        return

    if text == "OTP GROUP JOIN LINK" and context.user_data.get("bot_settings_mode") == "links_main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "set_otp_link"
        s = load_settings()
        cur = s.get("otp_group_url", "Not set")
        await update.message.reply_text(
            f"🔗 <b>SET OTP GROUP LINK</b>\n\n<blockquote>Current: <code>{cur}</code></blockquote>\n\n"
            f"Send the new OTP group invite link (e.g. https://t.me/+xxxxx):",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "set_otp_link" and is_admin(uid):
        s = load_settings()
        s["otp_group_url"] = text.strip()
        save_settings(s)
        await update.message.reply_text(
            f"✅ <b>OTP GROUP LINK UPDATED!</b>\n<blockquote>{html.escape(text.strip())}</blockquote>",
            parse_mode="HTML", reply_markup=links_config_keyboard())
        context.user_data["bot_settings_mode"] = "links_main"
        return

    if text == "MAIN CHANNEL BUTTON LINK" and context.user_data.get("bot_settings_mode") == "links_main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "set_channel_link"
        s = load_settings()
        cur = s.get("channel_url", "Not set")
        await update.message.reply_text(
            f"📢 <b>SET CHANNEL LINK</b>\n\n<blockquote>Current: <code>{cur}</code></blockquote>\n\n"
            f"এটা OTP আসার সময় group-এ পাঠানো 'CHANNEL' বাটনে ব্যবহার হয়। নতুন লিংক দিন:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "set_channel_link" and is_admin(uid):
        s = load_settings()
        s["channel_url"] = text.strip()
        save_settings(s)
        await update.message.reply_text(
            f"✅ <b>CHANNEL LINK UPDATED!</b>\n<blockquote>{html.escape(text.strip())}</blockquote>",
            parse_mode="HTML", reply_markup=links_config_keyboard())
        context.user_data["bot_settings_mode"] = "links_main"
        return

    if text == "NUMBER PANEL BUTTON LINK" and context.user_data.get("bot_settings_mode") == "links_main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "set_panel_url"
        s = load_settings()
        cur = s.get("panel_url", "Not set")
        await update.message.reply_text(
            f"📱 <b>SET PANEL URL</b>\n\n<blockquote>Current: <code>{cur}</code></blockquote>\n\n"
            f"এটা OTP আসার সময় group-এ পাঠানো 'NUMBER' বাটনে ব্যবহার হয়। নতুন লিংক দিন:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "set_panel_url" and is_admin(uid):
        s = load_settings()
        s["panel_url"] = text.strip()
        save_settings(s)
        await update.message.reply_text(
            f"✅ <b>PANEL URL UPDATED!</b>\n<blockquote>{html.escape(text.strip())}</blockquote>",
            parse_mode="HTML", reply_markup=links_config_keyboard())
        context.user_data["bot_settings_mode"] = "links_main"
        return

    if text == "DEVELOPER BUTTON LINK" and context.user_data.get("bot_settings_mode") == "links_main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "set_developer_link"
        s = load_settings()
        cur = s.get("developer_link", "Not set")
        await update.message.reply_text(
            f"👨‍💻 <b>SET DEVELOPER LINK</b>\n\n<blockquote>Current: <code>{cur}</code></blockquote>\n\n"
            f"এটা SUPPORT বাটনের 'DEVELOPER BY' লিংকে ব্যবহার হয়। নতুন লিংক দিন:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "set_developer_link" and is_admin(uid):
        s = load_settings()
        s["developer_link"] = text.strip()
        save_settings(s)
        await update.message.reply_text(
            f"✅ <b>DEVELOPER LINK UPDATED!</b>\n<blockquote>{html.escape(text.strip())}</blockquote>",
            parse_mode="HTML", reply_markup=links_config_keyboard())
        context.user_data["bot_settings_mode"] = "links_main"
        return

    if text == "SUPPORT BUTTON LINK" and context.user_data.get("bot_settings_mode") == "links_main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "set_support_link"
        s = load_settings()
        cur = s.get("support_link", "Not set")
        await update.message.reply_text(
            f"💬 <b>SET SUPPORT LINK</b>\n\n<blockquote>Current: <code>{cur}</code></blockquote>\n\n"
            f"এটা SUPPORT বাটনের 'SUPPORT' লিংকে ব্যবহার হয়। নতুন লিংক দিন:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "set_support_link" and is_admin(uid):
        s = load_settings()
        s["support_link"] = text.strip()
        save_settings(s)
        await update.message.reply_text(
            f"✅ <b>SUPPORT LINK UPDATED!</b>\n<blockquote>{html.escape(text.strip())}</blockquote>",
            parse_mode="HTML", reply_markup=links_config_keyboard())
        context.user_data["bot_settings_mode"] = "links_main"
        return

    # ==================== RENAME BUTTON LABELS (link button text, not URL) ====================
    if text == "RENAME BUTTON LABELS" and context.user_data.get("bot_settings_mode") == "links_main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "rename_btn_main"
        await update.message.reply_text(
            "✏️ <b>RENAME BUTTON LABELS</b>\n\n"
            "<blockquote>শুধু বাটনের নাম/টেক্সট বদলাবে, লিংক অপরিবর্তিত থাকবে।</blockquote>",
            parse_mode="HTML", reply_markup=rename_buttons_keyboard())
        return

    _rename_targets = {
        "RENAME: OTP GROUP BTN":    ("otp_group_btn_label", "📢 OTP GROUP", "নাম্বার পাওয়ার পর দেখানো 'OTP GROUP' বাটনের"),
        "RENAME: NUMBER PANEL BTN": ("panel_btn_label", "📱 NUMBER PANEL", "OTP গ্রুপ পোস্টের 'NUMBER PANEL' বাটনের"),
        "RENAME: MAIN CHANNEL BTN": ("channel_btn_label", "📢 MAIN CHANNEL", "OTP গ্রুপ পোস্টের 'MAIN CHANNEL' বাটনের"),
        "RENAME: SUPPORT BTN":      ("support_btn_label", "💬 SUPPORT", "SUPPORT মেনুর 'SUPPORT' বাটনের"),
        "RENAME: DEVELOPER BTN":    ("developer_btn_label", "👨‍💻 DEVELOPER BY", "SUPPORT মেনুর 'DEVELOPER BY' বাটনের"),
        "RENAME: DEVELOPER NAME":   ("developer_name", DEVELOPER_NAME, "/start ওয়েলকাম মেসেজে দেখানো ডেভেলপারের"),
    }
    if text in _rename_targets and context.user_data.get("bot_settings_mode") == "rename_btn_main" and is_admin(uid):
        key, default_label, desc = _rename_targets[text]
        context.user_data["rename_btn_key"] = key
        context.user_data["bot_settings_mode"] = "rename_btn_input"
        s = load_settings()
        cur = s.get(key, default_label)
        await update.message.reply_text(
            f"✏️ <b>{html.escape(text)}</b>\n\n"
            f"<blockquote>এটা {desc} নাম।\nবর্তমান: <code>{html.escape(cur)}</code></blockquote>\n\n"
            f"নতুন নাম/ইমোজি সহ টেক্সট দিন (যেমন <code>📢 OTP GROUP</code>):",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "rename_btn_input" and is_admin(uid):
        key = context.user_data.get("rename_btn_key")
        if key:
            s = load_settings()
            s[key] = text.strip()
            save_settings(s)
        await update.message.reply_text(
            f"✅ <b>UPDATED!</b>\n<blockquote>{html.escape(text.strip())}</blockquote>",
            parse_mode="HTML", reply_markup=rename_buttons_keyboard())
        context.user_data["bot_settings_mode"] = "rename_btn_main"
        context.user_data.pop("rename_btn_key", None)
        return

    # ==================== FORCE JOIN CHANNELS (multi-channel) ====================
    if text == "FORCE JOIN CHANNELS" and context.user_data.get("bot_settings_mode") == "links_main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "fj_main"
        await update.message.reply_text(
            "📢 <b>FORCE JOIN CHANNELS</b>\n\n"
            "<blockquote>একাধিক চ্যানেল/গ্রুপ যোগ করতে পারবেন — ইউজারকে সবগুলোতে জয়েন করতে হবে। "
            "যেগুলোতে ইতিমধ্যে জয়েন করা আছে, সেগুলো আর দেখানো হবে না, শুধু বাকিগুলো দেখাবে।</blockquote>",
            parse_mode="HTML", reply_markup=force_join_keyboard())
        return

    if text == "ADD CHANNEL" and context.user_data.get("bot_settings_mode") == "fj_main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "fj_add"
        await update.message.reply_text(
            "➕ <b>ADD FORCE JOIN CHANNEL</b>\n\n"
            "চ্যানেল/গ্রুপের username (যেমন <code>@mychannel</code>) অথবা numeric chat id দিন:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "fj_add" and is_admin(uid):
        val = text.strip()
        if not val.startswith("@") and not val.lstrip("-").isdigit():
            val = "@" + val
        s = load_settings()
        channels = s.get("force_join_channels", [])
        if val in channels:
            await update.message.reply_text("⚠️ এটা আগে থেকেই লিস্টে আছে!", reply_markup=force_join_keyboard())
        else:
            channels.append(val)
            s["force_join_channels"] = channels
            save_settings(s)
            await update.message.reply_text(
                f"✅ <b>CHANNEL ADDED!</b>\n<blockquote>{html.escape(val)}</blockquote>\n\n"
                f"বট এখন থেকে সব ব্যবহারকারীকে এই চ্যানেলেও জয়েন করতে বলবে।",
                parse_mode="HTML", reply_markup=force_join_keyboard())
        context.user_data["bot_settings_mode"] = "fj_main"
        return

    if text == "REMOVE CHANNEL" and context.user_data.get("bot_settings_mode") == "fj_main" and is_admin(uid):
        s = load_settings()
        channels = s.get("force_join_channels", [])
        if not channels:
            await update.message.reply_text("📋 কোনো চ্যানেল যোগ করা নেই।", reply_markup=force_join_keyboard())
            return
        lines = "\n".join(f"{i+1}. <code>{html.escape(str(c))}</code>" for i, c in enumerate(channels))
        context.user_data["bot_settings_mode"] = "fj_remove"
        await update.message.reply_text(
            f"➖ <b>REMOVE CHANNEL</b>\n\n<blockquote>{lines}</blockquote>\n\nনম্বর দিন:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "fj_remove" and is_admin(uid):
        if not text.isdigit():
            await update.message.reply_text("❌ Send a valid number from the list.")
            return
        idx = int(text) - 1
        s = load_settings()
        channels = s.get("force_join_channels", [])
        if idx < 0 or idx >= len(channels):
            await update.message.reply_text(f"❌ Invalid number! Choose 1-{len(channels)}.")
            return
        removed = channels.pop(idx)
        s["force_join_channels"] = channels
        # also clear legacy single-channel key if it matches, so it doesn't get re-migrated back in
        if s.get("force_join_channel") == removed:
            s["force_join_channel"] = None
        save_settings(s)
        await update.message.reply_text(
            f"✅ <b>CHANNEL REMOVED!</b>\n<blockquote>{html.escape(str(removed))}</blockquote>",
            parse_mode="HTML", reply_markup=force_join_keyboard())
        context.user_data["bot_settings_mode"] = "fj_main"
        return

    if text == "LIST CHANNELS" and context.user_data.get("bot_settings_mode") == "fj_main" and is_admin(uid):
        s = load_settings()
        channels = s.get("force_join_channels", [])
        if not channels:
            await update.message.reply_text("📋 কোনো চ্যানেল যোগ করা নেই — force join বন্ধ আছে।", reply_markup=force_join_keyboard())
        else:
            lines = "\n".join(f"{i+1}. <code>{html.escape(str(c))}</code>" for i, c in enumerate(channels))
            await update.message.reply_text(
                f"📋 <b>FORCE JOIN CHANNELS ({len(channels)})</b>\n\n<blockquote>{lines}</blockquote>",
                parse_mode="HTML", reply_markup=force_join_keyboard())
        return

    # ==================== SET OTP BONUS / REFERRAL BONUS ====================
    if text == "SET OTP BONUS" and context.user_data.get("bot_settings_mode") == "main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "set_otp_bonus"
        s = load_settings()
        cur = s.get("otp_bonus", OTP_RATE)
        await update.message.reply_text(
            f"💰 <b>SET PER-OTP BONUS</b>\n\n<blockquote>প্রতিটা successful OTP-এর জন্য ইউজার কত পাবে?\n"
            f"Current: <code>{cur:.2f} BDT</code></blockquote>\n\n"
            f"নতুন পরিমাণ দিন (যেমন <code>0.25</code>):",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "set_otp_bonus" and is_admin(uid):
        try:
            amount = float(text.strip())
            if amount < 0: raise ValueError
        except ValueError:
            await update.message.reply_text("❌ একটা বৈধ সংখ্যা দিন (যেমন 0.25)।")
            return
        s = load_settings()
        s["otp_bonus"] = amount
        save_settings(s)
        await update.message.reply_text(
            f"✅ <b>PER-OTP BONUS UPDATED!</b>\n<blockquote>{amount:.2f} BDT / OTP</blockquote>",
            parse_mode="HTML", reply_markup=bot_settings_keyboard())
        context.user_data["bot_settings_mode"] = "main"
        return

    if text == "SET REFERRAL BONUS" and context.user_data.get("bot_settings_mode") == "main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "set_referral_bonus"
        s = load_settings()
        cur = s.get("referral_bonus", REFERRAL_PRICE)
        await update.message.reply_text(
            f"🎁 <b>SET REFERRAL BONUS</b>\n\n<blockquote>একজন নতুন ইউজার রেফার করলে referrer কত পাবে?\n"
            f"Current: <code>{cur:.2f} BDT</code></blockquote>\n\n"
            f"নতুন পরিমাণ দিন (যেমন <code>5</code>):",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "set_referral_bonus" and is_admin(uid):
        try:
            amount = float(text.strip())
            if amount < 0: raise ValueError
        except ValueError:
            await update.message.reply_text("❌ একটা বৈধ সংখ্যা দিন (যেমন 5)।")
            return
        s = load_settings()
        s["referral_bonus"] = amount
        save_settings(s)
        await update.message.reply_text(
            f"✅ <b>REFERRAL BONUS UPDATED!</b>\n<blockquote>{amount:.2f} BDT / referral</blockquote>",
            parse_mode="HTML", reply_markup=bot_settings_keyboard())
        context.user_data["bot_settings_mode"] = "main"
        return

    # ==================== SET OTP FORWARD CHAT ID ====================
    if text == "SET OTP CHAT ID" and context.user_data.get("bot_settings_mode") == "main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "set_otp_chat_id"
        s = load_settings()
        cur = s.get("otp_group_chat_id") or "Not set"
        await update.message.reply_text(
            f"📋 <b>SET OTP FORWARD CHAT ID</b>\n\n<blockquote>Current: <code>{cur}</code></blockquote>\n\n"
            f"Send the Chat ID where OTPs are forwarded (e.g. -1001234567890):\n"
            f"Or send <b>DISABLE</b> to clear:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "set_otp_chat_id" and is_admin(uid):
        s = load_settings()
        val = text.strip()
        if val.upper() == "DISABLE":
            s["otp_group_chat_id"] = None
            await update.message.reply_text("✅ <b>OTP Chat ID CLEARED!</b>", parse_mode="HTML", reply_markup=bot_settings_keyboard())
        elif val.lstrip("-").isdigit():
            s["otp_group_chat_id"] = int(val)
            await update.message.reply_text(
                f"✅ <b>OTP CHAT ID SET!</b>\n<blockquote><code>{val}</code></blockquote>",
                parse_mode="HTML", reply_markup=bot_settings_keyboard())
        else:
            await update.message.reply_text("❌ Invalid! Send a numeric chat ID (e.g. -1001234567890)")
            return
        save_settings(s)
        context.user_data["bot_settings_mode"] = "main"
        return

    # ==================== SET RANGE BROADCAST CHAT ID (Master Range List) ====================
    if text == "SET RANGE BROADCAST CHAT ID" and context.user_data.get("bot_settings_mode") == "main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "set_range_broadcast_chat_id"
        s = load_settings()
        cur = s.get("range_broadcast_chat_id") or "Not set (disabled)"
        await update.message.reply_text(
            f"📡 <b>SET RANGE BROADCAST CHAT ID</b>\n\n<blockquote>Current: <code>{cur}</code></blockquote>\n\n"
            f"এখানে যে chat ID দেবেন, সেই গ্রুপে বট প্রতি কয়েক মিনিট পরপর বর্তমান Master Range "
            f"List (active provider থেকে) পাঠাতে থাকবে — Zenex ওয়েবসাইটের মতোই।\n\n"
            f"Chat ID দিন (e.g. -1001234567890):\nঅথবা <b>DISABLE</b> লিখুন বন্ধ করতে:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "set_range_broadcast_chat_id" and is_admin(uid):
        s = load_settings()
        val = text.strip()
        if val.upper() == "DISABLE":
            s["range_broadcast_chat_id"] = None
            await update.message.reply_text("✅ <b>RANGE BROADCAST DISABLED!</b>", parse_mode="HTML", reply_markup=bot_settings_keyboard())
        elif val.lstrip("-").isdigit():
            s["range_broadcast_chat_id"] = int(val)
            await update.message.reply_text(
                f"✅ <b>RANGE BROADCAST CHAT ID SET!</b>\n<blockquote><code>{val}</code></blockquote>\n\n"
                f"এই গ্রুপে এখন থেকে Master Range List নিয়মিত পাঠানো হবে (provider-এ live ranges "
                f"সাপোর্ট থাকলে)।",
                parse_mode="HTML", reply_markup=bot_settings_keyboard())
        else:
            await update.message.reply_text("❌ Invalid! Send a numeric chat ID (e.g. -1001234567890)")
            return
        save_settings(s)
        context.user_data["bot_settings_mode"] = "main"
        return

    # ==================== MANAGE TOP HIT RANGES (admin-added, shown to users) ====================
    if text == "MANAGE TOP HIT RANGES" and context.user_data.get("bot_settings_mode") == "main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "thr_main"
        await update.message.reply_text(
            "📈 <b>MANAGE TOP HIT RANGES</b>\n\n"
            "<blockquote>এখানে যা যোগ করবেন, ইউজারদের TOP HIT RANGES বাটনে সেটাও দেখাবে "
            "(বটের নিজের real ব্যবহার থেকে অটো-ট্র্যাক হওয়া হিটের পাশাপাশি)।</blockquote>",
            parse_mode="HTML", reply_markup=top_hit_ranges_admin_keyboard())
        return

    if text == "ADD RANGE" and context.user_data.get("bot_settings_mode") == "thr_main" and is_admin(uid):
        context.user_data["bot_settings_mode"] = "thr_add_range"
        context.user_data["new_thr"] = {}
        await update.message.reply_text(
            "➕ <b>ADD TOP HIT RANGE</b>\n\nStep 1: Range টা দিন (যেমন <code>261387XXX</code>)",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "thr_add_range" and is_admin(uid):
        context.user_data["new_thr"]["range"] = text.strip().upper()
        context.user_data["bot_settings_mode"] = "thr_add_app"
        await update.message.reply_text(
            "📱 Step 2: Service/App নাম দিন (যেমন Facebook, WhatsApp, Telegram)",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "thr_add_app" and is_admin(uid):
        context.user_data["new_thr"]["app"] = text.strip()
        context.user_data["bot_settings_mode"] = "thr_add_hits"
        await update.message.reply_text(
            "🔥 Step 3: কত Hits দেখাতে চান? (একটা সংখ্যা দিন, যেমন 25)",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "thr_add_hits" and is_admin(uid):
        if not text.strip().isdigit():
            await update.message.reply_text("❌ একটা সংখ্যা দিন (যেমন 25)।")
            return
        thr = context.user_data.get("new_thr", {})
        thr["hits"] = int(text.strip())
        s = load_settings()
        entries = s.get("manual_top_hit_ranges", [])
        entries.append(thr)
        s["manual_top_hit_ranges"] = entries
        save_settings(s)
        await update.message.reply_text(
            f"✅ <b>ADDED!</b>\n\n<blockquote>📶 <code>{html.escape(thr.get('range',''))}</code>\n"
            f"📱 {html.escape(thr.get('app',''))}\n🔥 {thr.get('hits',0)} Hits</blockquote>\n\n"
            f"এটা এখন ইউজারদের TOP HIT RANGES লিস্টে দেখাবে।",
            parse_mode="HTML", reply_markup=top_hit_ranges_admin_keyboard())
        context.user_data["bot_settings_mode"] = "thr_main"
        context.user_data.pop("new_thr", None)
        return

    if text == "REMOVE RANGE" and context.user_data.get("bot_settings_mode") == "thr_main" and is_admin(uid):
        s = load_settings()
        entries = s.get("manual_top_hit_ranges", [])
        if not entries:
            await update.message.reply_text("📋 কোনো ম্যানুয়াল range যোগ করা নেই।", reply_markup=top_hit_ranges_admin_keyboard())
            return
        lines = "\n".join(
            f"{i+1}. <code>{html.escape(e.get('range',''))}</code> — {html.escape(e.get('app',''))} — 🔥 {e.get('hits',0)}"
            for i, e in enumerate(entries)
        )
        context.user_data["bot_settings_mode"] = "thr_remove"
        await update.message.reply_text(
            f"➖ <b>REMOVE TOP HIT RANGE</b>\n\n<blockquote>{lines}</blockquote>\n\nনম্বর দিন:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("bot_settings_mode") == "thr_remove" and is_admin(uid):
        if not text.isdigit():
            await update.message.reply_text("❌ Send a valid number from the list.")
            return
        idx = int(text) - 1
        s = load_settings()
        entries = s.get("manual_top_hit_ranges", [])
        if idx < 0 or idx >= len(entries):
            await update.message.reply_text(f"❌ Invalid number! Choose 1-{len(entries)}.")
            return
        removed = entries.pop(idx)
        s["manual_top_hit_ranges"] = entries
        save_settings(s)
        await update.message.reply_text(
            f"✅ <b>REMOVED!</b>\n<blockquote>{html.escape(removed.get('range',''))}</blockquote>",
            parse_mode="HTML", reply_markup=top_hit_ranges_admin_keyboard())
        context.user_data["bot_settings_mode"] = "thr_main"
        return

    if text == "LIST RANGES" and context.user_data.get("bot_settings_mode") == "thr_main" and is_admin(uid):
        s = load_settings()
        entries = s.get("manual_top_hit_ranges", [])
        if not entries:
            await update.message.reply_text("📋 কোনো ম্যানুয়াল range যোগ করা নেই।", reply_markup=top_hit_ranges_admin_keyboard())
        else:
            lines = "\n".join(
                f"{i+1}. <code>{html.escape(e.get('range',''))}</code> — {html.escape(e.get('app',''))} — 🔥 {e.get('hits',0)} Hits"
                for i, e in enumerate(entries)
            )
            await update.message.reply_text(
                f"📋 <b>MANUAL TOP HIT RANGES ({len(entries)})</b>\n\n<blockquote>{lines}</blockquote>",
                parse_mode="HTML", reply_markup=top_hit_ranges_admin_keyboard())
        return

    # ==================== API PROVIDER CONFIG HANDLERS (MULTI-PROVIDER) ====================

    if text == "SET ALLOWED SERVICES" and context.user_data.get("zenex_config_mode") == "main" and is_admin(uid):
        context.user_data["zenex_config_mode"] = "set_services"
        s = load_settings()
        cur = ", ".join(s.get("allowed_services", []))
        await update.message.reply_text(
            f"📋 <b>SET ALLOWED SERVICES</b>\n\n<blockquote>Current:\n<code>{cur}</code></blockquote>\n\n"
            f"Send comma-separated list:\n<i>e.g. Instagram,Facebook,WhatsApp,TikTok</i>",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "set_services" and is_admin(uid):
        services = [x.strip() for x in text.split(",") if x.strip()]
        s = load_settings()
        s["allowed_services"] = services
        save_settings(s)
        _ranges_cache["data"] = {}
        _ranges_cache["updated_at"] = 0
        svc_text = "\n".join(f"• {sv}" for sv in services)
        await update.message.reply_text(
            f"✅ <b>ALLOWED SERVICES UPDATED!</b>\n\n<blockquote>{svc_text}</blockquote>",
            parse_mode="HTML", reply_markup=provider_config_keyboard())
        context.user_data["zenex_config_mode"] = "main"
        return

    # ── ZENEX SETUP (dedicated, known-correct template) ──
    if text == "ZENEX SETUP" and context.user_data.get("zenex_config_mode") == "main" and is_admin(uid):
        context.user_data["zenex_config_mode"] = "zx_base_url"
        s = load_settings()
        cur = (s.get("providers", {}).get("zenex", {}) or {}).get("base_url", "https://api.zenexnetwork.com")
        await update.message.reply_text(
            "⚡ <b>ZENEX SETUP</b>\n\n"
            f"<blockquote>Current: <code>{html.escape(cur)}</code></blockquote>\n\n"
            "Base URL দিন (সাধারণত <code>https://api.zenexnetwork.com</code>):",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "zx_base_url" and is_admin(uid):
        context.user_data["zx_base_url"] = text.strip().rstrip("/")
        context.user_data["zenex_config_mode"] = "zx_api_key"
        await update.message.reply_text("🔑 Zenex API Key দিন:", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "zx_api_key" and is_admin(uid):
        s = load_settings()
        providers = s.get("providers", {})
        providers["zenex"] = {
            "name": "Zenex",
            "base_url": context.user_data.get("zx_base_url", "https://api.zenexnetwork.com"),
            "api_key": text.strip(),
            "auth_type": "header",
            "auth_key": "mapikey",
            "getnum_method": "POST",
            "getnum_path": "/v1/getnum",
            "getnum_range_field": "range",
            "getnum_extra_body": {"is_national": False, "remove_plus": False},
            "number_fields": ["full_number", "no_plus_number", "copy", "number"],
            "ranges_path": "/v1/active-ranges",
            "otp_path": "/v1/numsuccess/info",
        }
        s["providers"] = providers
        s["active_provider"] = "zenex"
        save_settings(s)
        _ranges_cache["data"] = {}
        _ranges_cache["updated_at"] = 0
        await update.message.reply_text(
            "✅ <b>ZENEX CONFIGURED & ACTIVATED!</b>\n\nGET NUMBER দিয়ে এখন টেস্ট করুন।",
            parse_mode="HTML", reply_markup=provider_config_keyboard())
        context.user_data["zenex_config_mode"] = "main"
        context.user_data.pop("zx_base_url", None)
        return

    # ── MIAHSMS SETUP (dedicated, known-correct template) ──
    if text == "MIAHSMS SETUP" and context.user_data.get("zenex_config_mode") == "main" and is_admin(uid):
        context.user_data["zenex_config_mode"] = "ms_base_url"
        s = load_settings()
        cur = (s.get("providers", {}).get("miahsms", {}) or {}).get("base_url", "https://miahsms.com/bot")
        await update.message.reply_text(
            "⚡ <b>MIAHSMS SETUP</b>\n\n"
            f"<blockquote>Current: <code>{html.escape(cur)}</code></blockquote>\n\n"
            "Base URL দিন (সাধারণত <code>https://miahsms.com/bot</code> — শেষে /bot সহ):",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "ms_base_url" and is_admin(uid):
        context.user_data["ms_base_url"] = text.strip().rstrip("/")
        context.user_data["zenex_config_mode"] = "ms_api_key"
        await update.message.reply_text("🔑 miahsms API Key দিন:", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "ms_api_key" and is_admin(uid):
        s = load_settings()
        providers = s.get("providers", {})
        providers["miahsms"] = {
            "name": "MiahSMS",
            "base_url": context.user_data.get("ms_base_url", "https://miahsms.com/bot"),
            "api_key": text.strip(),
            "auth_type": "header",
            "auth_key": "X-API-Key",
            "getnum_method": "POST",
            "getnum_path": "/api/getnum",
            "getnum_range_field": "range",
            "getnum_extra_body": {},
            "number_fields": ["full_number", "no_plus_number", "national_number"],
            "ranges_path": "/api/liveaccess",
            "otp_path": "/api/success-otp-info",
        }
        s["providers"] = providers
        s["active_provider"] = "miahsms"
        save_settings(s)
        _ranges_cache["data"] = {}
        _ranges_cache["updated_at"] = 0
        await update.message.reply_text(
            "✅ <b>MIAHSMS CONFIGURED & ACTIVATED!</b>\n\nGET NUMBER দিয়ে এখন টেস্ট করুন।",
            parse_mode="HTML", reply_markup=provider_config_keyboard())
        context.user_data["zenex_config_mode"] = "main"
        context.user_data.pop("ms_base_url", None)
        return

    # ── VOLTXSMS / STEXSMS / MNITNETWORK SETUP (shared 2oo9.cloud-based API) ──
    # All three sites share an identical API shape: header "mauthapi", POST
    # {base}/getnum with body {"rid": "<range without XXX>"}, GET
    # {base}/liveaccess for live ranges, GET {base}/success-otp for OTPs.
    _2oo9_sites = {
        "VOLTXSMS SETUP":    ("voltxsms", "VoltXSMS", "https://api.2oo9.cloud/MXS47FLFX0U/tnevs/@public/api"),
        "STEXSMS SETUP":     ("stexsms", "StexSMS", "https://api.2oo9.cloud/MXS47FLFX0U/tness/@public/api"),
        "MNITNETWORK SETUP": ("mnitnetwork", "MnitNetwork", "https://api.2oo9.cloud/MXS47FLFX0U/tnemn/@public/api"),
    }
    if text in _2oo9_sites and context.user_data.get("zenex_config_mode") == "main" and is_admin(uid):
        pid, pname, default_base = _2oo9_sites[text]
        context.user_data["zenex_config_mode"] = "2oo9_base_url"
        context.user_data["2oo9_pid"] = pid
        context.user_data["2oo9_pname"] = pname
        s = load_settings()
        cur = (s.get("providers", {}).get(pid, {}) or {}).get("base_url", default_base)
        await update.message.reply_text(
            f"⚡ <b>{html.escape(text)}</b>\n\n"
            f"<blockquote>Current: <code>{html.escape(cur)}</code></blockquote>\n\n"
            f"Base URL দিন (সাধারণত <code>{html.escape(default_base)}</code>):",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "2oo9_base_url" and is_admin(uid):
        context.user_data["2oo9_base_url"] = text.strip().rstrip("/")
        context.user_data["zenex_config_mode"] = "2oo9_api_key"
        await update.message.reply_text(
            f"🔑 {html.escape(context.user_data.get('2oo9_pname',''))} API Key দিন "
            f"(<code>mauthapi</code> header-এ পাঠানো হবে):",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "2oo9_api_key" and is_admin(uid):
        pid = context.user_data.get("2oo9_pid")
        pname = context.user_data.get("2oo9_pname", pid)
        base_url = context.user_data.get("2oo9_base_url", "")
        s = load_settings()
        providers = s.get("providers", {})
        providers[pid] = {
            "name": pname,
            "base_url": base_url,
            "api_key": text.strip(),
            "auth_type": "header",
            "auth_key": "mauthapi",
            "getnum_method": "POST",
            "getnum_path": "/getnum",
            "getnum_range_field": "rid",
            "getnum_extra_body": {},
            "strip_range_xxx": True,
            "number_fields": ["full_number", "no_plus_number", "national_number"],
            "ranges_path": "/liveaccess",
            "otp_path": "/success-otp",
        }
        s["providers"] = providers
        s["active_provider"] = pid
        save_settings(s)
        _ranges_cache["data"] = {}
        _ranges_cache["updated_at"] = 0
        await update.message.reply_text(
            f"✅ <b>{html.escape(pname).upper()} CONFIGURED & ACTIVATED!</b>\n\nGET NUMBER দিয়ে এখন টেস্ট করুন।",
            parse_mode="HTML", reply_markup=provider_config_keyboard())
        context.user_data["zenex_config_mode"] = "main"
        for k in ["2oo9_pid", "2oo9_pname", "2oo9_base_url"]:
            context.user_data.pop(k, None)
        return

    # ── EDIT PROVIDER — change base URL / API key of an existing provider ──
    if text == "EDIT PROVIDER" and context.user_data.get("zenex_config_mode") == "main" and is_admin(uid):
        s = load_settings()
        providers = s.get("providers", {})
        if not providers:
            await update.message.reply_text("⚠️ কোনো provider যোগ করা নেই।", reply_markup=provider_config_keyboard())
            return
        lines = "\n".join(f"{i+1}. <code>{pid}</code> — {html.escape(p.get('name', pid))}"
                           for i, (pid, p) in enumerate(providers.items()))
        context.user_data["zenex_config_mode"] = "edit_prov_pick"
        await update.message.reply_text(
            f"✏️ <b>EDIT PROVIDER</b>\n\n<blockquote>{lines}</blockquote>\n\nনম্বর দিন:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "edit_prov_pick" and is_admin(uid):
        if not text.isdigit():
            await update.message.reply_text("❌ Send a valid number from the list.")
            return
        s = load_settings()
        providers = s.get("providers", {})
        pids = list(providers.keys())
        idx = int(text) - 1
        if idx < 0 or idx >= len(pids):
            await update.message.reply_text(f"❌ Invalid number! Choose 1-{len(pids)}.")
            return
        context.user_data["edit_prov_id"] = pids[idx]
        p = providers[pids[idx]]
        context.user_data["zenex_config_mode"] = "edit_prov_base_url"
        await update.message.reply_text(
            f"✏️ <b>EDITING: {html.escape(pids[idx])}</b>\n\n"
            f"<blockquote>Current base URL: <code>{html.escape(p.get('base_url',''))}</code></blockquote>\n\n"
            f"নতুন base URL দিন, অথবা <b>SKIP</b> লিখুন অপরিবর্তিত রাখতে:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "edit_prov_base_url" and is_admin(uid):
        if text.strip().upper() != "SKIP":
            s = load_settings()
            pid = context.user_data.get("edit_prov_id")
            if pid in s.get("providers", {}):
                s["providers"][pid]["base_url"] = text.strip().rstrip("/")
                save_settings(s)
        context.user_data["zenex_config_mode"] = "edit_prov_api_key"
        await update.message.reply_text(
            "🔑 নতুন API Key দিন, অথবা <b>SKIP</b> লিখুন অপরিবর্তিত রাখতে:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "edit_prov_api_key" and is_admin(uid):
        s = load_settings()
        pid = context.user_data.get("edit_prov_id")
        if text.strip().upper() != "SKIP" and pid in s.get("providers", {}):
            s["providers"][pid]["api_key"] = text.strip()
            save_settings(s)
        _ranges_cache["data"] = {}
        _ranges_cache["updated_at"] = 0
        await update.message.reply_text(
            f"✅ <b>PROVIDER '{html.escape(str(pid))}' UPDATED!</b>\n\n"
            f"এন্ডপয়েন্ট/ফিল্ড ফরম্যাট বদলাতে চাইলে <b>ADD PROVIDER (ADVANCED)</b> দিয়ে "
            f"একই id দিয়ে আবার সম্পূর্ণ যোগ করুন (ওভাররাইট হয়ে যাবে)।",
            parse_mode="HTML", reply_markup=provider_config_keyboard())
        context.user_data["zenex_config_mode"] = "main"
        context.user_data.pop("edit_prov_id", None)
        return

    # ── ADD PROVIDER (ADVANCED): Step 1 — id/name ──
    if text == "ADD PROVIDER (ADVANCED)" and context.user_data.get("zenex_config_mode") == "main" and is_admin(uid):
        context.user_data["zenex_config_mode"] = "prov_name"
        context.user_data["new_prov"] = {}
        await update.message.reply_text(
            "➕ <b>ADD NEW API PROVIDER (ADVANCED)</b>\n\n"
            "<blockquote>Step 1: একটা ছোট নাম দিন (id হিসেবে ব্যবহার হবে)\n"
            "উদাহরণ: <code>miahsms</code></blockquote>",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "prov_name" and is_admin(uid):
        context.user_data["new_prov"]["id"] = re.sub(r'\W+', '_', text.strip().lower())
        context.user_data["new_prov"]["name"] = text.strip()
        context.user_data["zenex_config_mode"] = "prov_base_url"
        await update.message.reply_text(
            "🌐 Step 2: Base URL দিন (যেমন <code>https://miahsms.com</code>)",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "prov_base_url" and is_admin(uid):
        context.user_data["new_prov"]["base_url"] = text.strip().rstrip("/")
        context.user_data["zenex_config_mode"] = "prov_api_key"
        await update.message.reply_text("🔑 Step 3: API Key দিন", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "prov_api_key" and is_admin(uid):
        context.user_data["new_prov"]["api_key"] = text.strip()
        context.user_data["zenex_config_mode"] = "prov_auth_type"
        await update.message.reply_text(
            "🔐 Step 4: Auth কিভাবে পাঠাতে হবে?\n"
            "<code>header</code> লিখুন যদি HTTP header দিয়ে যায়\n"
            "<code>query</code> লিখুন যদি URL param দিয়ে যায় (যেমন ?api_key=xxx)",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "prov_auth_type" and is_admin(uid):
        val = text.strip().lower()
        context.user_data["new_prov"]["auth_type"] = "query" if val == "query" else "header"
        context.user_data["zenex_config_mode"] = "prov_auth_key"
        await update.message.reply_text(
            "🔖 Step 5: Auth-এর field/header নাম কী?\n"
            "উদাহরণ: <code>mapikey</code>, <code>api_key</code>, <code>token</code>",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "prov_auth_key" and is_admin(uid):
        context.user_data["new_prov"]["auth_key"] = text.strip()
        context.user_data["zenex_config_mode"] = "prov_getnum_path"
        await update.message.reply_text(
            "📞 Step 6: Number নেওয়ার endpoint path দিন\n"
            "উদাহরণ: <code>/getnum</code> বা <code>/v1/getnum</code>",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "prov_getnum_path" and is_admin(uid):
        context.user_data["new_prov"]["getnum_path"] = "/" + text.strip().lstrip("/")
        context.user_data["zenex_config_mode"] = "prov_getnum_method"
        await update.message.reply_text(
            "📡 Step 7: এই endpoint GET নাকি POST?\n"
            "<code>GET</code> অথবা <code>POST</code> লিখুন",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "prov_getnum_method" and is_admin(uid):
        context.user_data["new_prov"]["getnum_method"] = "GET" if text.strip().upper() == "GET" else "POST"
        context.user_data["zenex_config_mode"] = "prov_range_field"
        await update.message.reply_text(
            "🏷️ Step 8: range/service প্যারামিটারের নাম কী?\n"
            "উদাহরণ: <code>range</code>, <code>service</code>, <code>country</code>",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "prov_range_field" and is_admin(uid):
        context.user_data["new_prov"]["getnum_range_field"] = text.strip()
        context.user_data["new_prov"]["getnum_extra_body"] = {}
        context.user_data["zenex_config_mode"] = "prov_number_fields"
        await update.message.reply_text(
            "🔢 Step 9: response-এ number কোন field-এ আসে?\n"
            "একাধিক সম্ভাব্য নাম কমা দিয়ে দিন (order matters)\n"
            "উদাহরণ: <code>phonenumber,number,full_number</code>",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "prov_number_fields" and is_admin(uid):
        fields = [f.strip() for f in text.split(",") if f.strip()]
        context.user_data["new_prov"]["number_fields"] = fields or ["number"]
        context.user_data["zenex_config_mode"] = "prov_ranges_path"
        await update.message.reply_text(
            "📶 Step 10 (ঐচ্ছিক): Live active ranges list করার endpoint path আছে?\n"
            "থাকলে path দিন, না থাকলে <code>SKIP</code> লিখুন",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "prov_ranges_path" and is_admin(uid):
        val = text.strip()
        context.user_data["new_prov"]["ranges_path"] = "" if val.upper() == "SKIP" else "/" + val.lstrip("/")
        context.user_data["zenex_config_mode"] = "prov_otp_path"
        await update.message.reply_text(
            "📩 Step 11 (ঐচ্ছিক): OTP/SMS চেক করার endpoint path আছে?\n"
            "থাকলে path দিন, না থাকলে <code>SKIP</code> লিখুন",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "prov_otp_path" and is_admin(uid):
        val = text.strip()
        prov = context.user_data.get("new_prov", {})
        prov["otp_path"] = "" if val.upper() == "SKIP" else "/" + val.lstrip("/")

        s = load_settings()
        providers = s.get("providers", {})
        providers[prov["id"]] = {
            "name": prov.get("name", prov["id"]),
            "base_url": prov.get("base_url", ""),
            "api_key": prov.get("api_key", ""),
            "auth_type": prov.get("auth_type", "header"),
            "auth_key": prov.get("auth_key", "mapikey"),
            "getnum_method": prov.get("getnum_method", "POST"),
            "getnum_path": prov.get("getnum_path", "/getnum"),
            "getnum_range_field": prov.get("getnum_range_field", "range"),
            "getnum_extra_body": {},
            "number_fields": prov.get("number_fields", ["number"]),
            "ranges_path": prov.get("ranges_path", ""),
            "otp_path": prov.get("otp_path", ""),
        }
        s["providers"] = providers
        save_settings(s)

        await update.message.reply_text(
            f"✅ <b>PROVIDER ADDED!</b>\n\n<blockquote>🆔 <code>{prov['id']}</code>\n📛 {html.escape(prov.get('name',''))}</blockquote>\n\n"
            f"এটাকে active করতে <b>SWITCH PROVIDER</b> ব্যবহার করুন।",
            parse_mode="HTML", reply_markup=provider_config_keyboard())
        context.user_data["zenex_config_mode"] = "main"
        context.user_data.pop("new_prov", None)
        return

    # ==================== SWITCH PROVIDER ====================
    if text == "SWITCH PROVIDER" and context.user_data.get("zenex_config_mode") == "main" and is_admin(uid):
        s = load_settings()
        providers = s.get("providers", {})
        if not providers:
            await update.message.reply_text("⚠️ কোনো provider যোগ করা নেই।", reply_markup=provider_config_keyboard())
            return
        active = s.get("active_provider", "")
        lines = "\n".join(
            f"{'✅' if pid == active else '▫️'} <code>{pid}</code> — {html.escape(p.get('name', pid))}"
            for pid, p in providers.items()
        )
        context.user_data["zenex_config_mode"] = "prov_switch"
        await update.message.reply_text(
            f"🔀 <b>SWITCH PROVIDER</b>\n\n<blockquote>{lines}</blockquote>\n\nProvider id লিখুন:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "prov_switch" and is_admin(uid):
        pid = text.strip().lower()
        s = load_settings()
        providers = s.get("providers", {})
        if pid not in providers:
            await update.message.reply_text("❌ এই id-তে কোনো provider নেই।")
            return
        s["active_provider"] = pid
        save_settings(s)
        _ranges_cache["data"] = {}
        _ranges_cache["updated_at"] = 0
        await update.message.reply_text(
            f"✅ <b>ACTIVE PROVIDER SET!</b>\n<blockquote>🆔 <code>{pid}</code></blockquote>",
            parse_mode="HTML", reply_markup=provider_config_keyboard())
        context.user_data["zenex_config_mode"] = "main"
        return

    # ==================== VIEW PROVIDERS ====================
    if text == "VIEW PROVIDERS" and context.user_data.get("zenex_config_mode") == "main" and is_admin(uid):
        s = load_settings()
        providers = s.get("providers", {})
        active = s.get("active_provider", "")
        if not providers:
            await update.message.reply_text("⚠️ কোনো provider নেই।", reply_markup=provider_config_keyboard())
            return
        blocks = []
        for pid, p in providers.items():
            mark = "✅ ACTIVE" if pid == active else ""
            blocks.append(
                f"<blockquote>🆔 <code>{pid}</code> {mark}\n"
                f"📛 {html.escape(p.get('name',''))}\n"
                f"🌐 {html.escape(p.get('base_url',''))}\n"
                f"🔐 {p.get('auth_type')} / {html.escape(p.get('auth_key',''))}\n"
                f"📞 {p.get('getnum_method')} {html.escape(p.get('getnum_path',''))}</blockquote>"
            )
        await update.message.reply_text(
            "📡 <b>API PROVIDERS</b>\n\n" + "\n\n".join(blocks),
            parse_mode="HTML", reply_markup=provider_config_keyboard())
        return

    # ==================== SYNC SERVICES (pull all services from provider API) ====================
    if text == "SYNC SERVICES" and context.user_data.get("zenex_config_mode") == "main" and is_admin(uid):
        status = await update.message.reply_text(
            "🔄 <b>SYNCING...</b>\n\n<blockquote>Active provider থেকে সরাসরি API কল করে সব service আনা হচ্ছে...</blockquote>",
            parse_mode="HTML")
        found, newly_added, err = await sync_provider_services()
        if err:
            await status.edit_text(
                f"❌ <b>SYNC FAILED</b>\n\n<blockquote>{html.escape(err)}</blockquote>",
                parse_mode="HTML")
            return
        if found is None:
            await status.edit_text(
                "⚠️ Active provider live-ranges সাপোর্ট করে না (ranges path সেট করা নেই)।",
                parse_mode="HTML")
            return
        found_text = "\n".join(f"  • {html.escape(sv)}" for sv in found) or "  (কিছু পাওয়া যায়নি)"
        added_text = ("\n".join(f"  ✅ {html.escape(sv)}" for sv in newly_added)
                      if newly_added else "  (নতুন কিছু নেই — সব আগে থেকেই যোগ করা আছে)")
        await status.edit_text(
            f"✅ <b>SYNC COMPLETE!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<blockquote>📋 <b>প্যানেলে পাওয়া সব সার্ভিস ({len(found)}):</b>\n{found_text}</blockquote>\n\n"
            f"<blockquote>🆕 <b>নতুন যোগ হলো ({len(newly_added)}):</b>\n{added_text}</blockquote>\n\n"
            f"GET NUMBER চেপে এখনই দেখতে পাবেন।",
            parse_mode="HTML")
        return

    # ==================== MULTI-PANEL MODE (run several providers together) ====================
    if text == "BACK TO API PROVIDER CONFIG" and is_admin(uid):
        context.user_data["zenex_config_mode"] = "main"
        await update.message.reply_text("📡 <b>API PROVIDER CONFIG</b>", parse_mode="HTML", reply_markup=provider_config_keyboard())
        return

    if text == "MULTI-PANEL MODE" and context.user_data.get("zenex_config_mode") == "main" and is_admin(uid):
        context.user_data["zenex_config_mode"] = "mp_main"
        s = load_settings()
        enabled = s.get("enabled_providers", [])
        note = (f"বর্তমানে {len(enabled)}টা প্যানেল একসাথে চলছে (round-robin, একটা ব্যর্থ হলে পরেরটা চেষ্টা করবে)।"
                if len(enabled) >= 2 else
                "বর্তমানে multi-panel mode বন্ধ — শুধু SWITCH PROVIDER দিয়ে বেছে নেওয়া single provider চলছে।")
        await update.message.reply_text(
            "🔀 <b>MULTI-PANEL MODE</b>\n\n"
            f"<blockquote>{note}</blockquote>\n\n"
            "<blockquote>২টা বা তার বেশি panel ENABLE করলে বট প্রতিবার পালাক্রমে বিভিন্ন প্যানেল থেকে "
            "নাম্বার দেবে, আর একটা প্যানেল কাজ না করলে অটোমেটিক পরেরটা চেষ্টা করবে। OTP-ও সব "
            "enabled প্যানেল থেকে চেক হবে। একটাই ENABLE থাকলে (বা কোনোটাই না থাকলে) আগের মতো "
            "single-panel mode-এ চলবে।</blockquote>",
            parse_mode="HTML", reply_markup=multi_panel_keyboard())
        return

    if text == "ENABLE PANEL" and context.user_data.get("zenex_config_mode") == "mp_main" and is_admin(uid):
        s = load_settings()
        providers = s.get("providers", {})
        enabled = set(s.get("enabled_providers", []))
        if not providers:
            await update.message.reply_text("⚠️ কোনো provider যোগ করা নেই।", reply_markup=multi_panel_keyboard())
            return
        lines = "\n".join(
            f"{'✅' if pid in enabled else '▫️'} <code>{pid}</code> — {html.escape(p.get('name', pid))}"
            for pid, p in providers.items()
        )
        context.user_data["zenex_config_mode"] = "mp_enable"
        await update.message.reply_text(
            f"➕ <b>ENABLE PANEL</b>\n\n<blockquote>{lines}</blockquote>\n\nযে provider এর id ENABLE করতে চান সেটা লিখুন:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "mp_enable" and is_admin(uid):
        pid = text.strip().lower()
        s = load_settings()
        providers = s.get("providers", {})
        if pid not in providers:
            await update.message.reply_text("❌ এই id-তে কোনো provider নেই।")
            return
        enabled = s.get("enabled_providers", [])
        if pid in enabled:
            await update.message.reply_text("⚠️ এটা আগে থেকেই enabled আছে।", reply_markup=multi_panel_keyboard())
        else:
            enabled.append(pid)
            s["enabled_providers"] = enabled
            save_settings(s)
            _ranges_cache["data"] = {}
            _ranges_cache["updated_at"] = 0
            mode_txt = "মাল্টি-প্যানেল মোড এখন সক্রিয় (২+ প্যানেল)!" if len(enabled) >= 2 else "আরেকটা প্যানেল ENABLE করলে multi-panel mode শুরু হবে।"
            await update.message.reply_text(
                f"✅ <b>ENABLED!</b>\n<blockquote>🆔 <code>{pid}</code></blockquote>\n\n{mode_txt}",
                parse_mode="HTML", reply_markup=multi_panel_keyboard())
        context.user_data["zenex_config_mode"] = "mp_main"
        return

    if text == "DISABLE PANEL" and context.user_data.get("zenex_config_mode") == "mp_main" and is_admin(uid):
        s = load_settings()
        enabled = s.get("enabled_providers", [])
        if not enabled:
            await update.message.reply_text("📋 কোনো প্যানেল enabled নেই (single-panel mode-এ আছেন)।", reply_markup=multi_panel_keyboard())
            return
        lines = "\n".join(f"{i+1}. <code>{pid}</code>" for i, pid in enumerate(enabled))
        context.user_data["zenex_config_mode"] = "mp_disable"
        await update.message.reply_text(
            f"➖ <b>DISABLE PANEL</b>\n\n<blockquote>{lines}</blockquote>\n\nনম্বর দিন:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("zenex_config_mode") == "mp_disable" and is_admin(uid):
        if not text.isdigit():
            await update.message.reply_text("❌ Send a valid number from the list.")
            return
        idx = int(text) - 1
        s = load_settings()
        enabled = s.get("enabled_providers", [])
        if idx < 0 or idx >= len(enabled):
            await update.message.reply_text(f"❌ Invalid number! Choose 1-{len(enabled)}.")
            return
        removed = enabled.pop(idx)
        s["enabled_providers"] = enabled
        save_settings(s)
        _ranges_cache["data"] = {}
        _ranges_cache["updated_at"] = 0
        await update.message.reply_text(
            f"✅ <b>DISABLED!</b>\n<blockquote>🆔 <code>{removed}</code></blockquote>",
            parse_mode="HTML", reply_markup=multi_panel_keyboard())
        context.user_data["zenex_config_mode"] = "mp_main"
        return

    if text == "LIST ENABLED PANELS" and context.user_data.get("zenex_config_mode") == "mp_main" and is_admin(uid):
        s = load_settings()
        enabled = s.get("enabled_providers", [])
        providers = s.get("providers", {})
        if not enabled:
            await update.message.reply_text(
                f"📋 কোনো panel enabled নেই — single-panel mode, active provider: <code>{html.escape(s.get('active_provider',''))}</code>",
                parse_mode="HTML", reply_markup=multi_panel_keyboard())
        else:
            lines = "\n".join(
                f"{i+1}. <code>{pid}</code> — {html.escape(providers.get(pid, {}).get('name', pid))}"
                for i, pid in enumerate(enabled)
            )
            mode_txt = "🔀 MULTI-PANEL MODE ACTIVE" if len(enabled) >= 2 else "single-panel (শুধু এটাই ব্যবহার হচ্ছে)"
            await update.message.reply_text(
                f"📋 <b>ENABLED PANELS ({len(enabled)})</b> — {mode_txt}\n\n<blockquote>{lines}</blockquote>",
                parse_mode="HTML", reply_markup=multi_panel_keyboard())
        return

    if text == "PANEL USAGE STATS" and context.user_data.get("zenex_config_mode") == "mp_main" and is_admin(uid):
        s = load_settings()
        providers = s.get("providers", {})
        pstats = load_provider_stats()
        if not pstats:
            await update.message.reply_text(
                "📊 এখনো কোনো ডেটা নেই — number issue হলে/OTP এলে এখানে দেখা যাবে।",
                reply_markup=multi_panel_keyboard())
            return
        # sort by total numbers issued, most-used panel first
        ordered = sorted(pstats.items(), key=lambda x: x[1].get("numbers_issued", 0), reverse=True)
        today = datetime.now().strftime("%Y-%m-%d")
        blocks = []
        total_numbers = total_otps = 0
        for pid, st in ordered:
            name = providers.get(pid, {}).get("name", pid)
            today_n = st.get("today_numbers", 0) if st.get("today_date") == today else 0
            today_o = st.get("today_otps", 0) if st.get("today_date") == today else 0
            all_n = st.get("numbers_issued", 0)
            all_o = st.get("otps_received", 0)
            total_numbers += all_n
            total_otps += all_o
            last_used = st.get("last_used", "")
            if last_used:
                try:
                    last_used = datetime.fromisoformat(last_used).strftime("%d/%m %I:%M %p")
                except Exception:
                    pass
            blocks.append(
                f"<blockquote>📡 <b>{html.escape(str(name))}</b> (<code>{html.escape(pid)}</code>)\n"
                f"✨ Today: 📱 {today_n} | 🔑 {today_o}\n"
                f"🌐 All time: 📱 {all_n} | 🔑 {all_o}\n"
                f"🕒 Last used: {html.escape(str(last_used) or 'N/A')}</blockquote>"
            )
        top_pid = ordered[0][0] if ordered else None
        top_name = providers.get(top_pid, {}).get("name", top_pid) if top_pid else "N/A"
        msg = (
            f"📊 <b>PANEL USAGE STATS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<blockquote>🏆 সবচেয়ে বেশি ব্যবহার হচ্ছে: <b>{html.escape(str(top_name))}</b>\n"
            f"🌐 সর্বমোট: 📱 {total_numbers} নাম্বার | 🔑 {total_otps} OTP</blockquote>\n\n"
            + "\n\n".join(blocks)
        )
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=multi_panel_keyboard())
        return

    # ==================== SERVICE MANAGEMENT HANDLERS ====================
    if text == "LIST SERVICES" and context.user_data.get("service_mgmt_mode") == "main" and is_admin(uid):
        s = load_settings()
        manual = s.get("manual_services", [])
        if not manual:
            await update.message.reply_text("📋 No manual services added yet.", reply_markup=service_management_keyboard())
        else:
            lines = []
            for i, svc in enumerate(manual):
                countries = svc.get("countries", [])
                clines = "\n".join(
                    f"   {c.get('flag','🌍')} {c.get('name','?')} — <code>{', '.join(c.get('ranges',[]))}</code>"
                    for c in countries
                )
                lines.append(f"{i+1}. <b>{html.escape(svc['name'])}</b>\n{clines}")
            msg = f"📋 <b>MANUAL SERVICES ({len(manual)})</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(lines)
            await update.message.reply_text(msg, parse_mode="HTML", reply_markup=service_management_keyboard())
        return

    # ── ADD SERVICE: Step 1 — Service Name ──
    if text == "ADD SERVICE" and context.user_data.get("service_mgmt_mode") == "main" and is_admin(uid):
        context.user_data["service_mgmt_mode"] = "add_name"
        await update.message.reply_text(
            "➕ <b>ADD MANUAL SERVICE</b>\n\n"
            "<blockquote>Step 1: Send the <b>Service Name</b>\n"
            "e.g. FACEBOOK, WHATSAPP, INSTAGRAM</blockquote>",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("service_mgmt_mode") == "add_name" and is_admin(uid):
        context.user_data["new_svc_name"] = text.strip().upper()
        context.user_data["new_svc_countries"] = []
        context.user_data["service_mgmt_mode"] = "add_country"
        await update.message.reply_text(
            f"➕ <b>{html.escape(context.user_data['new_svc_name'])}</b>\n\n"
            f"<blockquote>Step 2: Send the <b>Country Name</b>\n"
            f"e.g. Bangladesh, Nigeria, India</blockquote>\n\n"
            f"Send country name ↓",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    # ── ADD SERVICE: DONE — save service (must be checked BEFORE add_country handler) ──
    if text == "DONE" and context.user_data.get("service_mgmt_mode") == "add_country" and is_admin(uid):
        svc_name = context.user_data.get("new_svc_name")
        countries_done = context.user_data.get("new_svc_countries", [])
        if not svc_name or not countries_done:
            await update.message.reply_text("❌ No data to save. Start again.", reply_markup=service_management_keyboard())
            context.user_data["service_mgmt_mode"] = "main"
            return
        s = load_settings()
        manual = s.get("manual_services", [])
        manual.append({"name": svc_name, "countries": countries_done})
        s["manual_services"] = manual
        save_settings(s)
        done_lines = "\n".join(f"  {c['flag']} {c['name']} — <code>{', '.join(c['ranges'])}</code>" for c in countries_done)
        await update.message.reply_text(
            f"✅ <b>SERVICE SAVED!</b>\n\n"
            f"<blockquote>📱 <b>{html.escape(svc_name)}</b>\n{done_lines}</blockquote>",
            parse_mode="HTML", reply_markup=service_management_keyboard())
        context.user_data["service_mgmt_mode"] = "main"
        for k in ["new_svc_name", "new_svc_countries", "new_svc_cur_country"]:
            context.user_data.pop(k, None)
        return

    # ── ADD SERVICE: Step 2 — Country Name ──
    if context.user_data.get("service_mgmt_mode") == "add_country" and is_admin(uid):
        country_name = text.strip().title()
        context.user_data["new_svc_cur_country"] = country_name
        context.user_data["service_mgmt_mode"] = "add_range"
        svc_name = context.user_data.get("new_svc_name", "?")
        countries_done = context.user_data.get("new_svc_countries", [])
        done_text = ""
        if countries_done:
            done_text = "\n\n<blockquote>✅ Already added:\n" + "\n".join(
                f"  {c['flag']} {c['name']}" for c in countries_done) + "</blockquote>"
        await update.message.reply_text(
            f"➕ <b>{html.escape(svc_name)}</b> — 🌍 <b>{html.escape(country_name)}</b>{done_text}\n\n"
            f"<blockquote>Step 3: Send the <b>Range</b> for {html.escape(country_name)}\n\n"
            f"Format: Country Code + XXX\n"
            f"🇧🇩 Bangladesh → <code>880XXX</code>\n"
            f"🇳🇬 Nigeria → <code>234XXX</code>\n"
            f"🇮🇳 India → <code>91XXXXX</code>\n"
            f"🇲🇾 Malaysia → <code>60XXXXX</code>\n"
            f"🇮🇩 Indonesia → <code>62XXXXX</code>\n\n"
            f"Multiple ranges: <code>880XXX, 8801XXX</code></blockquote>",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    # ── ADD SERVICE: Step 3 — Ranges for that country ──
    if context.user_data.get("service_mgmt_mode") == "add_range" and is_admin(uid):
        raw_ranges = [r.strip().upper() for r in text.split(",") if r.strip()]
        country_name = context.user_data.get("new_svc_cur_country", "Unknown")
        # Detect flag from first range
        flag, detected = "🌍", country_name
        if raw_ranges:
            import re as _re
            prefix = _re.sub(r'[xX]+$', '', raw_ranges[0]).strip()
            pfx_d  = _re.sub(r'\D', '', prefix)
            flag, detected = get_country_info(pfx_d)
        countries_done = context.user_data.get("new_svc_countries", [])
        countries_done.append({"name": country_name, "flag": flag, "ranges": raw_ranges})
        context.user_data["new_svc_countries"] = countries_done
        context.user_data["service_mgmt_mode"] = "add_country"
        context.user_data.pop("new_svc_cur_country", None)
        ranges_text = ", ".join(raw_ranges)
        done_lines = "\n".join(f"  {c['flag']} {c['name']} — <code>{', '.join(c['ranges'])}</code>" for c in countries_done)
        await update.message.reply_text(
            f"✅ <b>{flag} {html.escape(country_name)}</b> added!\n"
            f"<blockquote>Ranges: <code>{html.escape(ranges_text)}</code></blockquote>\n\n"
            f"<blockquote>Countries so far:\n{done_lines}</blockquote>\n\n"
            f"Send another <b>Country Name</b> to add more,\n"
            f"or send <b>DONE</b> to save the service.",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if text == "REMOVE SERVICE" and context.user_data.get("service_mgmt_mode") == "main" and is_admin(uid):
        s = load_settings()
        manual = s.get("manual_services", [])
        if not manual:
            await update.message.reply_text("📋 No services to remove.", reply_markup=service_management_keyboard())
            return
        lines = "\n".join(f"{i+1}. <b>{html.escape(svc['name'])}</b>" for i, svc in enumerate(manual))
        context.user_data["service_mgmt_mode"] = "remove"
        await update.message.reply_text(
            f"➖ <b>REMOVE SERVICE</b>\n\n<blockquote>{lines}</blockquote>\n\n"
            f"Send the <b>number</b> of the service to remove:",
            parse_mode="HTML", reply_markup=cancel_keyboard())
        return

    if context.user_data.get("service_mgmt_mode") == "remove" and is_admin(uid):
        if not text.isdigit():
            await update.message.reply_text("❌ Send a valid number from the list.")
            return
        idx = int(text) - 1
        s = load_settings()
        manual = s.get("manual_services", [])
        if idx < 0 or idx >= len(manual):
            await update.message.reply_text(f"❌ Invalid number! Choose 1-{len(manual)}.")
            return
        removed = manual.pop(idx)
        s["manual_services"] = manual
        save_settings(s)
        await update.message.reply_text(
            f"✅ <b>SERVICE REMOVED!</b>\n<blockquote>📱 {html.escape(removed['name'])}</blockquote>",
            parse_mode="HTML", reply_markup=service_management_keyboard())
        context.user_data["service_mgmt_mode"] = "main"
        return

    await update.message.reply_text("🔹 PLEASE USE THE BUTTONS BELOW:", reply_markup=main_keyboard(uid))

# ==================== COMMAND HANDLERS SECTION ====================

async def get1number_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_banned(uid):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return
    await show_app_selection(update, context)

async def searchotp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_banned(uid):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return
    context.user_data["mode"] = "search_otp"
    await update.message.reply_text("🔍 **ENTER THE NUMBER TO SEARCH OTP:**", parse_mode="Markdown")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_banned(uid):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return
    balance = get_user(uid)['balance']
    await update.message.reply_text(f"💰 BALANCE: `{format_balance(balance)} BDT`", parse_mode="Markdown", reply_markup=main_keyboard(uid))

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_banned(uid):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return
    user_data = get_user(uid)
    stats = get_user_stats(uid)
    user = update.effective_user
    profile_text = (
        f"👤 **YOUR PROFILE**\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏷️ NAME: `{user.full_name}`\n"
        f"🆔 USERNAME: @{user.username or 'No username'}\n"
        f"🗝️ ID: `{uid}`\n\n"
        f"💵 BALANCE: {format_balance(user_data.get('balance', 0))} BDT\n\n"
        f"✨ TODAY: 📱 {stats['today_numbers']} | 🔑 {stats['today_otps']}\n"
        f"🔥 7 DAYS: 📱 {stats['last7d_numbers']} | 🔑 {stats['last7d_otps']}\n"
        f"🌐 ALL TIME: 📱 {stats['total_numbers']} | 🔑 {stats['total_otps']}"
    )
    await update.message.reply_text(profile_text, parse_mode="Markdown")

async def refer_command_slash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_banned(uid):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return
    await refer_command(update, context)

async def leaderboard_command_slash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_banned(uid):
        await update.message.reply_text("🚫 YOU ARE BANNED 🚫", reply_markup=main_keyboard(uid))
        return
    await leaderboard_command(update, context)

# ==================== START & CALLBACK SECTION ====================

async def get_not_joined_channels(uid, context):
    """Returns the list of configured force-join channels the user has NOT
    joined yet (admins always return an empty list)."""
    if is_admin(uid):
        return []
    s = load_settings()
    channels = list(s.get("force_join_channels") or [])
    legacy = s.get("force_join_channel")
    if legacy and legacy not in channels:
        channels.append(legacy)
    if not channels:
        return []
    not_joined = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(channel, uid)
            if member.status in ("member", "administrator", "creator", "restricted"):
                continue
        except Exception:
            pass
        not_joined.append(channel)
    return not_joined

async def _build_force_join_buttons(context, not_joined):
    """Builds JOIN buttons only for channels not yet joined, plus a CHECK
    button at the end to re-verify without retyping any command."""
    buttons = []
    for channel in not_joined:
        channel_clean = str(channel).lstrip("@")
        try:
            chat = await context.bot.get_chat(channel)
            invite_url = chat.invite_link or f"https://t.me/{channel_clean}"
            title = chat.title or channel_clean
        except Exception:
            invite_url = f"https://t.me/{channel_clean}"
            title = channel_clean
        buttons.append([InlineKeyboardButton(f"✅ JOIN {title}", url=invite_url,
                                              api_kwargs={"icon_custom_emoji_id": "5875017993909440887", "style": "success"})])
    buttons.append([InlineKeyboardButton("🔄 CHECK", callback_data="fj_check",
                                          api_kwargs={"icon_custom_emoji_id": "5231200819986047254", "style": "primary"})])
    return buttons

async def send_force_join_prompt(update, context, not_joined):
    """Shows JOIN buttons only for the channels the user hasn't joined yet,
    plus a CHECK button to re-verify in place."""
    buttons = await _build_force_join_buttons(context, not_joined)
    text = (
        "⚠️ <b>FORCE JOIN REQUIRED!</b>\n\n"
        "<blockquote>বট ব্যবহার করতে নিচের চ্যানেল/গ্রুপ(গুলো)-এ জয়েন করুন, "
        "তারপর নিচের 🔄 CHECK বাটনে চাপুন।</blockquote>"
    )
    markup = InlineKeyboardMarkup(buttons)
    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    elif update.callback_query:
        try:
            await update.callback_query.answer("আগে required channel(s) এ জয়েন করুন।", show_alert=True)
        except Exception:
            pass
        try:
            await context.bot.send_message(update.callback_query.from_user.id, text,
                                            parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass

async def check_force_join(update, context):
    """Returns True if user is allowed to proceed. False if they need to join first.
    Works for both a text message Update and a callback-query Update."""
    uid = update.effective_user.id
    not_joined = await get_not_joined_channels(uid, context)
    if not not_joined:
        return True
    await send_force_join_prompt(update, context, not_joined)
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uid_str = str(uid)

    existing_data = load_data(USER_DATA_FILE)
    is_new_user = uid_str not in existing_data
    if is_new_user:
        get_user(uid)
    update_user_profile(uid, update.effective_user.full_name, update.effective_user.username)

    args = context.args
    if args:
        param = args[0]
        if is_range_request(param):
            await request_queue.put({'type': 'auto_number', 'update': update, 'context': context, 'range_text': param})
            return
        elif is_referral_request(param) and is_new_user:
            try:
                referrer_id = int(param)
                if referrer_id != uid and str(referrer_id) in existing_data:
                    current_count = get_referral_count(referrer_id)
                    new_count = current_count + 1
                    update_referral_count(referrer_id, new_count)
                    referral_bonus = load_settings().get("referral_bonus", REFERRAL_PRICE)
                    await update_db_balance(referrer_id, referral_bonus)
                    log_global_activity(referrer_id, "REFERRAL_JOINED", {"referred_user": uid})
                    try:
                        await context.bot.send_message(
                            referrer_id,
                            f"🎉 <b>NEW REFERRAL!</b>\n\n<blockquote>🗝️ ID: <code>{uid}</code>\n💰 REWARD: {format_balance(referral_bonus)} BDT\n👥 TOTAL REFERS: {new_count}</blockquote>",
                            parse_mode="HTML"
                        )
                    except:
                        pass
            except Exception as e:
                print(f"Referral error: {e}")

    if not await check_force_join(update, context):
        return

    context.user_data.clear()
    _dev_cfg = load_settings()
    _dev_url = _dev_cfg.get("developer_link") or DEVELOPER_LINK
    _dev_name = _dev_cfg.get("developer_name") or DEVELOPER_NAME
    await update.message.reply_text(
        WELCOME_MESSAGE + f"\n\n👨‍💻 Developer: [{_dev_name}]({_dev_url})",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )
    await update.message.reply_text("🔹 PLEASE USE THE BUTTONS BELOW:", reply_markup=main_keyboard(uid))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    data = query.data
    await query.answer()

    if not is_admin(uid) and is_user_banned(uid):
        await query.edit_message_text("🚫 YOU ARE BANNED 🚫")
        return

    # FORCE JOIN — re-check button: verify membership in place, without
    # going through the generic gate below (which would just re-show the
    # same prompt). Only channels still not joined stay on screen.
    if data == "fj_check":
        not_joined = await get_not_joined_channels(uid, context)
        if not not_joined:
            try:
                await query.message.edit_text(
                    "✅ <b>ধন্যবাদ! আপনি সবগুলো চ্যানেলে জয়েন করেছেন।</b>\n\n"
                    "এখন বট ব্যবহার করতে পারবেন — নিচের বাটন থেকে শুরু করুন।",
                    parse_mode="HTML")
            except Exception:
                pass
            try:
                await context.bot.send_message(uid, "🔹 PLEASE USE THE BUTTONS BELOW:", reply_markup=main_keyboard(uid))
            except Exception:
                pass
        else:
            buttons = await _build_force_join_buttons(context, not_joined)
            try:
                await query.message.edit_text(
                    "⚠️ <b>এখনো কিছু চ্যানেলে জয়েন করা হয়নি!</b>\n\n"
                    "<blockquote>নিচের চ্যানেল(গুলো)-এ জয়েন করে আবার 🔄 CHECK চাপুন।</blockquote>",
                    parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
            except Exception:
                pass
        return

    # Force-join gate for inline button flows (country/range selection etc.)
    # too — not just text commands — and re-checked live every time.
    if not is_admin(uid) and data not in ("withdraw_cancel",):
        if not await check_force_join(update, context):
            return

    # PROVIDER APPS — APP SELECTION
    if data.startswith("sel_app_"):
        app_name = data[8:]
        top = context.user_data.get("top_ranges_by_app") or (_ranges_cache.get("data") or {})
        if not top or app_name not in top:
            await query.answer("App not found. Please try again.", show_alert=True); return
        ranges = top[app_name].get("ranges", [])
        if not ranges:
            await query.answer("No ranges available for this app right now.", show_alert=True); return
        context.user_data["sel_app"] = app_name
        context.user_data["sel_ranges"] = ranges
        btns, seen = [], {}
        clrs = ["primary","success","danger"]
        ci = 0
        for i, r in enumerate(ranges[:24]):
            prefix = re.sub(r'[xX]+$', '', str(r)).strip()
            pfx_d = re.sub(r'\D', '', prefix)
            flag, cname = get_country_info(pfx_d)
            label = f"{flag} {cname}"
            if label not in seen:
                seen[label] = i
                btns.append(InlineKeyboardButton(label, callback_data=f"sel_rng_{i}",
                    api_kwargs={"style": clrs[ci % 3]}))
                ci += 1
        rows = [btns[j:j+2] for j in range(0, len(btns), 2)]
        rows.append([InlineKeyboardButton("\u25c0\ufe0f BACK", callback_data="back_apps",
                                          api_kwargs={"style": "danger"})])
        await query.message.edit_text(
            f'{get_tg_emoji("get_number_btn")} <b>SELECT COUNTRY</b>\n'
            f'\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            f'<blockquote>\U0001f4f1 App: <b>{html.escape(app_name)}</b></blockquote>\n'
            f'<blockquote>\U0001f30d Select your Country:</blockquote>',
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        return

    # PROVIDER APPS — RANGE SELECTION
    if data.startswith("sel_rng_"):
        idx = int(data[8:])
        ranges = context.user_data.get("sel_ranges", [])
        app_name = context.user_data.get("sel_app", "")
        if idx >= len(ranges):
            await query.answer("Range not found. Please try again.", show_alert=True); return
        asyncio.create_task(fast_allocate_number(query, context, ranges[idx], app_name))
        return

    # CUSTOM RANGE
    if data == "custom_range":
        context.user_data["mode"] = "custom_range"
        await query.message.edit_text(
            "\u2699\ufe0f <b>CUSTOM RANGE</b>\n\n"
            "<blockquote>\U0001f4f6 Type your custom range.\n"
            "Example: <code>234XXX</code> or <code>225XXX</code></blockquote>\n\n"
            "<blockquote>\u2328\ufe0f Type range below and Send:</blockquote>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25c0\ufe0f BACK", callback_data="back_apps",
                                      api_kwargs={"style": "danger"})
            ]]))
        return

    # MANUAL SERVICE — Step 1: show countries
    if data.startswith("manual_svc_"):
        idx = int(data[len("manual_svc_"):])
        s = load_settings()
        manual = s.get("manual_services", [])
        if idx >= len(manual):
            await query.answer("Service not found.", show_alert=True); return
        svc = manual[idx]
        countries = svc.get("countries", [])
        if not countries:
            await query.answer("No countries added for this service yet.", show_alert=True); return

        clrs = ["primary", "success", "danger"]
        btns = []
        for ci, c in enumerate(countries):
            flag = c.get("flag", "🌍")
            cname = c.get("name", "Unknown")
            btns.append(InlineKeyboardButton(
                f"{flag} {cname}",
                callback_data=f"manual_cnt_{idx}_{ci}",
                api_kwargs={"style": clrs[ci % 3]}
            ))
        rows = [btns[j:j+2] for j in range(0, len(btns), 2)]
        rows.append([InlineKeyboardButton(
            "\u25c0\ufe0f BACK", callback_data="back_apps",
            api_kwargs={"style": "danger"}
        )])
        await query.message.edit_text(
            f'\U0001f527 <b>{html.escape(svc["name"])}</b>\n'
            f'\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n'
            f'<blockquote>\U0001f4f1 App: <b>{html.escape(svc["name"])}</b></blockquote>\n'
            f'<blockquote>\U0001f30d Select your Country:</blockquote>',
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    # MANUAL SERVICE — Step 2: country selected → directly allocate number
    if data.startswith("manual_cnt_"):
        parts = data.split("_")
        svc_idx = int(parts[2])
        cnt_idx = int(parts[3])
        s = load_settings()
        manual = s.get("manual_services", [])
        if svc_idx >= len(manual):
            await query.answer("Service not found.", show_alert=True); return
        svc = manual[svc_idx]
        countries = svc.get("countries", [])
        if cnt_idx >= len(countries):
            await query.answer("Country not found.", show_alert=True); return
        country = countries[cnt_idx]
        ranges = country.get("ranges", [])
        if not ranges:
            await query.answer("No ranges for this country.", show_alert=True); return
        import random
        range_text = random.choice(ranges).strip().upper()
        svc_name = svc["name"]
        asyncio.create_task(fast_allocate_number(query, context, range_text, svc_name))
        return

    # MANUAL SERVICE — Step 3: range selected → allocate number
    if data.startswith("manual_rng_"):
        parts = data.split("_")
        svc_idx = int(parts[2])
        cnt_idx = int(parts[3])
        rng_idx = int(parts[4])
        s = load_settings()
        manual = s.get("manual_services", [])
        if svc_idx >= len(manual):
            await query.answer("Service not found.", show_alert=True); return
        countries = manual[svc_idx].get("countries", [])
        if cnt_idx >= len(countries):
            await query.answer("Country not found.", show_alert=True); return
        ranges = countries[cnt_idx].get("ranges", [])
        if rng_idx >= len(ranges):
            await query.answer("Range not found.", show_alert=True); return
        range_text = ranges[rng_idx].strip().upper()
        svc_name   = manual[svc_idx]["name"]
        asyncio.create_task(fast_allocate_number(query, context, range_text, svc_name))
        return
    # BACK TO APP LIST
    if data in ("back_apps", "back_services"):
        top = context.user_data.get("top_ranges_by_app") or (_ranges_cache.get("data") or {})
        manual_rows = build_manual_service_buttons()
        zenex_rows = build_app_buttons_from_cache(top) if top else []
        all_rows = zenex_rows + manual_rows
        all_rows.append([InlineKeyboardButton("\u2699\ufe0f CUSTOM RANGE", callback_data="custom_range",
                                               api_kwargs={"style": "danger"})])
        msg = f'{get_tg_emoji("get_number_btn")} <b>SELECT APP TO GET NUMBER</b>\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501'
        await query.message.edit_text(msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(all_rows))
        return

    if data == "same_range":
        r_text = last_range.get(uid)
        if r_text:
            otp_url = load_settings().get("otp_group_url") or "https://t.me/+31eV11IT7WQzMjI9"
            try:
                await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📢 OTP GROUP", url=otp_url, api_kwargs={"style": "primary"})
                ]]))
            except:
                pass
            await process_numbers(update, context, r_text, 1)
        return

    # WITHDRAW
    if data == "withdraw_start":
        balance = get_user(uid)['balance']
        if balance < MIN_WITHDRAW:
            await query.message.reply_text(
                f"<blockquote>💵 BALANCE: {format_balance(balance)} BDT\n📉 MIN WITHDRAW: {MIN_WITHDRAW} BDT</blockquote>",
                parse_mode="HTML"
            )
            return
        context.user_data["withdraw_mode"] = "select_method"
        await query.message.reply_text("💳 SELECT YOUR PAYMENT METHOD!", reply_markup=withdraw_method_keyboard())
        return

    if data == "withdraw_confirm":
        await process_withdraw_confirm(update, context)
        return

    if data == "withdraw_cancel":
        await process_withdraw_cancel(update, context)
        return

    if data.startswith("admin_approve_"):
        await admin_approve_withdraw(update, context, data.replace("admin_approve_", ""))
        return

    if data.startswith("admin_reject_"):
        await admin_reject_withdraw(update, context, data.replace("admin_reject_", ""))
        return

    # COPY / MISC CALLBACKS
    if data.startswith("copy_id_"):
        await query.answer(f"✅ Copied ID: {data.replace('copy_id_', '')}", show_alert=True)
        return

    if data.startswith("copy_text_"):
        await query.answer(f"✅ Copied: {data.replace('copy_text_', '')}", show_alert=True)
        return

    if data.startswith("my_ref_"):
        target_uid = data.replace("my_ref_", "")
        all_logs = load_data(ACTIVITY_LOGS_FILE)
        my_referrals = [log for log in all_logs if str(log.get('uid')) == str(target_uid) and log.get('action') == "REFERRAL_JOINED"]
        content = f"👥 REFERRAL REPORT — {target_uid}\n━━━━━━━━━━━━\nTOTAL: {len(my_referrals)}\n\n"
        for i, log in enumerate(my_referrals, 1):
            try:
                dt_obj = datetime.fromisoformat(log['timestamp'])
                ref_id = log.get('details', {}).get('referred_user', 'N/A')
                content += f"{i}. ID: {ref_id} | {dt_obj.strftime('%d/%m/%Y %I:%M %p')}\n"
            except:
                continue
        f = io.BytesIO(content.encode())
        f.name = f"REF_{target_uid}.txt"
        await context.bot.send_document(chat_id=uid, document=f, caption="✅ **REFERRAL DATA**", parse_mode="Markdown")
        return

    if data.startswith("admin_ban_") and is_admin(uid):
        target_uid_str = data.replace("admin_ban_", "")
        target_uid_int = int(target_uid_str)
        if is_user_banned(target_uid_int):
            await query.answer("ইতিমধ্যে ব্যান করা আছে।", show_alert=True)
        else:
            ban_user(target_uid_int)
            try:
                await context.bot.send_message(target_uid_int, "🚫 **YOU HAVE BEEN BANNED**\n📞 Contact support.", parse_mode="Markdown")
            except Exception:
                pass
            try:
                await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📂 CHECK ALL DATA", callback_data=f"full_logs_{target_uid_str}", api_kwargs={"style": "primary"})],
                    [InlineKeyboardButton("✅ UNBAN", callback_data=f"admin_unban_{target_uid_str}", api_kwargs={"style": "success"})]
                ]))
            except Exception:
                pass
            await query.answer(f"✅ {target_uid_str} ব্যান করা হয়েছে।", show_alert=True)
        return

    if data.startswith("admin_unban_") and is_admin(uid):
        target_uid_str = data.replace("admin_unban_", "")
        target_uid_int = int(target_uid_str)
        if not is_user_banned(target_uid_int):
            await query.answer("ব্যান করা নেই।", show_alert=True)
        else:
            unban_user(target_uid_int)
            try:
                await context.bot.send_message(target_uid_int, "✅ **YOU HAVE BEEN UNBANNED!** Use /start", parse_mode="Markdown")
            except Exception:
                pass
            try:
                await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📂 CHECK ALL DATA", callback_data=f"full_logs_{target_uid_str}", api_kwargs={"style": "primary"})],
                    [InlineKeyboardButton("🚫 BAN", callback_data=f"admin_ban_{target_uid_str}", api_kwargs={"style": "danger"})]
                ]))
            except Exception:
                pass
            await query.answer(f"✅ {target_uid_str} আনব্যান করা হয়েছে।", show_alert=True)
        return

    if data.startswith("full_logs_"):
        target_uid = data.replace("full_logs_", "")
        stats = get_user_stats(target_uid)
        all_logs = load_data(ACTIVITY_LOGS_FILE)
        user_db = load_data(USER_DATA_FILE)
        user_info = user_db.get(str(target_uid), {})
        user_otps = [log for log in all_logs if str(log.get('uid')) == str(target_uid) and log.get('action') == "OTP_RECEIVED"]
        content = (
            f"📊 USER DATA REPORT — {target_uid}\n"
            f"💰 BALANCE: {user_info.get('balance', 0):.2f} BDT\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"TODAY NUMBERS: {stats['today_numbers']}\n"
            f"TODAY OTPS: {stats['today_otps']}\n"
            f"7D NUMBERS: {stats['last7d_numbers']}\n"
            f"7D OTPS: {stats['last7d_otps']}\n"
            f"TOTAL NUMBERS: {stats['total_numbers']}\n"
            f"TOTAL OTPS: {stats['total_otps']}\n"
            f"━━━━━━━━━━━━━━━━━━\n\nOTP LOGS:\n"
        )
        for i, log in enumerate(user_otps, 1):
            try:
                dt_obj = datetime.fromisoformat(log['timestamp'])
                d = log.get('details', {})
                content += f"{i}. {dt_obj.strftime('%d/%m/%Y %I:%M %p')}\n   📞 {d.get('number', 'N/A')}\n   🔑 {d.get('otp', 'N/A')}\n\n"
            except:
                continue
        f = io.BytesIO(content.encode())
        f.name = f"USER_{target_uid}.txt"
        await context.bot.send_document(
            chat_id=uid, document=f,
            caption=f"✅ <b>DATA FOR USER: <code>{target_uid}</code></b>",
            parse_mode="HTML"
        )
        return

# ==================== MAIN & POST INIT SECTION ====================

async def post_init(application):
    for _ in range(20):
        asyncio.create_task(worker())
    asyncio.create_task(monitor_loop(application))
    asyncio.create_task(_bg_refresh_ranges())
    asyncio.create_task(range_broadcast_loop(application))

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN environment variable is not set. Set it (e.g. in BOTIFYHOST's "
              "environment settings, a .env file, or systemd EnvironmentFile) and restart. "
              "The bot token is never hardcoded in this file for security.")
        return
    # Give Telegram API requests more time and retry startup after temporary network failures.
    telegram_request = HTTPXRequest(
        connection_pool_size=20,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=10.0,
    )
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(telegram_request)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("get1number", get1number_command))
    app.add_handler(CommandHandler("searchotp", searchotp_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("refer", refer_command_slash))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command_slash))

    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    # Render Web Service needs an open port: tiny health-check server (only when PORT is set)
    _port = os.environ.get("PORT", "").strip()
    if _port.isdigit():
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Bot is running")
            def do_HEAD(self):
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass

        threading.Thread(
            target=lambda: HTTPServer(("0.0.0.0", int(_port)), _HealthHandler).serve_forever(),
            daemon=True
        ).start()

    print("🚀 BOT RUNNING...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, bootstrap_retries=-1)

if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
