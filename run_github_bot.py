import os
import json
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
ADMIN_IDS = [x.strip() for x in ADMIN_CHAT_ID.split(",") if x.strip()]

DATA_FILE = "tracked_users.json"
FILTERS_FILE = "filters.json"
SENT_IDS_FILE = "sent_ids.json"
OFFSET_FILE = "tg_offset.json"

# ==========================================
# DATABASE LOAD/SAVE (LOCAL JSON FILES)
# ==========================================
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Failed to load {path}: {e}")
            return deepcopy(default)
    return deepcopy(default)

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Failed to save {path}: {e}")

tracked = load_json(DATA_FILE, {})
filters_db = load_json(FILTERS_FILE, {"chats": {}})
sent_ids = load_json(SENT_IDS_FILE, {})

def save_tracked():
    save_json(DATA_FILE, tracked)

def save_filters():
    save_json(FILTERS_FILE, filters_db)

def save_sent_ids():
    save_json(SENT_IDS_FILE, sent_ids)

# ==========================================
# TELEGRAM API HELPERS
# ==========================================
def send_msg(chat_id, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }, timeout=10)
    except Exception as e:
        print(f"Error sending message to {chat_id}: {e}")

def send_photo(chat_id, photo_url, caption):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Error sending photo to {chat_id}: {e}")

def send_media_group(chat_id, imageUrls, caption):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMediaGroup"
    media = []
    for i, img in enumerate(imageUrls[:10]):
        item = {"type": "photo", "media": img}
        if i == 0:
            item["caption"] = caption;
            item["parse_mode"] = "HTML";
        media.append(item)
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "media": media
        }, timeout=15)
    except Exception as e:
        print(f"Error sending media group to {chat_id}: {e}")

# ==========================================
# TELEGRAM COMMANDS PROCESSOR (getUpdates)
# ==========================================
def process_telegram_commands():
    offset = 0
    if os.path.exists(OFFSET_FILE):
        try:
            with open(OFFSET_FILE, "r") as f:
                offset = json.load(f).get("offset", 0)
        except:
            pass

    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?timeout=5"
    if offset > 0:
        url += f"&offset={offset}"

    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return
        updates = r.json().get("result", [])
    except Exception as e:
        print(f"Error getting updates: {e}")
        return

    if not updates:
        return

    global tracked, filters_db
    highest_id = 0

    for u in updates:
        update_id = u.get("update_id", 0)
        if update_id > highest_id:
            highest_id = update_id

        message = u.get("message")
        if not message or "text" not in message:
            continue

        chat_id = str(message["chat"]["id"])
        text = message["text"].strip()

        # Auth Check
        if ADMIN_IDS and chat_id not in ADMIN_IDS:
            send_msg(chat_id, "❌ شما دسترسی لازم برای استفاده از این ربات را ندارید.")
            continue

        args = text.split()
        if not args:
            continue
        command = args[0].lower()

        # Handle Commands
        if command == "/start":
            welcome = (
                "<b>به ربات فوروارد توییتر خوش آمدید! 🐦 (نسخه گیت‌هاب اکشنز)</b>\n\n"
                "این ربات هر ۱۰ دقیقه بیدار شده، کامندهای شما را پردازش کرده و توییت‌های جدید را ارسال می‌کند.\n\n"
                "📌 <b>راهنمای دستورات:</b>\n"
                "• <code>/add username1 username2</code> : شروع ردیابی اکانت‌ها\n"
                "• <code>/del username1 username2</code> : حذف اکانت‌ها\n"
                "• <code>/list</code> : لیست اکانت‌های فعال\n"
                "• <code>/filter_rt</code> : فعال/غیرفعال کردن ری‌توییت‌ها\n"
                "• <code>/filter_reply</code> : فعال/غیرفعال کردن ریپلای‌ها\n"
                "• <code>/keywords add [کلمه]</code> : فیلتر کلمه کلیدی\n"
                "• <code>/keywords del [کلمه]</code> : حذف کلمه کلیدی\n"
                "• <code>/keywords list</code> : لیست کلمات کلیدی"
            )
            send_msg(chat_id, welcome)

        elif command == "/add":
            if len(args) < 2:
                send_msg(chat_id, "❌ لطفا حداقل یک یوزرنیم وارد کنید.\nمثال: <code>/add vitalikbuterin elonmusk</code>")
                continue
            usernames = [u.replace("@", "").lower().strip() for u in args[1:]]
            added = []
            for u in usernames:
                if not u: continue
                if u not in tracked:
                    tracked[u] = {"last_id": "", "chats": []}
                if chat_id not in tracked[u]["chats"]:
                    tracked[u]["chats"].append(chat_id)
                    added.append(u)
            save_tracked()
            send_msg(chat_id, f"✅ اکانت‌های زیر به لیست ردیابی اضافه شدند:\n" + "\n".join([f"• @{x}" for x in added]))

        elif command == "/del":
            if len(args) < 2:
                send_msg(chat_id, "❌ لطفا حداقل یک یوزرنیم وارد کنید.\nمثال: <code>/del vitalikbuterin</code>")
                continue
            usernames = [u.replace("@", "").lower().strip() for u in args[1:]]
            deleted = []
            for u in usernames:
                if u in tracked and chat_id in tracked[u]["chats"]:
                    tracked[u]["chats"].remove(chat_id)
                    if not tracked[u]["chats"]:
                        del tracked[u]
                    deleted.append(u)
            save_tracked()
            send_msg(chat_id, f"✅ اکانت‌های زیر از لیست ردیابی حذف شدند:\n" + "\n".join([f"• @{x}" for x in deleted]))

        elif command == "/list":
            lst = [f"• @{u}" for u, info in tracked.items() if chat_id in info["chats"]]
            if lst:
                send_msg(chat_id, f"📋 <b>تعداد {len(lst)} اکانت فعال در این چت:</b>\n\n" + "\n".join(lst))
            else:
                send_msg(chat_id, "📭 هیچ اکانتی در حال حاضر ردیابی نمی‌شود.")

        elif command == "/filter_rt":
            filters_db.setdefault("chats", {})
            filters_db["chats"].setdefault(chat_id, {})
            rt_status = "فعال" if filters_db["chats"][chat_id]["filter_rt"] else "غیرفعال"
            send_msg(chat_id, f"✅ فیلتر ری‌توییت <b>{rt_status}</b> شد.")

        elif command == "/filter_reply":
            filters_db.setdefault("chats", {})
            filters_db["chats"].setdefault(chat_id, {})
            filters_db["chats"][chat_id]["filter_reply"] = not filters_db["chats"][chat_id].get("filter_reply", True)
            save_filters()
            reply_status = "فعال" if filters_db["chats"][chat_id]["filter_reply"] else "غیرفعال"
            send_msg(chat_id, f"✅ فیلتر ریپلای <b>{reply_status}</b> شد.")

        elif command == "/keywords":
            filters_db.setdefault("chats", {})
            filters_db["chats"].setdefault(chat_id, {})
            filters_db["chats"][chat_id].setdefault("keywords", [])
            
            sub = args[1].lower() if len(args) > 1 else ""
            if sub == "add" and len(args) > 2:
                word = " ".join(args[2:]).lower().strip()
                if word not in filters_db["chats"][chat_id]["keywords"]:
                    filters_db["chats"][chat_id]["keywords"].append(word)
                save_filters()
                send_msg(chat_id, f"✅ کلمه کلیدی <b>\"{word}\"</b> اضافه شد.")
            elif sub == "del" and len(args) > 2:
                word = " ".join(args[2:]).lower().strip()
                filters_db["chats"][chat_id]["keywords"] = [w for w in filters_db["chats"][chat_id]["keywords"] if w != word]
                save_filters()
                send_msg(chat_id, f"✅ کلمه کلیدی <b>\"{word}\"</b> حذف شد.")
            else:
                kws = filters_db["chats"][chat_id]["keywords"]
                if kws:
                    send_msg(chat_id, "🗝 <b>کلمات کلیدی فیلتر فعال:</b>\n\n" + "\n".join([f"• <code>{w}</code>" for w in kws]))
                else:
                    send_msg(chat_id, "💡 فیلتر کلمه کلیدی برای این چت تعریف نشده است.")

    # Confirm and clear updates queue on Telegram
    if highest_id > 0:
        with open(OFFSET_FILE, "w") as f:
            json.dump({"offset": highest_id + 1}, f)
        try:
            requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={highest_id + 1}&limit=1", timeout=5)
        except:
            pass

# ==========================================
# TWITTER SCRApING & FORWARDING LOOP
# ==========================================
def parse_rss_xml(xmlText):
    items = []
    matches = re.findall(r"<item>([\s\S]*?)</item>", xmlText)
    for m in matches:
        title_match = re.search(r"<title>([\s\S]*?)</title>", m)
        link_match = re.search(r"<link>([\s\S]*?)</link>", m)
        desc_match = re.search(r"<description>([\s\S]*?)</description>", m)
        guid_match = re.search(r"<guid[\s\S]*?>([\s\S]*?)</guid>", m)
        
        title = clean_xml_entities(title_match.group(1)) if title_match else ""
        link = clean_xml_entities(link_match.group(1)) if link_match else ""
        desc = clean_xml_entities(desc_match.group(1)) if desc_match else ""
        guid = clean_xml_entities(guid_match.group(1)) if guid_match else ""
        
        items.append({"title": title, "link": link, "description": desc, "guid": guid})
    return items

def clean_xml_entities(s):
    return s.replace("<![CDATA[", "").replace("]]>", "").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").strip()

def clean_tweet_text(text):
    if not text: return ""
    cleaned = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = cleaned.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").replace("&#x27;", "'")
    return cleaned.strip()

def extract_images(desc):
    return re.findall(r"<img[^>]+src=[\"']([^\"']+)[\"']", desc)

def translate_fa(text):
    if not text or text.strip() == "": return ""
    
    # Try Aerolink Premium GPT Translation first
    api_key = os.getenv("AEROLINK_API_KEY")
    base_url = os.getenv("AEROLINK_BASE_URL")
    model = os.getenv("AEROLINK_MODEL", "gpt-4o-mini")
    
    if api_key and base_url:
        try:
            url = f"{base_url.rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            r = requests.post(url, headers=headers, json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are an expert Persian crypto influencer and telegram admin. Translate the following English tweet into smooth, concise, and colloquial Persian."},
                    {"role": "user", "content": text}
                ]
            }, timeout=12)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print("Aerolink translation failed:", e)

    # Fallback to free Google Translate API
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=fa&dt=t&q={requests.utils.quote(text)}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data and data[0]:
                return "".join([sentence[0] for sentence in data[0] if sentence[0]])
    except Exception as e:
        print("Google translation fallback failed:", e)
    return ""

def format_message(username, clean_text, translated_text, tweet_link):
    msg = f"👤 <b><a href=\"https://x.com/{username}\">@{username}</a></b>\n\n"
    msg += f"{clean_text}\n"
    if translated_text:
        msg += f"\n📝 <b>ترجمه فارسی:</b>\n<i>{translated_text}</i>\n"
    msg += f"\n🔗 <a href=\"{tweet_link}\">مشاهده در توییتر</a>"
    return msg

def monitor_twitter_accounts():
    global tracked, sent_ids
    if not tracked:
        return

    # Nitter is the best and only reliable public source in 2026!
    RSS_SOURCES = [
        "https://nitter.net/{username}/rss"
    ]

    for username, info in list(tracked.items()):
        if not info.get("chats"):
            continue

        feed_data = None
        for source in RSS_SOURCES:
            url = source.replace("{username}", username)
            try:
                # GitHub Azure IP bypasses Cloudflare loop blocks perfectly!
                r = requests.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }, timeout=12)
                if r.status_code == 200:
                    feed_data = r.text
                    break
            except Exception as e:
                print(f"Failed fetching {url}: {e}")

        if not feed_data:
            print(f"Could not fetch feed for @{username} from any source.")
            continue

        items = parse_rss_xml(feed_data)
        if not items:
            continue

        last_id = info.get("last_id", "")
        new_entries = []
        found_last_id = False

        for item in items:
            # Extract tweet ID
            link_or_guid = item.get("guid") or item.get("link") or ""
            tid_match = re.search(r"status/(\d+)", link_or_guid)
            if not tid_match: continue
            tid = tid_match.group(1)
            
            if last_id and tid == last_id:
                found_last_id = True
                break
            new_entries.append((tid, item))

        # Backfill filter
        if not last_id:
            # First run: only capture the latest tweet and set as last_id (no posting history)
            entries_to_process = new_entries[:1]
        elif not found_last_id:
            # Big gap: send at most 3 tweets to avoid spam
            entries_to_process = new_entries[:3]
        else:
            entries_to_process = new_entries

        # Process oldest to newest
        entries_to_process.reverse()

        latest_id = last_id

        for tid, item in entries_to_process:
            desc = item.get("description", "")
            clean_text = clean_tweet_text(desc)
            title_text = clean_tweet_text(item.get("title", ""))
            
            is_rt = title_text.startswith("RT @") or "retweeted" in desc
            is_reply = title_text.startswith("@")

            for chat_id in info["chats"]:
                sent_list = sent_ids.setdefault(chat_id, [])
                if tid in sent_list:
                    continue

                chat_filters = filters_db.get("chats", {}).get(chat_id, {})
                
                # Filter RT
                if is_rt and chat_filters.get("filter_rt", True):
                    continue
                # Filter Reply
                if is_reply and chat_filters.get("filter_reply", True):
                    continue
                # Filter Keywords
                kws = chat_filters.get("keywords", [])
                if kws:
                    text_lower = clean_text.lower()
                    if not any(w in text_lower for w in kws):
                        continue

                # Translate if enabled
                translated = ""
                if os.getenv("TRANSLATE_FA", "true").lower() == "true":
                    translated = translate_fa(clean_text)

                tweet_link = f"https://x.com/{username}/status/{tid}"
                msg_text = format_message(username, clean_text, translated, tweet_link)

                # Get Images
                images = extract_images(desc)

                try:
                    if len(images) == 0:
                        send_msg(chat_id, msg_text)
                    elif len(images) == 1:
                        send_photo(chat_id, images[0], msg_text)
                    else:
                        send_media_group(chat_id, images, msg_text)

                    # Update Sent IDs DB
                    sent_list.append(tid)
                    if len(sent_list) > 500:
                        sent_list.pop(0)
                    save_sent_ids()
                except Exception as ex:
                    print(f"Failed forwarding tweet {tid} to {chat_id}: {ex}")

            latest_id = tid

        if latest_id != last_id:
            tracked[username]["last_id"] = latest_id
            save_tracked()

# ==========================================
# MAIN SINGLE RUNNER FOR GITHUB ACTIONS
# ==========================================
def main():
    if not TOKEN:
        print("TELEGRAM_BOT_TOKEN is missing!")
        return

    print("Step 1: Processing Telegram Commands...")
    process_telegram_commands()

    print("Step 2: Checking Twitter Updates...")
    monitor_twitter_accounts()

    print("Done! Executed successfully.")

if __name__ == "__main__":
    main()
