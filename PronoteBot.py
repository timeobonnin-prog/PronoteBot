import os
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

import pronotepy
import pronotepy.ent                     # <-- nécessaire pour getattr
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pronotebot")

# ---------------------------------------------------------------------------
# Variables d'environnement Render
# ---------------------------------------------------------------------------
PRONOTE_URL      = os.environ["PRONOTE_URL"]
PRONOTE_USERNAME = os.environ["PRONOTE_USERNAME"]
PRONOTE_PASSWORD = os.environ["PRONOTE_PASSWORD"]
PRONOTE_CAS      = os.environ.get("PRONOTE_CAS", "").strip()

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Fichier mémoire des notes déjà envoyées
# - Disque persistant Render : /var/data/seen_grades.json
# - Sinon : ./seen_grades.json (perdu à chaque redeploy)
SEEN_FILE = Path(os.environ.get("SEEN_FILE", "seen_grades.json"))

# Intervalle de vérification (minutes)
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_MIN", "30"))


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class PronoteBot:
    def __init__(self):
        self.client = None

    # --- Résolution de la fonction ENT -------------------------------------
    def _resolve_ent(self):
        """Transforme PRONOTE_CAS (str) en fonction pronotepy.ent.<nom>."""
        if not PRONOTE_CAS:
            return None
        if not hasattr(pronotepy.ent, PRONOTE_CAS):
            raise ValueError(
                f"ENT inconnu : '{PRONOTE_CAS}'. "
                f"Vérifie la liste dans la doc pronotepy (ex: ile_de_france, "
                f"monbureaunumerique, occitanie_montpellier, ac_reunion…)."
            )
        return getattr(pronotepy.ent, PRONOTE_CAS)

    # --- Connexion Pronote -------------------------------------------------
    def connect(self):
        ent_func = self._resolve_ent()
        self.client = pronotepy.Client(
            PRONOTE_URL,
            username=PRONOTE_USERNAME,
            password=PRONOTE_PASSWORD,
            ent=ent_func,
        )
        if not self.client.logged_in:
            raise RuntimeError("Connexion Pronote échouée")
        log.info("Connecté à Pronote (ent=%s)", PRONOTE_CAS or "aucun")

    # --- Persistance des notes déjà vues -----------------------------------
    def _load_seen(self):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _save_seen(self, seen):
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f)

    # --- Envoi Telegram ----------------------------------------------------
    def _telegram(self, text):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if r.status_code != 200:
                log.error("Telegram HTTP %s : %s", r.status_code, r.text)
        except requests.RequestException as e:
            log.exception("Erreur Telegram : %s", e)

    # --- Identifiant stable d'une note -------------------------------------
    @staticmethod
    def _grade_id(g):
        return "|".join([
            str(g.grade),
            g.subject.name[:30],
            (g.comment or "")[:40],
            g.date.strftime("%Y-%m-%d"),
        ])

    # --- Détection + envoi des nouvelles notes -----------------------------
    def check_grades(self):
        if self.client is None or not self.client.logged_in:
            self.connect()
        else:
            try:
                self.client.session_check()
            except Exception:
                log.warning("Session expirée, reconnexion…")
                self.connect()

        seen = self._load_seen()
        new_count = 0

        for period in self.client.periods:
            try:
                overall = f"{period.overall_average:.2f}"
            except Exception:
                overall = "—"

            for g in period.grades:
                gid = self._grade_id(g)
                if gid in seen:
                    continue

                try:
                    coeff = str(g.coefficient)
                except Exception:
                    coeff = "—"

                message = (
                    f"*Nouvelle note – {g.subject.name}*\n"
                    f"📝 Note : *{g.grade}/{g.out_of}*\n"
                    f"⚖️ Coefficient : {coeff}\n"
                    f"📅 Date : {g.date.strftime('%d/%m/%Y')}\n"
                    f"💬 Appréciation : {g.comment or '—'}\n"
                    f"🎯 Moyenne générale : {overall}"
                )
                self._telegram(message)
                seen.add(gid)
                new_count += 1

        if new_count:
            self._save_seen(seen)
            log.info("%d nouvelle(s) note(s) envoyée(s)", new_count)
        else:
            log.info("Aucune nouvelle note")


# ---------------------------------------------------------------------------
# Serveur HTTP de santé (Render Web Service)
# ---------------------------------------------------------------------------
app = Flask(__name__)
bot = PronoteBot()


@app.route("/")
def health():
    return jsonify(status="ok", service="PronoteBot"), 200


@app.route("/check")
def manual_check():
    try:
        bot.check_grades()
        return jsonify(status="done"), 200
    except Exception as e:
        log.exception("check_grades a échoué")
        return jsonify(status="error", error=str(e)), 500


# ---------------------------------------------------------------------------
# Démarrage : planificateur en arrière-plan + Flask
# ---------------------------------------------------------------------------
def scheduler_loop():
    try:
        bot.connect()
    except Exception:
        log.exception("Connexion initiale échouée (le job réessaiera)")

    scheduler = BackgroundScheduler(timezone="Europe/Paris")
    scheduler.add_job(
        bot.check_grades,
        "interval",
        minutes=CHECK_INTERVAL,
        next_run_time=datetime.now(),
    )
    scheduler.start()
    log.info("Planificateur démarré (toutes les %d min)", CHECK_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
