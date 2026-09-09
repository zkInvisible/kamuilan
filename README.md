# 🎯 KPSS P3 & Kamu İlanları Radar Sistemi

Türkiye'deki kamu personeli ve memur alım ilanlarını (KPSS P3 puan türü, özellikle **Makine Mühendisliği** lisans mezuniyeti, 80 puan ve 0 yıl tecrübe odaklı) 7/24 düzenli tarayan, akıllıca etiketleyen, Telegram üzerinden günlük bülten gönderen ve modern bir web panelinde listeleyen uçtan uca otonom otomasyon sistemi.

---

## 🚀 Öne Çıkan Özellikler

1. **5 Resmi Kamu Veri Kaynağı:**
   - `ilan.memurlar.net` (En güncel kamu sözleşmeli personel ve memur alımları)
   - `kamuilan.sbb.gov.tr` (Cumhurbaşkanlığı Strateji ve Bütçe Başkanlığı Kamu İlan Portalı)
   - `resmigazete.gov.tr` (T.C. Resmî Gazete günlük "Çeşitli İlanlar" bölümü ve PDF doğrulama)
   - `iskur.gov.tr` (İŞKUR Kamu Memur ve Kurum Dışı İşçi Alımları - EK-1 formları ve taranmış evrak OCR analizi)
   - `ilan.gov.tr` (Basın İlan Kurumu Kamu İlanları)
2. **Kişiselleştirilmiş Aday Uygunluk Motoru:**
   - **Makine Mühendisliği Lisans:** Sadece Makine/Mekatronik/Otomotiv/Enerji gibi uygun mühendislikler seçilir; İnşaat, Jeoloji, Ziraat, Elektrik gibi alakasız mühendislikler elenir.
   - **Yeni Mezun (0 Yıl Tecrübe):** SGK dökümlü tecrübe, sektör deneyimi veya bonservis arayan tüm ilanlar `TECRUBE_SARTI` ile filtrelenir.
   - **Akademik Olmayan Kadrolar:** Doçentlik, profesörlük, öğretim görevlisi, tezli yüksek lisans şartlı kadrolar elenir.
   - **Temiz Kadrolar:** Vasıfsız beden işçiliği (çöp, temizlik, şoförlük, hizmetli vb.) ve özel kontenjanlar (engelli, eski hükümlü) filtrelenir.
3. **Kalan Gün Geri Sayım Sayacı (Son Başvuru Tarihleri):**
   - İlan metinlerindeki başvuru sürelerini otomatik ayrıştırıp `🔥 Son 1 Gün!`, `⏳ Son 3 Gün!`, `📅 7 Gün Kaldı` aciliyet rozetleriyle gösterir.
4. **Favori Takip Listesi & Hızlı Önizleme Modalı:**
   - Kartlardaki ⭐ butonuyla takip listesi oluşturabilir, detay modalında kadroları ve başvuru tavsiyesini inceleyebilirsiniz.
5. **Excel / CSV Dışa Aktarma:**
   - İlanları tek tıkla Excel Türkçe karakter uyumlu (UTF-8 BOM) CSV olarak indirebilirsiniz.
6. **7/24 Otonom Çalışma ve Otomatik Keep-Alive:**
   - Render Free Tier uyku modunu (spin-down) engelleyen dahili otonom Keep-Alive iş parçacığı.
   - Veritabanı boşsa ilk taramayı beklemeden başlatan otomatik tetikleyici.

---

## 📁 Proje Dosya Yapısı

```text
kpss/
├── main.py                 # Flask web sunucusu, API rotaları, APScheduler ve Keep-Alive
├── scraper.py              # 5 kaynaktan kazıma, RapidOCR, PDF analizi ve aday uygunluk motoru
├── database.py             # SQLite veritabanı, favoriler, CSV dışa aktarma ve CRUD
├── telegram_notifier.py    # Telegram Bot API üzerinden bülten oluşturma ve mesaj iletimi
├── templates/
│   └── index.html          # Aydınlık temalı, responsive Tailwind CSS modern web dashboard'u
├── requirements.txt        # Gunicorn, RapidOCR, OpenCV Headless ve tüm kütüphaneler
├── render.yaml             # Render.com 1-tıkla Blueprint dağıtım yapılandırması
├── Procfile                # Render.com WSGI başlatma komutu
├── .python-version         # Python 3.11.9 sürüm kilidi
├── .env                    # Yerel ortam yapılandırma dosyası
└── test_system.py          # Otomatik birim ve entegrasyon test paketi
```

---

## 🛠️ Yerel Kurulum ve Çalıştırma

```bash
# 1. Bağımlılıkları yükleyin
pip install -r requirements.txt

# 2. Sistemi başlatın
python main.py
```
Tarayıcınızda 👉 **http://127.0.0.1:5000** adresini açın!

---

## 🧪 Testleri Çalıştırma

```bash
python -m unittest test_system.py
```

---

## ☁️ Render.com Üzerinde 7/24 Canlıya Alma (Adım Adım)

Projede `render.yaml` Blueprint dosyası ve `Procfile` hazır tanımlıdır.

### 1. Adım: GitHub'a Gönderme
```bash
git init
git add .
git commit -m "Render.com icin hazirlandi"
git branch -M main
git remote add origin https://github.com/KULLANICI_ADINIZ/kpss-radar.git
git push -u origin main
```

### 2. Adım: Render.com Blueprint ile 1 Tıkta Dağıtım
1. [Render.com](https://render.com) hesabınıza giriş yapın.
2. Sağ üstten **New +** butonuna tıklayın ve **Blueprint** seçin.
3. GitHub reponuzu seçin (`kpss-radar`).
4. Render `render.yaml` dosyasını otomatik algılayacaktır.
5. Sorulan ortam değişkenlerini girin:
   - `TELEGRAM_BOT_TOKEN`: BotFather'dan aldığınız token.
   - `TELEGRAM_CHAT_ID`: Telegram chat id'niz.
6. **Apply** butonuna tıklayın!

### 3. Adım: Cronjob ile 7/24 Uyanık Tutma
- Render servisiniz ayağa kalktığında ilk tarama otomatik başlayacaktır.
- Harici Cronjob servisinizden (cron-job.org, UptimeRobot vb.) her 5-10 dakikada bir `https://sizin-servisiniz.onrender.com/health` veya `/ping` adresine GET isteği atarak servisin uykuya geçmesini engelleyin.
- Sistem saatlik olarak 5 resmi kaynaktan ilanları taramaya ve her gün saat 11:00'de Telegram'a bülten göndermeye devam edecektir.
