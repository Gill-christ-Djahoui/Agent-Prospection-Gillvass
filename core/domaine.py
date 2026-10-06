"""Santé du domaine d'envoi : SPF, DKIM, DMARC.

Ces trois enregistrements DNS décident de ta délivrabilité : sans eux (ou mal réglés), tes emails finissent en spam
quel que soit leur contenu. La vérification interroge d'abord le résolveur de ta machine (nslookup), puis, s'il est
indisponible, le DNS public sur HTTPS (sans clé, sans compte) ; aucun email n'est envoyé.

Utilisable seul : python -m core.domaine          vérifie le domaine de ton adresse d'expédition
                  python -m core.domaine check    tests, DNS simulé
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

from core import db, http, reglages

journal = logging.getLogger("prospect")

SERVEURS_DNS = ["https://dns.google/resolve", "https://cloudflare-dns.com/dns-query"]
_RE_CHUNKS = re.compile(r'"((?:[^"\\]|\\.)*)"')
_RE_NOM_DNS = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}$")
_PAUSE_DOH_S = 300  # un réseau qui bloque le DNS sur HTTPS n'est pas réinterrogé à chaque question (chaque échec dure ~11 s)
_doh_bloque_jusqua = 0.0


class ErreurDomaine(Exception):
    """Vérification impossible (réseau, adresse d'expédition absente) : le message est affiché tel quel."""


class _Indisponible(Exception):
    """Un résolveur ne peut pas répondre (absent, bloqué, hors délai) : on essaie le suivant."""


def _lire_nslookup(texte):
    """Sortie de `nslookup -type=TXT` -> même forme que le DNS sur HTTPS ({"Answer": [{"type", "data"}]}).

    Les libellés « text = » et « canonical name = » ne sont pas traduits, y compris sous Windows en français.
    """
    reponses, courant = [], None
    for ligne in texte.splitlines():
        brut = ligne.strip()
        if "text =" in ligne:
            courant = {"type": 16, "data": ""}
            reponses.append(courant)
            brut = ligne.split("text =", 1)[1].strip()  # vide sous Windows : le texte est sur les lignes suivantes
        elif "canonical name =" in ligne:
            courant = None
            reponses.append({"type": 5, "data": ligne.split("=", 1)[1].strip()})
            continue
        elif courant is None or not brut.startswith('"'):
            if brut:
                courant = None  # une ligne sans guillemets ferme l'enregistrement ; une ligne vide ne le ferme pas
            continue
        if courant is not None and brut.startswith('"'):
            morceaux = " ".join('"' + m + '"' for m in _RE_CHUNKS.findall(brut))
            courant["data"] = (courant["data"] + " " + morceaux).strip()
    if reponses:
        return {"Status": 0, "Answer": reponses}
    bas = texte.lower()
    if "non-existent domain" in bas or "nxdomain" in bas:
        return {"Status": 3, "Answer": []}  # le nom n'existe pas : une réponse valable, pas une panne
    if any(m in bas for m in ("timed out", "délai", "server failed", "servfail", "refused", "refusé", "no response")):
        raise _Indisponible("le serveur DNS de ta machine ne répond pas")
    return {"Status": 0, "Answer": []}  # le nom existe, sans enregistrement de ce type


def _interroger_systeme(nom, type_, timeout):
    """Résolveur de ta machine (nslookup, présent sous Windows, macOS et Linux)."""
    exe = shutil.which("nslookup")
    if not exe or not _RE_NOM_DNS.match(nom):
        raise _Indisponible("nslookup introuvable" if not exe else "nom DNS invalide")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        sortie = subprocess.run([exe, "-timeout=5", "-retry=1", f"-type={type_}", nom], capture_output=True,
                                timeout=max(timeout, 12), **options)
    except (subprocess.TimeoutExpired, OSError) as erreur:
        raise _Indisponible(f"nslookup : {erreur.__class__.__name__}") from erreur
    return _lire_nslookup((sortie.stdout + b"\n" + sortie.stderr).decode("latin-1"))


def _interroger_doh(cfg, nom, type_):
    """DNS sur HTTPS : chaque serveur public dans l'ordre. Lève _Indisponible si aucun ne répond."""
    global _doh_bloque_jusqua
    if time.monotonic() < _doh_bloque_jusqua:
        raise _Indisponible("DNS sur HTTPS bloqué par le réseau (déjà constaté à l'instant)")
    timeout = int(cfg["http_timeout_s"])
    derniere, reseau_coupe = "aucune réponse", False
    for url in SERVEURS_DNS:
        try:
            reponse = http.requete("GET", url, timeout=timeout, headers={"accept": "application/dns-json"},
                                   params={"name": nom, "type": type_})
        except requests.RequestException as erreur:
            derniere, reseau_coupe = http.message_erreur_reseau(erreur, timeout), True
            continue
        if not reponse.ok:
            derniere = f"HTTP {reponse.status_code}"
            continue
        try:
            return reponse.json()
        except ValueError:
            derniere = "réponse illisible"
    if reseau_coupe:
        _doh_bloque_jusqua = time.monotonic() + _PAUSE_DOH_S
    raise _Indisponible(derniere)


def _interroger(cfg, nom, type_):
    """Réponse DNS : le résolveur de la machine d'abord (il marche même quand le réseau filtre le DNS sur HTTPS)."""
    raisons = []
    try:
        return _interroger_systeme(nom, type_, int(cfg["http_timeout_s"]))
    except _Indisponible as erreur:
        raisons.append(f"résolveur de la machine : {erreur}")
    try:
        return _interroger_doh(cfg, nom, type_)
    except _Indisponible as erreur:
        raisons.append(f"DNS public : {erreur}")
    raise ErreurDomaine("Impossible de lire le DNS de ton domaine (" + " ; ".join(raisons) + "). "
                        "Vérifie ta connexion Internet, puis réessaie.")


def enregistrements(cfg, nom):
    """(textes TXT, a_un_cname) pour un nom DNS. Les TXT coupés en morceaux sont recollés."""
    donnees = _interroger(cfg, nom, "TXT")
    textes, cname = [], False
    for r in donnees.get("Answer") or []:
        if r.get("type") == 16:
            morceaux = _RE_CHUNKS.findall(r.get("data", ""))
            textes.append("".join(morceaux) if morceaux else r.get("data", ""))
        elif r.get("type") == 5:
            cname = True
    return textes, cname


def _spf(cfg, domaine, dkim_ok=False):
    textes, _ = enregistrements(cfg, domaine)
    spf = [t for t in textes if t.lower().startswith("v=spf1")]
    if not spf:
        return {"cle": "spf", "titre": "SPF", "statut": "erreur", "detail": "Aucun enregistrement SPF.",
                "conseil": f"Ajoute un enregistrement TXT sur {domaine} : « v=spf1 include:spf.brevo.com ~all » "
                           "(ou ajoute include:spf.brevo.com à ton SPF existant)."}
    if len(spf) > 1:
        return {"cle": "spf", "titre": "SPF", "statut": "erreur", "detail": f"{len(spf)} enregistrements SPF : c'est invalide.",
                "conseil": "Un domaine ne doit avoir qu'UN seul enregistrement SPF : fusionne-les en un seul."}
    valeur = spf[0]
    if "brevo" not in valeur.lower() and "sendinblue" not in valeur.lower():
        if dkim_ok:  # DMARC n'exige qu'UN des deux alignements : la signature DKIM de Brevo suffit
            return {"cle": "spf", "titre": "SPF", "statut": "info", "detail": f"SPF présent ; il n'inclut pas Brevo : {valeur}",
                    "conseil": "Sans conséquence tant que DKIM est valide : Brevo signe tes emails avec la clé DKIM de ton domaine, "
                               "et c'est elle qui fait passer DMARC. Pour aligner aussi le SPF, ajoute « include:spf.brevo.com »."}
        return {"cle": "spf", "titre": "SPF", "statut": "attention", "detail": f"SPF présent, mais il n'autorise pas Brevo : {valeur}",
                "conseil": "Ajoute « include:spf.brevo.com » à ton enregistrement SPF, ou authentifie ton domaine en DKIM dans Brevo."}
    if re.search(r"[+?]all\b", valeur) or not re.search(r"[~-]all\b", valeur):
        return {"cle": "spf", "titre": "SPF", "statut": "attention", "detail": f"SPF qui autorise Brevo, mais sa fin est trop permissive : {valeur}",
                "conseil": "Termine l'enregistrement par « ~all » (ou « -all »)."}
    return {"cle": "spf", "titre": "SPF", "statut": "ok", "detail": "SPF présent et autorise Brevo.", "conseil": ""}


def _dkim(cfg, domaine):
    selecteurs = json.loads(cfg.get("dkim_selecteurs") or "[]")
    trouves = []
    for s in selecteurs:
        textes, cname = enregistrements(cfg, f"{s}._domainkey.{domaine}")
        if cname or any(("v=dkim1" in t.lower() or "p=" in t.lower()) for t in textes):
            trouves.append(s)
    if trouves:
        return {"cle": "dkim", "titre": "DKIM", "statut": "ok", "detail": "Clé DKIM trouvée (sélecteur : " + ", ".join(trouves) + ").", "conseil": ""}
    return {"cle": "dkim", "titre": "DKIM", "statut": "attention",
            "detail": "Aucune clé DKIM trouvée aux sélecteurs habituels (" + ", ".join(selecteurs) + ").",
            "conseil": "Dans Brevo (Expéditeurs, domaines et IP > Domaines), authentifie ton domaine et ajoute les enregistrements DNS indiqués. "
                       "Si c'est déjà fait avec un sélecteur inhabituel, ajoute-le dans Réglages (sélecteurs DKIM)."}


def _dmarc(cfg, domaine):
    textes, _ = enregistrements(cfg, "_dmarc." + domaine)
    dmarc = [t for t in textes if t.lower().startswith("v=dmarc1")]
    if not dmarc:
        return {"cle": "dmarc", "titre": "DMARC", "statut": "attention", "detail": "Aucun enregistrement DMARC.",
                "conseil": f"Ajoute un TXT sur _dmarc.{domaine} : « v=DMARC1; p=none; rua=mailto:{cfg.get('reponse_email') or 'ton@adresse'} ». "
                           "Gmail et Yahoo l'exigent pour les envois en volume."}
    politique = re.search(r"\bp=(\w+)", dmarc[0], re.I)
    politique = politique.group(1).lower() if politique else "?"
    if politique == "none":
        return {"cle": "dmarc", "titre": "DMARC", "statut": "info", "detail": "DMARC en mode surveillance (p=none) : c'est correct pour démarrer.",
                "conseil": "Une fois SPF et DKIM validés depuis quelques semaines, passe à p=quarantine."}
    return {"cle": "dmarc", "titre": "DMARC", "statut": "ok", "detail": f"DMARC actif (p={politique}).", "conseil": ""}


def verifier_domaine(cfg):
    """Contrôle SPF, DKIM et DMARC du domaine de l'adresse d'expédition."""
    adresse = db.normaliser_email(cfg.get("expediteur_email"))
    if not adresse or "@" not in adresse:
        raise ErreurDomaine("Renseigne d'abord ton adresse d'expédition dans Réglages.")
    domaine = adresse.rsplit("@", 1)[1]
    dkim = _dkim(cfg, domaine)
    controles = [_spf(cfg, domaine, dkim["statut"] == "ok"), dkim, _dmarc(cfg, domaine)]
    pire = "ok"
    for c in controles:
        if c["statut"] == "erreur":
            pire = "erreur"
        elif c["statut"] == "attention" and pire != "erreur":
            pire = "attention"
    resultat = {"domaine": domaine, "statut": pire, "controles": controles,
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    journal.info("domaine %s vérifié : %s", domaine, pire)
    return resultat


def memoriser(conn, resultat):
    db.set_reglage(conn, "domaine_derniere_verif", json.dumps(resultat, ensure_ascii=False))


def derniere_verification(conn):
    brut = db.get_reglage(conn, "domaine_derniere_verif")
    return json.loads(brut) if brut else None


# --------------------------------------------------------------------------------------
# Auto-tests : DNS simulé
# --------------------------------------------------------------------------------------


class _Reponse:
    def __init__(self, reponses):
        self.status_code, self.ok = 200, True
        self._json = {"Status": 0, "Answer": reponses}
        self.text = json.dumps(self._json)

    def json(self):
        return self._json


def _autotests():
    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    http_reel, systeme_reel = http.requete, _interroger_systeme
    cfg = {"http_timeout_s": "5", "expediteur_email": "Gill@Exemple.FR", "reponse_email": "r@exemple.fr",
           "dkim_selecteurs": '["brevo1", "brevo2", "mail"]'}
    dns = {}

    def systeme_en_panne(nom, type_, timeout):
        raise _Indisponible("test : pas de résolveur")
    globals()["_interroger_systeme"] = systeme_en_panne  # d'abord, on teste le DNS sur HTTPS seul
    globals()["_doh_bloque_jusqua"] = 0.0

    def txt(valeur):
        return {"type": 16, "data": '"' + valeur + '"'}

    def faux(methode, url, *, timeout, **kw):
        return _Reponse(dns.get(kw["params"]["name"], []))
    http.requete = faux
    try:
        dns.update({"exemple.fr": [txt("v=spf1 include:spf.brevo.com ~all")],
                    "brevo1._domainkey.exemple.fr": [{"type": 5, "data": "b1.exemple-fr.dkim.brevo.com."}],
                    "_dmarc.exemple.fr": [txt("v=DMARC1; p=none; rua=mailto:r@exemple.fr")]})
        r = verifier_domaine(cfg)
        etats = {c["cle"]: c["statut"] for c in r["controles"]}
        verifier("domaine bien réglé : SPF ok, DKIM ok (CNAME Brevo), DMARC en surveillance ; adresse normalisée",
                 etats == {"spf": "ok", "dkim": "ok", "dmarc": "info"} and r["domaine"] == "exemple.fr" and r["statut"] == "ok")
        dns["exemple.fr"] = []
        verifier("SPF absent : erreur avec le conseil exact à appliquer",
                 (c := verifier_domaine(cfg)["controles"][0])["statut"] == "erreur" and "include:spf.brevo.com" in c["conseil"])
        dns["exemple.fr"] = [txt("v=spf1 include:_spf.google.com ~all")]
        verifier("SPF sans Brevo mais DKIM valide : simple information (DMARC passe grâce au DKIM)",
                 (c := verifier_domaine(cfg)["controles"][0])["statut"] == "info" and "DKIM" in c["conseil"])
        dns["brevo1._domainkey.exemple.fr"] = []
        verifier("SPF sans Brevo ET pas de DKIM : attention", verifier_domaine(cfg)["controles"][0]["statut"] == "attention")
        dns["brevo1._domainkey.exemple.fr"] = [{"type": 5, "data": "b1.exemple-fr.dkim.brevo.com."}]
        dns["exemple.fr"] = [txt("v=spf1 include:spf.brevo.com ~all"), txt("v=spf1 include:autre.com ~all")]
        verifier("deux SPF : erreur (invalide)", verifier_domaine(cfg)["controles"][0]["statut"] == "erreur")
        dns["exemple.fr"] = [txt("v=spf1 include:spf.brevo.com +all")]
        verifier("SPF « +all » : attention (trop permissif)", verifier_domaine(cfg)["controles"][0]["statut"] == "attention")
        dns["exemple.fr"] = [{"type": 16, "data": '"v=spf1 include:spf.bre" "vo.com ~all"'}]
        verifier("TXT coupé en deux morceaux : recollé correctement", verifier_domaine(cfg)["controles"][0]["statut"] == "ok")
        dns["brevo1._domainkey.exemple.fr"] = []
        verifier("DKIM introuvable : attention avec la marche à suivre", (c := verifier_domaine(cfg)["controles"][1])["statut"] == "attention" and "Brevo" in c["conseil"])
        dns["mail._domainkey.exemple.fr"] = [txt("v=DKIM1; k=rsa; p=MIGfMA0")]
        verifier("DKIM en TXT (sélecteur « mail ») reconnu", verifier_domaine(cfg)["controles"][1]["statut"] == "ok")
        dns["_dmarc.exemple.fr"] = []
        verifier("DMARC absent : attention avec l'enregistrement à créer", (c := verifier_domaine(cfg)["controles"][2])["statut"] == "attention" and "v=DMARC1" in c["conseil"])
        dns["_dmarc.exemple.fr"] = [txt("v=DMARC1; p=quarantine")]
        verifier("DMARC p=quarantine : ok", verifier_domaine(cfg)["controles"][2]["statut"] == "ok")
        try:
            verifier_domaine({**cfg, "expediteur_email": ""})
            message = ""
        except ErreurDomaine as e:
            message = str(e)
        verifier("sans adresse d'expédition : message clair, aucun appel", "adresse d'expédition" in message)
        http.requete = lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("réseau coupé"))
        try:
            verifier_domaine(cfg)
            message = ""
        except ErreurDomaine as e:
            message = str(e)
        verifier("DNS entièrement injoignable : erreur explicite qui cite les deux voies essayées, pas de plantage",
                 "Impossible de lire le DNS" in message and "résolveur de la machine" in message and "DNS public" in message)

        # --- Résolveur de la machine : le cas d'un réseau qui bloque le DNS sur HTTPS ---
        globals()["_doh_bloque_jusqua"] = 0.0
        windows = ("Serveur :   dns.google\nAddress:  8.8.8.8\n\nRéponse ne faisant pas autorité :\n"
                   "exemple.fr\ttext =\n\n\t\"v=spf1 include:spf.brevo.com ~all\"\n"
                   "exemple.fr\ttext =\n\n\t\"brevo-code:abc\"\n")
        lu = _lire_nslookup(windows)["Answer"]
        verifier("nslookup (Windows, en français) : deux TXT lus, texte intact",
                 [r["data"] for r in lu] == ['"v=spf1 include:spf.brevo.com ~all"', '"brevo-code:abc"'])
        lu = _lire_nslookup("Serveur :   dns.google\nAddress:  8.8.8.8\n\nbrevo1._domainkey.exemple.fr\tcanonical name = b1.exemple-fr.dkim.brevo.com\n"
                            "b1.exemple-fr.dkim.brevo.com\tcanonical name = brevo5.dkim.brevo.com\n"
                            "brevo5.dkim.brevo.com\ttext =\n\n\t\"k=rsa;p=AAAA\"\n\t\"BBBB\"\n")["Answer"]
        verifier("nslookup : chaîne de CNAME reconnue, TXT coupé sur deux lignes recollé",
                 [r["type"] for r in lu] == [5, 5, 16] and "".join(_RE_CHUNKS.findall(lu[2]["data"])) == "k=rsa;p=AAAABBBB")
        lu = _lire_nslookup('exemple.fr\ttext = "v=spf1 a" "b ~all"\nexemple.fr\ttext = "autre"\n')["Answer"]
        verifier("nslookup (Linux : tout sur une ligne) : deux enregistrements, morceaux conservés",
                 len(lu) == 2 and "".join(_RE_CHUNKS.findall(lu[0]["data"])) == "v=spf1 ab ~all")
        verifier("nslookup : « Non-existent domain » = aucun enregistrement, ce n'est pas une panne",
                 _lire_nslookup("*** dns.google ne parvient pas à trouver x.exemple.fr : Non-existent domain")["Answer"] == [])
        try:
            _lire_nslookup("DNS request timed out.\n    timeout was 2 seconds.\n")
            panne = False
        except _Indisponible:
            panne = True
        verifier("nslookup : délai dépassé = résolveur indisponible (on essaie le DNS sur HTTPS)", panne)

        appels_doh = []
        http.requete = lambda *a, **k: appels_doh.append(1) or (_ for _ in ()).throw(requests.ConnectionError("bloqué"))
        textes = {"exemple.fr": "exemple.fr\ttext =\n\n\t\"v=spf1 include:spf.brevo.com ~all\"\n",
                  "brevo1._domainkey.exemple.fr": "brevo1._domainkey.exemple.fr\tcanonical name = b1.x.brevo.com\n",
                  "_dmarc.exemple.fr": "_dmarc.exemple.fr\ttext =\n\n\t\"v=DMARC1; p=none\"\n"}
        globals()["_interroger_systeme"] = lambda nom, type_, timeout: _lire_nslookup(textes.get(nom, "*** x : Non-existent domain"))
        r = verifier_domaine(cfg)
        verifier("DNS sur HTTPS bloqué par le réseau mais résolveur de la machine disponible : vérification complète, "
                 "sans aucun appel au DNS sur HTTPS", r["statut"] == "ok" and not appels_doh
                 and {c["cle"]: c["statut"] for c in r["controles"]} == {"spf": "ok", "dkim": "ok", "dmarc": "info"})

        globals()["_interroger_systeme"] = systeme_en_panne
        http.requete = lambda *a, **k: appels_doh.append(1) or (_ for _ in ()).throw(requests.ConnectionError("bloqué"))
        appels_doh.clear()
        for _ in range(2):
            try:
                enregistrements(cfg, "exemple.fr")
            except ErreurDomaine:
                pass
        verifier("réseau qui bloque le DNS sur HTTPS : constaté une fois (2 serveurs), puis plus réinterrogé (pas d'attente de 11 s par question)",
                 len(appels_doh) == len(SERVEURS_DNS))
    finally:
        http.requete = http_reel
        globals()["_interroger_systeme"] = systeme_reel
        globals()["_doh_bloque_jusqua"] = 0.0
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
    with db.connexion() as conn:
        cfg = reglages.valeurs_effectives(conn)
    try:
        r = verifier_domaine(cfg)
    except ErreurDomaine as e:
        print("Erreur :", e)
        return 1
    print(f"Domaine {r['domaine']} : {r['statut']}")
    for c in r["controles"]:
        print(f"  [{c['statut']}] {c['titre']} : {c['detail']}" + (f"\n        -> {c['conseil']}" if c["conseil"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
