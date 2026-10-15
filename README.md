# Aurora — prévision d'aurores Tromsø / Grindavík

La page est publiée sur GitHub Pages, à l'adresse <https://alexis-sk.github.io/aurora/>. Elle est régénérée par GitHub Actions trois fois par jour, à 01:15, 13:15 et 17:15 UTC.

## Fonctionnement

| Donnée | Source | Remarque |
|---|---|---|
| Kp (référence) | [NOAA SWPC 3-day forecast](https://services.swpc.noaa.gov/text/3-day-forecast.txt) | Couvre J à J+2 par tranches de 3 h UT ; la nuit de samedi est complète dans le bulletin du vendredi 12:30 UTC |
| Kp ensemble | [GFZ](https://spaceweather.gfz.de/) | Masqué si le fichier date de plus de 48 h |
| Nuages | [Open-Meteo](https://open-meteo.com/) | `cloud_cover` horaire, modèle « best match », sans clé |
| Lune / soleil | Calcul local ([PyEphem](https://rhodesmill.org/pyephem/)) | Aucune API |

**Nuit** : de 15h UTC le jour J à 09h UTC le lendemain. Une tranche n'est notée que si le soleil est sous −6° sur le site concerné.

**Score d'une tranche** = 0,4 × score Kp + 0,4 × (1 − nuages moyens) + 0,2 × (1 − illumination × fraction de la tranche avec la lune levée).

**Score Kp** : de 0 à 0,40 entre Kp 0 et 4, de 0,40 à 0,85 entre Kp 4 et 5, de 0,85 à 1,00 entre Kp 5 et 6, puis 1,00 à partir de Kp 6.

**Score d'un site pour la nuit** = meilleure tranche. La destination recommandée pour samedi est le site ayant le meilleur score. L'encadré reste vide tant que NOAA ne couvre pas la nuit de samedi.

**Alerte** : à chaque run, si une tranche NOAA des trois jours atteint Kp ≥ 5, une notification est envoyée via [ntfy](https://ntfy.sh). Il faut pour cela que le secret `NTFY_TOPIC` soit défini.

## Mise en route (une fois)

1. **Settings → Pages → Build and deployment → Source : « GitHub Actions »**.
2. **Alerte (optionnel)** : installer l'application ntfy sur le téléphone et s'abonner à un nom de topic long et aléatoire. Les topics ntfy.sh sont publics pour qui en connaît le nom. Créer ensuite le secret **Settings → Secrets and variables → Actions → New repository secret** avec le nom `NTFY_TOPIC` et le nom du topic comme valeur.
3. **Premier run** : onglet **Actions → Aurora forecast → Run workflow**.

## En local

```bash
pip install -r requirements.txt
python -m unittest discover tests
python forecast.py --out site                                   # données réelles
python forecast.py --fixtures tests/fixtures/storm_synthetic \
                   --now 2026-10-16T17:15 --out site            # scénario de test
```

## Limites connues

- Les runs planifiés de GitHub peuvent être retardés en cas de forte charge.
- Le cron est désactivé après 60 jours sans activité sur un dépôt public. Le commit de `docs/` à chaque run sert aussi à l'éviter.
- La prévision Kp au-delà de 24-48 h et la nébulosité à J+2 restent très incertaines.
