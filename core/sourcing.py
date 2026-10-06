"""Sourcing des prospects : OpenStreetMap (Overpass), géocodage Nominatim, recherche d'emails.

Google Places n'est pas encore branché : seuls son test de connexion et le choix du fournisseur
existent. Le scan fonctionne avec OpenStreetMap.

Utilisable seul :
    python -m core.sourcing            fournisseur utilisé + test de la clé Google
    python -m core.sourcing check      tests sur une base jetable, sans réseau
    python -m core.sourcing scan ID    scanne une campagne en direct (appels réels)
"""
import json
import logging
import math
import re
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from core import db, http, reglages

journal = logging.getLogger("prospect")

URL_PLACES = "https://places.googleapis.com/v1/places:searchText"
URL_NOMINATIM = "https://nominatim.openstreetmap.org/search"
URLS_OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
UA_BASE = "agent-prospection/1.0"
_PAUSE_NOMINATIM_S = 1.1  # la politique d'usage de Nominatim impose 1 requête par seconde au maximum
_verrou_nominatim = threading.Lock()
_dernier_appel_nominatim = 0.0


class ErreurSourcing(Exception):
    """Échec d'un appel de sourcing : le message est affiché tel quel dans l'interface."""


class ErreurQuotaGoogle(ErreurSourcing):
    """Google refuse à cause d'une limite de requêtes de TON projet (par jour…) : inutile d'insister, le scan s'arrête."""


def _user_agent(cfg):
    contact = (cfg.get("expediteur_email") or "").strip() or "usage local"
    return f"{UA_BASE} ({contact})"


def fournisseur_effectif(cfg):
    """Fournisseur réellement utilisé selon le réglage et la présence de la clé Google.

    Renvoie (code, explication). Le basculement est signalé, jamais silencieux.
    """
    choix = cfg.get("sourcing_provider") or "auto"
    cle_presente = bool((cfg.get("google_places_key") or "").strip())
    if choix == "osm":
        return "osm", "OpenStreetMap (choisi dans les réglages)."
    if choix == "google":
        if cle_presente:
            return "google", "Google Places."
        return None, ("Google Places est demandé mais aucune clé n'est enregistrée : "
                      "renseigne-la, ou choisis « auto ».")
    if cle_presente:
        return "google", "Google Places (clé présente)."
    return "osm", "OpenStreetMap : aucune clé Google enregistrée, bascule automatique."


# --------------------------------------------------------------------------------------
# Géocodage (Nominatim)
# --------------------------------------------------------------------------------------


def geocoder(cfg, ville, pays):
    """Coordonnées d'une ville via Nominatim (OpenStreetMap).

    Renvoie {'statut': 'ok', 'lat', 'lon', 'libelle'} | {'statut': 'introuvable'}
    | {'statut': 'erreur', 'message'}. « Introuvable » et « erreur réseau » sont distincts.
    """
    global _dernier_appel_nominatim
    timeout = int(cfg["http_timeout_s"])
    with _verrou_nominatim:
        attente = _PAUSE_NOMINATIM_S - (time.monotonic() - _dernier_appel_nominatim)
        if attente > 0:
            time.sleep(attente)
        try:
            reponse = http.requete(
                "GET", URL_NOMINATIM, timeout=timeout,
                headers={"User-Agent": _user_agent(cfg)},
                params={"q": ville, "countrycodes": pays.lower(), "format": "jsonv2", "limit": 1},
            )
        except requests.RequestException as erreur:
            return {"statut": "erreur", "message": http.message_erreur_reseau(erreur, timeout)}
        finally:
            _dernier_appel_nominatim = time.monotonic()
    if not reponse.ok:
        return {"statut": "erreur",
                "message": f"Nominatim refuse la requête (HTTP {reponse.status_code}) : "
                           f"{http.resume_erreur(reponse)}"}
    try:
        resultats = reponse.json()
        if not resultats:
            return {"statut": "introuvable"}
        premier = resultats[0]
        return {"statut": "ok", "lat": float(premier["lat"]), "lon": float(premier["lon"]),
                "libelle": premier.get("display_name", ville)}
    except (ValueError, KeyError, TypeError, IndexError):
        return {"statut": "erreur", "message": f"Réponse Nominatim illisible : {reponse.text[:200]}"}


# --------------------------------------------------------------------------------------
# OpenStreetMap (Overpass) : recherche d'établissements
# --------------------------------------------------------------------------------------


def _sans_accents(texte):
    decompose = unicodedata.normalize("NFKD", texte or "")
    return "".join(c for c in decompose if not unicodedata.combining(c)).lower()


def parser_regles_osm(lignes):
    """['dentiste|dentaire = amenity=dentist, nom', ...] -> [(mots-clés, étiquettes, avec_nom)]."""
    regles = []
    for ligne in lignes:
        mots, _, reste = ligne.partition("=")
        etiquettes = [e.strip() for e in reste.split(",") if e.strip()]
        regles.append((
            [_sans_accents(m).strip() for m in mots.split("|") if m.strip()],
            [e for e in etiquettes if e != "nom"],
            "nom" in etiquettes,
        ))
    return regles


def _clauses_etiquette(etiquette, autour):
    """Une clause d'égalité par valeur (« cle=a|b » -> deux clauses).

    Mesuré sur le serveur public : les égalités répondent, alors qu'une regex sur les valeurs ou sur le nom échoue
    (504, délai dépassé) dès que le service est chargé.
    """
    cle, _, valeurs = etiquette.partition("=")
    return [f'  nwr["{cle}"="{valeur}"]["name"]{autour};' for valeur in valeurs.split("|") if valeur]


def _echapper_regex_overpass(texte):
    motif = re.escape(texte).replace("\\ ", " ")
    return motif.replace("\\", "\\\\").replace('"', '\\"')


def construire_requete_overpass(requete, regles, lat, lon, rayon_km, timeout_s, partie=None):
    """Requête Overpass QL pour une requête de campagne autour d'un point.

    Une règle dont un mot-clé figure dans la requête fournit les étiquettes OSM ; « nom » ajoute
    une recherche par nom. Sans règle correspondante, la requête est cherchée par nom.
    `partie` : None = tout dans une seule requête ; « etiquettes » ou « nom » = seulement ce volet
    (None est renvoyé s'il est vide). Les deux volets sont posés séparément : la recherche par nom est la
    plus lourde, son échec ne doit pas faire perdre les résultats par étiquettes.
    """
    normalisee = _sans_accents(requete)
    etiquettes, par_nom, correspondance = [], False, False
    for mots, tags, avec_nom in regles:
        if any(m and m in normalisee for m in mots):
            correspondance = True
            etiquettes += [t for t in tags if t not in etiquettes]
            par_nom = par_nom or avec_nom
    if not correspondance:
        par_nom = True
    autour = f"(around:{int(rayon_km * 1000)},{lat},{lon})"
    par_etiquettes = list(dict.fromkeys(c for t in etiquettes for c in _clauses_etiquette(t, autour)))
    clauses_nom = [f'  nwr["name"~"{_echapper_regex_overpass(requete)}",i]{autour};'] if par_nom else []
    clauses = {"etiquettes": par_etiquettes, "nom": clauses_nom}.get(partie) if partie else par_etiquettes + clauses_nom
    if not clauses:
        return None
    return f"[out:json][timeout:{int(timeout_s)}];\n(\n" + "\n".join(clauses) + "\n);\nout center tags;"


def _premier_email(texte, exclus):
    for brut in re.split(r"[;,\s]+", texte or ""):
        email = db.normaliser_email(brut)
        if email and "@" in email and _email_acceptable(email, exclus):
            return email
    return None


def element_vers_prospect(element, ville_zone, exclus):
    """Établissement normalisé depuis un élément Overpass, ou None s'il n'a pas de nom."""
    tags = element.get("tags") or {}
    nom = (tags.get("name") or "").strip()
    if not nom:
        return None
    def adresse(cle):  # OSM utilise « addr:* » ou, selon les contributeurs, « contact:* »
        return tags.get("addr:" + cle) or tags.get("contact:" + cle)

    rue = " ".join(p for p in (adresse("housenumber"), adresse("street")) if p)
    commune = " ".join(p for p in (adresse("postcode"), adresse("city")) if p)
    site = (tags.get("website") or tags.get("contact:website") or tags.get("url") or "").strip()
    if site and "//" not in site:
        site = "https://" + site
    return {
        "nom": nom,
        "adresse": ", ".join(p for p in (rue, commune) if p) or None,
        "ville": (adresse("city") or ville_zone or "").strip() or None,
        "telephone": (tags.get("phone") or tags.get("contact:phone") or "").strip() or None,
        "site": site or None,
        "email": _premier_email(tags.get("email") or tags.get("contact:email"), exclus),
        "source": "openstreetmap",
        "source_id": f"osm:{element.get('type')}/{element.get('id')}",
        "note_moyenne": None,   # OpenStreetMap n'a ni note ni avis : « inconnu », pas « 0 »
        "nb_avis": None,
    }


def _poser_sur_serveur(cfg, url, requete_ql, ville_zone, essais):
    """Une requête Overpass sur un serveur. Renvoie (établissements, None, False) si elle a abouti, sinon
    (None, message, rejet_rapide). `rejet_rapide` : le serveur a répondu 429/5xx (saturé), il vaut la peine de le
    réessayer plus tard ; un délai dépassé ou un serveur injoignable ne se réessaie pas (ça coûterait encore des minutes).
    """
    timeout = int(cfg["timeout_overpass_s"])
    exclus = json.loads(cfg.get("emails_exclus") or "[]")
    message, rejet = "aucune réponse", False
    for essai in range(essais):
        try:
            reponse = http.requete("POST", url, timeout=timeout + 15, data={"data": requete_ql},
                                   headers={"User-Agent": _user_agent(cfg)})
        except requests.RequestException as erreur:
            return None, http.message_erreur_reseau(erreur, timeout + 15), False
        if reponse.status_code == 429 or reponse.status_code >= 500:
            message, rejet = f"Service OpenStreetMap saturé ou en panne (HTTP {reponse.status_code}).", True
            if essai + 1 < essais:
                time.sleep(10)
            continue
        if not reponse.ok:
            raise ErreurSourcing(f"OpenStreetMap refuse la requête (HTTP {reponse.status_code}) : "
                                 f"{http.resume_erreur(reponse)}")
        try:
            donnees = reponse.json()
        except ValueError:
            return None, f"Réponse illisible : {reponse.text[:200]}", False
        if "error" in (donnees.get("remark") or "").lower():
            return None, f"OpenStreetMap a interrompu la requête : {donnees['remark'][:200]}", False
        vus, trouves = set(), []
        for element in donnees.get("elements", []):
            etab = element_vers_prospect(element, ville_zone, exclus)
            if etab and etab["source_id"] not in vus:
                vus.add(etab["source_id"])
                trouves.append(etab)
        return trouves, None, False
    return None, message, rejet


def _interroger_overpass(cfg, requete_ql, ville_zone, au_mieux=False):
    """Envoie une requête Overpass et renvoie les établissements. Lève ErreurSourcing.

    Chaque serveur est essayé à son tour (deux essais s'il est saturé). Si aucun n'a répondu et que certains ont
    seulement rejeté la requête, une seconde passe les réessaie après `overpass_nouvelle_tentative_s` secondes
    (0 = jamais). `au_mieux` (volet secondaire, plus lourd) : un seul serveur, un seul essai, pas de seconde passe,
    pour ne pas immobiliser le scan quand le service est chargé.
    """
    serveurs = json.loads(cfg.get("overpass_urls") or "[]") or URLS_OVERPASS
    if au_mieux:
        serveurs = serveurs[:1]
    attente = 0 if au_mieux else int(cfg.get("overpass_nouvelle_tentative_s") or 0)
    erreurs, a_essayer = {}, serveurs
    for passe in range(2 if attente else 1):
        if passe:
            journal.info("OpenStreetMap saturé : nouvelle tentative dans %s s", attente)
            time.sleep(attente)
        reessayables = []
        for url in a_essayer:
            trouves, message, rejet = _poser_sur_serveur(cfg, url, requete_ql, ville_zone, 1 if au_mieux or passe else 2)
            if trouves is not None:
                return trouves
            erreurs[url] = message
            if rejet:
                reessayables.append(url)
        a_essayer = reessayables
        if not a_essayer:
            break
    if len(erreurs) == 1:
        raise ErreurSourcing(next(iter(erreurs.values())))
    raise ErreurSourcing("Aucun serveur OpenStreetMap n'a répondu — "
                         + " ; ".join(f"{urlsplit(u).hostname} : {m}" for u, m in erreurs.items()))


def _interroger_une_fois(cfg, requete_ql, ville_zone, memo, au_mieux=False):
    """Pose la requête Overpass, sauf si la même a déjà été posée pendant ce scan de zone (réponse réutilisée).

    `memo` = {"appels": nombre d'appels réseau déjà faits, "reponses": {requête QL: établissements}}.
    Une pause (réglage `pause_requetes_s`) précède chaque appel réel sauf le premier : le service public est partagé.
    """
    if requete_ql in memo["reponses"]:
        return memo["reponses"][requete_ql]
    if memo["appels"]:
        time.sleep(int(cfg["pause_requetes_s"]))
    memo["appels"] += 1
    trouves = _interroger_overpass(cfg, requete_ql, ville_zone, au_mieux)
    memo["reponses"][requete_ql] = trouves
    return trouves


def chercher_osm(cfg, requete, regles, lat, lon, rayon_km, ville_zone, avertissements=None, memo=None):
    """Établissements OpenStreetMap pour une requête autour d'un point. Lève ErreurSourcing.

    Deux volets, posés séparément : les étiquettes (fiable, obligatoire) puis la recherche par nom (lourde, « au
    mieux »). Si seule la recherche par nom échoue, les résultats par étiquettes sont conservés et l'échec est
    signalé dans `avertissements` ; il n'est fatal que s'il n'y a rien d'autre à chercher.
    `memo` (voir _interroger_une_fois) évite de reposer une requête identique pour deux requêtes de campagne.
    """
    memo = memo if memo is not None else {"appels": 0, "reponses": {}}
    timeout = int(cfg["timeout_overpass_s"])
    ql_etiquettes = construire_requete_overpass(requete, regles, lat, lon, rayon_km, timeout, "etiquettes")
    ql_nom = construire_requete_overpass(requete, regles, lat, lon, rayon_km, timeout, "nom")
    trouves = list(_interroger_une_fois(cfg, ql_etiquettes, ville_zone, memo)) if ql_etiquettes else []  # copie : le memo reste intact
    if ql_nom:
        try:
            par_nom = _interroger_une_fois(cfg, ql_nom, ville_zone, memo, au_mieux=bool(ql_etiquettes))
        except ErreurSourcing as erreur:
            if not ql_etiquettes:
                raise
            journal.warning("recherche par nom « %s » abandonnée à %s : %s", requete, ville_zone, erreur)
            if avertissements is not None:
                avertissements.append(f"recherche par nom « {requete} » abandonnée à {ville_zone} ({erreur})")
            par_nom = []
        connus = {e["source_id"] for e in trouves}
        trouves += [e for e in par_nom if e["source_id"] not in connus]
    return trouves[:int(cfg["plafond_resultats_scan"])]


# --------------------------------------------------------------------------------------
# Google Places (fournisseur principal, nécessite une clé)
# --------------------------------------------------------------------------------------

_CHAMPS_PLACES = ("places.id,places.displayName,places.formattedAddress,places.websiteUri,"
                  "places.nationalPhoneNumber,places.rating,places.userRatingCount,places.location,nextPageToken")
_PAGES_PLACES_MAX = 3  # l'API Places s'arrête à 60 résultats (3 pages de 20)


def _distance_km(lat1, lon1, lat2, lon2):
    """Distance à vol d'oiseau (formule de haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _cle_compteur_google(maintenant=None):
    # Le palier gratuit de Google se remet à zéro le 1er du mois, à minuit heure du Pacifique.
    return "google_requetes_" + (maintenant or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m")


def requetes_google_du_mois(conn=None):
    """Nombre de requêtes Google Places déjà faites ce mois-ci (compteur tenu en base, il survit aux redémarrages)."""
    if conn is None:
        with db.connexion() as c:
            return requetes_google_du_mois(c)
    return int(db.get_reglage(conn, _cle_compteur_google(), "0"))


def _conseil_google(detail, cle):
    """Que faire quand Google refuse la requête : son message brut (« API key not valid ») ne dit pas comment réparer."""
    bas = (detail or "").lower()
    cle = cle or ""
    if "api key not valid" in bas or "api_key_invalid" in bas:
        ici = f" Ici : {len(cle)} caractères, commence par « {cle[:4]} »." if cle else ""
        return ("Google ne reconnaît pas cette clé. Une clé Google Cloud commence par « AIza » et compte 39 caractères." + ici
                + " Crée-la sur console.cloud.google.com (APIs et services > Identifiants > Créer des identifiants > Clé API) "
                "et copie-la en entier, sans espace. Une clé obtenue sur un autre site (autre fournisseur, autre service Google) "
                "ne marche pas ici, et une clé toute neuve peut mettre quelques minutes à devenir active.")
    if "has not been used" in bas or "is disabled" in bas or "service_disabled" in bas:
        return ("L'API « Places API (New) » n'est pas activée sur ce projet Google : console.cloud.google.com > "
                "APIs et services > Bibliothèque > Places API (New) > Activer.")
    if "billing" in bas:
        return "La facturation n'est pas activée sur le projet Google (une carte est exigée, même pour le palier gratuit)."
    if "api_key_service_blocked" in bas or "are blocked" in bas:
        return "La clé est restreinte à d'autres API : autorise « Places API (New) » dans ses restrictions."
    if "referer" in bas or "ip address" in bas or "ip_address" in bas:
        return "La clé est restreinte par site web ou par adresse IP : retire cette restriction (l'application tourne sur ton ordinateur)."
    return ""


def _refus_google(statut, reponse, cle):
    """Message d'un refus de Google : le texte réel de Google, puis le conseil s'il y en a un."""
    detail = http.resume_erreur(reponse)
    conseil = _conseil_google(detail, cle)
    return f"Google refuse la requête (HTTP {statut}) : {detail}" + (f" — {conseil}" if conseil else "")


def _rembourser_requete_google():
    """Annule le comptage d'une requête que Google a refusée (erreur 4xx) : une requête refusée n'est pas facturée."""
    with db.connexion() as conn:
        fait = requetes_google_du_mois(conn)
        db.set_reglage(conn, _cle_compteur_google(), str(max(0, fait - 1)))


def _message_quota_google(reponse):
    return ("Google a atteint la limite de requêtes de ton projet (" + http.resume_erreur(reponse)[:230] + "). Cette limite se règle dans "
            "console.cloud.google.com > APIs et services > Places API (New) > Quotas ; une limite « par jour » repart à zéro à "
            "minuit, heure du Pacifique (9 h en France), ou tu peux la relever. Rien n'est perdu : relance le scan ensuite.")


def _ville_depuis_adresse(adresse):
    """Commune d'une adresse « 12 rue X, 33000 Bordeaux, France » (code postal + commune après une virgule), sinon None."""
    m = re.search(r",\s*\d{4,5}\s+([^,]+)", adresse or "")
    return m.group(1).strip() if m else None


def _compter_requete_google(cfg):
    """Refuse l'appel si le plafond mensuel est atteint ; sinon compte la requête AVANT de la faire (par prudence)."""
    maximum = int(cfg.get("google_requetes_mois_max") or 0)
    with db.connexion() as conn:
        fait = requetes_google_du_mois(conn)
        if maximum and fait >= maximum:
            raise ErreurSourcing(f"Plafond mensuel Google Places atteint ({fait}/{maximum} requêtes ce mois-ci) : "
                                 "l'application n'appelle plus Google pour ne jamais dépasser le palier gratuit. "
                                 "Tu peux relever ce plafond dans Réglages.")
        db.set_reglage(conn, _cle_compteur_google(), str(fait + 1))


def chercher_google(cfg, requete, lat, lon, rayon_km, ville_zone, pays, langue):
    """Établissements Google Places pour une requête autour d'un point. Lève ErreurSourcing.

    Le biais géographique est un cercle (coordonnées de la ville + rayon), pas du texte libre ; les résultats
    hors du rayon sont écartés. Les champs demandés sont listés explicitement pour maîtriser le coût.
    """
    timeout = int(cfg["http_timeout_s"])
    plafond = int(cfg["plafond_resultats_scan"])
    corps = {"textQuery": requete, "pageSize": 20, "regionCode": pays.upper(), "languageCode": langue.lower(),
             "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lon},
                                         "radius": min(float(rayon_km) * 1000, 50000.0)}}}
    en_tetes = {"X-Goog-Api-Key": cfg["google_places_key"].strip(), "X-Goog-FieldMask": _CHAMPS_PLACES,
                "Content-Type": "application/json"}
    trouves, vus, jeton = [], set(), None
    for page in range(_PAGES_PLACES_MAX):
        if jeton:
            corps["pageToken"] = jeton
        for essai in range(2):  # saturation (429, 5xx) : une nouvelle tentative après une pause
            _compter_requete_google(cfg)
            try:
                reponse = http.requete("POST", URL_PLACES, timeout=timeout, headers=en_tetes, json=corps)
            except requests.RequestException as erreur:
                raise ErreurSourcing(http.message_erreur_reseau(erreur, timeout)) from erreur
            if not reponse.ok and reponse.status_code < 500:
                _rembourser_requete_google()  # refusée : pas facturée, ne doit pas entamer le plafond mensuel
            if reponse.status_code == 429 and "per day" in (reponse.text or "").lower():
                raise ErreurQuotaGoogle(_message_quota_google(reponse))  # la limite du jour est atteinte : aucune nouvelle tentative
            if (reponse.status_code == 429 or reponse.status_code >= 500) and essai == 0:
                time.sleep(5)
                continue
            break
        if not reponse.ok:
            raise ErreurSourcing(_refus_google(reponse.status_code, reponse, cfg["google_places_key"].strip()))
        try:
            donnees = reponse.json()
        except ValueError:
            raise ErreurSourcing(f"Réponse Google Places illisible : {reponse.text[:200]}") from None
        for lieu in donnees.get("places", []):
            identifiant = lieu.get("id")
            nom = ((lieu.get("displayName") or {}).get("text") or "").strip()
            position = lieu.get("location") or {}
            if not identifiant or not nom or identifiant in vus:
                continue
            if "latitude" in position and _distance_km(lat, lon, position["latitude"], position["longitude"]) > float(rayon_km):
                continue  # le biais n'est pas une restriction : on écarte ce qui sort du rayon demandé
            vus.add(identifiant)
            site = (lieu.get("websiteUri") or "").strip()
            trouves.append({
                "nom": nom, "adresse": lieu.get("formattedAddress") or None,
                "ville": _ville_depuis_adresse(lieu.get("formattedAddress")) or ville_zone,
                "telephone": lieu.get("nationalPhoneNumber") or None, "site": site or None, "email": None,
                "source": "google_places", "source_id": f"google:{identifiant}",
                "note_moyenne": lieu.get("rating"), "nb_avis": lieu.get("userRatingCount", 0),
            })
        jeton = donnees.get("nextPageToken")
        if not jeton or len(trouves) >= plafond:
            break
        time.sleep(int(cfg["pause_requetes_s"]))
    return trouves[:plafond]


# --------------------------------------------------------------------------------------
# Recherche d'emails sur les sites
# --------------------------------------------------------------------------------------

_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}")
_EXTENSIONS_FAUSSES = {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "css", "js", "woff",
                       "woff2", "ico", "pdf"}
_TAILLE_MAX_PAGE = 1_500_000


def _email_acceptable(email, exclus):
    """Écarte les fausses adresses : images (« logo@2x.png »), plateformes, no-reply, exemples."""
    if len(email) > 120 or email.rsplit(".", 1)[-1] in _EXTENSIONS_FAUSSES:
        return False
    if re.fullmatch(r"x{2,}", email.split("@", 1)[0]):  # « xxx@gmail.com » : adresse bouchon d'un gabarit de site
        return False
    return not any(f.lower() in email for f in exclus)


def _meme_domaine(domaine_email, domaine_site):
    if not domaine_email or not domaine_site:
        return False
    return (domaine_email == domaine_site or domaine_email.endswith("." + domaine_site)
            or domaine_site.endswith("." + domaine_email))


def extraire_email(html, domaine_site, exclus, domaines_generiques):
    """Meilleure adresse d'une page, ou None.

    Les liens mailto: priment sur le texte brut. Une adresse n'est retenue que si elle est sur le
    domaine du site, ou chez un fournisseur grand public (gmail, orange…) : une adresse d'un autre
    domaine est presque toujours celle de l'agence web ou d'un partenaire, pas celle du prospect.
    """
    soupe = BeautifulSoup(html, "html.parser")
    candidats = []  # (email, vient_d_un_mailto)
    for lien in soupe.find_all("a", href=True):
        if lien["href"].lower().startswith("mailto:"):
            email = db.normaliser_email(lien["href"])
            if email:
                candidats.append((email, True))
    for trouve in _RE_EMAIL.findall(soupe.get_text(" ")):
        email = db.normaliser_email(trouve)
        if email:
            candidats.append((email, False))
    generiques = {d.lower() for d in domaines_generiques}
    retenus = []
    for email, mailto in candidats:
        if "@" not in email or not _email_acceptable(email, exclus):
            continue
        domaine = email.rsplit("@", 1)[1]
        if _meme_domaine(domaine, domaine_site):
            rang = 0 if mailto else 1
        elif domaine in generiques:
            rang = 2 if mailto else 3
        else:
            continue
        retenus.append((rang, email))
    return min(retenus)[1] if retenus else None


class _Robots:
    """Respect de robots.txt, avec un cache par site."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.cache = {}

    def autorise(self, url):
        morceaux = urlsplit(url)
        origine = f"{morceaux.scheme}://{morceaux.netloc}"
        if origine not in self.cache:
            self.cache[origine] = self._charger(origine)
        regles = self.cache[origine]
        return regles is not None and regles.can_fetch(UA_BASE, url)

    def _charger(self, origine):
        timeout = int(self.cfg["http_timeout_s"])
        regles = urllib.robotparser.RobotFileParser()
        try:
            reponse = http.requete("GET", origine + "/robots.txt", timeout=timeout,
                                   headers={"User-Agent": _user_agent(self.cfg)})
        except requests.RequestException:
            return None  # robots.txt inaccessible : par prudence, on ne visite pas le site
        if reponse.status_code >= 500 or reponse.status_code in (401, 403):
            return None
        if reponse.status_code >= 400:
            regles.parse([])  # pas de robots.txt : tout est permis
        else:
            regles.parse(reponse.text.splitlines())
        return regles


def _telecharger(cfg, url):
    """Page HTML d'un site. Renvoie (html, url_finale) ou (None, raison)."""
    timeout = int(cfg["http_timeout_s"])
    en_tetes = {"User-Agent": _user_agent(cfg), "Accept": "text/html,application/xhtml+xml"}
    try:
        reponse = http.requete("GET", url, timeout=timeout, headers=en_tetes, stream=True)
    except requests.exceptions.SSLError:
        if not url.startswith("https://"):
            return None, "certificat SSL invalide"
        return _telecharger(cfg, "http://" + url[len("https://"):])
    except requests.RequestException as erreur:
        return None, http.message_erreur_reseau(erreur, timeout)
    try:
        if reponse.status_code >= 400:
            return None, f"HTTP {reponse.status_code}"
        if "html" not in (reponse.headers.get("Content-Type") or "").lower():
            return None, "page non HTML"
        contenu = b""
        for morceau in reponse.iter_content(65536):
            contenu += morceau
            if len(contenu) > _TAILLE_MAX_PAGE:
                break
        return contenu.decode(reponse.encoding or "utf-8", errors="replace"), reponse.url
    finally:
        reponse.close()


def detecter_formulaire(html, domaines_formulaires=()):
    """True si la page porte un formulaire de contact : un champ de message libre (textarea), ou un formulaire
    hébergé ailleurs (Typeform, Google Forms…, liste des réglages). Une recherche, une connexion, les commentaires
    d'un blog ou une simple lettre d'information (un champ email, pas de message) n'en sont pas."""
    soupe = BeautifulSoup(html, "html.parser")
    for f in soupe.find_all("form"):
        if (f.get("role") or "").lower() == "search" or f.find("input", attrs={"type": "search"}) is not None \
                or f.find("input", attrs={"type": "password"}) is not None:
            continue
        if (f.get("id") or "") == "commentform" or "wp-comments-post" in (f.get("action") or ""):
            continue
        if f.find("textarea") is not None:
            return True
    hotes = [d.lower() for d in domaines_formulaires if d]
    return any(h in cadre["src"].lower() for cadre in soupe.find_all("iframe", src=True) for h in hotes)


def liens_contact(html, base, mots_cles, maximum):
    """Pages du même site dont le lien parle de contact (texte ou adresse), pour les sites dont la page de contact n'est
    pas à « /contact » (« /infos-pratiques-contact/ », « /contact-6 »…). Accents et casse ignorés."""
    if maximum <= 0:
        return []
    soupe = BeautifulSoup(html, "html.parser")
    origine = urlsplit(base).netloc.lower().removeprefix("www.")
    mots = [_sans_accents(m) for m in mots_cles if m]
    trouves = []
    for lien in soupe.find_all("a", href=True):
        href = lien["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base, href).split("#")[0]
        morceaux = urlsplit(url)
        if morceaux.scheme not in ("http", "https") or morceaux.netloc.lower().removeprefix("www.") != origine:
            continue
        if any(m in _sans_accents(lien.get_text(" ") + " " + morceaux.path) for m in mots) and url not in trouves:
            trouves.append(url)
            if len(trouves) >= maximum:
                break
    return trouves


def trouver_contact(cfg, site, robots):
    """Cherche un moyen de contacter le prospect : un email d'abord (page d'accueil, puis pages de contact), à défaut
    la page d'un formulaire de contact. Renvoie (email, formulaire_url, nb_pages) ; au plus l'un des deux."""
    domaine_site = db.extraire_domaine(site)
    exclus = json.loads(cfg.get("emails_exclus") or "[]")
    generiques = json.loads(cfg.get("domaines_generiques") or "[]")
    hotes_formulaires = json.loads(cfg.get("domaines_formulaires") or "[]")
    chemins = json.loads(cfg.get("pages_contact") or "[]")
    pause = int(cfg["pause_pages_s"])
    accueil = site if "//" in site else "https://" + site
    urls, origine, pages, formulaire = [accueil], None, 0, None
    for url in urls:
        if not robots.autorise(url):
            if pages == 0:
                return None, None, 0  # robots.txt interdit (ou inaccessible) : on ne visite pas ce site
            continue
        if pages:
            time.sleep(pause)
        html, finale = _telecharger(cfg, url)
        pages += 1
        if html is None:
            if pages == 1:
                return None, None, pages  # site mort : inutile d'essayer les autres pages
            continue
        if origine is None:
            morceaux = urlsplit(finale)
            origine = f"{morceaux.scheme}://{morceaux.netloc}"
            decouverts = liens_contact(html, finale, json.loads(cfg.get("mots_cles_contact") or "[]"),
                                       int(cfg.get("contact_liens_max") or 0))
            urls += [u for u in decouverts + [urljoin(origine, chemin) for chemin in chemins] if u not in urls]
        email = extraire_email(html, domaine_site, exclus, generiques)
        if email:
            return email, None, pages
        if formulaire is None and detecter_formulaire(html, hotes_formulaires):
            formulaire = finale
    return None, formulaire, pages


def trouver_email(cfg, site, robots):
    """Cherche un email sur la page d'accueil puis sur les pages de contact. Renvoie (email, nb_pages)."""
    email, _formulaire, pages = trouver_contact(cfg, site, robots)
    return email, pages


def completer_contacts(job_id, campagne_id, ids=None, base=0.0, part=100.0):
    """Cherche un email, sinon un formulaire de contact, pour les prospects qui ont un site propre mais aucun moyen de
    contact connu et dont le site n'a jamais été exploré (`contact_cherche_le`). Sert aux prospects importés ou trouvés
    avant l'existence de cette recherche ; un scan le fait déjà pour ses nouveaux prospects.
    Renvoie {'visites', 'emails', 'formulaires'}."""
    from core import jobs

    with db.connexion() as conn:
        cfg = reglages.valeurs_effectives(conn)
        filtre = f" AND id IN ({','.join('?' * len(ids))})" if ids else ""
        lignes = [dict(l) for l in conn.execute(
            "SELECT id, site FROM prospects WHERE campagne_id = ? AND email IS NULL AND site IS NOT NULL AND TRIM(site) <> '' "
            f"AND contact_cherche_le IS NULL AND statut <> 'ignore'{filtre} ORDER BY score DESC, id",
            (campagne_id, *(ids or [])))]
    plateformes = json.loads(cfg.get("domaines_plateformes") or "[]")
    lignes = [l for l in lignes if not db.est_plateforme(db.extraire_domaine(l["site"]), plateformes)]
    robots = _Robots(cfg)
    bilan = {"visites": len(lignes), "emails": 0, "formulaires": 0}
    for k, l in enumerate(lignes):
        jobs.maj(job_id, base + part * k / max(len(lignes), 1), f"Recherche d'un contact {k + 1}/{len(lignes)}")
        email, formulaire, _pages = trouver_contact(cfg, l["site"], robots)
        with db.connexion() as conn:
            conn.execute("UPDATE prospects SET email = COALESCE(email, ?), formulaire_url = ?, contact_cherche_le = ? WHERE id = ?",
                         (email, formulaire, _maintenant(), l["id"]))
        bilan["emails"] += bool(email)
        bilan["formulaires"] += bool(formulaire)
        if k + 1 < len(lignes):
            time.sleep(int(cfg["pause_pages_s"]))
    return bilan


def chercher_contacts(job_id, campagne_id):
    """Tâche de fond : complète les moyens de contact manquants d'une campagne (voir completer_contacts)."""
    bilan = completer_contacts(job_id, campagne_id)
    if not bilan["visites"]:
        return {"statut": "termine", "detail": "Rien à chercher : tous les sites avec un site propre ont déjà été explorés."}
    return {"statut": "termine", "detail": f"{bilan['visites']} site(s) exploré(s) — {bilan['emails']} email(s) trouvé(s), "
                                           f"{bilan['formulaires']} formulaire(s) de contact repéré(s)"}


# --------------------------------------------------------------------------------------
# Scan d'une campagne (exécuté par le fil de travail de core.jobs)
# --------------------------------------------------------------------------------------


def _maintenant():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resumer_echecs(echecs):
    """[(zone, message)] -> « ÉCHEC : … ». Une même cause sur plusieurs zones n'est écrite qu'une fois
    (29 zones en échec pour la même raison ne font pas 29 phrases)."""
    par_message = {}
    for ville, message in echecs:
        par_message.setdefault(message, []).append(ville)
    morceaux = []
    for message, villes in par_message.items():
        if len(villes) == 1:
            morceaux.append(f"{villes[0]} ({message})")
        else:
            liste = ", ".join(villes[:5]) + (f" et {len(villes) - 5} autre(s)" if len(villes) > 5 else "")
            morceaux.append(f"{len(villes)} zones ({liste}) : {message}")
    return "ÉCHEC : " + " ; ".join(morceaux)


def scanner(job_id, campagne_id, zone_ids=None):
    """Scanne les zones d'une campagne à la suite et consolide les prospects.

    Renvoie {'statut', 'detail'} pour core.jobs. Une zone qui échoue n'arrête pas les autres.
    """
    from core import jobs

    with db.connexion() as conn:
        cfg = reglages.valeurs_effectives(conn)
        campagne = conn.execute("SELECT * FROM campagnes WHERE id = ?", (campagne_id,)).fetchone()
        if campagne is None:
            return {"statut": "erreur", "detail": "Campagne introuvable."}
        if zone_ids:
            marques = ",".join("?" * len(zone_ids))
            lignes = conn.execute(f"SELECT * FROM zones WHERE campagne_id = ? AND id IN ({marques}) ORDER BY id",
                                  (campagne_id, *zone_ids)).fetchall()
        else:
            lignes = conn.execute("SELECT * FROM zones WHERE campagne_id = ? ORDER BY id", (campagne_id,)).fetchall()
        zones = [dict(z) for z in lignes]
        requetes = json.loads(campagne["requetes"])
        regles = parser_regles_osm(json.loads(cfg.get("osm_correspondances") or "[]"))

    code, explication = fournisseur_effectif(cfg)
    if code is None:
        return {"statut": "erreur", "detail": explication}
    if not requetes:
        return {"statut": "erreur", "detail": "Cette campagne n'a aucune requête de recherche."}
    if not zones:
        return {"statut": "erreur", "detail": "Aucune zone à scanner."}

    with db.connexion() as conn:
        conn.execute("UPDATE campagnes SET statut = 'active' WHERE id = ? AND statut = 'brouillon'", (campagne_id,))

    robots = _Robots(cfg)
    total = {"nouveaux": 0, "ignores": 0, "autres": 0, "emails": 0, "formulaires": 0, "sites": 0, "motifs": {}}
    echecs, sautees, secours, avertissements = [], [], [], []
    non_scannees = 0
    contexte = {"fournisseur": code, "pays": campagne["pays"], "langue": campagne["langue"],
                "secours_osm": (cfg.get("sourcing_provider") or "auto") == "auto", "secours": secours,
                "avertissements": avertissements}
    nb = len(zones)

    for i, zone in enumerate(zones):
        base, part = i / nb * 100, 100 / nb
        etiquette = f"{zone['ville']} {i + 1}/{nb}"
        if zone["lat"] is None or zone["lon"] is None:
            sautees.append(zone["ville"])
            jobs.maj(job_id, base + part, f"{etiquette} — coordonnées manquantes, zone ignorée")
            continue
        with db.connexion() as conn:
            conn.execute("UPDATE zones SET statut = 'en_cours', nb_trouves = 0, nb_ignores = 0 WHERE id = ?",
                         (zone["id"],))
        try:
            trouves = _scanner_zone(job_id, cfg, regles, requetes, campagne_id, zone, etiquette,
                                    base, part, robots, total, contexte)
        except Exception as erreur:  # la zone échoue, le scan continue ; son état est restauré
            if not isinstance(erreur, ErreurSourcing):
                journal.exception("scan de la zone %s", zone["ville"])
            echecs.append((zone["ville"], str(erreur)))
            with db.connexion() as conn:  # le résultat du scan précédent de la zone est conservé
                conn.execute("UPDATE zones SET statut = ?, nb_trouves = ?, nb_ignores = ? WHERE id = ?",
                             (zone["statut"] if zone["statut"] != "en_cours" else "a_scanner",
                              zone["nb_trouves"], zone["nb_ignores"], zone["id"]))
            if isinstance(erreur, ErreurQuotaGoogle) and not contexte["secours_osm"]:
                non_scannees = nb - i - 1  # inutile d'insister zone après zone : les suivantes restent « à scanner »
                break
            continue
        with db.connexion() as conn:
            conn.execute("UPDATE zones SET statut = 'termine', dernier_scan = ?, nb_trouves = ? WHERE id = ?",
                         (_maintenant(), trouves, zone["id"]))

    reussies = nb - len(echecs) - len(sautees) - non_scannees
    morceaux = [f"{reussies}/{nb} zone(s) scannée(s)",
                f"{total['nouveaux']} nouveau(x) prospect(s)",
                f"{total['ignores']} ignoré(s), déjà en base" + _explication_ignores(total),
                f"{total['emails']} email(s) trouvé(s), {total['formulaires']} formulaire(s) de contact repéré(s), "
                f"{total['sites']} site(s) visité(s)"]
    morceaux.insert(1, "source : " + ("Google Places" if code == "google" else "OpenStreetMap")
                    + (f" (secours OpenStreetMap pour : {', '.join(secours)}, Google ayant échoué)" if secours else ""))
    if sautees:
        morceaux.append("sans coordonnées : " + ", ".join(sautees))
    if avertissements:
        morceaux.append("ATTENTION, résultats incomplets : " + " ; ".join(dict.fromkeys(avertissements)))
    if non_scannees:
        morceaux.append(f"ARRÊT : limite de requêtes Google atteinte, {non_scannees} zone(s) non scannée(s) (elles restent « à scanner »)")
    if echecs:
        morceaux.append(_resumer_echecs(echecs))
    if reussies > 0:  # enchaînement du cycle : les nouveaux prospects sont analysés et scorés dans la foulée
        from core import analyzer
        jobs.soumettre("analyse", campagne_id, analyzer.analyser_campagne, campagne_id, True)
        morceaux.append("analyse des sites lancée")
    return {"statut": "erreur" if reussies == 0 else "termine", "detail": " — ".join(morceaux)}


_MOTIFS_IGNORES = {"source_id": "même fiche que lors d'un scan précédent",
                   "nom_ville": "même nom dans la même ville", "domaine": "même site web"}


def _explication_ignores(total):
    """« (même nom dans la même ville : 3 ; …) » : pourquoi des fiches n'ont pas été ajoutées."""
    morceaux = [f"{_MOTIFS_IGNORES[m]} : {n}" for m, n in total["motifs"].items() if m in _MOTIFS_IGNORES]
    if total["autres"]:
        morceaux.append(f"dont {total['autres']} dans une autre campagne")
    return f" ({' ; '.join(morceaux)})" if morceaux else ""


def _candidats_de_la_zone(job_id, cfg, regles, requetes, zone, etiquette, base, part, fournisseur, contexte):
    """Établissements trouvés pour toutes les requêtes de la campagne dans une zone, via un fournisseur."""
    from core import jobs

    candidats, vus = [], set()
    memo = {"appels": 0, "reponses": {}}  # requêtes OSM identiques entre deux requêtes de campagne : posées une seule fois
    for q, requete in enumerate(requetes):
        jobs.maj(job_id, base + part * 0.3 * q / len(requetes),
                 f"{etiquette} — requête {q + 1}/{len(requetes)} : « {requete} »")
        if fournisseur == "google":
            if contexte.get("google_epuise"):
                raise ErreurQuotaGoogle("limite de requêtes Google déjà atteinte pendant ce scan")
            if q:
                time.sleep(int(cfg["pause_requetes_s"]))
            trouves = chercher_google(cfg, requete, zone["lat"], zone["lon"], zone["rayon_km"], zone["ville"],
                                      contexte["pays"], contexte["langue"])
        else:
            trouves = chercher_osm(cfg, requete, regles, zone["lat"], zone["lon"], zone["rayon_km"], zone["ville"],
                                   contexte["avertissements"], memo)
        for etab in trouves:
            if etab["source_id"] not in vus:  # même établissement trouvé par deux requêtes : une seule fois
                vus.add(etab["source_id"])
                candidats.append(etab)
    return candidats


def _scanner_zone(job_id, cfg, regles, requetes, campagne_id, zone, etiquette, base, part, robots, total, contexte):
    """Une zone : requêtes OSM, insertion sans doublon, puis emails des nouveaux prospects.

    Renvoie le nombre d'établissements trouvés (nouveaux + déjà en base) ; les déjà connus sont
    comptés à part dans zones.nb_ignores.
    """
    from core import jobs

    try:
        candidats = _candidats_de_la_zone(job_id, cfg, regles, requetes, zone, etiquette, base, part,
                                          contexte["fournisseur"], contexte)
    except ErreurSourcing as erreur:
        if isinstance(erreur, ErreurQuotaGoogle):
            contexte["google_epuise"] = True  # les zones suivantes n'appellent plus Google
        if contexte["fournisseur"] == "google" and contexte["secours_osm"]:
            # Mode automatique : Google en échec (quota, clé refusée…), OpenStreetMap prend le relais pour cette zone.
            journal.warning("Google Places en échec pour %s (%s) : bascule sur OpenStreetMap", zone["ville"], erreur)
            contexte["secours"].append(zone["ville"])
            candidats = _candidats_de_la_zone(job_id, cfg, regles, requetes, zone, etiquette, base, part, "osm", contexte)
        else:
            raise

    sans_email = []  # (id du prospect, site) : nouveaux prospects à qui il manque un email
    nouveaux = 0
    with db.connexion() as conn:
        for etab in candidats:
            doublon = db.trouver_doublon(conn, etab)
            ajoute = doublon is None and db.inserer_prospect(
                conn, {**etab, "campagne_id": campagne_id, "zone_id": zone["id"]})
            if ajoute:
                nouveaux += 1
                total["nouveaux"] += 1
                if etab["email"]:
                    total["emails"] += 1
                elif etab["site"] and not db.est_plateforme(
                        db.extraire_domaine(etab["site"]), json.loads(cfg.get("domaines_plateformes") or "[]")):
                    ligne = conn.execute("SELECT id FROM prospects WHERE source_id = ?", (etab["source_id"],)).fetchone()
                    sans_email.append((ligne["id"], etab["site"]))
                continue
            total["ignores"] += 1
            if doublon is not None:  # (sinon inserer_prospect a déjà compté ce conflit)
                total["motifs"][doublon["motif"]] = total["motifs"].get(doublon["motif"], 0) + 1
                db.compter_ignore(conn, zone["id"])
                if doublon["campagne_id"] != campagne_id:
                    total["autres"] += 1

    for k, (prospect_id, site) in enumerate(sans_email):
        jobs.maj(job_id, base + part * (0.3 + 0.7 * k / len(sans_email)),
                 f"{etiquette} — {nouveaux} nouveau(x), recherche d'emails {k + 1}/{len(sans_email)}")
        total["sites"] += 1
        email, formulaire, _pages = trouver_contact(cfg, site, robots)
        with db.connexion() as conn:
            conn.execute("UPDATE prospects SET email = ?, formulaire_url = ?, contact_cherche_le = ? WHERE id = ?",
                         (email, formulaire, _maintenant(), prospect_id))
        total["emails"] += bool(email)
        total["formulaires"] += bool(formulaire)
        if k + 1 < len(sans_email):
            time.sleep(int(cfg["pause_pages_s"]))
    jobs.maj(job_id, base + part, f"{etiquette} — terminée, {nouveaux} nouveau(x)")
    return len(candidats)


# --------------------------------------------------------------------------------------
# Test de connexion Google
# --------------------------------------------------------------------------------------


def tester_connexion(cfg):
    """Appel réel à l'API Places. Renvoie {'statut': ok|erreur|non_configure, 'message': ...}."""
    cle = (cfg.get("google_places_key") or "").strip()
    if not cle:
        return {"statut": "non_configure",
                "message": "Aucune clé Google Places. Sans clé, l'application utilise OpenStreetMap."}
    timeout = int(cfg["http_timeout_s"])
    try:
        # Seul le champ « id » est demandé : c'est le niveau de facturation le plus bas.
        reponse = http.requete(
            "POST", URL_PLACES, timeout=timeout,
            headers={"X-Goog-Api-Key": cle, "X-Goog-FieldMask": "places.id",
                     "Content-Type": "application/json"},
            json={"textQuery": "test", "pageSize": 1},
        )
    except requests.RequestException as erreur:
        return {"statut": "erreur", "message": http.message_erreur_reseau(erreur, timeout)}
    if reponse.ok:
        return {"statut": "ok", "message": "Clé valide : l'API Places répond."}
    return {"statut": "erreur", "message": _refus_google(reponse.status_code, reponse, cle)}


# --------------------------------------------------------------------------------------
# Auto-tests (python -m core.sourcing check) : réseau simulé, base jetable
# --------------------------------------------------------------------------------------


class _Reponse:
    """Réponse HTTP factice pour les tests."""

    def __init__(self, texte="", statut=200, type_contenu="text/html; charset=utf-8", url=""):
        self.text, self.status_code, self.url = texte, statut, url
        self.headers = {"Content-Type": type_contenu}
        self.encoding = "utf-8"
        self.ok = statut < 400

    def json(self):
        return json.loads(self.text)

    def iter_content(self, taille):
        yield self.text.encode("utf-8")

    def close(self):
        pass


def _autotests():
    import config
    from core import campagnes, jobs

    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    # --- Construction des requêtes Overpass et lecture des règles ---
    regles = parser_regles_osm([
        "dentiste|dentaire = amenity=dentist, healthcare=dentist",
        "esthetique|aesthetic = healthcare:speciality=plastic_surgery|dermatology, nom",
    ])
    q = construire_requete_overpass("Dentiste", regles, 44.84, -0.58, 15, 60)
    verifier("requête avec règle : étiquettes OSM, rayon en mètres, pas de recherche par nom",
             '["amenity"="dentist"]["name"](around:15000,44.84,-0.58)' in q and '["name"~' not in q)
    q = construire_requete_overpass("médecine esthétique", regles, 1, 2, 10, 60)
    verifier("règle insensible aux accents, étiquette multi-valeurs + recherche par nom ajoutée",
             '"healthcare:speciality"="plastic_surgery"' in q and '"healthcare:speciality"="dermatology"' in q
             and '["name"~"m' in q)
    verifier("valeurs multiples : une égalité par valeur, jamais de regex sur les valeurs (trop lourde pour le serveur)",
             '"~"^(' not in q and '"healthcare:speciality"~' not in q)
    q_etiq = construire_requete_overpass("médecine esthétique", regles, 1, 2, 10, 60, "etiquettes")
    q_nom = construire_requete_overpass("médecine esthétique", regles, 1, 2, 10, 60, "nom")
    verifier("volets séparés : étiquettes sans recherche par nom, nom sans étiquettes",
             '"healthcare:speciality"' in q_etiq and '["name"~' not in q_etiq
             and '["name"~"m' in q_nom and "healthcare" not in q_nom)
    verifier("volet vide : None (dentiste n'a pas de recherche par nom)",
             construire_requete_overpass("Dentiste", regles, 1, 2, 10, 60, "nom") is None)
    q = construire_requete_overpass('boulan"gerie (bio)', regles, 1, 2, 10, 60)
    verifier("sans règle : recherche par nom, guillemets et regex échappés",
             '["name"~"boulan\\"gerie \\\\(bio\\\\)",i]' in q and "amenity" not in q)

    # --- Résumé des échecs d'un scan ---
    verifier("un seul échec : « zone (message) », format inchangé",
             _resumer_echecs([("Lyon", "réseau coupé")]) == "ÉCHEC : Lyon (réseau coupé)")
    resume = _resumer_echecs([(f"Zone {i}", "Quota dépassé") for i in range(29)] + [("Nice", "Ville introuvable")])
    verifier("29 zones en échec pour la même cause : la cause n'est écrite qu'une fois, avec 5 zones citées et le reste compté",
             resume.count("Quota dépassé") == 1 and "29 zones (Zone 0, Zone 1, Zone 2, Zone 3, Zone 4 et 24 autre(s))" in resume
             and "Nice (Ville introuvable)" in resume and len(resume) < 200)

    # --- Normalisation d'un élément OSM ---
    exclus = ["wixsite", "no-reply"]
    e = element_vers_prospect({"type": "node", "id": 7, "tags": {
        "name": "Cabinet A", "website": "cabinet-a.fr", "contact:phone": "+33 5 00", "addr:street": "rue X",
        "addr:housenumber": "12", "addr:postcode": "33000", "addr:city": "Bordeaux",
        "email": "no-reply@wixsite.com; Dr.A@Cabinet-A.fr"}}, "Zone", exclus)
    verifier("élément OSM : site complété en https, adresse, email normalisé et filtré, pas d'avis",
             e["site"] == "https://cabinet-a.fr" and e["adresse"] == "12 rue X, 33000 Bordeaux"
             and e["email"] == "dr.a@cabinet-a.fr" and e["nb_avis"] is None and e["note_moyenne"] is None
             and e["source_id"] == "osm:node/7")
    verifier("élément OSM sans nom ignoré", element_vers_prospect({"type": "node", "id": 1, "tags": {}}, "Z", []) is None)

    # --- Extraction d'emails ---
    html = """<html><body>
      <a href="mailto:agence@webagency.fr">Site par WebAgency</a>
      <img src="logo@2x.png"> <p>no-reply@wixsite.com</p>
      <p>Écrivez-nous : accueil@cabinet-a.fr</p>
      <a href="mailto:Dr.A@Cabinet-A.fr?subject=RDV">Contact</a></body></html>"""
    verifier("email : mailto du domaine du site prioritaire ; agence, image, plateforme écartés",
             extraire_email(html, "cabinet-a.fr", ["wixsite", "no-reply"], []) == "dr.a@cabinet-a.fr")
    verifier("email : adresse d'un autre domaine seule = rien",
             extraire_email('<a href="mailto:x@autre.com">x</a>', "cabinet-a.fr", [], ["gmail.com"]) is None)
    verifier("email : gmail accepté (petit cabinet), après le domaine du site",
             extraire_email('<a href="mailto:dr.a@gmail.com">x</a>', "cabinet-a.fr", [], ["gmail.com"]) == "dr.a@gmail.com")
    verifier("email : adresse bouchon « xxx@gmail.com » d'un gabarit de site écartée, « max@… » conservée",
             extraire_email('<a href="mailto:xxx@gmail.com">x</a>', "cabinet-a.fr", [], ["gmail.com"]) is None
             and extraire_email('<a href="mailto:max@gmail.com">x</a>', "cabinet-a.fr", [], ["gmail.com"]) == "max@gmail.com")

    # --- Formulaire de contact : détection, exploration, rattrapage des prospects déjà en base ---
    formulaire = "<form action='/envoi'><input name='nom'><input type='email' name='mail'><textarea name='message'></textarea></form>"
    verifier("formulaire de contact reconnu (champ de message libre)", detecter_formulaire(formulaire))
    verifier("pas un formulaire de contact : lettre d'information (email seul), recherche, connexion, commentaires de blog",
             not detecter_formulaire("<form><input type='email'><button>S'abonner</button></form>")
             and not detecter_formulaire("<form role='search'><input name='s'><textarea></textarea></form>")
             and not detecter_formulaire("<form><input type='password'><textarea></textarea></form>")
             and not detecter_formulaire("<form id='commentform'><textarea></textarea></form>")
             and not detecter_formulaire("<form action='/wp-comments-post.php'><textarea></textarea></form>")
             and not detecter_formulaire("<p>Écrivez-nous</p>"))
    verifier("formulaire intégré (Typeform…) reconnu d'après la liste des réglages ; une vidéo ne l'est pas",
             detecter_formulaire("<iframe src='https://form.typeform.com/to/abc'></iframe>", ["typeform.com"])
             and not detecter_formulaire("<iframe src='https://www.youtube.com/embed/x'></iframe>", ["typeform.com"])
             and not detecter_formulaire("<iframe src='https://form.typeform.com/to/abc'></iframe>", []))
    telecharger_reel = globals()["_telecharger"]
    accueil_liens = ("<a href='/infos-pratiques-contact/'>Infos</a><a href='https://autre.fr/contact'>x</a><a href='/Écrivez-nous'>y</a>"
                     "<a href='mailto:a@b.fr'>m</a><a href='/blog'>b</a><a href='#contact'>ancre</a><a href='/Contact'>doublon</a>")
    verifier("liens de contact : même site seulement, texte ou adresse, accents ignorés, mailto/ancre/autre site écartés",
             liens_contact(accueil_liens, "https://www.x.fr/", ["contact", "ecrire", "ecrivez"], 5)
             == ["https://www.x.fr/infos-pratiques-contact/", "https://www.x.fr/Écrivez-nous", "https://www.x.fr/Contact"])
    verifier("liens de contact : la limite des réglages est respectée, 0 = aucun",
             len(liens_contact(accueil_liens, "https://www.x.fr/", ["contact"], 1)) == 1
             and liens_contact(accueil_liens, "https://www.x.fr/", ["contact"], 0) == [])
    site_pages = {"https://x.fr": "<html>Accueil</html>", "https://x.fr/contact": formulaire,
                  "https://z.fr": "<html><a href='/infos-pratiques-contact/'>Infos pratiques</a></html>",
                  "https://z.fr/infos-pratiques-contact/": formulaire,
                  "https://y.fr": "<html><a href='mailto:contact@y.fr'>écrire</a>" + formulaire + "</html>"}
    globals()["_telecharger"] = lambda cfg, url: (site_pages[url], url) if url in site_pages else (None, "HTTP 404")

    class _RobotsOuvert:
        def autorise(self, url):
            return True

    class _RobotsFerme:
        def autorise(self, url):
            return False
    cfg_contact = {"pause_pages_s": "0", "emails_exclus": "[]", "domaines_generiques": '["gmail.com"]',
                   "pages_contact": '["/contact", "/nous-contacter"]', "domaines_formulaires": "[]",
                   "mots_cles_contact": '["contact"]', "contact_liens_max": "3"}
    try:
        email, page_formulaire, nb = trouver_contact(cfg_contact, "https://x.fr", _RobotsOuvert())
        verifier("exploration : pas d'email mais un formulaire sur /contact -> l'adresse de la page du formulaire", email is None
                 and page_formulaire == "https://x.fr/contact" and nb == 3)
        email, page_formulaire, _nb = trouver_contact(cfg_contact, "https://z.fr", _RobotsOuvert())
        verifier("exploration : la page de contact est découverte par les liens de l'accueil (« /infos-pratiques-contact/ »), pas seulement par les chemins habituels",
                 email is None and page_formulaire == "https://z.fr/infos-pratiques-contact/")
        verifier("exploration : un email trouvé a la priorité sur le formulaire (le formulaire n'est pas retenu)",
                 trouver_contact(cfg_contact, "https://y.fr", _RobotsOuvert()) == ("contact@y.fr", None, 1))
        verifier("exploration : robots.txt interdit -> rien, aucune page visitée",
                 trouver_contact(cfg_contact, "https://x.fr", _RobotsFerme()) == (None, None, 0))
        verifier("trouver_email reste utilisable (email, pages)", trouver_email(cfg_contact, "https://y.fr", _RobotsOuvert()) == ("contact@y.fr", 1))
        ancien_chemin_contact = config.DB_PATH
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config.DB_PATH = Path(tmp) / "t.db"
            try:
                db.init_db(importer_env=False)
                with db.connexion() as c:
                    db.set_reglage(c, "pause_pages_s", "0")
                    camp = campagnes.creer(c, {"nom": "C", "secteur": "s", "pays": "FR", "langue": "fr"})
                    for nom, site in (("Avec formulaire", "https://x.fr"), ("Avec email", "https://y.fr"), ("Plateforme", "https://doctolib.fr/x"),
                                      ("Déjà exploré", "https://deja.fr"), ("Ignoré", "https://ignore.fr"), ("Sans site", None)):
                        db.inserer_prospect(c, {"campagne_id": camp, "nom": nom, "site": site, "source": "google_places", "source_id": "g:" + nom})
                    c.execute("UPDATE prospects SET statut = 'qualifie'")
                    c.execute("UPDATE prospects SET contact_cherche_le = '2026-01-01T00:00:00Z' WHERE nom = 'Déjà exploré'")
                    c.execute("UPDATE prospects SET statut = 'ignore' WHERE nom = 'Ignoré'")
                robots_reel = globals()["_Robots"]
                globals()["_Robots"] = lambda cfg: _RobotsOuvert()  # pas d'appel réseau pour robots.txt dans ce test
                try:
                    bilan = completer_contacts(0, camp)
                finally:
                    globals()["_Robots"] = robots_reel
                with db.connexion() as c:
                    lignes = {r["nom"]: dict(r) for r in c.execute("SELECT * FROM prospects WHERE campagne_id = ?", (camp,))}
                verifier("rattrapage : seuls les prospects avec un site propre jamais exploré, sans email, non ignorés sont visités",
                         bilan == {"visites": 2, "emails": 1, "formulaires": 1})
                verifier("rattrapage : formulaire et email enregistrés, date d'exploration posée, les autres intacts",
                         lignes["Avec formulaire"]["formulaire_url"] == "https://x.fr/contact" and lignes["Avec formulaire"]["email"] is None
                         and lignes["Avec email"]["email"] == "contact@y.fr" and lignes["Avec email"]["formulaire_url"] is None
                         and lignes["Avec formulaire"]["contact_cherche_le"] and lignes["Plateforme"]["contact_cherche_le"] is None
                         and lignes["Ignoré"]["contact_cherche_le"] is None and lignes["Sans site"]["contact_cherche_le"] is None)
                verifier("rattrapage rejouable : rien à refaire la seconde fois", completer_contacts(0, camp)["visites"] == 0
                         and "déjà été explorés" in chercher_contacts(0, camp)["detail"])
            finally:
                config.DB_PATH = ancien_chemin_contact
    finally:
        globals()["_telecharger"] = telecharger_reel

    # --- Recherche par nom en échec : les résultats par étiquettes sont conservés, l'échec est signalé ---
    http_reel = http.requete
    appels_osm = []

    def http_osm(methode, url, *, timeout, data=None, **kw):
        volet = "nom" if '["name"~' in (data or {}).get("data", "") else "etiquettes"
        appels_osm.append((volet, url))
        if volet == "nom":
            return _Reponse("", 504, url=url)
        return _Reponse(json.dumps({"elements": [{"type": "node", "id": 31, "tags": {"name": "Dr Derma"}}]}),
                        type_contenu="application/json")

    cfg_osm = {"timeout_overpass_s": "30", "emails_exclus": "[]", "plafond_resultats_scan": "100",
               "pause_requetes_s": "0", "overpass_urls": json.dumps(["https://a.example/api", "https://b.example/api"])}
    http.requete, sommeil = http_osm, time.sleep
    time.sleep = lambda s: None
    try:
        avert = []
        res = chercher_osm(cfg_osm, "médecine esthétique", regles, 44.84, -0.58, 15, "Bordeaux", avert)
        verifier("recherche par nom en échec : résultats par étiquettes conservés + avertissement lisible",
                 [e["nom"] for e in res] == ["Dr Derma"] and len(avert) == 1 and "recherche par nom" in avert[0]
                 and "Bordeaux" in avert[0])
        verifier("volet nom « au mieux » : un seul serveur, un seul essai (le scan n'est pas immobilisé)",
                 [v for v, _ in appels_osm].count("nom") == 1 and [v for v, _ in appels_osm].count("etiquettes") == 1)
        appels_osm.clear()
        memo = {"appels": 0, "reponses": {}}
        r1 = chercher_osm(cfg_osm, "médecine esthétique", regles, 44.84, -0.58, 15, "Bordeaux", [], memo)
        r2 = chercher_osm(cfg_osm, "médecin esthétique", regles, 44.84, -0.58, 15, "Bordeaux", [], memo)
        volets = [v for v, _ in appels_osm]
        verifier("deux requêtes de même règle : la requête par étiquettes n'est posée qu'une fois, la recherche par nom deux",
                 volets.count("etiquettes") == 1 and volets.count("nom") == 2 and r1 == r2 and len(memo["reponses"]) == 1)  # un échec n'est jamais mis en cache
        appels_osm.clear()
        try:
            chercher_osm(cfg_osm, "boulangerie", regles, 44.84, -0.58, 15, "Bordeaux", [])
            leve = False
        except ErreurSourcing:
            leve = True
        verifier("sans aucune étiquette, la recherche par nom reste fatale (rien d'autre à chercher) : 2 serveurs x 2 essais",
                 leve and len(appels_osm) == 4)

        # Seconde passe : le serveur A rejette vite (504) puis répond ; le serveur B met trop de temps (jamais réessayé)
        journal_appels = []

        def http_passes(methode, url, *, timeout, data=None, **kw):
            journal_appels.append(url)
            if url.startswith("https://b."):
                raise requests.exceptions.ReadTimeout("trop lent")
            if journal_appels.count(url) <= 2:  # les deux essais de la première passe
                return _Reponse("", 504, url=url)
            return _Reponse(json.dumps({"elements": [{"type": "node", "id": 41, "tags": {"name": "Dr Revenu"}}]}),
                            type_contenu="application/json")

        http.requete = http_passes
        cfg_passes = {**cfg_osm, "overpass_nouvelle_tentative_s": "5"}
        res = chercher_osm(cfg_passes, "dentiste", regles, 44.84, -0.58, 15, "Bordeaux", [])
        verifier("seconde passe : le serveur qui avait rejeté répond au 3e essai ; le serveur trop lent n'est pas réessayé",
                 [e["nom"] for e in res] == ["Dr Revenu"] and journal_appels.count("https://a.example/api") == 3
                 and journal_appels.count("https://b.example/api") == 1)
        journal_appels.clear()
        http.requete = lambda m, u, *, timeout, **kw: (_ for _ in ()).throw(requests.exceptions.ConnectionError("coupé"))
        try:
            chercher_osm(cfg_osm, "dentiste", regles, 44.84, -0.58, 15, "Bordeaux", [])
            message = ""
        except ErreurSourcing as erreur:
            message = str(erreur)
        verifier("tous les serveurs en échec : le message nomme CHAQUE serveur (pas seulement le dernier)",
                 "a.example" in message and "b.example" in message and "Connexion impossible" in message)
    finally:
        http.requete, time.sleep = http_reel, sommeil

    # --- Scan complet avec réseau simulé ---
    appels = {"overpass": 0, "pages": []}
    elements = [
        {"type": "node", "id": 1, "tags": {"name": "Cabinet Dentaire A", "website": "https://www.cabinet-a.fr"}},
        {"type": "node", "id": 2, "tags": {"name": "Cabinet B", "email": "Contact@B.fr", "addr:city": "Talence"}},
        {"type": "node", "id": 3, "tags": {"amenity": "dentist"}},                                   # sans nom
        {"type": "way", "id": 4, "tags": {"name": "Cabinet C", "website": "https://c.fr"}},          # robots interdit
        {"type": "node", "id": 5, "tags": {"name": "Autre nom A", "website": "https://cabinet-a.fr"}},  # même domaine que A
        {"type": "node", "id": 6, "tags": {"name": "Cabinet D", "website": "https://mort.fr"}},      # site mort
        {"type": "node", "id": 9, "tags": {"name": "Déjà ailleurs"}},                               # déjà dans une autre campagne
    ]

    def faux_http(methode, url, *, timeout, **kw):
        if timeout is None:
            raise AssertionError("appel sans timeout")
        if "overpass" in url:
            appels["overpass"] += 1
            return _Reponse(json.dumps({"elements": elements}), type_contenu="application/json")
        if url.endswith("/robots.txt"):
            if "c.fr" in url:
                return _Reponse("User-agent: *\nDisallow: /", url=url, type_contenu="text/plain")
            return _Reponse("", 404, url=url, type_contenu="text/plain")
        appels["pages"].append(url)
        if "mort.fr" in url:
            return _Reponse("", 500, url=url)
        if "cabinet-a.fr" in url and url.endswith("/contact"):
            return _Reponse(html, url=url)
        if "cabinet-a.fr" in url:
            return _Reponse("<html><body>Bienvenue</body></html>", url=url)
        return _Reponse("<html></html>", url=url)

    ancien_chemin = config.DB_PATH
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        config.DB_PATH = Path(tmp) / "t.db"
        http.requete = faux_http
        try:
            db.init_db(importer_env=False)
            with db.connexion() as c:
                for cle, val in (("pause_requetes_s", "0"), ("pause_pages_s", "0"), ("sourcing_provider", "osm")):
                    db.set_reglage(c, cle, val)
                c1 = campagnes.creer(c, {"nom": "Camp 1", "secteur": "dentiste", "pays": "FR", "langue": "fr"})
                campagnes.modifier(c, c1, {"requetes": ["dentiste", "dentaire"]})
                for ville in ("Bordeaux", "Sansgps"):
                    campagnes.ajouter_zone(c, c1, ville, "FR", None,
                                           lambda v, p: {"statut": "ok", "lat": 44.84, "lon": -0.58}
                                           if v == "Bordeaux" else {"statut": "introuvable"})
                c2 = campagnes.creer(c, {"nom": "Camp 2", "secteur": "x", "pays": "FR", "langue": "fr"})
                db.inserer_prospect(c, {"campagne_id": c2, "nom": "Déjà ailleurs", "source": "openstreetmap",
                                        "source_id": "osm:node/9"})
            jid = jobs.soumettre("scan", c1, scanner, c1, None)
            for _ in range(200):
                with db.connexion() as c:
                    job = jobs.lire(c, jid)
                if job["statut"] in ("termine", "erreur"):
                    break
                time.sleep(0.05)
            with db.connexion() as c:
                lignes = {r["nom"]: dict(r) for r in c.execute("SELECT * FROM prospects WHERE campagne_id = ?", (c1,))}
                zones = {z["ville"]: dict(z) for z in c.execute("SELECT * FROM zones WHERE campagne_id = ?", (c1,))}
                statut_camp = c.execute("SELECT statut FROM campagnes WHERE id = ?", (c1,)).fetchone()[0]
            verifier("scan : tâche terminée, 100 %, résumé lisible",
                     job["statut"] == "termine" and job["progression"] == 100 and "nouveau(x)" in job["detail"])
            verifier("scan : 2 requêtes de même règle OSM = 1 seul appel ; la zone sans coordonnées est sautée",
                     appels["overpass"] == 1 and "Sansgps" in job["detail"])
            verifier("scan : 4 nouveaux (A, B, C, D) ; sans nom, doublon de domaine et doublon d'autre campagne écartés",
                     set(lignes) == {"Cabinet Dentaire A", "Cabinet B", "Cabinet C", "Cabinet D"})
            verifier("scan : ignorés comptés par zone (doublons connus) et signalés dans le résumé",
                     zones["Bordeaux"]["nb_ignores"] > 0 and "ignoré(s), déjà en base" in job["detail"]
                     and "dans une autre campagne" in job["detail"])
            verifier("scan : zone terminée avec date et nombre trouvé (nouveaux + déjà en base), zone sans GPS restée à scanner",
                     zones["Bordeaux"]["statut"] == "termine" and zones["Bordeaux"]["dernier_scan"]
                     and zones["Bordeaux"]["nb_trouves"] == 6 and zones["Sansgps"]["statut"] == "a_scanner")
            verifier("scan : campagne passée de brouillon à active", statut_camp == "active")
            verifier("emails : trouvé sur /contact pour A, repris d'OSM pour B",
                     lignes["Cabinet Dentaire A"]["email"] == "dr.a@cabinet-a.fr" and lignes["Cabinet B"]["email"] == "contact@b.fr")
            verifier("emails : robots.txt respecté (C jamais visité), site mort sans email (D)",
                     lignes["Cabinet C"]["email"] is None and not any("c.fr" in u for u in appels["pages"])
                     and lignes["Cabinet D"]["email"] is None)
            verifier("scan : la date d'exploration du site est posée pour les sites visités (A, D), pas pour B (email déjà connu)",
                     lignes["Cabinet Dentaire A"]["contact_cherche_le"] and lignes["Cabinet D"]["contact_cherche_le"]
                     and lignes["Cabinet B"]["contact_cherche_le"] is None)
            verifier("emails : site mort visité une seule fois (pas de pages de contact inutiles)",
                     sum("mort.fr" in u for u in appels["pages"]) == 1)
            verifier("prospects OSM : ville de l'adresse si connue, sinon ville de la zone ; avis inconnus",
                     lignes["Cabinet B"]["ville"] == "Talence" and lignes["Cabinet Dentaire A"]["ville"] == "Bordeaux"
                     and lignes["Cabinet B"]["nb_avis"] is None)

            jobs._file.join()  # attend l'analyse enchaînée automatiquement après le scan
            with db.connexion() as c:
                analyses = {r["nom"]: dict(r) for r in c.execute(
                    "SELECT nom, statut, problemes, score FROM prospects WHERE campagne_id = ?", (c1,))}
                job_analyse = jobs.dernier(c, c1, "analyse")
            verifier("scan -> analyse enchaînée : prospects qualifiés, tâche d'analyse terminée",
                     job_analyse and job_analyse["statut"] == "termine"
                     and all(a["statut"] == "qualifie" for a in analyses.values()))
            verifier("analyse : OSM sans site = site_inconnu et score 0 (décision A) ; site mort = site_casse (9 pts)",
                     json.loads(analyses["Cabinet B"]["problemes"]) == ["site_inconnu"] and analyses["Cabinet B"]["score"] == 0
                     and "site_casse" in json.loads(analyses["Cabinet D"]["problemes"]) and analyses["Cabinet D"]["score"] == 9)

            # Rescan : tout est déjà en base
            avant = len(lignes)
            jid2 = jobs.soumettre("scan", c1, scanner, c1, None)
            for _ in range(200):
                with db.connexion() as c:
                    job2 = jobs.lire(c, jid2)
                if job2["statut"] in ("termine", "erreur"):
                    break
                time.sleep(0.05)
            with db.connexion() as c:
                apres = c.execute("SELECT COUNT(*) FROM prospects WHERE campagne_id = ?", (c1,)).fetchone()[0]
            verifier("rescan : aucun doublon créé", apres == avant and "0 nouveau(x)" in job2["detail"])
            verifier("rescan : le résumé explique pourquoi tout est ignoré (même fiche qu'au scan précédent)",
                     "même fiche que lors d'un scan précédent" in job2["detail"])
            verifier("premier scan : le doublon de domaine est expliqué (« même site web »)",
                     "même site web : 1" in job["detail"])

            # Échec d'une zone : Overpass en panne -> erreur lisible, zone restaurée
            http.requete = lambda m, u, *, timeout, **kw: _Reponse("", 504, url=u)
            ancien_sleep = time.sleep
            time.sleep = lambda s: None
            try:
                jid3 = jobs.soumettre("scan", c1, scanner, c1, None)
                for _ in range(200):
                    with db.connexion() as c:
                        job3 = jobs.lire(c, jid3)
                    if job3["statut"] in ("termine", "erreur"):
                        break
                    ancien_sleep(0.05)
            finally:
                time.sleep = ancien_sleep
            with db.connexion() as c:
                zone_apres = dict(c.execute("SELECT statut, nb_trouves, nb_ignores FROM zones WHERE ville = 'Bordeaux'").fetchone())
            verifier("Overpass en panne : tâche en erreur avec le message réel, résultat précédent de la zone conservé",
                     job3["statut"] == "erreur" and "saturé ou en panne" in job3["detail"]
                     and zone_apres["statut"] == "termine" and zone_apres["nb_trouves"] == 6 and zone_apres["nb_ignores"] > 0)

            # Garde-fous divers
            with db.connexion() as c:
                db.set_reglage(c, "sourcing_provider", "google")
                db.set_reglage(c, "google_places_key", "")
            jid4 = jobs.soumettre("scan", c1, scanner, c1, None)
            for _ in range(100):
                with db.connexion() as c:
                    job4 = jobs.lire(c, jid4)
                if job4["statut"] in ("termine", "erreur"):
                    break
                time.sleep(0.05)
            verifier("fournisseur « google » sans clé : refus explicite, aucun appel réseau",
                     job4["statut"] == "erreur" and "aucune clé" in job4["detail"].lower())
            with db.connexion() as c:
                j = c.execute("INSERT INTO jobs (type, campagne_id, statut) VALUES ('scan', ?, 'en_cours')", (c1,)).lastrowid
                jobs.marquer_interrompus(c)
                interrompu = jobs.lire(c, j)
            verifier("redémarrage : une tâche restée en cours est marquée interrompue",
                     interrompu["statut"] == "erreur" and "Interrompue" in interrompu["detail"])
        finally:
            jobs._file.join()
            http.requete = http_reel
            config.DB_PATH = ancien_chemin
    return resultats + _autotests_google()


def _autotests_google():
    """Google Places : requête, pagination, filtre de distance, repli sur OpenStreetMap. Réseau simulé."""
    import config
    from core import campagnes, jobs

    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    appels = {"google": [], "overpass": 0}
    comportement = {"google": None}

    def lieu(identifiant, nom, lat, lon, site=None, note=4.5, avis=12):
        valeur = {"id": identifiant, "displayName": {"text": nom}, "formattedAddress": f"1 rue {nom}, Lyon",
                  "nationalPhoneNumber": "04 00 00 00 00", "rating": note, "userRatingCount": avis,
                  "location": {"latitude": lat, "longitude": lon}}
        if site:
            valeur["websiteUri"] = site
        return valeur

    def faux_http(methode, url, *, timeout, **kw):
        if "places.googleapis.com" in url:
            appels["google"].append({"corps": kw["json"], "en_tetes": kw["headers"]})
            return comportement["google"](kw["json"])
        if "overpass" in url:
            appels["overpass"] += 1
            return _Reponse(json.dumps({"elements": [{"type": "node", "id": 77, "tags": {"name": "Secours OSM"}}]}),
                            type_contenu="application/json")
        if url.endswith("/robots.txt"):
            return _Reponse("", 404, url=url, type_contenu="text/plain")
        return _Reponse("<html><head><title>Cabinet</title></head><body><h1>x</h1></body></html>", url=url)

    def pages(corps):
        if "pageToken" not in corps:
            return _Reponse(json.dumps({"places": [lieu("p1", "Cabinet Un", 45.76, 4.84, "https://un.fr", 4.8, 120),
                                                   lieu("p2", "Cabinet Deux", 45.77, 4.85, None, 5.0, 3),
                                                   lieu("loin", "Trop Loin", 46.9, 5.5, "https://loin.fr")],
                                         "nextPageToken": "SUITE"}), type_contenu="application/json")
        return _Reponse(json.dumps({"places": [lieu("p3", "Cabinet Trois", 45.75, 4.83, "https://www.doctolib.fr/x")]}),
                        type_contenu="application/json")

    http_reel, sleep_reel, chemin_reel = http.requete, time.sleep, config.DB_PATH
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        config.DB_PATH = Path(tmp) / "g.db"
        http.requete, time.sleep = faux_http, lambda s: None
        try:
            db.init_db(importer_env=False)
            with db.connexion() as c:
                for cle, val in (("pause_requetes_s", "0"), ("pause_pages_s", "0"), ("google_places_key", "AIza-fausse"),
                                 ("sourcing_provider", "google")):
                    db.set_reglage(c, cle, val)
                cid = campagnes.creer(c, {"nom": "G", "secteur": "dentiste", "pays": "fr", "langue": "fr"})
                campagnes.modifier(c, cid, {"requetes": ["dentiste lyon"]})
                campagnes.ajouter_zone(c, cid, "Lyon", "FR", 15, lambda v, p: {"statut": "ok", "lat": 45.76, "lon": 4.84})
                cfg = reglages.valeurs_effectives(c)

            # --- La requête ---
            comportement["google"] = pages
            trouves = chercher_google(cfg, "dentiste lyon", 45.76, 4.84, 15, "Lyon", "FR", "fr")
            premier, second = appels["google"][0], appels["google"][1]
            verifier("Google : clé dans l'en-tête, champs demandés explicitement, biais = cercle (ville + rayon en mètres), pays et langue",
                     premier["en_tetes"]["X-Goog-Api-Key"] == "AIza-fausse"
                     and "places.websiteUri" in premier["en_tetes"]["X-Goog-FieldMask"]
                     and premier["corps"]["locationBias"]["circle"] == {"center": {"latitude": 45.76, "longitude": 4.84}, "radius": 15000.0}
                     and premier["corps"]["regionCode"] == "FR" and premier["corps"]["languageCode"] == "fr"
                     and premier["corps"]["textQuery"] == "dentiste lyon")
            verifier("Google : pagination (jeton de page renvoyé à la 2e requête, même texte), 3 résultats dont 1 hors rayon écarté -> 3 gardés sur 4",
                     second["corps"].get("pageToken") == "SUITE" and second["corps"]["textQuery"] == "dentiste lyon"
                     and [t["nom"] for t in trouves] == ["Cabinet Un", "Cabinet Deux", "Cabinet Trois"])
            verifier("Google : site, note, nombre d'avis, téléphone, source et identifiant d'origine repris ; sans site = None",
                     trouves[0]["site"] == "https://un.fr" and trouves[0]["note_moyenne"] == 4.8 and trouves[0]["nb_avis"] == 120
                     and trouves[1]["site"] is None and trouves[1]["nb_avis"] == 3 and trouves[0]["source"] == "google_places"
                     and trouves[0]["source_id"] == "google:p1" and trouves[0]["email"] is None)
            appels["google"].clear()
            verifier("Google : plafond de résultats respecté",
                     len(chercher_google({**cfg, "plafond_resultats_scan": "2"}, "d", 45.76, 4.84, 15, "Lyon", "FR", "fr")) == 2)

            # --- Plafond mensuel : jamais au-delà du palier gratuit ---
            with db.connexion() as c:
                fait = requetes_google_du_mois(c)
                db.set_reglage(c, "google_requetes_mois_max", str(fait + 1))
                cfg_plafond = reglages.valeurs_effectives(c)
            appels["google"].clear()
            try:
                chercher_google(cfg_plafond, "dentiste lyon", 45.76, 4.84, 15, "Lyon", "FR", "fr")  # 2 pages : la 2e doit être refusée
                message = ""
            except ErreurSourcing as e:
                message = str(e)
            with db.connexion() as c:
                apres = requetes_google_du_mois(c)
                db.set_reglage(c, "google_requetes_mois_max", "900")
            verifier("plafond mensuel Google : la requête qui le dépasserait n'est JAMAIS envoyée, message clair, compteur exact",
                     "Plafond mensuel Google Places atteint" in message and len(appels["google"]) == 1 and apres == fait + 1)
            verifier("compteur Google : rangé par mois (fuseau du Pacifique, comme la facturation Google)",
                     _cle_compteur_google(datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)) == "google_requetes_2026-06"
                     and _cle_compteur_google(datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)) == "google_requetes_2026-07")

            # --- Erreurs ---
            with db.connexion() as c:
                compteur_avant_refus = requetes_google_du_mois(c)
            comportement["google"] = lambda corps: _Reponse(json.dumps({"error": {"message": "API key not valid"}}), 400)
            try:
                chercher_google(cfg, "d", 45.76, 4.84, 15, "Lyon", "FR", "fr")
                message = ""
            except ErreurSourcing as e:
                message = str(e)
            verifier("Google refuse la clé : le message réel de Google est affiché", "API key not valid" in message and "400" in message)
            with db.connexion() as c:
                apres_refus = requetes_google_du_mois(c)
            verifier("une requête refusée (400) est remboursée : le compteur mensuel n'avance pas pour rien",
                     apres_refus == requetes_google_du_mois() and apres_refus == compteur_avant_refus)
            verifier("clé refusée : conseil concret (AIza, 39 caractères, console Google) sans afficher la clé entière",
                     "AIza" in message and "39 caractères" in message and "console.cloud.google.com" in message
                     and "AIza-fausse" not in message.replace("commence par « AIza »", ""))
            http_google = http.requete
            for corps_erreur, attendu in (
                    ("Places API (New) has not been used in project 123 before or it is disabled.", "Places API (New)"),
                    ("This API method requires billing to be enabled.", "facturation"),
                    ("Requests from this IP address are blocked.", "restreinte")):
                http.requete = lambda m, u, *, timeout, _c=corps_erreur, **k: _Reponse(json.dumps({"error": {"message": _c}}), 403)
                verifier(f"Google refuse (403) « {corps_erreur[:34]}… » : conseil adapté",
                         attendu in tester_connexion({**cfg, "http_timeout_s": "5"})["message"])
            http.requete = http_google

            # --- Scan de bout en bout, Google seul : peu_avis et sans_site deviennent fiables ---
            comportement["google"] = pages
            appels["google"].clear()
            jid = jobs.soumettre("scan", cid, scanner, cid, None)
            jobs._file.join()
            with db.connexion() as c:
                job = jobs.lire(c, jid)
                lignes = {r["nom"]: dict(r) for r in c.execute("SELECT * FROM prospects WHERE campagne_id = ?", (cid,))}
                analyse = jobs.dernier(c, cid, "analyse")
            verifier("scan Google : 3 prospects, source google_places, avis enregistrés, résumé indique Google Places",
                     job["statut"] == "termine" and set(lignes) == {"Cabinet Un", "Cabinet Deux", "Cabinet Trois"}
                     and lignes["Cabinet Un"]["nb_avis"] == 120 and "Google Places" in job["detail"] and appels["overpass"] == 0)
            problemes = {n: json.loads(l["problemes"]) for n, l in lignes.items()}
            verifier("analyse Google : sans site = « sans_site » (source fiable) + « peu_avis » (3 < 10) ; page Doctolib = « site_plateforme »",
                     problemes["Cabinet Deux"] == ["sans_site", "peu_avis"] and problemes["Cabinet Trois"] == ["site_plateforme"]
                     and lignes["Cabinet Deux"]["score"] == 12 and "sans_site" not in problemes["Cabinet Un"])

            # --- Repli sur OpenStreetMap en mode automatique, pas en mode Google explicite ---
            comportement["google"] = lambda corps: _Reponse(json.dumps({"error": {"message": "quota"}}), 403)
            with db.connexion() as c:
                db.set_reglage(c, "sourcing_provider", "auto")
            jid = jobs.soumettre("scan", cid, scanner, cid, None)
            jobs._file.join()
            with db.connexion() as c:
                job = jobs.lire(c, jid)
                osm = c.execute("SELECT COUNT(*) FROM prospects WHERE source = 'openstreetmap'").fetchone()[0]
            verifier("mode automatique : Google en échec -> OpenStreetMap prend le relais, et le résumé le dit",
                     job["statut"] == "termine" and osm == 1 and "secours OpenStreetMap pour : Lyon" in job["detail"] and appels["overpass"] >= 1)
            avant = appels["overpass"]
            with db.connexion() as c:
                db.set_reglage(c, "sourcing_provider", "google")
            jid = jobs.soumettre("scan", cid, scanner, cid, None)
            jobs._file.join()
            with db.connexion() as c:
                job = jobs.lire(c, jid)
            verifier("mode « google » explicite : pas de repli silencieux, l'échec est affiché",
                     job["statut"] == "erreur" and "quota" in job["detail"] and appels["overpass"] == avant)

            # --- Limite de requêtes de TON projet Google (HTTP 429 « per day ») : on n'insiste pas ---
            with db.connexion() as c:
                db.set_reglage(c, "sourcing_provider", "google")
                campagnes.ajouter_zone(c, cid, "Villeurbanne", "FR", 5, lambda v, p: {"statut": "ok", "lat": 45.77, "lon": 4.88})
                avant_compteur = requetes_google_du_mois(c)
            quota_jour = json.dumps({"error": {"message": "Quota exceeded for quota metric 'SearchTextRequest' and limit "
                                                          "'SearchTextRequest per day' of service 'places.googleapis.com'"}})
            comportement["google"] = lambda corps: _Reponse(quota_jour, 429)
            appels["google"].clear()
            jid = jobs.soumettre("scan", cid, scanner, cid, None)
            jobs._file.join()
            with db.connexion() as c:
                job = jobs.lire(c, jid)
                compteur = requetes_google_du_mois(c)
                statut_ville = c.execute("SELECT statut FROM zones WHERE campagne_id = ? AND ville = 'Villeurbanne'", (cid,)).fetchone()[0]
            verifier("limite journalière Google (429) : UNE seule requête (pas de nouvel essai), scan arrêté, zones suivantes restées « à scanner »",
                     len(appels["google"]) == 1 and job["statut"] == "erreur" and statut_ville == "a_scanner"
                     and "1 zone(s) non scannée(s)" in job["detail"])
            verifier("limite journalière : message utile (où régler la limite, quand elle repart) ; la requête refusée n'entame pas le plafond mensuel",
                     "Quotas" in job["detail"] and "minuit" in job["detail"] and compteur == avant_compteur)
            with db.connexion() as c:
                db.set_reglage(c, "sourcing_provider", "auto")
            appels["google"].clear()
            jid = jobs.soumettre("scan", cid, scanner, cid, None)
            jobs._file.join()
            with db.connexion() as c:
                job = jobs.lire(c, jid)
            verifier("mode automatique + limite Google : OpenStreetMap prend le relais et Google n'est appelé qu'UNE fois pour tout le scan",
                     job["statut"] == "termine" and len(appels["google"]) == 1
                     and "secours OpenStreetMap pour : Lyon, Villeurbanne" in job["detail"])
            verifier("commune lue dans l'adresse Google (code postal + commune), sinon la zone ; adresses étrangères aussi",
                     _ville_depuis_adresse("142 Rue Fondaudège, 33000 Bordeaux, France") == "Bordeaux"
                     and _ville_depuis_adresse("Rue Neuve 12, 1000 Bruxelles, Belgique") == "Bruxelles"
                     and _ville_depuis_adresse("1 rue Lyon") is None and _ville_depuis_adresse(None) is None)
        finally:
            jobs._file.join()
            http.requete, time.sleep, config.DB_PATH = http_reel, sleep_reel, chemin_reel
    return resultats


def _scan_en_direct(campagne_id):
    """Lance un scan réel depuis la ligne de commande (suivi de l'avancement affiché)."""
    from core import jobs

    with db.connexion() as conn:
        job_id = conn.execute("INSERT INTO jobs (type, campagne_id, statut) VALUES ('scan', ?, 'en_cours')",
                              (campagne_id,)).lastrowid
    dernier = ""
    fil = threading.Thread(target=lambda: resultat.update(scanner(job_id, campagne_id)), daemon=True)
    resultat = {}
    fil.start()
    while fil.is_alive():
        with db.connexion() as conn:
            detail = (jobs.lire(conn, job_id) or {}).get("detail") or ""
        if detail != dernier:
            print(detail, flush=True)
            dernier = detail
        time.sleep(1)
    print(f"[{resultat['statut']}] {resultat['detail']}")


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    commande = argv[1] if len(argv) > 1 else ""
    if commande == "check":
        resultats = _autotests()
        for reussi, libelle in resultats:
            print(f"{'OK ' if reussi else 'KO '} {libelle}")
        ok = sum(1 for r, _ in resultats if r)
        print(f"\nRésultat : {ok}/{len(resultats)} tests réussis")
        return 0 if ok == len(resultats) else 1
    if commande == "scan" and len(argv) > 2:
        import config
        config.configurer_logging()
        _scan_en_direct(int(argv[2]))
        return 0
    with db.connexion() as conn:
        cfg = reglages.valeurs_effectives(conn)
    print(fournisseur_effectif(cfg)[1])
    resultat = tester_connexion(cfg)
    print(f"[{resultat['statut']}] {resultat['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
