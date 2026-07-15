import os
import json
import re
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CF_WORKER_URL = os.getenv("CF_WORKER_URL", "").rstrip("/")
CF_API_SECRET = os.getenv("CF_API_SECRET", "")

# ==========================================
# CLOUDFLARE KV DATABASE API WRAPPERS
# ==========================================
def load_json_from_cf(key_name, default_val):
    if not CF_WORKER_URL or not CF_API_SECRET:
        print("Warning: CF_WORKER_URL or CF_API_SECRET is missing. Using default local fallback.")
        # Fallback to local files
        if os.path.exists(f"{key_name}.json"):
            try:
                with open(f"{key_name}.json", "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                pass
        return default_val
    try:
        url = f"{CF_WORKER_URL}/api/kv?key={key_name}"
        headers = {"X-API-Key": CF_API_SECRET}
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200:
            print(f"Loaded '{key_name}' successfully from Cloudflare KV.")
            return r.json()
        else:
            print(f"Warning: Failed to load '{key_name}' from KV, status: {r.status_code}. Using local fallback.")
    except Exception as e:
        print(f"Error loading '{key_name}' from KV: {e}")
        
    # Local fallback
    if os.path.exists(f"{key_name}.json"):
        try:
            with open(f"{key_name}.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return default_val

def save_json_to_cf(key_name, data):
    # Save locally
    try:
        with open(f"{key_name}.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Failed local write for {key_name}: {e}")

    if not CF_WORKER_URL or not CF_API_SECRET:
        print("Warning: CF_WORKER_URL or CF_API_SECRET is missing. Cannot save to Cloudflare.")
        return
    try:
        url = f"{CF_WORKER_URL}/api/kv?key={key_name}"
        headers = {"X-API-Key": CF_API_SECRET, "Content-Type": "application/json"}
        r = requests.post(url, headers=headers, json=data, timeout=12)
        if r.status_code == 200:
            print(f"Saved '{key_name}' successfully to Cloudflare KV.")
        else:
            print(f"Error: Failed to save '{key_name}' to KV, status: {r.status_code}")
    except Exception as e:
        print(f"Error saving '{key_name}' to KV: {e}")

# Load databases from Cloudflare KV
tracked = load_json_from_cf("tracked_users", {})
sent_ids = load_json_from_cf("sent_ids", {})

# We no longer need to process commands in GitHub Actions!
# Cloudflare Worker processes commands instantly (via Webhook) and writes them to KV!
# GitHub Actions only wakes up to check Twitter, fetch the lists from KV, and forward new tweets.
# This provides INSTANT commands and RELIABLE Twitter scraping!

def save_tracked():
    save_json_to_cf("tracked_users", tracked)

def save_sent_ids():
    save_json_to_cf("sent_ids", sent_ids)

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
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "media": media
        }, timeout=15)
    except Exception as e:
        print(f"Error sending media group to {chat_id}: {e}")

# ==========================================
# TWITTER SCRAPING & FORWARDING LOOP
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
        print("No tracked accounts found in database.")
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

                # Load settings from Cloudflare KV (instantly updated by CF Webhook)
                # Fallback to defaults if missing
                try:
                    r_settings = requests.get(f"{CF_WORKER_URL}/api/kv?key=chat_settings:{chat_id}", headers={"X-API-Key": CF_API_SECRET}, timeout=10)
                    chat_filters = r_settings.json() if r_settings.status_code == 200 else {}
                except:
                    chat_filters = {}
                
                # Filter RT
                if is_rt and chat_filters.get("filter_rt", False):
                    continue
                # Filter Reply
                if is_reply and chat_filters.get("filter_reply", False):
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

    print("Checking Twitter Updates...")
    monitor_twitter_accounts()
    print("Done! Executed successfully.")

if __name__ == "__main__":
    main()
