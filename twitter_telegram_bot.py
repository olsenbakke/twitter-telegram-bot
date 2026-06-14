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
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode

load_dotenv()

# =============================
# Config
# =============================
TOKEN         = os.getenv("TELEGRAM_BOT_TOKEN")
DATA_DIR      = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

# پردازش لیست ادمین‌ها (پشتیبانی از چند ادمین با کاما)
ADMIN_IDS = [x.strip() for x in os.getenv("ADMIN_CHAT_ID", "").split(",") if x.strip()]

DATA_FILE     = os.path.join(DATA_DIR, "tracked_users.json")
FILTERS_FILE  = os.path.join(DATA_DIR, "filters.json")
SENT_IDS_FILE = os.path.join(DATA_DIR, "sent_ids.json")

CHECK_INTERVAL                  = int(os.getenv("CHECK_INTERVAL", "90"))
HTTP_TIMEOUT                    = int(os.getenv("HTTP_TIMEOUT", "20"))
MAX_BACKFILL_ON_MISSING_LAST_ID = int(os.getenv("MAX_BACKFILL_ON_MISSING_LAST_ID", "5"))
TRANSLATE_FA                    = os.getenv("TRANSLATE_FA", "true").lower() in ("1", "true", "yes", "on")
TRANSLATE_ENGINE                = os.getenv("TRANSLATE_ENGINE", "google").lower().strip()
TRANSLATE_CACHE_MAX             = int(os.getenv("TRANSLATE_CACHE_MAX", "1500"))
DEDUP_MAX_PER_CHAT              = int(os.getenv("DEDUP_MAX_PER_CHAT", "2000"))
DEDUP_FILE_MAX_PER_KEY          = int(os.getenv("DEDUP_FILE_MAX_PER_KEY", "500"))
FOLD_THRESHOLD                  = int(os.getenv("FOLD_THRESHOLD", "280"))
BACKUP_INTERVAL                 = int(os.getenv("BACKUP_INTERVAL", "21600"))  # هر ۶ ساعت

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

socket.setdefaulttimeout(HTTP_TIMEOUT)
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# =============================
# DEDUP — دولایه (RAM + فایل بهینه شده)
# =============================
_dedup_ram: Dict[str, Set[str]] = {}

def _dedup_key(chat_id: Any) -> str:
    return str(chat_id)

def _load_sent_ids() -> Dict[str, List[str]]:
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

def _init_dedup() -> None:
    data = _load_sent_ids()
    for key, ids in data.items():
        if isinstance(ids, list):
            _dedup_ram[key] = set(ids[-DEDUP_MAX_PER_CHAT:])
    total = sum(len(v) for v in _dedup_ram.values())
    logger.info(f"Dedup init: {total} IDs for {len(_dedup_ram)} chats")

def is_already_sent(chat_id: Any, tweet_id: str) -> bool:
    if not tweet_id or not re.match(r"^\d+$", str(tweet_id)):
        return False
    return tweet_id in _dedup_ram.get(_dedup_key(chat_id), set())

def mark_as_sent(chat_id: Any, tweet_id: str) -> None:
    if not tweet_id or not re.match(r"^\d+$", str(tweet_id)):
        return
    key = _dedup_key(chat_id)
    _dedup_ram.setdefault(key, set()).add(tweet_id)

def _flush_dedup_to_file() -> None:
    data: Dict[str, List[str]] = {}
    for key, ids in _dedup_ram.items():
        lst = list(ids)
        data[key] = lst[-DEDUP_FILE_MAX_PER_KEY:] if len(lst) > DEDUP_FILE_MAX_PER_KEY else lst
    _save_sent_ids_file(data)

# =============================
# Translation
# =============================
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "").strip()
translator          = None
groq_client         = None
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
_crypto_terms_pattern = "|".join(re.escape(t) for t in sorted(CRYPTO_TERMS, key=len, reverse=True))
PROTECTED_RE = re.compile(
    r"https?://[^\s<>()]+|www\.[^\s<>()]+|@\w+|#[A-Za-z0-9_\u0600-\u06FF]+|\$[A-Za-z][A-Za-z0-9_]*"
    r"|\b(?:" + _crypto_terms_pattern + r")\b",
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
            logger.info("Translator: Google enabled")
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
    return len(re.findall(r"[\u0600-\u06FF]", text or "")) / len(letters)

def protect_special_terms(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    protected: List[Tuple[str, str]] = []
    def repl(m: re.Match) -> str:
        tok = f"XTBKEEP{len(protected):03d}X"
        protected.append((tok, m.group(0)))
        return tok
    return PROTECTED_RE.sub(repl, text), protected

def restore_special_terms(text: str, protected: List[Tuple[str, str]]) -> str:
    for tok, val in protected:
        text = re.sub(re.escape(tok), val, text, flags=re.IGNORECASE)
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
        "ادعا کنید": "claim کنید", "مطالب کنید": "claim کنید",
        "راه اندازی": "لانچ", "راه‌اندازی": "لانچ",
        "صعودی": "bullish", "نزولی": "bearish",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\s+([،؛؟.!?])", r"\1", text)
    text = re.sub(r"([؟!]){3,}", r"\1\1", text)
    return re.sub(r"[ \t]+", " ", text).strip()

def translate_with_groq(text: str) -> Optional[str]:
    if not groq_client:
        return None
    try:
        # پرامپت مهندسی‌شده و سخت‌گیرانه برای لحن عامیانه و کریپتویی
        prompt = (
            "You are an expert Persian crypto influencer and telegram admin.\n"
            "Translate the following English tweet into smooth, concise, and colloquial (عامیانه/تهرانی) Persian, "
            "exactly how it's written on Iranian crypto channels.\n\n"
            
            "STRICT RULES:\n"
            "1. NEVER use formal/bookish Persian (like می باشد، است، کلمات کتابی). Use natural conversational tone (مثلا: داره، می‌شه، انجام بدین، برایِ).\n"
            "2. DO NOT translate crypto tech terms. Leave these words EXACTLY in English: "
            "Airdrop, Mainnet, Testnet, Mint, Stake, Staking, Claim, Snapshot, Node, Validator, Whitelist, Listing, "
            "Wallet, Bridge, Swap, Presale, Launchpad, Gas, L1, L2, TVL, IDO, TGE, Hodl, FOMO, FUD.\n"
            "3. Keep all @usernames, #hashtags, $tickers, and URLs exactly as they are in the original text.\n"
            "4. Output ONLY the Persian translation. No explanations, no introduction, no quotes.\n\n"
            f"Text to translate: {text}"
        )
        completion = groq_client.chat.completions.create(
            model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2, # کاهش خلاقیت برای وفاداری بیشتر به متن
            max_tokens=600,
        )
        return completion.choices[0].message.content.strip().strip('"\'\n ') or None
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
    return postprocess_persian_translation(result) or None

def translate_fa(text: str) -> Optional[str]:
    if not TRANSLATE_FA:
        return None
    cleaned = normalize_tweet_text(text)
    if not cleaned or persian_ratio(cleaned) > 0.55:
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
    if not TRANSLATE_FA: return "خاموش ❌"
    if groq_client and TRANSLATE_ENGINE in ("auto", "groq", "groq-ai"): return "AI Groq ✅"
    if translator: return "Google ✅"
    return "خاموش ❌"

def get_translate_data_engine() -> str:
    if not TRANSLATE_FA: return "off"
    if groq_client and TRANSLATE_ENGINE in ("auto", "groq", "groq-ai"): return "Groq AI"
    if translator: return "Google"
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
    raw = raw.replace("www.", "").replace("mobile.", "").lstrip("@")
    for domain in ("x.com/", "twitter.com/", "nitter.net/", "nitter.poast.org/", "xcancel.com/"):
        if domain in raw.lower():
            raw = raw.lower().split(domain, 1)[-1]
            break
    return raw.split("?")[0].split("#")[0].split("/")[0].lower().strip()

def valid_username(username: str) -> bool:
    return bool(re.match(r"^[a-z0-9_]{1,15}$", username or ""))

def to_chat_id(value: Any) -> Any:
    try: return int(value)
    except Exception: return value

def same_chat_id(a: Any, b: Any) -> bool:
    return str(a) == str(b)

tracked: Dict[str, Dict[str, Any]]  = load_json(DATA_FILE, {})
filters_db: Dict[str, Any]          = load_json(FILTERS_FILE, default_filters())

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
        cf.setdefault("filter_rt",      filters_db["global"].get("filter_rt", True))
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
_init_dedup()

def save_tracked() -> None:  save_json(DATA_FILE, tracked)
def save_filters() -> None:  save_json(FILTERS_FILE, filters_db)

def get_chat_filters(chat_id: Any) -> Dict[str, Any]:
    chat_key = str(chat_id)
    normalize_filters_db()
    if chat_key not in filters_db["chats"]:
        filters_db["chats"][chat_key] = {
            "keywords": [], "alert_keywords": [],
            "filter_rt":      filters_db["global"].get("filter_rt", True),
            "filter_replies": filters_db["global"].get("filter_replies", True),
        }
    cf = filters_db["chats"][chat_key]
    cf.setdefault("keywords", [])
    cf.setdefault("alert_keywords", [])
    cf.setdefault("filter_rt",      filters_db["global"].get("filter_rt", True))
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
    old = tracked[username].get("chats", [])
    new = [c for c in old if not same_chat_id(chat_id, c)]
    if len(new) == len(old):
        return False
    tracked[username]["chats"] = new
    if not new:
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
    return None

async def fetch_rss_feed(username: str) -> Optional[Any]:
    return await asyncio.to_thread(get_rss_feed, username)

def extract_tweet_id(entry: Any) -> str:
    link = entry.get("link", "") or ""
    m = re.search(r"/status(?:es)?/(\d+)", link)
    if m: return m.group(1)

    eid = str(entry.get("id", "") or "")
    m2 = re.search(r"(\d{15,})", eid)
    if m2: return m2.group(1)

    guid = str(entry.get("guid", "") or "")
    m3 = re.search(r"/status(?:es)?/(\d+)", guid)
    if m3: return m3.group(1)
    m4 = re.search(r"(\d{15,})", guid)
    if m4: return m4.group(1)

    desc = str(entry.get("description", "") or "")
    m5 = re.search(r"/status(?:es)?/(\d+)", desc)
    if m5: return m5.group(1)

    return ""

def normalize_x_link(link: str, username: str, tweet_id: str) -> str:
    link = html.unescape(link or "")
    if tweet_id and re.match(r"^\d+$", str(tweet_id)):
        return f"https://x.com/{username}/status/{tweet_id}"
    for old, new in {
        "https://twitter.com": "https://x.com",
        "http://twitter.com":  "https://x.com",
        "https://nitter.poast.org": "https://x.com",
        "http://nitter.poast.org":  "https://x.com",
        "https://nitter.net": "https://x.com",
        "http://nitter.net":  "https://x.com",
        "https://xcancel.com": "https://x.com",
        "http://xcancel.com":  "https://x.com",
    }.items():
        link = link.replace(old, new)
    return link if link.startswith("http") else f"https://x.com/{username}"

_RT_PATTERNS = re.compile(
    r"^RT\s+@\w+|"
    r"^RT\s*:|"
    r"^R\s+to\s+@\w+|"
    r"^Retweeted\s+@\w+|"
    r"^↩\s*@\w+|"
    r"^RE:\s*@\w+",
    re.IGNORECASE,
)

def is_retweet(text: str) -> bool:
    t = normalize_tweet_text(text)
    return bool(_RT_PATTERNS.match(t))

def is_reply(text: str, username: str) -> bool:
    t = normalize_tweet_text(text)
    if t.startswith("@"):
        first_word = t.split()[0].lower().lstrip("@")
        return first_word != username.lower()
    return False

def should_send(chat_id: Any, username: str, text: str) -> Tuple[bool, str, bool]:
    cf  = get_chat_filters(chat_id)
    low = normalize_tweet_text(text).lower()

    alert_kws = cf.get("alert_keywords", [])
    is_alert  = any(str(k).lower() in low for k in alert_kws) if alert_kws else False
    if is_alert:
        return True, "alert", True

    if cf.get("filter_rt", True) and is_retweet(text):
        return False, "retweet", False
    if cf.get("filter_replies", True) and is_reply(text, username):
        return False, "reply", False

    kws = cf.get("keywords", [])
    if kws and not any(str(k).lower() in low for k in kws):
        return False, "keyword", False

    return True, "", False

def extract_image_url(entry: Any) -> Optional[str]:
    link = entry.get("link", "") or ""
    for enc in entry.get("enclosures", []) or []:
        href     = enc.get("href", "") or ""
        enc_type = enc.get("type", "") or ""
        if href and ("image" in enc_type.lower() or re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", href, re.I)):
            return html.unescape(urljoin(link, href))
    desc = html.unescape(entry.get("description", "") or "")
    m    = re.search(r'<img[^>]+src=["\'"]([^"\']+)["\']', desc, re.IGNORECASE)
    if m:
        return html.unescape(urljoin(link, m.group(1)))
    return None

def trim_raw(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"

def pick_emoji(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("🚀", "moon", "pump", "ath", "bullish", "bull", "lfg", "green")): return "🚀"
    if any(w in t for w in ("airdrop", "free", "claim", "reward", "giveaway")): return "🎁"
    if any(w in t for w in ("listing", "listed", "list", "launch", "tge", "ido", "ieo")): return "📢"
    if any(w in t for w in ("mainnet", "testnet", "upgrade", "update", "deploy")): return "⚙️"
    if any(w in t for w in ("nft", "mint", "opensea", "blur")): return "🖼"
    if any(w in t for w in ("hack", "exploit", "scam", "rug", "warning", "alert", "beware")): return "⚠️"
    if any(w in t for w in ("partnership", "partner", "collab", "x ", " x ")): return "🤝"
    if any(w in t for w in ("bear", "dump", "sell", "short", "down", "red", "crash")): return "🔴"
    if any(w in t for w in ("staking", "yield", "apr", "apy", "farm", "liquidity")): return "💰"
    if any(w in t for w in ("vote", "governance", "dao", "proposal")): return "🗳"
    return "🐦"

_URL_RE = re.compile(r"https?://[^\s<>\"']+")

def escape_and_linkify(text: str) -> str:
    parts = []
    last  = 0
    for m in _URL_RE.finditer(text):
        parts.append(html.escape(text[last:m.start()]))
        url   = m.group(0)
        short = re.sub(r"^https?://", "", url)
        if len(short) > 30:
            short = short[:28] + "…"
        parts.append(f'<a href="{html.escape(url)}">{html.escape(short)}</a>')
        last = m.end()
    parts.append(html.escape(text[last:]))
    return "".join(parts)

def build_tweet_message(username: str, title: str, fa_text: Optional[str], is_alert: bool, image_url: Optional[str] = None) -> str:
    emoji = "🚨" if is_alert else pick_emoji(title)
    hidden_img = f'<a href="{image_url}">&#8203;</a>' if image_url else ""

    if is_alert:
        header = f"{hidden_img}🚨 <b>ALERT</b> 🚨\n{emoji} <b>@{html.escape(username)}</b>"
    else:
        header = f"{hidden_img}{emoji} <b>@{html.escape(username)}</b>"

    body_raw = trim_raw(title, 2200)

    if len(body_raw) > FOLD_THRESHOLD:
        body = f"<blockquote>{escape_and_linkify(body_raw)}</blockquote>"
    else:
        body = escape_and_linkify(body_raw)

    text = f"{header}\n\n{body}"

    if fa_text and fa_text.strip() != title.strip():
        fa_raw = trim_raw(fa_text, 1200)
        if len(fa_raw) > FOLD_THRESHOLD:
            fa_block = f"<blockquote>{html.escape(fa_raw)}</blockquote>"
        else:
            fa_block = html.escape(fa_raw)
        text += f"\n\n<b>🇮🇷 ترجمه:</b>\n{fa_block}"

    if len(text) > 4096:
        text = text[:4000].rstrip() + "\n…"
    return text

async def send_tweet_entry(chat_id: Any, username: str, entry: Any, bot: Any) -> Tuple[bool, str]:
    title = normalize_tweet_text(entry.get("title", "") or "")
    title = re.sub(rf"^{re.escape(username)}\s*:\s*", "", title, flags=re.IGNORECASE)
    tweet_id = extract_tweet_id(entry)

    if is_already_sent(chat_id, tweet_id):
        logger.debug(f"[DEDUP] skip {username}/{tweet_id} → {chat_id}")
        return False, "duplicate"

    ok, reason, is_alert = should_send(chat_id, username, title)
    if not ok:
        mark_as_sent(chat_id, tweet_id)
        return False, reason

    link      = normalize_x_link(entry.get("link", "") or "", username, tweet_id)
    fa_text   = await asyncio.to_thread(translate_fa, title) if TRANSLATE_FA else None
    image_url = extract_image_url(entry)
    
    text = build_tweet_message(username, title, fa_text, is_alert, image_url)

    keyboard      = [[InlineKeyboardButton("🔗 مشاهده در X", url=link)]]
    reply_markup  = InlineKeyboardMarkup(keyboard)
    disable_notif = not is_alert
    sent_msg      = None

    try:
        sent_msg = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=False, reply_markup=reply_markup,
            disable_notification=disable_notif,
        )
    except Exception as e:
        logger.error(f"Send failed: {e}")
        return False, "error"

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
async def auto_backup(app: Application) -> None:
    # یک بار لود اولیه متغیرهای خاموش/روشن بکاپ از محیط
    # پشتیبانی از متغیرهای رایج فعال/غیرفعال‌سازی
    enable_backup_env = os.getenv("ENABLE_BACKUP", "true").lower() in ("1", "true", "yes", "on")
    auto_backup_env = os.getenv("AUTO_BACKUP", "true").lower() in ("1", "true", "yes", "on")
    
    # اگر هرکدام از متغیرها روی حالت False تنظیم شده باشند، سیستم بکاپ خودکار کلاً خاموش می‌شود
    if not enable_backup_env or not auto_backup_env:
        logger.info("Auto-backup is completely DISABLED via environment variables.")
        return

    # اولین مکس برای اینکه ربات موقع روشن شدن اولیه بلافاصله پیام نفرستد و چت را شلوغ نکند
    await asyncio.sleep(60)
    
    while True:
        try:
            # ذخیره کردن وضعیت ددپ در فایل
            _flush_dedup_to_file()
            logger.info("Dedup database auto-flushed to file.")
            
            if ADMIN_IDS:
                caption_map = {
                    DATA_FILE:     f"📦 auto-backup tracked_users — {len(tracked)} اکانت",
                    FILTERS_FILE:  f"📦 auto-backup filters — {len(filters_db.get('chats', {}))} چت",
                    SENT_IDS_FILE: f"📦 auto-backup sent_ids — {sum(len(v) for v in _dedup_ram.values())} ID",
                }
                name_map = {
                    DATA_FILE:     "tracked_users.json",
                    FILTERS_FILE:  "filters.json",
                    SENT_IDS_FILE: "sent_ids.json",
                }
                
                # ارسال فایل‌های بکاپ تفکیک شده به تک‌تک ادمین‌های ست شده
                for aid in ADMIN_IDS:
                    for path, caption in caption_map.items():
                        if os.path.exists(path):
                            try:
                                with open(path, "rb") as f:
                                    await app.bot.send_document(
                                        chat_id=int(aid),
                                        document=f,
                                        filename=name_map[path],
                                        caption=caption,
                                    )
                            except Exception as e:
                                logger.warning(f"Auto-backup {path} failed for admin {aid}: {e}")
                logger.info(f"Auto-backup sent to all admins: {', '.join(ADMIN_IDS)}")
        except Exception as e:
            logger.error(f"Auto-backup error: {e}")
            
        # اصلاح فاحش: حالا ربات دقیقاً به اندازه BACKUP_INTERVAL ثانیه‌ای که ست کرده‌ای صبر می‌کند.
        # اگر در ریلوای متغیری نباشد، به طور پیش‌فرض هر ۲۱۶۰۰ ثانیه (۶ ساعت) انجام می‌شود.
        await asyncio.sleep(BACKUP_INTERVAL)

# =============================
# Commands & Admin Verification
# =============================
def is_admin_chat(chat_id: Any) -> bool:
    if not ADMIN_IDS: 
        return True
    return str(chat_id) in ADMIN_IDS

def parse_on_off(value: str) -> Optional[bool]:
    v = (value or "").lower().strip()
    if v in ("on",  "1", "true",  "yes", "enable",  "enabled",  "روشن", "فعال"):   return True
    if v in ("off", "0", "false", "no",  "disable", "disabled", "خاموش", "غیرفعال"): return False
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id        = update.effective_chat.id
    cf             = get_chat_filters(chat_id)
    accounts_count = len([u for u, i in tracked.items() if any(same_chat_id(chat_id, c) for c in i.get("chats", []))])
    alert_count    = len(cf.get("alert_keywords", []))
    kw_count       = len(cf.get("keywords", []))

    status_text = (
        f"🤖 <b>TweetBaan v6.0 (Multi-Admin Edition)</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📡 اکانت‌های فعال: <b>{accounts_count}</b>\n"
        f"🌐 ترجمه: <b>{get_translate_status()}</b>\n"
        f"🔁 فیلتر RT: {'✅' if cf.get('filter_rt') else '❌'}  "
        f"💬 ریپلای: {'✅' if cf.get('filter_replies') else '❌'}\n"
        f"🚨 آلارم: <b>{alert_count}</b> کلمه  "
        f"🔑 کلیدی: <b>{kw_count}</b> کلمه\n"
        f"━━━━━━━━━━━━━━━\n"
        f"یه دکمه بزن یا دستور بفرست 👇"
    )

    keyboard = [
        [
            InlineKeyboardButton("➕ اضافه کردن",    switch_inline_query_current_chat="/add "),
            InlineKeyboardButton("➖ حذف اکانت",     switch_inline_query_current_chat="/remove "),
        ],
        [
            InlineKeyboardButton("📋 لیست اکانت‌ها", callback_data="cmd_list"),
            InlineKeyboardButton("📊 آمار",           callback_data="cmd_stats"),
        ],
        [
            InlineKeyboardButton("🔍 چک همه اکانت‌ها", callback_data="cmd_check_all"),
            InlineKeyboardButton("💾 بکاپ کل سرور",     callback_data="cmd_export"),
        ],
        [
            InlineKeyboardButton("🚨 آلارم‌ها",      switch_inline_query_current_chat="/alert add "),
            InlineKeyboardButton("🔑 کلمات کلیدی",  switch_inline_query_current_chat="/keywords add "),
        ],
        [
            InlineKeyboardButton("⚙️ فیلتر RT/Reply", switch_inline_query_current_chat="/filter "),
            InlineKeyboardButton("🌐 ترجمه on/off",   callback_data="cmd_translate"),
        ],
    ]

    await update.message.reply_text(
        status_text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def toggle_translate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global TRANSLATE_FA
    TRANSLATE_FA = not TRANSLATE_FA
    if TRANSLATE_FA and not (translator or groq_client):
        init_translator()
    text = f"ترجمه: {get_translate_status()}"
    if update.message:
        await update.message.reply_text(text)
    elif update.callback_query:
        await update.callback_query.answer(text, show_alert=True)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    await query.answer()
    data = query.data or ""

    if data == "cmd_list": await list_users(update, context)
    elif data == "cmd_stats": await cmd_stats(update, context)
    elif data == "cmd_export": await cmd_export(update, context)
    elif data == "cmd_translate": await toggle_translate(update, context)
    elif data == "cmd_check_all": await check_now(update, context)

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message: return
    chat_id = update.effective_chat.id
    cf = get_chat_filters(chat_id)
    if not context.args:
        await update.message.reply_text(
            f"RT filter: {'ON' if cf.get('filter_rt') else 'OFF'}\n"
            f"Replies filter: {'ON' if cf.get('filter_replies') else 'OFF'}\n\n"
            "/filter rt on|off\n/filter replies on|off"
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
            await update.message.reply_text(f"فیلتر RT {'✅ فعال' if on else '❌ خاموش'} شد")
        elif what in ("replies", "reply"):
            cf["filter_replies"] = on
            save_filters()
            await update.message.reply_text(f"فیلتر ریپلای {'✅ فعال' if on else '❌ خاموش'} شد")
        else:
            await update.message.reply_text("دستور: /filter rt on|off  یا  /filter replies on|off")
    else:
        await update.message.reply_text("دستور: /filter rt on|off  یا  /filter replies on|off")

async def cmd_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message: return
    chat_id = update.effective_chat.id
    cf  = get_chat_filters(chat_id)
    kws = cf.get("keywords", [])
    if not context.args:
        await update.message.reply_text(
            "🔑 کلمات کلیدی: " + (", ".join(kws) if kws else "هیچی — همه ارسال میشه")
            + "\n\n/keywords add bitcoin listing\n/keywords clear"
        )
        return
    cmd = context.args[0].lower()
    if cmd == "clear":
        cf["keywords"] = []
        save_filters()
        await update.message.reply_text("✅ کلمات کلیدی پاک شد")
    elif cmd == "list":
        await update.message.reply_text("کلمات: " + (", ".join(kws) if kws else "هیچی"))
    else:
        new_kws = [k.lower().strip() for k in (context.args[1:] if cmd == "add" else context.args) if k.lower() != "add"]
        for k in new_kws:
            if k and k not in kws: kws.append(k)
        cf["keywords"] = kws
        save_filters()
        await update.message.reply_text("✅ کلمات: " + (", ".join(kws) if kws else "هیچی"))

async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message: return
    chat_id = update.effective_chat.id
    cf  = get_chat_filters(chat_id)
    kws = cf.get("alert_keywords", [])
    if not context.args:
        await update.message.reply_text(
            "🚨 آلارم‌ها: " + (", ".join(kws) if kws else "هیچی")
            + "\n\n/alert add airdrop listing\n/alert clear"
        )
        return
    cmd = context.args[0].lower()
    if cmd == "clear":
        cf["alert_keywords"] = []
        save_filters()
        await update.message.reply_text("✅ آلارم‌ها پاک شد")
    elif cmd == "list":
        await update.message.reply_text("آلارم‌ها: " + (", ".join(kws) if kws else "هیچی"))
    else:
        new_kws = [k.lower().strip() for k in (context.args[1:] if cmd == "add" else context.args) if k.lower() != "add"]
        for k in new_kws:
            if k and k not in kws: kws.append(k)
        cf["alert_keywords"] = kws
        save_filters()
        await update.message.reply_text("🚨 آلارم فعال: " + (", ".join(kws) if kws else "هیچی"))

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message: return
    chat_id  = update.effective_chat.id
    raw_text = ""
    if update.message.text:
        parts = update.message.text.split(None, 1)
        if len(parts) > 1: raw_text = parts[1]
    if not raw_text and context.args:
        raw_text = " ".join(context.args)
    if not raw_text.strip():
        await update.message.reply_text("استفاده:\n/add user1 user2 user3")
        return

    raw_usernames = re.split(r"[,\s\n\r\t]+", raw_text)
    usernames: List[str] = []
    seen = set()
    for item in raw_usernames:
        u = clean_username(item)
        if u and u not in seen and valid_username(u):
            seen.add(u)
            usernames.append(u)
    if not usernames:
        await update.message.reply_text("یوزرنیم معتبری پیدا نکردم.")
        return

    if len(usernames) == 1:
        username = usernames[0]
        msg  = await update.message.reply_text(f"⏳ در حال بررسی @{username} ...")
        feed = await fetch_rss_feed(username)
        if not feed or not feed.entries:
            await msg.edit_text(f"❌ @{username} پیدا نشد یا RSS در دسترس نیست.")
            return
        last_id = extract_tweet_id(feed.entries[0])
        already = chat_has_username(chat_id, username)
        add_chat_to_username(chat_id, username, last_id)
        for entry in feed.entries[:20]:
            tid = extract_tweet_id(entry)
            if tid: mark_as_sent(chat_id, tid)
        save_tracked()
        await msg.edit_text(f"{'ℹ️ از قبل وجود داشت' if already else '✅ اضافه شد'}: @{username}")
        return

    status_msg = await update.message.reply_text(f"📥 اضافه کردن {len(usernames)} اکانت...\n0/{len(usernames)}")
    added, failed, existed = [], [], []
    for i, username in enumerate(usernames, 1):
        try:
            if chat_has_username(chat_id, username):
                existed.append(username)
            else:
                feed = await fetch_rss_feed(username)
                if feed and feed.entries:
                    last_id = extract_tweet_id(feed.entries[0])
                    add_chat_to_username(chat_id, username, last_id)
                    for entry in feed.entries[:20]:
                        tid = extract_tweet_id(entry)
                        if tid: mark_as_sent(chat_id, tid)
                    save_tracked()
                    added.append(username)
                else:
                    failed.append(username)
        except Exception as e:
            logger.warning(f"Add failed {username}: {e}")
            failed.append(username)
        if i % 5 == 0 or i == len(usernames):
            try:
                await status_msg.edit_text(f"📥 {i}/{len(usernames)} | ✅{len(added)} ℹ️{len(existed)} ❌{len(failed)}")
            except Exception: pass
        await asyncio.sleep(0.8)
    report = f"✅ تمام!\nاضافه: {len(added)} | تکراری: {len(existed)} | ناموفق: {len(failed)}"
    if failed:
        report += "\n❌ " + ", ".join(f"@{u}" for u in failed[:10])
    await status_msg.edit_text(report)

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message: return
    chat_id  = update.effective_chat.id
    raw_text = ""
    if update.message.text:
        parts = update.message.text.split(None, 1)
        if len(parts) > 1: raw_text = parts[1]
    if not raw_text and context.args:
        raw_text = " ".join(context.args)
    if not raw_text.strip():
        await update.message.reply_text("استفاده: /remove username")
        return
    raw_usernames = re.split(r"[,\s\n\r\t]+", raw_text)
    removed, not_found = [], []
    for item in raw_usernames:
        u = clean_username(item)
        if not u: continue
        if remove_chat_from_username(chat_id, u): removed.append(u)
        else: not_found.append(u)
    save_tracked()
    msg = f"✅ حذف شد: {len(removed)}"
    if removed:  msg += "\n" + ", ".join(f"@{u}" for u in removed)
    if not_found: msg += f"\n❓ پیدا نشد: " + ", ".join(f"@{u}" for u in not_found)
    await update.message.reply_text(msg)

async def _reply(update: Update, text: str, **kwargs) -> None:
    if update.message:
        await update.message.reply_text(text, **kwargs)
    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, **kwargs)

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat: return
    chat_id     = update.effective_chat.id
    my_accounts = [u for u, i in tracked.items() if any(same_chat_id(chat_id, c) for c in i.get("chats", []))]
    if not my_accounts:
        await _reply(update, "هیچ اکانتی نداری.\n/add username")
        return
    text = f"📋 <b>{len(my_accounts)} اکانت:</b>\n" + "\n".join(f"• @{u}" for u in sorted(my_accounts))
    if len(text) > 4000: text = text[:4000] + "\n..."
    await _reply(update, text, parse_mode=ParseMode.HTML)

async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat: return
    chat_id = update.effective_chat.id
    message = update.message or (update.callback_query.message if update.callback_query else None)
    if not message: return

    args  = list(context.args) if context.args else []
    count = 1
    if args and args[-1].isdigit():
        count = max(1, min(int(args.pop()), 5))

    clean_args = [clean_username(a) for a in args]
    usernames  = [u for u in clean_args if valid_username(u)]

    if not usernames:
        usernames = [u for u, i in tracked.items() if any(same_chat_id(chat_id, c) for c in i.get("chats", []))]

    if not usernames:
        await message.reply_text("هیچ اکانتی نداری. /add")
        return

    await message.reply_text(f"🔍 چک کردن {len(usernames)} اکانت، {count} توییت...")
    sent = filtered = 0
    for username in usernames:
        feed = await fetch_rss_feed(username)
        if not feed or not feed.entries: continue
        for entry in reversed(feed.entries[:count]):
            ok, _ = await send_tweet_entry(chat_id, username, entry, context.bot)
            if ok: sent += 1
            else:  filtered += 1
            await asyncio.sleep(0.4)
    msg = f"✅ ارسال: {sent}"
    if filtered: msg += f" | فیلتر/تکراری: {filtered}"
    await message.reply_text(msg)

# =============================
# Background checker
# =============================
async def auto_backup(app: Application) -> None:
    enable_backup_env = os.getenv("ENABLE_BACKUP", "true").lower() in ("1", "true", "yes", "on")
    auto_backup_env = os.getenv("AUTO_BACKUP", "true").lower() in ("1", "true", "yes", "on")
    
    if not enable_backup_env or not auto_backup_env:
        logger.info("Auto-backup is completely DISABLED via environment variables.")
        return

    await asyncio.sleep(60)
    while True:
        try:
            _flush_dedup_to_file()
            logger.info("Dedup database auto-flushed to file.")
            if ADMIN_IDS:
                caption_map = {
                    DATA_FILE:     f"📦 auto-backup tracked_users — {len(tracked)} اکانت",
                    FILTERS_FILE:  f"📦 auto-backup filters — {len(filters_db.get('chats', {}))} چت",
                    SENT_IDS_FILE: f"📦 auto-backup sent_ids — {sum(len(v) for v in _dedup_ram.values())} ID",
                }
                name_map = {
                    DATA_FILE:     "tracked_users.json",
                    FILTERS_FILE:  "filters.json",
                    SENT_IDS_FILE: "sent_ids.json",
                }
                for aid in ADMIN_IDS:
                    for path, caption in caption_map.items():
                        if os.path.exists(path):
                            try:
                                with open(path, "rb") as f:
                                    await app.bot.send_document(
                                        chat_id=int(aid),
                                        document=f,
                                        filename=name_map[path],
                                        caption=caption,
                                    )
                            except Exception as e:
                                logger.warning(f"Auto-backup {path} failed for admin {aid}: {e}")
                logger.info(f"Auto-backup sent to all admins: {', '.join(ADMIN_IDS)}")
        except Exception as e:
            logger.error(f"Auto-backup error: {e}")
            
        await asyncio.sleep(BACKUP_INTERVAL)


# جابجا کردن این تابع به اینجای کد (قبل از دستورات ادمین و متد post_init)
async def check_twitter_updates(app: Application) -> None:
    while True:
        if not tracked:
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        for username, info in list(tracked.items()):
            try:
                feed = await fetch_rss_feed(username)
                if not feed or not feed.entries:
                    await asyncio.sleep(1.5)
                    continue

                last_id       = str(info.get("last_id", ""))
                new_entries   = []
                found_last_id = False

                for entry in feed.entries:
                    tid = extract_tweet_id(entry)
                    if last_id and tid == last_id:
                        found_last_id = True
                        break
                    new_entries.append(entry)

                if last_id and not found_last_id:
                    chats_of_user = list(info.get("chats", []))
                    truly_new = []
                    for e in new_entries:
                        tid = extract_tweet_id(e)
                        if not any(is_already_sent(c, tid) for c in chats_of_user):
                            truly_new.append(e)
                    
                    if len(truly_new) > MAX_BACKFILL_ON_MISSING_LAST_ID:
                        truly_new = truly_new[:MAX_BACKFILL_ON_MISSING_LAST_ID]
                    new_entries = truly_new

                if not new_entries:
                    await asyncio.sleep(1.5)
                    continue

                processed_ids: List[str] = []

                for entry in reversed(new_entries):
                    tid = extract_tweet_id(entry)
                    if not tid:
                        continue

                    for chat_id in list(info.get("chats", [])):
                        if is_already_sent(chat_id, tid):
                            continue
                        try:
                            await send_tweet_entry(chat_id, username, entry, app.bot)
                        except Exception as e:
                            logger.error(f"send_tweet_entry error {username}/{tid}: {e}")
                        await asyncio.sleep(0.3)

                    processed_ids.append(tid)

                if processed_ids and username in tracked:
                    tracked[username]["last_id"] = str(processed_ids[-1])
                    save_tracked()
                    logger.info(f"[{username}] last_id → {processed_ids[-1]} ({len(processed_ids)} processed)")

                await asyncio.sleep(1.5)

            except Exception as e:
                logger.error(f"Check failed for {username}: {e}")

        await asyncio.sleep(CHECK_INTERVAL)
# =============================
# Bot setup
# =============================
BOT_COMMANDS = [
    BotCommand("add",       "➕ اضافه کردن اکانت"),
    BotCommand("remove",    "➖ حذف اکانت"),
    BotCommand("list",      "📋 لیست اکانت‌ها"),
    BotCommand("check",     "🔍 چک دستی فید"),
    BotCommand("alert",     "🚨 آلارم کلمات طلایی"),
    BotCommand("keywords",  "🔑 فیلتر کلمات کلیدی"),
    BotCommand("filter",    "⚙️ فیلتر RT/Reply"),
    BotCommand("translate", "🌐 ترجمه فارسی on/off"),
    BotCommand("export",    "💾 دانلود بکاپ سرور"),
    BotCommand("stats",     "📊 آمار کانال و سرور"),
    BotCommand("start",     "❓ راهنما و منو اصلی"),
]

async def post_init(application: Application) -> None:
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        logger.warning(f"set_my_commands failed: {e}")
    asyncio.create_task(check_twitter_updates(application))
    asyncio.create_task(auto_backup(application))

def main() -> None:
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN رو در متغیرها قرار بده")
        return
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("add",       add_user))
    app.add_handler(CommandHandler("remove",    remove_user))
    app.add_handler(CommandHandler("list",      list_users))
    app.add_handler(CommandHandler("check",     check_now))
    app.add_handler(CommandHandler("translate", toggle_translate))
    app.add_handler(CommandHandler("filter",    cmd_filter))
    app.add_handler(CommandHandler("keywords",  cmd_keywords))
    app.add_handler(CommandHandler("alert",     cmd_alert))
    app.add_handler(CommandHandler("export",    cmd_export))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    logger.info(f"TweetBaan v6.0 Online | Admins={len(ADMIN_IDS)} | data={DATA_DIR}")
    app.run_polling()

if __name__ == "__main__":
    main()
