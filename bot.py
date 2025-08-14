import os
import telebot
from dotenv import load_dotenv
from google.generativeai import configure, GenerativeModel  # Припускаємо використання google-generativeai для Gemini
import logging  # Додано для логування
import json  # Для читання config.json
import requests  # Для HTTP запитів
import sys  # Для sys.stdout
from telebot import types  # Для інлайн-кнопок та reply-клавіатури
from requests.utils import requote_uri
from bs4 import BeautifulSoup
import dateparser
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import threading
import time as _time
from pathlib import Path
from typing import Optional
import gspread
from google.oauth2.service_account import Credentials as ServiceAccountCredentials

# Налаштування логування
logging.basicConfig(filename='bot.log', level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')  # Змінено на DEBUG для дебагу
logger = logging.getLogger(__name__)
logger.debug('Тестовий дебаг лог: Логування працює')

# Додаємо хендлер для виводу в термінал
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setLevel(logging.DEBUG)
stream_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(stream_handler)

# Завантажуємо змінні середовища з .env
load_dotenv()

# Ключі з .env
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_KEY = os.getenv('GEMINI_API_KEY')
ANYCRAWL_KEY = os.getenv('ANYCRAWL_API_KEY')
ALLOWED_USER_ID = os.getenv('ALLOWED_USER_ID')
ALLOWED_USER_ID_INT = int(ALLOWED_USER_ID) if ALLOWED_USER_ID and ALLOWED_USER_ID.isdigit() else None

# Налаштування Gemini
configure(api_key=GEMINI_KEY)
model = GenerativeModel('gemini-2.0-flash')

# Завантажуємо конфіг новин
try:
    with open('config.json', 'r', encoding='utf-8') as cf:
        _cfg = json.load(cf)
        NEWS_SOURCES = _cfg.get('news_sources', {})
except Exception as e:
    logger.error(f'Не вдалося завантажити config.json: {e}')
    NEWS_SOURCES = {}


def get_category_urls(category: str) -> list[str]:
    return NEWS_SOURCES.get(category, [])


def fetch_markdown_anycrawl(url: str) -> str:
    if not ANYCRAWL_KEY:
        logger.error('ANYCRAWL_KEY відсутній для новин')
        return ""
    safe_url = requote_uri(url)
    engines = ['cheerio', 'playwright']
    for engine in engines:
        try:
            payload = {
                'url': safe_url,
                'engine': engine,
                'formats': ['markdown'],
            }
            headers = {
                'Authorization': f'Bearer {ANYCRAWL_KEY}',
                'Content-Type': 'application/json'
            }
            resp = requests.post('https://api.anycrawl.dev/v1/scrape', headers=headers, json=payload, timeout=45)
            logger.debug(f'News fetch {safe_url} via {engine} -> {resp.status_code}')
            if resp.status_code != 200:
                snippet = resp.text[:200] if resp.text else ''
                logger.warning(f'Fetch non-200 for {safe_url} via {engine}: {resp.status_code} {snippet}')
                continue
            data = resp.json()
            if data.get('success') and data.get('data', {}).get('status') == 'completed':
                md = data['data'].get('markdown', '') or ''
                if md.strip():
                    return md
                else:
                    logger.warning(f'Empty markdown for {safe_url} via {engine}')
            else:
                logger.warning(f'Scrape not completed for {safe_url} via {engine}: {data}')
        except Exception as e:
            logger.error(f'Помилка fetch_markdown_anycrawl({safe_url}) via {engine}: {e}')
            continue
    return ""


def summarize_news_with_gemini(category: str, markdown_chunks: list[str]) -> str:
    joined = "\n\n".join(markdown_chunks)
    # Обмежуємо розмір контенту до ~20к символів для стабільності
    joined = joined[:20000]
    prompt = (
        f"Ти — помічник-редактор новин. Зведи коротко по категорії '{category}'. "
        f"Виділи 10-15 ключових пунктів з джерел (дати/цифри/імена), додай 3-4 речення висновку. "
        f"Поверни у форматі: Спочатку маркований список, потім рядок 'Висновок: ...'.\n\n"
        f"Джерела (markdown нижче):\n{joined}"
    )
    try:
        response = model.generate_content(prompt)
        return (response.text or '').strip() or 'Не вдалося сформувати підсумок.'
    except Exception as e:
        logger.error(f'Помилка summarize_news_with_gemini: {e}')
        return 'Не вдалося сформувати підсумок.'


def send_long_text(chat_id: int, text: str) -> None:
    import time
    max_len = 4096
    if len(text) <= max_len:
        bot.send_message(chat_id, text)
        return
    parts = [text[i:i+max_len] for i in range(0, len(text), max_len)]
    for part in parts:
        bot.send_message(chat_id, part)
        time.sleep(1)


def extract_recent_articles_markdown(markdown_page: str, base_url: str) -> list[dict]:
    # Якщо AnyCrawl віддав markdown розділу, спробуємо вилучити посилання
    # Простий хак: витягнемо рядки з форматованими посиланнями [текст](url)
    import re
    links = re.findall(r"\[([^\]]+)\]\((https?://[^\)]+)\)", markdown_page)
    articles = []
    for title, url in links:
        articles.append({"title": title.strip(), "url": url.strip()})
    return articles


def fetch_article_if_recent(url: str, hours: int = 24) -> dict | None:
    """Завантажує сторінку статті, намагається розпізнати дату публікації; якщо свіжа — повертає dict."""
    try:
        payload = {
            'url': url,
            'engine': 'cheerio',
            'formats': ['html', 'markdown']
        }
        headers = {
            'Authorization': f'Bearer {ANYCRAWL_KEY}',
            'Content-Type': 'application/json'
        }
        resp = requests.post('https://api.anycrawl.dev/v1/scrape', headers=headers, json=payload, timeout=45)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not (data.get('success') and data.get('data', {}).get('status') == 'completed'):
            return None
        html = data['data'].get('html') or ''
        md = data['data'].get('markdown') or ''
        if not html and not md:
            return None
        # Парсимо HTML для пошуку дати
        pub_dt = None
        try:
            soup = BeautifulSoup(html, 'html.parser')
            # типові місця для дат
            candidates = []
            candidates.extend([t.get('datetime') for t in soup.find_all('time') if t.get('datetime')])
            candidates.extend([t.text for t in soup.find_all('time') if t.text])
            for sel in ['meta[property="article:published_time"]', 'meta[name="article:published_time"]', 'meta[name="pubdate"]', 'meta[name="date"]']:
                m = soup.select_one(sel)
                if m and m.get('content'):
                    candidates.append(m['content'])
            # розпізнаємо перший валідний час
            now = datetime.utcnow()
            for c in candidates:
                dt = dateparser.parse(c, settings={'TIMEZONE': 'UTC', 'RETURN_AS_TIMEZONE_AWARE': False})
                if dt:
                    pub_dt = dt
                    break
            # якщо не знайшли дату — спробуємо з markdown (часто містить дату як текст)
            if not pub_dt and md:
                dt = dateparser.parse(md[:2000], settings={'TIMEZONE': 'UTC', 'RETURN_AS_TIMEZONE_AWARE': False})
                if dt:
                    pub_dt = dt
            # фільтр за давністю
            if pub_dt and (now - pub_dt) > timedelta(hours=hours):
                return None
        except Exception:
            # Якщо не змогли визначити дату, вважаємо статтю потенційно новою і включаємо
            pub_dt = None
        # Формуємо об'єкт статті
        title = None
        if md:
            # заголовок з першого рядка markdown, якщо є
            first_line = md.strip().splitlines()[0]
            if len(first_line) < 160:
                title = first_line.strip('# ').strip()
        return {
            'url': url,
            'title': title or url,
            'published_at': pub_dt.isoformat() if pub_dt else None,
            'markdown': md[:4000]
        }
    except Exception:
        return None


def collect_recent_news_from_source(listing_url: str, hours: int = 24, max_items: int = 5) -> list[dict]:
    # Завантажуємо розділ-список, витягаємо посилання на статті, тягнемо кожну і фільтруємо за часом
    listing_md = fetch_markdown_anycrawl(listing_url)
    if not listing_md:
        return []
    items = extract_recent_articles_markdown(listing_md, listing_url)
    results = []
    for it in items[:20]:  # не більше 20 посилань зі списку
        art = fetch_article_if_recent(it['url'], hours=hours)
        if art:
            results.append(art)
            if len(results) >= max_items:
                break
    return results


def summarize_category_recent(category: str, urls: list[str], hours: int = 24) -> str:
    all_articles = []
    for u in urls:
        all_articles.extend(collect_recent_news_from_source(u, hours=hours, max_items=5))
    if not all_articles:
        return 'За останні 24 години свіжих публікацій не знайдено на наданих джерелах.'
    # Готуємо консолідований markdown для Gemini
    blocks = []
    for a in all_articles[:10]:
        pub = a['published_at'] or 'unknown'
        blocks.append(f"- {a['title']}\n{a['url']}\nОпубліковано: {pub}\n\n{a['markdown']}")
    joined = "\n\n".join(blocks)[:25000]
    prompt = (
        f"Ось список останніх матеріалів (до 24 годин) по категорії '{category}'. "
        f"Зроби стислий дайджест і цікавий висновок по останіх новинах, розскажи свою точку зору приводу новин, дай особистий висновок і прогноз застасування технології чи прогноз подій, коли це доречно: 10-15 маркованих пунктів з фактами (дати/цифри/імена), додай 'Висновок: ...'.\n\n"
        f"Матеріали:\n{joined}"
    )
    try:
        response = model.generate_content(prompt)
        return (response.text or '').strip() or 'Не вдалося сформувати підсумок.'
    except Exception as e:
        logger.error(f'Помилка summarize_category_recent: {e}')
        return 'Не вдалося сформувати підсумок.'


# Ініціалізація Telegram бота
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ----------------------- GMAIL AUTO WATCHER -----------------------
_seen_store_path = Path('gmail_seen.json')
_seen_ids_lock = threading.Lock()
_seen_ids: set[str] = set()
_notification_chat_id: Optional[int] = None


def _load_seen_ids() -> None:
    try:
        if _seen_store_path.exists():
            data = json.loads(_seen_store_path.read_text(encoding='utf-8'))
            if isinstance(data, list):
                with _seen_ids_lock:
                    _seen_ids.update(str(x) for x in data)
    except Exception as e:
        logger.warning(f'Не вдалося завантажити gmail_seen.json: {e}')


def _save_seen_ids() -> None:
    try:
        with _seen_ids_lock:
            _seen_store_path.write_text(json.dumps(list(_seen_ids)), encoding='utf-8')
    except Exception as e:
        logger.warning(f'Не вдалося зберегти gmail_seen.json: {e}')


def set_notification_chat(chat_id: int) -> None:
    global _notification_chat_id
    _notification_chat_id = chat_id


def _notify_new_email(item: dict) -> None:
    if _notification_chat_id:
        text = f"Новий лист!\nВід: {item.get('from','')}\nТема: {item.get('subject','(без теми)')}\n{item.get('snippet','')}"
        send_long_text(_notification_chat_id, text)
    elif ALLOWED_USER_ID_INT:
        text = f"Новий лист!\nВід: {item.get('from','')}\nТема: {item.get('subject','(без теми)')}\n{item.get('snippet','')}"
        try:
            bot.send_message(ALLOWED_USER_ID_INT, text)
        except Exception as e:
            logger.warning(f'Не вдалося відправити повідомлення користувачу: {e}')


def gmail_watcher_loop(interval_sec: int = 30) -> None:
    _load_seen_ids()
    while True:
        try:
            # Чекаємо поки не буде авторизації (token.json)
            if not Path(GOOGLE_TOKEN_FILE).exists():
                logger.debug('Gmail watcher: очікує авторизацію (немає token.json)')
                _time.sleep(interval_sec)
                continue
            service = get_gmail_service()
            if not service:
                _time.sleep(interval_sec)
                continue
            # Шукаємо нові непрочитані за 12h 
            msgs = list_messages(service, query='label:inbox is:unread newer_than:12h', max_results=10)
            for m in msgs:
                mid = str(m.get('id'))
                with _seen_ids_lock:
                    if mid in _seen_ids:
                        continue
                details = fetch_message_details(service, mid)
                if details:
                    _notify_new_email(details)
                    with _seen_ids_lock:
                        _seen_ids.add(mid)
                    _save_seen_ids()
        except Exception as e:
            logger.error(f'Gmail watcher error: {e}')
        finally:
            _time.sleep(interval_sec)
# --------------------- END GMAIL AUTO WATCHER ---------------------


# ----------------------- NEWS FEATURE -----------------------

def create_news_keyboard() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    buttons = [
        ("ІТ новини", "it_news"),
        ("ШІ новини", "ai_news"),
        ("Новини Києва", "kyiv_news"),
        ("Новини України", "ukraine_news"),
        ("Новини світу", "world_news"),
    ]
    for text, data in buttons:
        keyboard.add(types.InlineKeyboardButton(text=text, callback_data=data))
    return keyboard


# ----------------------- GMAIL FEATURE -----------------------
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

GOOGLE_CREDENTIALS_FILE = os.getenv('GOOGLE_CREDENTIALS_FILE', 'credentials.json')
GOOGLE_TOKEN_FILE = os.getenv('GOOGLE_TOKEN_FILE', 'token.json')


def get_gmail_service():
    creds = None
    try:
        if os.path.exists(GOOGLE_TOKEN_FILE):
            creds = UserCredentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
                    logger.error('Не знайдено credentials.json для Gmail API')
                    return None
                flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(GOOGLE_TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
        service = build('gmail', 'v1', credentials=creds)
        return service
    except Exception as e:
        logger.error(f'Помилка ініціалізації Gmail сервісу: {e}')
        return None


def _get_header(headers: list[dict], name: str) -> str:
    for h in headers or []:
        if h.get('name', '').lower() == name.lower():
            return h.get('value', '')
    return ''


def list_messages(service, query: str, max_results: int = 20) -> list[dict]:
    try:
        res = service.users().messages().list(userId='me', q=query, maxResults=max_results).execute()
        return res.get('messages', [])
    except Exception as e:
        logger.error(f'Помилка list_messages: {e}')
        return []


def fetch_message_details(service, msg_id: str) -> dict | None:
    try:
        msg = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        payload = msg.get('payload', {})
        headers = payload.get('headers', [])
        subject = _get_header(headers, 'Subject') or '(без теми)'
        from_h = _get_header(headers, 'From')
        date_h = _get_header(headers, 'Date')
        snippet = msg.get('snippet', '')
        return {
            'subject': subject,
            'from': from_h,
            'date': date_h,
            'snippet': snippet,
            'id': msg_id,
        }
    except Exception as e:
        logger.error(f'Помилка fetch_message_details: {e}')
        return None


def format_messages_markdown(items: list[dict]) -> str:
    lines = []
    for it in items:
        lines.append(f"• {it['subject']}\nВід: {it['from']}\nДата: {it['date']}\n{it['snippet']}\n")
    return "\n".join(lines)


def create_mail_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text='Непрочитані 12h', callback_data='mail_unread_12h'))
    kb.add(types.InlineKeyboardButton(text='Останні 10 (Інбокс)', callback_data='mail_last_10'))
    return kb


@bot.message_handler(commands=['news'])
def news_menu(message):
    # Обмеження доступу
    if ALLOWED_USER_ID_INT is not None and message.from_user and message.from_user.id != ALLOWED_USER_ID_INT:
        logger.warning(f"Доступ до /news заборонено для id={message.from_user.id}")
        bot.reply_to(message, "Вибачте, у вас немає доступу до цього бота.")
        return
    set_notification_chat(message.chat.id)
    kb = create_news_keyboard()
    bot.send_message(message.chat.id, "Оберіть категорію новин:", reply_markup=kb)


# Кнопка Пошта
@bot.message_handler(func=lambda m: m.text == '📧 Пошта')
def open_mail_from_button(message):
    if ALLOWED_USER_ID_INT is not None and message.from_user and message.from_user.id != ALLOWED_USER_ID_INT:
        logger.warning(f"Доступ до кнопки Пошта заборонено для id={message.from_user.id}")
        bot.reply_to(message, "Вибачте, у вас немає доступу до цього бота.")
        return
    set_notification_chat(message.chat.id)
    mail_menu(message)


# Кнопка Нотатки
@bot.message_handler(func=lambda m: m.text == '📝 Нотатки')
def open_notes_from_button(message):
    if ALLOWED_USER_ID_INT is not None and message.from_user and message.from_user.id != ALLOWED_USER_ID_INT:
        logger.warning(f"Доступ до кнопки Нотатки заборонено для id={message.from_user.id}")
        bot.reply_to(message, "Вибачте, у вас немає доступу до цього бота.")
        return
    set_notification_chat(message.chat.id)
    bot.send_message(message.chat.id, 'Нотатки: оберіть дію', reply_markup=create_notes_keyboard())


@bot.callback_query_handler(func=lambda call: call.data in {"it_news", "ai_news", "kyiv_news", "ukraine_news", "world_news"})
def handle_news_category(call):
    # Обмеження доступу
    if ALLOWED_USER_ID_INT is not None and call.from_user and call.from_user.id != ALLOWED_USER_ID_INT:
        logger.warning(f"Доступ до новин заборонено для id={call.from_user.id}")
        bot.answer_callback_query(call.id, "Немає доступу")
        return
    category = call.data
    bot.answer_callback_query(call.id, f"Збираю {category.replace('_', ' ')}...")
    logger.info(f'Старт збору новин: {category}')
    urls = get_category_urls(category)
    if not urls:
        bot.send_message(call.message.chat.id, "Немає джерел для цієї категорії.")
        return
    summary = summarize_category_recent(category, urls, hours=24)
    send_long_text(call.message.chat.id, summary)
    logger.info('Новини надіслані користувачу')
# --------------------- END NEWS FEATURE ---------------------

# Обробник команди /start
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привіт! Доступні команди: /news — меню новин, /mail — перегляд пошти.", reply_markup=create_main_keyboard())
    set_notification_chat(message.chat.id)

# ----------------------- NEWS FEATURE -----------------------

# ----------------------- GMAIL FEATURE -----------------------


@bot.message_handler(commands=['mail'])
def mail_menu(message):
    if ALLOWED_USER_ID_INT is not None and message.from_user and message.from_user.id != ALLOWED_USER_ID_INT:
        logger.warning(f"Доступ до /mail заборонено для id={message.from_user.id}")
        bot.reply_to(message, "Вибачте, у вас немає доступу до цього бота.")
        return
    set_notification_chat(message.chat.id)
    bot.send_message(message.chat.id, 'Оберіть режим перегляду пошти:', reply_markup=create_mail_keyboard())


@bot.callback_query_handler(func=lambda call: call.data in {"mail_unread_24h", "mail_unread_7d", "mail_last_20"})
def handle_mail_query(call):
    if ALLOWED_USER_ID_INT is not None and call.from_user and call.from_user.id != ALLOWED_USER_ID_INT:
        logger.warning(f"Доступ до пошти заборонено для id={call.from_user.id}")
        bot.answer_callback_query(call.id, "Немає доступу")
        return
    service = get_gmail_service()
    if not service:
        bot.answer_callback_query(call.id, "Немає підключення до Gmail")
        bot.send_message(call.message.chat.id, 'Неможливо підключитися до Gmail API. Перевірте credentials.json.')
        return
    if call.data == 'mail_unread_12h':
        query = 'label:inbox is:unread newer_than:12h'
    else:
        query = 'label:inbox newer_than:12h'
    msgs = list_messages(service, query=query, max_results=10)
    if not msgs:
        bot.send_message(call.message.chat.id, 'Листів не знайдено за вибраним фільтром.')
        return
    details = []
    for m in msgs:
        d = fetch_message_details(service, m['id'])
        if d:
            details.append(d)
    text = format_messages_markdown(details[:20])
    if not text:
        text = 'Не вдалося сформувати список листів.'
    send_long_text(call.message.chat.id, text)
# --------------------- END GMAIL FEATURE ---------------------


# ----------------------- GOOGLE SHEETS (KEEP PROXY) -----------------------
SHEETS_SERVICE_ACCOUNT_FILE = os.getenv('SHEETS_SERVICE_ACCOUNT_FILE', 'service_account.json')
SHEET_NAME = os.getenv('SHEET_NAME', 'Shopping List')

_gs_client = None
_gs_sheet = None
_last_sheet_snapshot: list[list[str]] = []


def get_sheet_client():
    global _gs_client, _gs_sheet
    if _gs_client and _gs_sheet:
        return _gs_client, _gs_sheet
    try:
        scope = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive',
        ]
        creds = ServiceAccountCredentials.from_service_account_file(SHEETS_SERVICE_ACCOUNT_FILE, scopes=scope)
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1
        _gs_client, _gs_sheet = client, sheet
        return client, sheet
    except Exception as e:
        logger.error(f'Помилка ініціалізації Google Sheets: {e}')
        return None, None


def sheet_get_all() -> list[list[str]]:
    _, sheet = get_sheet_client()
    if not sheet:
        return []
    try:
        return sheet.get_all_values()
    except Exception as e:
        logger.error(f'Помилка читання Google Sheets: {e}')
        return []


def sheet_append_row(values: list[str]) -> bool:
    _, sheet = get_sheet_client()
    if not sheet:
        return False
    try:
        sheet.append_row(values, value_input_option='USER_ENTERED')
        return True
    except Exception as e:
        logger.error(f'Помилка додавання рядка в Google Sheets: {e}')
        return False


@bot.message_handler(commands=['list_add'])
def list_add_handler(message):
    if ALLOWED_USER_ID_INT is not None and message.from_user and message.from_user.id != ALLOWED_USER_ID_INT:
        logger.warning(f"Доступ до /list_add заборонено для id={message.from_user.id}")
        bot.reply_to(message, "Вибачте, у вас немає доступу до цього бота.")
        return
    text = message.text[len('/list_add'):].strip()
    if not text:
        bot.reply_to(message, "Формат: /list_add продукт [x кількість]")
        return
    ok = sheet_append_row([text, datetime.utcnow().isoformat()])
    if ok:
        bot.reply_to(message, f'Додано в список: {text}')
    else:
        bot.reply_to(message, 'Не вдалося додати до списку (перевірте доступи до таблиці).')


def sheets_watcher_loop(interval_sec: int = 30) -> None:
    global _last_sheet_snapshot
    # Ініціальна зйомка
    _last_sheet_snapshot = sheet_get_all()
    while True:
        try:
            curr = sheet_get_all()
            if curr and curr != _last_sheet_snapshot:
                # Знайдемо нові рядки (простий diff: по довжині і вмісту)
                new_rows = []
                if len(curr) > len(_last_sheet_snapshot):
                    new_rows = curr[len(_last_sheet_snapshot):]
                else:
                    # Зміни в середині — знайдемо різницю
                    old_set = {tuple(r) for r in _last_sheet_snapshot}
                    for r in curr:
                        if tuple(r) not in old_set:
                            new_rows.append(r)
                for r in new_rows:
                    item = r[0] if r else '(порожньо)'
                    msg = f'У список додано: {item}'
                    if _notification_chat_id:
                        bot.send_message(_notification_chat_id, msg)
                    elif ALLOWED_USER_ID_INT:
                        bot.send_message(ALLOWED_USER_ID_INT, msg)
                _last_sheet_snapshot = curr
        except Exception as e:
            logger.error(f'Sheets watcher error: {e}')
        finally:
            _time.sleep(interval_sec)
# --------------------- END GOOGLE SHEETS ---------------------


# ----------------------- MAIN REPLY KEYBOARD -----------------------

def create_main_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton('📧 Пошта'), types.KeyboardButton('📝 Нотатки'))
    return kb


def create_notes_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text='Показати список', callback_data='notes_show'))
    kb.add(types.InlineKeyboardButton(text='Додати позицію', callback_data='notes_add'))
    return kb


def format_sheet_list(rows: list[list[str]]) -> str:
    if not rows:
        return 'Список порожній.'
    out = []
    for i, r in enumerate(rows, start=1):
        line = r[0].strip() if r and r[0].strip() else '(порожньо)'
        out.append(f"{i}. {line}")
    return "\n".join(out)
# --------------------- END MAIN REPLY KEYBOARD ---------------------


@bot.callback_query_handler(func=lambda call: call.data in {'notes_show','notes_add'})
def handle_notes_actions(call):
    if ALLOWED_USER_ID_INT is not None and call.from_user and call.from_user.id != ALLOWED_USER_ID_INT:
        logger.warning(f"Доступ до нотаток заборонено для id={call.from_user.id}")
        bot.answer_callback_query(call.id, "Немає доступу")
        return
    if call.data == 'notes_show':
        rows = sheet_get_all()
        text = format_sheet_list(rows)
        send_long_text(call.message.chat.id, text)
    else:  # notes_add
        bot.send_message(call.message.chat.id, "Надішліть позицію у форматі: /list_add назва [x кількість]")


# Реєстрація команд бота (меню команд у Telegram)
def register_bot_commands() -> None:
    try:
        bot.set_my_commands([
            types.BotCommand('start', 'Головне меню'),
            types.BotCommand('mail', 'Перегляд пошти'),
            types.BotCommand('news', 'Новини'),
            types.BotCommand('list_add', 'Додати позицію до списку'),
        ])
    except Exception as e:
        logger.warning(f'Не вдалося зареєструвати команди бота: {e}')


# Запуск бота
if __name__ == '__main__':
    try:
        # Стартуємо фонові наглядачі
        threading.Thread(target=gmail_watcher_loop, args=(60,), daemon=True).start()
        threading.Thread(target=sheets_watcher_loop, args=(30,), daemon=True).start()
        register_bot_commands()
        logger.info("Бот успішно запущений")
        bot.polling()
    except Exception as e:
        logger.error(f"Помилка під час запуску бота: {e}") 