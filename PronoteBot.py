import os
import json
import logging
import threading
import time
import io
from datetime import datetime, timedelta, date
from pathlib import Path

import requests
import pronotepy
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

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
IPLOOP_API_KEY     = os.environ.get("IPLOOP_API_KEY")

CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL_MIN", "30")) 

JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
COULEURS_COMPETENCES = {
    "rouge": "🔴", "jaune": "🟡", "vert": "🟢", "vert foncé": "🟢", "bleu": "🔵"
}

class PronoteBot:
    def __init__(self):
        self.client = None
        self.last_update_id = 0
        self.last_command_time = 0

    # --- GESTION DU GIST ---
    def _gist_request(self, method, data=None):
        if not GIST_ID or not GITHUB_TOKEN: return None
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        url = f"https://api.github.com/gists/{GIST_ID}"
        try:
            if method == "GET": r = requests.get(url, headers=headers, timeout=10)
            elif method == "PATCH": r = requests.patch(url, headers=headers, json=data, timeout=10)
            if r.status_code == 200: return r.json()
        except Exception as e: log.error("Erreur Gist API : %s", e)
        return None

    def _load_gist_file(self, filename, default=None):
        gist = self._gist_request("GET")
        if gist and "files" in gist and filename in gist["files"]:
            content = gist["files"][filename]["content"]
            if not content or content.strip() == "": return default
            try: return json.loads(content)
            except json.JSONDecodeError: return default
        return default

    def _save_gist_file(self, filename, data):
        files = {filename: {"content": json.dumps(data)}}
        self._gist_request("PATCH", {"files": files})

    # --- CONNEXION ---
    def connect(self):
        log.info("Connexion à Pronote via Token QR Code…")
        
        # --- CONFIGURATION DU PROXY IPLOOP ---
        proxy_session = None
        if IPLOOP_API_KEY:
            try:
                from iploop import IPLoop
                ip_client = IPLoop(api_key=IPLOOP_API_KEY, country="FR")
                proxy_session = ip_client.session()
                log.info("✅ Session IPLoop récupérée avec succès.")
            except Exception as e:
                log.error("❌ Impossible de configurer IPLoop : %s", e)

        creds = self._load_gist_file("creds.json")
        if creds:
            log.info("Token récupéré depuis le Gist.")
        else:
            if not PRONOTE_CREDS_JSON: 
                raise ValueError("Le Gist est vide et PRONOTE_CREDS_JSON est manquant sur Render.")
            try:
                creds = json.loads(PRONOTE_CREDS_JSON)
                log.info("Utilisation du token initial (Render).")
            except json.JSONDecodeError as e:
                log.error("❌ Le PRONOTE_CREDS_JSON est mal formaté : %s", e)
                self._telegram("❌ *Erreur de token* : Le JSON dans Render est mal formaté (vérifie les virgules et les sauts de ligne).")
                raise

        # Connexion
        self.client = pronotepy.Client.token_login(**creds)
        
        # --- INJECTION DE LA SESSION PROXY ---
        if proxy_session:
            self.client.communication.session = proxy_session
            log.info("✅ Session IPLoop injectée dans pronotepy.")
        # -------------------------------------

        if not self.client.logged_in: 
            raise RuntimeError("Token invalide ou IP bloquée (Erreur 23)")
        
        log.info("Connecté avec succès !")
        new_creds = self.client.export_credentials()
        self._save_gist_file("creds.json", new_creds)

    def _refresh_session(self):
        if self.client is None or not self.client.logged_in: self.connect()
        else:
            try: self.client.session_check()
            except Exception as e:
                log.warning("Session expirée, reconnexion… : %s", e)
                self.client = None
                self.connect()

    # --- ENVOIS TELEGRAM ---
    def _telegram(self, text, keyboard=None):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
        if keyboard: payload["reply_markup"] = keyboard
        try: requests.post(url, json=payload, timeout=15)
        except Exception as e: log.error("Erreur Telegram : %s", e)

    def _telegram_photo(self, photo_bytes, caption=""):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        try:
            files = {'photo': ('image.png', photo_bytes)}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
            requests.post(url, data=data, files=files, timeout=30)
        except Exception as e: log.error("Erreur envoi photo : %s", e)

    def _telegram_document(self, file_bytes, filename, caption=""):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        try:
            files = {'document': (filename, file_bytes)}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}
            requests.post(url, data=data, files=files, timeout=30)
        except Exception as e: log.error("Erreur envoi document : %s", e)

    def _set_telegram_commands(self):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands"
        commands = [
            {"command": "menu", "description": "Afficher le clavier principal"},
            {"command": "notes", "description": "Voir les 5 dernières notes"},
            {"command": "devoirs", "description": "Devoirs pour demain"},
            {"command": "emploi", "description": "Emploi du temps de la semaine"},
            {"command": "graphique", "description": "Voir l'évolution de la moyenne"}
        ]
        requests.post(url, json={"commands": commands})

    def _get_main_keyboard(self):
        return {
            "keyboard": [
                [{"text": "📝 Dernières notes"}, {"text": "📚 Devoirs pour demain"}],
                [{"text": "📅 Emploi du temps"}, {"text": "📈 Graphique"}]
            ],
            "resize_keyboard": True,
            "is_persistent": True
        }

    def _is_vacation(self):
        today = date.today()
        for i in range(1, 6):
            next_day = today + timedelta(days=i)
            if next_day.weekday() >= 5: continue
            try:
                if self.client.lessons(next_day, next_day): return False
            except Exception: pass
        return True

    def _check_already_sent(self, task_name, date_str):
        last_sent = self._load_gist_file("last_sent.json", {})
        return last_sent.get(task_name) == date_str

    def _mark_as_sent(self, task_name, date_str):
        last_sent = self._load_gist_file("last_sent.json", {})
        last_sent[task_name] = date_str
        self._save_gist_file("last_sent.json", last_sent)

    # --- TÂCHES AUTOMATIQUES ---
    def check_grades(self):
        try:
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
            if new_count: self._save_gist_file("seen_grades.json", list(seen))
        except Exception as e: log.error("Erreur check_grades : %s", e)

    def check_lesson_changes(self):
        try:
            self._refresh_session()
            old_lessons = self._load_gist_file("seen_lessons.json", {})
            new_lessons, changes = {}, []
            today = date.today()
            for i in range(0, 3):
                target_date = today + timedelta(days=i)
                lessons = self.client.lessons(target_date, target_date)
                for lesson in lessons:
                    lid = f"{target_date.strftime('%Y-%m-%d')}|{lesson.start.strftime('%H:%M')}|{lesson.subject.name}"
                    is_perm = getattr(lesson, 'is_permanent', False) or "perm" in str(lesson.subject.name).lower()
                    current_state = {"canceled": lesson.canceled or lesson.exempted, "room": getattr(lesson, 'classroom', ''), "subject": lesson.subject.name, "start": lesson.start.strftime("%d/%m à %H:%M"), "is_perm": is_perm}
                    new_lessons[lid] = current_state
                    if lid in old_lessons:
                        old_state = old_lessons[lid]
                        if current_state["canceled"] and not old_state["canceled"] and not old_state["is_perm"]:
                            changes.append(f"*⚠️ Cours ANNULÉ*\n📚 {lesson.subject.name}\n📅 {current_state['start']}")
                        elif current_state["room"] != old_state["room"] and not current_state["canceled"] and not current_state["is_perm"]:
                            changes.append(f"*🚪 Changement de salle*\n📚 {lesson.subject.name}\n📅 {current_state['start']}\n📍 {old_state['room']} ➡️ {current_state['room']}")
            if changes:
                for change in changes: self._telegram(change)
            self._save_gist_file("seen_lessons.json", new_lessons)
        except Exception as e: log.error("Erreur check_lesson_changes : %s", e)

    def check_informations(self):
        try:
            self._refresh_session()
            seen = set(self._load_gist_file("seen_infos.json", []))
            new_count = 0
            infos = self.client.information_and_surveys()
            for item in infos:
                iid = f"{item.creation_date.strftime('%Y-%m-%d')}|{item.title}"
                if iid not in seen:
                    is_sondage = "sondage" in str(item.category).lower() or "sondage" in item.title.lower()
                    icon = "📊" if is_sondage else "📢"
                    type_txt = "Nouveau Sondage" if is_sondage else "Nouvelle Information"
                    content = f"\n📝 {item.content[:200]}..." if getattr(item, 'content', None) else ""
                    msg = f"*{icon} {type_txt}*\n📌 {item.title}\n📅 {item.creation_date.strftime('%d/%m/%Y')}{content}"
                    self._telegram(msg)
                    seen.add(iid)
                    new_count += 1
            if new_count: self._save_gist_file("seen_infos.json", list(seen))
        except Exception as e: log.error("Erreur check_informations : %s", e)

    def check_discussions(self):
        try:
            self._refresh_session()
            seen = set(self._load_gist_file("seen_disc.json", []))
            new_count = 0
            discussions = self.client.messages()
            for disc in discussions:
                for msg in disc.messages:
                    if not msg.read:
                        mid = f"{disc.id}|{msg.id}"
                        if mid not in seen:
                            txt = f"*💬 Nouveau message – {msg.author}*\n📌 {disc.subject}\n📝 {msg.content[:150]}..."
                            self._telegram(txt)
                            seen.add(mid)
                            new_count += 1
            if new_count: self._save_gist_file("seen_disc.json", list(seen))
        except Exception as e: log.error("Erreur check_discussions : %s", e)

    def check_absences(self):
        try:
            self._refresh_session()
            seen = set(self._load_gist_file("seen_abs.json", []))
            new_count = 0
            absences = self.client.absences()
            for ab in absences:
                aid = f"{ab.from_date.strftime('%Y-%m-%d')}|{ab.to_date.strftime('%Y-%m-%d')}"
                if aid not in seen:
                    msg = f"*⚠️ Absence/Retard*\n📅 {ab.from_date.strftime('%d/%m/%Y %H:%M')}\n💬 {ab.reasons or '—'}"
                    self._telegram(msg)
                    seen.add(aid)
                    new_count += 1
            if new_count: self._save_gist_file("seen_abs.json", list(seen))
        except Exception as e: log.error("Erreur check_absences : %s", e)

    def check_punishments(self):
        try:
            self._refresh_session()
            seen = set(self._load_gist_file("seen_punishments.json", []))
            new_count = 0
            for period in self.client.periods:
                for pun in period.punishments:
                    pid = f"{pun.id}"
                    if pid not in seen:
                        msg = f"*⚖️ Nouvelle Punition/Sanction*\n📅 {pun.date.strftime('%d/%m/%Y')}\n💬 Motif : {pun.reason or '—'}\n⏱️ Durée : {pun.duration or '—'}"
                        self._telegram(msg)
                        seen.add(pid)
                        new_count += 1
            if new_count: self._save_gist_file("seen_punishments.json", list(seen))
        except Exception as e: log.error("Erreur check_punishments : %s", e)

    def check_skills(self):
        try:
            self._refresh_session()
            seen = set(self._load_gist_file("seen_skills.json", []))
            new_count = 0
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
            if new_count: self._save_gist_file("seen_skills.json", list(seen))
        except Exception as e: log.error("Erreur check_skills : %s", e)

    def check_homework_and_timetable_daily(self):
        today_str = date.today().strftime("%Y-%m-%d")
        if self._check_already_sent("devoirs_edt", today_str): return
        try:
            self._refresh_session()
            if self._is_vacation(): return
            today = date.today()
            if today.weekday() >= 5: return
            tomorrow = today + timedelta(days=1)
            
            timetable_msg = ""
            lessons = self.client.lessons(tomorrow, tomorrow)
            if lessons:
                timetable_msg = f"*📅 Emploi du temps de demain ({JOURS_FR[tomorrow.weekday()]} {tomorrow.strftime('%d/%m')})*\n"
                for l in lessons:
                    salle = f" - Salle {l.classroom}" if l.classroom else ""
                    prof = f" ({l.teacher_name})" if l.teacher_name else ""
                    timetable_msg += f"• {l.start.strftime('%H:%M')} : *{l.subject.name}*{prof}{salle}\n"
                timetable_msg += "\n"

            homework_msg = ""
            homeworks = self.client.homework(tomorrow, tomorrow)
            unfinished = [hw for hw in homeworks if not hw.done]
            if unfinished:
                homework_msg = "*📚 Devoirs NON FAITS pour demain*\n\n"
                for hw in unfinished:
                    homework_msg += f"• *{hw.subject.name}* : {hw.description[:150]}\n"
                    if hw.files:
                        for f in hw.files:
                            if f.type == 1:
                                try: self._telegram_document(f.data, f.name, caption=f"📎 Pièce jointe : {hw.subject.name}")
                                except: pass
                homework_msg += "\n"

            if timetable_msg or homework_msg:
                self._telegram(timetable_msg + homework_msg)
                self._mark_as_sent("devoirs_edt", today_str)
        except Exception as e: log.error("Erreur devoirs/edt : %s", e)

    def check_menus(self):
        today_str = date.today().strftime("%Y-%m-%d")
        if self._check_already_sent("menus", today_str): return
        try:
            self._refresh_session()
            today = date.today()
            next_monday = today + timedelta(days=(7 - today.weekday()))
            next_sunday = next_monday + timedelta(days=6)
            menus = self.client.menus(next_monday, next_sunday)
            if not menus: return

            msg = "*🍽️ Menus de la cantine (Semaine prochaine)*\n\n"
            has_content = False
            for menu in menus:
                if hasattr(menu, 'date') and menu.date:
                    jour_fr = JOURS_FR[menu.date.weekday()]
                    msg += f"*{jour_fr} {menu.date.strftime('%d/%m')}*\n"
                    if hasattr(menu, 'name') and menu.name: msg += f"_{menu.name}_\n"
                    if hasattr(menu, 'meals') and menu.meals:
                        for meal in menu.meals: msg += f"  • {meal}\n"
                        has_content = True
                    elif hasattr(menu, 'name') and menu.name: has_content = True
                    msg += "\n"
            if has_content:
                self._telegram(msg)
                self._mark_as_sent("menus", today_str)
        except Exception as e: log.error("Erreur menus : %s", e)

    def send_weekly_summary(self):
        today_str = date.today().strftime("%Y-%m-%d")
        if self._check_already_sent("bilan", today_str): return
        try:
            self._refresh_session()
            if self._is_vacation():
                self._telegram("🏖️ *Bonnes vacances !*")
                self._mark_as_sent("bilan", today_str)
                return

            today = date.today()
            one_week_ago = today - timedelta(days=7)
            new_grades_count, total_pts, total_out = 0, 0, 0
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
                        except ValueError: pass

            hw_count = 0
            try: hw_count = len(self.client.homework(today, today + timedelta(days=7)))
            except Exception: pass

            moyenne_str = f"{(total_pts / total_out):.2f}/20" if total_out > 0 else "—"
            msg = f"📊 *Bilan de la semaine*\n\n📝 Nouvelles notes : *{new_grades_count}*\n📈 Moyenne : *{moyenne_str}*\n\n📚 Devoirs à venir : *{hw_count}*\n"
            self._telegram(msg)
            self._mark_as_sent("bilan", today_str)
            self._generate_grade_chart()
        except Exception as e: log.error("Erreur bilan : %s", e)

    def _generate_grade_chart(self):
        try:
            grades_data = []
            for period in self.client.periods:
                for g in period.grades:
                    if g.grade and g.out_of:
                        try:
                            note = float(str(g.grade).replace(',', '.'))
                            bareme = float(str(g.out_of).replace(',', '.'))
                            if bareme > 0: grades_data.append((g.date, (note / bareme) * 20))
                        except ValueError: pass
            if not grades_data: return

            grades_data.sort(key=lambda x: x[0])
            dates = [g[0] for g in grades_data]
            notes = [g[1] for g in grades_data]

            plt.style.use('dark_background')
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(dates, notes, marker='o', linestyle='-', color='#00d2ff', linewidth=2, markersize=6, markerfacecolor='#ff0055')
            
            moyenne_generale = sum(notes) / len(notes)
            ax.axhline(y=moyenne_generale, color='#ff0055', linestyle='--', alpha=0.7, label=f'Moyenne ({moyenne_generale:.2f}/20)')
            
            ax.set_title("Évolution de tes notes (/20)", color='white', fontsize=14, pad=15)
            ax.set_ylabel("Note", color='#aaaaaa')
            ax.grid(color='#333333', linestyle=':', alpha=0.7)
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m'))
            fig.autofmt_xdate(rotation=45)
            ax.legend(facecolor='#222222', edgecolor='#555555')
            
            buf = io.BytesIO()
            plt.savefig(buf, format='png', bbox_inches='tight', dpi=150)
            buf.seek(0)
            plt.close()
            self._telegram_photo(buf, caption="📈 *Ton évolution au fil du temps*")
        except Exception as e:
            log.error("Erreur graphique : %s", e)

    # --- POLLING ET COMMANDES TELEGRAM ---
    def _poll_telegram(self):
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook")
        log.info("Écoute des messages Telegram en cours...")
        
        while True:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
                params = {'offset': self.last_update_id + 1, 'timeout': 20}
                r = requests.get(url, params=params, timeout=25)
                
                if r.status_code == 200:
                    data = r.json()
                    updates = data.get('result', [])
                    if updates:
                        log.info(f"📥 {len(updates)} message(s) reçu(s) de Telegram.")
                    
                    for update in updates:
                        self.last_update_id = update['update_id']
                        if 'message' in update and 'text' in update['message']:
                            log.info(f"💬 Commande reçue : {update['message']['text']}")
                            self._handle_command(update['message']['text'])
                elif r.status_code == 409:
                    log.warning("⚠️ Conflit Telegram (409) : Une autre instance du bot tourne encore. Attente...")
                    time.sleep(10)
                else:
                    log.error(f"Erreur Telegram API ({r.status_code}) : {r.text}")
                    
            except Exception as e:
                log.error("Erreur polling Telegram : %s", e)
            time.sleep(1)

    def _handle_command(self, text):
        text = text.lower().strip()
        
        if time.time() - self.last_command_time < 10 and text not in ['/start', '/menu', 'menu']:
            self._telegram("⏳ _Doucement ! Attends 10 secondes entre chaque commande pour ne pas faire bannir le serveur par Pronote._")
            return
            
        self.last_command_time = time.time()

        try:
            if text in ['/start', '/menu', 'menu']:
                welcome = "👋 *Bienvenue !*\nChoisis une option."
                self._telegram(welcome, keyboard=self._get_main_keyboard())
                
            elif text in ['/notes', '📝 dernières notes']:
                self._telegram("⏳ _Recherche de tes notes en cours..._")
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
                
            elif text in ['/devoirs', '📚 devoirs pour demain']:
                self._telegram("⏳ _Recherche des devoirs en cours..._")
                self._refresh_session()
                tomorrow = date.today() + timedelta(days=1)
                homeworks = self.client.homework(tomorrow, tomorrow)
                unfinished = [hw for hw in homeworks if not hw.done]
                if unfinished:
                    msg = "*📚 Devoirs pour demain*\n\n"
                    for hw in unfinished:
                        msg += f"• *{hw.subject.name}* : {hw.description[:100]}\n"
                    self._telegram(msg)
                else:
                    self._telegram("✅ Aucun devoir non fait pour demain !")
                    
            elif text in ['/emploi', '📅 emploi du temps']:
                self._telegram("⏳ _Recherche de l'emploi du temps..._")
                self._refresh_session()
                today = date.today()
                msg = "*📅 Emploi du temps de la semaine*\n\n"
                has_lessons = False
                for i in range(7):
                    day = today + timedelta(days=i)
                    if day.weekday() >= 5: continue
                    lessons = self.client.lessons(day, day)
                    if lessons:
                        has_lessons = True
                        msg += f"*{JOURS_FR[day.weekday()]} {day.strftime('%d/%m')}*\n"
                        for l in lessons:
                            salle = f" ({l.classroom})" if l.classroom else ""
                            msg += f"  • {l.start.strftime('%H:%M')} {l.subject.name}{salle}\n"
                self._telegram(msg if has_lessons else "Aucun cours cette semaine.")
                
            elif text in ['/graphique', '📈 graphique']:
                self._telegram("⏳ _Génération du graphique en cours..._")
                self._refresh_session()
                self._generate_grade_chart()
                
        except Exception as e:
            log.error(f"Erreur commande {text} : {e}")
            self._telegram(f"❌ *Oups, une erreur est survenue :*\n`{str(e)}`\n\nL'IP est sûrement bloquée (Erreur 23) ou le proxy a échoué. Réessaie plus tard.")

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
        bot.check_homework_and_timetable_daily()
        return jsonify(status="done"), 200
    except Exception as e:
        return jsonify(status="error", error=str(e)), 500

def scheduler_loop():
    try:
        bot.connect()
        bot._set_telegram_commands()
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

if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
