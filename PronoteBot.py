import os
import json
import logging
import threading
import time
import io
import re
import random
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
PROXIES_RAW        = os.environ.get("WEBSHARE_PROXIES", "")
PROXIES_LIST       = [p.strip() for p in PROXIES_RAW.split(",") if p.strip()]
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL_MIN", "30")) 

JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
COULEURS_COMPETENCES = {
    "rouge": "🔴", "jaune": "🟡", "vert": "🟢", "vert foncé": "🟢", "bleu": "🔵"
}

class PronoteBot:
    def __init__(self):
        self.client = None
        self.last_update_id = 0
        self.is_paused = False

    # --- FONCTION DE NETTOYAGE ULTIME ---
    def _parse_creds_robust(self, text):
        if not text: return None
        creds = {}
        match_url = re.search(r'"pronote_url"\s*:\s*"([^"]+)"', text)
        match_user = re.search(r'"username"\s*:\s*"([^"]+)"', text)
        match_pass = re.search(r'"password"\s*:\s*"([^"]+)"', text)
        match_client = re.search(r'"client_identifier"\s*:\s*"([^"]+)"', text)
        match_uuid = re.search(r'"uuid"\s*:\s*"([^"]+)"', text)

        if match_url: creds["pronote_url"] = match_url.group(1)
        if match_user: creds["username"] = match_user.group(1)
        if match_pass: creds["password"] = match_pass.group(1)
        if match_client: creds["client_identifier"] = match_client.group(1)
        if match_uuid: creds["uuid"] = match_uuid.group(1)

        if "password" in creds and "client_identifier" in creds:
            return creds
        return None

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
            try: return self._parse_creds_robust(content) or json.loads(content)
            except: return default
        return default

    def _save_gist_file(self, filename, data):
        files = {filename: {"content": json.dumps(data)}}
        self._gist_request("PATCH", {"files": files})

    # --- CONNEXION SÉCURISÉE ---
    def connect(self):
        log.info("Connexion à Pronote (Sécurisée)…")
        proxy_session = None
        if PROXIES_LIST:
            try:
                proxy_url = random.choice(PROXIES_LIST)
                proxy_session = requests.Session()
                proxy_session.trust_env = False
                proxy_session.proxies.update({"http": proxy_url, "https": proxy_url})
                log.info(f"✅ Session proxy configurée (masquée).")
                time.sleep(random.uniform(1.5, 3.5))
            except Exception as e: log.error("❌ Erreur proxy : %s", e)

        settings = self._load_gist_file("settings.json", {"paused": False})
        self.is_paused = settings.get("paused", False)

        creds = self._load_gist_file("creds.json")
        if creds:
            log.info("Token récupéré depuis le Gist.")
        else:
            if not PRONOTE_CREDS_JSON: raise ValueError("PRONOTE_CREDS_JSON manquant.")
            creds = self._parse_creds_robust(PRONOTE_CREDS_JSON)
            if not creds: raise ValueError("JSON illisible.")
            log.info("Utilisation du token initial.")

        self.client = pronotepy.Client.token_login(**creds)
        if proxy_session: self.client.communication.session = proxy_session

        if not self.client.logged_in: raise RuntimeError("Token invalide ou Erreur 23")
        
        log.info("Connecté avec succès !")
        new_creds = self.client.export_credentials()
        self._save_gist_file("creds.json", new_creds)

    def _refresh_session(self):
        if self.client is None or not self.client.logged_in: 
            self.connect()
        else:
            try: 
                time.sleep(random.uniform(0.5, 1.5))
                self.client.session_check()
            except Exception:
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

    # --- CLAVIERS ---
    def _get_main_keyboard(self):
        return {
            "keyboard": [
                [{"text": "📝 Notes"}, {"text": "📚 Devoirs demain"}],
                [{"text": "📅 Emploi du temps"}, {"text": "🍽️ Cantine"}],
                [{"text": "💬 Messages"}, {"text": "➕ Plus d'options"}],
                [{"text": "⏸️ Pause / ▶️ Reprendre"}]
            ],
            "resize_keyboard": True,
            "is_persistent": True
        }

    def _get_inline_menu(self):
        return {
            "inline_keyboard": [
                [{"text": "📊 Moyennes", "callback_data": "btn_moyennes"}, {"text": "📈 Graphique", "callback_data": "btn_graphique"}],
                [{"text": "⚠️ Contrôles / DS", "callback_data": "btn_ds"}, {"text": "📚 Devoirs (semaine)", "callback_data": "btn_semaine"}],
                [{"text": "📢 Infos & Sondages", "callback_data": "btn_infos"}, {"text": "📄 Fichiers (24h)", "callback_data": "btn_fichiers"}],
                [{"text": "💬 Messages non lus", "callback_data": "btn_messages"}]
            ]
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
        return last_sent.get(task_name) == date_str if isinstance(last_sent, dict) else False

    def _mark_as_sent(self, task_name, date_str):
        last_sent = self._load_gist_file("last_sent.json", {})
        if not isinstance(last_sent, dict): last_sent = {}
        last_sent[task_name] = date_str
        self._save_gist_file("last_sent.json", last_sent)

    # --- FICHIERS DES COURS (Automatique) ---
    def check_lesson_files(self):
        if self.is_paused: return
        try:
            self._refresh_session()
            seen_files = set(self._load_gist_file("seen_files.json", []))
            new_count = 0
            today = date.today()
            lessons = self.client.lessons(today - timedelta(days=1), today + timedelta(days=1))
            for lesson in lessons:
                if hasattr(lesson, 'content') and lesson.content:
                    for item in lesson.content:
                        if getattr(item, 'type', 0) == 1:
                            fid = f"{lesson.subject.name}|{item.name}"
                            if fid not in seen_files:
                                try:
                                    self._telegram_document(item.data, item.name, caption=f"📄 Nouveau document : *{lesson.subject.name}*")
                                    seen_files.add(fid)
                                    new_count += 1
                                except Exception: pass
            if new_count: self._save_gist_file("seen_files.json", list(seen_files))
        except Exception as e: log.error("Erreur check_lesson_files : %s", e)

    # --- TÂCHES AUTOMATIQUES ---
    def check_grades(self):
        if self.is_paused: return
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
        if self.is_paused: return
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
                        if current_state["canceled"] and not old_state.get("canceled", False) and not old_state.get("is_perm", False):
                            changes.append(f"*⚠️ Cours ANNULÉ*\n📚 {lesson.subject.name}\n📅 {current_state['start']}")
                        elif current_state["room"] != old_state.get("room", '') and not current_state["canceled"] and not current_state["is_perm"]:
                            changes.append(f"*🚪 Changement de salle*\n📚 {lesson.subject.name}\n📅 {current_state['start']}\n📍 {old_state.get('room', '')} ➡️ {current_state['room']}")
            if changes:
                for change in changes: self._telegram(change)
            self._save_gist_file("seen_lessons.json", new_lessons)
        except Exception as e: log.error("Erreur check_lesson_changes : %s", e)

    def check_informations(self):
        if self.is_paused: return
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
        if self.is_paused: return
        try:
            self._refresh_session()
            seen = set(self._load_gist_file("seen_disc.json", []))
            new_count = 0
            # CORRECTION ICI : self.client.messages() -> self.client.discussions()
            discussions = self.client.discussions()
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
        if self.is_paused: return
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
        if self.is_paused: return
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
        if self.is_paused: return
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
        if self.is_paused: return
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
                homework_msg += "\n"

            if timetable_msg or homework_msg:
                self._telegram(timetable_msg + homework_msg)
                self._mark_as_sent("devoirs_edt", today_str)
        except Exception as e: log.error("Erreur devoirs/edt : %s", e)

    def check_menus(self):
        if self.is_paused: return
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
                        for meal in menu.meals:
                            if isinstance(meal, dict):
                                msg += f"  • {meal.get('libelle', meal.get('name', str(meal)))}\n"
                            else:
                                msg += f"  • {meal}\n"
                        has_content = True
                    elif hasattr(menu, 'name') and menu.name: has_content = True
                    msg += "\n"
            if has_content:
                self._telegram(msg)
                self._mark_as_sent("menus", today_str)
        except Exception as e: log.error("Erreur menus : %s", e)

    def send_weekly_summary(self):
        if self.is_paused: return
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

    # --- POLLING ET GESTION DES BOUTONS ---
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
                    if updates: log.info(f"📥 {len(updates)} interaction(s) reçue(s).")
                    
                    for update in updates:
                        self.last_update_id = update['update_id']
                        if 'message' in update and 'text' in update['message']:
                            self._handle_command(update['message']['text'])
                        elif 'callback_query' in update:
                            cb = update['callback_query']
                            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb['id']})
                            self._handle_callback(cb['data'])
                            
                elif r.status_code == 409: time.sleep(10)
                else: log.error(f"Erreur Telegram API ({r.status_code})")
                    
            except Exception as e:
                log.error("Erreur polling Telegram : %s", e)
            time.sleep(1)

    def _handle_callback(self, data):
        if data == "btn_moyennes": self._handle_command("/moyennes")
        elif data == "btn_graphique": self._handle_command("/graphique")
        elif data == "btn_ds": self._handle_command("/ds")
        elif data == "btn_semaine": self._handle_command("/semaine")
        elif data == "btn_infos": self._handle_command("/infos")
        elif data == "btn_fichiers": self._handle_command("/fichiers")
        elif data == "btn_messages": self._handle_command("/messages")

    def _handle_command(self, text):
        text = text.lower().strip()
        
        # --- GESTION PAUSE / REPRENDRE ---
        if text in ['/stop', '⏸️ pause / ▶️ reprendre']:
            if self.is_paused:
                self.is_paused = False
                self._save_gist_file("settings.json", {"paused": False})
                self._telegram("🚀 *Bot réactivé !* Je recommence à surveiller Pronote.")
            else:
                self.is_paused = True
                self._save_gist_file("settings.json", {"paused": True})
                self._telegram("💤 *Mode Pause activé.* Le bot est en veille.")
            return
            
        elif text == '/start':
            self.is_paused = False
            self._save_gist_file("settings.json", {"paused": False})
            self._telegram("🚀 *Bot réactivé !*", keyboard=self._get_main_keyboard())
            return

        try:
            if text in ['/menu', 'menu', '➕ plus d\'options']:
                self._telegram("👋 Voici le menu principal :", keyboard=self._get_main_keyboard())
                msg = "🛠️ *Menu Avancé*"
                payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown", "reply_markup": self._get_inline_menu()}
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=15)
                
            elif text in ['/notes', '📝 notes']:
                self._telegram("⏳ _Recherche en cours..._")
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
                
            elif text in ['/devoirs', '📚 devoirs demain']:
                self._telegram("⏳ _Recherche en cours..._")
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
                    self._telegram("✅ Aucun devoir non fait !")

            elif text == '/semaine':
                self._telegram("⏳ _Recherche en cours..._")
                self._refresh_session()
                today = date.today()
                msg = "*📚 Devoirs de la semaine*\n\n"
                has_hw = False
                for i in range(7):
                    day = today + timedelta(days=i)
                    if day.weekday() >= 5: continue
                    hws = self.client.homework(day, day)
                    unfinished = [hw for hw in hws if not hw.done]
                    if unfinished:
                        has_hw = True
                        msg += f"*{JOURS_FR[day.weekday()]} {day.strftime('%d/%m')}*\n"
                        for hw in unfinished:
                            msg += f"  • {hw.subject.name} : {hw.description[:80]}\n"
                self._telegram(msg if has_hw else "✅ Aucun devoir cette semaine !")
                    
            elif text in ['/emploi', '📅 emploi du temps']:
                self._telegram("⏳ _Recherche en cours..._")
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
                self._telegram("⏳ _Génération en cours..._")
                self._refresh_session()
                self._generate_grade_chart()

            elif text in ['/cantine', '🍽️ cantine']:
                self._telegram("⏳ _Recherche du menu..._")
                self._refresh_session()
                today = date.today()
                next_monday = today + timedelta(days=(7 - today.weekday()))
                next_sunday = next_monday + timedelta(days=6)
                menus = self.client.menus(next_monday, next_sunday)
                if not menus:
                    self._telegram("Aucun menu publié.")
                else:
                    msg = "*🍽️ Menus de la cantine*\n\n"
                    for menu in menus:
                        if hasattr(menu, 'date') and menu.date:
                            msg += f"*{JOURS_FR[menu.date.weekday()]} {menu.date.strftime('%d/%m')}*\n"
                            if hasattr(menu, 'name') and menu.name: msg += f"_{menu.name}_\n"
                            
                            if hasattr(menu, 'meals') and menu.meals:
                                for meal in menu.meals:
                                    if isinstance(meal, dict):
                                        msg += f"  • {meal.get('libelle', meal.get('name', str(meal)))}\n"
                                    else:
                                        msg += f"  • {meal}\n"
                            msg += "\n"
                    self._telegram(msg)

            elif text == '/debug_menu':
                self._telegram("⏳ _Analyse des données brutes..._")
                self._refresh_session()
                today = date.today()
                next_monday = today + timedelta(days=(7 - today.weekday()))
                menus = self.client.menus(next_monday, next_monday + timedelta(days=6))
                if not menus:
                    self._telegram("Aucun menu à analyser.")
                else:
                    for m in menus:
                        debug_msg = f"Date: {m.date}\nNom: {m.name}\nRepas brut: `{str(m.meals)}`\n"
                        self._telegram(debug_msg)

            elif text == '/moyennes':
                self._telegram("⏳ _Calcul des moyennes..._")
                self._refresh_session()
                msg = "*📊 Tes moyennes*\n\n"
                try:
                    current_period = self.client.current_period
                    try:
                        overall = float(str(current_period.overall_average).replace(',', '.'))
                        msg += f"🎯 *Générale* : {overall:.2f}/20\n\n"
                    except:
                        msg += f"🎯 *Générale* : {current_period.overall_average}/20\n\n"

                    for subject, avg in current_period.averages.items():
                        try:
                            avg_float = float(str(avg).replace(',', '.'))
                            msg += f"• {subject} : {avg_float:.2f}\n"
                        except:
                            msg += f"• {subject} : {avg}\n"
                    self._telegram(msg)
                except Exception as e:
                    self._telegram(f"❌ Erreur : {str(e)}")

            elif text == '/ds':
                self._telegram("⏳ _Recherche des contrôles..._")
                self._refresh_session()
                today = date.today()
                msg = "*⚠️ Tes prochains contrôles / DS*\n\n"
                found = False
                keywords = ["ds", "controle", "contrôle", "interro", "evaluation", "évaluation", "test"]
                for i in range(7):
                    day = today + timedelta(days=i)
                    if day.weekday() >= 5: continue
                    hws = self.client.homework(day, day)
                    for hw in hws:
                        desc_lower = hw.description.lower()
                        if any(kw in desc_lower for kw in keywords):
                            found = True
                            msg += f"• *{JOURS_FR[day.weekday()]} {day.strftime('%d/%m')}* - {hw.subject.name} :\n  {hw.description[:100]}...\n"
                self._telegram(msg if found else "✅ Aucun contrôle détecté.")

            elif text == '/infos':
                self._telegram("⏳ _Recherche des infos..._")
                self._refresh_session()
                infos = self.client.information_and_surveys()
                msg = "*📢 Dernières informations*\n\n"
                count = 0
                for item in infos:
                    if count >= 5: break
                    msg += f"• *{item.title}* ({item.creation_date.strftime('%d/%m')})\n"
                    count += 1
                self._telegram(msg if count > 0 else "Aucune nouvelle information.")

            elif text == '/fichiers':
                self._telegram("⏳ _Recherche des fichiers récents..._")
                self._refresh_session()
                today = date.today()
                lessons = self.client.lessons(today - timedelta(days=1), today)
                found = False
                for lesson in lessons:
                    if hasattr(lesson, 'content') and lesson.content:
                        for item in lesson.content:
                            if getattr(item, 'type', 0) == 1:
                                found = True
                                self._telegram_document(item.data, item.name, caption=f"📄 {lesson.subject.name}")
                if not found:
                    self._telegram("Aucun nouveau fichier aujourd'hui.")

            elif text in ['/messages', '💬 messages']:
                self._telegram("⏳ _Recherche des messages..._")
                self._refresh_session()
                # CORRECTION ICI : self.client.messages() -> self.client.discussions()
                discussions = self.client.discussions()
                msg = "*💬 Tes derniers messages*\n\n"
                count = 0
                for disc in discussions:
                    for m in disc.messages:
                        if count >= 5: break
                        statut = "🟢" if not m.read else "⚪"
                        msg += f"{statut} *{m.author}* ({m.date.strftime('%d/%m')}) :\n{disc.subject} - {m.content[:80]}...\n\n"
                        count += 1
                    if count >= 5: break
                self._telegram(msg if count > 0 else "Aucun message.")
                
        except Exception as e:
            log.error(f"Erreur commande {text} : {e}")
            self._telegram(f"❌ *Erreur :*\n`{str(e)}`")

# ---------------------------------------------------------------------------
# LANCEMENT
# ---------------------------------------------------------------------------
app = Flask(__name__)
bot = PronoteBot()

@app.route("/")
def health():
    return jsonify(status="ok", connected=bot.client is not None and bot.client.logged_in, paused=bot.is_paused), 200

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
    scheduler.add_job(bot.check_lesson_files, "interval", minutes=CHECK_INTERVAL)
    
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
