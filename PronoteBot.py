import os
import json
import logging
import threading
from datetime import datetime, timedelta, date, time
from pathlib import Path

import pronotepy
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
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
PRONOTE_CREDS_JSON = os.environ.get("PRONOTE_CREDS_JSON")
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE        = Path(os.environ.get("SEEN_FILE", "seen_grades.json"))
CREDS_FILE       = Path(os.environ.get("CREDS_FILE", "creds.json")) 
CHECK_INTERVAL   = int(os.environ.get("CHECK_INTERVAL_MIN", "30"))

class PronoteBot:
    def __init__(self):
        self.client = None

    def connect(self):
        log.info("Connexion à Pronote via Token QR Code…")
        creds = None
        if CREDS_FILE.exists():
            try:
                with open(CREDS_FILE, "r", encoding="utf-8") as f:
                    creds = json.load(f)
                log.info("Utilisation du token sauvegardé localement.")
            except Exception:
                pass
        
        if not creds:
            if not PRONOTE_CREDS_JSON:
                raise ValueError("PRONOTE_CREDS_JSON manquant.")
            creds = json.loads(PRONOTE_CREDS_JSON)
            log.info("Utilisation du token initial (depuis Render).")

        self.client = pronotepy.Client.token_login(**creds)
        if not self.client.logged_in:
            raise RuntimeError("Token invalide ou expiré")
        
        log.info("Connecté avec succès !")
        
        try:
            new_creds = self.client.export_credentials()
            with open(CREDS_FILE, "w", encoding="utf-8") as f:
                json.dump(new_creds, f)
            log.warning("⚠️ NOUVEAU TOKEN GÉNÉRÉ (à copier dans Render si redémarrage) : %s", json.dumps(new_creds))
        except Exception as e:
            log.error("Impossible de sauvegarder le token : %s", e)

    def _load_seen(self, filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _save_seen(self, filename, seen):
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(list(seen), f)

    def _telegram(self, text):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        try:
            requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID, "text": text,
                "parse_mode": "Markdown", "disable_web_page_preview": True
            }, timeout=15)
        except Exception as e:
            log.error("Erreur Telegram : %s", e)

    def _refresh_session(self):
        if self.client is None or not self.client.logged_in:
            self.connect()
        else:
            try:
                self.client.session_check()
            except Exception:
                log.warning("Session expirée, reconnexion…")
                self.client = None
                self.connect()

    # --- 1. NOTES (toutes les 30 min) ---
    def check_grades(self):
        self._refresh_session()
        seen = self._load_seen(SEEN_FILE)
        new_count = 0

        for period in self.client.periods:
            overall = str(period.overall_average) if period.overall_average else "—"
            for g in period.grades:
                gid = f"{g.grade}|{g.subject.name[:30]}|{(g.comment or '')[:40]}|{g.date.strftime('%Y-%m-%d')}"
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
            self._save_seen(SEEN_FILE, seen)
            log.info("%d nouvelle(s) note(s) envoyée(s)", new_count)

    # --- 2. MENU CANTINE (Dimanche 17h) ---
    def check_menus(self):
        self._refresh_session()
        log.info("Vérification des menus de la cantine…")
        
        # On récupère les menus de la semaine à venir (lundi à dimanche)
        today = date.today()
        next_monday = today + timedelta(days=(7 - today.weekday()))
        next_sunday = next_monday + timedelta(days=6)
        
        try:
            menus = self.client.menus(next_monday, next_sunday)
        except Exception as e:
            log.warning("Impossible de récupérer les menus : %s", e)
            return

        if not menus:
            log.info("Aucun menu publié pour la semaine prochaine.")
            return

        msg = "*🍽️ Menus de la cantine (Semaine prochaine)*\n\n"
        has_content = False
        for menu in menus:
            # On suppose que l'objet Menu a un attribut 'name' et 'date'
            # Adapte les attributs selon ce que renvoie l'API
            if hasattr(menu, 'name') and menu.name:
                msg += f"*{menu.date.strftime('%A %d/%m')}* : {menu.name}\n"
                has_content = True
        
        if has_content:
            self._telegram(msg)
            log.info("Menu de la cantine envoyé.")
        else:
            log.info("Les menus récupérés sont vides.")

    # --- 3. DEVOIRS NON FAITS (Tous les soirs à 19h30) ---
    def check_homework_daily(self):
        self._refresh_session()
        
        # Vérifier si on est en vacances ou un week-end
        today = date.today()
        if today.weekday() >= 5:  # Samedi ou Dimanche
            log.info("Week-end, pas de vérification des devoirs.")
            return
        
        # On regarde les cours de demain. Si pas de cours, c'est peut-être un jour férié ou vacances.
        tomorrow = today + timedelta(days=1)
        try:
            lessons_tomorrow = self.client.lessons(tomorrow, tomorrow)
            if not lessons_tomorrow:
                log.info("Pas de cours demain (vacances ou jour férié).")
                return
        except Exception as e:
            log.warning("Impossible de vérifier les cours de demain : %s", e)

        # On récupère les devoirs pour demain
        try:
            homeworks = self.client.homework(tomorrow, tomorrow)
        except Exception as e:
            log.warning("Impossible de récupérer les devoirs : %s", e)
            return

        # On filtre les devoirs non faits
        unfinished = [hw for hw in homeworks if not hw.done]

        if not unfinished:
            log.info("Tous les devoirs sont faits pour demain. Pas de message.")
            return

        msg = "*📚 Devoirs pour demain*\n\n"
        for hw in unfinished:
            msg += f"• *{hw.subject.name}* : {hw.description[:100]}\n"

        self._telegram(msg)
        log.info("%d devoir(s) non fait(s) envoyé(s).", len(unfinished))

    # --- 4. CHANGEMENTS DE COURS (Toutes les 30 min) ---
    def check_lesson_changes(self):
        self._refresh_session()
        seen_changes_file = "seen_lesson_changes.json"
        seen_changes = self._load_seen(seen_changes_file)
        new_count = 0

        # On vérifie les cours des 3 prochains jours
        today = date.today()
        end_date = today + timedelta(days=3)
        
        try:
            lessons = self.client.lessons(today, end_date)
        except Exception as e:
            log.warning("Impossible de vérifier les changements de cours : %s", e)
            return

        for lesson in lessons:
            # On ne s'intéresse qu'aux cours annulés ou modifiés
            if lesson.canceled or lesson.exempted:
                # Identifiant unique pour éviter les doublons
                change_id = f"{lesson.subject.name}|{lesson.start.strftime('%Y-%m-%d %H:%M')}|{lesson.canceled}"
                if change_id not in seen_changes:
                    status = "ANNULÉ" if lesson.canceled else "DISPENSÉ"
                    msg = (
                        f"*⚠️ Changement de cours*\n"
                        f"📚 Matière : {lesson.subject.name}\n"
                        f"📅 Date : {lesson.start.strftime('%d/%m/%Y à %H:%M')}\n"
                        f"🚫 Statut : *{status}*"
                    )
                    self._telegram(msg)
                    seen_changes.add(change_id)
                    new_count += 1

        if new_count > 0:
            self._save_seen(seen_changes_file, seen_changes)
            log.info("%d changement(s) de cours envoyé(s).", new_count)

# ---------------------------------------------------------------------------
# APPLICATION FLASK ET LANCEMENT
# ---------------------------------------------------------------------------
app = Flask(__name__)
bot = PronoteBot()

@app.route("/")
def health():
    return jsonify(status="ok", connected=bot.client is not None and bot.client.logged_in), 200

@app.route("/check")
def manual_check():
    try:
        bot.check_grades()
        bot.check_lesson_changes()
        return jsonify(status="done"), 200
    except Exception as e:
        log.exception("check a échoué")
        return jsonify(status="error", error=str(e)), 500

def scheduler_loop():
    try:
        bot.connect()
    except Exception as e:
        log.warning("Connexion initiale échouée : %s", e)

    scheduler = BackgroundScheduler(timezone="Europe/Paris")
    
    # Tâches récurrentes
    scheduler.add_job(bot.check_grades, "interval", minutes=CHECK_INTERVAL, next_run_time=datetime.now())
    scheduler.add_job(bot.check_lesson_changes, "interval", minutes=CHECK_INTERVAL)
    
    # Tâche du dimanche à 17h00 (Menu)
    scheduler.add_job(bot.check_menus, CronTrigger(day_of_week='sun', hour=17, minute=0))
    
    # Tâche tous les soirs à 19h30 (Devoirs)
    scheduler.add_job(bot.check_homework_daily, CronTrigger(hour=19, minute=30))
    
    scheduler.start()
    log.info("Planificateur démarré (notes/cours: %d min, menus: dim 17h, devoirs: 19h30)", CHECK_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
