import json
import os
import time
import uuid
import re
import hmac
import hashlib
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, Integer, Text, DateTime, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import JSONB
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import cloudinary
import cloudinary.uploader

# ---------- Google Auth (опционально) ----------
try:
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests
    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    GOOGLE_AUTH_AVAILABLE = False
    print("⚠️ Google Auth не установлен, эндпоинт /auth/google будет недоступен")

app = Flask(__name__)
CORS(app)

# ---------- Настройка Cloudinary ----------
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET')
)

# ---------- Healthcheck ----------
@app.route('/')
def home():
    return jsonify({'status': 'ok', 'message': 'Soul Pair API is running'}), 200

# ---------- Конфигурация базы данных ----------
DATABASE_URL = os.environ.get('DATABASE_URL')
USE_DB = DATABASE_URL is not None

if USE_DB:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
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
        text = Column(Text, nullable=True)
        image_url = Column(Text, nullable=True)
        timestamp = Column(DateTime, default=datetime.utcnow)

    Base.metadata.create_all(bind=engine)
    print("✅ Подключена база данных Neon.tech")
else:
    print("⚠️ DATABASE_URL не задана, работаем с JSON-файлами")
    SessionLocal = None
    Profile = None
    Message = None

# ---------- Константы и вспомогательные функции для JSON ----------
PROFILES_FILE = 'profiles.json'
MESSAGES_FILE = 'messages.json'
HEARTBEAT_FILE = 'heartbeats.json'
LAST_READ_FILE = 'last_read.json'
REPORTS_FILE = 'reports.json'
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def load_json(file):
    if os.path.exists(file):
        with open(file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_json(file, data):
    with open(file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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

# ---------- Универсальные функции для работы с данными ----------
def get_profile_data(user_id):
    if USE_DB and Profile:
        session = SessionLocal()
        try:
            prof = session.query(Profile).filter_by(user_id=user_id).first()
            return prof.data if prof else None
        finally:
            session.close()
    else:
        profiles = load_json(PROFILES_FILE)
        return profiles.get(user_id)

def save_profile_data(user_id, data):
    if USE_DB and Profile:
        session = SessionLocal()
        try:
            prof = session.query(Profile).filter_by(user_id=user_id).first()
            if prof:
                prof.data = data
                prof.updated_at = datetime.utcnow()
            else:
                prof = Profile(user_id=user_id, data=data)
                session.add(prof)
            session.commit()
        except Exception as e:
            print(f"❌ Ошибка сохранения профиля: {e}")
            session.rollback()
        finally:
            session.close()
    else:
        profiles = load_json(PROFILES_FILE)
        profiles[user_id] = data
        save_json(PROFILES_FILE, profiles)

def is_admin(user_id):
    profile = get_profile_data(user_id)
    return profile is not None and profile.get('is_admin', False)

# ---------- Работа с сообщениями ----------
def save_message_db(from_user, to_user, text='', image_url=None):
    if USE_DB and Message:
        session = SessionLocal()
        try:
            msg = Message(from_user=from_user, to_user=to_user, text=text, image_url=image_url)
            session.add(msg)
            session.commit()
            return msg.id
        finally:
            session.close()
    else:
        msg_db = load_json(MESSAGES_FILE)
        msg_id = msg_db.get('next_id', 1)
        msg = {
            'id': msg_id,
            'from': from_user,
            'to': to_user,
            'text': text,
            'timestamp': datetime.now().isoformat(),
        }
        if image_url:
            msg['image_url'] = image_url
        msg_db.setdefault('messages', []).append(msg)
        msg_db['next_id'] = msg_id + 1
        save_json(MESSAGES_FILE, msg_db)
        return msg_id

def get_dialog_db(user1, user2, last_id):
    if USE_DB and Message:
        session = SessionLocal()
        try:
            query = session.query(Message).filter(
                ((Message.from_user == user1) & (Message.to_user == user2)) |
                ((Message.from_user == user2) & (Message.to_user == user1))
            ).filter(Message.id > last_id).order_by(Message.id.asc())
            return [{
                'id': m.id,
                'from': m.from_user,
                'to': m.to_user,
                'text': m.text or '',
                'image_url': m.image_url,
                'timestamp': m.timestamp.isoformat()
            } for m in query.all()]
        finally:
            session.close()
    else:
        msg_db = load_json(MESSAGES_FILE)
        return [m for m in msg_db.get('messages', [])
                if ((m['from'] == user1 and m['to'] == user2) or
                    (m['from'] == user2 and m['to'] == user1))
                and m['id'] > last_id]

def get_messages_for_user(user_id, last_id):
    if USE_DB and Message:
        session = SessionLocal()
        try:
            query = session.query(Message).filter(Message.to_user == user_id, Message.id > last_id)
            return [{
                'id': m.id,
                'from': m.from_user,
                'to': m.to_user,
                'text': m.text or '',
                'image_url': m.image_url,
                'timestamp': m.timestamp.isoformat()
            } for m in query.all()]
        finally:
            session.close()
    else:
        msg_db = load_json(MESSAGES_FILE)
        return [m for m in msg_db.get('messages', []) if m['to'] == user_id and m['id'] > last_id]

def get_all_profiles_exclude(exclude_user):
    if USE_DB and Profile:
        session = SessionLocal()
        try:
            all_profiles = session.query(Profile).all()
            result = {p.user_id: p.data for p in all_profiles if p.user_id != exclude_user}
            for uid in result:
                if 'password_hash' in result[uid]:
                    del result[uid]['password_hash']
            return result
        finally:
            session.close()
    else:
        profiles = load_json(PROFILES_FILE)
        if exclude_user:
            profiles = {uid: p for uid, p in profiles.items() if uid != exclude_user}
        for uid in profiles:
            if 'password_hash' in profiles[uid]:
                del profiles[uid]['password_hash']
        return profiles

def delete_user_completely(user_id):
    if USE_DB and Profile and Message:
        session = SessionLocal()
        try:
            prof = session.query(Profile).filter_by(user_id=user_id).first()
            if prof:
                session.delete(prof)
            session.query(Message).filter((Message.from_user == user_id) | (Message.to_user == user_id)).delete()
            session.commit()
        finally:
            session.close()
    else:
        profiles = load_json(PROFILES_FILE)
        if user_id in profiles:
            del profiles[user_id]
            save_json(PROFILES_FILE, profiles)
        msg_db = load_json(MESSAGES_FILE)
        msg_db['messages'] = [m for m in msg_db.get('messages', []) if m['from'] != user_id and m['to'] != user_id]
        save_json(MESSAGES_FILE, msg_db)
    hb = load_heartbeats()
    if user_id in hb:
        del hb[user_id]
    save_heartbeats(hb)
    lr = load_last_read()
    if user_id in lr:
        del lr[user_id]
    save_last_read(lr)

# ---------- ЭНДПОИНТЫ ----------
# Регистрация (с паролем) – здесь добавлено условие для администратора
@app.route('/register', methods=['POST'])
def register():
    data = request.json
    user_id = data.get('id')
    if not user_id:
        return jsonify({'error': 'Missing user id'}), 400
    existing = get_profile_data(user_id)
    password = data.get('password')
    if existing:
        if password:
            if len(password) < 6:
                return jsonify({'error': 'Password must be at least 6 characters'}), 400
            password_hash = generate_password_hash(password)
            data['password_hash'] = password_hash
        data['is_admin'] = existing.get('is_admin', False)
        data['banned'] = existing.get('banned', False)
        if 'values' not in data or data['values'] is None:
            data['values'] = existing.get('values', {})
        if 'type_scores' not in data or data['type_scores'] is None:
            data['type_scores'] = existing.get('type_scores', {})
        if 'dominant_type' not in data or data['dominant_type'] is None:
            data['dominant_type'] = existing.get('dominant_type')
    else:
        if not password or len(password) < 6:
            return jsonify({'error': 'Password must be at least 6 characters'}), 400
        password_hash = generate_password_hash(password)
        data['password_hash'] = password_hash
        data.setdefault('is_admin', False)
        data.setdefault('banned', False)
        data.setdefault('values', {})
        data.setdefault('type_scores', {})
        data.setdefault('dominant_type', None)

    # ---------- НАЗНАЧЕНИЕ АДМИНИСТРАТОРА ПО EMAIL ----------
    # Если регистрируется пользователь с email electron.geo@gmail.com, даём ему права администратора
    if user_id == 'electron.geo@gmail.com':
        data['is_admin'] = True
        print(f"👑 Пользователь {user_id} назначен администратором")

    save_profile_data(user_id, data)
    print(f"✅ Зарегистрирован {user_id}")
    return jsonify({'status': 'ok'}), 200

# Логин по email+пароль
@app.route('/login', methods=['POST'])
def login():
    data = request.json
    user_id = data.get('id')
    password = data.get('password')
    if not user_id or not password:
        return jsonify({'error': 'Missing id or password'}), 400
    profile = get_profile_data(user_id)
    if not profile:
        return jsonify({'error': 'User not found'}), 404
    stored_hash = profile.get('password_hash')
    if not stored_hash:
        return jsonify({'error': 'Account not secured. Please re-register.'}), 401
    if not check_password_hash(stored_hash, password):
        return jsonify({'error': 'Invalid password'}), 401
    return jsonify({'status': 'ok', 'user_id': user_id}), 200

# Вход через Google
@app.route('/auth/google', methods=['POST'])
def google_auth():
    if not GOOGLE_AUTH_AVAILABLE:
        return jsonify({'error': 'Google auth not configured on this server'}), 501
    token = request.json.get('idToken')
    if not token:
        return jsonify({'error': 'Missing token'}), 400
    CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', 
'69193608569-lljeikos3ttitkug6vg05dn43mshsivq.apps.googleusercontent.com')
    try:
        info = id_token.verify_oauth2_token(token, google_requests.Request(), CLIENT_ID)
        if info is None:
            return jsonify({'error': 'Invalid token'}), 401
        email = info['email']
        name = info.get('name', '')
        picture = info.get('picture', '')
        user_id = email.replace('@', '_at_').replace('.', '_dot_')
        profile = get_profile_data(user_id)
        if not profile:
            profile = {
                'id': user_id,
                'email': email,
                'name': name,
                'selected_avatar': picture,
                'is_admin': False,
                'banned': False,
                'values': {},
                'type_scores': {},
                'dominant_type': None,
                'completed_at': datetime.now().isoformat(),
            }
            # Если email совпадает с админским, назначаем администратора
            if email == 'electron.geo@gmail.com':
                profile['is_admin'] = True
                print(f"👑 Google-пользователь {user_id} назначен администратором")
            save_profile_data(user_id, profile)
        else:
            if name and name != profile.get('name'):
                profile['name'] = name
            if picture and picture != profile.get('selected_avatar'):
                profile['selected_avatar'] = picture
            save_profile_data(user_id, profile)
        return jsonify({'status': 'ok', 'user_id': user_id}), 200
    except Exception as e:
        print(f'Google auth error: {e}')
        return jsonify({'error': 'Authentication failed'}), 500

# Профили
@app.route('/profiles', methods=['GET'])
def get_profiles():
    exclude = request.args.get('exclude')
    result = get_all_profiles_exclude(exclude)
    return jsonify(result), 200

@app.route('/profile/<user_id>', methods=['GET'])
def get_profile(user_id):
    profile = get_profile_data(user_id)
    if profile:
        if 'password_hash' in profile:
            profile = profile.copy()
            del profile['password_hash']
        return jsonify(profile), 200
    return jsonify({'error': 'Not found'}), 404

# Сообщения
@app.route('/send_message', methods=['POST'])
def send_message():
    data = request.json
    from_user = data.get('from')
    to_user = data.get('to')
    text = data.get('text', '')
    image_url = data.get('image_url')
    if not from_user or not to_user:
        return jsonify({'error': 'Missing from or to'}), 400
    if not text and not image_url:
        return jsonify({'error': 'No content'}), 400
    msg_id = save_message_db(from_user, to_user, text, image_url)
    print(f"📨 Сообщение {msg_id} от {from_user} к {to_user}")
    return jsonify({'status': 'ok', 'id': msg_id}), 200

@app.route('/get_messages', methods=['GET'])
def get_messages():
    user_id = request.args.get('user_id')
    last_id = request.args.get('last_id', default=0, type=int)
    if not user_id:
        return jsonify({'error': 'Missing user_id'}), 400
    new_msgs = get_messages_for_user(user_id, last_id)
    return jsonify({'messages': new_msgs}), 200

@app.route('/get_dialog', methods=['GET'])
def get_dialog():
    user1 = request.args.get('user1')
    user2 = request.args.get('user2')
    last_id = request.args.get('last_id', default=0, type=int)
    if not user1 or not user2:
        return jsonify({'error': 'Missing user1 or user2'}), 400
    dialog = get_dialog_db(user1, user2, last_id)
    return jsonify({'messages': dialog}), 200

# Онлайн
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

# Непрочитанные
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
    lr = load_last_read()
    user_last_read = lr.get(user_id, {})
    unread_counts = {}
    if USE_DB:
        session = SessionLocal()
        try:
            all_msgs = session.query(Message).filter(Message.to_user == user_id).all()
            for msg in all_msgs:
                from_user = msg.from_user
                last_read_id = user_last_read.get(from_user, 0)
                if msg.id > last_read_id:
                    unread_counts[from_user] = unread_counts.get(from_user, 0) + 1
        finally:
            session.close()
    else:
        msg_db = load_json(MESSAGES_FILE)
        for msg in msg_db.get('messages', []):
            if msg['to'] == user_id:
                from_user = msg['from']
                last_read_id = user_last_read.get(from_user, 0)
                if msg['id'] > last_read_id:
                    unread_counts[from_user] = unread_counts.get(from_user, 0) + 1
    return jsonify(unread_counts), 200

# Администрирование
@app.route('/ban_user', methods=['POST'])
def ban_user():
    data = request.json
    admin_id = data.get('admin_id')
    user_id = data.get('user_id')
    if not admin_id or not user_id:
        return jsonify({'error': 'Missing admin_id or user_id'}), 400
    if not is_admin(admin_id):
        return jsonify({'error': 'Forbidden'}), 403
    profile = get_profile_data(user_id)
    if profile is None:
        return jsonify({'error': 'User not found'}), 404
    profile['banned'] = True
    save_profile_data(user_id, profile)
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
    delete_user_completely(user_id)
    print(f"🗑 Пользователь {user_id} удалён администратором {admin_id}")
    return jsonify({'status': 'ok'}), 200

# Жалобы
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

# Статистика
@app.route('/stats', methods=['GET'])
def stats():
    admin_id = request.args.get('admin_id')
    if not admin_id or not is_admin(admin_id):
        return jsonify({'error': 'Forbidden'}), 403
    now = time.time()
    hb = load_heartbeats()
    online_now = sum(1 for ts in hb.values() if now - ts < 30)
    if USE_DB:
        session = SessionLocal()
        try:
            total_users = session.query(Profile).count()
            total_messages = session.query(Message).count()
            week_ago = datetime.utcnow() - timedelta(days=7)
            new_users_week = session.query(Profile).filter(Profile.updated_at > week_ago).count()
        finally:
            session.close()
    else:
        profiles_db = load_json(PROFILES_FILE)
        msg_db = load_json(MESSAGES_FILE)
        total_users = len(profiles_db)
        total_messages = len(msg_db.get('messages', []))
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

# Удаление сообщения
@app.route('/delete_message', methods=['POST'])
def delete_message():
    data = request.json
    user_id = data.get('user_id')
    message_id = data.get('message_id')
    if not user_id or not message_id:
        return jsonify({'error': 'Missing user_id or message_id'}), 400
    if USE_DB and Message:
        session = SessionLocal()
        try:
            msg = session.query(Message).filter(Message.id == message_id).first()
            if not msg:
                return jsonify({'error': 'Message not found'}), 404
            if msg.from_user != user_id:
                return jsonify({'error': 'You can only delete your own messages'}), 403
            if msg.image_url and 'cloudinary.com' in msg.image_url:
                try:
                    match = re.search(r'/upload/(?:v\d+/)?([^/.]+)(?:\.[^.]+)?$', msg.image_url)
                    if match:
                        public_id = match.group(1)
                        cloudinary.uploader.destroy(public_id)
                        print(f"🗑 Удалено фото из Cloudinary: {public_id}")
                except Exception as e:
                    print(f"⚠️ Ошибка удаления фото из Cloudinary: {e}")
            session.delete(msg)
            session.commit()
            print(f"🗑 Сообщение {message_id} удалено пользователем {user_id}")
            return jsonify({'status': 'ok'}), 200
        except Exception as e:
            session.rollback()
            print(f"❌ Ошибка удаления сообщения: {e}")
            return jsonify({'error': 'Internal server error'}), 500
        finally:
            session.close()
    else:
        msg_db = load_json(MESSAGES_FILE)
        messages = msg_db.get('messages', [])
        found = None
        for i, m in enumerate(messages):
            if m['id'] == message_id:
                found = i
                break
        if found is None:
            return jsonify({'error': 'Message not found'}), 404
        msg = messages[found]
        if msg['from'] != user_id:
            return jsonify({'error': 'You can only delete your own messages'}), 403
        del messages[found]
        save_json(MESSAGES_FILE, msg_db)
        print(f"🗑 Сообщение {message_id} удалено из JSON")
        return jsonify({'status': 'ok'}), 200

# Загрузка фото на Cloudinary с модерацией
YOUR_WEBHOOK_URL = 'https://soul-backend-5pcj.onrender.com/webhook/moderation'

@app.route('/upload_photo', methods=['POST'])
def upload_photo():
    if 'photo' not in request.files:
        return jsonify({'error': 'No photo'}), 400
    file = request.files['photo']
    if file.filename == '' or not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file'}), 400
    try:
        upload_result = cloudinary.uploader.upload(
            file,
            upload_preset='soul_pair_moderation',
            notification_url=YOUR_WEBHOOK_URL,
            folder='soul_pair_users_photos'
        )
        photo_url = upload_result['secure_url']
        print(f"✅ Фото загружено на модерацию: {photo_url}")
        return jsonify({'photo_url': photo_url}), 200
    except Exception as e:
        print(f"❌ Ошибка загрузки в Cloudinary: {e}")
        return jsonify({'error': 'Failed to upload photo'}), 500

# Вебхук модерации
@app.route('/webhook/moderation', methods=['POST'])
def moderation_webhook():
    data = request.get_json()
    try:
        if 'moderation' not in data or not data['moderation']:
            print("ℹ️ Уведомление о начале модерации или промежуточное, пропускаем")
            return jsonify({'status': 'ok'}), 200
        public_id = data.get('public_id')
        moderation_status = data['moderation'][0]['status']
        print(f"📸 Вердикт для {public_id}: {moderation_status}")
        if moderation_status == 'rejected':
            print(f"🚫 Фото {public_id} отклонено. Удаляем из Cloudinary...")
            cloudinary.uploader.destroy(public_id)
    except Exception as e:
        print(f"❌ Ошибка обработки вебхука: {e}")
    return jsonify({'status': 'ok'}), 200

# ---------- ЗАПУСК ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
