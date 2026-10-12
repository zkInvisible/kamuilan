# KPSS Radar

Makine Mühendisliği lisans mezunu, **2026 KPSS P3 puanı 78** olan ve yüksek lisansı bulunmayan aday için kamu ilanlarını toplar. Koyu temalı arayüzde arama, kategori, kaynak ve son başvuru tarihine göre sıralama bulunur. Telegram, favoriler, CSV ve bölge önceliği yoktur.

## Hangi ilanlar gösterilir?

- P3 puan türü, en fazla 78 taban puan ve Makine Mühendisliği veya herhangi bir lisans mezuniyetini kabul eden diploma şartı doğrulanmış kadrolar.
- İlan açıkça sınav yılı belirtiyorsa 2026 kabul edilmelidir. Yıl belirtilmiyorsa ayrıca kontrol uyarısı gösterilir.
- Çok kadrolu ilanlarda yalnızca uygun satırlar ve onların kontenjan toplamı gösterilir; diğer mesleklerin şartları uygun kadroya uygulanmaz.
- P93/P94 ve diğer puan türleri, 78 üstü puan, akademik kadro, lisansüstü eğitim, tecrübe, özel kontenjan, beden işçisi/teknisyen/şoför/güvenlik gibi kapsam dışı görevler elenir.
- Diploma veya puan şartı okunamayan ilanlar gizlenir. Açıkça belirtilmiş son başvuru tarihi geçen kayıtlar temizlenir. Yayın tarihinden tahmini başvuru süresi üretilmez.

Yaş, ehliyet, boy/kilo, cinsiyet ve sertifika bilgilerin bilinmediğinden bunlar kesin uygunluk garantisi değil, kontrol edilmesi gereken koşullar olarak sunulur. Memurluktan mühendisliğe geçiş garanti değildir; kurumun kadrosuna ve mevzuata bağlıdır. Başvurmadan önce resmî ilanı mutlaka okuyun.

## Yerelde çalıştırma

Python 3.11 veya üzeri gerekir. PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Adres: http://127.0.0.1:5000 . Uygulama açılıştan 3 saniye sonra taramayı başlatır ve çalıştığı sürece 480 dakikada bir tekrarlar. “İlanları güncelle” düğmesi taramayı başlatır; eşzamanlı taramalar engellenir ve manuel taramalar 15 dakika arayla sınırlandırılır. Temel ayarlar kodda varsayılan, Render için `render.yaml` içinde tanımlıdır. Yerelde otomatik taramayı kapatmak için `RUN_SCHEDULER=false` kullanılır.

## Render Free kurulumu

1. Kaynak dosyaları GitHub deposuna yükleyin; `.env`, SQLite veritabanı ve sanal ortamı yüklemeyin. `.gitignore` bunları dışarıda tutar.
2. Render'da **New → Blueprint** ile depoyu seçin. `render.yaml`, ücretsiz Python servisini, bağımlılık kurulumunu ve Gunicorn başlangıcını tanımlar.
3. Alternatif elle kurulum: build `pip install -r requirements.txt`, start `gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`, health check `/health`.
4. cron-job.org üzerinden `https://SERVIS-ADINIZ.onrender.com/health` adresine 5–10 dakikada bir GET gönderin. Bu çağrı hafiftir; taramayı kendisi başlatmaz. Zamanlayıcı uygulamanın içinde çalışır.

Free servis uyuyabilir ve dosya sistemi kalıcı değildir. Ping kalıcı depolama sağlamaz; yeniden başlatmada SQLite kaybolabilir, ilk taramayla liste yeniden oluşturulur. Servis çalışmıyorken 8 saatlik tarama gerçekleşmez. Birden fazla Gunicorn worker açmayın; her worker ayrı zamanlayıcı başlatabilir. [Render Free sınırlamaları](https://render.com/docs/free).

## Kaynaklar ve sınırlar

Memurlar.net, Kamu İlan/SBB, Resmî Gazete, Basın İlan Kurumu, MSB/SSB ve İŞKUR kamu memur duyuruları sınırlı sayıda sayfa üzerinden kontrol edilir. SSB'nin en yeni 24 duyurusu kontrol edilir; sonuç/erteleme/kayıt duyuruları başvuru ilanı olarak alınmaz. Bu, bütün Türkiye ilanlarının eksiksiz arşivi değildir. Kaynakların erişim hataları arayüzde “Kaynak durumu” bölümünde görünür; erişilemeyen kaynak boş ilan listesi gibi sunulmaz.

Kaynak sayfaları sırayla okunur; aynı anda yalnızca bir ilan belgesi indirilip incelenir ve yeni belgeler arasında kısa ara verilir. Bir taramada en fazla 60 belge kontrol edilir. Önce bilinen uygun ilanlar yenilenir, ardından doğrulanmamış/eski kayıtlar okunur. Bütçeyi aşanlar sonraki tura ertelenir; manuel güncelleme de kalanları kontrol eder. İŞKUR'da tur başına en fazla 24 aktif il sayfası okunur; daha fazlası varsa sıradaki turlara dönüşümlü bırakılır. Detay önbelleği 8 saatte geçersizleşir. Eski ya da okunamayan şartlar doğrulanmış uygun ilan olarak gösterilmez. PDF'nin ilk 8 sayfası işlenir; görüntü taraması OCR ile çözülmez ve belirsiz tablolar gizlenebilir. Bu sınırlar Render Free kaynak tüketimini azaltır ancak uygun ilan kaçırılmasına yol açabilir.

SBB belgeleri normal kamu portalı oturumu ve yönlendirme bilgisiyle alınır. İlan bağlantısı PDF olmayan bir hata sayfası döndürürse belge olarak sunulmaz; kullanıcıya kaynak bağlantısı verilir. Erişim engelleri/ağ hataları için başarı garantisi yoktur.

## Testler

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

Testler ayrı geçici veritabanı kullanır, normal ilan verisini değiştirmez. Filtreler, karışık kadro tabloları, sınav yılı, kesin tarihler, önbellek, mükerrer belgeler, kaynak hataları ve PDF doğrulaması test edilir. `/api/scan/status` son taramanın kaynak bazlı sonucunu verir.
