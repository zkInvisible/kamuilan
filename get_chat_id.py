import os
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

if not token:
    print("❌ HATA: .env dosyasında TELEGRAM_BOT_TOKEN tanımlı değil!")
    exit(1)

print(f"🔍 Telegram Bot Kontrol Ediliyor (Token: {token[:10]}...):")
url = f"https://api.telegram.org/bot{token}/getUpdates"

try:
    r = requests.get(url, timeout=10)
    data = r.json()
    if not data.get("ok"):
        print(f"❌ Telegram Hatası: {data.get('description')}")
        exit(1)

    results = data.get("result", [])
    if not results:
        print("\n⚠️ Henüz bota bir mesaj atılmamış!")
        print("👉 Lütfen Telegram'da botunuzu açın ve /start yazarak bir mesaj gönderin.")
        print("👉 Ardından bu komutu tekrar çalıştırın: python get_chat_id.py\n")
    else:
        # En son mesajı atan kullanıcının chat id'sini al
        last_update = results[-1]
        msg = last_update.get("message") or last_update.get("my_chat_member", {})
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        username = chat.get("username", "Bilinmeyen")
        first_name = chat.get("first_name", "")

        print("\n✅ TEBRİKLER! Chat ID Tespit Edildi:")
        print(f"   👤 Kullanıcı: {first_name} (@{username})")
        print(f"   🆔 CHAT ID: {chat_id}")
        print("\n👉 .env dosyanızdaki TELEGRAM_CHAT_ID kısmına bu numarayı yazın:")
        print(f"   TELEGRAM_CHAT_ID={chat_id}\n")

except Exception as e:
    print(f"❌ Bağlantı hatası: {e}")
