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

JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
COULEURS_COMPETENCES = {
    "rouge": "🔴", "jaune": "🟡", "vert": "🟢", "vert foncé": "🟢", "bleu": "🔵"
}

class PronoteBot:
    def __init__(self):
        self.client = None
        self.last_update_id = 0
        self.is_paused = False

    # --- NETTOYAGE JSON ---
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

    # --- GIST ---
    def _gist_request(self, method, data=None):
        if not GIST_ID or not GITHUB_TOKEN: return None
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        url = f"https://api.github.com/gists/{GIST_ID}"
        try:
            if method == "GET": r = requests.get(url, headers=headers, timeout=10)
            elif method == "PATCH": r = requests.patch(url, headers=headers, json=data, timeout=10)
            if r.status_code == 200: return r.json()
        except Exception as e: log.error("Erreur Gist : %s", e)
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

    # --- CONNEXION ---
    def connect(self):
        log.info("Connexion à Pronote (Sécurisée)…")
        proxy_session = None
        if PROXIES_LIST:
            try:
                proxy_url = random.choice(PROXIES_LIST)
                proxy_session = requests.Session()
                proxy_session.trust_env = False
                proxy_session.proxies.update({"http": proxy_url, "https": proxy_url})
                time.sleep(random.uniform(1.0, 2.5))
            except Exception as e: log.error("Erreur proxy : %s", e)

        settings = self._load_gist_file("settings.json", {"paused": False})
        self.is_paused = settings.get("paused", False)

        creds = self._load_gist_file("creds.json")
        if creds: pass
        else:
            if not PRONOTE_CREDS_JSON: raise ValueError("Token manquant.")
            creds = self._parse_creds_robust(PRONOTE_CREDS_JSON)
            if not creds: raise ValueError("JSON illisible.")

        self.client = pronotepy.Client.token_login(**creds)
        if proxy_session: self.client.communication.session = proxy_session

        if not self.client.logged_in: raise RuntimeError("Token invalide ou Erreur 23")
        
        new_creds = self.client.export_credentials()
        self._save_gist_file("creds.json", new_creds)
        log.info("Connecté !")

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
        except Exception as e: log.error("Erreur photo : %s", e)

    def _telegram_document(self, file_bytes, filename, caption=""):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        try:
            files = {'document': (filename, file_bytes)}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}
            requests.post(url, data=data, files=files, timeout=30)
        except Exception as e: log.error("Erreur doc : %s", e)

    # --- MENUS ---
    def _get_main_keyboard(self):
        return {
            "keyboard": [
                [{"text": "📝 Notes"}, {"text": "📚 Devoirs demain"}],
                [{"text": "📅 Emploi du temps"}, {"text": "🍽️ Cantine"}],
                [{"text": "💬 Messages"}, {"text": "➕ Plus d'options"}],
                [{"text": "🔄 Actualiser (Sync)"}, {"text": "⏸️ Pause / ▶️ Reprendre"}]
            ],
            "resize_keyboard": True,
            "is_persistent": True
        }

    def _get_inline_menu(self):
        return {
            "inline_keyboard": [
                [{"text": "📊 Moyennes", "callback_data": "btn_moyennes"}, {"text": "📈 Graphique", "callback_data": "btn_graphique"}],
                [{"text": "⚠️ Contrôles / DS", "callback_data": "btn_ds"}, {"text": "📚 Devoirs (semaine)", "callback_data": "btn_semaine"}],
                [{"text": "📢 Infos & Sondages", "callback_data": "btn_infos"}]
            ]
        }

    # --- LE CŒUR DU SYSTÈME : LA SYNCHRONISATION ---
    def sync_data(self, manual=False):
        if self.is_paused and not manual:
            log.info("Sync annulée (mode pause).")
            return
            
        log.info("Début de la synchronisation globale avec Pronote...")
        if manual:
            self._telegram("⏳ _Synchronisation avec Pronote en cours..._")
            
        try:
            self.connect()
            
            # 1. Récupération des Notes
            grades_list = []
            for period in self.client.periods:
                overall = str(period.overall_average) if period.overall_average else "—"
                for g in period.grades:
                    grades_list.append({
                        "subject": g.subject.name,
                        "grade": str(g.grade) if g.grade else "Non noté",
                        "out_of": str(g.out_of) if g.out_of else "",
                        "date": g.date.strftime('%d/%m/%Y'),
                        "comment": g.comment or "—",
                        "overall": overall
                    })

            # 2. Récupération des Devoirs (7 prochains jours)
            hw_list = []
            today = date.today()
            for i in range(7):
                day = today + timedelta(days=i)
                if day.weekday() >= 5: continue
                hws = self.client.homework(day, day)
                for hw in hws:
                    if not hw.done:
                        hw_list.append({
                            "date": JOURS_FR[day.weekday()] + " " + day.strftime('%d/%m'),
                            "subject": hw.subject.name,
                            "description": hw.description
                        })

            # 3. Récupération de l'Emploi du temps (Semaine)
            edt_list = []
            for i in range(7):
                day = today + timedelta(days=i)
                if day.weekday() >= 5: continue
                lessons = self.client.lessons(day, day)
                for l in lessons:
                    edt_list.append({
                        "date": JOURS_FR[day.weekday()] + " " + day.strftime('%d/%m'),
                        "start": l.start.strftime('%H:%M'),
                        "subject": l.subject.name,
                        "room": l.classroom or "",
                        "canceled": l.canceled
                    })

            # 4. Récupération des Messages (5 derniers)
            msg_list = []
            discussions = self.client.messages()
            count = 0
            for disc in discussions:
                for m in disc.messages:
                    if count >= 5: break
                    msg_list.append({
                        "author": m.author,
                        "date": m.date.strftime('%d/%m'),
                        "subject": disc.subject,
                        "content": m.content,
                        "read": m.read
                    })
                    count += 1
                if count >= 5: break

            # 5. Récupération des Menus
            menu_list = []
            next_monday = today + timedelta(days=(7 - today.weekday()))
            menus = self.client.menus(next_monday, next_monday + timedelta(days=6))
            if menus:
                for menu in menus:
                    if hasattr(menu, 'date') and menu.date:
                        meals_str = ""
                        if hasattr(menu, 'meals') and menu.meals:
                            for meal in menu.meals:
                                if isinstance(meal, dict):
                                    meals_str += f"  • {meal.get('libelle', meal.get('name', str(meal)))}\n"
                                else:
                                    meals_str += f"  • {meal}\n"
                        menu_list.append({
                            "date": JOURS_FR[menu.date.weekday()] + " " + menu.date.strftime('%d/%m'),
                            "meals": meals_str
                        })

            # 6. Récupération des Infos / Sondages
            info_list = []
            infos = self.client.information_and_surveys()
            count = 0
            for item in infos:
                if count >= 5: break
                info_list.append({
                    "title": item.title,
                    "date": item.creation_date.strftime('%d/%m/%Y'),
                    "content": getattr(item, 'content', "—")[:200]
                })
                count += 1

            # 7. Calcul des moyennes
            averages_dict = {}
            try:
                current_period = self.client.current_period
                averages_dict["general"] = str(current_period.overall_average) if current_period.overall_average else "—"
                averages_dict["subjects"] = {}
                for subject, avg in current_period.averages.items():
                    averages_dict["subjects"][subject] = str(avg)
            except Exception:
                averages_dict["general"] = "—"

            # SAUVEGARDE DANS LE GIST
            cache = {
                "last_sync": datetime.now().strftime("%d/%m/%Y à %H:%M"),
                "grades": grades_list,
                "homework": hw_list,
                "timetable": edt_list,
                "messages": msg_list,
                "menus": menu_list,
                "infos": info_list,
                "averages": averages_dict
            }
            
            self._save_gist_file("pronote_cache.json", cache)
            log.info("Synchronisation terminée et sauvegardée dans le Gist.")
            
            if manual:
                self._telegram(f"✅ *Synchronisation réussie !*\nDernière mise à jour : {cache['last_sync']}")
                
        except Exception as e:
            log.error("Erreur de synchro : %s", e)
            if manual:
                self._telegram(f"❌ *Erreur de synchronisation :*\n`{str(e)}`\n\nPronote est peut-être saturé, réessaie plus tard.")

    # --- GRAPHIQUE (Basé sur le cache) ---
    def _generate_grade_chart(self):
        cache = self._load_gist_file("pronote_cache.json", {})
        if not cache or not cache.get("grades"):
            self._telegram("Aucune note en cache pour créer un graphique. Fais une sync d'abord.")
            return
            
        try:
            grades_data = []
            for g in cache["grades"]:
                if g["grade"] != "Non noté" and g["out_of"]:
                    try:
                        note = float(g["grade"].replace(',', '.'))
                        bareme = float(g["out_of"].replace(',', '.'))
                        if bareme > 0:
                            d = datetime.strptime(g["date"], "%d/%m/%Y")
                            grades_data.append((d, (note / bareme) * 20))
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
            self._telegram_photo(buf, caption="📈 *Ton évolution (basé sur le cache)*")
        except Exception as e:
            log.error("Erreur graphique : %s", e)

    # --- COMMANDES TELEGRAM (Lecture du cache) ---
    def _handle_command(self, text):
        text = text.lower().strip()
        
        if text in ['/stop', '⏸️ pause / ▶️ reprendre']:
            if self.is_paused:
                self.is_paused = False
                self._save_gist_file("settings.json", {"paused": False})
                self._telegram("🚀 *Bot réactivé !*")
            else:
                self.is_paused = True
                self._save_gist_file("settings.json", {"paused": True})
                self._telegram("💤 *Mode Pause activé.* Le bot ne fera plus de synchronisation automatique.")
            return
            
        elif text in ['/sync', '🔄 actualiser (sync)']:
            threading.Thread(target=self.sync_data, args=(True,)).start()
            return

        cache = self._load_gist_file("pronote_cache.json", {})
        last_sync = cache.get("last_sync", "Inconnue")
        
        if not cache:
            self._telegram("⚠️ Aucune donnée. Fais `/sync` d'abord.")
            return

        try:
            if text in ['/menu', 'menu', '➕ plus d\'options']:
                self._telegram("👋 Menu Principal :", keyboard=self._get_main_keyboard())
                msg = f"🛠️ *Menu Avancé* (Sync : {last_sync})"
                payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown", "reply_markup": self._get_inline_menu()}
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=15)
                
            elif text in ['/notes', '📝 notes']:
                grades = cache.get("grades", [])[:5]
                msg = f"*📝 Tes 5 dernières notes* (Sync : {last_sync})\n\n"
                for g in grades:
                    note_str = f"{g['grade']}/{g['out_of']}" if g['out_of'] else g['grade']
                    msg += f"• {g['date']} *{g['subject']}* : {note_str}\n"
                self._telegram(msg if grades else "Aucune note en cache.")
                
            elif text in ['/devoirs', '📚 devoirs demain']:
                msg = f"*📚 Devoirs pour les prochains jours* (Sync : {last_sync})\n\n"
                tomorrow_str = JOURS_FR[(date.today() + timedelta(days=1)).weekday()] + " " + (date.today() + timedelta(days=1)).strftime('%d/%m')
                hws = [h for h in cache.get("homework", []) if h["date"] == tomorrow_str]
                for h in hws:
                    msg += f"• *{h['subject']}* : {h['description'][:100]}\n"
                self._telegram(msg if hws else "✅ Aucun devoir pour demain (dans le cache).")

            elif text == '/semaine':
                hws = cache.get("homework", [])
                msg = f"*📚 Devoirs de la semaine* (Sync : {last_sync})\n\n"
                current_date = ""
                for h in hws:
                    if h["date"] != current_date:
                        current_date = h["date"]
                        msg += f"*{current_date}*\n"
                    msg += f"  • {h['subject']} : {h['description'][:80]}\n"
                self._telegram(msg if hws else "✅ Aucun devoir cette semaine.")
                    
            elif text in ['/emploi', '📅 emploi du temps']:
                edt = cache.get("timetable", [])
                msg = f"*📅 Emploi du temps* (Sync : {last_sync})\n\n"
                current_date = ""
                for l in edt:
                    if l["date"] != current_date:
                        current_date = l["date"]
                        msg += f"*{current_date}*\n"
                    salle = f" ({l['room']})" if l["room"] else ""
                    status = "❌ ANNULÉ" if l["canceled"] else l["subject"]
                    msg += f"  • {l['start']} {status}{salle}\n"
                self._telegram(msg if edt else "Aucun cours cette semaine.")
                
            elif text in ['/graphique', '📈 graphique']:
                self._generate_grade_chart()

            elif text in ['/cantine', '🍽️ cantine']:
                menus = cache.get("menus", [])
                msg = f"*🍽️ Menus de la cantine* (Sync : {last_sync})\n\n"
                for m in menus:
                    msg += f"*{m['date']}*\n{m['meals']}\n"
                self._telegram(msg if menus else "Aucun menu en cache.")

            elif text == '/moyennes':
                avgs = cache.get("averages", {})
                msg = f"*📊 Tes moyennes* (Sync : {last_sync})\n\n"
                msg += f"🎯 *Générale* : {avgs.get('general', '—')}/20\n\n"
                for subj, val in avgs.get("subjects", {}).items():
                    msg += f"• {subj} : {val}\n"
                self._telegram(msg)

            elif text == '/ds':
                hws = cache.get("homework", [])
                msg = f"*⚠️ Tes prochains contrôles / DS* (Sync : {last_sync})\n\n"
                found = False
                keywords = ["ds", "controle", "contrôle", "interro", "evaluation", "évaluation", "test"]
                for hw in hws:
                    if any(kw in hw["description"].lower() for kw in keywords):
                        found = True
                        msg += f"• *{hw['date']}* - {hw['subject']} :\n  {hw['description'][:100]}...\n"
                self._telegram(msg if found else "✅ Aucun contrôle détecté.")

            elif text == '/infos':
                infos = cache.get("infos", [])
                msg = f"*📢 Dernières informations* (Sync : {last_sync})\n\n"
                for i in infos:
                    msg += f"• *{i['title']}* ({i['date']})\n"
                self._telegram(msg if infos else "Aucune info en cache.")

            elif text in ['/messages', '💬 messages']:
                msgs = cache.get("messages", [])
                msg = f"*💬 Tes derniers messages* (Sync : {last_sync})\n\n"
                for m in msgs:
                    statut = "🟢" if not m["read"] else "⚪"
                    msg += f"{statut} *{m['author']}* ({m['date']}) :\n{m['subject']} - {m['content'][:80]}...\n\n"
                self._telegram(msg if msgs else "Aucun message.")
                
        except Exception as e:
            log.error(f"Erreur commande {text} : {e}")
            self._telegram(f"❌ *Erreur :*\n`{str(e)}`")

    def _poll_telegram(self):
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook")
        log.info("Écoute des messages Telegram...")
        while True:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
                params = {'offset': self.last_update_id + 1, 'timeout': 20}
                r = requests.get(url, params=params, timeout=25)
                if r.status_code == 200:
                    for update in r.json().get('result', []):
                        self.last_update_id = update['update_id']
                        if 'message' in update and 'text' in update['message']:
                            self._handle_command(update['message']['text'])
                        elif 'callback_query' in update:
                            cb = update['callback_query']
                            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb['id']})
                            self._handle_command(cb['data'].replace("btn_", "/"))
            except Exception as e:
                log.error("Erreur polling : %s", e)
            time.sleep(1)

# ---------------------------------------------------------------------------
# LANCEMENT
# ---------------------------------------------------------------------------
app = Flask(__name__)
bot = PronoteBot()

@app.route("/")
def health():
    return jsonify(status="ok", paused=bot.is_paused), 200

@app.route("/sync")
def force_sync():
    threading.Thread(target=bot.sync_data, args=(True,)).start()
    return jsonify(status="syncing"), 200

def scheduler_loop():
    scheduler = BackgroundScheduler(timezone="Europe/Paris")
    time.sleep(5)
    bot.sync_data(manual=False)
    scheduler.add_job(bot.sync_data, CronTrigger(hour=18, minute=0))
    
    scheduler.start()
    log.info("Planificateur (Mode Cache) démarré : Sync tous les jours à 18h.")

    threading.Thread(target=bot._poll_telegram, daemon=True).start()

if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
