import os
import sys
import subprocess
import requests
import base64
import time
import re
import urllib.parse
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from serpapi import GoogleSearch
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import inspect, text

# --- КОНФІГУРАЦІЯ ТА РОЛІ ---
ADMIN_ROLES = {
    'fortalo': 'superadmin',
    'fortalo1': 'admin',
    'fortalo2': 'support',
    'fortalo3': 'moderator'
}

ROLE_TITLES = {
    'superadmin': 'Головний адміністратор',
    'admin': 'Адміністратор',
    'moderator': 'Модератор',
    'support': 'Сапорт',
    'user': 'Учасник системи Auction'
}

# --- АВТОМАТИЧНЕ НАЛАШТУВАННЯ ---
def setup_environment():
    try:
        import dotenv
    except ImportError:
        print("⏳ Встановлюємо відсутні бібліотеки (dotenv)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "python-dotenv"])
        print("✅ Бібліотеки успішно встановлено!")

setup_environment()
# --------------------------------

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "dev-secret-key-12345")

# Створюємо шлях до бази даних у поточній директорії
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'auction.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)

CORS(app, supports_credentials=True)

# --- МОДЕЛІ БАЗИ ДАНИХ ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    avatar_icon = db.Column(db.Text, default='user')
    role = db.Column(db.String(20), default='user')
    created_at = db.Column(db.DateTime, default=db.func.now())
    favorites = db.relationship('Favorite', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def to_dict(self):
        date_str = self.created_at.strftime('%Y-%m-%d') if self.created_at else '2026-03-26'
        return {
            'username': self.username,
            'avatar': self.avatar_icon or 'user',
            'role': self.role or 'user',
            'role_title': ROLE_TITLES.get(self.role, ROLE_TITLES['user']),
            'created_at': date_str
        }

class Favorite(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    item_id = db.Column(db.String(100), nullable=False)
    title = db.Column(db.String(500))
    price = db.Column(db.String(100))
    image_url = db.Column(db.String(1000))
    item_url = db.Column(db.String(1000))
    shipping_info = db.Column(db.Text)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Створення бази даних та міграція
with app.app_context():
    try:
        db.create_all()
        with db.engine.connect() as conn:
            inspector = inspect(db.engine)
            
            # Додаємо колонки якщо їх немає
            cols_fav = [c['name'] for c in inspector.get_columns('favorite')]
            if 'shipping_info' not in cols_fav:
                conn.execute(text("ALTER TABLE favorite ADD COLUMN shipping_info TEXT"))
            
            cols_user = [c['name'] for c in inspector.get_columns('user')]
            if 'avatar_icon' not in cols_user:
                conn.execute(text("ALTER TABLE user ADD COLUMN avatar_icon TEXT DEFAULT 'user'"))
            if 'role' not in cols_user:
                conn.execute(text("ALTER TABLE user ADD COLUMN role TEXT DEFAULT 'user'"))
            conn.commit()

        # Оновлюємо ролі користувачів згідно ADMIN_ROLES
        all_users = User.query.all()
        for user in all_users:
            new_role = ADMIN_ROLES.get(user.username.lower(), 'user')
            if user.role != new_role:
                user.role = new_role
                print(f"🔄 Роль для '{user.username}' оновлена на {new_role}")
        
        db.session.commit()
            
    except Exception as e:
        print(f"❌ Помилка БД: {e}")

# --- EBAY & API CONFIG ---
APP_ID = os.environ.get("EBAY_APP_ID")
CERT_ID = os.environ.get("EBAY_CERT_ID")
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "").strip()
SERPAPI_KEY = os.environ.get("SERPAPI_KEY")
FREEIMAGE_API_KEY = os.environ.get("FREEIMAGE_API_KEY")

CURRENT_TOKEN = None
TOKEN_EXPIRY = 0

def get_valid_token():
    global CURRENT_TOKEN, TOKEN_EXPIRY
    if CURRENT_TOKEN and time.time() < (TOKEN_EXPIRY - 60):
        return CURRENT_TOKEN

    print("⏳ Токен застарів або відсутній. Отримуємо новий від eBay...")
    url = "https://api.ebay.com/identity/v1/oauth2/token"
    credentials = f"{APP_ID}:{CERT_ID}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {encoded_credentials}"
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope"
    }

    try:
        response = requests.post(url, headers=headers, data=data)
        if response.status_code == 200:
            token_data = response.json()
            CURRENT_TOKEN = token_data['access_token']
            TOKEN_EXPIRY = time.time() + token_data['expires_in']
            print("✅ Новий токен успішно згенеровано!")
            return CURRENT_TOKEN
        else:
            print(f"❌ eBay повернув помилку {response.status_code}: {response.text}")
    except Exception as e:
        print(f"❌ Критична помилка запиту токена: {e}")
    return None

def refine_search_query(query, category=None):
    q_lower = query.lower()
    exclusions = ' -broken -parts -repair -damaged -dummy -"box only" -icloud -locked -bad'

    if category == 'Smartphone':
        if not any(w in q_lower for w in ['phone', 'mobile', 'iphone', 'galaxy', 'pixel', 'android']):
            query += " smartphone"
        exclusions += ' -case -cover -screen -protector -glass -film -holder -skin -adapter -cable -mount'
    elif category == 'Laptop':
        if not any(w in q_lower for w in ['laptop', 'macbook', 'notebook', 'chromebook']):
            query += " laptop"
        exclusions += ' -bag -case -cover -skin -keyboard -sticker -battery -charger'
    elif category == 'Console':
        if not any(w in q_lower for w in ['console', 'ps5', 'ps4', 'xbox', 'switch', 'nintendo', 'deck']):
            query += " console"
        exclusions += ' -skin -sticker -decal -stand -fan -dock -mount -case'
    elif category == 'TV':
        if not any(w in q_lower for w in ['tv', 'television']):
            query += " tv"
        exclusions += ' -remote -mount -stand -cable -broken -parts -box'
    elif category == 'Watch':
        if not any(w in q_lower for w in ['watch']):
            query += " watch"
        exclusions += ' -band -strap -case -protector -glass -film -charger'
    elif category == 'Sim Racing':
        exclusions += ' -sticker -decal -mount -bracket -cockpit -seat -glove -shoe'
        if not any(w in q_lower for w in ['wheel', 'base', 'pedal', 'bundle']):
            query += " (wheel, base)"

    return query + exclusions

def calculate_relevance_score(item, query, category=None):
    title = item.get('title', '').lower()
    q_lower = query.lower()
    score = 0
    if q_lower in title: score += 10
    bad_words = ['adapter', 'cable', 'case', 'cover', 'glass', 'part', 'replacement', 'mount', 'stand']
    for word in bad_words:
        if word in title: score -= 50
    if title.startswith("for ") or "compatible with" in title: score -= 50
    if category == 'Smartphone':
        if any(w in title for w in ['unlocked', 'gb', '5g']): score += 5
    elif category == 'Console':
        if 'console' in title: score += 10
    return score

# --- EBAY ROUTES ---

@app.route('/api/auctions', methods=['GET'])
def get_auctions():
    raw_query = request.args.get('q', 'laptop')
    sort_by = request.args.get('sort', '')
    offset = request.args.get('offset', '0')
    buying_options = request.args.get('buyingOptions', '')
    category = request.args.get('category', '')
    max_price = request.args.get('max_price', '')
    min_price = request.args.get('min_price', '')

    refined_query = refine_search_query(raw_query, category)
    filters = []
    limit = request.args.get('limit', '24')

    price_filter_active = bool(min_price or max_price)
    if price_filter_active:
        if min_price and max_price: filters.append(f"price:[{min_price}..{max_price}],priceCurrency:USD")
        elif min_price: filters.append(f"price:[{min_price}..],priceCurrency:USD")
        elif max_price: filters.append(f"price:[..{max_price}],priceCurrency:USD")
    else:
        if category in ['Smartphone', 'Laptop', 'Console', 'Sim Racing']:
            filters.append("price:[5..],priceCurrency:USD")

    if buying_options == 'AUCTION': filters.append('buyingOptions:{AUCTION}')
    filter_param = ",".join(filters)

    url = f'https://api.ebay.com/buy/browse/v1/item_summary/search?q={refined_query}&limit={limit}&offset={offset}'
    if filter_param: url += f'&filter={filter_param}'
    if sort_by: url += f'&sort={sort_by}'

    token = get_valid_token()
    if not token: return jsonify({'error': 'Не вдалося отримати токен eBay'}), 500

    country_code = request.args.get('country', 'US')
    headers = {
        'Authorization': f'Bearer {token}',
        'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
        'X-EBAY-C-ENDUSERCTX': f'contextualLocation=country={country_code}'
    }

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            items = data.get('itemSummaries', [])
            if not sort_by and items:
                items.sort(key=lambda x: calculate_relevance_score(x, raw_query, category), reverse=True)
                data['itemSummaries'] = items
            return jsonify(data)
        return jsonify({'error': f'eBay error: {response.status_code}'}), response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/item/<path:item_id>', methods=['GET'])
def get_item(item_id):
    country_code = request.args.get('country', 'US')
    encoded_item_id = urllib.parse.quote(item_id)
    url = f'https://api.ebay.com/buy/browse/v1/item/{encoded_item_id}'

    token = get_valid_token()
    if not token: return jsonify({'error': 'No token'}), 500

    headers = {
        'Authorization': f'Bearer {token}',
        'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
        'X-EBAY-C-ENDUSERCTX': f'contextualLocation=country={country_code}'
    }

    try:
        response = requests.get(url, headers=headers)
        return jsonify(response.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/translate', methods=['POST'])
def translate_text():
    data = request.json
    text_to_translate = data.get('text')
    target_lang = data.get('lang', 'EN-US').upper()
    if target_lang == 'EN': target_lang = 'EN-US'

    if not DEEPL_API_KEY: return jsonify({'error': 'DeepL API key missing'}), 500
    
    url = "https://api-free.deepl.com/v2/translate"
    headers = {'Authorization': f'DeepL-Auth-Key {DEEPL_API_KEY}', 'Content-Type': 'application/json'}
    payload = {'text': [text_to_translate], 'target_lang': target_lang}

    try:
        res = requests.post(url, headers=headers, json=payload)
        return jsonify({'translatedText': res.json()['translations'][0]['text']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/search-by-image', methods=['POST'])
def search_by_image():
    if 'image' not in request.files: return jsonify({'error': 'No image'}), 400
    file = request.files['image']
    
    try:
        print("📸 Uploading to Freeimage...")
        file.seek(0)
        upload_res = requests.post("https://freeimage.host/api/1/upload", data={"key": FREEIMAGE_API_KEY, "action": "upload", "format": "json"}, files={"source": (file.filename, file.read(), file.mimetype)})
        img_url = upload_res.json()['image']['url']

        print("🤖 Analyzing with Google Lens...")
        search = GoogleSearch({"api_key": SERPAPI_KEY, "engine": "google_lens", "url": img_url})
        results = search.get_dict()

        query = ""
        if "visual_matches" in results and results["visual_matches"]: query = results["visual_matches"][0]["title"]
        elif "knowledge_graph" in results: query = results["knowledge_graph"][0]["title"]

        if not query: return jsonify({'error': 'Could not identify'}), 404

        # Cleaning query
        query = re.sub(r'(?i)(Amazon\.com:|Ebay\.com:|Walmart\.com:|BestBuy\.com:)', '', query).strip()
        query = re.sub(r'(?i)^for\s+|compatible with\s+', '', query).strip()
        
        return jsonify({'query': query, 'confidence': 1.0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- FAVORITES ROUTES ---

@app.route('/api/favorites', methods=['GET'])
@login_required
def get_favorites():
    favs = Favorite.query.filter_by(user_id=current_user.id).all()
    return jsonify([{'itemId': f.item_id, 'title': f.title, 'price': f.price, 'image': f.image_url, 'url': f.item_url, 'shippingInfo': f.shipping_info} for f in favs])

@app.route('/api/favorites/toggle', methods=['POST'])
@login_required
def toggle_favorite():
    data = request.json
    item_id = data.get('itemId')
    existing = Favorite.query.filter_by(user_id=current_user.id, item_id=item_id).first()
    
    if existing and data.get('updateOnly'):
        if data.get('shippingInfo'): existing.shipping_info = data.get('shippingInfo')
        db.session.commit()
        return jsonify({'status': 'updated'})
    
    if existing:
        db.session.delete(existing)
        status = 'removed'
    else:
        new_fav = Favorite(
            user_id=current_user.id, item_id=item_id, title=data.get('title'),
            price=data.get('price'), image_url=data.get('image'),
            item_url=data.get('url'), shipping_info=data.get('shippingInfo')
        )
        db.session.add(new_fav)
        status = 'added'
    
    db.session.commit()
    return jsonify({'status': status})

# --- AUTH ROUTES ---

@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.json
    if User.query.filter_by(username=data.get('username')).first():
        return jsonify({'error': 'Користувач вже існує'}), 400
    
    user = User(username=data.get('username'))
    user.set_password(data.get('password'))
    
    # Призначаємо ролі при реєстрації
    user.role = ADMIN_ROLES.get(user.username.lower(), 'user')
        
    db.session.add(user)
    db.session.commit()
    login_user(user)
    return jsonify({'user': user.to_dict()})

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    user = User.query.filter_by(username=data.get('username')).first()
    if user and user.check_password(data.get('password')):
        login_user(user, remember=True)
        return jsonify({'user': user.to_dict()})
    return jsonify({'error': 'Невірний логін або пароль'}), 401

@app.route('/api/auth/logout')
@login_required
def logout():
    logout_user()
    return jsonify({'ok': True})

@app.route('/api/auth/me')
def get_me():
    if current_user.is_authenticated:
        # Оновлення ролей у реальному часі за нікнеймом
        expected_role = ADMIN_ROLES.get(current_user.username.lower(), 'user')
        if current_user.role != expected_role:
            current_user.role = expected_role
            db.session.commit()
            
        return jsonify({'user': current_user.to_dict()})
    return jsonify({'user': None})

@app.route('/api/user/update_avatar', methods=['POST'])
@login_required
def update_avatar():
    data = request.json
    if data and data.get('avatar'):
        current_user.avatar_icon = data.get('avatar')
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'error': 'No data'}), 400

# --- STATIC ROUTES ---

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
