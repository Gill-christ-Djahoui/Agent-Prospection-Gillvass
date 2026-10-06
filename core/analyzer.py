"""Audit technique des sites : détecte les faiblesses, écrit les codes dans `prospects.problemes`.

Les codes détectés sont TOUS enregistrés, quel que soit leur poids : le score est calculé à part
(core.scorer) à partir des poids en base, ce qui permet de recalculer sans re-télécharger les sites.

Ajouter un critère d'analyse = écrire une fonction `verifier_*(page)`, l'ajouter à VERIFICATIONS,
insérer une ligne dans `criteres` (voir le README, section « Ajouter un critère d'analyse »).

Utilisable seul :
    python -m core.analyzer URL           analyse une URL et affiche les codes détectés
    python -m core.analyzer check         tests sur pages simulées, sans réseau
"""
import json
import logging
import sys
import time
import unicodedata
import warnings
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from core import db, http, reglages

journal = logging.getLogger("prospect")

_TAILLE_MAX_PAGE = 1_500_000
# Statuts HTTP qui signifient « le serveur nous refuse » (protection anti-robot, quota) et non « le site est mort » :
# on ne dit jamais « votre site est cassé » sur un simple refus de notre visite.
_STATUTS_REFUS = {401, 403, 429, 503}
_STATUTS_PAGE_DISPARUE = {404, 410}


def _sans_accents(texte):
    decompose = unicodedata.normalize("NFKD", texte or "")
    return "".join(c for c in decompose if not unicodedata.combining(c)).lower()


# --------------------------------------------------------------------------------------
# Chargement de la page
# --------------------------------------------------------------------------------------


def charger_page(cfg, prospect, robots=None):
    """Télécharge la page d'accueil et renvoie le dictionnaire « page » que reçoivent les vérifications.

    Clés : prospect, cfg, site, charge (page lue), erreur (None | 'casse' | 'non_analysable'),
    detail_erreur, statut_http, url_finale, secondes, https_invalide, soup.
    """
    site = (prospect.get("site") or "").strip()
    page = {"prospect": prospect, "cfg": cfg, "site": site, "charge": False, "erreur": None,
            "detail_erreur": None, "statut_http": None, "url_finale": None, "secondes": None,
            "https_invalide": False, "soup": None, "plateforme": False}
    if not site:
        return page
    if db.est_plateforme(db.extraire_domaine(site), json.loads(cfg.get("domaines_plateformes") or "[]")):
        page["plateforme"] = True  # une page Doctolib ou Facebook n'est pas un site à auditer
        return page
    url = site if "//" in site else "https://" + site
    if robots is not None and not robots.autorise(url):
        page["erreur"], page["detail_erreur"] = "non_analysable", "robots.txt interdit ou inaccessible"
        return page

    timeout = int(cfg["http_timeout_s"])
    en_tetes = {"User-Agent": f"agent-prospection/1.0 ({(cfg.get('expediteur_email') or 'usage local').strip()})",
                "Accept": "text/html,application/xhtml+xml"}
    reponse, debut = None, time.monotonic()
    file = [(url, True)]  # (adresse, vérifier le certificat)
    if url.startswith("https://"):
        file.append(("http://" + url[len("https://"):], True))  # certains vieux sites n'ont que du http
    while file:
        cible, verifier_ssl = file.pop(0)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # verify=False ne doit pas inonder la console d'avertissements
                reponse = http.requete("GET", cible, timeout=timeout, headers=en_tetes, stream=True,
                                       verify=verifier_ssl)
            break
        except requests.exceptions.SSLError:
            page["https_invalide"] = True
            if verifier_ssl and cible.startswith("https://"):
                file.insert(0, (cible, False))  # certificat invalide : on lit quand même le contenu
        except requests.Timeout:
            page["erreur"], page["detail_erreur"] = "non_analysable", f"délai dépassé ({timeout} s)"
            return page
        except requests.ConnectionError as erreur:
            if not file:
                page["erreur"], page["detail_erreur"] = "casse", f"connexion impossible : {str(erreur)[:120]}"
                return page
        except requests.RequestException as erreur:
            page["erreur"], page["detail_erreur"] = "casse", f"adresse invalide : {str(erreur)[:120]}"
            return page
    if reponse is None:
        page["erreur"], page["detail_erreur"] = "casse", "aucune connexion n'a abouti"
        return page

    # Un 404 sur un lien profond (« …/services/centre-x.html », fréquent dans OpenStreetMap) prouve que cette page a
    # disparu, pas que le site est cassé : on juge alors le site par sa page d'accueil.
    adresse = urlsplit(reponse.url)
    if reponse.status_code in _STATUTS_PAGE_DISPARUE and adresse.path.strip("/"):
        reponse.close()
        suite = charger_page(cfg, {**prospect, "site": f"{adresse.scheme}://{adresse.netloc}/"}, robots)
        suite["prospect"], suite["site"], suite["lien_profond_perime"] = prospect, site, True
        return suite

    try:
        page["statut_http"], page["url_finale"] = reponse.status_code, reponse.url
        if reponse.status_code >= 400:
            page["erreur"] = "non_analysable" if reponse.status_code in _STATUTS_REFUS else "casse"
            page["detail_erreur"] = f"HTTP {reponse.status_code}"
            return page
        contenu = b""
        for morceau in reponse.iter_content(65536):
            contenu += morceau
            if len(contenu) > _TAILLE_MAX_PAGE:
                break
        page["secondes"] = time.monotonic() - debut  # temps total de chargement de la page d'accueil
        texte = contenu.decode(reponse.encoding or "utf-8", errors="replace")
    finally:
        reponse.close()
    page["soup"] = BeautifulSoup(texte, "html.parser")
    page["charge"] = True
    seuil = float(cfg.get("seuil_lenteur_s") or 0)
    if seuil and page["secondes"] > seuil:
        # Une seule mesure lente peut être un accident (réseau, démarrage à froid du serveur) : « lent » exige
        # que la seconde mesure le soit aussi. On garde la meilleure des deux.
        seconde = _remesurer(cible, verifier_ssl, en_tetes, timeout)
        if seconde is not None:
            page["secondes"] = min(page["secondes"], seconde)
    return page


def _remesurer(cible, verifier_ssl, en_tetes, timeout):
    """Temps de téléchargement d'une seconde visite de la même page, ou None si elle échoue."""
    debut = time.monotonic()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            reponse = http.requete("GET", cible, timeout=timeout, headers=en_tetes, stream=True, verify=verifier_ssl)
        try:
            lu = 0
            for morceau in reponse.iter_content(65536):
                lu += len(morceau)
                if lu > _TAILLE_MAX_PAGE:
                    break
        finally:
            reponse.close()
    except requests.RequestException:
        return None
    return time.monotonic() - debut


# --------------------------------------------------------------------------------------
# Vérifications : chacune reçoit la page et renvoie un code si le problème est détecté, sinon None
# --------------------------------------------------------------------------------------


def _liste(page, cle):
    return json.loads(page["cfg"].get(cle) or "[]")


def _source_fiable(page):
    return page["prospect"].get("source") in _liste(page, "sources_site_fiable")


def verifier_sans_site(page):
    if not page["site"] and _source_fiable(page):
        return "sans_site"


def verifier_site_inconnu(page):
    # Décision produit : dans OpenStreetMap ou un CSV, un site vide ne prouve pas l'absence de site.
    if not page["site"] and not _source_fiable(page):
        return "site_inconnu"


def verifier_site_plateforme(page):
    if page.get("plateforme"):
        return "site_plateforme"


def verifier_non_analysable(page):
    if page["erreur"] == "non_analysable":
        return "non_analysable"


def verifier_site_casse(page):
    if page["erreur"] == "casse":
        return "site_casse"


def verifier_pas_https(page):
    if page["charge"] and (page["https_invalide"] or (page["url_finale"] or "").startswith("http://")):
        return "pas_https"


def verifier_lent(page):
    if page["charge"] and page["secondes"] > float(page["cfg"]["seuil_lenteur_s"]):
        return "lent"


def verifier_pas_mobile(page):
    if page["charge"] and page["soup"].find("meta", attrs={"name": lambda v: v and v.lower() == "viewport"}) is None:
        return "pas_mobile"


def verifier_titre_vide(page):
    if not page["charge"]:
        return None
    balise = page["soup"].find("title")
    titre = " ".join(balise.get_text().split()).lower() if balise else ""
    if not titre or titre in [t.lower() for t in _liste(page, "titres_generiques")]:
        return "titre_vide"


def verifier_description_vide(page):
    if not page["charge"]:
        return None
    meta = page["soup"].find("meta", attrs={"name": lambda v: v and v.lower() == "description"})
    if meta is None or not (meta.get("content") or "").strip():
        return "description_vide"


def verifier_pas_h1(page):
    if page["charge"] and page["soup"].find("h1") is None:
        return "pas_h1"


def verifier_pas_rdv(page):
    if not page["charge"]:
        return None
    soupe = page["soup"]
    for formulaire in soupe.find_all("form"):
        recherche = (formulaire.get("role") or "").lower() == "search" or (
            formulaire.find("input", attrs={"type": "search"}) is not None)
        if not recherche:
            return None  # un vrai formulaire (contact, réservation) = un moyen de conversion
    # Mots-clés dans le texte visible, les adresses des liens (doctolib…) et les attributs des boutons
    morceaux = [soupe.get_text(" ")]
    morceaux += [a.get("href", "") for a in soupe.find_all("a", href=True)]
    morceaux += [e.get("onclick", "") for e in soupe.find_all(attrs={"onclick": True})]
    ensemble = _sans_accents(" ".join(morceaux))
    if any(_sans_accents(mot) in ensemble for mot in _liste(page, "mots_cles_rdv") if mot):
        return None
    return "pas_rdv"


def verifier_peu_avis(page):
    avis = page["prospect"].get("nb_avis")
    # Avis inconnus (OpenStreetMap, CSV) : ce n'est pas « peu d'avis », on ne déclenche rien.
    if avis is not None and avis < int(page["cfg"]["seuil_avis_min"]):
        return "peu_avis"


VERIFICATIONS = [
    verifier_sans_site,
    verifier_site_inconnu,
    verifier_site_plateforme,
    verifier_non_analysable,
    verifier_site_casse,
    verifier_pas_https,
    verifier_lent,
    verifier_pas_mobile,
    verifier_titre_vide,
    verifier_description_vide,
    verifier_pas_h1,
    verifier_pas_rdv,
    verifier_peu_avis,
]


def analyser_page(page):
    """Codes détectés pour une page chargée (dans l'ordre de VERIFICATIONS)."""
    return [code for code in (verification(page) for verification in VERIFICATIONS) if code]


# --------------------------------------------------------------------------------------
# Analyse d'une campagne (tâche de fond)
# --------------------------------------------------------------------------------------


def analyser_campagne(job_id, campagne_id, seulement_nouveaux=True):
    """Analyse les sites des prospects d'une campagne, enregistre problèmes et score.

    seulement_nouveaux=True : uniquement les prospects jamais analysés (statut « nouveau »).
    False : ré-analyse aussi les « qualifiés » (les sites sont re-téléchargés).
    Les prospects ignorés, approuvés, envoyés ou répondus ne sont jamais touchés.
    """
    from core import jobs, scorer
    from core.sourcing import _Robots, completer_contacts

    statuts = ("nouveau",) if seulement_nouveaux else ("nouveau", "qualifie")
    with db.connexion() as conn:
        cfg = reglages.valeurs_effectives(conn)
        marques = ",".join("?" * len(statuts))
        prospects = [dict(l) for l in conn.execute(
            f"SELECT id, nom, site, source, nb_avis FROM prospects WHERE campagne_id = ? "
            f"AND statut IN ({marques}) ORDER BY id", (campagne_id, *statuts))]
    if not prospects:
        return {"statut": "termine", "detail": "Aucun prospect à analyser."}

    robots = _Robots(cfg)
    compte = {}
    nb = len(prospects)
    for i, prospect in enumerate(prospects):
        jobs.maj(job_id, 60 * i / nb, f"Analyse {i + 1}/{nb} : {prospect['nom']}")
        page = charger_page(cfg, prospect, robots)
        codes = analyser_page(page)
        with db.connexion() as conn:
            score = scorer.calculer_score(conn, campagne_id, codes)
            conn.execute(
                "UPDATE prospects SET problemes = ?, score = ?, "
                "statut = CASE WHEN statut = 'nouveau' THEN 'qualifie' ELSE statut END WHERE id = ?",
                (json.dumps(codes), score, prospect["id"]))
        for code in codes:
            compte[code] = compte.get(code, 0) + 1
    resume = ", ".join(f"{code} : {n}" for code, n in sorted(compte.items(), key=lambda x: -x[1])) or "aucun problème"
    # Un prospect qui a un site propre mais aucun email connu : on cherche son email, à défaut son formulaire de contact
    # (un scan l'a déjà fait pour ses nouveaux prospects ; ceci couvre les imports CSV et les anciens prospects).
    contacts = completer_contacts(job_id, campagne_id, ids=[p["id"] for p in prospects], base=60, part=40)
    suite = (f" — contacts : {contacts['emails']} email(s), {contacts['formulaires']} formulaire(s) de contact repéré(s)"
             if contacts["visites"] else "")
    return {"statut": "termine", "detail": f"{nb} prospect(s) analysé(s) et scoré(s) — {resume}{suite}"}


# --------------------------------------------------------------------------------------
# Auto-tests et ligne de commande
# --------------------------------------------------------------------------------------

_CFG_TEST = {"domaines_plateformes": '["doctolib.fr", "facebook.com"]', "seuil_lenteur_s": "3", "seuil_avis_min": "10", "sources_site_fiable": '["google_places"]',
             "titres_generiques": '["accueil", "home"]', "mots_cles_rdv": '["rendez-vous", "doctolib", "réserver"]'}

_HTML_BON = """<html><head><title>Cabinet Dr Martin — dentiste à Lyon</title>
  <meta name="viewport" content="width=device-width"><meta name="description" content="Cabinet dentaire à Lyon">
  </head><body><h1>Cabinet Martin</h1><a href="https://www.doctolib.fr/dentiste/lyon/martin">Prendre RDV</a></body></html>"""
_HTML_MAUVAIS = """<html><head><title>Accueil</title></head><body><p>Bienvenue</p>
  <form role="search"><input type="search"></form></body></html>"""


def _page_test(html, source="openstreetmap", site="https://x.fr", avis=None, secondes=0.5, **extra):
    page = {"prospect": {"source": source, "nb_avis": avis, "site": site}, "cfg": _CFG_TEST, "site": site,
            "charge": html is not None, "erreur": None, "detail_erreur": None, "statut_http": 200,
            "url_finale": site, "secondes": secondes, "https_invalide": False,
            "soup": BeautifulSoup(html, "html.parser") if html is not None else None}
    page.update(extra)
    return page


class _ReponseFausse:
    def __init__(self, texte="", statut=200, url="https://x.fr/"):
        self.status_code, self.url, self.encoding = statut, url, "utf-8"
        self._texte = texte
        self.headers = {"Content-Type": "text/html"}

    def iter_content(self, taille):
        yield self._texte.encode("utf-8")

    def close(self):
        pass


def _autotests():
    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    verifier("bon site : aucun problème détecté", analyser_page(_page_test(_HTML_BON, avis=50)) == [])
    codes = analyser_page(_page_test(_HTML_MAUVAIS, secondes=4.2, url_finale="http://x.fr"))
    verifier("mauvais site : https, lent, mobile, titre générique, description, h1, rdv (le formulaire de recherche ne compte pas)",
             set(codes) == {"pas_https", "lent", "pas_mobile", "titre_vide", "description_vide", "pas_h1", "pas_rdv"})
    verifier("un vrai formulaire (contact) compte comme moyen de conversion : pas de « pas_rdv »",
             "pas_rdv" not in analyser_page(_page_test("<html><form><input name='nom'></form></html>")))
    verifier("mot-clé de réservation dans l'adresse d'un lien (doctolib) reconnu, accents ignorés",
             "pas_rdv" not in analyser_page(_page_test("<a href='https://doctolib.fr/x'>ici</a>"))
             and "pas_rdv" not in analyser_page(_page_test("<p>Réserver en ligne</p>")))
    verifier("certificat invalide = pas_https", "pas_https" in analyser_page(_page_test(_HTML_BON, https_invalide=True)))
    verifier("seuil de lenteur lu dans les réglages (3 s)",
             "lent" not in analyser_page(_page_test(_HTML_BON, secondes=3.0))
             and "lent" in analyser_page(_page_test(_HTML_BON, secondes=3.1)))

    http_avant = http.requete
    http.requete = lambda *a, **k: (_ for _ in ()).throw(AssertionError("une plateforme ne doit jamais être téléchargée"))
    try:
        page_plateforme = charger_page(_CFG_TEST, {"site": "https://www.doctolib.fr/dentiste/lyon/martin", "source": "openstreetmap", "nb_avis": None})
        page_sous_domaine = charger_page(_CFG_TEST, {"site": "https://fr-fr.facebook.com/cabinet", "source": "openstreetmap", "nb_avis": None})
    finally:
        http.requete = http_avant
    verifier("page Doctolib / Facebook : « site_plateforme », jamais téléchargée ni analysée (donc pas de faux « pas_h1 »…)",
             analyser_page(page_plateforme) == ["site_plateforme"] and analyser_page(page_sous_domaine) == ["site_plateforme"])

    sans_site_osm = {"prospect": {"source": "openstreetmap", "nb_avis": None}, "cfg": _CFG_TEST, "site": "",
                     "charge": False, "erreur": None, "soup": None, "https_invalide": False}
    sans_site_google = {**sans_site_osm, "prospect": {"source": "google_places", "nb_avis": 3}}
    verifier("OpenStreetMap sans site : « site_inconnu », jamais « sans_site » (décision A)",
             analyser_page(sans_site_osm) == ["site_inconnu"])
    verifier("Google sans site : « sans_site » ; et 3 avis < 10 : « peu_avis »",
             analyser_page(sans_site_google) == ["sans_site", "peu_avis"])
    verifier("avis inconnus (OSM) : « peu_avis » jamais déclenché",
             "peu_avis" not in analyser_page(_page_test(_HTML_BON, avis=None)))
    verifier("site cassé : seul « site_casse » (pas de faux « pas_mobile »…)",
             analyser_page(_page_test(None, erreur="casse")) == ["site_casse"])
    verifier("refus d'accès (HTTP 403, anti-robot) : « non_analysable », jamais « site_casse »",
             analyser_page(_page_test(None, erreur="non_analysable")) == ["non_analysable"])

    # Chargement : classification des erreurs HTTP, sans réseau
    http_reel = http.requete
    try:
        cfg = {"http_timeout_s": "5", "expediteur_email": ""}
        p = {"site": "https://x.fr", "source": "openstreetmap", "nb_avis": None}

        def repondre(statut, texte=""):
            http.requete = lambda *a, **k: _ReponseFausse(texte, statut)
            return charger_page(cfg, p)
        verifier("HTTP 404 = site cassé ; HTTP 403 et 503 = non analysable",
                 repondre(404)["erreur"] == "casse" and repondre(403)["erreur"] == "non_analysable"
                 and repondre(503)["erreur"] == "non_analysable")
        verifier("HTTP 200 : page lue, temps mesuré", repondre(200, _HTML_BON)["charge"] and repondre(200, _HTML_BON)["secondes"] is not None)

        def lever(erreur):
            def f(*a, **k):
                raise erreur
            http.requete = f
            return charger_page(cfg, p)
        verifier("délai dépassé = non analysable ; domaine introuvable = site cassé",
                 lever(requests.Timeout())["erreur"] == "non_analysable"
                 and lever(requests.ConnectionError("getaddrinfo failed"))["erreur"] == "casse")
        appels = []

        def ssl_puis_ok(methode, url, **k):
            appels.append(k.get("verify"))
            if k.get("verify") is True:
                raise requests.exceptions.SSLError("certificat expiré")
            return _ReponseFausse(_HTML_BON)
        http.requete = ssl_puis_ok
        page = charger_page(cfg, p)
        verifier("certificat invalide : contenu quand même analysé (verify=False) et « pas_https » détecté",
                 page["charge"] and "pas_https" in analyser_page({**page, "cfg": _CFG_TEST}))

        # Lien profond périmé (404) : le site est jugé par sa page d'accueil
        vus = []

        def profond_perime(methode, url, **k):
            vus.append(url)
            return _ReponseFausse(_HTML_BON, 200 if url.rstrip("/") == "https://x.fr" else 404, url)
        http.requete = profond_perime
        profond = {"site": "https://x.fr/services/centre-x.html", "source": "openstreetmap", "nb_avis": None}
        page = charger_page(cfg, profond)
        verifier("404 sur un lien profond : le site n'est PAS déclaré cassé, sa page d'accueil est analysée",
                 page["charge"] and page["erreur"] is None and page.get("lien_profond_perime")
                 and vus == ["https://x.fr/services/centre-x.html", "https://x.fr/"]
                 and "site_casse" not in analyser_page({**page, "cfg": _CFG_TEST}))
        http.requete = lambda m, url, **k: _ReponseFausse("", 404, url)
        verifier("404 sur le lien profond ET sur la page d'accueil : site cassé confirmé",
                 charger_page(cfg, profond)["erreur"] == "casse")
        http.requete = lambda m, url, **k: _ReponseFausse("", 403, url)
        verifier("403 sur un lien profond : refus d'accès, aucune seconde visite (non analysable)",
                 charger_page(cfg, profond)["erreur"] == "non_analysable")

        # Lenteur : confirmée par une seconde mesure, on garde la meilleure
        cfg_lent = {**cfg, "seuil_lenteur_s": "0.05"}
        durees = iter([0.12, 0.0])

        def lente_puis_rapide(methode, url, **k):
            time.sleep(next(durees, 0.0))
            return _ReponseFausse(_HTML_BON, 200, url)
        http.requete = lente_puis_rapide
        page = charger_page(cfg_lent, p)
        verifier("une première visite lente puis une seconde rapide : pas de « lent » (accident de réseau)",
                 page["secondes"] < 0.05 and "lent" not in analyser_page({**page, "cfg": {**_CFG_TEST, "seuil_lenteur_s": "0.05"}}))
        http.requete = lambda m, url, **k: (time.sleep(0.12), _ReponseFausse(_HTML_BON, 200, url))[1]
        page = charger_page(cfg_lent, p)
        verifier("deux visites lentes : « lent » confirmé",
                 page["secondes"] > 0.05 and "lent" in analyser_page({**page, "cfg": {**_CFG_TEST, "seuil_lenteur_s": "0.05"}}))
    finally:
        http.requete = http_reel
    return resultats


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
    if len(argv) > 1:
        with db.connexion() as conn:
            cfg = reglages.valeurs_effectives(conn)
        page = charger_page(cfg, {"site": argv[1], "source": "google_places", "nb_avis": None})
        print(f"HTTP {page['statut_http']}, {page['secondes'] and round(page['secondes'], 2)} s, erreur : "
              f"{page['erreur'] or 'aucune'} {page['detail_erreur'] or ''}")
        print("Codes détectés :", ", ".join(analyser_page(page)) or "aucun")
        return 0
    print("Usage : python -m core.analyzer URL | check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
