import os
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

import pronotepy
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("pronotebot")

# ---------------------------------------------------------------------------
# Variables d'environnement Render
# ---------------------------------------------------------------------------
# Le JSON complet initial généré par le QR Code
PRONOTE_CREDS_JSON = os.environ.get("PRONOTE_CREDS_JSON")
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# Fichiers de sauvegarde locale (effacés à chaque redémarrage sur le plan gratuit)
SEEN_FILE        = Path(os.environ.get("SEEN_FILE", "seen_grades.json"))
CREDS_FILE       = Path(os.environ.get("CREDS_FILE", "creds.json")) 
CHECK_INTERVAL   = int(os.environ.get("CHECK_INTERVAL_MIN", "30"))

class PronoteBot:
    def __init__(self):
        self.client = None

    def connect(self):
        log.info("Connexion à Pronote via Token QR Code…")
        
        creds = None
        # 1. On cherche d'abord si on a un fichier de credentials récent
        if CREDS_FILE.exists():
            try:
                with open(CREDS_FILE, "r", encoding="utf-8") as f:
                    creds = json.load(f)
                log.info("Utilisation du token sauvegardé localement.")
            except Exception as e:
                log.warning("Impossible de lire creds.json, on utilise la variable d'environnement.")
        
        # 2. Sinon, on utilise le token initial fourni par Render
        if not creds:
            if not PRONOTE_CREDS_JSON:
                raise ValueError("PRONOTE_CREDS_JSON manquant dans les variables d'environnement.")
            creds = json.loads(PRONOTE_CREDS_JSON)
            log.info("Utilisation du token initial (depuis Render).")

        # 3. Connexion
        self.client = pronotepy.Client.token_login(**creds)
        if not self.client.logged_in:
            raise RuntimeError("Token invalide ou expiré")
        
        log.info("Connecté avec succès !")
        
        # 4. Pronote renouvelle le token à chaque connexion.
        # On l'exporte, on le sauvegarde en local, ET on l'affiche dans les logs.
        try:
            new_creds = self.client.export_credentials()
            with open(CREDS_FILE, "w", encoding="utf-8") as f:
                json.dump(new_creds, f)
            
            # AFFICHE LE NOUVEAU TOKEN DANS LES LOGS (pour le copier dans Render)
            log.warning("⚠️ NOUVEAU TOKEN GÉNÉRÉ (à copier dans Render si redémarrage) : %s", json.dumps(new_creds))
        except Exception as e:
            log.error("Impossible de sauvegarder le nouveau token : %s", e)

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

    def _telegram(self, text):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        try:
            requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID, "text": text,
                "parse_mode": "Markdown", "disable_web_page_preview": True
            }, timeout=15)
        except Exception as e:
            log.error("Erreur Telegram : %s", e)

    def _grade_id(self, g):
        return f"{g.grade}|{g.subject.name[:30]}|{(g.comment or '')[:40]}|{g.date.strftime('%Y-%m-%d')}"

    def check_grades(self):
        if self.client is None or not self.client.logged_in:
            self.connect()
        else:
            try:
                self.client.session_check()
            except Exception:
                log.warning("Session expirée, reconnexion…")
                self.client = None
                self.connect()

        seen = self._load_seen()
        new_count = 0

        for period in self.client.periods:
            # CORRECTION : on utilise str() pour éviter le ValueError si c'est déjà du texte
            overall = str(period.overall_average) if period.overall_average else "—"
            
            for g in period.grades:
                gid = self._grade_id(g)
                if gid in seen:
                    continue
                
                msg = (
                    f"*Nouvelle note – {g.subject.name}*\n"
                    f"📝 Note : *{g.grade}/{g.out_of}*\n"
                    f"📅 Date : {g.date.strftime('%d/%m/%Y')}\n"
                    f"💬 Appréciation : {g.comment or '—'}\n"
                    f"🎯 Moyenne générale : {overall}"
                )
                self._telegram(msg)
                seen.add(gid)
                new_count += 1

        if new_count:
            self._save_seen(seen)
            log.info("%d nouvelle(s) note(s) envoyée(s)", new_count)
        else:
            log.info("Aucune nouvelle note")

app = Flask(__name__)
bot = PronoteBot()

@app.route("/")
def health():
    return jsonify(status="ok", connected=bot.client is not None and bot.client.logged_in), 200

@app.route("/check")
def manual_check():
    try:
        bot.check_grades()
        return jsonify(status="done"), 200
    except Exception as e:
        log.exception("check_grades a échoué")
        return jsonify(status="error", error=str(e)), 500

def scheduler_loop():
    try:
        bot.connect()
    except Exception as e:
        log.warning("Connexion initiale échouée : %s. Le job réessaiera.", e)

    scheduler = BackgroundScheduler(timezone="Europe/Paris")
    scheduler.add_job(bot.check_grades, "interval", minutes=CHECK_INTERVAL, next_run_time=datetime.now())
    scheduler.start()
    log.info("Planificateur démarré (toutes les %d min)", CHECK_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
