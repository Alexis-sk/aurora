"""Tests hors ligne : python -m unittest discover tests"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import forecast as f  # noqa: E402

UTC = timezone.utc
FIX = ROOT / "tests" / "fixtures"


class KpScore(unittest.TestCase):
    def test_paliers(self):
        cases = {0: 0, 2: 0.2, 4: 0.4, 4.5: 0.625, 5: 0.85, 5.5: 0.925, 6: 1.0, 8: 1.0, -1: 0}
        for kp, expected in cases.items():
            self.assertAlmostEqual(f.kp_score(kp), expected, places=6, msg=f"Kp {kp}")

    def test_monotone(self):
        values = [f.kp_score(x / 100) for x in range(0, 900)]
        self.assertEqual(values, sorted(values))


class Parsers(unittest.TestCase):
    def test_noaa_reel(self):
        issued, kp = f.parse_noaa((FIX / "real_2026-10-14" / "noaa.txt").read_text())
        self.assertEqual(issued, datetime(2026, 10, 14, 12, 30, tzinfo=UTC))
        self.assertEqual(len(kp), 24)
        self.assertEqual(kp[datetime(2026, 10, 14, 21, tzinfo=UTC)], 3.67)
        self.assertEqual(kp[datetime(2026, 10, 16, 0, tzinfo=UTC)], 2.33)

    def test_noaa_annotations_tempete(self):
        _, kp = f.parse_noaa((FIX / "storm_synthetic" / "noaa.txt").read_text())
        self.assertEqual(kp[datetime(2026, 10, 17, 21, tzinfo=UTC)], 6.0)   # "6.00 (G2)"
        self.assertEqual(kp[datetime(2026, 10, 18, 0, tzinfo=UTC)], 5.67)

    def test_noaa_passage_annee(self):
        text = (":Issued: 2026 Dec 31 1230 UTC\n\nNOAA Kp index breakdown Dec 31-Jan 02 2027\n\n"
                "             Dec 31       Jan 01       Jan 02\n"
                + "".join(f"{h:02d}-{(h + 3) % 24:02d}UT   1.00   2.00   3.00\n" for h in range(0, 24, 3)))
        _, kp = f.parse_noaa(text)
        self.assertIn(datetime(2026, 12, 31, 0, tzinfo=UTC), kp)
        self.assertIn(datetime(2027, 1, 2, 21, tzinfo=UTC), kp)

    def test_gfz(self):
        g = f.parse_gfz((FIX / "storm_synthetic" / "gfz.json").read_text())
        self.assertEqual(min(g), datetime(2026, 10, 16, tzinfo=UTC))
        self.assertEqual(set(next(iter(g.values()))), {"median", "q25", "q75", "min", "max"})


class Samedi(unittest.TestCase):
    def test_next_saturday(self):
        self.assertEqual(f.next_saturday(date(2026, 10, 15)), date(2026, 10, 17))  # jeudi
        self.assertEqual(f.next_saturday(date(2026, 10, 17)), date(2026, 10, 17))  # samedi même

    def run_scenario(self, name, now):
        with tempfile.TemporaryDirectory() as d:
            code = f.run(Path(d), datetime.fromisoformat(now).replace(tzinfo=UTC), FIX / name, "")
            data = json.loads((Path(d) / "data.json").read_text())
            html = (Path(d) / "index.html").read_text()
        return code, data, html

    def test_samedi_vide_et_gfz_perime(self):
        code, data, html = self.run_scenario("real_2026-10-14", "2026-10-14T13:15")
        self.assertEqual(code, 0)
        self.assertIsNone(data["recommendation"])
        self.assertIn("pas encore de données NOAA", html)
        self.assertTrue(any("GFZ périmé" in w for w in data["warnings"]))
        self.assertEqual(data["alerts"], [])

    def test_tempete_vendredi_soir(self):
        code, data, html = self.run_scenario("storm_synthetic", "2026-10-16T17:15")
        self.assertEqual(code, 0)
        self.assertEqual(data["recommendation"]["key"], "grindavik")   # ciel dégagé vs Tromsø couvert
        self.assertEqual(len(data["alerts"]), 4)
        self.assertIn("Grindavík : UTC+0", html)
        self.assertEqual(data["saturday"]["noaa_slots"], 6)
        self.assertNotIn("Nuit partielle", html)

    def test_fichier_alerte(self):
        with tempfile.TemporaryDirectory() as d:
            alert = Path(d) / "alert.md"
            f.run(Path(d) / "a", datetime(2026, 10, 16, 17, 15, tzinfo=UTC), FIX / "storm_synthetic", "https://x", alert)
            body = alert.read_text()
            self.assertIn("**Kp 6**", body)
            self.assertIn("sam. 17/10 21h-00h UTC", body)
            f.run(Path(d) / "b", datetime(2026, 10, 14, 13, 15, tzinfo=UTC), FIX / "real_2026-10-14", "", alert)
            self.assertFalse(alert.exists())  # plus d'alerte -> fichier supprimé

    def test_meteo_et_accord_modeles(self):
        code, data, html = self.run_scenario("storm_synthetic", "2026-10-16T17:15")
        slot = data["saturday"]["slots"][2]  # 21h-00h UTC
        g, t = slot["sites"]["grindavik"], slot["sites"]["tromso"]
        self.assertEqual(g["models"], {"dmi_seamless": 15.0, "ecmwf_ifs": 5.0})
        self.assertEqual(g["agreement"], "bon")
        self.assertEqual(t["agreement"], "faible")            # 95 vs 30
        self.assertEqual(g["precip_mm"], 0.3)                 # 3 h x 0,1 mm
        self.assertEqual((g["clouds_low"], g["gust_kmh"], g["temp_c"]), (5.0, 35.0, 1.5))
        self.assertIsNone(t["clouds_low"])                    # variable absente du fixture
        self.assertTrue(any("icon_seamless" in e for e in data["model_errors"]))
        self.assertEqual(data["errors"], [])                  # un modèle en échec n'est pas une erreur bloquante
        self.assertIn('class="dot agr-bon"', html)
        self.assertIn("DMI 15 % · ECMWF 5 %", html)                # détail des modèles au toucher
        self.assertIn("Accord des modèles météo", html)
        self.assertNotIn("<th>Lune</th>", html)
        self.assertNotIn("heures locales", html)                   # tout en UTC
        self.assertRegex(html, r"Lune \d+ %</strong> — <strong>Tromsø</strong> : (lever .* UTC|levée|sous)")
        self.assertEqual(data["recommendation"]["key"], "grindavik")

    def test_liens_reservation(self):
        links = f.booking_links(date(2026, 10, 31))
        self.assertEqual(list(links), ["tromso", "keflavik"])          # Tromsø en premier
        t, k = links["tromso"], links["keflavik"]
        self.assertIn("/transport/vols/mrs/tos/261031/261101/", t["flights"][0]["url"])
        self.assertEqual([f["url"].split("/vols/")[1][:7] for f in k["flights"]], ["mrs/kef", "mrs/cdg", "cdg/kef"])
        self.assertIn("/transport/vols/cdg/kef/261031/261101/", k["flights"][2]["url"])
        cars = {c["label"]: c["url"] for c in t["cars"]}
        self.assertIn("hire-location=TOS&return-location=TOS&date-from=31%2F10%2F2026&time-from=1500"
                      "&date-to=01%2F11%2F2026&time-to=1300", cars["Avis"])
        self.assertIn("pid=TOST50&did=TOST50&pdate=2026-10-31T15%3A00%3A00&ddate=2026-11-01T13%3A00%3A00", cars["Hertz"])
        self.assertIn("checkin=2026-10-31&checkout=2026-11-01", k["hotel"])
        self.assertIn("dest_id=338", k["hotel"])
        kcars = {c["label"]: c["url"] for c in k["cars"]}
        self.assertIn("pickupTime=T15%3A00%3A00.000Z", kcars["carrental.is"])
        self.assertNotIn("carrental.is", cars)  # pas pour Tromsø

    def test_page_reservation(self):
        with tempfile.TemporaryDirectory() as d:
            f.run(Path(d), datetime(2026, 10, 28, 9, 0, tzinfo=UTC), FIX / "storm_synthetic", "")
            html = (Path(d) / "reservation.html").read_text()
            index = (Path(d) / "index.html").read_text()
        self.assertIn("week-end du 31/10/2026", html)
        self.assertLess(html.index('id="tromso"'), html.index('id="keflavik"'))
        self.assertIn('href="reservation.html">Organisation voyage</a>', index)
        self.assertIn("Organisation voyage — week-end du 31/10/2026", html)
        # carte des spots : déplacée sur la page principale (fin de « Pratique »), lecture seule
        self.assertNotIn("Carte des spots", html)
        self.assertIn("maps/d/viewer?mid=1RMZHua_485EpJi3OGaLJ4qXm_fTGVc4", index)
        self.assertNotIn("maps/d/edit", index)
        self.assertLess(index.index("Pratique :"), index.index("Carte des spots"))
        self.assertLess(index.index("Carte des spots"), index.index("Analyse :"))
        self.assertLess(index.index("Analyse :"), index.index('href="rapport.html"'))
        # note Lufthansa : uniquement dans l'encart Keflavík, en italique
        self.assertEqual(html.count("Lufthansa MRS → KEF le week-end"), 1)
        self.assertGreater(html.index("Lufthansa"), html.index('id="keflavik"'))
        self.assertIn('href="https://www.windy.com/?2026-10-31-21,69.642,18.839,11" target="_blank"', index)
        self.assertIn('href="https://www.windy.com/?2026-10-31-21,63.926,-22.491,11" target="_blank"', index)
        self.assertLess(index.index("Prévisions :"), index.index("Pratique :"))
        self.assertEqual(index.count(">NOAA Forecast</a>"), 1)              # une seule série de liens
        self.assertEqual(index.count('href="reservation.html"'), 1)

    def test_discussion_et_page_rapport(self):
        with tempfile.TemporaryDirectory() as d:
            f.run(Path(d), datetime(2026, 10, 16, 17, 15, tzinfo=UTC), FIX / "storm_synthetic", "")
            data = json.loads((Path(d) / "data.json").read_text())
            disc = (Path(d) / "sources" / "discussion.txt").read_text()
            index = (Path(d) / "index.html").read_text()
            rapport = (Path(d) / "rapport.html").read_text()
        self.assertIn(":Product: Forecast Discussion", disc)                 # copie brute
        self.assertEqual(data["discussion"]["issued"], "2026-10-16T12:30:00+00:00")
        self.assertEqual(data["discussion"]["file"], "sources/discussion.txt")
        self.assertIn('href="rapport.html">Rapport d\'analyse</a>', index)
        self.assertLess(index.index("Organisation voyage"), index.index("Rapport d'analyse"))
        self.assertIn("contents/docs/rapport.json", rapport)                 # lu dans le dépôt, sans republier
        self.assertNotIn("innerHTML", rapport)                               # rendu en texte seul
        # sans discussion (fixture réelle) : pas d'erreur, champ null
        code, data, _ = self.run_scenario("real_2026-10-14", "2026-10-14T13:15")
        self.assertIsNone(data["discussion"])
        self.assertEqual(data["errors"], [])

    def test_samedi_partiel_jeudi(self):
        # bulletin couvrant 15-17/10 : nuit de samedi 17 couverte jusqu'à 00h UTC seulement
        text = (FIX / "storm_synthetic" / "noaa.txt").read_text().replace("Oct 16 1230", "Oct 15 1230") \
            .replace("Oct 16-Oct 18", "Oct 15-Oct 17").replace("Oct 16       Oct 17       Oct 18", "Oct 15       Oct 16       Oct 17")
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src"; src.mkdir()
            for name in ("gfz.json", "open_meteo_tromso.json", "open_meteo_grindavik.json"):
                (src / name).write_text((FIX / "storm_synthetic" / name).read_text())
            (src / "noaa.txt").write_text(text)
            f.run(Path(d) / "out", datetime(2026, 10, 15, 13, 15, tzinfo=UTC), src, "")
            data = json.loads((Path(d) / "out" / "data.json").read_text())
            html = (Path(d) / "out" / "index.html").read_text()
        self.assertEqual(data["saturday"]["noaa_slots"], 3)
        self.assertIn("Nuit partielle : 3/6", html)


if __name__ == "__main__":
    unittest.main()
