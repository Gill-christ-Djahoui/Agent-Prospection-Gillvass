"""Génération des messages par IA, via un endpoint compatible OpenAI.

Le prompt système vient de la campagne (`modeles_message`), jamais du code. Une génération crée une
NOUVELLE version du message (les précédentes sont conservées) et annule toute approbation en cours.
La mention de désinscription n'est pas stockée dans le message : l'envoi l'ajoute à chaque email.

Utilisable seul :
    python -m core.writer                  teste la connexion au fournisseur d'IA
    python -m core.writer message ID       affiche les versions du message d'un prospect
    python -m core.writer check            tests sans réseau, sur base jetable
"""
import json
import logging
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from core import db, http, reglages

journal = logging.getLogger("prospect")

# Variables que le prompt, l'objet et la signature d'un modèle de message peuvent contenir
# (liste imposée par la spécification). Toute autre {variable} est refusée à l'enregistrement.
VARIABLES_PROMPT = ("nom", "ville", "secteur", "problemes", "score", "site", "expediteur", "signature")

STATUTS_GENERABLES = ("qualifie", "message_pret", "approuve")  # un prospect ignoré ou déjà contacté ne l'est pas
_STATUTS_CONFIG = {400, 401, 403, 404}  # erreurs qui se répéteront à l'identique pour tous les prospects
_RE_LIEN = re.compile(r"https?://|www\.", re.I)


class ErreurRedaction(Exception):
    """Échec de rédaction : le message est affiché tel quel dans l'interface."""

    def __init__(self, message, statut_http=None):
        super().__init__(message)
        self.statut_http = statut_http


# --------------------------------------------------------------------------------------
# Appel au fournisseur d'IA
# --------------------------------------------------------------------------------------


def configuration_manquante(cfg):
    """Liste de ce qui manque pour pouvoir appeler l'IA (vide si tout est prêt)."""
    manquants = [nom for nom, cle in (("l'endpoint", "ai_endpoint"), ("le modèle", "ai_model"), ("la clé", "ai_key"))
                 if not (cfg.get(cle) or "").strip()]
    if not (cfg.get("expediteur_nom") or "").strip():
        manquants.append("le nom d'expéditeur (Réglages, Identité d'expédition)")
    return manquants


def appeler_ia(cfg, messages):
    """Envoie la conversation au fournisseur et renvoie le texte de la réponse. Lève ErreurRedaction."""
    endpoint, modele, cle = (cfg["ai_endpoint"].strip(), cfg["ai_model"].strip(), cfg["ai_key"].strip())
    timeout = int(cfg["timeout_ia_s"])
    for essai in range(2):  # un fournisseur saturé (429, 5xx) est retenté une fois, après une pause
        try:
            reponse = http.requete("POST", endpoint, timeout=timeout,
                                   headers={"Authorization": f"Bearer {cle}", "Content-Type": "application/json"},
                                   json={"model": modele, "messages": messages})
        except requests.RequestException as erreur:
            raise ErreurRedaction(http.message_erreur_reseau(erreur, timeout)) from erreur
        if (reponse.status_code == 429 or reponse.status_code >= 500) and essai == 0:
            time.sleep(10)
            continue
        if not reponse.ok:
            raise ErreurRedaction(f"Le fournisseur d'IA refuse la requête (HTTP {reponse.status_code}) : "
                                  f"{http.resume_erreur(reponse)}", reponse.status_code)
        try:
            contenu = reponse.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ErreurRedaction("Réponse du fournisseur illisible : cet endpoint n'est pas compatible OpenAI "
                                  f"(extrait : {reponse.text[:200]})") from None
        if isinstance(contenu, list):  # certains fournisseurs renvoient une liste de morceaux
            contenu = "".join(m.get("text", "") for m in contenu if isinstance(m, dict))
        if not (contenu or "").strip():
            raise ErreurRedaction("Le fournisseur d'IA a renvoyé une réponse vide.")
        return contenu
    raise ErreurRedaction("Le fournisseur d'IA reste indisponible après une nouvelle tentative.")


# --------------------------------------------------------------------------------------
# Construction du prompt
# --------------------------------------------------------------------------------------


def rendre(texte, valeurs):
    """Remplace les {variables} connues. Un simple remplacement (pas str.format) : les accolades
    qu'un utilisateur écrit librement dans son prompt ne cassent rien."""
    for cle, valeur in valeurs.items():
        valeur = str(valeur)
        marque = "{" + cle + "}"
        if cle == "problemes" or "\n" in valeur.strip():  # une liste : sur ses propres lignes, jamais collée à du texte
            texte = re.sub(r"[ \t]*" + re.escape(marque), lambda m: "\n" + valeur if m.start() and texte[m.start() - 1] != "\n" else valeur, texte)
        else:
            texte = texte.replace(marque, valeur)
    return texte


def _nettoyer_reponse(texte):
    texte = texte.strip()
    texte = re.sub(r"^```[a-z]*\n|\n```$", "", texte).strip()
    texte = re.sub(r"^(objet|subject)\s*:.*\n+", "", texte, flags=re.I)  # l'objet vient du gabarit, pas de l'IA
    texte = "\n".join(l.rstrip() for l in texte.splitlines())  # l'IA termine souvent ses lignes par deux espaces (retour Markdown)
    return re.sub(r"\n{3,}", "\n\n", texte).strip()


def _sans_ponctuation(texte):
    return re.sub(r"[\W_]+", "", texte.casefold())


def poser_signature(corps, signature):
    """Le corps suivi de la signature EXACTE des réglages, sans doublon.

    L'IA recopie souvent la signature en la modifiant un peu (virgule, espaces, seulement le prénom) : ces recopies en fin de
    message sont retirées avant d'ajouter la vraie, sinon le message partirait avec deux signatures.
    """
    if not signature:
        return corps
    attendues = [_sans_ponctuation(l) for l in signature.splitlines() if l.strip()]
    lignes = corps.rstrip().split("\n")
    for k in range(len(attendues), 0, -1):  # la plus longue recopie d'abord (signature entière, puis juste le début)
        fin = [l for l in lignes if l.strip()][-k:]
        if len(fin) == k and [_sans_ponctuation(l) for l in fin] == attendues[:k]:
            retirees = 0
            while retirees < k:
                if lignes.pop().strip():
                    retirees += 1
            break
    return "\n".join(lignes).rstrip() + "\n\n" + signature


def verifier_message(corps, signature, cfg):
    """Défauts du message généré (liste vide si conforme) : lien, longueur."""
    defauts = []
    if _RE_LIEN.search(corps):
        defauts.append("il contient un lien ou une adresse web (interdit dans un premier message)")
    maximum = int(cfg.get("mots_max_message") or 0)
    if maximum:
        sans_signature = corps.replace(signature, "") if signature else corps
        mots = len(sans_signature.split())
        if mots > maximum:
            defauts.append(f"il fait {mots} mots (maximum {maximum})")
    return defauts


def _preparer(conn, cfg, prospect_id, relance=False):
    """Rassemble ce qu'il faut pour rédiger. Lève ErreurRedaction si le prospect n'est pas rédigeable."""
    from core import campagnes, scorer, suivi

    p = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    if p is None:
        raise ErreurRedaction("Prospect introuvable.")
    infos_relance = None
    if relance:
        ligne = suivi.relance_possible(conn, cfg, prospect_id)
        if ligne is None:
            raise ErreurRedaction("pas (ou plus) éligible à une relance : sans réponse depuis le délai réglé, "
                                  "et deux relances au maximum")
        precedent = conn.execute(
            "SELECT m.objet, m.corps FROM messages m JOIN envois e ON e.message_id = m.id AND e.statut IN ('envoye', 'ouvert') "
            "WHERE m.prospect_id = ? ORDER BY e.id DESC LIMIT 1", (prospect_id,)).fetchone()
        jours = (datetime.now(timezone.utc) - datetime.strptime(ligne["dernier_envoi"], "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=timezone.utc)).days
        infos_relance = {"numero": ligne["numero"], "jours": jours, "message_precedent": precedent["corps"],
                         "objet_precedent": precedent["objet"]}
    elif p["statut"] == "nouveau":
        raise ErreurRedaction("pas encore analysé : lance d'abord l'analyse des sites")
    elif p["statut"] not in STATUTS_GENERABLES:
        raise ErreurRedaction(f"statut « {p['statut']} » : aucun message ne peut être généré")
    campagne = conn.execute("SELECT * FROM campagnes WHERE id = ?", (p["campagne_id"],)).fetchone()
    if not campagne["modele_message_id"]:
        raise ErreurRedaction(f"la campagne « {campagne['nom']} » n'a pas d'angle de message rattaché")
    modele = conn.execute("SELECT * FROM modeles_message WHERE id = ?", (campagne["modele_message_id"],)).fetchone()

    criteres = {c["code"]: c for c in campagnes.criteres_effectifs(conn, campagne["id"])}
    concrets = sorted((criteres[c] for c in json.loads(p["problemes"]) if c in criteres and criteres[c]["actif"]
                       and criteres[c]["points"] > 0), key=lambda c: -c["points"])
    if not concrets:
        raise ErreurRedaction("aucun problème concret détecté : un message sans problème réel à citer serait inventé")

    signature = (modele["signature"] or "").strip() or (cfg.get("signature_defaut") or "").strip() \
        or cfg["expediteur_nom"].strip()
    valeurs = {
        "nom": p["nom"], "ville": p["ville"] or "", "secteur": campagne["secteur"],
        "problemes": "\n".join(f"- {c['libelle']}" for c in concrets),
        "score": min(p["score"], scorer.PLAFOND_AFFICHAGE),
        "site": p["site"] or "aucun site web connu", "expediteur": cfg["expediteur_nom"].strip(),
        "signature": signature,
    }
    return {"prospect": dict(p), "campagne": dict(campagne), "modele": dict(modele), "valeurs": valeurs,
            "signature": signature, "relance": infos_relance}


# --------------------------------------------------------------------------------------
# Génération
# --------------------------------------------------------------------------------------


def generer_message(cfg, prospect_id, forcer=False, relance=False):
    """Génère une nouvelle version du message d'un prospect (ou sa relance, avec relance=True).

    Renvoie {'statut': 'genere'|'score_bas'|'liste_noire'|'erreur', 'message': str, 'message_id'?}.
    Aucune écriture en base pendant l'appel réseau.
    """
    with db.connexion() as conn:
        try:
            prep = _preparer(conn, cfg, prospect_id, relance)
        except ErreurRedaction as erreur:
            return {"statut": "erreur", "message": str(erreur)}
        p = prep["prospect"]
        if db.est_en_liste_noire(conn, p["email"]):
            return {"statut": "liste_noire", "message": "adresse en liste noire"}
        if not relance and not forcer and p["score"] < int(cfg["score_min_generation"]):
            return {"statut": "score_bas", "message": f"score {p['score']} sous le seuil ({cfg['score_min_generation']})"}

    systeme = rendre(prep["modele"]["prompt_systeme"], prep["valeurs"])
    if prep["relance"]:  # le prompt de relance vient des réglages, jamais du code
        systeme += "\n\n" + rendre(cfg["prompt_relance"], {**prep["valeurs"], **{
            k: v for k, v in prep["relance"].items() if k != "objet_precedent"}})
    systeme += f"\n\nRédige le message en langue « {prep['campagne']['langue']} »."
    conversation = [{"role": "system", "content": systeme},
                    {"role": "user", "content": "Rédige le message de prospection pour cet établissement."}]
    try:
        corps = _nettoyer_reponse(appeler_ia(cfg, conversation))
        defauts = verifier_message(corps, prep["signature"], cfg)
        if defauts:  # une seule correction automatique, puis l'échec est visible
            conversation += [{"role": "assistant", "content": corps},
                             {"role": "user", "content": "Ce message ne respecte pas les règles : "
                              + " ; ".join(defauts) + ". Réécris-le en les respectant strictement."}]
            corps = _nettoyer_reponse(appeler_ia(cfg, conversation))
            defauts = verifier_message(corps, prep["signature"], cfg)
            if defauts:
                raise ErreurRedaction("message refusé, " + " ; ".join(defauts) + " (après une correction)")
    except ErreurRedaction as erreur:
        return {"statut": "erreur", "message": str(erreur), "statut_http": erreur.statut_http}
    corps = poser_signature(corps, prep["signature"])
    if prep["relance"]:
        objet = "Re: " + re.sub(r"^(re\s*:\s*)+", "", prep["relance"]["objet_precedent"], flags=re.I)
    else:
        objet = " ".join(rendre(prep["modele"]["objet_template"], prep["valeurs"]).split())[:200]

    with db.connexion() as conn:
        version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM messages WHERE prospect_id = ?",
                               (prospect_id,)).fetchone()[0]
        # Le trigger de la base annule l'approbation en cours et repasse un prospect « approuvé » à « message_pret ».
        message_id = conn.execute(
            "INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, ?, ?, ?)",
            (prospect_id, objet, corps, version)).lastrowid
        conn.execute("UPDATE prospects SET statut = 'message_pret' WHERE id = ? AND statut IN ('qualifie', 'envoye')",
                     (prospect_id,))
    journal.info("message généré : prospect %s, version %s", prospect_id, version)
    return {"statut": "genere", "message": f"version {version}", "message_id": message_id, "version": version}


def rediger_lot(job_id, prospect_ids, forcer=False, relance=False):
    """Tâche de fond : un prospect à la fois, pause configurable. Un échec n'arrête pas la file."""
    from core import jobs

    with db.connexion() as conn:
        cfg = reglages.valeurs_effectives(conn)
    manquants = configuration_manquante(cfg)
    if manquants:
        return {"statut": "erreur", "detail": "Fournisseur d'IA non configuré : il manque " + ", ".join(manquants) + "."}

    compte = {"genere": 0, "score_bas": 0, "liste_noire": 0, "erreur": 0}
    erreurs, config_consecutives = [], 0
    nb = len(prospect_ids)
    for i, prospect_id in enumerate(prospect_ids):
        with db.connexion() as conn:
            ligne = conn.execute("SELECT nom FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
        nom = ligne["nom"] if ligne else f"#{prospect_id}"
        jobs.maj(job_id, 100 * i / nb, f"Rédaction {i + 1}/{nb} : {nom}")
        resultat = generer_message(cfg, prospect_id, forcer, relance)
        compte[resultat["statut"]] += 1
        if resultat["statut"] == "erreur":
            erreurs.append(f"{nom} ({resultat['message']})")
            config_consecutives = config_consecutives + 1 if resultat.get("statut_http") in _STATUTS_CONFIG else 0
            if config_consecutives >= 3:  # clé refusée, modèle inconnu… : inutile d'insister sur toute la file
                erreurs.append(f"arrêt après 3 refus consécutifs du fournisseur ; {nb - i - 1} prospect(s) non traité(s)")
                break
        else:
            config_consecutives = 0
        if resultat["statut"] == "genere" and i + 1 < nb:
            time.sleep(int(cfg["pause_ia_s"]))
    morceaux = [f"{compte['genere']} message(s) généré(s)"]
    if compte["score_bas"]:
        morceaux.append(f"{compte['score_bas']} sous le seuil de score (non forcés)")
    if compte["liste_noire"]:
        morceaux.append(f"{compte['liste_noire']} en liste noire")
    if erreurs:
        morceaux.append(f"{compte['erreur']} échec(s) : " + " ; ".join(erreurs[:5])
                        + (f" (+{len(erreurs) - 5} autres)" if len(erreurs) > 5 else ""))
    toutes_erreurs = compte["genere"] == 0 and compte["erreur"] > 0
    return {"statut": "erreur" if toutes_erreurs else "termine", "detail": " — ".join(morceaux)}


# --------------------------------------------------------------------------------------
# Test de connexion
# --------------------------------------------------------------------------------------


def url_des_modeles(endpoint):
    """Adresse de la liste des modèles, déduite de l'endpoint de chat (convention OpenAI : …/models)."""
    endpoint = endpoint.strip().rstrip("/")
    for suffixe in ("/chat/completions", "/completions"):
        if endpoint.endswith(suffixe):
            return endpoint[: -len(suffixe)] + "/models"
    return endpoint.rsplit("/", 1)[0] + "/models"


def lister_modeles(cfg):
    """Identifiants des modèles proposés par le fournisseur (appel réel, rien n'est consommé).

    Renvoie {'statut': 'ok', 'modeles': [...]} ou {'statut': 'erreur'|'non_configure', 'message': ...}.
    """
    endpoint = (cfg.get("ai_endpoint") or "").strip()
    cle = (cfg.get("ai_key") or "").strip()
    manquants = [nom for nom, valeur in (("l'endpoint", endpoint), ("la clé", cle)) if not valeur]
    if manquants:
        return {"statut": "non_configure", "message": f"Il manque {' et '.join(manquants)}."}
    timeout = int(cfg["http_timeout_s"])
    try:
        reponse = http.requete("GET", url_des_modeles(endpoint), timeout=timeout,
                               headers={"Authorization": f"Bearer {cle}", "Accept": "application/json"})
    except requests.RequestException as erreur:
        return {"statut": "erreur", "message": http.message_erreur_reseau(erreur, timeout)}
    if not reponse.ok:
        return {"statut": "erreur", "message": f"Le fournisseur refuse la requête (HTTP {reponse.status_code}) : "
                                               f"{http.resume_erreur(reponse)}"}
    try:
        modeles = sorted({m["id"] for m in reponse.json()["data"] if m.get("id")})
    except (ValueError, KeyError, TypeError):
        return {"statut": "erreur", "message": "Ce fournisseur ne publie pas sa liste de modèles à l'adresse habituelle "
                                               f"(extrait : {reponse.text[:150]}). Saisis l'identifiant du modèle à la main."}
    if not modeles:
        return {"statut": "erreur", "message": "Le fournisseur n'a renvoyé aucun modèle."}
    return {"statut": "ok", "modeles": modeles}


def tester_connexion(cfg):
    """Appel réel (une courte réponse) à l'endpoint configuré."""
    endpoint = (cfg.get("ai_endpoint") or "").strip()
    modele = (cfg.get("ai_model") or "").strip()
    cle = (cfg.get("ai_key") or "").strip()
    manquants = [nom for nom, valeur in
                 (("l'endpoint", endpoint), ("le modèle", modele), ("la clé", cle)) if not valeur]
    if manquants:
        return {"statut": "non_configure", "message": f"Il manque {', '.join(manquants)}."}
    timeout = int(cfg["http_timeout_s"])
    try:
        # Pas de max_tokens : certains modèles récents le refusent (max_completion_tokens à la place).
        reponse = http.requete(
            "POST", endpoint, timeout=timeout,
            headers={"Authorization": f"Bearer {cle}", "Content-Type": "application/json"},
            json={"model": modele, "messages": [{"role": "user", "content": "Réponds uniquement : ok"}]},
        )
    except requests.RequestException as erreur:
        return {"statut": "erreur", "message": http.message_erreur_reseau(erreur, timeout)}
    if not reponse.ok:
        message = (f"Le fournisseur refuse la requête (HTTP {reponse.status_code}) : "
                   f"{http.resume_erreur(reponse)}")
        if reponse.status_code in (400, 404) and "model" in message.lower():
            message += (" — La clé est acceptée, mais le champ « Modèle » doit contenir l'identifiant exact d'un modèle "
                        "(pas le nom du fournisseur) : clique sur « Voir les modèles disponibles ».")
        return {"statut": "erreur", "message": message}
    try:
        reponse.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        return {"statut": "erreur",
                "message": "Réponse reçue mais format inattendu : cet endpoint n'est pas compatible "
                           f"OpenAI (extrait : {reponse.text[:200]})"}
    return {"statut": "ok", "message": f"Le modèle « {modele} » a répondu."}


# --------------------------------------------------------------------------------------
# Auto-tests (python -m core.writer check) : IA simulée, base jetable
# --------------------------------------------------------------------------------------


class _Reponse:
    def __init__(self, contenu=None, statut=200, brut=None):
        self.status_code, self.ok = statut, statut < 400
        self._json = brut if brut is not None else {"choices": [{"message": {"content": contenu}}]}
        self.text = json.dumps(self._json)

    def json(self):
        return self._json


def _autotests():
    import config
    from core import campagnes, jobs, seeds

    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    verifier("réponse de l'IA nettoyée : espaces de fin de ligne retirés (retours Markdown), jamais plus d'une ligne vide, paragraphes gardés",
             _nettoyer_reponse("Objet : x\nBonjour,  \n\n\n\nPremière phrase.  \nDeuxième phrase.\n\nQuestion ?  ")
             == "Bonjour,\n\nPremière phrase.\nDeuxième phrase.\n\nQuestion ?")
    verifier("le prompt par défaut impose un message court, aéré, sans jargon ni mot de vente, et n'est pas propre à un métier",
             all(m in seeds.composer_prompt("Angle : x") for m in ("Entre 45 et 75 mots", "15 mots au maximum", "aucun terme technique", "Aucun mot de vente"))
             and "patient" not in seeds._PROMPT_COMMUN.lower() and "Bonjour Maître" in seeds.composer_prompt("A", seeds.REGLE_SALUTATION_AVOCATS)
             and "{regle_salutation}" not in seeds.composer_prompt("A") and "{angle}" not in seeds.composer_prompt("A"))

    appels = []
    comportement = {"fonction": None}

    def faux_http(methode, url, *, timeout, **kw):
        appels.append(kw["json"])
        return comportement["fonction"](kw["json"])

    http_reel, sleep_reel, chemin_reel = http.requete, time.sleep, config.DB_PATH
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        config.DB_PATH = Path(tmp) / "t.db"
        http.requete, time.sleep = faux_http, lambda s: None
        try:
            db.init_db(importer_env=False)
            with db.connexion() as c:
                for cle, val in (("ai_endpoint", "https://ia.test/v1/chat"), ("ai_model", "m"), ("ai_key", "k"),
                                 ("expediteur_nom", "Gill"), ("pause_ia_s", "0")):
                    db.set_reglage(c, cle, val)
                cid = campagnes.creer(c, {"nom": "Dentistes", "secteur": "cabinet dentaire", "pays": "FR", "langue": "fr"},
                                      modele_depart_id=c.execute("SELECT id FROM campagnes WHERE est_modele = 1 ORDER BY id LIMIT 1 OFFSET 1").fetchone()[0])
                base = {"campagne_id": cid, "source": "openstreetmap", "ville": "Lyon"}
                specs = [("A", ["pas_rdv", "lent"], 9, "a@a.fr"), ("B", ["pas_mobile"], 3, "b@b.fr"),
                         ("C", ["lent"], 6, "c@listenoire.fr"), ("D", ["site_inconnu"], 0, "d@d.fr"),
                         ("E", ["pas_rdv", "lent", "pas_mobile"], 9, "e@e.fr")]
                ids = {}
                for nom, codes, score, email in specs:
                    db.inserer_prospect(c, {**base, "nom": nom, "email": email, "source_id": "osm:node/" + nom,
                                            "site": f"https://{nom.lower()}.fr"})
                    ids[nom] = c.execute("SELECT id FROM prospects WHERE nom = ?", (nom,)).fetchone()[0]
                    c.execute("UPDATE prospects SET problemes = ?, score = ?, statut = 'qualifie' WHERE id = ?",
                              (json.dumps(codes), score, ids[nom]))
                c.execute("UPDATE prospects SET statut = 'nouveau' WHERE nom = 'E'")
                db.ajouter_liste_noire(c, "listenoire.fr", "manuel")
            with db.connexion() as c:
                cfg = reglages_cfg = reglages.valeurs_effectives(c)

            # --- Prompt ---
            comportement["fonction"] = lambda j: _Reponse("Bonjour A, votre site met du temps à charger.\n\nGill")
            r = generer_message(cfg, ids["A"])
            systeme = appels[-1]["messages"][0]["content"]
            verifier("message généré : succès, version 1, objet issu du gabarit (pas de l'IA)",
                     r["statut"] == "genere" and r["version"] == 1)
            verifier("prompt : variables remplacées (nom, ville, secteur, expéditeur, score), aucune {variable} restante",
                     "A" in systeme and "Lyon" in systeme and "cabinet dentaire" in systeme and "Gill" in systeme
                     and not re.search(r"\{(nom|ville|secteur|problemes|score|site|expediteur|signature)\}", systeme))
            verifier("prompt : problèmes concrets en clair, du plus lourd au plus léger ; langue de la campagne ajoutée",
                     systeme.index("Pas de prise de rendez-vous") < systeme.index("Site lent") and "langue « fr »" in systeme
                     and "Score de besoin : 9" in systeme)
            with db.connexion() as c:
                m = dict(c.execute("SELECT * FROM messages WHERE prospect_id = ?", (ids["A"],)).fetchone())
                statut_a = c.execute("SELECT statut FROM prospects WHERE id = ?", (ids["A"],)).fetchone()[0]
            verifier("message enregistré : objet rendu depuis le gabarit, corps conservé, prospect « message_pret »",
                     m["objet"] == "A : vos patients vous trouvent-ils ?" and "Bonjour A" in m["corps"]
                     and statut_a == "message_pret" and m["edite_par_humain"] == 0 and m["approuve_le"] is None)
            verifier("signature absente de la réponse : ajoutée à la fin",
                     m["corps"].rstrip().endswith("Gill"))
            signature_test = "Gill, \nDéveloppeur Web & Mobile Freelance"
            verifier("signature recopiée par l'IA avec des variantes (virgule, espaces, majuscules, seulement le prénom) : jamais en double",
                     poser_signature("Bonjour,\n\nTexte.\n\nGill,\nDéveloppeur Web & Mobile Freelance", signature_test)
                     == "Bonjour,\n\nTexte.\n\n" + signature_test
                     and poser_signature("Bonjour,\n\nTexte.\n\nGill", signature_test) == "Bonjour,\n\nTexte.\n\n" + signature_test
                     and poser_signature("Bonjour.\n\nGill,  \nDéveloppeur web & mobile freelance\n\n", signature_test)
                     == "Bonjour.\n\n" + signature_test
                     and poser_signature("Bonjour,\n\nTexte.", signature_test) == "Bonjour,\n\nTexte.\n\n" + signature_test
                     and poser_signature("Texte", "") == "Texte")
            verifier("un « Gill » au milieu du message n'est pas pris pour une signature : conservé, la signature est ajoutée à la fin",
                     poser_signature("Gill m'a dit bonjour.\n\nTexte final.", "Gill") == "Gill m'a dit bonjour.\n\nTexte final.\n\nGill")

            # --- Régénération : nouvelle version, approbation annulée ---
            with db.connexion() as c:
                c.execute("UPDATE messages SET approuve_le = '2026-01-01T00:00:00Z', approuve_par_humain = 1 WHERE id = ?", (m["id"],))
                c.execute("UPDATE prospects SET statut = 'approuve' WHERE id = ?", (ids["A"],))
            comportement["fonction"] = lambda j: _Reponse("Version deux.\n\nGill")
            r2 = generer_message(cfg, ids["A"])
            with db.connexion() as c:
                v1 = c.execute("SELECT approuve_le, corps FROM messages WHERE prospect_id = ? AND version = 1", (ids["A"],)).fetchone()
                statut_a = c.execute("SELECT statut FROM prospects WHERE id = ?", (ids["A"],)).fetchone()[0]
            verifier("régénération : version 2 créée, version 1 conservée mais plus approuvée, prospect repassé à message_pret",
                     r2["version"] == 2 and v1["approuve_le"] is None and "Bonjour A" in v1["corps"] and statut_a == "message_pret")

            # --- Seuil, force, liste noire, statuts ---
            verifier("score sous le seuil (3 < 5) : pas de génération, aucun appel IA",
                     (lambda n: generer_message(cfg, ids["B"])["statut"] == "score_bas" and len(appels) == n)(len(appels)))
            verifier("génération forcée sous le seuil : acceptée",
                     generer_message(cfg, ids["B"], forcer=True)["statut"] == "genere")
            verifier("adresse en liste noire (domaine) : refusée même avec score élevé, même forcée",
                     generer_message(cfg, ids["C"], forcer=True)["statut"] == "liste_noire")
            verifier("prospect sans problème concret (site_inconnu seul) : refusé même forcé, message explicite",
                     "aucun problème concret" in generer_message(cfg, ids["D"], forcer=True)["message"])
            verifier("prospect pas encore analysé : refusé", "pas encore analysé" in generer_message(cfg, ids["E"], forcer=True)["message"])

            # --- Contrôle du message : lien, longueur, correction automatique ---
            appels.clear()
            réponses = iter(["Visitez https://monsite.fr\n\nGill", "Version propre sans lien.\n\nGill"])
            comportement["fonction"] = lambda j: _Reponse(next(réponses))
            r3 = generer_message(cfg, ids["A"])
            verifier("lien dans la réponse : une correction automatique est demandée, puis acceptée",
                     r3["statut"] == "genere" and len(appels) == 2 and "lien" in appels[1]["messages"][-1]["content"])
            comportement["fonction"] = lambda j: _Reponse("Voir www.exemple.fr\n\nGill")
            verifier("lien persistant après correction : échec visible, aucun message créé",
                     generer_message(cfg, ids["A"])["statut"] == "erreur")
            comportement["fonction"] = lambda j: _Reponse("mot " * 130 + "\n\nGill")
            verifier("trop long (> 120 mots) : refusé avec le nombre de mots",
                     "130 mots" in generer_message(cfg, ids["A"])["message"])

            # --- Erreurs du fournisseur ---
            comportement["fonction"] = lambda j: _Reponse(statut=401, brut={"error": {"message": "Invalid API Key"}})
            r4 = generer_message(cfg, ids["A"])
            verifier("HTTP 401 : le message réel du fournisseur est conservé",
                     r4["statut"] == "erreur" and "Invalid API Key" in r4["message"] and r4["statut_http"] == 401)
            comportement["fonction"] = lambda j: _Reponse(brut={"pas": "openai"})
            verifier("réponse non compatible OpenAI : erreur explicite", "compatible OpenAI" in generer_message(cfg, ids["A"])["message"])
            comportement["fonction"] = lambda j: _Reponse("   ")
            verifier("réponse vide : erreur (jamais de message vide en base)", "vide" in generer_message(cfg, ids["A"])["message"])
            with db.connexion() as c:
                nb_a = c.execute("SELECT COUNT(*) FROM messages WHERE prospect_id = ?", (ids["A"],)).fetchone()[0]
            verifier("aucun message vide ou refusé n'a été stocké (A : 3 versions valides seulement)", nb_a == 3)

            # --- Lot : un échec n'arrête pas la file ; arrêt après 3 refus d'authentification ---
            avec_a = {"n": 0}

            def alterne(j):
                avec_a["n"] += 1
                return _Reponse(statut=500, brut={"error": "panne"}) if avec_a["n"] <= 2 else _Reponse("Bonjour.\n\nGill")
            comportement["fonction"] = alterne
            jid = jobs.soumettre("redaction", cid, rediger_lot, [ids["A"], ids["B"], ids["C"], ids["D"]], True)
            jobs._file.join()
            with db.connexion() as c:
                job = jobs.lire(c, jid)
            verifier("lot : le prospect en échec n'interrompt pas la file ; résumé complet",
                     job["statut"] == "termine" and "message(s) généré(s)" in job["detail"] and "liste noire" in job["detail"]
                     and "échec" in job["detail"])
            comportement["fonction"] = lambda j: _Reponse(statut=401, brut={"error": "Invalid API Key"})
            with db.connexion() as c:
                c.execute("UPDATE prospects SET statut = 'qualifie' WHERE nom IN ('A', 'B')")
                for nom in "FGH":
                    db.inserer_prospect(c, {**base, "nom": nom, "email": nom.lower() + "@x.fr", "source_id": "osm:node/" + nom})
                    pid = c.execute("SELECT id FROM prospects WHERE nom = ?", (nom,)).fetchone()[0]
                    c.execute("UPDATE prospects SET problemes = '[\"lent\"]', score = 8, statut = 'qualifie' WHERE id = ?", (pid,))
                    ids[nom] = pid
            jid2 = jobs.soumettre("redaction", cid, rediger_lot, [ids["A"], ids["B"], ids["F"], ids["G"], ids["H"]], True)
            jobs._file.join()
            with db.connexion() as c:
                job2 = jobs.lire(c, jid2)
            verifier("clé refusée : arrêt après 3 refus consécutifs, prospects restants signalés (pas de boucle inutile)",
                     job2["statut"] == "erreur" and "arrêt après 3 refus" in job2["detail"] and "2 prospect(s) non traité(s)" in job2["detail"])

            # --- Relances : même chemin que le premier message, avec le prompt de relance des réglages ---
            with db.connexion() as c:
                db.set_reglage(c, "expediteur_nom", "Gill")
                db.set_reglage(c, "ai_key", "k")
                for nom, jours, nb in (("R", 10, 1), ("S", 1, 1), ("T", 10, 3)):
                    db.inserer_prospect(c, {**base, "nom": nom, "email": nom.lower() + "@r.fr", "source_id": "osm:" + nom})
                    pid = ids[nom] = c.execute("SELECT id FROM prospects WHERE nom = ?", (nom,)).fetchone()[0]
                    c.execute("UPDATE prospects SET problemes = '[\"lent\"]', score = 8, statut = 'qualifie' WHERE id = ?", (pid,))
                    for k in range(nb):
                        mid = c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'Re: Votre site', ?, ?)",
                                        (pid, f"Premier message à {nom}", k + 1)).lastrowid
                        c.execute("INSERT INTO envois (message_id, statut, envoye_le) VALUES (?, 'envoye', "
                                  "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?))", (mid, f"-{jours} days"))
                    c.execute("UPDATE prospects SET statut = 'envoye' WHERE id = ?", (pid,))
                cfg = reglages.valeurs_effectives(c)
            comportement["fonction"] = lambda j: _Reponse("Je me permets de revenir vers vous.\n\nGill")
            appels.clear()
            r = generer_message(cfg, ids["R"], relance=True)
            systeme = appels[-1]["messages"][0]["content"]
            with db.connexion() as c:
                nouveau = dict(c.execute("SELECT * FROM messages WHERE prospect_id = ? ORDER BY version DESC LIMIT 1", (ids["R"],)).fetchone())
                statut_r = c.execute("SELECT statut FROM prospects WHERE id = ?", (ids["R"],)).fetchone()[0]
            verifier("relance : prompt de la campagne + consignes de relance des réglages (n°1, 10 jours, message précédent)",
                     r["statut"] == "genere" and "relance n°1" in systeme and "il y a 10 jours" in systeme
                     and "Premier message à R" in systeme and "Problèmes constatés" in systeme)
            verifier("relance : objet « Re: » sans doublon, nouvelle version, prospect repassé « message_pret » pour validation",
                     nouveau["objet"] == "Re: Votre site" and nouveau["version"] == 2 and statut_r == "message_pret")
            verifier("relance : prospect envoyé hier (délai non atteint) refusé", "éligible" in generer_message(cfg, ids["S"], relance=True)["message"])
            verifier("relance : prospect qui a déjà eu 1 message + 2 relances : abandon définitif, refusé",
                     "éligible" in generer_message(cfg, ids["T"], relance=True)["message"])
            verifier("sans le drapeau relance, un prospect déjà contacté n'est pas régénérable",
                     "statut" in generer_message(cfg, ids["S"], forcer=True)["message"])

            # --- Liste des modèles du fournisseur ---
            verifier("URL de la liste des modèles déduite de l'endpoint (Groq, OpenAI, xAI et chemins inhabituels)",
                     url_des_modeles("https://api.groq.com/openai/v1/chat/completions") == "https://api.groq.com/openai/v1/models"
                     and url_des_modeles("https://api.openai.com/v1/chat/completions/") == "https://api.openai.com/v1/models"
                     and url_des_modeles("http://127.0.0.1:11434/v1/chat") == "http://127.0.0.1:11434/v1/models")
            urls_vues = []

            def faux_modeles(methode, url, *, timeout, **kw):
                urls_vues.append((methode, url))
                return _Reponse(brut={"data": [{"id": "zeta-modele"}, {"id": "alpha-modele"}, {"id": "alpha-modele"}, {"object": "x"}]})
            http.requete = faux_modeles
            r = lister_modeles({**cfg, "ai_endpoint": "https://api.groq.com/openai/v1/chat/completions", "ai_key": "k"})
            verifier("liste des modèles : appel GET sur /models, identifiants triés et sans doublon",
                     r == {"statut": "ok", "modeles": ["alpha-modele", "zeta-modele"]} and urls_vues[0] == ("GET", "https://api.groq.com/openai/v1/models"))
            http.requete = lambda *a, **k: _Reponse(statut=401, brut={"error": {"message": "Invalid API Key"}})
            verifier("liste des modèles : clé refusée -> message réel du fournisseur",
                     "Invalid API Key" in lister_modeles({**cfg, "ai_endpoint": "https://x/v1/chat/completions", "ai_key": "k"})["message"])
            http.requete = lambda *a, **k: _Reponse(brut={"pas": "openai"})
            verifier("liste des modèles : réponse inhabituelle -> message clair, pas de plantage",
                     "à la main" in lister_modeles({**cfg, "ai_endpoint": "https://x/v1/chat/completions", "ai_key": "k"})["message"])
            verifier("liste des modèles : sans clé -> « non configuré », aucun appel",
                     lister_modeles({**cfg, "ai_key": ""})["statut"] == "non_configure")
            http.requete = lambda *a, **k: _Reponse(statut=404, brut={"error": {"message": "The model `Groq` does not exist"}})
            message = tester_connexion({**cfg, "ai_endpoint": "https://x/v1/chat/completions", "ai_key": "k", "ai_model": "Groq", "http_timeout_s": "5"})["message"]
            verifier("modèle inexistant (404) : le message explique quoi faire (identifiant exact + bouton de la liste)",
                     "identifiant exact" in message and "Voir les modèles" in message)
            http.requete = faux_http

            # --- Configuration ---
            with db.connexion() as c:
                db.set_reglage(c, "expediteur_nom", "")
                db.set_reglage(c, "ai_key", "")
                cfg2 = reglages.valeurs_effectives(c)
            verifier("configuration incomplète : ce qui manque est nommé (clé, nom d'expéditeur)",
                     any("clé" in m for m in configuration_manquante(cfg2)) and any("nom d'expéditeur" in m for m in configuration_manquante(cfg2)))
            jid3 = jobs.soumettre("redaction", cid, rediger_lot, [ids["A"]], False)
            jobs._file.join()
            with db.connexion() as c:
                job3 = jobs.lire(c, jid3)
            verifier("lot sans configuration : erreur explicite, aucun appel réseau", job3["statut"] == "erreur" and "non configuré" in job3["detail"])
        finally:
            jobs._file.join()
            http.requete, time.sleep, config.DB_PATH = http_reel, sleep_reel, chemin_reel
    return resultats


def _afficher_messages(prospect_id):
    with db.connexion() as conn:
        lignes = conn.execute("SELECT * FROM messages WHERE prospect_id = ? ORDER BY version", (prospect_id,)).fetchall()
        if not lignes:
            print("Aucun message pour ce prospect.")
        for m in lignes:
            approuve = f"approuvé le {m['approuve_le']}" if m["approuve_le"] else "non approuvé"
            print(f"--- version {m['version']} ({m['genere_le']}, {approuve})\nObjet : {m['objet']}\n\n{m['corps']}\n")


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
    if len(argv) > 2 and argv[1] == "message":
        _afficher_messages(int(argv[2]))
        return 0
    with db.connexion() as conn:
        resultat = tester_connexion(reglages.valeurs_effectives(conn))
    print(f"[{resultat['statut']}] {resultat['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
