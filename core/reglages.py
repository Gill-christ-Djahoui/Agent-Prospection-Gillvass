"""Réglages : définition des champs éditables, validation, lecture masquée, enregistrement.

Utilisable seul : python -m core.reglages   (affiche les réglages, clés masquées)
"""
import json
import logging
import re
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import db

journal = logging.getLogger("prospect")

_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ETIQUETTE_OSM = r"(?:nom|[\w:\-]+=[\w:\-|]+)"
_RE_REGLE_OSM = re.compile(rf"^[^=]+=\s*{_ETIQUETTE_OSM}(?:\s*,\s*{_ETIQUETTE_OSM})*$")
_RE_DOMAINE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

# Champs éditables depuis l'écran Réglages. Les clés internes (env_importe, seeds_appliques)
# n'y figurent pas : elles ne sont jamais modifiables depuis l'interface.
SPECS = {
    # Clés et services
    "sourcing_provider": {"type": "choix", "choix": ("auto", "google", "osm")},
    "google_places_key": {"type": "secret"},
    "ai_endpoint": {"type": "url"},
    "ai_model": {"type": "texte"},
    "ai_key": {"type": "secret"},
    "brevo_key": {"type": "secret"},
    "imap_serveur": {"type": "texte"},
    "imap_port": {"type": "entier", "min": 1, "max": 65535},
    "imap_utilisateur": {"type": "texte"},
    "imap_mot_de_passe": {"type": "secret"},
    "imap_dossier": {"type": "texte"},
    "canal_appels": {"type": "choix", "choix": ("oui", "non")},
    # Identité d'expédition
    "expediteur_nom": {"type": "texte"},
    "expediteur_email": {"type": "email"},
    "reponse_email": {"type": "email"},
    "signature_defaut": {"type": "texte"},
    "mon_site": {"type": "url"},
    "site_dans_signature": {"type": "choix", "choix": ("jamais", "relances", "toujours")},
    "mention_desinscription": {"type": "texte", "requis": True},
    "email_format": {"type": "choix", "choix": ("html", "texte")},
    "email_couleur_accent": {"type": "couleur"},
    "email_style": {"type": "choix", "choix": ("elegant", "sobre")},
    "modele_email_elegant": {"type": "texte", "requis": True, "variables": ("contenu", "mention", "couleur"),
                             "obligatoires": ("contenu",)},
    "modele_email_html": {"type": "texte", "requis": True, "variables": ("contenu", "mention", "couleur"),
                          "obligatoires": ("contenu",)},
    "prompt_relance": {"type": "texte", "requis": True, "variables": (
        "nom", "ville", "secteur", "problemes", "score", "site", "expediteur", "signature",
        "numero", "jours", "message_precedent")},
    # Limites
    "plafond_jour": {"type": "entier", "min": 0, "max": 1000},
    "envoi_adresses_autorisees": {"type": "liste", "adresses": True},
    "pause_envoi_min_s": {"type": "entier", "min": 0, "max": 3600},
    "pause_envoi_max_s": {"type": "entier", "min": 0, "max": 3600},
    "pause_ia_s": {"type": "entier", "min": 0, "max": 600},
    "timeout_ia_s": {"type": "entier", "min": 5, "max": 300},
    "mots_max_message": {"type": "entier", "min": 0, "max": 1000},
    "score_min_generation": {"type": "entier", "min": 0, "max": 100},
    "delai_relance_jours": {"type": "entier", "min": 1, "max": 365},
    "max_relances": {"type": "entier", "min": 0, "max": 10},
    # Avancé : scan, analyse, dédoublonnage, temps
    "plafond_resultats_scan": {"type": "entier", "min": 1, "max": 1000},
    "http_timeout_s": {"type": "entier", "min": 1, "max": 120},
    "seuil_lenteur_s": {"type": "entier", "min": 1, "max": 60},
    "seuil_avis_min": {"type": "entier", "min": 0, "max": 100000},
    "pause_requetes_s": {"type": "entier", "min": 0, "max": 60},
    "timeout_overpass_s": {"type": "entier", "min": 10, "max": 300},
    "overpass_nouvelle_tentative_s": {"type": "entier", "min": 0, "max": 300},
    "pause_pages_s": {"type": "entier", "min": 0, "max": 60},
    "osm_correspondances": {"type": "regles_osm"},
    "google_requetes_mois_max": {"type": "entier", "min": 0, "max": 1000000},
    "overpass_urls": {"type": "liste", "urls": True},
    "montee_plafonds": {"type": "liste_entiers", "min": 1, "max": 100000},
    "analyses_tranches_score": {"type": "liste_entiers", "min": 0, "max": 1000},
    "dkim_selecteurs": {"type": "liste"},
    "prompt_analyste": {"type": "texte", "requis": True},
    "prompt_niche": {"type": "texte", "requis": True, "variables": (
        "niche", "offre", "probleme", "ville", "pays", "criteres", "zones_instruction", "nom")},
    "prompt_appel": {"type": "texte", "requis": True, "variables": (
        "expediteur", "nom", "ville", "secteur", "site", "problemes", "score", "angle")},
    "prompt_formulaire": {"type": "texte", "requis": True, "variables": (
        "expediteur", "nom", "ville", "secteur", "site", "problemes", "score", "angle", "signature", "reputation")},
    "mots_interdits_message": {"type": "liste"},
    "prompt_social": {"type": "texte", "requis": True, "variables": (
        "expediteur", "nom", "ville", "secteur", "site", "problemes", "score", "angle", "signature", "reputation")},
    "domaines_reseaux": {"type": "liste", "domaines": True},
    "whatsapp_indicatif": {"type": "entier", "min": 1, "max": 999},
    "whatsapp_prefixes_mobiles": {"type": "liste"},
    "domaines_formulaires": {"type": "liste"},
    "mots_cles_contact": {"type": "liste"},
    "contact_liens_max": {"type": "entier", "min": 0, "max": 10},
    "niche_zones_max": {"type": "entier", "min": 1, "max": 60},
    "niche_distance_max_km": {"type": "entier", "min": 5, "max": 300},
    "domaines_plateformes": {"type": "liste", "domaines": True},
    "sources_site_fiable": {"type": "liste", "valeurs": ("google_places", "openstreetmap", "import_csv")},
    "emails_exclus": {"type": "liste"},
    "pages_contact": {"type": "liste", "chemins": True},
    "fuseau": {"type": "fuseau"},
    "session_heures": {"type": "entier", "min": 1, "max": 720},
    "analyses_echantillon_min": {"type": "entier", "min": 5, "max": 100000},
    "analyses_echantillon_groupe": {"type": "entier", "min": 3, "max": 100000},
    "analyses_seuil_rebond_pct": {"type": "entier", "min": 1, "max": 100},
    "analyses_seuil_ouverture_pct": {"type": "entier", "min": 1, "max": 100},
    "sauvegardes_max": {"type": "entier", "min": 1, "max": 365},
    "mots_cles_rdv": {"type": "liste"},
    "import_statuts_email_acceptes": {"type": "liste"},
    "titres_generiques": {"type": "liste"},
    "mentions_juridiques": {"type": "liste"},
    "domaines_generiques": {"type": "liste", "domaines": True},
}


class ErreurValidation(Exception):
    def __init__(self, erreurs):
        super().__init__("; ".join(f"{c}: {m}" for c, m in erreurs.items()))
        self.erreurs = erreurs


def _liste_depuis(valeur):
    """Accepte une liste ou un texte (une entrée par ligne). Minuscules, sans doublon, ordre conservé."""
    if isinstance(valeur, str):
        valeur = valeur.splitlines()
    vus, propre = set(), []
    for element in valeur:
        texte = str(element).strip().lower()
        if texte and texte not in vus:
            vus.add(texte)
            propre.append(texte)
    return propre


def _valider_un(cle, brut, spec):
    """Renvoie la valeur à stocker (texte), ou lève ValueError avec un message clair."""
    genre = spec["type"]
    texte = "" if brut is None else str(brut).strip() if not isinstance(brut, list) else ""
    if genre == "choix":
        if texte not in spec["choix"]:
            raise ValueError(f"Valeur attendue : {', '.join(spec['choix'])}.")
        return texte
    if genre == "entier":
        try:
            nombre = int(texte)
        except ValueError:
            raise ValueError("Un nombre entier est attendu.") from None
        if not spec["min"] <= nombre <= spec["max"]:
            raise ValueError(f"Valeur comprise entre {spec['min']} et {spec['max']}.")
        return str(nombre)
    if genre == "email":
        adresse = db.normaliser_email(texte)
        if adresse is None:
            return ""
        if not _RE_EMAIL.match(adresse):
            raise ValueError("Adresse email invalide.")
        return adresse
    if genre == "url":
        if texte and not re.match(r"^https?://[^\s/]+", texte):
            raise ValueError("Adresse attendue, commençant par http:// ou https://.")
        return texte
    if genre == "couleur":
        if not re.match(r"^#[0-9a-fA-F]{6}$", texte):
            raise ValueError("Couleur attendue au format #RRGGBB (exemple : #1f6feb).")
        return texte.lower()
    if genre == "fuseau":
        try:
            ZoneInfo(texte)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Fuseau inconnu (exemple : Europe/Paris).") from None
        return texte
    if genre == "liste_entiers":
        morceaux = [m for m in re.split(r"[\s,;]+", brut if isinstance(brut, str) else " ".join(map(str, brut or []))) if m]
        try:
            nombres = [int(m) for m in morceaux]
        except ValueError:
            raise ValueError("Des nombres entiers sont attendus, un par ligne (ou séparés par des virgules).") from None
        if not nombres or any(not spec["min"] <= n <= spec["max"] for n in nombres):
            raise ValueError(f"Au moins un nombre, chacun compris entre {spec['min']} et {spec['max']}.")
        return json.dumps(nombres)
    if genre == "regles_osm":
        lignes = _liste_depuis(brut if brut is not None else "")
        invalides = [l for l in lignes if not _RE_REGLE_OSM.match(l)]
        if invalides:
            raise ValueError("Ligne(s) invalide(s) (format : mot1|mot2 = tag=valeur, tag=valeur) : "
                             + " / ".join(invalides[:3]))
        return json.dumps(lignes, ensure_ascii=False)
    if genre == "liste":
        elements = _liste_depuis(brut if brut is not None else "")
        if spec.get("adresses"):
            elements = [e.lstrip("@") for e in elements]
            invalides = [e for e in elements if not (_RE_EMAIL.match(e) or _RE_DOMAINE.match(e))]
            if invalides:
                raise ValueError(f"Adresse(s) ou domaine(s) invalide(s) : {', '.join(invalides[:5])}.")
        if spec.get("valeurs"):
            invalides = [e for e in elements if e not in spec["valeurs"]]
            if invalides:
                raise ValueError(f"Valeurs possibles : {', '.join(spec['valeurs'])}.")
        if spec.get("urls"):
            invalides = [e for e in elements if not re.match(r"^https?://[^\s/]+", e)]
            if invalides:
                raise ValueError(f"Adresse(s) invalide(s) (http:// ou https://) : {', '.join(invalides[:3])}.")
        if spec.get("chemins"):
            invalides = [e for e in elements if not e.startswith("/")]
            if invalides:
                raise ValueError(f"Chaque chemin commence par « / » : {', '.join(invalides[:5])}.")
        if spec.get("domaines"):
            elements = [e.lstrip("@") for e in elements]
            invalides = [e for e in elements if not _RE_DOMAINE.match(e)]
            if invalides:
                raise ValueError(f"Domaine(s) invalide(s) : {', '.join(invalides[:5])}.")
        return json.dumps(elements, ensure_ascii=False)
    if genre == "secret":
        return texte
    # texte libre
    if spec.get("requis") and not texte:
        raise ValueError("Ce champ ne peut pas être vide.")
    if spec.get("variables"):
        inconnues = sorted(set(re.findall(r"\{(\w*)\}", texte)) - set(spec["variables"]))
        if inconnues:
            raise ValueError("Variable(s) inconnue(s) : " + ", ".join("{" + v + "}" for v in inconnues)
                             + ". Autorisées : " + ", ".join("{" + v + "}" for v in spec["variables"]) + ".")
    manquantes = [v for v in spec.get("obligatoires", ()) if "{" + v + "}" not in texte]
    if manquantes:
        raise ValueError("Doit contenir " + ", ".join("{" + v + "}" for v in manquantes) + " : c'est là que le texte du message est placé.")
    return texte


def valider(valeurs, existant=None):
    """Valide un lot de valeurs. Renvoie {clé: valeur à stocker} ; lève ErreurValidation sinon.

    Un secret vide est ignoré (la clé enregistrée est conservée).
    """
    propres, erreurs = {}, {}
    for cle, brut in valeurs.items():
        spec = SPECS.get(cle)
        if spec is None:
            erreurs[cle] = "Réglage inconnu."
            continue
        if spec["type"] == "secret" and not str(brut or "").strip():
            continue
        try:
            propres[cle] = _valider_un(cle, brut, spec)
        except ValueError as erreur:
            erreurs[cle] = str(erreur)

    fusion = {**(existant or {}), **propres}
    if "pause_envoi_min_s" not in erreurs and "pause_envoi_max_s" not in erreurs:
        try:
            if int(fusion["pause_envoi_min_s"]) > int(fusion["pause_envoi_max_s"]):
                erreurs["pause_envoi_max_s"] = "Doit être supérieure ou égale à la pause minimale."
        except (KeyError, ValueError):
            pass
    if erreurs:
        raise ErreurValidation(erreurs)
    return propres


def valeur_liste_noire_valide(valeur):
    """Renvoie la valeur normalisée (email ou domaine), ou None si elle n'a l'air d'aucun des deux."""
    normalisee = db.normaliser_valeur_liste_noire(valeur)
    if normalisee and (_RE_EMAIL.match(normalisee) or _RE_DOMAINE.match(normalisee)):
        return normalisee
    return None


def valeurs_effectives(conn, surcharges=None):
    """Tous les réglages stockés, éventuellement surchargés par des valeurs non encore enregistrées.

    Sert aux boutons « Tester la connexion » : on teste ce qui est dans le formulaire,
    et un champ de clé laissé vide retombe sur la clé enregistrée.
    """
    cfg = {l["cle"]: l["valeur"] or "" for l in conn.execute("SELECT cle, valeur FROM reglages")}
    for cle, valeur in (surcharges or {}).items():
        spec = SPECS.get(cle)
        if spec is None:
            continue
        if spec["type"] == "secret" and not str(valeur or "").strip():
            continue
        cfg[cle] = str(valeur or "").strip() if spec["type"] not in ("liste", "regles_osm", "liste_entiers") else cfg.get(cle, "")
    return cfg


def lire_pour_ui(conn):
    """État des réglages pour l'écran. Une clé n'est jamais renvoyée : seulement son masque."""
    etat = {}
    for cle, spec in SPECS.items():
        brut = db.get_reglage(conn, cle, "")
        if spec["type"] == "secret":
            etat[cle] = {"secret": True, "definie": bool(brut), "masque": db.masquer_secret(brut)}
        elif spec["type"] in ("liste", "regles_osm", "liste_entiers"):
            etat[cle] = {"secret": False, "valeur": "\n".join(str(x) for x in json.loads(brut)) if brut else ""}
        else:
            etat[cle] = {"secret": False, "valeur": brut}
    return etat


def sauvegarder(conn, valeurs, effacer=()):
    """Valide puis enregistre, tout ou rien. Renvoie la liste des clés modifiées."""
    existant = {l["cle"]: l["valeur"] or "" for l in conn.execute("SELECT cle, valeur FROM reglages")}
    propres = valider(valeurs, existant)
    a_effacer = [c for c in effacer if SPECS.get(c, {}).get("type") == "secret"]
    if any(SPECS[c]["type"] == "secret" for c in propres) or a_effacer:
        db.verifier_gitignore()  # on n'écrit pas une clé si .gitignore ne protège plus data/
    for cle, valeur in propres.items():
        db.set_reglage(conn, cle, valeur)
    for cle in a_effacer:
        db.set_reglage(conn, cle, "")
    modifiees = sorted(set(propres) | set(a_effacer))
    journal.info("réglages modifiés : %s", ", ".join(modifiees) or "(aucun)")
    return modifiees


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with db.connexion() as conn:
        for cle, info in lire_pour_ui(conn).items():
            valeur = (info["masque"] or "(vide)") if info["secret"] else info["valeur"].replace("\n", " | ")
            print(f"{cle:26} {valeur}")


if __name__ == "__main__":
    main()
