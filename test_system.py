import unittest
import os
import json
import tempfile
import database as db
import scraper
import telegram_notifier
from main import app

class TestKPSSSystem(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.temp_db = tempfile.mkstemp(suffix=".db")
        db.DB_PATH = self.temp_db
        db.init_db()
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        os.close(self.db_fd)
        if os.path.exists(self.temp_db):
            os.remove(self.temp_db)

    def test_database_insert_and_deduplication(self):
        job1 = {
            "title": "Ondokuz Mayıs Üniversitesi 2 Makine Mühendisi Alacak",
            "institution": "Ondokuz Mayıs Üniversitesi",
            "url": "https://test.com/ilan/1",
            "source": "Memurlar.Net",
            "summary": "Samsun OMÜ Makine Mühendisliği KPSS P3 80 alımı",
            "tags": ["SAMSUN_MUHENDIS", "SAMSUN_HEPSI", "MAKINE_MUHENDIS"],
            "published_date": "10.09.2026"
        }
        job_id, is_new = db.insert_job(job1)
        self.assertTrue(is_new)
        self.assertIsNotNone(job_id)

        job_id2, is_new2 = db.insert_job(job1)
        self.assertFalse(is_new2)
        self.assertEqual(job_id, job_id2)

        samsun_jobs = db.get_jobs(tag="SAMSUN_MUHENDIS")
        self.assertEqual(len(samsun_jobs), 1)
        self.assertEqual(samsun_jobs[0]["institution"], "Ondokuz Mayıs Üniversitesi")

    def test_favorites_and_sorting(self):
        job1 = {
            "title": "Samsun Üniversitesi Makine Mühendisi",
            "institution": "SAMÜ",
            "url": "https://test.com/ilan/fav1",
            "source": "Memurlar.Net",
            "tags": ["SAMSUN_MUHENDIS"],
            "published_date": "15 Eylül 2026"
        }
        job_id, _ = db.insert_job(job1)
        
        # Başlangıçta favori değil
        fav_jobs = db.get_jobs(only_favorites=True)
        self.assertEqual(len(fav_jobs), 0)

        # Favoriye ekle
        is_fav = db.toggle_favorite(job_id)
        self.assertTrue(is_fav)

        fav_jobs_after = db.get_jobs(only_favorites=True)
        self.assertEqual(len(fav_jobs_after), 1)
        self.assertEqual(fav_jobs_after[0]["id"], job_id)

        # API üzerinden toggle testi
        res = self.client.post("/api/jobs/favorite", json={"job_id": job_id})
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertFalse(data["is_favorite"]) # Tekrar tıklandığında false olmalı

    def test_deadline_calculation(self):
        info = scraper.parse_deadline_info("Son başvuru tarihi 14 Eylül 2026")
        self.assertIsNotNone(info["deadline_iso"])
        self.assertIsNotNone(info["days_left"])
        self.assertIn("Gün", info["badge_text"])

        # Tarih aralığı testi (Bitiş tarihini almalı)
        info_range = scraper.parse_deadline_info("08.09.2026 - 22.09.2026")
        self.assertEqual(info_range["deadline_iso"], "2026-09-22")
        self.assertNotEqual(info_range["badge_type"], "expired")

        # Resmi Gazete testi (Yayım tarihinden itibaren +15 gün olmalı, düne ait ilan süresi doldu sayılmamalı)
        info_rg = scraper.parse_deadline_info("07.09.2026", source="Resmi Gazete")
        self.assertEqual(info_rg["deadline_iso"], "2026-09-22")
        self.assertNotEqual(info_rg["badge_type"], "expired")

        # Basın İlan Kurumu testi
        info_bik = scraper.parse_deadline_info("06.09.2026", source="Basın İlan Kurumu")
        self.assertEqual(info_bik["deadline_iso"], "2026-09-21")
        self.assertNotEqual(info_bik["badge_type"], "expired")

    def test_classification_logic(self):
        tags1 = scraper.classify_job(
            title="Samsun Büyükşehir Belediyesi 3 Makine Mühendisi Alımı",
            institution="Samsun Büyükşehir",
            summary="Samsun ilinde istihdam edilmek üzere lisans KPSS P3 ile makine mühendisi alınacaktır."
        )
        self.assertIn("SAMSUN_MUHENDIS", tags1)
        self.assertIn("MAKINE_MUHENDIS", tags1)
        self.assertIn("SAMSUN_HEPSI", tags1)
        self.assertIn("BELEDIYE_MAHALLI", tags1)
        self.assertNotIn("TURKIYE_MUHENDIS", tags1)

        tags2 = scraper.classify_job(
            title="Samsun Atakum Belediyesi 10 Zabıta Memuru Alacak",
            institution="Atakum Belediyesi",
            summary="Atakum ilçesi için memur alımı yapılacaktır."
        )
        self.assertIn("SAMSUN_HEPSI", tags2)
        self.assertIn("BELEDIYE_MAHALLI", tags2)
        self.assertNotIn("SAMSUN_MUHENDIS", tags2)

        tags3 = scraper.classify_job(
            title="DSİ Genel Müdürlüğü 25 Mühendis Alacak",
            institution="Devlet Su İşleri",
            summary="Merkez ve taşra teşkilatına mühendis kadroları."
        )
        self.assertIn("MAKINE_MUHENDIS", tags3)
        self.assertIn("KIT_KURUMLARI", tags3)
        self.assertNotIn("SAMSUN_MUHENDIS", tags3)

        tags4 = scraper.classify_job(
            title="Çevre Bakanlığı 15 Uzman Yardımcısı Alacak",
            institution="Bakanlık",
            summary="KPSS P3 puan türünden en az 70 puan almış lisans mezunu adaylar arasından uzman yardımcısı."
        )
        self.assertIn("KARIYER_MESLEK", tags4)
        self.assertIn("BANA_UYGUN_DIGER", tags4)

        # Uyumsuz mühendislikler (İnşaat, Bilgisayar, Ziraat vb.) Makine Mühendisine elenmelidir
        tags5 = scraper.classify_job(
            title="Saray Belediyesi 1 Bilgisayar Mühendisi 1 Ulaştırma Mühendisi Alacak",
            institution="Saray Belediyesi",
            summary="Bilgisayar ve ulaştırma mühendisliği mezunları"
        )
        self.assertNotIn("TURKIYE_MUHENDIS", tags5)
        self.assertNotIn("MAKINE_MUHENDIS", tags5)

    def test_telegram_message_formatting(self):
        sample_jobs = [
            {
                "title": "OMÜ 2 Makine Mühendisi",
                "institution": "Ondokuz Mayıs Üniversitesi",
                "url": "https://ilan.memurlar.net/ilan/1",
                "tags": ["SAMSUN_MUHENDIS"],
                "published_date": "15 Eylül"
            }
        ]
        messages = telegram_notifier.format_job_bulletin(sample_jobs)
        self.assertTrue(len(messages) >= 1)
        self.assertIn("SAMSUN MÜHENDİSLİK KADROLARI", messages[0])

    def test_flask_routes_and_csv_export(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)

        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data["status"], "healthy")

        res = self.client.get("/api/jobs")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data["status"], "success")
        self.assertIn("stats", data)

        # CSV İndirme Testi
        res_csv = self.client.get("/api/export-csv")
        self.assertEqual(res_csv.status_code, 200)
        self.assertEqual(res_csv.mimetype, "text/csv")
        self.assertIn("İlan Başlığı", res_csv.data.decode("utf-8"))

        # Detay API Testi
        job_id, _ = db.insert_job({
            "title": "Samsun Üniversitesi 2 Makine Mühendisi Alacak",
            "institution": "SAMÜ",
            "url": "https://test.com/ilan/detail-test",
            "source": "Memurlar.Net",
            "summary": "Makine mühendisi alımı",
            "tags": ["SAMSUN_MUHENDIS"]
        })
        res_det = self.client.get(f"/api/jobs/{job_id}/detail")
        self.assertEqual(res_det.status_code, 200)
        det_data = json.loads(res_det.data)
        self.assertEqual(det_data["status"], "success")
        self.assertIn("cadres", det_data["detail"])
        self.assertIn("candidate_advice", det_data["detail"])

    def test_new_sources_scraper_integration(self):
        # 1. Basın İlan Kurumu testi
        bik_jobs = scraper.scrape_basin_ilan_kurumu(max_pages=1)
        self.assertIsInstance(bik_jobs, list)
        if bik_jobs:
            first_bik = bik_jobs[0]
            self.assertEqual(first_bik["source"], "Basın İlan Kurumu")
            self.assertIn("title", first_bik)
            self.assertIn("institution", first_bik)
            self.assertIn("url", first_bik)
            self.assertIn("tags", first_bik)

        # 2. Savunma Sanayii / MSB testi
        ssb_jobs = scraper.scrape_savunma_msb()
        self.assertIsInstance(ssb_jobs, list)
        if ssb_jobs:
            first_ssb = ssb_jobs[0]
            self.assertEqual(first_ssb["source"], "Savunma Sanayii / MSB")
            self.assertIn("title", first_ssb)
            self.assertIn("url", first_ssb)

        # 3. İŞKUR Kamu İlanları testi
        iskur_jobs = scraper.scrape_iskur_kamu(max_cities=3)
        self.assertIsInstance(iskur_jobs, list)
        if iskur_jobs:
            first_iskur = iskur_jobs[0]
            self.assertEqual(first_iskur["source"], "İŞKUR Kamu İlanları")
            self.assertIn("title", first_iskur)
            self.assertIn("url", first_iskur)

    def test_experience_requirement_detection(self):
        # Tecrübe şartı arayan metinler
        t1, n1 = scraper.check_experience_requirement("Adayların en az 2 yıl mesleki tecrübeye sahip olduğunu SGK dökümü ile belgelemesi gerekir.")
        self.assertTrue(t1)
        self.assertIn("tecrübe", n1.lower())

        t2, n2 = scraper.check_experience_requirement("En az 3 yıl şantiye tecrübesi olan makine mühendisi alınacaktır.")
        self.assertTrue(t2)

        t3, n3 = scraper.check_experience_requirement("Kamu veya özel sektörde fiilen çalışmış olmak ve çalışma belgesi sunmak.")
        self.assertTrue(t3)

        # Yeni mezun / Tecrübesiz metinler
        t4, n4 = scraper.check_experience_requirement("Ondokuz Mayıs Üniversitesi KPSS P3 ile makine mühendisi alacaktır.")
        self.assertFalse(t4)

        t5, n5 = scraper.check_experience_requirement("Bu kadro için herhangi bir tecrübe şartı aranmamaktadır. Yeni mezunlar başvurabilir.")
        self.assertFalse(t5)

        t6, n6 = scraper.check_experience_requirement("Staj yapmış olmak tercih sebebidir ancak tecrübe şartı aranmaz.")
        self.assertFalse(t6)

    def test_experience_filtering_in_db_and_api(self):
        # 1. Yeni mezuna uygun ilan
        job_fresh = {
            "title": "Samsun Üniversitesi Makine Mühendisi Alımı",
            "institution": "SAMÜ",
            "url": "https://test.com/ilan/fresh-1",
            "source": "Memurlar.Net",
            "summary": "Tecrübe şartı aranmamaktadır. KPSS P3 80.",
            "tags": ["SAMSUN_MUHENDIS", "MAKINE_MUHENDIS"],
            "requires_experience": 0,
            "experience_note": ""
        }
        id_fresh, _ = db.insert_job(job_fresh)

        # 2. Tecrübe şartı olan ilan
        job_exp = {
            "title": "Büyükşehir Belediyesi 5 Yıl Deneyimli Mühendis Alımı",
            "institution": "Büyükşehir",
            "url": "https://test.com/ilan/exp-1",
            "source": "Memurlar.Net",
            "summary": "En az 5 yıl şantiye tecrübesi ve SGK dökümü şarttır.",
            "tags": ["MAKINE_MUHENDIS", "TECRUBE_SARTI"],
            "requires_experience": 1,
            "experience_note": "5 yıl deneyim şartı"
        }
        id_exp, _ = db.insert_job(job_exp)

        # DB get_jobs tecrübe filtreli çağırma
        fresh_jobs = db.get_jobs(exclude_experience=True)
        job_ids = [j["id"] for j in fresh_jobs]
        self.assertIn(id_fresh, job_ids)
        self.assertNotIn(id_exp, job_ids)

        # Telegram unnotified jobs
        unnotified = db.get_unnotified_jobs()
        unnotified_ids = [j["id"] for j in unnotified]
        self.assertIn(id_fresh, unnotified_ids)
        self.assertNotIn(id_exp, unnotified_ids)

        # Web API testi (/api/jobs tecrübe şartlı olanı eler)
        res = self.client.get("/api/jobs")
        self.assertEqual(res.status_code, 200)
        api_data = json.loads(res.data)
        api_job_ids = [j["id"] for j in api_data["jobs"]]
        self.assertIn(id_fresh, api_job_ids)
        self.assertNotIn(id_exp, api_job_ids)

        # Telegram bülten formatlayıcı testi (Tecrübe şartlı olanlar bültende yer almaz)
        bulletin_msgs = telegram_notifier.format_job_bulletin([job_fresh, job_exp])
        self.assertTrue(len(bulletin_msgs) >= 1)
        self.assertIn("Samsun Üniversitesi", bulletin_msgs[0])
        self.assertNotIn("5 Yıl Deneyimli", bulletin_msgs[0])

if __name__ == "__main__":
    unittest.main()


