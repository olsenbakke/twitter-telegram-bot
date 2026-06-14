import os
import json
import asyncio
import logging
import feedparser
import html
import re
import socket
from copy import deepcopy
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

load_dotenv()

# =============================
# Config
# =============================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

DATA_FILE      = os.path.join(DATA_DIR, "tracked_users.json")
FILTERS_FILE   = os.path.join(DATA_DIR, "filters.json")
SENT_IDS_FILE  = os.path.join(DATA_DIR, "sent_ids.json")   # ← فایل dedup جدید

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "90"))
HTTP_TIMEOUT   = int(os.getenv("HTTP_TIMEOUT", "20"))
MAX_BACKFILL_ON_MISSING_LAST_ID = int(os.getenv("MAX_BACKFILL_ON_MISSING_LAST_ID", "5"))

TRANSLATE_FA     = os.getenv("TRANSLATE_FA", "true").lower() in ("1", "true", "yes", "on")
TRANSLATE_ENGINE = os.getenv("TRANSLATE_ENGINE", "google").lower().strip()
TRANSLATE_CACHE_MAX = int(os.getenv("TRANSLATE_CACHE_MAX", "1500"))

RSS_HUB_URL = os.getenv("RSS_HUB_URL", "https://rsshub.app").rstrip("/")
RSS_SOURCES = [
    RSS_HUB_URL + "/twitter/user/{username}",
    "https://rsshub.rssforever.com/twitter/user/{username}",
    "https://xcancel.com/{username}/rss",
    "https://nitter.poast.org/{username}/rss",
    "https://nitter.net/{username}/rss",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120 Safari/537.36"
)

# ظرفیت حافظه dedup در RAM (per chat_id)
DEDUP_MAX_PER_CHAT = int(os.getenv("DEDUP_MAX_PER_CHAT", "1000"))
# چند آیدی آخر ارسال‌شده per (chat_id, username) ذخیره بشه در فایل
DEDUP_FILE_MAX_PER_KEY = int(os.getenv("DEDUP_FILE_MAX_PER_KEY", "200"))

socket.setdefaulttimeout(HTTP_TIMEOUT)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =============================
# ── DEDUP لایه دوگانه ──────
# =============================
# RAM: dict of  chat_id_str -> set of tweet_ids
_dedup_ram: Dict[str, Set[str]] = {}

def _dedup_key(chat_id: Any) -> str:
    return str(chat_id)

def _load_sent_ids() -> Dict[str, List[str]]:
    """بارگذاری sent_ids از فایل."""
    if os.path.exists(SENT_IDS_FILE):
        try:
            with open(SENT_IDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"sent_ids.json load failed: {e}")
    return {}

def _save_sent_ids_file(data: Dict[str, List[str]]) -> None:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(SENT_IDS_FILE)), exist_ok=True)
        with open(SENT_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"sent_ids.json save failed: {e}")

# بارگذاری اولیه فایل به RAM
def _init_dedup() -> None:
    data = _load_sent_ids()
    for key, ids in data.items():
        if isinstance(ids, list):
            _dedup_ram[key] = set(ids[-DEDUP_MAX_PER_CHAT:])
    logger.info(f"Dedup init: {sum(len(v) for v in _dedup_ram.values())} IDs loaded for {len(_dedup_ram)} chats")

def is_already_sent(chat_id: Any, tweet_id: str) -> bool:
    """آیا این tweet قبلاً برای این چت ارسال شده؟"""
    if not tweet_id or not re.match(r"^\d+$", str(tweet_id)):
        return False
    key = _dedup_key(chat_id)
    return tweet_id in _dedup_ram.get(key, set())

def mark_as_sent(chat_id: Any, tweet_id: str) -> None:
    """ثبت tweet_id به‌عنوان ارسال‌شده — هم RAM هم فایل."""
    if not tweet_id or not re.match(r"^\d+$", str(tweet_id)):
        return
    key = _dedup_key(chat_id)
    if key not in _dedup_ram:
        _dedup_ram[key] = set()
    _dedup_ram[key].add(tweet_id)
    # اگه بیش از حد شد، قدیمی‌ترها رو نمیشه از set حذف کرد (unordered)،
    # پس فقط در save فایل کوتاه می‌کنیم.
    _flush_dedup_to_file()

def _flush_dedup_to_file() -> None:
    """همه RAM dedup رو در فایل بنویس (با محدودیت تعداد)."""
    data: Dict[str, List[str]] = {}
    for key, ids in _dedup_ram.items():
        lst = list(ids)
        # محدود کردن به آخرین N عدد (ids مرتب‌شده نیستن — همه رو نگه‌دار تا سقف)
        data[key] = lst[-DEDUP_FILE_MAX_PER_KEY:] if len(lst) > DEDUP_FILE_MAX_PER_KEY else lst
    _save_sent_ids_file(data)

# =============================
# Translation
# =============================
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
translator = None
groq_client = None
translate_engine_name = "off"
translate_cache: Dict[str, str] = {}

CRYPTO_TERMS = [
    "airdrop", "airdrops", "mainnet", "testnet", "listing", "listings",
    "delist", "delisting", "staking", "unstaking", "yield", "swap",
    "bridge", "bridges", "mint", "nft", "nfts", "dao", "defi", "cefi",
    "dex", "cex", "wallet", "wallets", "seed phrase", "token", "tokens",
    "coin", "coins", "memecoin", "memecoins", "meme coin", "presale",
    "launchpad", "roadmap", "snapshot", "halving", "burn", "claim",
    "farming", "liquidity", "pool", "tvl", "apr", "apy", "bullish",
    "bearish", "long", "short", "leverage", "margin", "spot", "futures",
    "perp", "perps", "lfg", "hodl", "fud", "fomo", "alpha", "beta",
    "whitelist", "allowlist", "kyc", "aml", "ido", "ieo", "ico", "tge",
    "tokenomics", "gas fee", "gas", "layer 2", "l2", "layer 1", "l1",
    "rollup", "rollups", "zk", "on-chain", "off-chain", "governance",
    "validator", "validators", "node", "nodes", "rpc", "api",
    "airdrop hunter", "airdrop hunters",
]

_crypto_terms_pattern = "|".join(
    re.escape(term) for term in sorted(CRYPTO_TERMS, key=len, reverse=True)
)
PROTECTED_RE = re.compile(
    r"https?://[^\s<>()]+|"
    r"www\.[^\s<>()]+|"
    r"@\w+|"
    r"#[A-Za-z0-9_\u0600-\u06FF]+|"
    r"\$[A-Za-z][A-Za-z0-9_]*|"
    r"\b(?:" + _crypto_terms_pattern + r")\b",
    re.IGNORECASE,
)

def init_translator() -> None:
    global translator, groq_client, translate_engine_name
    if not TRANSLATE_FA or TRANSLATE_ENGINE == "off":
        translate_engine_name = "off"
        return
    if TRANSLATE_ENGINE in ("auto", "groq", "groq-ai") and GROQ_API_KEY:
        try:
            from groq import Groq  # type: ignore
            groq_client = Groq(api_key=GROQ_API_KEY)
            translate_engine_name = "groq-ai"
            logger.info("Translator: Groq AI enabled")
            if TRANSLATE_ENGINE in ("groq", "groq-ai"):
                return
        except Exception as e:
            logger.warning(f"Groq init failed: {e}")
            groq_client = None
    if TRANSLATE_ENGINE in ("auto", "google", "") or not groq_client:
        try:
            from deep_translator import GoogleTranslator
            translator = GoogleTranslator(source="auto", target="fa")
            translate_engine_name = "google"
            logger.info("Translator: Google enabled with crypto protection")
        except Exception as e:
            logger.warning(f"Google translator disabled: {e}")
            translator = None
            if not groq_client:
                translate_engine_name = "off"

init_translator()

def normalize_tweet_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\u200f", "").replace("\u200e", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def persian_ratio(text: str) -> float:
    letters = re.findall(r"[A-Za-z\u0600-\u06FF]", text or "")
    if not letters:
        return 0.0
    fa_letters = re.findall(r"[\u0600-\u06FF]", text or "")
    return len(fa_letters) / len(letters)

def protect_special_terms(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    protected: List[Tuple[str, str]] = []
    def repl(match: re.Match) -> str:
        token = f"XTBKEEP{len(protected):03d}X"
        protected.append((token, match.group(0)))
        return token
    return PROTECTED_RE.sub(repl, text), protected

def restore_special_terms(text: str, protected: List[Tuple[str, str]]) -> str:
    for token, value in protected:
        text = re.sub(re.escape(token), value, text, flags=re.IGNORECASE)
    return text

def postprocess_persian_translation(text: str) -> str:
    replacements = {
        "قطره هوایی": "airdrop", "قطره‌های هوایی": "airdrops",
        "ایردراپ": "airdrop", "ایردراپ‌ها": "airdrops",
        "شبکه اصلی": "mainnet", "شبکه آزمایشی": "testnet",
        "فهرست شدن": "لیست شدن", "فهرست شده": "لیست شده",
        "فهرست می‌شود": "لیست می‌شود",
        "سهام گذاری": "staking", "سهام‌گذاری": "staking",
        "کیف پول": "ولت", "کیف‌پول": "ولت",
        "رمزنگاری": "کریپتو", "ارز دیجیتال": "کریپتو",
        "ارزهای دیجیتال": "کریپتوها",
        "نشانه": "توکن", "نشانه‌ها": "توکن‌ها",
        "ادعا کنید": "claim کنید", "مطالبه کنید": "claim کنید",
        "راه اندازی": "لانچ", "راه‌اندازی": "لانچ",
        "صعودی": "bullish", "نزولی": "bearish",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\s+([،؛؟.!?])", r"\1", text)
    text = re.sub(r"([؟!]){3,}", r"\1\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()

def translate_with_groq(text: str) -> Optional[str]:
    if not groq_client:
        return None
    try:
        prompt = (
            "Translate the following English tweet to natural, colloquial Persian.\n"
            "Rules:\n"
            "- Keep @usernames, #hashtags, $tickers, URLs, emojis exactly as-is\n"
            "- Keep common crypto terms like airdrop, mainnet, listing, LFG, HODL, DeFi in English\n"
            "- Be concise and natural, like a native Persian crypto Twitter user\n"
            "- Output ONLY the Persian translation, no quotes, no explanations\n\n"
            f"Text: {text}"
        )
        completion = groq_client.chat.completions.create(
            model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=600,
        )
        result = completion.choices[0].message.content.strip().strip('"\'\n ')
        return result or None
    except Exception as e:
        logger.warning(f"Groq translate failed: {e}")
        return None

def translate_with_google(text: str) -> Optional[str]:
    if not translator:
        return None
    protected_text, protected = protect_special_terms(text[:4500])
    try:
        result = translator.translate(protected_text)
    except Exception as e:
        logger.warning(f"Google translate failed: {e}")
        return None
    if not result:
        return None
    result = html.unescape(result)
    result = restore_special_terms(result, protected)
    result = postprocess_persian_translation(result)
    return result or None

def translate_fa(text: str) -> Optional[str]:
    if not TRANSLATE_FA:
        return None
    cleaned = normalize_tweet_text(text)
    if not cleaned:
        return None
    if persian_ratio(cleaned) > 0.55:
        return None
    if cleaned in translate_cache:
        return translate_cache[cleaned]
    result = None
    if groq_client and TRANSLATE_ENGINE in ("auto", "groq", "groq-ai"):
        result = translate_with_groq(cleaned)
    if not result and translator:
        result = translate_with_google(cleaned)
    if not result or result.strip() == cleaned.strip():
        return None
    if len(translate_cache) >= TRANSLATE_CACHE_MAX:
        translate_cache.pop(next(iter(translate_cache)), None)
    translate_cache[cleaned] = result
    return result

def get_translate_status() -> str:
    if not TRANSLATE_FA:
        return "خاموش ❌"
    if groq_client and TRANSLATE_ENGINE in ("auto", "groq", "groq-ai"):
        return "AI Groq ✅"
    if translator:
        return "Google بهترشده ✅"
    return "خاموش ❌"

def get_translate_data_engine() -> str:
    if not TRANSLATE_FA:
        return "off"
    if groq_client and TRANSLATE_ENGINE in ("auto", "groq", "groq-ai"):
        return "Groq AI"
    if translator:
        return "Google improved"
    return "off"

# =============================
# Storage
# =============================
def default_filters() -> Dict[str, Any]:
    return {"global": {"filter_rt": True, "filter_replies": True}, "chats": {}}

def load_json(path: str, default: Any) -> Any:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load {path}: {e}")
            return deepcopy(default)
    return deepcopy(default)

def save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def clean_username(raw: str) -> str:
    raw = (raw or "").strip()
    raw = raw.replace("https://", "").replace("http://", "")
    raw = raw.replace("www.", "").replace("mobile.", "")
    raw = raw.lstrip("@")
    for domain in ("x.com/", "twitter.com/", "nitter.net/", "nitter.poast.org/", "xcancel.com/"):
        if domain in raw.lower():
            raw = raw.lower().split(domain, 1)[-1]
            break
    raw = raw.split("?")[0].split("#")[0].split("/")[0]
    return raw.lower().strip()

def valid_username(username: str) -> bool:
    return bool(re.match(r"^[a-z0-9_]{1,15}$", username or ""))

def to_chat_id(value: Any) -> Any:
    try:
        return int(value)
    except Exception:
        return value

def same_chat_id(a: Any, b: Any) -> bool:
    return str(a) == str(b)

tracked: Dict[str, Dict[str, Any]] = load_json(DATA_FILE, {})
filters_db: Dict[str, Any] = load_json(FILTERS_FILE, default_filters())

def normalize_filters_db() -> None:
    filters_db.setdefault("global", {})
    filters_db["global"].setdefault("filter_rt", True)
    filters_db["global"].setdefault("filter_replies", True)
    filters_db.setdefault("chats", {})
    for chat_id, cf in list(filters_db.get("chats", {}).items()):
        if not isinstance(cf, dict):
            filters_db["chats"][chat_id] = {}
            cf = filters_db["chats"][chat_id]
        cf.setdefault("keywords", [])
        cf.setdefault("alert_keywords", [])
        cf.setdefault("filter_rt", filters_db["global"].get("filter_rt", True))
        cf.setdefault("filter_replies", filters_db["global"].get("filter_replies", True))

def normalize_tracked_db() -> None:
    normalized: Dict[str, Dict[str, Any]] = {}
    for username, info in list(tracked.items()):
        clean = clean_username(username)
        if not valid_username(clean):
            continue
        if not isinstance(info, dict):
            info = {}
        chats: List[Any] = []
        for chat in info.get("chats", []):
            chat = to_chat_id(chat)
            if not any(same_chat_id(chat, old) for old in chats):
                chats.append(chat)
        if clean not in normalized:
            normalized[clean] = {"last_id": str(info.get("last_id", "")), "chats": chats}
        else:
            for chat in chats:
                if not any(same_chat_id(chat, old) for old in normalized[clean]["chats"]):
                    normalized[clean]["chats"].append(chat)
            if info.get("last_id"):
                normalized[clean]["last_id"] = str(info.get("last_id"))
    tracked.clear()
    tracked.update(normalized)

normalize_filters_db()
normalize_tracked_db()

# بارگذاری dedup بعد از normalize
_init_dedup()

def save_tracked() -> None:
    save_json(DATA_FILE, tracked)

def save_filters() -> None:
    save_json(FILTERS_FILE, filters_db)

def get_chat_filters(chat_id: Any) -> Dict[str, Any]:
    chat_key = str(chat_id)
    normalize_filters_db()
    if chat_key not in filters_db["chats"]:
        filters_db["chats"][chat_key] = {
            "keywords": [],
            "alert_keywords": [],
            "filter_rt": filters_db["global"].get("filter_rt", True),
            "filter_replies": filters_db["global"].get("filter_replies", True),
        }
    cf = filters_db["chats"][chat_key]
    cf.setdefault("keywords", [])
    cf.setdefault("alert_keywords", [])
    cf.setdefault("filter_rt", filters_db["global"].get("filter_rt", True))
    cf.setdefault("filter_replies", filters_db["global"].get("filter_replies", True))
    return cf

def chat_has_username(chat_id: Any, username: str) -> bool:
    return username in tracked and any(same_chat_id(chat_id, c) for c in tracked[username].get("chats", []))

def add_chat_to_username(chat_id: Any, username: str, last_id: str) -> None:
    if username not in tracked:
        tracked[username] = {"last_id": str(last_id), "chats": []}
    if not tracked[username].get("last_id"):
        tracked[username]["last_id"] = str(last_id)
    if not any(same_chat_id(chat_id, c) for c in tracked[username].get("chats", [])):
        tracked[username].setdefault("chats", []).append(to_chat_id(chat_id))

def remove_chat_from_username(chat_id: Any, username: str) -> bool:
    if username not in tracked:
        return False
    old_chats = tracked[username].get("chats", [])
    new_chats = [c for c in old_chats if not same_chat_id(chat_id, c)]
    if len(new_chats) == len(old_chats):
        return False
    tracked[username]["chats"] = new_chats
    if not new_chats:
        del tracked[username]
    return True

# =============================
# RSS / Tweet helpers
# =============================
def get_rss_feed(username: str) -> Optional[Any]:
    username = clean_username(username)
    if not valid_username(username):
        return None
    for template in RSS_SOURCES:
        url = template.format(username=username)
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
            if not feed.entries:
                continue
            first_title = (feed.entries[0].get("title", "") or "").lower()
            if any(x in first_title for x in ("whitelist", "rss reader", "not yet")):
                continue
            return feed
        except Exception as e:
            logger.warning(f"Failed RSS source {url}: {e}")
            continue
    return None

async def fetch_rss_feed(username: str) -> Optional[Any]:
    return await asyncio.to_thread(get_rss_feed, username)

def extract_tweet_id(entry: Any) -> str:
    link = entry.get("link", "") or ""
    m = re.search(r"/status(?:es)?/(\d+)", link)
    if m:
        return m.group(1)
    return str(entry.get("id", link) or "")

def normalize_x_link(link: str, username: str, tweet_id: str) -> str:
    link = html.unescape(link or "")
    if tweet_id and re.match(r"^\d+$", str(tweet_id)):
        return f"https://x.com/{username}/status/{tweet_id}"
    replacements = {
        "https://twitter.com": "https://x.com",
        "http://twitter.com": "https://x.com",
        "https://nitter.poast.org": "https://x.com",
        "http://nitter.poast.org": "https://x.com",
        "https://nitter.net": "https://x.com",
        "http://nitter.net": "https://x.com",
        "https://xcancel.com": "https://x.com",
        "http://xcancel.com": "https://x.com",
    }
    for old, new in replacements.items():
        link = link.replace(old, new)
    if not link.startswith("http"):
        link = f"https://x.com/{username}"
    return link

def is_retweet(text: str) -> bool:
    t = normalize_tweet_text(text)
    return t.startswith("RT @") or t.startswith("RT ")

def is_reply(text: str, username: str) -> bool:
    t = normalize_tweet_text(text)
    if t.startswith("@"):
        first_word = t.split()[0].lower().lstrip("@")
        return first_word != username.lower()
    return False

def should_send(chat_id: Any, username: str, text: str) -> Tuple[bool, str, bool]:
    cf = get_chat_filters(chat_id)
    low = normalize_tweet_text(text).lower()
    alert_keywords = cf.get("alert_keywords", [])
    is_alert = any(str(k).lower() in low for k in alert_keywords) if alert_keywords else False
    if is_alert:
        return True, "alert", True
    if cf.get("filter_rt", True) and is_retweet(text):
        return False, "retweet", False
    if cf.get("filter_replies", True) and is_reply(text, username):
        return False, "reply", False
    keywords = cf.get("keywords", [])
    if keywords and not any(str(k).lower() in low for k in keywords):
        return False, "keyword", False
    return True, "", False

def extract_image_url(entry: Any) -> Optional[str]:
    link = entry.get("link", "") or ""
    for enclosure in entry.get("enclosures", []) or []:
        href = enclosure.get("href", "") or ""
        enc_type = enclosure.get("type", "") or ""
        if href and ("image" in enc_type.lower() or re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", href, re.I)):
            return html.unescape(urljoin(link, href))
    description = html.unescape(entry.get("description", "") or "")
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", description, re.IGNORECASE)
    if m:
        return html.unescape(urljoin(link, m.group(1)))
    return None

def trim_raw(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"

def build_tweet_message(username: str, title: str, fa_text: Optional[str], is_alert: bool) -> str:
    title_for_send = trim_raw(title, 2200)
    if is_alert:
        text = f"🚨🚨 <b>ALERT</b> 🚨🚨\n🐦 @{html.escape(username)}\n\n{html.escape(title_for_send)}"
    else:
        text = f"🐦 <b>@{html.escape(username)}</b>\n\n{html.escape(title_for_send)}"
    if fa_text and fa_text.strip() != title.strip():
        fa_for_send = trim_raw(fa_text, 1600)
        text += f"\n\n━━━━━━━\n🦁☀️ <b>ترجمه فارسی:</b>\n{html.escape(fa_for_send)}"
    if len(text) > 4096:
        text = text[:4000].rstrip() + "\n…"
    return text

async def send_tweet_entry(chat_id: Any, username: str, entry: Any, bot: Any) -> Tuple[bool, str]:
    title = normalize_tweet_text(entry.get("title", "") or "")
    if re.match(rf"^{re.escape(username)}\s*:\s*", title, re.IGNORECASE):
        title = re.sub(rf"^{re.escape(username)}\s*:\s*", "", title, flags=re.IGNORECASE)

    tweet_id = extract_tweet_id(entry)

    # ── بررسی dedup قبل از هر کار دیگه‌ای ──
    if is_already_sent(chat_id, tweet_id):
        logger.debug(f"Dedup skip: {username}/{tweet_id} → chat {chat_id}")
        return False, "duplicate"

    ok, reason, is_alert = should_send(chat_id, username, title)
    if not ok:
        return False, reason

    link = normalize_x_link(entry.get("link", "") or "", username, tweet_id)
    fa_text = await asyncio.to_thread(translate_fa, title) if TRANSLATE_FA else None
    text = build_tweet_message(username, title, fa_text, is_alert)

    keyboard: List[List[InlineKeyboardButton]] = [[InlineKeyboardButton("🔗 مشاهده در X", url=link)]]
    if tweet_id and re.match(r"^\d+$", str(tweet_id)):
        keyboard.append([
            InlineKeyboardButton("❤️ Like", url=f"https://x.com/intent/like?tweet_id={tweet_id}"),
            InlineKeyboardButton("🔁 RT", url=f"https://x.com/intent/retweet?tweet_id={tweet_id}"),
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    image_url = extract_image_url(entry)
    disable_notification = not is_alert
    sent_msg = None

    try:
        if image_url and image_url.startswith("http") and len(text) <= 1024:
            sent_msg = await bot.send_photo(
                chat_id=chat_id, photo=image_url, caption=text,
                parse_mode=ParseMode.HTML, reply_markup=reply_markup,
                disable_notification=disable_notification,
            )
        elif image_url and image_url.startswith("http"):
            try:
                await bot.send_photo(chat_id=chat_id, photo=image_url, disable_notification=disable_notification)
            except Exception as e:
                logger.warning(f"Photo send skipped: {e}")
            sent_msg = await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
                disable_web_page_preview=False, reply_markup=reply_markup,
                disable_notification=disable_notification,
            )
        else:
            sent_msg = await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
                disable_web_page_preview=False, reply_markup=reply_markup,
                disable_notification=disable_notification,
            )
    except Exception as e:
        logger.error(f"Send failed: {e}")
        try:
            sent_msg = await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
                reply_markup=reply_markup, disable_notification=disable_notification,
            )
        except Exception as e2:
            logger.error(f"Send failed 2: {e2}")
            return False, "error"

    # ── ثبت dedup فقط بعد از ارسال موفق ──
    mark_as_sent(chat_id, tweet_id)

    if is_alert and sent_msg:
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id, disable_notification=True)
        except Exception:
            pass

    return True, "alert" if is_alert else "sent"

# =============================
# Background checker
# =============================
async def check_twitter_updates(app: Application) -> None:
    while True:
        if not tracked:
            await asyncio.sleep(CHECK_INTERVAL)
            continue
        for username, info in list(tracked.items()):
            try:
                feed = await fetch_rss_feed(username)
                if not feed or not feed.entries:
                    continue

                last_id = str(info.get("last_id", ""))
                new_entries = []
                found_last_id = False

                for entry in feed.entries:
                    tweet_id = extract_tweet_id(entry)
                    if last_id and tweet_id == last_id:
                        found_last_id = True
                        break
                    new_entries.append(entry)

                if last_id and not found_last_id and len(new_entries) > MAX_BACKFILL_ON_MISSING_LAST_ID:
                    new_entries = new_entries[:MAX_BACKFILL_ON_MISSING_LAST_ID]

                latest_sent_id = None
                for entry in reversed(new_entries):
                    tweet_id = extract_tweet_id(entry)
                    any_sent = False
                    for chat_id in list(info.get("chats", [])):
                        sent, reason = await send_tweet_entry(chat_id, username, entry, app.bot)
                        if sent:
                            any_sent = True
                        await asyncio.sleep(0.3)
                    # last_id فقط وقتی حداقل یه چت موفق بوده یا dedup گرفته بهش رسیدیم
                    if any_sent or reason == "duplicate":
                        latest_sent_id = tweet_id

                if latest_sent_id and username in tracked:
                    tracked[username]["last_id"] = str(latest_sent_id)
                    save_tracked()

                await asyncio.sleep(1.5)
            except Exception as e:
                logger.error(f"Check failed for {username}: {e}")

        await asyncio.sleep(CHECK_INTERVAL)

# =============================
# Commands
# =============================
def is_admin_chat(chat_id: Any) -> bool:
    if not ADMIN_CHAT_ID:
        return True
    return str(chat_id) == str(ADMIN_CHAT_ID)

def parse_on_off(value: str) -> Optional[bool]:
    value = (value or "").lower().strip()
    if value in ("on", "1", "true", "yes", "enable", "enabled", "روشن", "فعال"):
        return True
    if value in ("off", "0", "false", "no", "disable", "disabled", "خاموش", "غیرفعال"):
        return False
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    tr_status = get_translate_status()
    cf = get_chat_filters(chat_id)
    alert_count = len(cf.get("alert_keywords", []))
    accounts_count = len([u for u, info in tracked.items() if any(same_chat_id(chat_id, c) for c in info.get("chats", []))])
    await update.message.reply_text(
        f"🤖 TweetBaan v5.4\n"
        f"ترجمه: {tr_status}\n"
        f"اکانت‌های شما: {accounts_count}\n"
        f"فیلتر RT: {'✅' if cf.get('filter_rt', True) else '❌'} | ریپلای: {'✅' if cf.get('filter_replies', True) else '❌'}\n"
        f"آلارم: {alert_count} کلمه\n\n"
        "➕ /add user1 user2 ... - اضافه دسته‌جمعی\n"
        "/list - لیست اکانت‌ها\n"
        "/remove user - حذف اکانت\n"
        "/check [user] [count] - چک دستی\n"
        "/translate - ترجمه on/off\n"
        "/filter rt on/off\n"
        "/filter replies on/off\n"
        "/keywords add ...\n"
        "/alert add ...\n"
        "/export - بکاپ گرفتن\n"
        "/stats - آمار"
    )

async def toggle_translate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global TRANSLATE_FA
    if not update.message:
        return
    TRANSLATE_FA = not TRANSLATE_FA
    if TRANSLATE_FA and not (translator or groq_client):
        init_translator()
    await update.message.reply_text(f"ترجمه: {get_translate_status()}")

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    cf = get_chat_filters(chat_id)
    if not context.args:
        await update.message.reply_text(
            f"RT filter: {'ON' if cf.get('filter_rt', True) else 'OFF'}\n"
            f"Replies filter: {'ON' if cf.get('filter_replies', True) else 'OFF'}\n\n"
            "/filter rt on\n/filter rt off\n/filter replies on\n/filter replies off"
        )
        return
    if context.args[0].lower() == "status":
        await update.message.reply_text(
            f"RT: {'ON' if cf.get('filter_rt', True) else 'OFF'}\n"
            f"Replies: {'ON' if cf.get('filter_replies', True) else 'OFF'}"
        )
        return
    if len(context.args) >= 2:
        what, val = context.args[0].lower(), context.args[1].lower()
        on = parse_on_off(val)
        if on is None:
            await update.message.reply_text("مقدار باید on یا off باشد.")
            return
        if what in ("rt", "retweet", "retweets"):
            cf["filter_rt"] = on
            save_filters()
            await update.message.reply_text(f"فیلتر ریتوییت {'فعال' if on else 'خاموش'} شد")
            return
        if what in ("replies", "reply"):
            cf["filter_replies"] = on
            save_filters()
            await update.message.reply_text(f"فیلتر ریپلای {'فعال' if on else 'خاموش'} شد")
            return
    await update.message.reply_text("دستور درست: /filter rt on یا /filter replies off")

async def cmd_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    cf = get_chat_filters(chat_id)
    if not context.args:
        kws = cf.get("keywords", [])
        await update.message.reply_text(
            "کلمات کلیدی: " + (", ".join(kws) if kws else "هیچی - همه ارسال میشه")
            + "\n\n/keywords add airdrop listing\n/keywords list\n/keywords clear"
        )
        return
    cmd = context.args[0].lower()
    if cmd == "clear":
        cf["keywords"] = []
        save_filters()
        await update.message.reply_text("✅ کلمات کلیدی پاک شد")
        return
    if cmd == "list":
        kws = cf.get("keywords", [])
        await update.message.reply_text("کلمات: " + (", ".join(kws) if kws else "هیچی"))
        return
    new_kws = [k.lower().strip() for k in (context.args[1:] if cmd == "add" else context.args)]
    kws = cf.get("keywords", [])
    for k in new_kws:
        if k and k not in kws and k != "add":
            kws.append(k)
    cf["keywords"] = kws
    save_filters()
    await update.message.reply_text("✅ کلمات کلیدی: " + (", ".join(kws) if kws else "هیچی"))

async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    cf = get_chat_filters(chat_id)
    if not context.args:
        kws = cf.get("alert_keywords", [])
        await update.message.reply_text(
            "🚨 آلارم‌ها: " + (", ".join(kws) if kws else "هیچی")
            + "\n\n/alert add airdrop listing\n/alert list\n/alert clear"
        )
        return
    cmd = context.args[0].lower()
    if cmd == "clear":
        cf["alert_keywords"] = []
        save_filters()
        await update.message.reply_text("✅ آلارم‌ها پاک شد")
        return
    if cmd == "list":
        kws = cf.get("alert_keywords", [])
        await update.message.reply_text("آلارم‌ها: " + (", ".join(kws) if kws else "هیچی"))
        return
    new_kws = [k.lower().strip() for k in (context.args[1:] if cmd == "add" else context.args) if k.lower() != "add"]
    kws = cf.get("alert_keywords", [])
    for k in new_kws:
        if k and k not in kws:
            kws.append(k)
    cf["alert_keywords"] = kws
    save_filters()
    await update.message.reply_text("🚨 آلارم فعال: " + (", ".join(kws) if kws else "هیچی"))

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    raw_text = ""
    if update.message.text:
        parts = update.message.text.split(None, 1)
        if len(parts) > 1:
            raw_text = parts[1]
    if not raw_text and context.args:
        raw_text = " ".join(context.args)
    if not raw_text.strip():
        await update.message.reply_text("استفاده:\n/add user1 user2 user3\nیا هر کدوم تو یه خط")
        return
    raw_usernames = re.split(r"[,\s\n\r\t]+", raw_text)
    usernames: List[str] = []
    seen = set()
    for item in raw_usernames:
        username = clean_username(item)
        if username and username not in seen and valid_username(username):
            seen.add(username)
            usernames.append(username)
    if not usernames:
        await update.message.reply_text("یوزرنیم معتبری پیدا نکردم.")
        return
    if len(usernames) == 1:
        username = usernames[0]
        msg = await update.message.reply_text(f"در حال بررسی @{username} ...")
        feed = await fetch_rss_feed(username)
        if not feed or not feed.entries:
            await msg.edit_text(f"❌ @{username} پیدا نشد یا RSS در دسترس نیست.")
            return
        last_id = extract_tweet_id(feed.entries[0])
        already = chat_has_username(chat_id, username)
        add_chat_to_username(chat_id, username, last_id)
        # توییت‌های فعلی رو به dedup اضافه کن تا بعد از add ارسال نشن
        for entry in feed.entries[:20]:
            tid = extract_tweet_id(entry)
            if tid:
                mark_as_sent(chat_id, tid)
        save_tracked()
        await msg.edit_text(f"{'ℹ️' if already else '✅'} @{username} {'از قبل وجود داشت' if already else 'اضافه شد!'}")
        return
    status_msg = await update.message.reply_text(f"📥 در حال اضافه کردن {len(usernames)} اکانت...\n0/{len(usernames)}")
    added: List[str] = []
    failed: List[str] = []
    existed: List[str] = []
    for i, username in enumerate(usernames, 1):
        try:
            if chat_has_username(chat_id, username):
                existed.append(username)
            else:
                feed = await fetch_rss_feed(username)
                if feed and feed.entries:
                    last_id = extract_tweet_id(feed.entries[0])
                    add_chat_to_username(chat_id, username, last_id)
                    # توییت‌های فعلی رو به dedup اضافه کن
                    for entry in feed.entries[:20]:
                        tid = extract_tweet_id(entry)
                        if tid:
                            mark_as_sent(chat_id, tid)
                    save_tracked()
                    added.append(username)
                else:
                    failed.append(username)
        except Exception as e:
            logger.warning(f"Add user failed for {username}: {e}")
            failed.append(username)
        if i % 5 == 0 or i == len(usernames):
            try:
                await status_msg.edit_text(f"📥 {i}/{len(usernames)} | ✅ {len(added)} | ℹ️ {len(existed)} | ❌ {len(failed)}")
            except Exception:
                pass
        await asyncio.sleep(0.8)
    report = f"✅ تمام شد!\nاضافه شد: {len(added)}\nتکراری: {len(existed)}\nناموفق: {len(failed)}"
    if failed:
        report += "\nناموفق: " + ", ".join([f"@{u}" for u in failed[:10]])
    await status_msg.edit_text(report)

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    raw_text = ""
    if update.message.text:
        parts = update.message.text.split(None, 1)
        if len(parts) > 1:
            raw_text = parts[1]
    if not raw_text and context.args:
        raw_text = " ".join(context.args)
    if not raw_text.strip():
        await update.message.reply_text("استفاده: /remove username\nیا چندتایی: /remove user1 user2")
        return
    raw_usernames = re.split(r"[,\s\n\r\t]+", raw_text)
    usernames: List[str] = []
    seen = set()
    for item in raw_usernames:
        username = clean_username(item)
        if username and username not in seen:
            seen.add(username)
            usernames.append(username)
    removed: List[str] = []
    not_found: List[str] = []
    for username in usernames:
        if remove_chat_from_username(chat_id, username):
            removed.append(username)
        else:
            not_found.append(username)
    save_tracked()
    msg = f"✅ حذف شد: {len(removed)}"
    if removed:
        msg += "\n" + ", ".join([f"@{u}" for u in removed])
    if not_found:
        msg += f"\nپیدا نشد: {len(not_found)}"
    await update.message.reply_text(msg)

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    my_accounts = [u for u, info in tracked.items() if any(same_chat_id(chat_id, c) for c in info.get("chats", []))]
    if not my_accounts:
        await update.message.reply_text("هیچ اکانتی نداری. برای شروع: /add username")
        return
    text = f"📋 {len(my_accounts)} اکانت:\n" + "\n".join([f"• @{u}" for u in sorted(my_accounts)])
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await update.message.reply_text(text)

async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    args = context.args.copy() if context.args else []
    count = 1
    if args and args[-1].isdigit():
        count = int(args.pop())
        count = max(1, min(count, 5))
    if args:
        usernames = [clean_username(arg) for arg in args if valid_username(clean_username(arg))]
    else:
        usernames = [u for u, info in tracked.items() if any(same_chat_id(chat_id, c) for c in info.get("chats", []))]
    if not usernames:
        await update.message.reply_text("هیچ اکانتی نداری. /add")
        return
    await update.message.reply_text(f"🔍 {len(usernames)} اکانت، {count} توییت...")
    sent = 0
    filtered_count = 0
    for username in usernames:
        feed = await fetch_rss_feed(username)
        if not feed or not feed.entries:
            continue
        for entry in reversed(feed.entries[:count]):
            ok, reason = await send_tweet_entry(chat_id, username, entry, context.bot)
            if ok:
                sent += 1
            else:
                filtered_count += 1
            await asyncio.sleep(0.4)
    msg = f"✅ تمام شد. ارسال: {sent}"
    if filtered_count:
        msg += f" | فیلتر/تکراری: {filtered_count}"
    await update.message.reply_text(msg)

# =============================
# Backup / Restore
# =============================
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        await update.message.reply_text("⛔️ فقط ادمین اصلی اجازه گرفتن بکاپ کامل را دارد.")
        return
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id, document=f, filename="tracked_users.json",
                    caption=f"📦 Backup tracked_users\nAccounts: {len(tracked)}",
                )
        if os.path.exists(FILTERS_FILE):
            with open(FILTERS_FILE, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id, document=f, filename="filters.json",
                    caption=f"📦 Backup filters\nChats: {len(filters_db.get('chats', {}))}",
                )
        if os.path.exists(SENT_IDS_FILE):
            with open(SENT_IDS_FILE, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id, document=f, filename="sent_ids.json",
                    caption="📦 Backup sent_ids (dedup)",
                )
        await update.message.reply_text("✅ بکاپ ارسال شد.")
    except Exception as e:
        await update.message.reply_text(f"خطا در export: {e}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global tracked, filters_db
    if not update.effective_chat or not update.message or not update.message.document:
        return
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        await update.message.reply_text("⛔️ فقط ادمین اصلی اجازه import دیتابیس را دارد.")
        return
    doc = update.message.document
    filename = doc.file_name or ""
    if filename not in ("tracked_users.json", "filters.json", "sent_ids.json"):
        await update.message.reply_text("فقط فایل‌های tracked_users.json، filters.json و sent_ids.json قبول میشه.")
        return
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        tmp_path = os.path.join(DATA_DIR, f".{filename}.tmp")
        dest_path = os.path.join(DATA_DIR, filename)
        await tg_file.download_to_drive(tmp_path)
        with open(tmp_path, "r", encoding="utf-8") as f:
            new_data = json.load(f)
        os.replace(tmp_path, dest_path)
        if filename == "tracked_users.json":
            tracked.clear()
            tracked.update(new_data if isinstance(new_data, dict) else {})
            normalize_tracked_db()
            save_tracked()
            await update.message.reply_text(f"✅ tracked_users.json import شد\n{len(tracked)} اکانت لود شد")
        elif filename == "filters.json":
            filters_db.clear()
            filters_db.update(new_data if isinstance(new_data, dict) else default_filters())
            normalize_filters_db()
            save_filters()
            await update.message.reply_text(f"✅ filters.json import شد")
        elif filename == "sent_ids.json":
            _dedup_ram.clear()
            for key, ids in (new_data if isinstance(new_data, dict) else {}).items():
                if isinstance(ids, list):
                    _dedup_ram[key] = set(ids[-DEDUP_MAX_PER_CHAT:])
            _flush_dedup_to_file()
            await update.message.reply_text(f"✅ sent_ids.json import شد\n{sum(len(v) for v in _dedup_ram.values())} ID لود شد")
    except Exception as e:
        await update.message.reply_text(f"خطا در import: {e}")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    total_accounts = len(tracked)
    total_chats = len({str(chat) for info in tracked.values() for chat in info.get("chats", [])})
    my_accounts = len([u for u, info in tracked.items() if any(same_chat_id(chat_id, c) for c in info.get("chats", []))])
    cf = get_chat_filters(chat_id)
    dedup_count = len(_dedup_ram.get(_dedup_key(chat_id), set()))
    await update.message.reply_text(
        f"📊 آمار TweetBaan\n\n"
        f"کل اکانت‌های توییتر در سیستم: {total_accounts}\n"
        f"کل چت‌های فعال: {total_chats}\n"
        f"اکانت‌های شما: {my_accounts}\n"
        f"کلمات کلیدی شما: {len(cf.get('keywords', []))}\n"
        f"آلارم‌های شما: {len(cf.get('alert_keywords', []))}\n"
        f"فیلتر RT شما: {'ON' if cf.get('filter_rt', True) else 'OFF'}\n"
        f"فیلتر Reply شما: {'ON' if cf.get('filter_replies', True) else 'OFF'}\n"
        f"موتور ترجمه: {get_translate_data_engine()}\n"
        f"ID‌های dedup شما: {dedup_count}\n"
        f"دیتا: {DATA_DIR}"
    )

# =============================
# Bot setup
# =============================
BOT_COMMANDS = [
    BotCommand("add", "➕ اضافه کردن اکانت"),
    BotCommand("remove", "➖ حذف اکانت"),
    BotCommand("list", "📋 لیست اکانت‌ها"),
    BotCommand("check", "🔍 چک دستی"),
    BotCommand("alert", "🚨 آلارم کلمات طلایی"),
    BotCommand("keywords", "🔑 فیلتر کلمات"),
    BotCommand("filter", "⚙️ فیلتر RT/Reply"),
    BotCommand("translate", "🌐 ترجمه on/off"),
    BotCommand("export", "💾 بکاپ گرفتن"),
    BotCommand("stats", "📊 آمار"),
    BotCommand("start", "❓ راهنما"),
]

async def post_init(application: Application) -> None:
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        logger.warning(f"set_my_commands failed: {e}")
    asyncio.create_task(check_twitter_updates(application))

def main() -> None:
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN رو در فایل .env قرار بده")
        return
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("remove", remove_user))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("check", check_now))
    app.add_handler(CommandHandler("translate", toggle_translate))
    app.add_handler(CommandHandler("filter", cmd_filter))
    app.add_handler(CommandHandler("keywords", cmd_keywords))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    logger.info(f"Bot running... translate={get_translate_status()} rsshub={RSS_HUB_URL} data_dir={DATA_DIR}")
    print(f"Bot running... translate={get_translate_status()} rsshub={RSS_HUB_URL} data_dir={DATA_DIR}")
    app.run_polling()

if __name__ == "__main__":
    main()
