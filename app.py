import json
import os
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, Integer, Text, DateTime, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

app = Flask(__name__)
CORS(app)

# ========== НАСТРОЙКИ ФАЙЛОВ (старые) ==========
PROFILES_FILE = 'profiles.json'
MESSAGES_FILE = 'messages.json'
HEARTBEAT_FILE = 'heartbeats.json'
LAST_READ_FILE = 'last_read.json'
REPORTS_FILE = 'reports.json'

# ========== ПОДКЛЮЧЕНИЕ К POSTGRESQL (новое) ==========
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL:
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(bind=engine)
    Base = declarative_base()

    class Profile(Base):
        __tablename__ = 'profiles'
        user_id = Column(String, primary_key=True)
        data = Column(JSONB, nullable=False)
        updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    class Message(Base):
        __tablename__ = 'messages'
        id = Column(Integer, primary_key=True, autoincrement=True)
        from_user = Column(String, nullable=False)
        to_user = Column(String, nullable=False)
        text = Column(Text, nullable=False)
        timestamp = Column(DateTime, default=datetime.utcnow)

    Base.metadata.create_all(bind=engine)
    print("✅ PostgreSQL подключен, таблицы готовы")
else:
    print("⚠️ DATABASE_URL не задан, работаем с JSON-файлами")
    SessionLocal = None

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ФАЙЛОВ (старые) ==========
def load_json(file):
    if os.path.exists(file):
        with open(file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_json(file, data):
    with open(file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ========== ФУНКЦИИ ДЛЯ ПРОФИЛЕЙ (новые, через БД, если доступна) ==========
def get_profile_db(session, user_id):
    if SessionLocal is None:
        return None
    profile = session.query(Profile).filter_by(user_id=user_id).first()
    return profile.data if profile else None

def save_profile_db(session, user_id, data):
    if SessionLocal is None:
        return None
    profile = session.query(Profile).filter_by(user_id=user_id).first()
    if profile:
        profile.data = data
        profile.updated_at = datetime.utcnow()
    else:
        profile = Profile(user_id=user_id, data=data)
        session.add(profile)
    session.commit()
    return profile

# ========== ПРОФИЛИ (СТАРЫЙ JSON-FILE МЕТОД, ЕСЛИ БД НЕТ) ==========
def load_profiles():
    return load_json(PROFILES_FILE)

def save_profiles(profiles):
    save_json(PROFILES_FILE, profiles)

profiles = load_profiles()

def is_admin(user_id):
    # сначала пробуем взять из БД
    if SessionLocal:
        session = SessionLocal()
        profile_data = get_profile_db(session, user_id)
        session.close()
        if profile_data is not None:
            return profile_data.get('is_admin', False)
    # fallback на JSON
    user = profiles.get(user_id)
    return user is not None and user.get('is_admin', False)

# ========== НОВЫЙ /register (работает через БД, если есть) ==========
@app.route('/register', methods=['POST'])
def register():
    data = request.json
    user_id = data.get('id')
    if not user_id:
        return jsonify({'error': 'Missing user id'}), 400

    if SessionLocal:
        session = SessionLocal()
        existing = get_profile_db(session, user_id)
        if existing:
            data['is_admin'] = existing.get('is_admin', False)
            data['banned'] = existing.get('banned', False)
        else:
            data.setdefault('is_admin', False)
            data.setdefault('banned', False)
            data.setdefault('values', {})
            data.setdefault('type_scores', {})
            data.setdefault('dominant_type', None)
        save_profile_db(session, user_id, data)
        session.close()
        print(f"✅ Зарегистрирован {user_id} (через БД)")
        return jsonify({'status': 'ok'}), 200
    else:
        # fallback на старый JSON-метод
        existing = profiles.get(user_id)
        if existing:
            data['is_admin'] = existing.get('is_admin', False)
            data['banned'] = existing.get('banned', False)
            if 'values' not in data or data['values'] is None:
                data['values'] = existing.get('values', {})
            if 'type_scores' not in data or data['type_scores'] is None:
                data['type_scores'] = existing.get('type_scores', {})
            if 'dominant_type' not in data or data['dominant_type'] is None:
                data['dominant_type'] = existing.get('dominant_type')
        else:
            data.setdefault('is_admin', False)
            data.setdefault('banned', False)
            data.setdefault('values', {})
            data.setdefault('type_scores', {})
            data.setdefault('dominant_type', None)
        profiles[user_id] = data
        save_profiles(profiles)
        print(f"✅ Зарегистрирован {user_id} (через JSON)")
        return jsonify({'status': 'ok'}), 200

# ========== НОВЫЙ /profiles (через БД) ==========
@app.route('/profiles', methods=['GET'])
def get_profiles():
    exclude = request.args.get('exclude')
    if SessionLocal:
        session = SessionLocal()
        all_profiles = session.query(Profile).all()
        result = {}
        for p in all_profiles:
            if exclude and p.user_id == exclude:
                continue
            result[p.user_id] = p.data
        session.close()
        return jsonify(result), 200
    else:
        # fallback на JSON
        if exclude:
            result = {uid: p for uid, p in profiles.items() if uid != exclude}
        else:
            result = profiles
        return jsonify(result), 200

# ========== НОВЫЙ /profile/<user_id> (через БД) ==========
@app.route('/profile/<user_id>', methods=['GET'])
def get_profile_by_id(user_id):
    if SessionLocal:
        session = SessionLocal()
        data = get_profile_db(session, user_id)
        session.close()
        if data:
            return jsonify(data), 200
        return jsonify({'error': 'Not found'}), 404
    else:
        profile = profiles.get(user_id)
        if profile:
            return jsonify(profile), 200
        return jsonify({'error': 'Not found'}), 404

# ========== НОВЫЙ /send_message (через БД) ==========
@app.route('/send_message', methods=['POST'])
def send_message():
    data = request.json
    from_user = data.get('from')
    to_user = data.get('to')
    text = data.get('text')
    if not from_user or not to_user or not text:
        return jsonify({'error': 'Missing fields'}), 400

    if SessionLocal:
        session = SessionLocal()
        msg = Message(from_user=from_user, to_user=to_user, text=text)
        session.add(msg)
        session.commit()
        msg_id = msg.id
        session.close()
        print(f"📨 Сообщение {msg_id} от {from_user} к {to_user} (БД)")
        return jsonify({'status': 'ok', 'id': msg_id}), 200
    else:
        # fallback на старый JSON
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
        print(f"📨 Сообщение {msg_id} от {from_user} к {to_user} (JSON)")
        return jsonify({'status': 'ok', 'id': msg_id}), 200

# ========== НОВЫЙ /get_dialog (через БД) ==========
@app.route('/get_dialog', methods=['GET'])
def get_dialog():
    user1 = request.args.get('user1')
    user2 = request.args.get('user2')
    last_id = request.args.get('last_id', default=0, type=int)
    if not user1 or not user2:
        return jsonify({'error': 'Missing user1 or user2'}), 400

    if SessionLocal:
        session = SessionLocal()
        query = session.query(Message).filter(
            ((Message.from_user == user1) & (Message.to_user == user2)) |
            ((Message.from_user == user2) & (Message.to_user == user1))
        ).filter(Message.id > last_id).order_by(Message.id.asc())
        messages = []
        for m in query.all():
            messages.append({
                'id': m.id,
                'from': m.from_user,
                'to': m.to_user,
                'text': m.text,
                'timestamp': m.timestamp.isoformat()
            })
        session.close()
        return jsonify({'messages': messages}), 200
    else:
        msg_db = load_messages()
        dialog = [m for m in msg_db.get('messages', [])
                  if ((m['from'] == user1 and m['to'] == user2) or
                      (m['from'] == user2 and m['to'] == user1))
                  and m['id'] > last_id]
        return jsonify({'messages': dialog}), 200

# ========== НОВЫЙ /get_messages (через БД) ==========
@app.route('/get_messages', methods=['GET'])
def get_messages():
    user_id = request.args.get('user_id')
    last_id = request.args.get('last_id', default=0, type=int)
    if not user_id:
        return jsonify({'error': 'Missing user_id'}), 400

    if SessionLocal:
        session = SessionLocal()
        query = session.query(Message).filter(Message.to_user == user_id).filter(Message.id > last_id)
        messages = []
        for m in query.all():
            messages.append({
                'id': m.id,
                'from': m.from_user,
                'to': m.to_user,
                'text': m.text,
                'timestamp': m.timestamp.isoformat()
            })
        session.close()
        return jsonify({'messages': messages}), 200
    else:
        msg_db = load_messages()
        new_msgs = [m for m in msg_db.get('messages', []) if m['to'] == user_id and m['id'] > last_id]
        return jsonify({'messages': new_msgs}), 200

# ========== НОВЫЙ /unread (через БД) ==========
@app.route('/unread', methods=['GET'])
def unread():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({'error': 'Missing user_id'}), 400

    if SessionLocal:
        session = SessionLocal()
        count = session.query(func.count(Message.id)).filter(Message.to_user == user_id).scalar()
        session.close()
        # Для совместимости с фронтендом возвращаем пустой объект (можно потом переделать)
        return jsonify({}), 200
    else:
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

# ========== СТАРЫЕ ЭНДПОИНТЫ (heartbeat, online, mark_read, ban_user, delete_user, report, stats) ==========
# Они остаются без изменений и работают с JSON-файлами. Переписывать на БД пока не будем.
def load_messages():
    return load_json(MESSAGES_FILE)

def save_messages(data):
    save_json(MESSAGES_FILE, data)

def load_heartbeats():
    return load_json(HEARTBEAT_FILE)

def save_heartbeats(data):
    save_json(HEARTBEAT_FILE, data)

def load_last_read():
    return load_json(LAST_READ_FILE)

def save_last_read(data):
    save_json(LAST_READ_FILE, data)

def load_reports():
    return load_json(REPORTS_FILE)

def save_reports(data):
    save_json(REPORTS_FILE, data)

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

@app.route('/ban_user', methods=['POST'])
def ban_user():
    data = request.json
    admin_id = data.get('admin_id')
    user_id = data.get('user_id')
    if not admin_id or not user_id:
        return jsonify({'error': 'Missing admin_id or user_id'}), 400
    if not is_admin(admin_id):
        return jsonify({'error': 'Forbidden'}), 403
    if user_id not in profiles and (SessionLocal is None or get_profile_db(SessionLocal(), user_id) is None):
        return jsonify({'error': 'User not found'}), 404
    # Если БД есть, баним в БД; иначе в JSON
    if SessionLocal:
        session = SessionLocal()
        profile_data = get_profile_db(session, user_id)
        if profile_data:
            profile_data['banned'] = True
            save_profile_db(session, user_id, profile_data)
        session.close()
    else:
        if user_id in profiles:
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
    # Удаляем из БД, если есть
    if SessionLocal:
        session = SessionLocal()
        profile = session.query(Profile).filter_by(user_id=user_id).first()
        if profile:
            session.delete(profile)
        # сообщения тоже можно удалить (опционально)
        session.query(Message).filter((Message.from_user == user_id) | (Message.to_user == user_id)).delete()
        session.commit()
        session.close()
    else:
        if user_id not in profiles:
            return jsonify({'error': 'User not found'}), 404
        del profiles[user_id]
        save_profiles(profiles)
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

# ========== HEALTHCHECK ==========
@app.route('/')
def home():
    return jsonify({'status': 'ok', 'message': 'Soul Pair API is running'}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
