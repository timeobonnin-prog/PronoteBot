"""
PronoteBot — Version robuste avec Telegram.
- Correctif ENT ac_orleans_tours (&service= retiré)
- Backoff exponentiel pour éviter le bannissement d'IP
- Reconnexion propre et gestion d'erreurs détaillée
- Serveur Flask pour le health check Render
"""

import os
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode

import pronotepy
import pronotepy.ent
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
# Variables d'environnement
# ---------------------------------------------------------------------------
PRONOTE_URL      = os.environ["PRONOTE_URL"].strip()
PRONOTE_USERNAME = os.environ["PRONOTE_USERNAME"].strip()
PRONOTE_PASSWORD = os.environ["PRONOTE_PASSWORD"]
PRONOTE_CAS      = os.environ.get("PRONOTE_CAS", "").strip()

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE      = Path(os.environ.get("SEEN_FILE", "seen_grades.json"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_MIN", "30"))

# Anti-ban : délai minimum entre deux tentatives de connexion
MIN_RETRY_DELAY   = int(os.environ.get("MIN_RETRY_DELAY_SEC", "600"))  # 10 min
MAX_BACKOFF       = int(os.environ.get("MAX_BACKOFF_SEC", "3600"))     # 1 h


# ---------------------------------------------------------------------------
# Correctif ENT pour ac_orleans_tours
# ---------------------------------------------------------------------------
# La fonction intégrée de pronotepy envoie un paramètre &service=... qui est
# désormais refusé par le portail CAS de l'académie Orléans-Tours.
# On reconstruit l'URL CAS sans ce paramètre.

def _clean_cas_url(url: str) -> str:
    """Retire le paramètre `service` de l'URL CAS s'il est présent."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params.pop("service", None)
    clean_query = urlencode(params, doseq=True)
    return parsed._replace(query=clean_query).geturl()


def ent_orleans_tours_fixed(username: str, password: str, **kwargs):
    """Version corrigée de pronotepy.ent.ac_orleans_tours."""
    base_url = (
        "https://ent.netocentre.fr/cas/login"
        "?token=ce8ae867a0accc0b7577fcc340bb99f4"
        "&idpId=parentEleveEN-IdP"
    )
    clean_url = _clean_cas_url(base_url)
    log.debug("ENT ac_orleans_tours — URL CAS nettoyée : %s", clean_url)
    return pronotepy.ent.ac_orleans_tours(
        username, password, url=clean_url, **kwargs
    )


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class PronoteBot:
    def __init__(self):
        self.client            = None
        self.last_attempt      = None   # datetime de la dernière tentative
        self.consecutive_fails = 0      # compteur d'échecs
        self.lock              = threading.Lock()

    # --- Résolution de la fonction ENT -------------------------------------
    def _resolve_ent(self):
        if not PRONOTE_CAS:
            return None
        # Correctif spécifique
        if PRONOTE_CAS == "ac_orleans_tours":
            log.info("Utilisation du correctif ENT ac_orleans_tours")
            return ent_orleans_tours_fixed
        # Sinon, résolution standard
        if not hasattr(pronotepy.ent, PRONOTE_CAS):
            raise ValueError(
                f"ENT inconnu : '{PRONOTE_CAS}'. "
                f"Vérifie la doc pronotepy (ex: ile_de_france, "
                f"monbureaunumerique, occitanie_montpellier, ac_reunion…)."
            )
        return getattr(pronotepy.ent, PRONOTE_CAS)

    # --- Connexion avec anti-ban -------------------------------------------
    def connect(self):
        with self.lock:
            # Anti-ban : si une tentative a échoué récemment, on attend
            if self.last_attempt is not None and self.consecutive_fails > 0:
                delay = min(
                    MIN_RETRY_DELAY * (2 ** (self.consecutive_fails - 1)),
                    MAX_BACKOFF,
                )
                elapsed = (datetime.now() - self.last_attempt).total_seconds()
                if elapsed < delay:
                    remaining = int(delay - elapsed)
                    log.warning(
                        "Anti-ban actif : nouvelle tentative dans %ds "
                        "(échecs consécutifs : %d)",
                        remaining,
                        self.consecutive_fails,
                    )
                    raise RuntimeError(
                        f"Anti-ban actif ({remaining}s restantes)"
                    )

            self.last_attempt = datetime.now()
            ent_func = self._resolve_ent()

            log.info(
                "Tentative de connexion à Pronote (ent=%s, échecs=%d)…",
                PRONOTE_CAS or "aucun",
                self.consecutive_fails,
            )

            try:
                self.client = pronotepy.Client(
                    PRONOTE_URL,
                    username=PRONOTE_USERNAME,
                    password=PRONOTE_PASSWORD,
                    ent=ent_func,
                )
                if not self.client.logged_in:
                    raise RuntimeError("Pronote a refusé les identifiants")
            except pronotepy.exceptions.ENTLoginError as e:
                self.consecutive_fails += 1
                log.error(
                    "ENTLoginError (échec %d) : %s. "
                    "Vérifie que l'IP n'est pas bannie.",
                    self.consecutive_fails, e,
                )
                raise
            except Exception as e:
                self.consecutive_fails += 1
                log.exception("Erreur de connexion (échec %d)", self.consecutive_fails)
                raise

            # Succès
            self.consecutive_fails = 0
            log.info("Connecté à Pronote (ent=%s)", PRONOTE_CAS or "aucun")

    # --- Persistance des notes vues ----------------------------------------
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
            else:
                log.debug("Message Telegram envoyé (%d caractères)", len(text))
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

    # --- Vérification des notes --------------------------------------------
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
# Serveur HTTP (Render health check)
# ---------------------------------------------------------------------------
app = Flask(__name__)
bot = PronoteBot()


@app.route("/")
def health():
    return jsonify(
        status="ok",
        service="PronoteBot",
        connected=bot.client is not None and bot.client.logged_in,
        consecutive_fails=bot.consecutive_fails,
    ), 200


@app.route("/check")
def manual_check():
    try:
        bot.check_grades()
        return jsonify(status="done"), 200
    except Exception as e:
        log.exception("check_grades a échoué")
        return jsonify(status="error", error=str(e)), 500


# ---------------------------------------------------------------------------
# Boucle du planificateur
# ---------------------------------------------------------------------------
def scheduler_loop():
    # Petit délai pour laisser Flask démarrer
    time.sleep(5)

    # Tentative initiale, sans faire planter le service si elle échoue
    try:
        bot.connect()
    except Exception as e:
        log.warning(
            "Connexion initiale échouée : %s. "
            "Le job réessaiera toutes les %d min.",
            e, CHECK_INTERVAL,
        )

    scheduler = BackgroundScheduler(timezone="Europe/Paris")
    scheduler.add_job(
        bot.check_grades,
        "interval",
        minutes=CHECK_INTERVAL,
        next_run_time=datetime.now() + timedelta(seconds=30),
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("Planificateur démarré (toutes les %d min)", CHECK_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
