"""
🎬 KUKU PROXY - MULTI-USER VERSION
=================================
✅ Admin token auto-capture karta hai
✅ Multiple users ke liye separate tokens manage karta hai
✅ Rate limiting per user
✅ User authentication with X-Username header
✅ SQLite database for users
✅ Complete logging system
"""

from flask import Flask, request, Response, jsonify
from curl_cffi import requests as cffi_requests
import json
import os
import time
import urllib.parse
import sqlite3
from threading import Lock

app = Flask(__name__)

# ============================================
# ⚙️ CONFIGURATION
# ============================================
TARGET = 'https://api.kukufm.com'
SKIP = {'host', 'content-length', 'transfer-encoding', 'connection', 'accept-encoding'}

# File paths - UPDATE these according to your VPS
ADMIN_FILE = '/www/wwwroot/kuku.wowtv.inddrama.in/admin_data.json'
DB_FILE = '/www/wwwroot/kuku.wowtv.inddrama.in/users.db'
LOG_FILE = '/www/wwwroot/kuku.wowtv.inddrama.in/proxy.log'

# Secret credentials
ADMIN_SECRET = "Admin_Secret_9988_WowTV_IndDrama_2024"
SECRET_UA = "kukufm-android-ree"

# Stealth mode - kaunse endpoints pe admin token inject karna hai
INJECT_PATHS = [
    'api/v2.3/channels/',
    'api/v1.2/shows/next-episode-autoplay',
    'generate-drm-token',
    'utility/refresh-cloudfront-cookie',
    'api/v2.0/episodes',
    'play'
]

# Admin tokens global state
ADMIN_STATE = {
    'access': '',
    'refresh': '',
    'uuid': '',
    'user': {},
    'phone': '',
    'captured_at': 0
}

# Thread safety
admin_lock = Lock()
db_lock = Lock()

# ============================================
# 📝 LOGGING FUNCTION
# ============================================
def log_message(message):
    """Log with timestamp"""
    try:
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        log_line = f'[{timestamp}] {message}\n'
        print(message)  # Console mein bhi dikhao
        # File mein bhi save karo
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(log_line)
    except:
        print(message)

# ============================================
# 🗄️ DATABASE FUNCTIONS
# ============================================
def init_db():
    """Database setup - users table banao"""
    try:
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()

            # Users table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    phone TEXT,
                    access_token TEXT,
                    refresh_token TEXT,
                    uuid_token TEXT,
                    premium BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_api_call TIMESTAMP
                )
            ''')

            # API logs table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS api_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT,
                    endpoint TEXT,
                    method TEXT,
                    status_code INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Rate limits table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS rate_limits (
                    username TEXT UNIQUE,
                    request_count INTEGER DEFAULT 0,
                    window_start TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            conn.commit()
            conn.close()
            log_message('[DB] ✅ Database initialized')
    except Exception as e:
        log_message(f'[DB ERROR] {e}')

# User management
def add_user(username, phone=''):
    """Naya user add karo"""
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO users (username, phone) VALUES (?, ?)',
                (username, phone)
            )
            conn.commit()
            conn.close()
            log_message(f'[USER] ✅ Added: {username}')
            return {'success': True, 'message': f'User {username} added'}
    except sqlite3.IntegrityError:
        return {'success': False, 'error': f'User {username} already exists'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def get_user(username):
    """User ka data fetch karo"""
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
            user = cursor.fetchone()
            conn.close()
            return dict(user) if user else None
    except Exception as e:
        log_message(f'[ERROR] get_user: {e}')
        return None

def update_user_tokens(username, access, refresh, uuid):
    """User ke tokens update karo"""
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users SET
                access_token = ?, refresh_token = ?, uuid_token = ?,
                updated_at = CURRENT_TIMESTAMP,
                last_api_call = CURRENT_TIMESTAMP
                WHERE username = ?
            ''', (access, refresh, uuid, username))
            conn.commit()
            conn.close()
            return True
    except Exception as e:
        log_message(f'[ERROR] update_user_tokens: {e}')
        return False

def get_all_users():
    """Sab users list karo"""
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT username, phone, premium, created_at FROM users')
            users = [dict(row) for row in cursor.fetchall()]
            conn.close()
            return users
    except Exception as e:
        log_message(f'[ERROR] get_all_users: {e}')
        return []

# ============================================
# 🔑 ADMIN TOKEN MANAGEMENT
# ============================================
def load_admin_state():
    """Admin tokens file se load karo"""
    global ADMIN_STATE
    try:
        if os.path.exists(ADMIN_FILE):
            with open(ADMIN_FILE, 'r') as f:
                data = json.load(f)
                ADMIN_STATE['access'] = data.get('access_token', '')
                ADMIN_STATE['refresh'] = data.get('refresh_token', '')
                ADMIN_STATE['uuid'] = data.get('uuid_token', '')
                ADMIN_STATE['user'] = data.get('user', {})
                ADMIN_STATE['phone'] = data.get('phone', '')
                ADMIN_STATE['captured_at'] = data.get('captured_at', 0)
                if ADMIN_STATE['access']:
                    log_message(f'[ADMIN] ✅ Loaded tokens for: {ADMIN_STATE["phone"]}')
    except Exception as e:
        log_message(f'[ADMIN ERROR] {e}')

def save_admin_state():
    """Admin tokens file mein save karo"""
    try:
        os.makedirs(os.path.dirname(ADMIN_FILE), exist_ok=True)
        with open(ADMIN_FILE, 'w') as f:
            json.dump({
                'access_token': ADMIN_STATE['access'],
                'refresh_token': ADMIN_STATE['refresh'],
                'uuid_token': ADMIN_STATE['uuid'],
                'user': ADMIN_STATE['user'],
                'phone': ADMIN_STATE['phone'],
                'captured_at': ADMIN_STATE['captured_at']
            }, f, indent=2)
    except Exception as e:
        log_message(f'[ADMIN SAVE ERROR] {e}')

def refresh_admin_token_if_needed():
    """Admin token auto-refresh karo agar purana ho"""
    global ADMIN_STATE

    if not ADMIN_STATE['access'] or not ADMIN_STATE['refresh']:
        return False

    # Agar 50 min se pehle ka nahi hai toh skip
    if time.time() - ADMIN_STATE['captured_at'] < 3000:
        return True

    log_message('[REFRESH] Admin token refresh ho raha hai...')
    try:
        body = urllib.parse.urlencode({
            'app_name': 'com.vlv.aravali.reels',
            'os_type': 'android',
            'app_build_number': '5080600',
            'installed_version': '5.8.6',
            'access_token': '',
            'refresh_token': ADMIN_STATE['refresh']
        })

        r = cffi_requests.post(
            f'{TARGET}/api/v1.1/users/get-session-token/',
            headers={
                'Host': 'api.kukufm.com',
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': SECRET_UA,
                'client-country': 'IN',
                'app-version': '5080600',
                'package-name': 'com.vlv.aravali.reels',
            },
            data=body,
            impersonate="chrome120",
            timeout=15
        )

        if r.status_code == 200:
            data = r.json()
            if data.get('access_token'):
                with admin_lock:
                    ADMIN_STATE['access'] = data['access_token']
                    if data.get('refresh_token'):
                        ADMIN_STATE['refresh'] = data['refresh_token']
                    ADMIN_STATE['captured_at'] = time.time()
                    save_admin_state()
                log_message('[REFRESH] ✅ Admin token refreshed!')
                return True
    except Exception as e:
        log_message(f'[REFRESH ERROR] {e}')

    return False

# ============================================
# 🛡️ REQUEST HANDLING
# ============================================
def build_headers(include_admin=False):
    """Request headers build karo"""
    h = {}
    for key, value in request.headers:
        if key.lower() not in SKIP:
            h[key] = value

    h['Accept-Encoding'] = 'identity'
    if 'Host' not in h:
        h['Host'] = 'api.kukufm.com'

    # Admin token inject karo
    if include_admin and ADMIN_STATE['access']:
        h['Authorization'] = f"jwt {ADMIN_STATE['access']}"

    return h

def forward(url, body=None, headers=None):
    """Original server ko request forward karo"""
    try:
        response = cffi_requests.request(
            request.method,
            url,
            headers=headers or build_headers(),
            data=body,
            timeout=20,
            impersonate="chrome120"
        )
        return response
    except Exception as e:
        log_message(f'[FORWARD ERROR] {e}')
        return None

def raw_response(r):
    """Original response ko pass-through karo"""
    if r is None:
        return jsonify({'error': 'Proxy Error'}), 502

    resp = Response(r.content, status=r.status_code)
    for key, value in r.headers.multi_items():
        if key.lower() not in SKIP:
            resp.headers.add(key, value)
    return resp

def patch_config(data):
    """Config response mein se premium checks hatao"""
    if not isinstance(data, dict):
        return data

    if 'config_data' in data and isinstance(data['config_data'], dict):
        cd = data['config_data']
        cd['show_language_change_icon'] = True
        if 'kukufm' in cd and isinstance(cd['kukufm'], dict):
            cd['kukufm']['is_international'] = False

    if 'free_trial_data' in data and isinstance(data['free_trial_data'], dict):
        ft = data['free_trial_data']
        ft['ft_enable'] = False
        ft['ft_enable_home'] = False
        ft['ft_enable_player_avenue'] = False
        ft['ft_enable_guilt_popup'] = False
        ft['ft_enable_show_page'] = False

    if 'gilt_data' in data:
        data['gilt_data'] = {}

    data['show_restore'] = False
    data['show_upgrade'] = False
    data['show_subscription'] = False

    return data

# ============================================
# 📊 LOGGING & RATE LIMITING
# ============================================
def log_api_call(username, endpoint, method, status_code):
    """API call log karo"""
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO api_logs (username, endpoint, method, status_code)
                VALUES (?, ?, ?, ?)
            ''', (username, endpoint, method, status_code))
            conn.commit()
            conn.close()
    except Exception as e:
        log_message(f'[LOG ERROR] {e}')

def check_rate_limit(username, limit=100, window=60):
    """Rate limiting check (100 requests per minute per user)"""
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT request_count, window_start FROM rate_limits
                WHERE username = ?
            ''', (username,))

            result = cursor.fetchone()

            if result:
                count, window_start_str = result
                window_start = int(window_start_str.split('.')[0]) if '.' in str(window_start_str) else int(time.mktime(time.strptime(str(window_start_str), '%Y-%m-%d %H:%M:%S')))

                if time.time() - window_start < window:
                    if count >= limit:
                        conn.close()
                        return False
                    cursor.execute('''
                        UPDATE rate_limits SET request_count = request_count + 1
                        WHERE username = ?
                    ''', (username,))
                else:
                    cursor.execute('''
                        UPDATE rate_limits SET request_count = 1, window_start = CURRENT_TIMESTAMP
                        WHERE username = ?
                    ''', (username,))
            else:
                cursor.execute('''
                    INSERT INTO rate_limits (username, request_count, window_start)
                    VALUES (?, 1, CURRENT_TIMESTAMP)
                ''', (username,))

            conn.commit()
            conn.close()

        return True
    except Exception as e:
        log_message(f'[RATE LIMIT ERROR] {e}')
        return True

# ============================================
# 🌐 ADMIN ENDPOINTS
# ============================================
@app.route('/admin/status')
def admin_status():
    """Admin status check"""
    secret = request.args.get('secret')
    if secret != ADMIN_SECRET:
        return jsonify({'error': 'Unauthorized'}), 403

    return jsonify({
        'status': 'online',
        'has_token': bool(ADMIN_STATE['access']),
        'admin_phone': ADMIN_STATE['phone'],
        'token_age_minutes': round((time.time() - ADMIN_STATE['captured_at']) / 60) if ADMIN_STATE['captured_at'] else None
    })

@app.route('/admin/set-phone', methods=['GET', 'POST'])
def admin_set_phone():
    """Admin phone set karo"""
    secret = request.args.get('secret') or (request.get_json(silent=True) or {}).get('secret')
    phone = request.args.get('phone') or (request.get_json(silent=True) or {}).get('phone')

    if secret != ADMIN_SECRET:
        return jsonify({'error': 'Unauthorized'}), 403
    if not phone:
        return jsonify({'error': 'Phone required'}), 400

    with admin_lock:
        ADMIN_STATE['phone'] = str(phone).replace("+", "").replace(" ", "")
        ADMIN_STATE['access'] = ''
        ADMIN_STATE['refresh'] = ''
        ADMIN_STATE['uuid'] = ''
        ADMIN_STATE['captured_at'] = 0
        save_admin_state()

    log_message(f'[ADMIN] Phone set to: {ADMIN_STATE["phone"]}')
    return jsonify({
        'success': True,
        'message': f'Admin phone set to {ADMIN_STATE["phone"]}. Ab app se login karo tokens capture karne ke liye.'
    })

@app.route('/admin/clear', methods=['GET', 'POST'])
def admin_clear():
    """Admin data clear karo"""
    secret = request.args.get('secret') or (request.get_json(silent=True) or {}).get('secret')
    if secret != ADMIN_SECRET:
        return jsonify({'error': 'Unauthorized'}), 403

    with admin_lock:
        ADMIN_STATE['access'] = ''
        ADMIN_STATE['refresh'] = ''
        ADMIN_STATE['uuid'] = ''
        ADMIN_STATE['phone'] = ''
        ADMIN_STATE['captured_at'] = 0
        if os.path.exists(ADMIN_FILE):
            os.remove(ADMIN_FILE)

    log_message('[ADMIN] Data cleared')
    return jsonify({'success': True, 'message': 'Admin data cleared'})

# ============================================
# 👥 USER MANAGEMENT ENDPOINTS
# ============================================
@app.route('/users/add', methods=['POST'])
def add_user_endpoint():
    """Naya user add karo"""
    secret = (request.get_json(silent=True) or {}).get('secret')
    if secret != ADMIN_SECRET:
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json(silent=True) or {}
    username = data.get('username')
    phone = data.get('phone', '')

    if not username:
        return jsonify({'error': 'Username required'}), 400

    result = add_user(username, phone)
    return jsonify(result)

@app.route('/users/list', methods=['GET'])
def list_users():
    """Sab users list karo"""
    secret = request.args.get('secret')
    if secret != ADMIN_SECRET:
        return jsonify({'error': 'Unauthorized'}), 403

    users = get_all_users()
    return jsonify({'users': users, 'total': len(users)})

@app.route('/users/<username>/info', methods=['GET'])
def get_user_info(username):
    """User info fetch karo"""
    # Optional: secret check
    secret = request.args.get('secret')
    if secret != ADMIN_SECRET:
        # Allow user to see their own info with X-Username header
        header_user = request.headers.get('X-Username', '')
        if header_user != username:
            return jsonify({'error': 'Unauthorized'}), 403

    user = get_user(username)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    return jsonify({
        'username': user['username'],
        'phone': user['phone'],
        'premium': user['premium'],
        'created_at': user['created_at'],
        'last_api_call': user['last_api_call']
    })

# ============================================
# 🎬 LOGIN ENDPOINT
# ============================================
@app.route('/api/v1.1/users/get-session-token/', methods=['POST', 'OPTIONS'])
def get_session_token():
    """Login endpoint - admin token capture karo aur user tokens update karo"""
    if request.method == 'OPTIONS':
        return jsonify({})

    # Forward to API server
    url = f'{TARGET}/api/v1.1/users/get-session-token/'
    r = forward(url, body=request.get_data())

    if r and r.status_code == 200:
        try:
            data = r.json()
            user_phone = data.get('user', {}).get('phone', '')
            access_token = data.get('access_token', '')
            refresh_token = data.get('refresh_token', '')
            uuid_token = data.get('uuid_token', '')

            # Check if this is admin phone
            if user_phone and ADMIN_STATE['phone']:
                admin_digits = ''.join(filter(str.isdigit, ADMIN_STATE['phone']))[-10:]
                user_digits = ''.join(filter(str.isdigit, user_phone))[-10:]

                if admin_digits == user_digits:
                    # Ye admin hai - save admin tokens
                    with admin_lock:
                        ADMIN_STATE['access'] = access_token
                        ADMIN_STATE['refresh'] = refresh_token
                        ADMIN_STATE['uuid'] = uuid_token
                        ADMIN_STATE['user'] = data.get('user', {})
                        ADMIN_STATE['captured_at'] = time.time()
                        save_admin_state()
                    log_message(f'[ADMIN TOKEN] ✅ Captured for {user_phone}')
            else:
                log_message(f'[LOGIN] User: {user_phone}')
        except:
            pass

    return raw_response(r)

# ============================================
# 🚀 MAIN PROXY ENDPOINT
# ============================================
@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def proxy(path):
    if request.method == 'OPTIONS':
        return Response('{}', content_type='application/json')

    # 1️⃣ USER-AGENT CHECK
    req_ua = request.headers.get('User-Agent', '')
    if SECRET_UA not in req_ua:
        return jsonify({'error': 'Unauthorized Client'}), 403

    # 2️⃣ GET USERNAME FROM HEADER
    username = request.headers.get('X-Username', '')
    if not username:
        return jsonify({'error': 'X-Username header required'}), 400

    # 3️⃣ CHECK USER EXISTS
    user = get_user(username)
    if not user:
        return jsonify({'error': 'User not found. Please register first.'}), 404

    # 4️⃣ RATE LIMITING
    if not check_rate_limit(username):
        return jsonify({'error': 'Rate limit exceeded (100 req/min)'}), 429

    # 5️⃣ FAST ENDPOINTS - Analytics, FCM, etc (instant response)
    if path.startswith('events') or any(x in path for x in [
        'record-episode-events', 'record-page-events', 'analytics/events',
        'users/me/history', 'register-fcm', 'firebase', 'fcm'
    ]):
        if request.method == 'POST':
            log_api_call(username, f'/{path}', request.method, 200)
            return jsonify({'success': True})
        return jsonify({'success': True, 'data': {'items': [], 'total_count': 0, 'page': 1}})

    # 6️⃣ BUILD URL
    url = f'{TARGET}/{path}'
    if request.query_string:
        url += f'?{request.query_string.decode()}'

    body = request.get_data() if request.method != 'GET' else None

    # 7️⃣ BLOCK PAYMENTS
    if any(x in path for x in ['payments/', 'subscription/']):
        log_api_call(username, f'/{path}', request.method, 403)
        return jsonify({'error': 'Already premium'}), 403

    if 'payment-metadata' in path:
        return jsonify({
            'success': True,
            'data': {
                'is_premium': True,
                'subscription_status': 'active',
                'expiry': '2099-12-31T23:59:59Z',
                'plan_name': 'Lifetime'
            }
        })

    # 8️⃣ CONFIG PATCHING
    if 'config' in path:
        try:
            r = forward(url, body=body)
            log_api_call(username, f'/{path}', request.method, r.status_code if r else 0)
            if r.status_code == 200 and 'json' in r.headers.get('Content-Type', ''):
                try:
                    return jsonify(patch_config(r.json()))
                except:
                    pass
            return raw_response(r)
        except:
            return jsonify({'error': 'Proxy Error'}), 502

    # 9️⃣ PROFILE & LIBRARY - NO INJECTION
    if any(x in path for x in ['users/me', 'library/items', 'recent-listens', 'listening-history']):
        try:
            r = forward(url, body=body, headers=build_headers())
            log_api_call(username, f'/{path}', request.method, r.status_code if r else 0)
            return raw_response(r)
        except:
            return jsonify({'error': 'Proxy Error'}), 502

    # 🔟 INJECT ADMIN TOKEN FOR PLAYBACK
    headers = build_headers()
    inject_admin = any(p in path for p in INJECT_PATHS)

    if inject_admin and ADMIN_STATE['access']:
        refresh_admin_token_if_needed()
        headers['Authorization'] = f"jwt {ADMIN_STATE['access']}"
        log_message(f"🥷 [ADMIN INJECTED] User={username}, Path=/{path}")

    # ➜ FORWARD REQUEST
    try:
        r = forward(url, body=body, headers=headers)
        log_api_call(username, f'/{path}', request.method, r.status_code if r else 0)
        return raw_response(r)
    except:
        return jsonify({'error': 'Proxy Error'}), 502

# ============================================
# 🔧 CORS & ERROR HANDLERS
# ============================================
@app.after_request
def cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, PATCH, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = '*'
    return response

@app.route('/health')
def health():
    return jsonify({
        'status': 'online',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'admin_token_active': bool(ADMIN_STATE['access']),
        'admin_phone': ADMIN_STATE['phone'],
        'total_users': len(get_all_users())
    })

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Endpoint not found'}), 404

# ============================================
# 🚀 STARTUP
# ============================================
if __name__ == '__main__':
    log_message('\n' + '='*50)
    log_message('🎬 KUKU PROXY - MULTI-USER v2.0 STARTING')
    log_message('='*50)

    init_db()
    load_admin_state()

    log_message(f'📍 Target: {TARGET}')
    log_message(f'🗄️ Database: {DB_FILE}')
    log_message(f'📝 Logs: {LOG_FILE}')
    log_message(f'🚀 Running on 127.0.0.1:3020')
    log_message('='*50 + '\n')

    app.run(host='127.0.0.1', port=3020, debug=False, threaded=True)
