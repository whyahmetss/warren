# Warren Bot V4 - Kurulum Rehberi

## 1. Twelve Data API Key Al (Bedava)
- twelvedata.com'a git
- Ucretsiz kayit ol
- Dashboard'dan API key kopyala

## 2. GitHub'a Yukle
- github.com'da yeni repo ac: "warrenbot"
- bot.py ve requirements.txt yukle

## 3. Render.com Deploy
- render.com → GitHub ile giris
- "New Web Service" → repo sec
- Ayarlar:
  - Runtime: Python 3
  - Build: pip install -r requirements.txt
  - Start: python bot.py

## 4. Environment Variables (Render Dashboard → Environment)
TG_TOKEN   = 8698295551:AAFLixj0p8t7REyHcIkXnSp0gChNf6bNk6w
TG_CHAT_ID = -1003838635441
TD_API_KEY = [Twelve Data'dan aldigin key]

## 5. Deploy!
"Create Web Service" → 2-3 dk bekle → bot calisiyor!

## Komutlar
/durum        - Bot durumu
/fiyat        - Anlik Gold ve US100 fiyati
/analiz       - ICT analizi (ornek: /analiz XAUUSD)
/sinyal       - Manuel sinyal tara
/istatistik   - Win/Loss istatistigi
/ac           - Botu ac
/kapat        - Botu kapat

## Grup Yonetimi (reply yaparak kullan)
/kick         - Gruptan at
/ban          - Kalici ban
/unban        - Bani kaldir
/mute [dk]    - Sustur (varsayilan 10dk)
/unmute       - Sesi ac
/uyar [sebep] - Uyar (3 uyari = otomatik ban)
/uyarlar      - Uyari sayisini gor

## Otomatik Ozellikler
- Her 60 saniyede Gold ve US100 taranir
- ICT: OB + FVG + Sweep + BOS + OTE (min 2 confluance)
- Spam koruması: 10sn'de 8 mesaj = 5dk mute
- Yeni uye karsilama mesaji
