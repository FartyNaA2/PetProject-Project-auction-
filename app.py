import os
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests
import base64
import time
import os
import re
import urllib.parse
from serpapi import GoogleSearch
from dotenv import load_dotenv
import psutil

load_dotenv()

app = Flask(__name__)
CORS(app)


APP_ID = os.environ.get("EBAY_APP_ID")
CERT_ID = os.environ.get("EBAY_CERT_ID")
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "").strip()
SERPAPI_KEY = os.environ.get("SERPAPI_KEY")
FREEIMAGE_API_KEY = os.environ.get("FREEIMAGE_API_KEY")

CURRENT_TOKEN = None
TOKEN_EXPIRY = 0

# Словник для зберігання активних користувачів (IP -> час останнього запиту)
active_users = {}

@app.before_request
def track_user():
    """Відстежуємо активність користувачів за їх IP"""
    # Не рахуємо запити до самого статусу, щоб не створювати "штучну" активність
    if request.endpoint != 'server_status':
        ip = request.remote_addr
        active_users[ip] = time.time()

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
            print(f"❌ Помилка токена: {response.text}")
            return None
    except Exception as e:
        print(f"❌ Помилка сервера під час отримання токена: {e}")
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

    if q_lower in title:
        score += 10

    bad_words = ['adapter', 'cable', 'case', 'cover', 'glass', 'part', 'replacement', 'mount', 'stand']
    for word in bad_words:
        if word in title:
            score -= 50

    if title.startswith("for ") or "compatible with" in title:
        score -= 50

    if category == 'Smartphone':
        if any(w in title for w in ['unlocked', 'gb', '5g']):
            score += 5
    elif category == 'Console':
        if 'console' in title:
            score += 10

    return score


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
    print(f"🔍 Raw: {raw_query} | Cat: {category}")

    filters = []

    limit = request.args.get('limit', '24')
    api_offset = offset

    price_filter_active = bool(min_price or max_price)

    if price_filter_active:
        if min_price and max_price:
            filters.append(f"price:[{min_price}..{max_price}],priceCurrency:USD")
        elif min_price:
            filters.append(f"price:[{min_price}..],priceCurrency:USD")
        elif max_price:
            filters.append(f"price:[..{max_price}],priceCurrency:USD")
    else:
        if category in ['Smartphone', 'Laptop', 'Console', 'Sim Racing']:
            filters.append("price:[5..],priceCurrency:USD")

    if buying_options == 'AUCTION':
        filters.append('buyingOptions:{AUCTION}')

    filter_param = ",".join(filters)

    url = f'https://api.ebay.com/buy/browse/v1/item_summary/search?q={refined_query}&limit={limit}&offset={api_offset}'

    if filter_param:
        url += f'&filter={filter_param}'

    if sort_by:
        url += f'&sort={sort_by}'

    token = get_valid_token()
    if not token:
        return jsonify({'error': 'Не вдалося отримати токен доступу eBay'}), 500

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
        else:
            return jsonify({'error': f'Помилка від eBay: {response.status_code}'}), response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/item/<path:item_id>', methods=['GET'])
def get_item(item_id):
    country_code = request.args.get('country', 'US')
    encoded_item_id = urllib.parse.quote(item_id)
    url = f'https://api.ebay.com/buy/browse/v1/item/{encoded_item_id}'

    token = get_valid_token()
    if not token:
        return jsonify({'error': 'Не вдалося отримати токен доступу eBay'}), 500

    headers = {
        'Authorization': f'Bearer {token}',
        'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
        'X-EBAY-C-ENDUSERCTX': f'contextualLocation=country={country_code}'
    }

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'error': f'Помилка: {response.status_code}'}), response.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/translate', methods=['POST'])
def translate_text():
    data = request.json
    text_to_translate = data.get('text')
    target_lang = data.get('lang', 'EN').upper()

    if not text_to_translate:
        return jsonify({'error': 'No text provided for translation'}), 400

    if target_lang == 'EN':
        target_lang = 'EN-US'

    deepl_url = "https://api-free.deepl.com/v2/translate"

    if not DEEPL_API_KEY or "YOUR_DEEPL_API_KEY" in DEEPL_API_KEY:
        return jsonify({'error': 'DeepL API key is not configured.'}), 500

    headers = {
        'Authorization': f'DeepL-Auth-Key {DEEPL_API_KEY}',
        'Content-Type': 'application/json'
    }

    payload = {
        'text': [text_to_translate],
        'target_lang': target_lang
    }

    try:
        response = requests.post(deepl_url, headers=headers, json=payload)

        if response.status_code != 200:
            return jsonify({'error': f'DeepL Error: {response.text}'}), response.status_code

        translated_data = response.json()
        translated_text = translated_data['translations'][0]['text']

        return jsonify({'translatedText': translated_text})

    except Exception as e:
        return jsonify({'error': f'Server Error: {str(e)}'}), 500


@app.route('/api/search-by-image', methods=['POST'])
def search_by_image():
    if 'image' not in request.files:
        return jsonify({'error': 'No image uploaded'}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({'error': 'No image selected'}), 400

    try:
        print("📸 Uploading image to Freeimage.host...")
        freeimage_url = "https://freeimage.host/api/1/upload"
        file.seek(0)
        payload = {
            "key": FREEIMAGE_API_KEY,
            "action": "upload",
            "format": "json"
        }
        files = {
            "source": (file.filename, file.read(), file.mimetype)
        }

        upload_response = requests.post(freeimage_url, data=payload, files=files)

        if upload_response.status_code != 200:
            return jsonify({'error': f'Failed to upload image: {upload_response.text}'}), 500

        img_url = upload_response.json()['image']['url']
        print(f"🔗 Public Image URL: {img_url}")

        print("🤖 Analyzing with Google Lens...")
        params = {
            "api_key": SERPAPI_KEY,
            "engine": "google_lens",
            "url": img_url
        }

        search = GoogleSearch(params)
        results = search.get_dict()

        query = ""

        if "visual_matches" in results and results["visual_matches"]:
            query = results["visual_matches"][0]["title"]
            print(f"🎯 Visual Match: {query}")

        elif "knowledge_graph" in results:
            query = results["knowledge_graph"][0]["title"]
            print(f"📚 Knowledge Graph: {query}")

        if not query:
            return jsonify({'error': 'Could not identify object'}), 404

        domain_patterns = [
            r'Amazon\.com:', r'Ebay\.com:', r'Walmart\.com:', r'BestBuy\.com:',
            r'Target\.com:', r'Newegg\.com:', r'B&H Photo Video:', r'Adorama:',
            r'bhphotovideo\.com:', r'mozaracing\.com:', r'simracing\.com:'
        ]
        for pattern in domain_patterns:
            query = re.sub(pattern, '', query, flags=re.IGNORECASE).strip()

        part_patterns = [
            r'(?i)(back glass|screen protector|camera lens|battery cover|lcd display|digitizer|replacement screen|protective case|silicone cover)\s*(for|&)?\s*',
        ]
        for pattern in part_patterns:
            query = re.sub(pattern, '', query).strip()

        sim_match = re.search(r'(?i)(moza\s+r\d+|fanatec\s+(csl|dd)\d?|simucube\s+\d+|thrustmaster\s+t\d+)', query)
        if sim_match:
            query = sim_match.group(0)
        else:
            phone_match = re.search(
                r'(?i)(samsung\s+galaxy\s+[a-z]?\d+\s?(ultra|plus|fe|pro)?|iphone\s?\d+\s?(pro|max|plus|mini)?)', query)
            if phone_match:
                query = phone_match.group(0)
            else:
                console_match = re.search(r'(?i)(playstation\s?5|ps5|xbox\s?series\s?[xs]|nintendo\s?switch)', query)
                if console_match:
                    query = console_match.group(0)

        query = re.sub(r'(?i)^for\s+', '', query)
        query = re.sub(r'(?i)compatible with\s+', '', query)
        query = re.sub(r'^[:\s\-\|]+|[:\s\-\|]+$', '', query).strip()

        print(f"✨ Cleaned Query: {query}")

        return jsonify({
            'query': query,
            'confidence': 1.0
        })

    except Exception as e:
        print(f"❌ SerpApi Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/monitor')
def serve_monitor():
    return send_from_directory('.', 'monitor.html')

@app.route('/api/status')
def server_status():
    """Ендпоінт для перевірки навантаження на сервер"""
    try:
        current_time = time.time()
        
        # Рахуємо юзерів, які були активні останні 5 хвилин (300 секунд)
        active_ips = [ip for ip, last_seen in active_users.items() if current_time - last_seen < 300]
        
        # Очищаємо старі записи, щоб не забивати пам'ять
        for ip in list(active_users.keys()):
            if ip not in active_ips:
                del active_users[ip]

        return jsonify({
            'cpu_load_percent': psutil.cpu_percent(interval=0.1),
            'ram_used_percent': psutil.virtual_memory().percent,
            'ram_available_mb': round(psutil.virtual_memory().available / (1024 * 1024), 2),
            'active_users': len(active_ips)
        })
    except Exception as e:
        print(f"❌ Помилка монітора: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # Отримуємо порт від хмарного провайдера (або 5000 локально)
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)