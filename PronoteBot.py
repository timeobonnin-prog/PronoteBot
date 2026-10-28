import os
import json
import logging
import threading
import time
import io
from datetime import datetime, timedelta, date
from pathlib import Path

import pronotepy
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify

# Pour le graphique
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pronotebot")

# ---------------------------------------------------------------------------
# Variables d'environnement
# ---------------------------------------------------------------------------
PRONOTE_CREDS_JSON = os.environ.get("PRONOTE_CREDS_JSON")
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GIST_ID            = os.environ.get("GIST_ID")
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN")
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL_MIN", "15"))

JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
COULEURS_COMPETENCES = {
    "rouge": "🔴", "jaune": "🟡", "vert": "🟢", "vert foncé": "🟢", "bleu": "🔵"
}

class PronoteBot:
    def __init__(self):
        self.client = None
        self.last_update_id = 0

    # --- GESTION DU GIST (Mémoire et Token) ---
    def _gist_request(self, method, data=None):
        if not GIST_ID or not GITHUB_TOKEN:
            return None
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        url = f"https://api.github.com/gists/{GIST_ID}"
        try:
            if method == "GET":
                r = requests.get(url, headers=headers, timeout=10)
            elif method == "PATCH":
                r = requests.patch(url, headers=headers, json=data, timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            log.error("Erreur Gist API : %s", e)
        return None

    def _load_gist_file(self, filename, default=None):
        gist = self._gist_request("GET")
        if gist and "files" in gist and filename in gist["files"]:
            try:
                return json.loads(gist["files"][filename]["content"])
            except:
                pass
        return default

    def _save_gist_file(self, filename, data):
        files = {filename: {"content": json.dumps(data)}}
        self._gist_request("PATCH", {"files": files})

    # --- CONNEXION ---
    def connect(self):
        log.info("Connexion à Pronote via Token QR Code…")
        creds = self._load_gist_file("creds.json")
        if creds:
            log.info("Token récupéré depuis le Gist.")
        else:
            if not PRONOTE_CREDS_JSON:
                raise ValueError("Pas de token nulle part.")
            creds = json.loads(PRONOTE_CREDS_JSON)
            log.info("Utilisation du token initial (Render).")

        self.client = pronotepy.Client.token_login(**creds)
        if not self.client.logged_in:
            raise RuntimeError("Token invalide")
        
        log.info("Connecté avec succès !")
        new_creds = self.client.export_credentials()
        self._save_gist_file("creds.json", new_creds)
        log.info("Nouveau token sauvegardé dans le Gist.")

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

    # --- ENVOIS TELEGRAM ---
    def _telegram(self, text, keyboard=None):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID, "text": text,
            "parse_mode": "Markdown", "disable_web_page_preview": True
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        try:
            requests.post(url, json=payload, timeout=15)
        except Exception as e:
            log.error("Erreur Telegram : %s", e)

    def _telegram_photo(self, photo_bytes, caption=""):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        try:
            files = {'photo': ('image.png', photo_bytes)}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}
            requests.post(url, data=data, files=files, timeout=30)
        except Exception as e:
            log.error("Erreur envoi photo : %s", e)

    def _telegram_document(self, file_bytes, filename, caption=""):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        try:
            files = {'document': (filename, file_bytes)}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}
            requests.post(url, data=data, files=files, timeout=30)
        except Exception as e:
            log.error("Erreur envoi document : %s", e)

    # --- CLAVIER INTERACTIF ---
    def _get_main_keyboard(self):
        return {
            "inline_keyboard": [
                [
                    {"text": "📝 Dernières notes", "callback_data": "btn_notes"},
                    {"text": "📚 Devoirs pour demain", "callback_data": "btn_devoirs"}
                ],
                [
                    {"text": "📅 Emploi du temps", "callback_data": "btn_emploi"}
                ]
            ]
        }

    # --- DÉTECTION DES VACANCES ---
    def _is_vacation(self):
        today = date.today()
        for i in range(1, 6):
            next_day = today + timedelta(days=i)
            if next_day.weekday() >= 5:
                continue
            try:
                lessons = self.client.lessons(next_day, next_day)
                if lessons:
                    return False
            except Exception:
                pass
        return True

    # --- SYSTÈME ANTI-DOUBLON ---
    def _check_already_sent(self, task_name, date_str):
        last_sent = self._load_gist_file("last_sent.json", {})
        return last_sent.get(task_name) == date_str

    def _mark_as_sent(self, task_name, date_str):
        last_sent = self._load_gist_file("last_sent.json", {})
        last_sent[task_name] = date_str
        self._save_gist_file("last_sent.json", last_sent)

    # --- 1. NOTES ---
    def check_grades(self):
        self._refresh_session()
        seen = set(self._load_gist_file("seen_grades.json", []))
        new_count = 0
        for period in self.client.periods:
            overall = str(period.overall_average) if period.overall_average else "—"
            for g in period.grades:
                gid = f"{g.grade}|{g.subject.name[:30]}|{(g.comment or '')[:40]}|{g.date.strftime('%Y-%m-%d')}"
                if gid not in seen:
                    note_str = f"{g.grade}/{g.out_of}" if g.grade else "Non noté"
                    msg = f"*Nouvelle note – {g.subject.name}*\n📝 Note : *{note_str}*\n📅 Date : {g.date.strftime('%d/%m/%Y')}\n💬 Évaluation : {g.comment or '—'}\n🎯 Moyenne générale : {overall}"
                    self._telegram(msg)
                    seen.add(gid)
                    new_count += 1
        if new_count:
            self._save_gist_file("seen_grades.json", list(seen))
            log.info("%d nouvelle(s) note(s) envoyée(s)", new_count)

    # --- 2. MENUS DÉTAILLÉS (Dimanche 18h) ---
    def check_menus(self):
        today_str = date.today().strftime("%Y-%m-%d")
        if self._check_already_sent("menus", today_str):
            return

        self._refresh_session()
        today = date.today()
        next_monday = today + timedelta(days=(7 - today.weekday()))
        next_sunday = next_monday + timedelta(days=6)
        
        try:
            menus = self.client.menus(next_monday, next_sunday)
        except Exception:
            return
        if not menus:
            return

        msg = "*🍽️ Menus de la cantine (Semaine prochaine)*\n\n"
        has_content = False
        for menu in menus:
            if hasattr(menu, 'date') and menu.date:
                jour_fr = JOURS_FR[menu.date.weekday()]
                msg += f"*{jour_fr} {menu.date.strftime('%d/%m')}*\n"
                if hasattr(menu, 'name') and menu.name:
                    msg += f"_{menu.name}_\n"
                if hasattr(menu, 'meals') and menu.meals:
                    for meal in menu.meals:
                        msg += f"  • {meal}\n"
                    has_content = True
                elif hasattr(menu, 'name') and menu.name:
                    has_content = True
                msg += "\n"
        if has_content:
            self._telegram(msg)
            self._mark_as_sent("menus", today_str)

    # --- 3. DEVOIRS ET EMPLOI DU TEMPS (19h30) ---
    def check_homework_and_timetable_daily(self):
        today_str = date.today().strftime("%Y-%m-%d")
        if self._check_already_sent("devoirs_edt", today_str):
            return

        self._refresh_session()
        if self._is_vacation():
            log.info("Mode vacances actif.")
            return

        today = date.today()
        if today.weekday() >= 5: return
        tomorrow = today + timedelta(days=1)
        
        timetable_msg = ""
        try:
            lessons = self.client.lessons(tomorrow, tomorrow)
            if lessons:
                timetable_msg = f"*📅 Emploi du temps de demain ({JOURS_FR[tomorrow.weekday()]} {tomorrow.strftime('%d/%m')})*\n"
                for l in lessons:
                    heure = l.start.strftime('%H:%M')
                    salle = f" - Salle {l.classroom}" if l.classroom else ""
                    prof = f" ({l.teacher_name})" if l.teacher_name else ""
                    timetable_msg += f"• {heure} : *{l.subject.name}*{prof}{salle}\n"
                timetable_msg += "\n"
        except Exception as e:
            log.warning("Impossible de récupérer l'emploi du temps : %s", e)

        homework_msg = ""
        try:
            homeworks = self.client.homework(tomorrow, tomorrow)
            unfinished = [hw for hw in homeworks if not hw.done]
            if unfinished:
                homework_msg = "*📚 Devoirs NON FAITS pour demain*\n\n"
                for hw in unfinished:
                    homework_msg += f"• *{hw.subject.name}* : {hw.description[:150]}\n"
                    if hw.files:
                        for f in hw.files:
                            if f.type == 1:
                                try:
                                    self._telegram_document(f.data, f.name, caption=f"📎 Pièce jointe : {hw.subject.name}")
                                except Exception:
                                    pass
                homework_msg += "\n"
        except Exception as e:
            log.warning("Impossible de récupérer les devoirs : %s", e)

        if timetable_msg or homework_msg:
            self._telegram(timetable_msg + homework_msg)
            self._mark_as_sent("devoirs_edt", today_str)

    # --- 4. CHANGEMENTS DE COURS ---
    def check_lesson_changes(self):
        self._refresh_session()
        old_lessons = self._load_gist_file("seen_lessons.json", {})
        new_lessons = {}
        changes = []
        today = date.today()
        
        for i in range(0, 3):
            target_date = today + timedelta(days=i)
            try:
                lessons = self.client.lessons(target_date, target_date)
            except Exception:
                continue

            for lesson in lessons:
                lid = f"{target_date.strftime('%Y-%m-%d')}|{lesson.start.strftime('%H:%M')}|{lesson.subject.name}"
                is_perm = getattr(lesson, 'is_permanent', False) or "perm" in str(lesson.subject.name).lower()
                current_state = {
                    "canceled": lesson.canceled or lesson.exempted,
                    "room": getattr(lesson, 'classroom', ''),
                    "subject": lesson.subject.name,
                    "start": lesson.start.strftime("%d/%m à %H:%M"),
                    "is_perm": is_perm
                }
                new_lessons[lid] = current_state

                if lid in old_lessons:
                    old_state = old_lessons[lid]
                    if current_state["canceled"] and not old_state["canceled"] and not old_state["is_perm"]:
                        changes.append(f"*⚠️ Cours ANNULÉ*\n📚 {lesson.subject.name}\n📅 {current_state['start']}")
                    elif current_state["room"] != old_state["room"] and not current_state["canceled"] and not current_state["is_perm"]:
                        changes.append(f"*🚪 Changement de salle*\n📚 {lesson.subject.name}\n📅 {current_state['start']}\n📍 {old_state['room']} ➡️ {current_state['room']}")

        if changes:
            for change in changes:
                self._telegram(change)
        self._save_gist_file("seen_lessons.json", new_lessons)

    # --- 5. INFORMATIONS ---
    def check_informations(self):
        self._refresh_session()
        seen = set(self._load_gist_file("seen_infos.json", []))
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
                content_preview = f"\n📝 {item.content[:200]}..." if getattr(item, 'content', None) else ""
                msg = f"*{icon} {type_txt}*\n📌 {item.title}\n📅 {item.creation_date.strftime('%d/%m/%Y')}{content_preview}"
                self._telegram(msg)
                seen.add(iid)
                new_count += 1
        if new_count:
            self._save_gist_file("seen_infos.json", list(seen))

    # --- 6. DISCUSSIONS ---
    def check_discussions(self):
        self._refresh_session()
        seen = set(self._load_gist_file("seen_disc.json", []))
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
            self._save_gist_file("seen_disc.json", list(seen))

    # --- 7. ABSENCES ---
    def check_absences(self):
        self._refresh_session()
        seen = set(self._load_gist_file("seen_abs.json", []))
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
            self._save_gist_file("seen_abs.json", list(seen))

    # --- 8. PUNITIONS ---
    def check_punishments(self):
        self._refresh_session()
        seen = set(self._load_gist_file("seen_punishments.json", []))
        new_count = 0
        try:
            for period in self.client.periods:
                for pun in period.punishments:
                    pid = f"{pun.id}"
                    if pid not in seen:
                        msg = f"*⚖️ Nouvelle Punition/Sanction*\n📅 {pun.date.strftime('%d/%m/%Y')}\n💬 Motif : {pun.reason or '—'}\n⏱️ Durée : {pun.duration or '—'}"
                        self._telegram(msg)
                        seen.add(pid)
                        new_count += 1
        except Exception as e:
            log.warning("Erreur punitions : %s", e)
        if new_count:
            self._save_gist_file("seen_punishments.json", list(seen))

    # --- 9. COMPÉTENCES ---
    def check_skills(self):
        self._refresh_session()
        seen = set(self._load_gist_file("seen_skills.json", []))
        new_count = 0
        try:
            for period in self.client.periods:
                for ev in period.evaluations:
                    for acq in ev.acquisitions:
                        aid = f"{ev.id}|{acq.id}"
                        if aid not in seen:
                            color = COULEURS_COMPETENCES.get(acq.level.lower(), "⚪")
                            msg = f"*🎯 Nouvelle Compétence*\n📚 {ev.subject.name}\n📝 {acq.name}\n{color} Niveau : *{acq.level}*"
                            self._telegram(msg)
                            seen.add(aid)
                            new_count += 1
        except Exception as e:
            log.warning("Erreur compétences : %s", e)
        if new_count:
            self._save_gist_file("seen_skills.json", list(seen))

    # --- 10. BILAN ET GRAPHIQUE (Dimanche 19h05) ---
    def send_weekly_summary(self):
        today_str = date.today().strftime("%Y-%m-%d")
        if self._check_already_sent("bilan", today_str):
            return

        self._refresh_session()
        if self._is_vacation():
            self._telegram("🏖️ *Bonnes vacances !*\nLe bilan est en pause.")
            self._mark_as_sent("bilan", today_str)
            return

        today = date.today()
        one_week_ago = today - timedelta(days=7)
        new_grades_count, total_pts, total_out = 0, 0, 0

        try:
            for period in self.client.periods:
                for g in period.grades:
                    if g.date >= one_week_ago and g.date <= today and g.grade and g.out_of:
                        try:
                            note = float(str(g.grade).replace(',', '.'))
                            bareme = float(str(g.out_of).replace(',', '.'))
                            if bareme > 0:
                                total_pts += (note / bareme) * 20
                                total_out += 1
                                new_grades_count += 1
                        except ValueError:
                            pass
        except Exception:
            pass

        hw_count = 0
        try:
            homeworks = self.client.homework(today, today + timedelta(days=7))
            hw_count = len(homeworks)
        except Exception:
            pass

        moyenne_str = f"{(total_pts / total_out):.2f}/20" if total_out > 0 else "—"
        msg = (
            f"📊 *Bilan de la semaine*\n\n"
            f"📝 Nouvelles notes : *{new_grades_count}*\n"
            f"📈 Moyenne : *{moyenne_str}*\n\n"
            f"📚 Devoirs à venir : *{hw_count}*\n"
        )
        self._telegram(msg)
        self._mark_as_sent("bilan", today_str)
        self._generate_grade_chart()

    def _generate_grade_chart(self):
        try:
            dates, averages = [], []
            for period in self.client.periods:
                if period.overall_average:
                    try:
                        averages.append(float(str(period.overall_average).replace(',', '.')))
                        dates.append(period.name)
                    except ValueError:
                        pass
            if not dates:
                return
            plt.figure(figsize=(10, 5))
            plt.plot(dates, averages, marker='o', linestyle='-', color='b')
            plt.title("Évolution de la moyenne générale")
            plt.ylabel("Moyenne /20")
            plt.grid(True)
            plt.xticks(rotation=45)
            plt.tight_layout()
            buf = io.BytesIO()
            plt.savefig(buf, format='png')
            buf.seek(0)
            plt.close()
            self._telegram_photo(buf, caption="📈 *Évolution de ta moyenne*")
        except Exception as e:
            log.error("Erreur graphique : %s", e)

    # --- 11. POLLING ET COMMANDES TELEGRAM ---
    def _poll_telegram(self):
        while True:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
                params = {'offset': self.last_update_id + 1, 'timeout': 30}
                r = requests.get(url, params=params, timeout=35)
                if r.status_code == 200:
                    for update in r.json().get('result', []):
                        self.last_update_id = update['update_id']
                        if 'message' in update and 'text' in update['message']:
                            self._handle_command(update['message']['text'])
                        if 'callback_query' in update:
                            cb = update['callback_query']
                            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb['id']})
                            self._handle_callback(cb['data'])
            except Exception as e:
                log.error("Erreur polling Telegram : %s", e)
            time.sleep(1)

    def _handle_callback(self, data):
        if data == "btn_notes":
            self._handle_command("/notes")
        elif data == "btn_devoirs":
            self._handle_command("/devoirs")
        elif data == "btn_emploi":
            self._handle_command("/emploi")

    def _handle_command(self, text):
        text = text.lower().strip()
        if text in ['/start', '/menu']:
            welcome = "👋 *Bienvenue !*\nUtilise les boutons ci-dessous pour naviguer."
            self._telegram(welcome, keyboard=self._get_main_keyboard())
            
        elif text == '/notes':
            self._refresh_session()
            msg = "*📝 Tes 5 dernières notes*\n\n"
            count = 0
            for period in self.client.periods:
                for g in period.grades:
                    if count >= 5: break
                    note_str = f"{g.grade}/{g.out_of}" if g.grade else "Non noté"
                    msg += f"• {g.date.strftime('%d/%m')} *{g.subject.name}* : {note_str}\n"
                    count += 1
                if count >= 5: break
            self._telegram(msg if count > 0 else "Aucune note trouvée.")
            
        elif text == '/devoirs':
            self._refresh_session()
            tomorrow = date.today() + timedelta(days=1)
            try:
                homeworks = self.client.homework(tomorrow, tomorrow)
                unfinished = [hw for hw in homeworks if not hw.done]
                if unfinished:
                    msg = "*📚 Devoirs pour demain*\n\n"
                    for hw in unfinished:
                        msg += f"• *{hw.subject.name}* : {hw.description[:100]}\n"
                    self._telegram(msg)
                else:
                    self._telegram("✅ Aucun devoir non fait pour demain !")
            except Exception:
                self._telegram("Erreur lors de la récupération des devoirs.")
                
        elif text == '/emploi':
            self._refresh_session()
            today = date.today()
            msg = "*📅 Emploi du temps de la semaine*\n\n"
            has_lessons = False
            for i in range(7):
                day = today + timedelta(days=i)
                if day.weekday() >= 5: continue
                try:
                    lessons = self.client.lessons(day, day)
                    if lessons:
                        has_lessons = True
                        msg += f"*{JOURS_FR[day.weekday()]} {day.strftime('%d/%m')}*\n"
                        for l in lessons:
                            salle = f" ({l.classroom})" if l.classroom else ""
                            msg += f"  • {l.start.strftime('%H:%M')} {l.subject.name}{salle}\n"
                except Exception:
                    pass
            self._telegram(msg if has_lessons else "Aucun cours cette semaine.")

# ---------------------------------------------------------------------------
# LANCEMENT
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
        bot.check_homework_and_timetable_daily()
        bot.check_informations()
        bot.check_discussions()
        bot.check_absences()
        bot.check_punishments()
        bot.check_skills()
        bot.check_menus()
        bot.send_weekly_summary()
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
    scheduler.add_job(bot.check_grades, "interval", minutes=CHECK_INTERVAL, next_run_time=datetime.now())
    scheduler.add_job(bot.check_lesson_changes, "interval", minutes=CHECK_INTERVAL)
    scheduler.add_job(bot.check_informations, "interval", minutes=CHECK_INTERVAL)
    scheduler.add_job(bot.check_discussions, "interval", minutes=CHECK_INTERVAL)
    scheduler.add_job(bot.check_absences, "interval", minutes=CHECK_INTERVAL)
    scheduler.add_job(bot.check_punishments, "interval", minutes=CHECK_INTERVAL)
    scheduler.add_job(bot.check_skills, "interval", minutes=CHECK_INTERVAL)
    
    scheduler.add_job(bot.check_menus, CronTrigger(day_of_week='sun', hour=18, minute=0))
    scheduler.add_job(bot.check_homework_and_timetable_daily, CronTrigger(hour=19, minute=30))
    scheduler.add_job(bot.send_weekly_summary, CronTrigger(day_of_week='sun', hour=19, minute=5))
    
    scheduler.start()
    log.info("Planificateur démarré.")

    threading.Thread(target=bot._poll_telegram, daemon=True).start()
    log.info("Polling Telegram démarré.")

if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
