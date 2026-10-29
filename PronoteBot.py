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
            try: self.client.session_check()
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

    # --- MISE A JOUR DU CACHE (Appelé 4 fois par jour) ---
    def update_cache(self):
        if self.is_paused: 
            log.info("Bot en pause, pas de mise à jour du cache.")
            return
            
        try:
            self._refresh_session()
            log.info("Mise à jour du cache en cours...")
            cache = {}

            # 1. Notes et Moyennes
            cache["grades"] = []
            cache["averages"] = {}
            try:
                period = self.client.current_period
                overall = getattr(period, 'overall_average', '—')
                try: cache["averages"]["general"] = float(str(overall).replace(',', '.'))
                except: cache["averages"]["general"] = str(overall)
                
                for avg in period.averages:
                    subj = getattr(avg.subject, 'name', 'Inconnu') if hasattr(avg, 'subject') else 'Inconnu'
                    val = getattr(avg, 'average', '—')
                    try: cache["averages"][subj] = float(str(val).replace(',', '.'))
                    except: cache["averages"][subj] = str(val)

                for g in period.grades:
                    cache["grades"].append({
                        "subject": g.subject.name,
                        "grade": str(g.grade),
                        "out_of": str(g.out_of),
                        "date": g.date.strftime('%d/%m/%Y'),
                        "comment": g.comment or '—'
                    })
            except Exception as e: log.error("Erreur cache notes : %s", e)

            # 2. Devoirs
            cache["homework"] = []
            try:
                today = date.today()
                for i in range(7):
                    day = today + timedelta(days=i)
                    if day.weekday() >= 5: continue
                    hws = self.client.homework(day, day)
                    for hw in hws:
                        if not hw.done:
                            cache["homework"].append({
                                "date": f"{JOURS_FR[day.weekday()]} {day.strftime('%d/%m')}",
                                "subject": hw.subject.name,
                                "description": hw.description
                            })
            except Exception as e: log.error("Erreur cache devoirs : %s", e)

            # 3. Emploi du temps
            cache["timetable"] = []
            try:
                for i in range(7):
                    day = today + timedelta(days=i)
                    if day.weekday() >= 5: continue
                    lessons = self.client.lessons(day, day)
                    for l in lessons:
                        cache["timetable"].append({
                            "date": f"{JOURS_FR[day.weekday()]} {day.strftime('%d/%m')}",
                            "time": l.start.strftime('%H:%M'),
                            "subject": l.subject.name,
                            "room": getattr(l, 'classroom', '')
                        })
            except Exception as e: log.error("Erreur cache emploi : %s", e)

            # 4. Messages
            cache["messages"] = []
            try:
                discussions = self.client.discussions()
                for disc in discussions:
                    for m in disc.messages:
                        if not getattr(m, 'read', True):
                            cache["messages"].append({
                                "author": m.author,
                                "subject": disc.subject,
                                "content": m.content[:100],
                                "date": m.date.strftime('%d/%m')
                            })
            except Exception as e: log.error("Erreur cache messages : %s", e)

            # 5. Informations
            cache["infos"] = []
            try:
                infos = self.client.information_and_surveys()
                for item in infos[:5]:
                    cache["infos"].append({
                        "title": item.title,
                        "date": item.creation_date.strftime('%d/%m'),
                        "content": getattr(item, 'content', '')[:200]
                    })
            except Exception as e: log.error("Erreur cache infos : %s", e)

            # 6. Cantine
            cache["menus"] = []
            try:
                next_monday = today + timedelta(days=(7 - today.weekday()))
                menus = self.client.menus(next_monday, next_monday + timedelta(days=6))
                for m in menus:
                    if hasattr(m, 'date') and m.date:
                        meals = []
                        if hasattr(m, 'meals') and m.meals:
                            for meal in m.meals:
                                if isinstance(meal, dict):
                                    name = meal.get('nom', meal.get('libelle', meal.get('name', '')))
                                    if name: meals.append(name)
                                else: meals.append(str(meal))
                        cache["menus"].append({
                            "date": f"{JOURS_FR[m.date.weekday()]} {m.date.strftime('%d/%m')}",
                            "name": getattr(m, 'name', ''),
                            "meals": meals
                        })
            except Exception as e: log.error("Erreur cache cantine : %s", e)

            cache["last_update"] = datetime.now().strftime("%d/%m/%Y à %H:%M")
            self._save_gist_file("cache_data.json", cache)
            log.info("✅ Cache mis à jour avec succès.")

        except Exception as e:
            log.error("❌ Erreur lors de la mise à jour du cache : %s", e)

    # --- COMMANDES TELEGRAM (Lecture seule dans le cache) ---
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
                self._telegram("💤 *Mode Pause activé.*")
            return
            
        elif text == '/start':
            self.is_paused = False
            self._save_gist_file("settings.json", {"paused": False})
            self._telegram("🚀 *Bot réactivé !*", keyboard=self._get_main_keyboard())
            return

        # Lecture du cache
        cache = self._load_gist_file("cache_data.json")
        if not cache or "last_update" not in cache:
            self._telegram("⚠️ *Le cache est vide.* Attends la prochaine mise à jour automatique (9h, 12h, 14h30 ou 19h30).")
            return

        last_up = cache["last_update"]

        try:
            if text in ['/menu', 'menu', '➕ plus d\'options']:
                self._telegram(f"👋 Voici le menu principal.\n_(Dernière mise à jour : {last_up})_", keyboard=self._get_main_keyboard())
                payload = {"chat_id": TELEGRAM_CHAT_ID, "text": "🛠️ *Menu Avancé*", "parse_mode": "Markdown", "reply_markup": self._get_inline_menu()}
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=15)
                
            elif text in ['/notes', '📝 notes']:
                msg = f"*📝 Tes 5 dernières notes*\n_(MàJ : {last_up})_\n\n"
                grades = cache.get("grades", [])[:5]
                if grades:
                    for g in grades:
                        msg += f"• {g['date']} *{g['subject']}* : {g['grade']}/{g['out_of']}\n"
                    self._telegram(msg)
                else:
                    self._telegram("Aucune note dans le cache.")

            elif text in ['/moyennes']:
                msg = f"*📊 Tes moyennes*\n_(MàJ : {last_up})_\n\n"
                avgs = cache.get("averages", {})
                gen = avgs.pop("general", "—") # On extrait la générale
                msg += f"🎯 *Générale* : {gen}/20\n\n"
                for subj, val in avgs.items():
                    msg += f"• {subj} : {val}\n"
                self._telegram(msg)

            elif text in ['/devoirs', '📚 devoirs demain', '📚 devoirs semaine', '/semaine']:
                msg = f"*📚 Devoirs de la semaine*\n_(MàJ : {last_up})_\n\n"
                hws = cache.get("homework", [])
                current_date = ""
                if hws:
                    for hw in hws:
                        if hw['date'] != current_date:
                            current_date = hw['date']
                            msg += f"*{current_date}*\n"
                        msg += f"  • {hw['subject']} : {hw['description'][:80]}\n"
                    self._telegram(msg)
                else:
                    self._telegram("✅ Aucun devoir dans le cache.")

            elif text in ['/emploi', '📅 emploi du temps']:
                msg = f"*📅 Emploi du temps*\n_(MàJ : {last_up})_\n\n"
                edt = cache.get("timetable", [])
                current_date = ""
                if edt:
                    for l in edt:
                        if l['date'] != current_date:
                            current_date = l['date']
                            msg += f"*{current_date}*\n"
                        room = f" ({l['room']})" if l['room'] else ""
                        msg += f"  • {l['time']} {l['subject']}{room}\n"
                    self._telegram(msg)
                else:
                    self._telegram("Aucun cours dans le cache.")

            elif text in ['/messages', '💬 messages']:
                msg = f"*💬 Messages non lus*\n_(MàJ : {last_up})_\n\n"
                msgs = cache.get("messages", [])
                if msgs:
                    for m in msgs:
                        msg += f"🟢 *{m['author']}* ({m['date']}) :\n{m['subject']} - {m['content']}...\n\n"
                    self._telegram(msg)
                else:
                    self._telegram("Aucun nouveau message.")

            elif text in ['/infos']:
                msg = f"*📢 Dernières informations*\n_(MàJ : {last_up})_\n\n"
                infos = cache.get("infos", [])
                if infos:
                    for i in infos:
                        msg += f"• *{i['title']}* ({i['date']})\n"
                    self._telegram(msg)
                else:
                    self._telegram("Aucune info.")

            elif text in ['/cantine', '🍽️ cantine']:
                msg = f"*🍽️ Menus de la cantine*\n_(MàJ : {last_up})_\n\n"
                menus = cache.get("menus", [])
                if menus:
                    for m in menus:
                        msg += f"*{m['date']}*\n"
                        if m['name']: msg += f"_{m['name']}_\n"
                        for meal in m['meals']:
                            msg += f"  • {meal}\n"
                        msg += "\n"
                    self._telegram(msg)
                else:
                    self._telegram("Aucun menu.")

            elif text in ['/ds']:
                msg = f"*⚠️ Tes prochains contrôles / DS*\n_(MàJ : {last_up})_\n\n"
                hws = cache.get("homework", [])
                keywords = ["ds", "controle", "contrôle", "interro", "evaluation", "évaluation", "test"]
                found = False
                for hw in hws:
                    if any(kw in hw['description'].lower() for kw in keywords):
                        found = True
                        msg += f"• *{hw['date']}* - {hw['subject']} :\n  {hw['description'][:100]}...\n"
                self._telegram(msg if found else "✅ Aucun contrôle détecté.")

            elif text in ['/graphique', '📈 graphique']:
                self._telegram("⏳ _Génération du graphique depuis le cache..._")
                grades = cache.get("grades", [])
                if not grades:
                    self._telegram("Pas assez de notes pour faire un graphique.")
                    return
                
                # Recréation du graphique avec les données du cache
                import matplotlib.pyplot as plt
                import matplotlib.dates as mdates
                from datetime import datetime
                
                grades_data = []
                for g in grades:
                    try:
                        note = float(g['grade'].replace(',', '.'))
                        bareme = float(g['out_of'].replace(',', '.'))
                        if bareme > 0:
                            dt = datetime.strptime(g['date'], '%d/%m/%Y')
                            grades_data.append((dt, (note / bareme) * 20))
                    except: pass
                
                if not grades_data: return
                grades_data.sort(key=lambda x: x[0])
                dates = [g[0] for g in grades_data]
                notes = [g[1] for g in grades_data]

                plt.style.use('dark_background')
                fig, ax = plt.subplots(figsize=(10, 5))
                ax.plot(dates, notes, marker='o', linestyle='-', color='#00d2ff', linewidth=2, markerfacecolor='#ff0055')
                moyenne = sum(notes) / len(notes)
                ax.axhline(y=moyenne, color='#ff0055', linestyle='--', label=f'Moyenne ({moyenne:.2f}/20)')
                ax.set_title("Évolution de tes notes (/20)", color='white')
                ax.grid(color='#333333', linestyle=':')
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m'))
                fig.autofmt_xdate(rotation=45)
                ax.legend()
                
                buf = io.BytesIO()
                plt.savefig(buf, format='png', bbox_inches='tight', dpi=150)
                buf.seek(0)
                plt.close()
                self._telegram_photo(buf, caption="📈 *Ton évolution (depuis le cache)*")
                
        except Exception as e:
            log.error(f"Erreur commande cache {text} : {e}")
            self._telegram(f"❌ *Erreur de lecture du cache :*\n`{str(e)}`")

    # --- POLLING TELEGRAM ---
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
                    for update in updates:
                        self.last_update_id = update['update_id']
                        if 'message' in update and 'text' in update['message']:
                            self._handle_command(update['message']['text'])
                        elif 'callback_query' in update:
                            cb = update['callback_query']
                            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb['id']})
                            data = cb['data']
                            # Redirection des boutons vers les commandes
                            if data == "btn_moyennes": self._handle_command("/moyennes")
                            elif data == "btn_graphique": self._handle_command("/graphique")
                            elif data == "btn_ds": self._handle_command("/ds")
                            elif data == "btn_semaine": self._handle_command("/semaine")
                            elif data == "btn_infos": self._handle_command("/infos")
                            elif data == "btn_messages": self._handle_command("/messages")
                            
                elif r.status_code == 409: time.sleep(10)
                    
            except Exception as e:
                log.error("Erreur polling Telegram : %s", e)
            time.sleep(1)

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
        bot.update_cache()
        return jsonify(status="done"), 200
    except Exception as e:
        return jsonify(status="error", error=str(e)), 500

def scheduler_loop():
    try:
        bot.connect()
        bot.update_cache() # Mise à jour initiale au démarrage
    except Exception as e:
        log.warning("Connexion initiale échouée : %s", e)

    scheduler = BackgroundScheduler(timezone="Europe/Paris")

    # --- CONFIGURATION DES HORAIRES (9h00, 12h00, 14h30, 19h30) ---
    heures = [(9, 0), (12, 0), (14, 30), (19, 30)]
    for h, m in heures:
        # Une seule tâche par horaire : elle se connecte, télécharge tout et met à jour le cache.
        scheduler.add_job(bot.update_cache, CronTrigger(hour=h, minute=m))

    scheduler.start()
    log.info("Planificateur démarré (Mise à jour du cache à 9h, 12h, 14h30 et 19h30).")

    threading.Thread(target=bot._poll_telegram, daemon=True).start()

if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
