import json
import os
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

PROFILES_FILE = 'profiles.json'
MESSAGES_FILE = 'messages.json'
HEARTBEAT_FILE = 'heartbeats.json'
LAST_READ_FILE = 'last_read.json'
REPORTS_FILE = 'reports.json'

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def load_json(file):
    if os.path.exists(file):
        with open(file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_json(file, data):
    with open(file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ========== ПРОФИЛИ ==========
def load_profiles():
    return load_json(PROFILES_FILE)

def save_profiles(profiles):
    save_json(PROFILES_FILE, profiles)

profiles = load_profiles()

def is_admin(user_id):
    user = profiles.get(user_id)
    return user is not None and user.get('is_admin', False)

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    user_id = data.get('id')
    if not user_id:
        return jsonify({'error': 'Missing user id'}), 400

    existing = profiles.get(user_id)
    if existing:
        # Сохраняем админский статус и бан из существующего профиля
        data['is_admin'] = existing.get('is_admin', False)
        data['banned'] = existing.get('banned', False)
        # Остальные поля (values, type_scores, dominant_type) берём из запроса,
        # но если их нет – оставляем существующие (не трогаем)
        if 'values' not in data or data['values'] is None:
            data['values'] = existing.get('values', {})
        if 'type_scores' not in data or data['type_scores'] is None:
            data['type_scores'] = existing.get('type_scores', {})
        if 'dominant_type' not in data or data['dominant_type'] is None:
            data['dominant_type'] = existing.get('dominant_type')
    else:
        # Новый пользователь: по умолчанию не админ, не забанен
        data.setdefault('is_admin', False)
        data.setdefault('banned', False)
        data.setdefault('values', {})
        data.setdefault('type_scores', {})
        data.setdefault('dominant_type', None)

    profiles[user_id] = data
    save_profiles(profiles)
    print(f"✅ Зарегистрирован {user_id} в {datetime.now()}")
    return jsonify({'status': 'ok'}), 200

@app.route('/profiles', methods=['GET'])
def get_profiles():
    exclude = request.args.get('exclude')
    if exclude:
        result = {uid: p for uid, p in profiles.items() if uid != exclude}
    else:
        result = profiles
    return jsonify(result), 200

@app.route('/profile/<user_id>', methods=['GET'])
def get_profile(user_id):
    profile = profiles.get(user_id)
    if profile:
        return jsonify(profile), 200
    return jsonify({'error': 'Not found'}), 404

# ========== СООБЩЕНИЯ ==========
def load_messages():
    return load_json(MESSAGES_FILE)

def save_messages(data):
    save_json(MESSAGES_FILE, data)

@app.route('/send_message', methods=['POST'])
def send_message():
    data = request.json
    from_user = data.get('from')
    to_user = data.get('to')
    text = data.get('text')
    if not from_user or not to_user or not text:
        return jsonify({'error': 'Missing fields'}), 400
    msg_db = load_messages()
    msg_id = msg_db.get('next_id', 1)
    msg = {
        'id': msg_id,
        'from': from_user,
        'to': to_user,
        'text': text,
        'timestamp': datetime.now().isoformat()
    }
    msg_db.setdefault('messages', []).append(msg)
    msg_db['next_id'] = msg_id + 1
    save_messages(msg_db)
    print(f"📨 Сообщение {msg_id} от {from_user} к {to_user}: {text[:50]}")
    return jsonify({'status': 'ok', 'id': msg_id}), 200

@app.route('/get_messages', methods=['GET'])
def get_messages():
    user_id = request.args.get('user_id')
    last_id = request.args.get('last_id', default=0, type=int)
    if not user_id:
        return jsonify({'error': 'Missing user_id'}), 400
    msg_db = load_messages()
    new_msgs = [m for m in msg_db.get('messages', []) if m['to'] == user_id and m['id'] > last_id]
    return jsonify({'messages': new_msgs}), 200

@app.route('/get_dialog', methods=['GET'])
def get_dialog():
    user1 = request.args.get('user1')
    user2 = request.args.get('user2')
    last_id = request.args.get('last_id', default=0, type=int)
    if not user1 or not user2:
        return jsonify({'error': 'Missing user1 or user2'}), 400
    msg_db = load_messages()
    dialog = [m for m in msg_db.get('messages', [])
              if ((m['from'] == user1 and m['to'] == user2) or
                  (m['from'] == user2 and m['to'] == user1))
              and m['id'] > last_id]
    return jsonify({'messages': dialog}), 200

# ========== ОНЛАЙН ==========
def load_heartbeats():
    return load_json(HEARTBEAT_FILE)

def save_heartbeats(data):
    save_json(HEARTBEAT_FILE, data)

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    user_id = request.json.get('user_id')
    if not user_id:
        return jsonify({'error': 'Missing user_id'}), 400
    hb = load_heartbeats()
    hb[user_id] = time.time()
    save_heartbeats(hb)
    return jsonify({'status': 'ok'}), 200

@app.route('/online', methods=['GET'])
def online():
    hb = load_heartbeats()
    now = time.time()
    online_users = [uid for uid, ts in hb.items() if now - ts < 30]
    return jsonify(online_users), 200

# ========== НЕПРОЧИТАННЫЕ ==========
def load_last_read():
    return load_json(LAST_READ_FILE)

def save_last_read(data):
    save_json(LAST_READ_FILE, data)

@app.route('/mark_read', methods=['POST'])
def mark_read():
    data = request.json
    user_id = data.get('user_id')
    from_user = data.get('from_user')
    last_read_id = data.get('last_read_id')
    if not user_id or not from_user or last_read_id is None:
        return jsonify({'error': 'Missing fields'}), 400
    lr = load_last_read()
    lr.setdefault(user_id, {})[from_user] = last_read_id
    save_last_read(lr)
    return jsonify({'status': 'ok'}), 200

@app.route('/unread', methods=['GET'])
def unread():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({'error': 'Missing user_id'}), 400
    msg_db = load_messages()
    lr = load_last_read()
    user_last_read = lr.get(user_id, {})
    unread_counts = {}
    for msg in msg_db.get('messages', []):
        if msg['to'] == user_id:
            from_user = msg['from']
            last_read_id = user_last_read.get(from_user, 0)
            if msg['id'] > last_read_id:
                unread_counts[from_user] = unread_counts.get(from_user, 0) + 1
    return jsonify(unread_counts), 200

# ========== АДМИНИСТРИРОВАНИЕ ==========
@app.route('/ban_user', methods=['POST'])
def ban_user():
    data = request.json
    admin_id = data.get('admin_id')
    user_id = data.get('user_id')
    if not admin_id or not user_id:
        return jsonify({'error': 'Missing admin_id or user_id'}), 400
    if not is_admin(admin_id):
        return jsonify({'error': 'Forbidden'}), 403
    if user_id not in profiles:
        return jsonify({'error': 'User not found'}), 404
    profiles[user_id]['banned'] = True
    save_profiles(profiles)
    print(f"🚫 Пользователь {user_id} заблокирован администратором {admin_id}")
    return jsonify({'status': 'ok'}), 200

@app.route('/delete_user', methods=['POST'])
def delete_user():
    data = request.json
    admin_id = data.get('admin_id')
    user_id = data.get('user_id')
    if not admin_id or not user_id:
        return jsonify({'error': 'Missing admin_id or user_id'}), 400
    if not is_admin(admin_id):
        return jsonify({'error': 'Forbidden'}), 403
    if user_id not in profiles:
        return jsonify({'error': 'User not found'}), 404
    # Удаляем профиль
    del profiles[user_id]
    save_profiles(profiles)
    # Удаляем сообщения
    msg_db = load_messages()
    msg_db['messages'] = [m for m in msg_db.get('messages', []) if m['from'] != user_id and m['to'] != user_id]
    save_messages(msg_db)
    # Удаляем heartbeat
    hb = load_heartbeats()
    if user_id in hb:
        del hb[user_id]
    save_heartbeats(hb)
    # Удаляем last_read
    lr = load_last_read()
    if user_id in lr:
        del lr[user_id]
    save_last_read(lr)
    print(f"🗑 Пользователь {user_id} удалён администратором {admin_id}")
    return jsonify({'status': 'ok'}), 200

# ========== ЖАЛОБЫ ==========
def load_reports():
    return load_json(REPORTS_FILE)

def save_reports(data):
    save_json(REPORTS_FILE, data)

@app.route('/report_message', methods=['POST'])
def report_message():
    data = request.json
    from_user = data.get('from_user')
    reported_user = data.get('reported_user')
    message_id = data.get('message_id')
    reason = data.get('reason')
    if not from_user or not reported_user or not message_id or not reason:
        return jsonify({'error': 'Missing fields'}), 400
    reports_db = load_reports()
    report_id = reports_db.get('next_id', 1)
    report = {
        'id': report_id,
        'from_user': from_user,
        'reported_user': reported_user,
        'message_id': message_id,
        'reason': reason,
        'timestamp': datetime.now().isoformat(),
        'resolved': False
    }
    reports_db.setdefault('reports', []).append(report)
    reports_db['next_id'] = report_id + 1
    save_reports(reports_db)
    print(f"📝 Жалоба {report_id} от {from_user} на {reported_user}")
    return jsonify({'status': 'ok'}), 200

@app.route('/get_reports', methods=['GET'])
def get_reports():
    admin_id = request.args.get('admin_id')
    if not admin_id or not is_admin(admin_id):
        return jsonify({'error': 'Forbidden'}), 403
    reports_db = load_reports()
    return jsonify(reports_db.get('reports', [])), 200

@app.route('/resolve_report', methods=['POST'])
def resolve_report():
    data = request.json
    admin_id = data.get('admin_id')
    report_id = data.get('report_id')
    if not admin_id or not report_id:
        return jsonify({'error': 'Missing admin_id or report_id'}), 400
    if not is_admin(admin_id):
        return jsonify({'error': 'Forbidden'}), 403
    reports_db = load_reports()
    for rep in reports_db.get('reports', []):
        if rep['id'] == report_id:
            rep['resolved'] = True
            break
    save_reports(reports_db)
    print(f"✅ Жалоба {report_id} отмечена как решённая админом {admin_id}")
    return jsonify({'status': 'ok'}), 200

# ========== СТАТИСТИКА ==========
@app.route('/stats', methods=['GET'])
def stats():
    admin_id = request.args.get('admin_id')
    if not admin_id or not is_admin(admin_id):
        return jsonify({'error': 'Forbidden'}), 403
    profiles_db = load_profiles()
    msg_db = load_messages()
    hb = load_heartbeats()
    now = time.time()
    total_users = len(profiles_db)
    total_messages = len(msg_db.get('messages', []))
    online_now = sum(1 for ts in hb.values() if now - ts < 30)
    # Новые пользователи за последние 7 дней
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    new_users_week = 0
    for p in profiles_db.values():
        completed = p.get('completed_at')
        if completed and completed > week_ago:
            new_users_week += 1
    return jsonify({
        'total_users': total_users,
        'total_messages': total_messages,
        'new_users_week': new_users_week,
        'online_now': online_now,
    }), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
