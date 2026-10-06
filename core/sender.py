"""Envoi des emails via l'API Brevo.

RÈGLE ABSOLUE : rien ne part sans approbation humaine. `envoyer_un` est le SEUL chemin d'envoi du
code ; il revérifie l'approbation dans la base juste avant l'appel à Brevo et refuse sinon. Aucun
paramètre, aucune option, aucun mode test ne peut contourner cette garde.

Chaque envoi enchaîne : garde d'approbation, puis trois vérifications (liste noire, pas déjà envoyé,
plafond du jour), puis une ligne « envoi en cours » écrite AVANT l'appel réseau : si l'application
s'arrête en plein appel, l'envoi est marqué incertain et jamais rejoué automatiquement.

Utilisable seul : python -m core.sender          teste la clé Brevo (aucun email envoyé)
                  python -m core.sender check    tests sur base jetable, réseau simulé
"""
import json
import logging
import random
import sys
import tempfile
import threading
import time
from datetime import date, datetime, time as heure, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from core import courriel, db, http, reglages

journal = logging.getLogger("prospect")

URL_COMPTE = "https://api.brevo.com/v3/account"
URL_EXPEDITEURS = "https://api.brevo.com/v3/senders"
URL_ENVOI = "https://api.brevo.com/v3/smtp/email"
INCERTAIN = "INCERTAIN"  # préfixe de `envois.erreur` : l'email est peut-être parti, à vérifier dans Brevo
_arret = threading.Event()


class EnvoiRefuse(Exception):
    """Un envoi a été refusé AVANT tout appel réseau. `motif` : code court ; le message est lisible."""

    def __init__(self, motif, message):
        super().__init__(message)
        self.motif = motif


# --------------------------------------------------------------------------------------
# Quota du jour
# --------------------------------------------------------------------------------------


def bornes_du_jour_utc(cfg, maintenant=None):
    """Début et fin (en UTC, format base) du jour calendaire courant dans le fuseau métier."""
    fuseau = ZoneInfo(cfg["fuseau"])
    local = (maintenant or datetime.now(timezone.utc)).astimezone(fuseau)
    debut = datetime.combine(local.date(), heure.min, tzinfo=fuseau)
    fin = datetime.combine(local.date() + timedelta(days=1), heure.min, tzinfo=fuseau)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return debut.astimezone(timezone.utc).strftime(fmt), fin.astimezone(timezone.utc).strftime(fmt)


def quota(conn, cfg, maintenant=None):
    """{'plafond', 'envoyes', 'reste'} pour aujourd'hui. Calculé depuis `envois` : il survit aux redémarrages.

    Les envois incertains comptent (par prudence : ils sont peut-être partis).
    """
    debut, fin = bornes_du_jour_utc(cfg, maintenant)
    envoyes = conn.execute(
        "SELECT COUNT(*) FROM envois WHERE envoye_le >= ? AND envoye_le < ? "
        "AND (statut <> 'echec' OR erreur LIKE ?)", (debut, fin, INCERTAIN + "%")).fetchone()[0]
    plafond = int(cfg["plafond_jour"])
    return {"plafond": plafond, "envoyes": envoyes, "reste": max(0, plafond - envoyes)}


# --------------------------------------------------------------------------------------
# Éligibilité
# --------------------------------------------------------------------------------------

_DEJA_PARTI = ("EXISTS (SELECT 1 FROM envois e WHERE e.message_id = m.id "
               "AND (e.statut <> 'echec' OR e.erreur LIKE 'INCERTAIN%'))")


def en_attente_d_envoi(conn):
    """Messages approuvés prêts à partir (jamais envoyés, sans envoi incertain), plus gros scores d'abord."""
    return [dict(l) for l in conn.execute(
        f"""SELECT m.id AS message_id, p.id AS prospect_id, p.nom, p.email, p.score, p.campagne_id
              FROM messages m JOIN prospects p ON p.id = m.prospect_id
             WHERE p.statut = 'approuve' AND m.approuve_le IS NOT NULL AND m.approuve_par_humain = 1
               AND NOT {_DEJA_PARTI}
               AND NOT EXISTS (SELECT 1 FROM envois e WHERE e.message_id = m.id AND e.erreur LIKE 'Refusé avant envoi%')
             ORDER BY p.score DESC, p.id""")]


def configuration_manquante(cfg):
    manquants = []
    for cle, libelle in (("brevo_key", "la clé Brevo"), ("expediteur_nom", "le nom d'expéditeur"),
                         ("expediteur_email", "l'adresse d'expédition"),
                         ("mention_desinscription", "la mention de désinscription")):
        if not (cfg.get(cle) or "").strip():
            manquants.append(libelle)
    return manquants


# --------------------------------------------------------------------------------------
# L'envoi : un seul chemin
# --------------------------------------------------------------------------------------


def destinataire_autorise(cfg, email):
    """MODE ESSAI. Liste vide : aucune restriction. Sinon, seules les adresses (ou domaines) listées peuvent recevoir un email."""
    autorisees = json.loads(cfg.get("envoi_adresses_autorisees") or "[]")
    if not autorisees:
        return True
    adresse = db.normaliser_email(email) or ""
    return adresse in autorisees or adresse.rsplit("@", 1)[-1] in autorisees


def _garde_approbation(conn, message_id):
    """LA GARDE. Relit l'état dans la base ; lève EnvoiRefuse si le message n'est pas approuvé par un humain
    ou si son prospect n'est pas au statut « approuve »."""
    ligne = conn.execute(
        "SELECT m.*, p.statut AS statut_prospect FROM messages m JOIN prospects p ON p.id = m.prospect_id "
        "WHERE m.id = ?", (message_id,)).fetchone()
    if ligne is None:
        raise EnvoiRefuse("non_approuve", "Message introuvable : rien n'est envoyé.")
    if ligne["statut_prospect"] != "approuve" or not ligne["approuve_le"] or ligne["approuve_par_humain"] != 1:
        raise EnvoiRefuse("non_approuve",
                          "Ce message n'est pas approuvé : seul un message approuvé à la main peut partir.")
    return dict(ligne)


def _verifier_avant_envoi(conn, cfg, message, prospect):
    """Les vérifications, dans l'ordre : mode essai, liste noire, pas déjà envoyé, plafond du jour."""
    if not destinataire_autorise(cfg, prospect["email"]):
        raise EnvoiRefuse("mode_essai", "Mode essai actif : ce destinataire n'est pas dans la liste des adresses autorisées "
                                        "(Réglages > Limites). Rien n'est envoyé.")
    if db.est_en_liste_noire(conn, prospect["email"]):
        raise EnvoiRefuse("liste_noire", "Adresse ou domaine en liste noire.")
    deja = conn.execute(
        f"SELECT 1 FROM messages m WHERE m.id = ? AND {_DEJA_PARTI}", (message["id"],)).fetchone()
    if deja:
        raise EnvoiRefuse("deja_envoye", "Ce message a déjà été envoyé (ou son envoi est incertain).")
    total = conn.execute(
        "SELECT COUNT(*) FROM envois e JOIN messages m ON m.id = e.message_id "
        "WHERE m.prospect_id = ? AND (e.statut <> 'echec' OR e.erreur LIKE 'INCERTAIN%')", (prospect["id"],)).fetchone()[0]
    if total > int(cfg["max_relances"]):  # le premier message + max_relances relances, jamais davantage
        raise EnvoiRefuse("trop_de_relances", f"Ce prospect a déjà reçu {total} message(s) : limite atteinte.")
    autre = conn.execute(
        "SELECT p2.nom FROM prospects p2 JOIN messages m2 ON m2.prospect_id = p2.id JOIN envois e ON e.message_id = m2.id "
        "WHERE p2.campagne_id = ? AND p2.email = ? AND p2.id <> ? AND e.statut <> 'echec' LIMIT 1",
        (prospect["campagne_id"], prospect["email"], prospect["id"])).fetchone()
    if autre:
        raise EnvoiRefuse("deja_envoye", f"Cette adresse a déjà reçu un email de cette campagne (via « {autre['nom']} »).")
    if quota(conn, cfg)["reste"] <= 0:
        raise EnvoiRefuse("plafond", f"Plafond du jour atteint ({cfg['plafond_jour']} envois).")


def site_a_afficher(cfg, est_relance):
    """Adresse de ton site à placer sous la signature d'un message, ou '' selon le réglage « site_dans_signature »."""
    site = (cfg.get("mon_site") or "").strip()
    mode = cfg.get("site_dans_signature") or "relances"
    if site and (mode == "toujours" or (mode == "relances" and est_relance)):
        return site
    return ""


def texte_de_l_email(cfg, corps, est_relance=False):
    """Version texte de l'email tel qu'il part : corps approuvé, ton site si le réglage le demande, mention de désinscription."""
    site = site_a_afficher(cfg, est_relance)
    return corps.rstrip() + (("\n" + site) if site else "") + "\n\n" + cfg["mention_desinscription"].strip()


def apercu(cfg, corps, est_relance=False):
    """Ce que le destinataire recevra, à partir du texte affiché : {'format', 'html', 'texte'}. Aucun envoi, aucune écriture."""
    texte = texte_de_l_email(cfg, corps, est_relance)
    if cfg.get("email_format") == "html" and cfg.get("modele_email_html"):
        html = courriel.rendre_html(cfg, corps, site_a_afficher(cfg, est_relance), cfg["mention_desinscription"])
        return {"format": "html", "html": html, "texte": texte}
    return {"format": "texte", "html": None, "texte": texte}


def _construire_envoi(cfg, message, prospect, est_relance=False):
    """Corps de la requête Brevo. La mention de désinscription et l'identité sont imposées ici."""
    expediteur = cfg["expediteur_email"].strip()
    reponse = (cfg.get("reponse_email") or "").strip() or expediteur
    site = site_a_afficher(cfg, est_relance)
    charge = {
        "sender": {"name": cfg["expediteur_nom"].strip(), "email": expediteur},
        "to": [{"email": prospect["email"], "name": prospect["nom"]}],
        "replyTo": {"email": reponse},
        "subject": message["objet"],
        "textContent": texte_de_l_email(cfg, message["corps"], est_relance),
        "headers": {"List-Unsubscribe": f"<mailto:{reponse}?subject=STOP>"},
        "tags": ["prospection", f"campagne-{prospect['campagne_id']}"],
    }
    if cfg.get("email_format") == "html" and cfg.get("modele_email_html"):
        # Même texte que ci-dessus, simplement mis en page (voir core/courriel.py) ; la version texte reste jointe.
        charge["htmlContent"] = courriel.rendre_html(cfg, message["corps"], site, cfg["mention_desinscription"])
    return charge


def _poster_brevo(cfg, charge):
    """L'appel réseau d'envoi. NE JAMAIS l'appeler ailleurs que depuis `envoyer_un`, après la garde."""
    return http.requete("POST", URL_ENVOI, timeout=int(cfg["http_timeout_s"]),
                        headers={"api-key": cfg["brevo_key"].strip(), "accept": "application/json",
                                 "content-type": "application/json"}, json=charge)


def _incertain(erreur):
    """Une exception réseau laisse-t-elle un doute sur l'arrivée de la requête chez Brevo ?"""
    if isinstance(erreur, requests.exceptions.ConnectTimeout):
        return False
    texte = str(erreur)
    return not ("Failed to establish a new connection" in texte or "NameResolutionError" in texte
                or "getaddrinfo failed" in texte)


def envoyer_un(message_id):
    """Envoie UN message approuvé. Renvoie {'statut': 'envoye'|'echec'|'incertain', ...}.

    Lève EnvoiRefuse si l'envoi est refusé avant tout appel réseau. Ne prend AUCUN paramètre de
    contournement : l'approbation est relue dans la base à chaque appel.
    """
    with db.connexion() as conn:
        cfg = reglages.valeurs_effectives(conn)
        manquants = configuration_manquante(cfg)
        if manquants:
            raise EnvoiRefuse("configuration", "Réglages incomplets : il manque " + ", ".join(manquants) + ".")
        message = _garde_approbation(conn, message_id)  # <- la garde, avant toute autre chose
        prospect = dict(conn.execute("SELECT * FROM prospects WHERE id = ?", (message["prospect_id"],)).fetchone())
        if not prospect["email"]:
            raise EnvoiRefuse("sans_email", "Ce prospect n'a pas d'email.")
        refus_a_signaler = None
        try:
            _verifier_avant_envoi(conn, cfg, message, prospect)
        except EnvoiRefuse as refus:
            refus_a_signaler = refus
            if refus.motif in ("liste_noire", "trop_de_relances", "deja_envoye"):  # la raison est consignée
                conn.execute("INSERT INTO envois (message_id, statut, erreur) VALUES (?, 'echec', ?)",
                             (message_id, f"Refusé avant envoi : {refus}"))
                journal.warning("envoi refusé : message %s vers %s : %s", message_id, prospect["email"], refus)
        if refus_a_signaler is None:
            # Trace écrite AVANT l'appel : si tout s'arrête ici, l'envoi reste « incertain » et n'est jamais rejoué.
            envoi_id = conn.execute(
                "INSERT INTO envois (message_id, statut, erreur) VALUES (?, 'echec', ?)",
                (message_id, f"{INCERTAIN} : envoi en cours")).lastrowid
            deja = conn.execute(
                "SELECT COUNT(*) FROM envois e JOIN messages m ON m.id = e.message_id "
                "WHERE m.prospect_id = ? AND e.statut IN ('envoye', 'ouvert', 'repondu')", (prospect["id"],)).fetchone()[0]
            charge = _construire_envoi(cfg, message, prospect, est_relance=deja > 0)
    if refus_a_signaler is not None:  # levé après le commit : la raison consignée est bien enregistrée
        raise refus_a_signaler
    # ---- l'appel réseau est hors transaction ----
    with db.connexion() as conn:
        _garde_approbation(conn, message_id)  # revérifiée une dernière fois, juste avant Brevo
    resultat = {"envoi_id": envoi_id, "message_id": message_id, "destinataire": prospect["email"]}
    try:
        reponse = _poster_brevo(cfg, charge)
    except requests.RequestException as erreur:
        texte = http.message_erreur_reseau(erreur, int(cfg["http_timeout_s"]))
        if _incertain(erreur):
            texte = f"{INCERTAIN} : {texte} L'email est peut-être parti : vérifie dans Brevo avant de réessayer."
        with db.connexion() as conn:
            conn.execute("UPDATE envois SET erreur = ? WHERE id = ?", (texte, envoi_id))
            if not _incertain(erreur):
                conn.execute("UPDATE envois SET erreur = ? WHERE id = ?", (f"Non envoyé : {texte}", envoi_id))
        journal.error("envoi %s vers %s : %s", envoi_id, prospect["email"], texte)
        return {**resultat, "statut": "incertain" if _incertain(erreur) else "echec", "message": texte}
    if reponse.status_code in (200, 201):
        try:
            brevo_id = str(reponse.json().get("messageId") or "")
        except ValueError:
            brevo_id = ""
        with db.connexion() as conn:
            conn.execute("UPDATE envois SET statut = 'envoye', brevo_id = ?, erreur = NULL, "
                         "envoye_le = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?", (brevo_id or None, envoi_id))
            conn.execute("UPDATE prospects SET statut = 'envoye' WHERE id = ?", (prospect["id"],))
        journal.info("envoyé : envoi %s, message %s, vers %s, brevo %s", envoi_id, message_id, prospect["email"], brevo_id)
        return {**resultat, "statut": "envoye", "message": "envoyé", "brevo_id": brevo_id}
    texte = f"Brevo refuse l'envoi (HTTP {reponse.status_code}) : {http.resume_erreur(reponse)}"
    with db.connexion() as conn:
        conn.execute("UPDATE envois SET erreur = ? WHERE id = ?", (f"Non envoyé : {texte}", envoi_id))
    journal.error("envoi %s vers %s : %s", envoi_id, prospect["email"], texte)
    return {**resultat, "statut": "echec", "message": texte}


# --------------------------------------------------------------------------------------
# Lot (tâche de fond) et arrêt d'urgence
# --------------------------------------------------------------------------------------


def demander_arret():
    """Arrêt d'urgence : le lot en cours s'arrête avant le prochain email (même pendant une pause)."""
    _arret.set()


def envoyer_lot(job_id):
    """Envoie les messages approuvés, dans la limite du quota, un à un, avec une pause aléatoire entre deux envois."""
    from core import jobs

    _arret.clear()
    with db.connexion() as conn:
        cfg = reglages.valeurs_effectives(conn)
        manquants = configuration_manquante(cfg)
        file = en_attente_d_envoi(conn)
        bloques = [f for f in file if db.est_en_liste_noire(conn, f["email"])]
        hors_essai = [f for f in file if not destinataire_autorise(cfg, f["email"])]
        reste = quota(conn, cfg)["reste"]
    if manquants:
        return {"statut": "erreur", "detail": "Réglages incomplets : il manque " + ", ".join(manquants) + "."}
    file = [f for f in file if f not in bloques and f not in hors_essai]
    if not file:
        return {"statut": "termine", "detail": "Aucun message approuvé à envoyer."
                + (f" {len(bloques)} en liste noire (non envoyés)." if bloques else "")
                + (f" {len(hors_essai)} hors liste du mode essai (non envoyés)." if hors_essai else "")}

    compte = {"envoye": 0, "echec": 0, "incertain": 0, "refuse": 0}
    problemes, raison_arret = [], None
    mini, maxi = int(cfg["pause_envoi_min_s"]), int(cfg["pause_envoi_max_s"])
    nb = len(file)
    for i, item in enumerate(file):
        if _arret.is_set():
            raison_arret = "arrêt demandé"
            break
        if reste <= 0:
            raison_arret = f"plafond du jour atteint ({cfg['plafond_jour']})"
            break
        jobs.maj(job_id, 100 * i / nb, f"Envoi {i + 1}/{nb} : {item['nom']}")
        try:
            resultat = envoyer_un(item["message_id"])
        except EnvoiRefuse as refus:
            if refus.motif == "plafond":
                raison_arret = "plafond du jour atteint"
                break
            compte["refuse"] += 1
            problemes.append(f"{item['nom']} ({refus})")
            continue
        compte[resultat["statut"]] += 1
        if resultat["statut"] != "envoye":
            problemes.append(f"{item['nom']} ({resultat['message']})")
            continue
        reste -= 1
        if i + 1 < nb and reste > 0:
            pause = random.randint(mini, max(mini, maxi))
            for restant in range(pause, 0, -1):  # la pause se coupe dès qu'un arrêt est demandé
                if _arret.is_set():
                    break
                if restant % 5 == 0 or restant == pause:
                    jobs.maj(job_id, None, f"Envoi {i + 1}/{nb} fait — pause de {restant} s avant le suivant")
                time.sleep(1)
    non_traites = nb - sum(compte.values())
    morceaux = [f"{compte['envoye']} envoyé(s)"]
    for cle, libelle in (("echec", "échec(s)"), ("incertain", "incertain(s) à vérifier dans Brevo"), ("refuse", "refusé(s)")):
        if compte[cle]:
            morceaux.append(f"{compte[cle]} {libelle}")
    if non_traites:
        morceaux.append(f"{non_traites} non envoyé(s) : {raison_arret or 'non traités'}")
    if bloques:
        morceaux.append(f"{len(bloques)} en liste noire")
    if hors_essai:
        morceaux.append(f"{len(hors_essai)} hors liste du mode essai (non envoyés)")
    if problemes:
        morceaux.append("détail : " + " ; ".join(problemes[:5]) + (f" (+{len(problemes) - 5})" if len(problemes) > 5 else ""))
    tout_rate = compte["envoye"] == 0 and (compte["echec"] or compte["incertain"] or compte["refuse"])
    return {"statut": "erreur" if tout_rate else "termine", "detail": " — ".join(morceaux)}


def resoudre_incertain(conn, envoi_id, envoye):
    """Après vérification dans Brevo : l'email est-il parti ? Si oui l'envoi devient « envoyé » ; sinon il redevient réessayable."""
    e = conn.execute("SELECT e.*, m.prospect_id FROM envois e JOIN messages m ON m.id = e.message_id WHERE e.id = ?",
                     (envoi_id,)).fetchone()
    if e is None or not (e["erreur"] or "").startswith(INCERTAIN):
        raise ValueError("Cet envoi n'est pas marqué incertain.")
    if envoye:
        conn.execute("UPDATE envois SET statut = 'envoye', erreur = NULL WHERE id = ?", (envoi_id,))
        conn.execute("UPDATE prospects SET statut = 'envoye' WHERE id = ? AND statut = 'approuve'", (e["prospect_id"],))
    else:
        conn.execute("UPDATE envois SET erreur = 'Non envoyé (vérifié à la main dans Brevo)' WHERE id = ?", (envoi_id,))
    journal.info("envoi incertain %s résolu à la main : %s", envoi_id, "envoyé" if envoye else "non envoyé")


# --------------------------------------------------------------------------------------
# Test de connexion (aucun email envoyé)
# --------------------------------------------------------------------------------------


def tester_connexion(cfg):
    """Lit le compte Brevo (aucun envoi) et vérifie que l'expéditeur y est déclaré."""
    cle = (cfg.get("brevo_key") or "").strip()
    if not cle:
        return {"statut": "non_configure", "message": "Aucune clé Brevo : l'envoi est impossible."}
    timeout = int(cfg["http_timeout_s"])
    en_tetes = {"api-key": cle, "accept": "application/json"}
    try:
        compte = http.requete("GET", URL_COMPTE, timeout=timeout, headers=en_tetes)
    except requests.RequestException as erreur:
        return {"statut": "erreur", "message": http.message_erreur_reseau(erreur, timeout)}
    if not compte.ok:
        return {"statut": "erreur",
                "message": f"Brevo refuse la clé (HTTP {compte.status_code}) : {http.resume_erreur(compte)}"}
    try:
        email_compte = compte.json().get("email", "?")
    except ValueError:
        email_compte = "?"
    message = f"Clé valide, compte {email_compte}."

    expediteur = db.normaliser_email(cfg.get("expediteur_email"))
    if not expediteur:
        return {"statut": "attention",
                "message": message + " Aucune adresse d'expédition n'est renseignée dans les réglages."}
    try:
        liste = http.requete("GET", URL_EXPEDITEURS, timeout=timeout, headers=en_tetes)
    except requests.RequestException as erreur:
        return {"statut": "attention",
                "message": message + f" Expéditeur non vérifié : {http.message_erreur_reseau(erreur, timeout)}"}
    if not liste.ok:
        return {"statut": "attention",
                "message": message + f" Expéditeur non vérifié (HTTP {liste.status_code}) : "
                                     f"{http.resume_erreur(liste)}"}
    try:
        declares = {db.normaliser_email(e.get("email")): e for e in liste.json().get("senders", [])}
    except (ValueError, AttributeError):
        return {"statut": "attention", "message": message + " Liste des expéditeurs illisible."}
    trouve = declares.get(expediteur)
    if trouve is None:
        return {"statut": "attention",
                "message": message + f" Attention : {expediteur} n'est pas un expéditeur déclaré chez Brevo."}
    if not trouve.get("active", False):
        return {"statut": "attention",
                "message": message + f" Attention : l'expéditeur {expediteur} existe mais n'est pas actif."}
    return {"statut": "ok", "message": message + f" Expéditeur {expediteur} déclaré et actif."}


# --------------------------------------------------------------------------------------
# Auto-tests (python -m core.sender check) : Brevo simulé, base jetable
# --------------------------------------------------------------------------------------


class _Reponse:
    def __init__(self, statut=201, brut=None):
        self.status_code, self.ok = statut, statut < 400
        self._json = brut if brut is not None else {"messageId": "<abc@smtp-relay.mailin.fr>"}
        self.text = json.dumps(self._json)

    def json(self):
        return self._json


def _autotests():
    import config
    from core import campagnes, jobs, messages

    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    envoyes, comportement = [], {"f": lambda charge: _Reponse()}

    def faux_http(methode, url, *, timeout, **kw):
        if url == URL_ENVOI:
            envoyes.append({"url": url, "en_tetes": kw["headers"], "charge": kw["json"]})
            return comportement["f"](kw["json"])
        raise AssertionError("appel réseau inattendu : " + url)

    http_reel, sleep_reel, chemin_reel = http.requete, time.sleep, config.DB_PATH
    pauses = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        config.DB_PATH = Path(tmp) / "t.db"
        http.requete = faux_http
        time.sleep = lambda s: pauses.append(s)
        random.seed(1)
        try:
            db.init_db(importer_env=False)
            with db.connexion() as c:
                for cle, val in (("brevo_key", "cle-brevo"), ("expediteur_nom", "Gill"), ("expediteur_email", "gill@exemple.fr"),
                                 ("reponse_email", "reponse@exemple.fr"), ("plafond_jour", "3"),
                                 ("pause_envoi_min_s", "30"), ("pause_envoi_max_s", "60")):
                    db.set_reglage(c, cle, val)
                cid = campagnes.creer(c, {"nom": "C", "secteur": "s", "pays": "FR", "langue": "fr"})
                ids, msgs = {}, {}
                for nom, email, score in (("A", "a@a.fr", 9), ("B", "b@b.fr", 8), ("C", "c@c.fr", 7), ("D", "d@d.fr", 6),
                                          ("E", "e@bloque.fr", 5), ("F", "a@a.fr", 4)):
                    db.inserer_prospect(c, {"campagne_id": cid, "nom": nom, "email": email, "ville": nom, "source": "openstreetmap",
                                            "source_id": "osm:" + nom})
                    ids[nom] = c.execute("SELECT id FROM prospects WHERE nom = ?", (nom,)).fetchone()[0]
                    c.execute("UPDATE prospects SET problemes = '[\"lent\"]', score = ?, statut = 'qualifie' WHERE id = ?", (score, ids[nom]))
                    msgs[nom] = c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, ?, ?, 1)",
                                          (ids[nom], f"Objet {nom}", f"Bonjour {nom}\n\nGill")).lastrowid
                    c.execute("UPDATE prospects SET statut = 'message_pret' WHERE id = ?", (ids[nom],))

            def refus(message_id):
                try:
                    envoyer_un(message_id)
                except EnvoiRefuse as r:
                    return r.motif
                return None

            def approuver(nom, **kw):
                with db.connexion() as c:
                    messages.approuver(c, msgs[nom], f"Objet {nom}", f"Bonjour {nom}\n\nGill")

            # --- LA GARDE ---
            verifier("garde : message jamais approuvé -> refus « non_approuve », aucun appel Brevo, aucune ligne envois",
                     refus(msgs["A"]) == "non_approuve" and not envoyes
                     and not conn_count("SELECT COUNT(*) FROM envois"))
            with db.connexion() as c:   # le prospect passe « approuve » sans que le message le soit : impossible (trigger)
                try:
                    c.execute("UPDATE prospects SET statut = 'approuve' WHERE id = ?", (ids["A"],))
                    forge = False
                except Exception:
                    forge = True
            verifier("garde : forcer le statut « approuve » sans approbation de message est refusé par la base", forge)
            approuver("A")
            with db.connexion() as c:   # le message est approuvé, puis le prospect est repassé à message_pret
                c.execute("UPDATE prospects SET statut = 'message_pret' WHERE id = ?", (ids["A"],))
            verifier("garde : prospect pas « approuve » (message approuvé mais prospect repassé à valider) -> refusé",
                     refus(msgs["A"]) == "non_approuve" and not envoyes)
            with db.connexion() as c:
                c.execute("UPDATE messages SET approuve_le = NULL, approuve_par_humain = 0 WHERE id = ?", (msgs["A"],))
            approuver("A")
            import inspect
            verifier("garde : envoyer_un n'accepte que message_id : aucune option, aucun mode test, aucun contournement",
                     list(inspect.signature(envoyer_un).parameters) == ["message_id"])
            code_source = Path(__file__).read_text(encoding="utf-8")
            verifier("garde : un seul appel d'envoi Brevo dans tout le module (pas de chemin parallèle)",
                     code_source.count("= _poster_brevo" + "(cfg, charge)") == 1 and code_source.count("URL_ENVOI" + ",") == 1)

            # --- Envoi réussi ---
            r = envoyer_un(msgs["A"])
            charge, en_tetes = envoyes[-1]["charge"], envoyes[-1]["en_tetes"]
            with db.connexion() as c:
                e = dict(c.execute("SELECT * FROM envois WHERE message_id = ?", (msgs["A"],)).fetchone())
                statut_a = c.execute("SELECT statut FROM prospects WHERE id = ?", (ids["A"],)).fetchone()[0]
            verifier("envoi réussi : ligne envois « envoye » avec l'identifiant Brevo, prospect « envoye »",
                     r["statut"] == "envoye" and e["statut"] == "envoye" and e["brevo_id"] == "<abc@smtp-relay.mailin.fr>"
                     and e["erreur"] is None and statut_a == "envoye")
            verifier("requête Brevo : expéditeur, destinataire, objet, adresse de réponse, en-tête de désinscription, clé API",
                     charge["sender"] == {"name": "Gill", "email": "gill@exemple.fr"} and charge["to"][0]["email"] == "a@a.fr"
                     and charge["subject"] == "Objet A" and charge["replyTo"]["email"] == "reponse@exemple.fr"
                     and "mailto:reponse@exemple.fr" in charge["headers"]["List-Unsubscribe"] and en_tetes["api-key"] == "cle-brevo")
            verifier("mention de désinscription imposée à la fin de chaque email, texte approuvé conservé",
                     charge["textContent"].startswith("Bonjour A\n\nGill") and charge["textContent"].rstrip().endswith("répondez STOP à ce message."))
            verifier("mise en forme : le HTML joint contient exactement les mêmes mots que la version texte approuvée",
                     "htmlContent" in charge and courriel.texte_du_html(charge["htmlContent"]) == " ".join(charge["textContent"].split()))
            nb_avant = len(envoyes)
            verifier("après envoi, le prospect n'est plus « approuve » : impossible de renvoyer", refus(msgs["A"]) == "non_approuve" and len(envoyes) == nb_avant)

            # --- MODE ESSAI : une restriction de destinataires, jamais un contournement ---
            cfg_essai = {"envoi_adresses_autorisees": '["moi+t1@gmail.com", "maboite.fr"]'}
            verifier("mode essai : liste vide = aucune restriction ; adresses exactes et domaines listés autorisés (casse ignorée), le reste refusé",
                     destinataire_autorise({}, "n'importe@qui.fr") and destinataire_autorise(cfg_essai, "Moi+T1@Gmail.com")
                     and destinataire_autorise(cfg_essai, "x@maboite.fr") and not destinataire_autorise(cfg_essai, "dentiste@gmail.com")
                     and not destinataire_autorise(cfg_essai, "moi+t2@gmail.com") and not destinataire_autorise(cfg_essai, None))
            with db.connexion() as c:
                db.set_reglage(c, "envoi_adresses_autorisees", '["a@a.fr"]')
            avant_essai = len(envoyes)
            approuver("D")
            verifier("mode essai : un message APPROUVÉ vers une adresse hors liste est refusé AVANT tout appel réseau (motif « mode_essai »)",
                     refus(msgs["D"]) == "mode_essai" and len(envoyes) == avant_essai)
            with db.connexion() as c:
                db.set_reglage(c, "envoi_adresses_autorisees", "[]")

            # --- Ton site sous la signature : jamais / relances / toujours ---
            cfg_site = {"mon_site": "https://gdjahoui.com", "site_dans_signature": "relances", "expediteur_email": "a@b.fr",
                        "expediteur_nom": "G", "mention_desinscription": "STOP"}
            corps_test = {"objet": "o", "corps": "Bonjour\n\nGill"}
            pro = {"email": "x@y.fr", "nom": "X", "campagne_id": 1}
            texte = lambda c, rel: _construire_envoi(c, corps_test, pro, rel)["textContent"]
            verifier("ton site sous la signature, mode « relances » (défaut) : absent du premier message, présent dans la relance",
                     "gdjahoui.com" not in texte(cfg_site, False) and texte(cfg_site, True) == "Bonjour\n\nGill\nhttps://gdjahoui.com\n\nSTOP")
            verifier("mode « toujours » : présent partout ; mode « jamais » ou site vide : absent partout",
                     "gdjahoui.com" in texte({**cfg_site, "site_dans_signature": "toujours"}, False)
                     and "gdjahoui.com" not in texte({**cfg_site, "site_dans_signature": "jamais"}, True)
                     and "http" not in texte({**cfg_site, "mon_site": ""}, True))

            verifier("format « texte » : aucun HTML joint ; format « html » sans modèle : texte seul, jamais d'email vide",
                     "htmlContent" not in _construire_envoi({**cfg_site, "email_format": "texte"}, corps_test, pro, False)
                     and "htmlContent" not in _construire_envoi({**cfg_site, "email_format": "html", "modele_email_html": ""}, corps_test, pro, False)
                     and "htmlContent" in _construire_envoi({**cfg_site, "email_format": "html", "modele_email_html": "<p>{contenu}</p>"}, corps_test, pro, False))

            # --- Les trois vérifications ---
            approuver("E")
            with db.connexion() as c:  # l'adresse est mise en liste noire APRÈS l'approbation : l'envoi doit quand même refuser
                db.ajouter_liste_noire(c, "bloque.fr", "manuel")
            verifier("liste noire (domaine) : refusé, raison consignée dans l'historique, aucun appel",
                     refus(msgs["E"]) == "liste_noire" and len(envoyes) == nb_avant
                     and "liste noire" in conn_val("SELECT erreur FROM envois WHERE message_id = ?", msgs["E"]))
            approuver("F")  # même adresse que A, déjà contactée dans la campagne
            verifier("même adresse déjà contactée dans la campagne (autre prospect) : refusé", refus(msgs["F"]) == "deja_envoye")

            # --- Incident réseau / refus Brevo ---
            approuver("B")
            comportement["f"] = lambda ch: _Reponse(401, {"code": "unauthorized", "message": "Key not found"})
            r = envoyer_un(msgs["B"])
            with db.connexion() as c:
                eb = dict(c.execute("SELECT * FROM envois WHERE message_id = ?", (msgs["B"],)).fetchone())
                sb = c.execute("SELECT statut FROM prospects WHERE id = ?", (ids["B"],)).fetchone()[0]
            verifier("Brevo refuse (401) : échec consigné avec le message réel, prospect reste « approuve », réessayable",
                     r["statut"] == "echec" and eb["statut"] == "echec" and "Key not found" in eb["erreur"] and sb == "approuve"
                     and not eb["erreur"].startswith(INCERTAIN) and msgs["B"] in en_attente_ids())
            comportement["f"] = lambda ch: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("lu trop lentement"))
            r = envoyer_un(msgs["B"])
            verifier("délai de lecture dépassé : envoi « incertain », jamais rejoué automatiquement, exclu du prochain lot",
                     r["statut"] == "incertain" and msgs["B"] not in en_attente_ids())
            with db.connexion() as c:
                eid = c.execute("SELECT id FROM envois WHERE message_id = ? AND erreur LIKE 'INCERTAIN%'", (msgs["B"],)).fetchone()[0]
            verifier("un incertain bloque aussi un envoi direct du même message", refus(msgs["B"]) == "deja_envoye")
            comportement["f"] = lambda ch: (_ for _ in ()).throw(requests.exceptions.ConnectionError("Failed to establish a new connection"))
            with db.connexion() as c:
                resoudre_incertain(c, eid, False)
            r = envoyer_un(msgs["B"])
            verifier("connexion impossible (rien n'est parti) : échec certain, pas « incertain »; un incertain résolu « non envoyé » redevient réessayable",
                     r["statut"] == "echec")

            # --- Quota ---
            comportement["f"] = lambda ch: _Reponse()
            envoyer_un(msgs["B"])                                     # 2e envoi du jour (A, B)
            approuver("C")
            envoyer_un(msgs["C"])                                     # 3e : plafond de 3 atteint
            approuver("D")
            q = None
            with db.connexion() as c:
                q = quota(c, reglages.valeurs_effectives(c))
            verifier("quota : 3 envoyés sur 3, reste 0 (calculé depuis la base)", q == {"plafond": 3, "envoyes": 3, "reste": 0})
            verifier("plafond atteint : le 4e envoi est refusé dans le code d'envoi lui-même", refus(msgs["D"]) == "plafond")
            with db.connexion() as c:   # les envois d'hier ne comptent pas
                c.execute("UPDATE envois SET envoye_le = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-2 days')")
                q2 = quota(c, reglages.valeurs_effectives(c))
            verifier("quota : remis à zéro à minuit (fuseau métier) - les envois d'il y a 2 jours ne comptent plus", q2["reste"] == 3)
            with db.connexion() as c:
                debut, fin = bornes_du_jour_utc({"fuseau": "Europe/Paris"}, datetime(2026, 7, 1, 22, 30, tzinfo=timezone.utc))
            verifier("minuit en fuseau Paris (heure d'été) : 22:30 UTC le 1er juillet = 00:30 le 2 => jour du 2 (22:00Z -> 22:00Z)",
                     debut == "2026-07-01T22:00:00Z" and fin == "2026-07-02T22:00:00Z")

            # --- Lot ---
            with db.connexion() as c:
                c.execute("DELETE FROM envois")
            # nouveaux prospects approuvés pour le lot
            with db.connexion() as c:
                lot = []
                for k in range(5):
                    db.inserer_prospect(c, {"campagne_id": cid, "nom": f"L{k}", "email": f"l{k}@lot.fr", "ville": f"L{k}", "source": "openstreetmap", "source_id": f"osm:L{k}"})
                    pid = c.execute("SELECT id FROM prospects WHERE nom = ?", (f"L{k}",)).fetchone()[0]
                    c.execute("UPDATE prospects SET problemes = '[\"lent\"]', score = ?, statut = 'qualifie' WHERE id = ?", (20 - k, pid))
                    mid = c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'O', 'C', 1)", (pid,)).lastrowid
                    c.execute("UPDATE prospects SET statut = 'message_pret' WHERE id = ?", (pid,))
                    messages.approuver(c, mid, "O", "C")
                    lot.append(mid)
                db.set_reglage(c, "plafond_jour", "4")
            envoyes.clear(); pauses.clear()
            comportement["f"] = lambda ch: _Reponse(400, {"message": "Sender not valid"}) if ch["to"][0]["email"] == "l1@lot.fr" else _Reponse()
            jid = jobs.soumettre("envoi", cid, envoyer_lot)
            jobs._file.join()
            with db.connexion() as c:
                job = jobs.lire(c, jid)
            ordre = [e["charge"]["to"][0]["email"] for e in envoyes]
            verifier("lot : plus gros scores d'abord ; un échec (l1) n'arrête pas le lot",
                     ordre[:3] == ["l0@lot.fr", "l1@lot.fr", "l2@lot.fr"] and "échec" in job["detail"])
            verifier("lot : plafond de 4 respecté (l'email en échec compte comme tenté mais pas comme envoyé), reste signalé",
                     job["statut"] == "termine" and "non envoyé(s)" in job["detail"] and "plafond" in job["detail"])
            verifier("lot : pause aléatoire entre 30 et 60 s entre deux envois réussis (jamais en rafale)",
                     pauses and all(p == 1 for p in pauses) and len(pauses) >= 30)
            verifier("la tâche d'envoi se termine à 100 %", job["progression"] == 100)

            # --- Mode essai dans un lot : seuls les destinataires autorisés partent, les autres sont signalés ---
            with db.connexion() as c:
                c.execute("DELETE FROM envois")
                db.set_reglage(c, "plafond_jour", "50")
                restants = en_attente_d_envoi(c)
                autorise = restants[0]["email"]
                db.set_reglage(c, "envoi_adresses_autorisees", json.dumps([autorise]))
            envoyes.clear()
            comportement["f"] = lambda ch: _Reponse()
            jid = jobs.soumettre("envoi", cid, envoyer_lot)
            jobs._file.join()
            with db.connexion() as c:
                job = jobs.lire(c, jid)
            verifier("lot en mode essai : seul le destinataire autorisé reçoit un email, les autres sont signalés « hors liste du mode essai »",
                     len(restants) >= 2 and [e["charge"]["to"][0]["email"] for e in envoyes] == [autorise]
                     and "hors liste du mode essai" in job["detail"])
            with db.connexion() as c:
                db.set_reglage(c, "envoi_adresses_autorisees", "[]")
                c.execute("DELETE FROM envois")
                c.execute("UPDATE prospects SET statut = 'approuve' WHERE id IN (SELECT prospect_id FROM messages WHERE approuve_le IS NOT NULL) AND statut = 'envoye'")

            # --- Arrêt d'urgence ---
            with db.connexion() as c:
                c.execute("DELETE FROM envois")
                db.set_reglage(c, "plafond_jour", "50")
            comportement["f"] = lambda ch: (demander_arret(), _Reponse())[1]
            envoyes.clear()
            jid = jobs.soumettre("envoi", cid, envoyer_lot)
            jobs._file.join()
            with db.connexion() as c:
                job = jobs.lire(c, jid)
            verifier("arrêt d'urgence : le lot s'arrête après l'envoi en cours, le reste est signalé « arrêt demandé »",
                     len(envoyes) == 1 and "arrêt demandé" in job["detail"])

            # --- Configuration ---
            with db.connexion() as c:
                db.set_reglage(c, "mention_desinscription", "")
            before = len(envoyes)
            verifier("mention de désinscription vide : aucun envoi possible", refus(lot[2]) == "configuration" and len(envoyes) == before)
            with db.connexion() as c:
                db.set_reglage(c, "mention_desinscription", "STOP")
                db.set_reglage(c, "brevo_key", "")
            verifier("clé Brevo absente : aucun envoi possible", refus(lot[2]) == "configuration" and len(envoyes) == before)
        finally:
            jobs._file.join()
            http.requete, time.sleep, config.DB_PATH = http_reel, sleep_reel, chemin_reel
    return resultats


def conn_count(sql, *params):
    with db.connexion() as c:
        return c.execute(sql, params).fetchone()[0]


def conn_val(sql, *params):
    with db.connexion() as c:
        ligne = c.execute(sql, params).fetchone()
        return ligne[0] if ligne else None


def en_attente_ids():
    with db.connexion() as c:
        return [f["message_id"] for f in en_attente_d_envoi(c)]


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) > 1 and argv[1] == "check":
        resultats = _autotests()
        for reussi, libelle in resultats:
            print(f"{'OK ' if reussi else 'KO '} {libelle}")
        ok = sum(1 for r, _ in resultats if r)
        print(f"\nRésultat : {ok}/{len(resultats)} tests réussis")
        return 0 if ok == len(resultats) else 1
    with db.connexion() as conn:
        resultat = tester_connexion(reglages.valeurs_effectives(conn))
    print(f"[{resultat['statut']}] {resultat['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
