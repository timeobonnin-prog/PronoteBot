import os
import json
import logging
import threading
from datetime import datetime, timedelta, date
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

# Variables pour le stockage persistant (Gist)
GIST_ID            = os.environ.get("GIST_ID")
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN")

SEEN_FILE        = Path(os.environ.get("SEEN_FILE", "seen_grades.json"))
CHECK_INTERVAL   = int(os.environ.get("CHECK_INTERVAL_MIN", "30"))

class PronoteBot:
    def __init__(self):
        self.client = None

    def connect(self):
        log.info("Connexion à Pronote via Token QR Code…")
        creds = None
        
        # 1. Lire le token depuis le Gist GitHub (Stockage persistant)
        if GIST_ID and GITHUB_TOKEN:
            try:
                headers = {"Authorization": f"token {GITHUB_TOKEN}"}
                r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers, timeout=10)
                if r.status_code == 200:
                    files = r.json().get("files", {})
                    if "creds.json" in files:
                        content = files["creds.json"]["content"]
                        creds = json.loads(content)
                        log.info("Token récupéré depuis le Gist GitHub.")
            except Exception as e:
                log.warning("Erreur lecture Gist : %s", e)

        # 2. Si le Gist est vide, on utilise la variable Render de secours
        if not creds:
            if not PRONOTE_CREDS_JSON:
                raise ValueError("PRONOTE_CREDS_JSON manquant et Gist vide.")
            creds = json.loads(PRONOTE_CREDS_JSON)
            log.info("Utilisation du token initial (depuis Render).")

        # 3. Connexion
        self.client = pronotepy.Client.token_login(**creds)
        if not self.client.logged_in:
            raise RuntimeError("Token invalide ou expiré")
        
        log.info("Connecté avec succès !")
        
        # 4. Sauvegarder le nouveau token dans le Gist (pour la prochaine fois)
        try:
            new_creds = self.client.export_credentials()
            if GIST_ID and GITHUB_TOKEN:
                headers = {
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.v3+json"
                }
                data = {"files": {"creds.json": {"content": json.dumps(new_creds)}}}
                r = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=headers, json=data, timeout=10)
                if r.status_code == 200:
                    log.info("Nouveau token sauvegardé dans le Gist GitHub.")
                else:
                    log.error("Erreur de sauvegarde Gist : %s", r.text)
            else:
                log.warning("Gist non configuré, le token ne sera pas persisté.")
        except Exception as e:
            log.error("Impossible de sauvegarder le token : %s", e)

    # --- OUTILS ---
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

    # --- 1. NOTES ---
    def check_grades(self):
        self._refresh_session()
        seen = self._load_seen(SEEN_FILE)
        new_count = 0

        for period in self.client.periods:
            overall = str(period.overall_average) if period.overall_average else "—"
            for g in period.grades:
                gid = f"{g.grade}|{g.subject.name[:30]}|{(g.comment or '')[:40]}|{g.date.strftime('%Y-%m-%d')}"
                if gid not in seen:
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
        log.info("Vérification des menus…")
        today = date.today()
        next_monday = today + timedelta(days=(7 - today.weekday()))
        next_sunday = next_monday + timedelta(days=6)
        
        try:
            menus = self.client.menus(next_monday, next_sunday)
        except Exception as e:
            log.warning("Impossible de récupérer les menus : %s", e)
            return

        if not menus:
            return

        msg = "*🍽️ Menus de la cantine (Semaine prochaine)*\n\n"
        has_content = False
        for menu in menus:
            if hasattr(menu, 'name') and menu.name:
                msg += f"*{menu.date.strftime('%A %d/%m')}* : {menu.name}\n"
                has_content = True
        
        if has_content:
            self._telegram(msg)

    # --- 3. DEVOIRS NON FAITS (Soir 19h30) ---
    def check_homework_daily(self):
        self._refresh_session()
        today = date.today()
        
        if today.weekday() >= 5: # Samedi ou Dimanche
            return
        
        tomorrow = today + timedelta(days=1)
        try:
            lessons = self.client.lessons(tomorrow, tomorrow)
            if not lessons: # Pas de cours (vacances/férié)
                return
        except Exception:
            pass

        try:
            homeworks = self.client.homework(tomorrow, tomorrow)
        except Exception:
            return

        unfinished = [hw for hw in homeworks if not hw.done]
        if not unfinished:
            return

        msg = "*📚 Devoirs pour demain*\n\n"
        for hw in unfinished:
            msg += f"• *{hw.subject.name}* : {hw.description[:100]}\n"

        self._telegram(msg)

    # --- 4. CHANGEMENTS DE COURS ---
    def check_lesson_changes(self):
        self._refresh_session()
        seen = self._load_seen("seen_changes.json")
        new_count = 0
        today = date.today()
        
        try:
            lessons = self.client.lessons(today, today + timedelta(days=3))
        except Exception:
            return

        for lesson in lessons:
            if lesson.canceled or lesson.exempted:
                cid = f"{lesson.subject.name}|{lesson.start.strftime('%Y-%m-%d %H:%M')}"
                if cid not in seen:
                    status = "ANNULÉ" if lesson.canceled else "DISPENSÉ"
                    msg = (
                        f"*⚠️ Changement de cours*\n"
                        f"📚 Matière : {lesson.subject.name}\n"
                        f"📅 Date : {lesson.start.strftime('%d/%m/%Y à %H:%M')}\n"
                        f"🚫 Statut : *{status}*"
                    )
                    self._telegram(msg)
                    seen.add(cid)
                    new_count += 1
        if new_count:
            self._save_seen("seen_changes.json", seen)

    # --- 5. INFORMATIONS ET SONDAGES ---
    def check_informations(self):
        self._refresh_session()
        seen = self._load_seen("seen_infos.json")
        new_count = 0
        try:
            infos = self.client.information_and_surveys()
        except Exception:
            return

        for item in infos:
            iid = f"{item.creation_date.strftime('%Y-%m-%d')}|{item.title}"
            if iid not in seen:
                is_sondage = "sondage" in str(item.category).lower() or "sondage" in item.title.lower()
                icon = "📊" if is_sondage else "📢"
                type_txt = "Nouveau Sondage" if is_sondage else "Nouvelle Information"
                msg = f"*{icon} {type_txt}*\n📌 {item.title}\n📅 {item.creation_date.strftime('%d/%m/%Y')}"
                self._telegram(msg)
                seen.add(iid)
                new_count += 1
        if new_count:
            self._save_seen("seen_infos.json", seen)

    # --- 6. DISCUSSIONS ---
    def check_discussions(self):
        self._refresh_session()
        seen = self._load_seen("seen_disc.json")
        new_count = 0
        try:
            discussions = self.client.messages()
        except Exception:
            return

        for disc in discussions:
            for msg in disc.messages:
                if not msg.read:
                    mid = f"{disc.id}|{msg.id}"
                    if mid not in seen:
                        txt = f"*💬 Nouveau message – {msg.author}*\n📌 {disc.subject}\n📝 {msg.content[:150]}..."
                        self._telegram(txt)
                        seen.add(mid)
                        new_count += 1
        if new_count:
            self._save_seen("seen_disc.json", seen)

    # --- 7. ABSENCES ---
    def check_absences(self):
        self._refresh_session()
        seen = self._load_seen("seen_abs.json")
        new_count = 0
        try:
            absences = self.client.absences()
        except Exception:
            return

        for ab in absences:
            aid = f"{ab.from_date.strftime('%Y-%m-%d')}|{ab.to_date.strftime('%Y-%m-%d')}"
            if aid not in seen:
                msg = f"*⚠️ Absence/Retard*\n📅 {ab.from_date.strftime('%d/%m/%Y %H:%M')}\n💬 {ab.reasons or '—'}"
                self._telegram(msg)
                seen.add(aid)
                new_count += 1
        if new_count:
            self._save_seen("seen_abs.json", seen)


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
        bot.check_homework_daily()
        bot.check_informations()
        bot.check_discussions()
        bot.check_absences()
        bot.check_menus()
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
    
    # Tâches régulières (toutes les 30 min)
    scheduler.add_job(bot.check_grades, "interval", minutes=CHECK_INTERVAL, next_run_time=datetime.now())
    scheduler.add_job(bot.check_lesson_changes, "interval", minutes=CHECK_INTERVAL)
    scheduler.add_job(bot.check_informations, "interval", minutes=CHECK_INTERVAL)
    scheduler.add_job(bot.check_discussions, "interval", minutes=CHECK_INTERVAL)
    scheduler.add_job(bot.check_absences, "interval", minutes=CHECK_INTERVAL)
    
    # Tâches planifiées
    scheduler.add_job(bot.check_menus, CronTrigger(day_of_week='sun', hour=17, minute=0))
    scheduler.add_job(bot.check_homework_daily, CronTrigger(hour=19, minute=30))
    
    scheduler.start()
    log.info("Planificateur démarré.")

if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
