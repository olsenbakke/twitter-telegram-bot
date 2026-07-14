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
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

# Parse admin list (supports multiple admins separated by commas)
ADMIN_IDS = [x.strip() for x in os.getenv("ADMIN_CHAT_ID", "").split(",") if x.strip()]

DATA_FILE = os.path.join(DATA_DIR, "tracked_users.json")
FILTERS_FILE = os.path.join(DATA_DIR, "filters.json")
SENT_IDS_FILE = os.path.join(DATA_DIR, "sent_ids.json")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "90"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
MAX_BACKFILL_ON_MISSING_LAST_ID = int(os.getenv("MAX_BACKFILL_ON_MISSING_LAST_ID", "5"))
TRANSLATE_FA = os.getenv("TRANSLATE_FA", "true").lower() in ("1", "true", "yes", "on")
TRANSLATE_ENGINE = os.getenv("TRANSLATE_ENGINE", "google").lower().strip()
TRANSLATE_CACHE_MAX = int(os.getenv("TRANSLATE_CACHE_MAX", "1500"))
DEDUP_MAX_PER_CHAT = int(os.getenv("DEDUP_MAX_PER_CHAT", "2000"))
DEDUP_FILE_MAX_PER_KEY = int(os.getenv("DEDUP_FILE_MAX_PER_KEY", "500"))
FOLD_THRESHOLD = int(os.getenv("FOLD_THRESHOLD", "280"))
BACKUP_INTERVAL = int(os.getenv("BACKUP_INTERVAL", "21600"))  # default every 6 hours

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
# DEDUP - two-layer (RAM + optimized file)
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
# Translation Engine (Aerolink Gateway)
# =============================
import httpx

AEROLINK_API_KEY = os.getenv("AEROLINK_API_KEY", "").strip()
AEROLINK_BASE_URL = os.getenv("AEROLINK_BASE_URL", "").rstrip("/")
AEROLINK_MODEL = os.getenv("AEROLINK_MODEL", "gpt-4o-mini").strip()

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
    "airdrop hunter", "airdrop hunters", "proof", "proofs", "verification",
    "verify", "early", "building", "trust"
]
_crypto_terms_pattern = "|".join(re.escape(t) for t in sorted(CRYPTO_TERMS, key=len, reverse=True))
PROTECTED_RE = re.compile(
    r"https?://[^\s<>()]+|www\.[^\s<>()]+|@\w+|#[A-Za-z0-9_\u0600-\u06FF]+|\$[A-Za-z][A-Za-z0-9_]*"
    r"|\b(?:" + _crypto_terms_pattern + r")\b",
    re.IGNORECASE,
)

def init_translator() -> None:
    global translate_engine_name
    if TRANSLATE_FA and AEROLINK_API_KEY and AEROLINK_BASE_URL:
        translate_engine_name = "aerolink-ai"
        logger.info(f"Translator: Aerolink AI gateway enabled using {AEROLINK_MODEL}")
    else:
        try:
            from deep_translator import GoogleTranslator
            global translator
            translator = GoogleTranslator(source="auto", target="fa")
            translate_engine_name = "google"
            logger.info("Translator: Aerolink fallback to Google")
        except Exception:
            translate_engine_name = "off"

def normalize_tweet_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
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

def get_translate_data_engine() -> str:
    if not TRANSLATE_FA:
        return "off"
    if AEROLINK_API_KEY and AEROLINK_BASE_URL:
        return f"Aerolink AI ({AEROLINK_MODEL})"
    return "Google Fallback"

def get_translate_status() -> str:
    if not TRANSLATE_FA:
        return "Off"
    if AEROLINK_API_KEY and AEROLINK_BASE_URL:
        return f"Aerolink AI ({AEROLINK_MODEL})"
    return "Google Fallback"

def translate_with_aerolink(text: str) -> Optional[str]:
    if not AEROLINK_API_KEY or not AEROLINK_BASE_URL:
        return None
    try:
        url = f"{AEROLINK_BASE_URL}/chat/completions"
        headers = {
            "Authorization": f"Bearer {AEROLINK_API_KEY}",
            "Content-Type": "application/json"
        }
        prompt = (
            "You are an expert Persian crypto influencer and telegram admin.\n"
            "Translate the following English tweet into smooth, concise, and colloquial "
            "(informal/Tehran dialect) Persian, exactly how it's written on Iranian crypto channels.\n\n"
            "STRICT RULES:\n"
            "1. NEVER use formal/bookish Persian. Use natural conversational tone.\n"
            "2. DO NOT translate crypto tech terms. Leave these words EXACTLY in English: "
            "Airdrop, Mainnet, Testnet, Mint, Stake, Staking, Claim, Snapshot, Node, Validator, "
            "Whitelist, Listing, Wallet, Bridge, Swap, Presale, Launchpad, Gas, L1, L2, TVL, IDO, "
            "TGE, Hodl, FOMO, FUD, Proof, Verification, Early, Building.\n"
            "3. Keep all @usernames, #hashtags, $tickers, and URLs exactly as they are in the original text.\n"
            "4. Output ONLY the Persian translation. No explanations, no introduction, no quotes.\n\n"
            f"Text to translate: {text}"
        )
        payload = {
            "model": AEROLINK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2
        }

        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.post(url, headers=headers, json=payload)
            if response.status_code == 200:
                res_data = response.json()
                result = res_data["choices"][0]["message"]["content"].strip()
                return result.strip("\"'\n ") or None
            else:
                logger.warning(f"Aerolink API error: Status {response.status_code} - {response.text}")
                return None
    except Exception as e:
        logger.warning(f"Aerolink translate failed: {e}")
        return None

def translate_with_google(text: str) -> Optional[str]:
    try:
        from deep_translator import GoogleTranslator
        g_translator = GoogleTranslator(source="auto", target="fa")
        result = g_translator.translate(text[:4500])
        if result:
            result = re.sub(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+', '', html.unescape(result))
            return result.strip()
    except Exception as e:
        logger.warning(f"Google fallback failed: {e}")
        return None

def translate_fa(text: str) -> Optional[str]:
    if not TRANSLATE_FA:
        return None
    cleaned = normalize_tweet_text(text)
    if not cleaned or persian_ratio(cleaned) > 0.55:
        return None
    if cleaned in translate_cache:
        return translate_cache[cleaned]

    result = translate_with_aerolink(cleaned)
    if not result:
        result = translate_with_google(cleaned)

    if not result or result.strip() == cleaned.strip():
        return None

    if len(translate_cache) >= TRANSLATE_CACHE_MAX:
        translate_cache.pop(next(iter(translate_cache)), None)
    translate_cache[cleaned] = result
    return result

init_translator()

# =============================
# Storage & Data Management
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
            "keywords": [], "alert_keywords": [],
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
    old = tracked[username].get("chats", [])
    new = [c for c in old if not same_chat_id(chat_id, c)]
    if len(new) == len(old):
        return False
    tracked[username]["chats"] = new
    if not new:
        del tracked[username]
    return True

# =============================
# RSS / Tweet Parsing Helpers
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
    if m:
        return m.group(1)

    eid = str(entry.get("id", "") or "")
    m2 = re.search(r"(\d{15,})", eid)
    if m2:
        return m2.group(1)

    guid = str(entry.get("guid", "") or "")
    m3 = re.search(r"/status(?:es)?/(\d+)", guid)
    if m3:
        return m3.group(1)
    m4 = re.search(r"(\d{15,})", guid)
    if m4:
        return m4.group(1)

    desc = str(entry.get("description", "") or "")
    m5 = re.search(r"/status(?:es)?/(\d+)", desc)
    if m5:
        return m5.group(1)

    return ""

def normalize_x_link(link: str, username: str, tweet_id: str) -> str:
    link = html.unescape(link or "")
    if tweet_id and re.match(r"^\d+$", str(tweet_id)):
        return f"https://x.com/{username}/status/{tweet_id}"
    for old, new in {
        "https://twitter.com": "https://x.com",
        "http://twitter.com": "https://x.com",
        "https://nitter.poast.org": "https://x.com",
        "http://nitter.poast.org": "https://x.com",
        "https://nitter.net": "https://x.com",
        "http://nitter.net": "https://x.com",
        "https://xcancel.com": "https://x.com",
        "http://xcancel.com": "https://x.com",
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
    cf = get_chat_filters(chat_id)
    low = normalize_tweet_text(text).lower()

    alert_kws = cf.get("alert_keywords", [])
    is_alert = any(str(k).lower() in low for k in alert_kws) if alert_kws else False
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
        href = enc.get("href", "") or ""
        enc_type = enc.get("type", "") or ""
        if href and ("image" in enc_type.lower() or re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", href, re.I)):
            return html.unescape(urljoin(link, href))
    desc = html.unescape(entry.get("description", "") or "")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc, re.IGNORECASE)
    if m:
        return html.unescape(urljoin(link, m.group(1)))
    return None

def trim_raw(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"

def pick_emoji(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("rocket", "moon", "pump", "ath", "bullish", "bull", "lfg", "green")):
        return "🚀"
    if any(w in t for w in ("airdrop", "free", "claim", "reward", "giveaway")):
        return "🎁"
    if any(w in t for w in ("listing", "listed", "list", "launch", "tge", "ido", "ieo")):
        return "📢"
    if any(w in t for w in ("mainnet", "testnet", "upgrade", "update", "deploy")):
        return "⚙️"
    if any(w in t for w in ("nft", "mint", "opensea", "blur")):
        return "🖼️"
    if any(w in t for w in ("hack", "exploit", "scam", "rug", "warning", "alert", "beware")):
        return "⚠️"
    if any(w in t for w in ("partnership", "partner", "collab", "x ", " x ")):
        return "🤝"
    if any(w in t for w in ("bear", "dump", "sell", "short", "down", "red", "crash")):
        return "🔴"
    if any(w in t for w in ("staking", "yield", "apr", "apy", "farm", "liquidity")):
        return "💰"
    if any(w in t for w in ("vote", "governance", "dao", "proposal")):
        return "🗳️"
    return "🐦"

_URL_RE = re.compile(r"https?://[^\s<>\"']+")

def escape_and_linkify(text: str) -> str:
    parts = []
    last = 0
    for m in _URL_RE.finditer(text):
        parts.append(html.escape(text[last:m.start()]))
        url = m.group(0)
        short = re.sub(r"^https?://", "", url)
        if len(short) > 30:
            short = short[:28] + "…"
        parts.append(f' [{html.escape(short)}]({html.escape(url)})')
        last = m.end()
    parts.append(html.escape(text[last:]))
    return "".join(parts)

# Emoji name to unicode mapping for output
_EMOJI_MAP = {
    "rocket": "🚀", "gift": "🎁", "mega": "📢", "gear": "⚙️",
    "frame": "🖼️", "warning": "⚠️", "handshake": "🤝",
    "red_circle": "🔴", "money_bag": "💰",
    "ballot_box": "🗳️", "bird": "🐦",
}

def build_tweet_message(username: str, title: str, fa_text: Optional[str], is_alert: bool, image_url: Optional[str] = None) -> str:
    emoji = "🚨" if is_alert else pick_emoji(title)
    hidden_img = f' [​]({image_url})' if image_url else ""

    if is_alert:
        header = f"{hidden_img}🚨 **ALERT** 🚨\n{emoji} **@{html.escape(username)}**"
    else:
        header = f"{hidden_img}{emoji} **@{html.escape(username)}**"

    body_raw = trim_raw(title, 2200)

    if len(body_raw) > FOLD_THRESHOLD:
        body = f"\n\n> {escape_and_linkify(body_raw)}\n\n"
    else:
        body = escape_and_linkify(body_raw)

    text = f"{header}\n\n{body}"

    if fa_text and fa_text.strip() != title.strip():
        fa_raw = trim_raw(fa_text, 1200)
        if len(fa_raw) > FOLD_THRESHOLD:
            fa_block = f"\n\n> {html.escape(fa_raw)}\n\n"
        else:
            fa_block = html.escape(fa_raw)
        text += f"\n\n**Translation:**\n{fa_block}"

    if len(text) > 4096:
        text = text[:4000].rstrip() + "\n…"
    return text

async def send_tweet_entry(chat_id: Any, username: str, entry: Any, bot: Any) -> Tuple[bool, str]:
    title = normalize_tweet_text(entry.get("title", "") or "")
    title = re.sub(rf"^{re.escape(username)}\s*:\s*", "", title, flags=re.IGNORECASE)
    tweet_id = extract_tweet_id(entry)

    if is_already_sent(chat_id, tweet_id):
        return False, "duplicate"

    ok, reason, is_alert = should_send(chat_id, username, title)
    if not ok:
        mark_as_sent(chat_id, tweet_id)
        return False, reason

    link = normalize_x_link(entry.get("link", "") or "", username, tweet_id)
    fa_text = await asyncio.to_thread(translate_fa, title) if TRANSLATE_FA else None
    image_url = extract_image_url(entry)

    text = build_tweet_message(username, title, fa_text, is_alert, image_url)

    keyboard = [[InlineKeyboardButton("View on X", url=link)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    disable_notif = not is_alert
    sent_msg = None

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
# Telegram Commands Execution
# =============================
def is_admin_chat(chat_id: Any) -> bool:
    if not ADMIN_IDS:
        return True
    return str(chat_id) in ADMIN_IDS

def parse_on_off(value: str) -> Optional[bool]:
    value_str = (value or "").lower().strip()
    if value_str in ("on", "1", "true", "yes", "enable", "enabled"):
        return True
    if value_str in ("off", "0", "false", "no", "disable", "disabled"):
        return False
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    cf = get_chat_filters(chat_id)
    accounts_count = len([u for u, i in tracked.items() if any(same_chat_id(chat_id, c) for c in i.get("chats", []))])

    msg = (
        f"Welcome to the Twitter Monitoring Bot!\n\n"
        f"Filters status for this chat:\n"
        f"- Active accounts: `{accounts_count}`\n"
        f"- Translator: `{get_translate_status()}`\n"
        f"- Filter Retweets: `{'On' if cf.get('filter_rt', True) else 'Off'}`\n"
        f"- Filter Replies: `{'On' if cf.get('filter_replies', True) else 'Off'}`\n\n"
        f"Commands:\n"
        f"- `/add username` : Add one or more accounts (e.g. `/add user1 user2 user3`)\n"
        f"- `/import user1 user2` : Import many accounts at once (or send a .txt/.json file)\n"
        f"- `/del username` : Remove an account\n"
        f"- `/list` : Show tracked accounts\n"
        f"- `/filter_rt on/off` : Toggle retweet filter\n"
        f"- `/filter_reply on/off` : Toggle reply filter\n"
        f"- `/keywords a, b` : Set normal keyword filter\n"
        f"- `/alert_keywords x, y` : Set pinning alert keywords"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        return

    if not context.args:
        await update.message.reply_text(
            "Please provide one or more usernames.\n"
            "Example: `/add elonmusk`\n"
            "Multiple: `/add user1 user2 user3`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Combine all args and split by whitespace or comma -> supports multiple accounts
    raw = " ".join(context.args)
    raw_items = [u for u in re.split(r"[,\s]+", raw) if u]

    # Clean + dedupe (preserve order)
    seen: Set[str] = set()
    clean_list: List[str] = []
    for u in raw_items:
        c = clean_username(u)
        if c and c not in seen:
            seen.add(c)
            clean_list.append(c)

    if not clean_list:
        await update.message.reply_text("No valid username was provided.")
        return

    wait_msg = await update.message.reply_text(
        f"Validating and adding {len(clean_list)} account(s)...",
        parse_mode=ParseMode.MARKDOWN,
    )

    added: List[str] = []
    failed: List[str] = []
    skipped: List[str] = []

    for username in clean_list:
        if not valid_username(username):
            failed.append(username)
            continue

        if chat_has_username(chat_id, username):
            skipped.append(username)
            continue

        try:
            feed = await fetch_rss_feed(username)
            if not feed or not feed.entries:
                failed.append(username)
                continue

            last_id = extract_tweet_id(feed.entries[0])
            add_chat_to_username(chat_id, username, last_id)
            added.append(username)
        except Exception as e:
            logger.warning(f"add failed for {username}: {e}")
            failed.append(username)

    # Persist once
    if added:
        save_tracked()

    parts: List[str] = []
    if added:
        parts.append(f"Added {len(added)} account(s):\n" + "\n".join(f"+ `@{u}`" for u in added))
    if skipped:
        parts.append(f"Already tracked ({len(skipped)}):\n" + " ".join(f"`@{u}`" for u in skipped))
    if failed:
        parts.append(f"Failed / not found ({len(failed)}):\n" + " ".join(f"`@{u}`" for u in failed))

    if not parts:
        parts.append("Nothing to add.")

    result_msg = "\n\n".join(parts)
    if len(result_msg) > 4000:
        result_msg = result_msg[:4000] + "\n…"
    await wait_msg.edit_text(result_msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        return

    if not context.args:
        await update.message.reply_text(
            "Please provide a username.\nExample: `/del elonmusk`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    username = clean_username(context.args[0])
    if remove_chat_from_username(chat_id, username):
        save_tracked()
        await update.message.reply_text(f"Account `@{username}` removed from this chat.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("This account is not tracked in this chat.")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id

    users = [u for u, info in tracked.items() if any(same_chat_id(chat_id, c) for c in info.get("chats", []))]
    if not users:
        await update.message.reply_text("No accounts are tracked in this chat.")
        return

    msg = "Tracked accounts in this chat:\n\n" + "\n".join(f"- `@{u}`" for u in sorted(users))
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_filter_rt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        return

    if not context.args:
        cf = get_chat_filters(chat_id)
        status = "On" if cf.get("filter_rt", True) else "Off"
        await update.message.reply_text(f"Retweet filter is currently: `{status}`", parse_mode=ParseMode.MARKDOWN)
        return

    val = parse_on_off(context.args[0])
    if val is None:
        await update.message.reply_text("Invalid value. Use `on` or `off`.", parse_mode=ParseMode.MARKDOWN)
        return

    cf = get_chat_filters(chat_id)
    cf["filter_rt"] = val
    save_filters()
    await update.message.reply_text(f"Retweet filter set to: `{'On' if val else 'Off'}`", parse_mode=ParseMode.MARKDOWN)

async def cmd_filter_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        return

    if not context.args:
        cf = get_chat_filters(chat_id)
        status = "On" if cf.get("filter_replies", True) else "Off"
        await update.message.reply_text(f"Reply filter is currently: `{status}`", parse_mode=ParseMode.MARKDOWN)
        return

    val = parse_on_off(context.args[0])
    if val is None:
        await update.message.reply_text("Invalid value. Use `on` or `off`.", parse_mode=ParseMode.MARKDOWN)
        return

    cf = get_chat_filters(chat_id)
    cf["filter_replies"] = val
    save_filters()
    await update.message.reply_text(f"Reply filter set to: `{'On' if val else 'Off'}`", parse_mode=ParseMode.MARKDOWN)

async def cmd_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        return

    cf = get_chat_filters(chat_id)
    if not context.args:
        kws = cf.get("keywords", [])
        msg = f"Current keywords:\n`{', '.join(kws)}`" if kws else "No keywords set (all tweets are sent)."
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    raw = " ".join(context.args)
    if raw.lower() in ("clear", "none"):
        cf["keywords"] = []
        save_filters()
        await update.message.reply_text("All normal keywords have been cleared.")
        return

    kws = [k.strip() for k in raw.split(",") if k.strip()]
    cf["keywords"] = kws
    save_filters()
    await update.message.reply_text(f"Normal keywords updated:\n`{', '.join(kws)}`", parse_mode=ParseMode.MARKDOWN)

async def cmd_alert_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        return

    cf = get_chat_filters(chat_id)
    if not context.args:
        kws = cf.get("alert_keywords", [])
        msg = f"Current alert keywords:\n`{', '.join(kws)}`" if kws else "No alert keywords set."
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    raw = " ".join(context.args)
    if raw.lower() in ("clear", "none"):
        cf["alert_keywords"] = []
        save_filters()
        await update.message.reply_text("All alert keywords have been cleared.")
        return

    kws = [k.strip() for k in raw.split(",") if k.strip()]
    cf["alert_keywords"] = kws
    save_filters()
    await update.message.reply_text(f"Alert keywords updated:\n`{', '.join(kws)}`", parse_mode=ParseMode.MARKDOWN)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    if not is_admin_chat(update.effective_chat.id):
        return

    total_cached = len(translate_cache)
    engine = get_translate_data_engine()
    total_ids = sum(len(v) for v in _dedup_ram.values())

    msg = (
        f"Server stats:\n\n"
        f"- Translation engine: `{engine}`\n"
        f"- Total tracked accounts: `{len(tracked)}`\n"
        f"- Dedup records: `{total_ids}`\n"
        f"- Cached translations: `{total_cached}/{TRANSLATE_CACHE_MAX}`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    if not is_admin_chat(update.effective_chat.id):
        return

    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename="tracked_users.json",
                caption="Tracked accounts database"
            )
    else:
        await update.message.reply_text("No database file found.")

# =============================
# Import helpers
# =============================
def _extract_usernames(text: str) -> List[str]:
    """Extract usernames from a JSON or plain-text payload."""
    raw: List[str] = []
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            # export format: keys are usernames
            raw = list(data.keys())
        elif isinstance(data, list):
            raw = [str(x) for x in data]
    except (json.JSONDecodeError, ValueError):
        raw = re.split(r"[,\s]+", text.strip())

    cleaned: List[str] = []
    for u in raw:
        c = clean_username(str(u))
        if c:
            cleaned.append(c)
    return cleaned


async def _import_accounts_batch(
    chat_id: Any, usernames: List[str], status_msg
) -> Tuple[List[str], List[str], List[str]]:
    """Fetch last_id for each account concurrently and add them."""
    seen: Set[str] = set()
    clean_list: List[str] = []
    for u in usernames:
        c = clean_username(str(u))
        if c and c not in seen:
            seen.add(c)
            clean_list.append(c)

    to_fetch: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    for username in clean_list:
        if not valid_username(username):
            failed.append(username)
        elif chat_has_username(chat_id, username):
            skipped.append(username)
        else:
            to_fetch.append(username)

    added: List[str] = []
    BATCH = 8
    total = len(to_fetch)

    for start in range(0, total, BATCH):
        batch = to_fetch[start:start + BATCH]
        feeds = await asyncio.gather(
            *[fetch_rss_feed(u) for u in batch], return_exceptions=True
        )
        for username, feed in zip(batch, feeds):
            try:
                if isinstance(feed, Exception) or not feed or not feed.entries:
                    # feed not available now; add anyway, bot handles it later
                    add_chat_to_username(chat_id, username, "")
                    added.append(username)
                else:
                    last_id = extract_tweet_id(feed.entries[0])
                    add_chat_to_username(chat_id, username, last_id)
                    added.append(username)
            except Exception as e:
                logger.warning(f"import failed for {username}: {e}")
                failed.append(username)

        done = min(start + BATCH, total)
        try:
            await status_msg.edit_text(
                f"Importing... {done}/{total} processed "
                f"({len(added)} added, {len(skipped)} existing, {len(failed)} failed)"
            )
        except Exception:
            pass

    if added:
        save_tracked()

    return added, skipped, failed


def _build_import_report(added: List[str], skipped: List[str], failed: List[str]) -> str:
    parts: List[str] = []
    if added:
        parts.append(f"Added {len(added)} account(s):\n" + " ".join(f"`@{u}`" for u in added))
    if skipped:
        parts.append(f"Already tracked ({len(skipped)}):\n" + " ".join(f"`@{u}`" for u in skipped))
    if failed:
        parts.append(f"Failed ({len(failed)}):\n" + " ".join(f"`@{u}`" for u in failed))
    if not parts:
        parts.append("Nothing to import.")
    msg = "\n\n".join(parts)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n…"
    return msg


async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Import multiple accounts at once from command arguments or a file."""
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        return

    usernames: List[str] = []

    # Mode: a file is attached (with or without /import caption)
    if update.message.document:
        try:
            doc = update.message.document
            tg_file = await context.bot.get_file(doc.file_id)
            data = await tg_file.download_as_bytearray()
            text = bytes(data).decode("utf-8", errors="ignore")
            usernames = _extract_usernames(text)
        except Exception as e:
            logger.warning(f"import file read failed: {e}")
            await update.message.reply_text("Failed to read the attached file.")
            return
    elif context.args:
        usernames = list(context.args)

    if not usernames:
        await update.message.reply_text(
            "Usage:\n"
            "- `/import user1 user2 user3` to import a list of usernames\n"
            "- Or send a .txt / .json file with the usernames",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status_msg = await update.message.reply_text(
        f"Importing {len(usernames)} account(s)... this may take a moment."
    )
    added, skipped, failed = await _import_accounts_batch(chat_id, usernames, status_msg)
    await status_msg.edit_text(
        _build_import_report(added, skipped, failed), parse_mode=ParseMode.MARKDOWN
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Import accounts from a .txt / .json file sent to the chat."""
    if not update.effective_chat or not update.message or not update.message.document:
        return
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        return

    doc = update.message.document
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = await tg_file.download_as_bytearray()
        text = bytes(data).decode("utf-8", errors="ignore")
    except Exception as e:
        logger.warning(f"document read failed: {e}")
        await update.message.reply_text("Failed to read the attached file.")
        return

    usernames = _extract_usernames(text)
    if not usernames:
        await update.message.reply_text("No valid usernames found in the file.")
        return

    status_msg = await update.message.reply_text(
        f"Importing {len(usernames)} account(s) from file..."
    )
    added, skipped, failed = await _import_accounts_batch(chat_id, usernames, status_msg)
    await status_msg.edit_text(
        _build_import_report(added, skipped, failed), parse_mode=ParseMode.MARKDOWN
    )

# =============================
# Background Tasks
# =============================
async def auto_backup(app: Application) -> None:
    enable_backup_env = os.getenv("ENABLE_BACKUP", "true").lower() in ("1", "true", "yes", "on")
    auto_backup_env = os.getenv("AUTO_BACKUP", "true").lower() in ("1", "true", "yes", "on")

    if not enable_backup_env or not auto_backup_env:
        logger.info("Auto-backup is completely DISABLED via environment variables.")
        return

    await asyncio.sleep(60)  # initial delay

    while True:
        try:
            _flush_dedup_to_file()
            logger.info("Dedup database auto-flushed to file.")

            if ADMIN_IDS:
                caption_map = {
                    DATA_FILE: f"auto-backup tracked_users - {len(tracked)} accounts",
                    FILTERS_FILE: f"auto-backup filters - {len(filters_db.get('chats', {}))} chats",
                    SENT_IDS_FILE: f"auto-backup sent_ids - {sum(len(v) for v in _dedup_ram.values())} IDs",
                }
                name_map = {
                    DATA_FILE: "tracked_users.json",
                    FILTERS_FILE: "filters.json",
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

                last_id = str(info.get("last_id", ""))
                new_entries = []
                found_last_id = False

                for entry in feed.entries:
                    tid = extract_tweet_id(entry)
                    if last_id and tid == last_id:
                        found_last_id = True
                        break
                    new_entries.append(entry)

                if not found_last_id:
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
                    logger.info(f"[{username}] last_id -> {processed_ids[-1]} ({len(processed_ids)} processed)")

                await asyncio.sleep(1.5)

            except Exception as e:
                logger.error(f"Check failed for {username}: {e}")

        await asyncio.sleep(CHECK_INTERVAL)

async def post_init(application: Application) -> None:
    asyncio.create_task(check_twitter_updates(application))
    asyncio.create_task(auto_backup(application))

    # Register the official Telegram bot command menu
    commands = [
        BotCommand("start", "Start the bot and check filters"),
        BotCommand("add", "Add one or more Twitter accounts"),
        BotCommand("del", "Remove a tracked Twitter account"),
        BotCommand("list", "List all active accounts in this chat"),
        BotCommand("keywords", "Manage normal keyword filters"),
        BotCommand("alert_keywords", "Manage alert (pinning) keywords"),
        BotCommand("filter_rt", "Toggle retweet filter on/off"),
        BotCommand("filter_reply", "Toggle reply filter on/off"),
        BotCommand("stats", "Show server stats and load"),
        BotCommand("export", "Download the accounts database file"),
        BotCommand("import", "Import accounts from a list or file"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Full Bot Command-menu structure registered successfully.")

# =============================
# MAIN RUNNER
# =============================
def main() -> None:
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is missing!")
        return

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("del", cmd_del))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("filter_rt", cmd_filter_rt))
    application.add_handler(CommandHandler("filter_reply", cmd_filter_reply))
    application.add_handler(CommandHandler("keywords", cmd_keywords))
    application.add_handler(CommandHandler("alert_keywords", cmd_alert_keywords))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("export", cmd_export))
    application.add_handler(CommandHandler("import", cmd_import))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("All bot features initialized. Starting Polling...")
    application.run_polling()

if __name__ == "__main__":
    main()
