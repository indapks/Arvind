from flask import Flask, request, Response, jsonify
from curl_cffi import requests as cffi_requests
import json
import logging
import os
import base64
import re
import time
from urllib.parse import parse_qsl, urlencode

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

APP_VERSION = "3.0"
START_TS = time.time()
REQ_COUNT = 0
STATS = {'path_fix': 0, 'signed': 0, 'session_rewrite': 0, 'retry_1016': 0}

cffi_session = cffi_requests.Session(impersonate="chrome120")
cffi_session.headers.update({
    'User-Agent': 'okhttp/4.12.0',
    'Accept': 'application/json'
})

# 🔧 Env-var overrides (defaults = production values — deploy pe koi change nahi chahiye)
TARGET_BASE_DOMAIN = os.environ.get('SHE_TARGET', 'prod.api.shem.apisaranyu.in')
TARGET_SCHEME = os.environ.get('SHE_SCHEME', 'https')
TOKEN_FILE = os.environ.get('SHE_TOKEN_FILE', '/www/wwwroot/she.inddarama.in/shemaroo_vip_data.json')

ADMIN_PHONE = "9315441351"
ADMIN_PHONE_FULL = "919315441351"

XOR_KEY = "1nDD@r@m@_C4stl3_S3cr3t_K3y_9876543210_!@#$%^&*()_X0R_Pr0t3ct10n_M0d_9988_El1t3_M0d5_Z3r0_D4y"

# ============================================================
# 🔐 XOR Encryption (original mod-layer — APK is designed for this)
# ============================================================
def xor_encrypt(data_str, key):
    data_bytes = data_str.encode('utf-8')
    key_bytes = key.encode('utf-8')
    xored_bytes = bytes([data_bytes[i] ^ key_bytes[i % len(key_bytes)] for i in range(len(data_bytes))])
    return base64.b64encode(xored_bytes).decode('utf-8')

def xor_decrypt(b64_str, key):
    try:
        xored_bytes = base64.b64decode(b64_str)
        key_bytes = key.encode('utf-8')
        data_bytes = bytes([xored_bytes[i] ^ key_bytes[i % len(key_bytes)] for i in range(len(xored_bytes))])
        return data_bytes.decode('utf-8')
    except: return ""

def aes_decrypt(b64_str):
    try:
        return xor_decrypt(b64_str, XOR_KEY)
    except: return ""

def is_text_content(content_type):
    ct = (content_type or '').lower()
    return 'text' in ct or 'json' in ct or 'xml' in ct or 'form' in ct

def protect_value(val):
    if not val: return val
    return "XOR:" + xor_encrypt(val, XOR_KEY)

def unprotect_value(val):
    if not val or not isinstance(val, str): return val
    if val.startswith('XOR:'):
        return xor_decrypt(val[4:], XOR_KEY)
    if val.startswith('AES:'):
        try:
            return aes_decrypt(val[4:])
        except:
            return ""
    return val

PLAINTEXTISH_RE = re.compile(r'^[A-Za-z0-9_.@:+\-]{1,128}$')

def decrypt_path_segments(path):
    """🛠️ FIX (v3 — ASLI "Grown Ups nahi chal rahe" ROOT CAUSE):
    Purana greedy regex (XOR:([A-Za-z0-9+/=]+)) '/' ko bhi base64 char maan kar
    aage wale path segments KHA jaata tha (live logs se PROVEN):
      /users/XOR:{s}/account                    → /users/{s}           (account kha gaya!)
      /users/XOR:{s}/user_plans                 → /users/{s}_plans     (user kha gaya!)
      /users/XOR:{s}/playlists/watchhistory/... → /users/{s}.gzip      (sab kha gaya!)
    Upstream 404 deta tha → fake-200 masking → user_plans empty → premium
    (Grown Ups) content ka entitlement check fail → play nahi hota tha.
    Ab: SEGMENT-WISE decrypt — har '/'-separated segment alag, kabhi cross-eat nahi."""
    segs = path.split('/')
    changed = False
    for i, seg in enumerate(segs):
        if seg.startswith('XOR:') or seg.startswith('AES:'):
            dec = unprotect_value(seg)
            if dec and PLAINTEXTISH_RE.match(dec) and dec != seg:
                segs[i] = dec
                changed = True
    if changed:
        new_path = '/'.join(segs)
        STATS['path_fix'] += 1
        return new_path
    return path

SESSION_HEX_RE = re.compile(r'^[0-9a-f]{32}$')

def rewrite_path_session(path, fresh_session, old_saved_session=''):
    """🛡️ ANTI-LOGOUT (v3): APK path me STALE session para ho (purane login ka) →
    use FRESH captured session se badal do. Upstream path-session hi asli auth hai
    (live-proven), isliye ye rewrite = silent re-login — APK ko pata bhi nahi chalta.
    Sirf /users/... ya /v2/users/... ke 32-hex session segment ko touch karta hai —
    catalogs/items ids kabhi nahi."""
    if not fresh_session:
        return path, None
    segs = path.split('/')
    for i in range(1, len(segs)):
        seg = segs[i]
        if segs[i-1] != 'users':
            continue
        is_hex_session = bool(SESSION_HEX_RE.match(seg))
        is_old_session = bool(old_saved_session) and seg == str(old_saved_session)
        if (is_hex_session or is_old_session) and seg != str(fresh_session):
            old_val = seg
            segs[i] = str(fresh_session)
            STATS['session_rewrite'] += 1
            return '/'.join(segs), old_val
    return path, None

def _protect_json_value_walk(obj, sensitive_map, _depth=0):
    """v4: JSON-aware response protection. Walks tree, replaces string values
    that EXACTLY match admin's sensitive values. Prevents substring corruption
    of episode IDs / durations / view counts that happen to contain admin's
    user_id/mobile_number as a substring."""
    if _depth > 10:
        return obj, 0
    changed = 0
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and v in sensitive_map:
                out[k] = sensitive_map[v]
                changed += 1
            elif isinstance(v, (dict, list)):
                nv, ch = _protect_json_value_walk(v, sensitive_map, _depth + 1)
                out[k] = nv
                changed += ch
            else:
                out[k] = v
        return out, changed
    if isinstance(obj, list):
        out = []
        for item in obj:
            if isinstance(item, str) and item in sensitive_map:
                out.append(sensitive_map[item])
                changed += 1
            elif isinstance(item, (dict, list)):
                nv, ch = _protect_json_value_walk(item, sensitive_map, _depth + 1)
                out.append(nv)
                changed += ch
            else:
                out.append(item)
        return out, changed
    return obj, changed

def protect_response_content(content_str, saved):
    """v4 FIX: JSON-aware response protection.

    PROBLEM (v3): Blind content_str.replace(val, protect_value(val)) corrupted
    episode_list / catalog item responses when admin's user_id (e.g.
    "188726715626") or mobile_number ("9315441351") appeared as a SUBSTRING
    inside other numeric values (episode IDs, durations, view counts, cast IDs,
    image URLs, etc.). For SOME shows only -> APK couldn't parse the corrupted
    JSON -> "Sorry, currently there are no videos" error on those shows.

    FIX (v4): Walk JSON tree and replace only EXACT string-value matches.
    Falls back to word-boundary regex for non-JSON text (HTML error pages,
    plain text, etc.) so adjacent alphanumeric characters are never eaten.

    Note: This mirrors the v3 JSON-aware approach already used in
    unprotect_body() for requests - the response side was missed."""
    keys_to_protect = ['auth_token', 'session', 'user_id', 'mobile_number']
    sensitive_map = {}
    for k in keys_to_protect:
        val = saved.get(k, '')
        # only protect non-trivial-length values to avoid false positives
        if val and isinstance(val, str) and len(str(val)) >= 6:
            sensitive_map[str(val)] = protect_value(str(val))

    if not sensitive_map:
        return content_str

    # 1) JSON-aware protection (safest - only EXACT value matches)
    try:
        obj = json.loads(content_str)
        new_obj, changed_count = _protect_json_value_walk(obj, sensitive_map)
        if changed_count > 0:
            logger.info(f"🔒 [PROTECT-JSON] {changed_count} value(s) replaced (exact-match only)")
            return json.dumps(new_obj)
        return content_str  # parsed fine, nothing sensitive to protect
    except (ValueError, TypeError):
        pass

    # 2) Fallback: word-boundary regex for non-JSON text
    # Only matches when surrounded by non-alphanumeric chars (or string edges),
    # so substrings inside longer IDs / URLs / numbers are NEVER touched.
    try:
        result = content_str
        total_replaced = 0
        for orig, protected in sensitive_map.items():
            if orig not in result:
                continue
            pattern = re.escape(orig)
            new_result, n = re.subn(
                r'(?<![A-Za-z0-9_])' + pattern + r'(?![A-Za-z0-9_])',
                lambda m, p=protected: p,
                result
            )
            if n > 0:
                result = new_result
                total_replaced += n
        if total_replaced > 0:
            logger.info(f"🔒 [PROTECT-REGEX] {total_replaced} value(s) replaced (word-boundary fallback)")
        return result
    except Exception:
        return content_str

def decrypt_json_values(obj, _depth=0):
    """🛠️ FIX (v3): body ke andar XOR:/AES: values — JSON-aware decrypt.
    Purana regex body me bhi adjacent text kha sakta tha (XOR:ab=cd → 'cd' kha jaata).
    Ab exact VALUE-level decrypt — koi structural damage nahi."""
    changed = False
    if _depth > 6: return obj, changed
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            nv, ch = decrypt_json_values(v, _depth + 1)
            out[k] = nv; changed = changed or ch
        return out, changed
    if isinstance(obj, list):
        out = []
        for v in obj:
            nv, ch = decrypt_json_values(v, _depth + 1)
            out.append(nv); changed = changed or ch
        return out, changed
    if isinstance(obj, str) and (obj.startswith('XOR:') or obj.startswith('AES:')):
        dec = unprotect_value(obj)
        if dec and PLAINTEXTISH_RE.match(dec) and dec != obj:
            return dec, True
    return obj, changed

def unprotect_body(body_bytes, content_type):
    """Body decrypt — JSON-aware → form-aware → safe-regex fallback (isme bhi
    padding ke baad ka text ab KABHI eat nahi hota)."""
    if not body_bytes: return body_bytes, False
    ct = (content_type or '').lower()
    # JSON (ya CT-less jo JSON parse ho) — exact VALUE-level decrypt
    if 'form' not in ct:
        try:
            obj = json.loads(body_bytes.decode('utf-8'))
            new_obj, ch = decrypt_json_values(obj)
            if ch:
                return json.dumps(new_obj).encode('utf-8'), True
            return body_bytes, False
        except Exception:
            pass
    # form-encoded — value-level decrypt
    if 'form' in ct:
        try:
            pairs = parse_qsl(body_bytes.decode('utf-8'), keep_blank_values=True)
            if pairs:
                out, ch = [], False
                for k, v in pairs:
                    if v.startswith('XOR:') or v.startswith('AES:'):
                        dec = unprotect_value(v)
                        if dec and PLAINTEXTISH_RE.match(dec) and dec != v:
                            v = dec; ch = True
                    out.append((k, v))
                if ch:
                    return urlencode(out).encode('utf-8'), True
        except Exception:
            pass
    # safe fallback — b64 data, padding sirf END me (no over-eat)
    try:
        s = body_bytes.decode('utf-8')
        def _repl(m):
            dec = unprotect_value(m.group(0))
            return dec if (dec and PLAINTEXTISH_RE.match(dec) and dec != m.group(0)) else m.group(0)
        new_s = re.sub(r'(?:XOR|AES):[A-Za-z0-9+/]+={0,2}', _repl, s)
        if new_s != s:
            return new_s.encode('utf-8'), True
    except Exception:
        pass
    return body_bytes, False

def get_saved_data():
    global ADMIN_PHONE, ADMIN_PHONE_FULL
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'r') as f:
                data = json.loads(f.read().strip())
                if data.get('admin_phone'): ADMIN_PHONE = data['admin_phone']
                if data.get('admin_phone_full'): ADMIN_PHONE_FULL = data['admin_phone_full']
                return data
        except: return {}
    return {}

def save_data(new_data):
    existing = get_saved_data()
    got_session = False
    for k, v in new_data.items():
        if v:
            existing[k] = v
            if k in ('session', 'auth_token', 'user_id'):
                got_session = True
    if got_session:
        existing['captured_at'] = time.time()
    try:
        with open(TOKEN_FILE, 'w') as f:
            f.write(json.dumps(existing))
        if got_session:
            logger.info(f"👑 [CAPTURED] session={str(existing.get('session',''))[:12]}... auth={str(existing.get('auth_token',''))[:12]}... uid={existing.get('user_id','')} mob={existing.get('admin_phone_full','')}")
        else:
            logger.info(f"📱 [PHONE] {existing.get('admin_phone_full','')} saved — session pending, login hone do")
    except Exception as e:
        logger.error(f"save_data error: {e}")

def delete_data():
    global ADMIN_PHONE, ADMIN_PHONE_FULL
    saved = get_saved_data()
    phone_keep = saved.get('admin_phone', ADMIN_PHONE)
    phone_full_keep = saved.get('admin_phone_full', ADMIN_PHONE_FULL)
    if os.path.exists(TOKEN_FILE):
        try:
            os.remove(TOKEN_FILE)
            logger.info("👑 Tokens cleared! (phone kept safe)")
        except: pass
    save_data({'admin_phone': phone_keep, 'admin_phone_full': phone_full_keep})
    ADMIN_PHONE = phone_keep
    ADMIN_PHONE_FULL = phone_full_keep

def make_json_resp(data_dict, status=200):
    content_str = json.dumps(data_dict)
    resp = Response(content_str.encode('utf-8'), status=status)
    resp.headers['Content-Type'] = 'application/json'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

def read_request_body():
    data = request.get_data(cache=True)
    if data:
        return data
    try:
        if request.form:
            pairs = []
            for k in request.form.keys():
                for v in request.form.getlist(k):
                    pairs.append((k, v))
            if pairs:
                return urlencode(pairs).encode('utf-8')
    except Exception:
        pass
    return b''

def fake_ok_response(saved, extra=None):
    admin_session = saved.get('session', '')
    admin_user_id = saved.get('user_id', '')
    fake_data = {"status": True, "message": "Success", "is_subscribed": True}
    if admin_session: fake_data["session"] = admin_session
    if admin_user_id: fake_data["user_id"] = admin_user_id
    if extra: fake_data.update(extra)
    fake = {"data": fake_data}
    fake_str = protect_response_content(json.dumps(fake), saved)
    resp = Response(fake_str.encode('utf-8'), status=200)
    resp.headers['Content-Type'] = 'application/json'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

def fake_vip_verify_details(saved):
    """verify_details fallback — real call fail ho tab (logout prevention)."""
    admin_user_id = saved.get('user_id', '')
    logger.info("🛡️ [INTERCEPT] verify_details → 200 fake VIP (prevent logout)")
    return make_json_resp({"data": {
        "id": admin_user_id or "188726715626",
        "mobile": "ULTIMATEAURHAWSIKABAAP",
        "country_code": "+91",
        "status": "active",
        "is_subscribed": True,
        "subscription_end_date": "2099-12-31",
        "subscription_type": "SVOD",
        "plan_id": "vip_plan",
        "ext_mobile_number_key": True
    }})

@app.route('/admin-access', methods=['GET'])
def admin_panel():
    global ADMIN_PHONE, ADMIN_PHONE_FULL
    action = request.args.get('action', 'status')

    if action == 'clear':
        delete_data()
        return jsonify({"success": True, "message": "Admin tokens cleared. Phone number kept safe. Ab login karke fresh session capture hoga."})

    elif action == 'set_phone':
        new_phone = request.args.get('phone', '').replace(" ", "").replace("+", "")
        if not new_phone:
            return jsonify({"error": "Phone parameter required. Example: &phone=919315441351"}), 400

        if new_phone.startswith('91') and len(new_phone) > 10:
            ADMIN_PHONE_FULL = new_phone
            ADMIN_PHONE = new_phone[-10:]
        else:
            ADMIN_PHONE = new_phone
            ADMIN_PHONE_FULL = "91" + new_phone

        save_data({'admin_phone': ADMIN_PHONE, 'admin_phone_full': ADMIN_PHONE_FULL})
        return jsonify({"success": True, "message": f"Admin phone set to {ADMIN_PHONE_FULL}. Login to capture new session."})

    saved = get_saved_data()
    has_vip = bool(saved.get('session'))

    return jsonify({
        "status": "online",
        "app_version": APP_VERSION,
        "has_vip": has_vip,
        "login_pending": not has_vip,
        "admin_phone": saved.get('admin_phone_full', ADMIN_PHONE_FULL),
        "session": saved.get('session', ''),
        "user_id": saved.get('user_id', ''),
        "auth_token": saved.get('auth_token', ''),
        "captured_at": saved.get('captured_at', None)
    })

@app.route('/proxy-status')
def proxy_status():
    saved = get_saved_data()
    return jsonify({
        "app": "she-proxy", "version": APP_VERSION, "status": "online",
        "uptime_sec": int(time.time() - START_TS),
        "requests_served": REQ_COUNT,
        "has_vip": bool(saved.get('session')),
        "login_pending": not bool(saved.get('session')),
        "admin_phone": saved.get('admin_phone_full', ADMIN_PHONE_FULL),
        "last_capture_at": saved.get('captured_at', None),
        "target": TARGET_BASE_DOMAIN,
        "stats": STATS
    })

# ============================================================
# 🛡️ BLOCKLIST — real Shemaroo API endpoints ke hisaab se
# ============================================================
LOGOUT_PATTERNS = [
    'logout', 'loginout', 'signout', 'sign_out', 'log_out', 'logoff', 'log_off',
    'delete_session', 'destroy_session', 'clear_session', 'remove_session',
    'expire_session', 'invalidate', 'revoke', 'session_logout', 'logout_session',
    'end_session', 'kill_session', 'generate_session_tv', 'tv_login',
    'deleteaccount', 'cancelaccount', 'account/delete', 'account_delete',
    'delete_account', 'delete_user', 'cancel_account', 'deactivate',
    'remove_account', 'unsubscribe', 'cancel_subscription',
    'remove_device', 'unlink_device', 'unregister_device', 'delete_device',
    'fcm_token', 'unregister',
]

PROFILE_PATTERNS = [
    'update/name', 'update_name', 'name_update', 'edit_name', 'change_name',
    'update/profile', 'update_profile', 'profile_update', 'edit/profile',
    'edit_profile', 'update_user', 'user_update', 'edit_user', 'modify_user',
    'update_user_profile', 'update_user_detail', 'update_user_details',
    'update_user_information', 'modify_profile', 'save_profile', 'set_name',
    'update/mobile', 'update_mobile', 'mobile_update', 'change_mobile',
    'edit_mobile', 'change_number', 'update_email', 'change_email',
    'delete_profile', 'remove_profile',
    'updateprofile', 'updateuserprofile', 'editprofile', 'editusername',
    'changename', 'updatename', 'nameupdate', 'profileupdate', 'updateuser',
    'userupdate', 'saveprofile', 'modifyprofile', 'updatemobile', 'changemobile',
    'editmobile', 'mobilupdate', 'updateemail', 'changephonenumber',
]

NAME_KEYS = ('name', 'user_name', 'username', 'first_name', 'last_name',
             'full_name', 'display_name', 'profile_name', 'nick_name', 'nickname')
NAME_KEYS_SET = set(NAME_KEYS)
MOBILE_KEYS = ('mobile', 'mobile_number', 'phone', 'phone_number',
               'email', 'email_id', 'user_email')
MOBILE_KEYS_SET = set(MOBILE_KEYS)

CONTENT_KEYS = ('content_id', 'category_id', 'page', 'limit', 'offset', 'episode',
                'season', 'item_id', 'video_id', 'content', 'search', 'query', 'q',
                'playlist_id', 'list_id', 'collection_id', 'genre', 'cast', 'artist')

NAME_PARENT_WHITELIST = {'playlist', 'playlists', 'content', 'contents', 'item', 'items',
                         'movie', 'movies', 'video', 'videos', 'episode', 'episodes',
                         'season', 'seasons', 'cast', 'artist', 'artists', 'song', 'songs',
                         'track', 'tracks', 'category', 'categories', 'search', 'query',
                         'filters', 'banner', 'banners', 'channel', 'channels', 'show',
                         'shows', 'data', 'list', 'language', 'genre', 'trailer', 'image',
                         'images', 'thumb', 'thumbnail'}

LOGIN_PATH_MARKERS = ('otp_verification', 'resend_otp', 'send_otp', 'otp_send', 'generate_otp', 'request_otp')
VERIFY_RESPONSE_MARKERS = ('verification', 'verify_otp', 'otp_verification')

def is_login_path(path_lower):
    return any(x in path_lower for x in LOGIN_PATH_MARKERS)

def is_verify_response_path(path_lower):
    return any(x in path_lower for x in VERIFY_RESPONSE_MARKERS)

def extract_body_params(body_bytes, content_type):
    if not body_bytes: return {}
    try:
        ct = (content_type or '').lower()
        if 'json' in ct:
            obj = json.loads(body_bytes.decode('utf-8'))
            if isinstance(obj, dict): return obj
        elif 'form' in ct:
            return dict(parse_qsl(body_bytes.decode('utf-8'), keep_blank_values=True))
        else:
            obj = json.loads(body_bytes.decode('utf-8'))
            if isinstance(obj, dict): return obj
    except: pass
    return {}

def find_name_keys(obj, _depth=0):
    if _depth > 4: return []
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if isinstance(v, (dict, list)):
                if lk in NAME_PARENT_WHITELIST:
                    continue
                hits.extend(find_name_keys(v, _depth + 1))
            elif lk in NAME_KEYS_SET and v not in (None, '', [], {}):
                hits.append(str(k))
    elif isinstance(obj, list):
        for item in obj:
            hits.extend(find_name_keys(item, _depth + 1))
    return hits

def find_mobile_keys(obj, _depth=0):
    if _depth > 4: return []
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if isinstance(v, (dict, list)):
                if lk in NAME_PARENT_WHITELIST:
                    continue
                hits.extend(find_mobile_keys(v, _depth + 1))
            elif lk in MOBILE_KEYS_SET and v not in (None, '', [], {}):
                hits.append(str(k))
    elif isinstance(obj, list):
        for item in obj:
            hits.extend(find_mobile_keys(item, _depth + 1))
    return hits

def strip_name_fields(body_bytes, content_type):
    if not body_bytes: return body_bytes
    try:
        obj = json.loads(body_bytes.decode('utf-8'))
        if isinstance(obj, dict):
            changed = False
            for k in list(obj.keys()):
                if str(k).lower() in NAME_KEYS_SET and obj[k] not in (None, '', []):
                    obj.pop(k); changed = True
            for k, v in obj.items():
                if isinstance(v, dict) and str(k).lower() not in NAME_PARENT_WHITELIST:
                    for kk in list(v.keys()):
                        if str(kk).lower() in NAME_KEYS_SET and v[kk] not in (None, '', []):
                            v.pop(kk); changed = True
            if changed:
                return json.dumps(obj).encode('utf-8')
    except: pass
    return body_bytes

def inject_credentials(args, body_bytes, content_type, saved, is_login, method):
    """Unsigned requests pe hi chalta hai (v3: signed requests exact-forward hote hain)."""
    admin_session = saved.get('session', '')
    admin_user_id = saved.get('user_id', '')
    admin_auth = saved.get('auth_token', '')
    injected = []

    if admin_auth:
        if 'auth_token' not in args:
            args['auth_token'] = admin_auth; injected.append('+auth')
        elif str(args['auth_token']) != str(admin_auth):
            args['auth_token'] = admin_auth; injected.append('auth')

    if not is_login:
        for k, fresh in (('session', admin_session), ('user_id', admin_user_id)):
            if not fresh: continue
            if k in args:
                if str(args[k]) != str(fresh):
                    args[k] = fresh; injected.append(k)
            elif method == 'GET':
                args[k] = fresh; injected.append('+' + k)

    if body_bytes and not is_login:
        try:
            ct = (content_type or '').lower()
            if 'json' in ct or ct == '':
                try:
                    obj = json.loads(body_bytes.decode('utf-8'))
                    if isinstance(obj, dict):
                        changed = False
                        if admin_session and 'session' in obj and str(obj['session']) != str(admin_session):
                            obj['session'] = admin_session; changed = True
                        if admin_user_id and 'user_id' in obj and str(obj['user_id']) != str(admin_user_id):
                            obj['user_id'] = admin_user_id; changed = True
                        if admin_auth and 'auth_token' in obj and str(obj['auth_token']) != str(admin_auth):
                            obj['auth_token'] = admin_auth; changed = True
                        if changed:
                            body_bytes = json.dumps(obj).encode('utf-8')
                            injected.append('body')
                except: pass
            elif 'form' in ct:
                pairs = parse_qsl(body_bytes.decode('utf-8'), keep_blank_values=True)
                out = []
                changed = False
                for k, v in pairs:
                    if k == 'session' and admin_session and v != admin_session:
                        v = admin_session; changed = True
                    elif k == 'user_id' and admin_user_id and v != str(admin_user_id):
                        v = str(admin_user_id); changed = True
                    elif k == 'auth_token' and admin_auth and v != admin_auth:
                        v = admin_auth; changed = True
                    out.append((k, v))
                if changed:
                    body_bytes = urlencode(out).encode('utf-8')
                    injected.append('body')
        except: pass

    return args, body_bytes, injected

def normalize_outgoing_body(body_bytes, content_type, method):
    """Unsigned POSTs ke liye Rails CT-json guarantee (v2 fix, live-proven).
    Signed requests isse PASS hote hain (exact forward)."""
    if method not in ('POST', 'PUT', 'PATCH'):
        return body_bytes, content_type
    if body_bytes is None or body_bytes == b'':
        return b'{}', 'application/json'
    ct_l = (content_type or '').lower()
    if 'json' in ct_l:
        return body_bytes, 'application/json'
    try:
        json.loads(body_bytes.decode('utf-8'))
        return body_bytes, 'application/json'
    except Exception:
        pass
    try:
        pairs = parse_qsl(body_bytes.decode('utf-8'), keep_blank_values=True)
        if pairs:
            return json.dumps(dict(pairs)).encode('utf-8'), 'application/json'
    except Exception:
        pass
    return body_bytes, 'application/json'

@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def proxy(path):
    global REQ_COUNT
    REQ_COUNT += 1
    original_args = request.args.to_dict()
    original_data = read_request_body()
    req_content_type = request.headers.get('Content-Type', '')

    # query args me XOR:/AES: values decrypt karo (exact value-level)
    args = original_args.copy()
    for k, v in args.items():
        if isinstance(v, str) and (v.startswith('AES:') or v.startswith('XOR:')):
            dec = unprotect_value(v)
            if dec and dec != v:
                args[k] = dec
                logger.info(f"🔓 Query arg decrypted: {k}")

    # body decrypt — JSON-aware (v3: koi regex-eating nahi)
    decrypted_data = original_data
    if original_data and is_text_content(req_content_type):
        new_data, ch = unprotect_body(original_data, req_content_type)
        if new_data != original_data:
            decrypted_data = new_data
            logger.info(f"🔓 Request Body Unprotected for /{path}")

    # 🛠️ v3 CORE FIX: path me XOR:/AES: segments — SEGMENT-WISE decrypt (suffix safe)
    path = decrypt_path_segments(path)

    # path decryption ke BAAD path_lower recompute
    path_lower = path.lower()

    saved = get_saved_data()
    admin_session = saved.get('session', '')
    admin_user_id = saved.get('user_id', '')
    admin_mobile = saved.get('mobile_number', ADMIN_PHONE_FULL)

    # 📸 snapshot — capture/phone-check ke liye (injection se PEHLE)
    decrypted_args_snapshot = dict(args)
    try:
        decrypted_body_snapshot = decrypted_data.decode('utf-8', errors='ignore') if decrypted_data else ''
    except:
        decrypted_body_snapshot = ''
    original_body_str = decrypted_body_snapshot

    is_login = is_login_path(path_lower)
    is_verify_resp = is_verify_response_path(path_lower)
    is_signed = ('md5' in args) and (not is_login)

    # 🛡️ v3: SIGNED requests (md5 param = APK signature) → EXACT param forward.
    # Injection extra params jodta tha → upstream signature mismatch (5002 "Invalid
    # signature") → 422 → fake-200 → status/profiles data gayab. Ab signed requests
    # me session path se hi chalti hai (live-proven) — injection ki zaroorat hi nahi.
    if is_signed:
        STATS['signed'] += 1
        logger.info(f"🖊️ [SIGNED] /{path} → exact param forward (signature safe)")
    else:
        args, decrypted_data, injected_keys = inject_credentials(args, decrypted_data, req_content_type, saved, is_login, request.method)
        if injected_keys:
            logger.info(f"💉 [SESSION INJECTED] {injected_keys} → fresh admin session /{path}")

    # 🛡️ v3: APK ke path me STALE session → FRESH captured session rewrite (anti-logout)
    old_path_session = None
    if not is_login:
        path, old_path_session = rewrite_path_session(path, admin_session, saved.get('session', ''))
        if old_path_session:
            logger.info(f"🔄 [SESSION-REWRITE] path session {old_path_session[:12]}... → fresh admin session /{path}")
            path_lower = path.lower()

    # login path + old session + name fields → name STRIP
    if is_login and (saved.get('session') or saved.get('user_id')):
        for k in list(args.keys()):
            if k.lower() in NAME_KEYS_SET and args[k] not in (None, ''):
                del args[k]
                logger.info(f"🚫 [LOGIN-NAME STRIPPED] query arg '{k}' hata diya (account name safe)")
        decrypted_data = strip_name_fields(decrypted_data, req_content_type)

    body_params = extract_body_params(decrypted_data, req_content_type)
    combined_params = dict(body_params)
    combined_params.update({k: v for k, v in args.items() if v})

    has_session = bool(combined_params.get('session'))
    contentish_body = any(k in body_params for k in CONTENT_KEYS) or any(k in args for k in CONTENT_KEYS)

    # 🛑 1) LOGOUT/ACCOUNT-DESTROY paths
    if any(x in path_lower for x in LOGOUT_PATTERNS):
        logger.warning(f"🚫 [BLOCKED] /{path} → Action prevented!")
        return make_json_resp({"data": {"message": "Action temporarily unavailable. Please try again later."}})

    # 🛑 2) PROFILE/NAME/MOBILE endpoints — fake success
    if any(x in path_lower for x in PROFILE_PATTERNS):
        logger.warning(f"🚫 [PROFILE-BLOCKED] /{path} → Name/mobile change prevented!")
        return make_json_resp({"data": {"status": True, "message": "Profile updated successfully."}})

    # 🛑 3) NAME-CHANGE HEURISTIC (session-bearing + name field = profile change)
    if has_session and not contentish_body and not is_login:
        name_hits = find_name_keys(body_params)
        name_arg_hits = [k for k in NAME_KEYS if args.get(k)]
        if name_hits or name_arg_hits:
            logger.warning(f"🚫 [NAME-CHANGE BLOCKED] /{path} keys={name_hits or name_arg_hits} → Name change prevented!")
            return make_json_resp({"data": {"status": True, "message": "Profile updated successfully."}})

        updateish = any(x in path_lower for x in ['update', 'edit', 'change', 'modify', 'save', 'set_'])
        mobile_hits = find_mobile_keys(body_params)
        mobile_arg_hits = [k for k in MOBILE_KEYS if args.get(k)]
        if (mobile_hits or mobile_arg_hits) and updateish:
            logger.warning(f"🚫 [MOBILE-CHANGE BLOCKED] /{path} keys={mobile_hits or mobile_arg_hits} → Mobile change prevented!")
            return make_json_resp({"data": {"status": True, "message": "Profile updated successfully."}})

    if request.method == 'DELETE' or request.method == 'PUT':
        logger.warning(f"🚫 [BLOCKED {request.method}] /{path}")
        return make_json_resp({"data": {"message": "Action not available."}})

    # 🇮🇳 Force India (geo-routing)
    if 'autodetect' in path_lower or 'ip.gzip' in path_lower:
        logger.info("🇮🇳 India!")
        return make_json_resp({"region": {"ip": "127.0.0.1", "country_code2": "IN", "country_name": "India",
            "continent_code": "AS", "region_name": "Maharashtra", "city_name": "Mumbai",
            "state": "Maharashtra", "state_code": "MH", "calling_code": "91",
            "min_digits": 10, "max_digits": 10, "allowed_regions": ["all"]}})

    # 🛡️ verify_details → fake VIP (v2/mod design preserved — session check yahin fasalta tha;
    # live-proven: real response {"status":"success"} minimal hota hai, modded APK ko
    # is_subscribed=true chahiye rehta hai. Entitlement asli data user_plans se aata hai jo
    # v3 me ab REAL jaata hai.)
    if 'verify_details' in path_lower:
        logger.info("🛡️ [INTERCEPT] verify_details 200 (prevent logout)")
        return make_json_resp({"data": {
            "id": admin_user_id or "188726715626",
            "mobile": "ULTIMATEAURHAWSIKABAAP",
            "country_code": "+91",
            "status": "active",
            "is_subscribed": True,
            "subscription_end_date": "2099-12-31",
            "subscription_type": "SVOD",
            "plan_id": "vip_plan",
            "ext_mobile_number_key": True
        }})

    target_url = f"{TARGET_SCHEME}://{TARGET_BASE_DOMAIN}/{path}"

    headers = {}
    for k, v in request.headers:
        lk = k.lower()
        if lk in ['host', 'content-length', 'x-forwarded-for', 'x-forwarded-proto',
                  'x-forwarded-host', 'x-real-ip', 'accept-encoding', 'connection',
                  'transfer-encoding', 'cf-connecting-ip', 'cf-ipcountry', 'cdn-loop',
                  'cf-visitor', 'cf-ray', 'content-type']: continue
        headers[k] = v
    headers['Host'] = TARGET_BASE_DOMAIN

    # v3: signed → body/CT EXACT forward (sirf CT missing ho to json — body untouched);
    #     unsigned → v2 normalize (Rails CT-json guarantee)
    if is_signed:
        if not req_content_type and (decrypted_data or request.method in ('POST', 'PUT', 'PATCH')):
            headers['Content-Type'] = 'application/json'
        elif req_content_type:
            headers['Content-Type'] = req_content_type
        data = decrypted_data
    else:
        data, out_ct = normalize_outgoing_body(decrypted_data, req_content_type, request.method)
        if out_ct:
            headers['Content-Type'] = out_ct

    logger.info(f"🔵 [REQ] /{path} | {request.method}")

    def upstream_call(url, params, body):
        return cffi_session.request(request.method, url, headers=headers, data=body,
                                    params=params, allow_redirects=False, timeout=25)

    try:
        r = upstream_call(target_url, args, data)
        content = r.content
        status_code = r.status_code
        resp_content_type = r.headers.get('Content-Type', '')
        resp_is_html = 'html' in (resp_content_type or '').lower()

        # 🔄 v3 AUTO-HEAL: upstream 422 "Invalid Session Id" (code 1016) aaya aur
        # path me 32-hex session segment fresh se ALAG hai → token file RE-READ karke
        # latest captured session se EK retry (login mid-browse hua ho to bhi self-heal).
        if status_code == 422 and not is_login:
            try:
                body_s = content[:600].decode('utf-8', errors='ignore')
                needs_retry = ('"1016"' in body_s or 'Invalid Session Id' in body_s)
            except Exception:
                needs_retry = False
            if needs_retry:
                fresh_saved = get_saved_data()
                fresh_sess = fresh_saved.get('session', '')
                if fresh_sess and fresh_sess != admin_session:
                    admin_session = fresh_sess
                    saved = fresh_saved
                if admin_session:
                    segs = path.split('/')
                    for i in range(1, len(segs)):
                        if (segs[i-1] == 'users' and SESSION_HEX_RE.match(segs[i])
                                and segs[i] != str(admin_session)):
                            segs[i] = str(admin_session)
                            retry_path = '/'.join(segs)
                            retry_url = f"{TARGET_SCHEME}://{TARGET_BASE_DOMAIN}/{retry_path}"
                            logger.info(f"🔄 [SESSION-RETRY 1016] /{path} → fresh session se retry /{retry_path}")
                            r2 = upstream_call(retry_url, args, data)
                            if r2.status_code == 200:
                                STATS['retry_1016'] += 1
                                r = r2
                                content = r.content
                                status_code = r.status_code
                                resp_content_type = r.headers.get('Content-Type', '')
                                resp_is_html = 'html' in (resp_content_type or '').lower()
                            break

        # 🛡️ 5xx/HTML KABHI APK tak nahi jaata (Rails exception pages = logout trigger)
        if status_code >= 500 or resp_is_html:
            if is_login:
                logger.warning(f"🛡️ [UPSTREAM {status_code}] /{path} → 422 retry JSON (login safe)")
                return make_json_resp({"error": {"code": "1002", "message": "Server busy. Please retry in a moment."}}, 422)
            if 'verify_details' in path_lower:
                return fake_vip_verify_details(saved)
            logger.warning(f"🛡️ [UPSTREAM {status_code}] /{path} → 200 fake (logout prevented)")
            return fake_ok_response(saved)

        # 🛡️ 401/403/422 — sirf asli login endpoints ki REAL errors APK tak jaati hain
        if status_code in [401, 403, 422]:
            if is_login and not resp_is_html:
                logger.info(f"📤 [PASS-THROUGH {status_code}] /{path} → Real login response to APK")
            elif 'verify_details' in path_lower:
                return fake_vip_verify_details(saved)
            else:
                logger.warning(f"🛡️ [INTERCEPT {status_code}] /{path} → 200 fake (logout prevented)")
                return fake_ok_response(saved)

        # 👑 Session capture — sirf ADMIN ke verification requests se
        if is_verify_resp and status_code == 200:
            admin_in_req = (ADMIN_PHONE in original_body_str or ADMIN_PHONE_FULL in original_body_str or
                            ADMIN_PHONE in str(decrypted_args_snapshot) or ADMIN_PHONE_FULL in str(decrypted_args_snapshot) or
                            ADMIN_PHONE in path or ADMIN_PHONE_FULL in path)
            if admin_in_req:
                try:
                    resp_json = json.loads(content.decode('utf-8'))
                    new_data = {}
                    if 'data' in resp_json:
                        d = resp_json['data']
                        if 'session' in d and d['session']: new_data['session'] = d['session']
                        if 'user_id' in d and d['user_id']: new_data['user_id'] = d['user_id']
                        for mk in ['mobile_number', 'ext_user_id', 'primary_id']:
                            if mk in d and d[mk]: new_data['mobile_number'] = d[mk]; break
                    if 'auth_token' not in new_data:
                        if 'auth_token' in decrypted_args_snapshot: new_data['auth_token'] = decrypted_args_snapshot['auth_token']
                        elif original_body_str:
                            try:
                                ob = json.loads(original_body_str)
                                if 'auth_token' in ob: new_data['auth_token'] = ob['auth_token']
                            except: pass
                    if new_data:
                        save_data(new_data)
                        saved = get_saved_data()
                        admin_session = saved.get('session', '')
                        admin_user_id = saved.get('user_id', '')
                        admin_mobile = saved.get('mobile_number', ADMIN_PHONE_FULL)
                except: pass

        # 👑 Session + VIP injection — verification response me sabko admin session
        if is_verify_resp and admin_session and status_code == 200:
            try:
                resp_json = json.loads(content.decode('utf-8'))
                if 'data' in resp_json and isinstance(resp_json['data'], dict):
                    resp_json['data']['session'] = admin_session
                    if admin_user_id: resp_json['data']['user_id'] = admin_user_id
                    for pk in ['mobile_number', 'ext_user_id', 'primary_id', 'ext_mobile_number']:
                        if pk in resp_json['data']: resp_json['data'][pk] = admin_mobile
                    if 'is_subscribed' in resp_json['data']: resp_json['data']['is_subscribed'] = True
                    for sk in ('subscription_end_date', 'subscription_type', 'plan_id'):
                        if sk in resp_json['data'] and not resp_json['data'][sk]:
                            resp_json['data'][sk] = "2099-12-31" if sk == 'subscription_end_date' else ("SVOD" if sk == 'subscription_type' else "vip_plan")
                    if 'profile_obj' in resp_json['data'] and isinstance(resp_json['data']['profile_obj'], dict):
                        resp_json['data']['profile_obj']['region'] = 'IN'
                    content = json.dumps(resp_json).encode('utf-8')
                    logger.info(f"👑 [VERIFY] Injected admin session!")
            except: pass

        # 👑 Phone hiding
        if ADMIN_PHONE_FULL.encode() in content or ADMIN_PHONE.encode() in content:
            content = content.replace(ADMIN_PHONE_FULL.encode(), b"ULTIMATEAURHAWSIKABAAP")
            content = content.replace(ADMIN_PHONE.encode(), b"ULTIMATEAURHAWSIKABAAP")
            logger.info("👑 [HIDDEN] Phone number replaced with ULTIMATEAURHAWSIKABAAP")

        if content and is_text_content(resp_content_type):
            try:
                resp_str = content.decode('utf-8')
                protected_str = protect_response_content(resp_str, saved)
                if protected_str != resp_str:
                    content = protected_str.encode('utf-8')
                    logger.info(f"🔒 Response Protected (Targeted XOR) for /{path}")
            except Exception as e:
                logger.warning(f"Could not protect response: {e}")

        logger.info(f"🟢 [RESP] /{path} | {status_code} | {len(content)}B")

        resp = Response(content, status=status_code)
        excluded = ['content-encoding', 'transfer-encoding', 'content-length', 'connection']
        for h_key, h_val in r.headers.items():
            lk = h_key.lower()
            if lk in excluded: continue
            resp.headers[h_key] = h_val

        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS,PATCH'
        return resp

    except Exception as e:
        logger.error(f"ERROR /{path}: {e}")
        if is_login:
            return make_json_resp({"error": {"code": "1002", "message": "Network issue. Please retry."}}, 422)
        if 'verify_details' in path_lower:
            return fake_vip_verify_details(saved)
        return fake_ok_response(saved)

if __name__ == '__main__':
    saved = get_saved_data()
    logger.info(f"🚀 she-proxy v{APP_VERSION} ready | target={TARGET_BASE_DOMAIN} | has_vip={bool(saved.get('session'))} | phone={saved.get('admin_phone_full', ADMIN_PHONE_FULL)}")
    logger.info("🛠️ v3.0 FIXES: [1] path-decrypt segment-wise (Grown Ups playback — user_plans 404 fix) [2] signed exact-forward [3] stale-session path rewrite [4] 1016 auto-retry")
    app.run(host=os.environ.get('SHE_HOST', '127.0.0.1'),
            port=int(os.environ.get('SHE_PORT', '3452')), threaded=True)