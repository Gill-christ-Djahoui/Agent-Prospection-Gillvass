"""Réponses des prospects : lues dans ta boîte mail (IMAP, en LECTURE SEULE), pas saisies à la main.

L'application ouvre ton dossier de réception en lecture seule (elle ne marque rien comme lu, ne déplace ni ne supprime
rien), repère les messages qui répondent à un de tes envois, et met à jour le suivi :

- une vraie réponse     -> le prospect passe en « répondu » (plus de relance) et un extrait s'affiche dans Envois ;
- un « STOP » (ou « ne plus me contacter ») -> son adresse entre en liste noire, c'est un désabonnement ;
- une réponse automatique (absence, serveur) -> notée, mais ne compte pas comme une réponse.

Un message est rattaché à un envoi par l'en-tête de réponse (In-Reply-To / References) ou, à défaut, par l'adresse de
l'expéditeur. Chaque message n'est traité qu'une fois. Le mot de passe est stocké comme tes autres clés (masqué) ; avec
Gmail, utilise un « mot de passe d'application ».

Utilisable seul : python -m core.boite check     tests avec une boîte simulée
"""
import hashlib
import imaplib
import logging
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup

from core import db, reglages, suivi

journal = logging.getLogger("prospect")

_MOIS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_MESSAGES_MAX = 500
_JOURS_MAX = 60
_EXTRAIT_MAX = 500
_RE_AUTOMATIQUE_OBJET = re.compile(
    r"r[ée]ponse automatique|absence|absent|out of office|automatic reply|auto.?reply|undeliver|delivery status|"
    r"non remis|mail delivery|returned mail|accus[ée] de r[ée]ception", re.I)
_RE_EXPEDITEUR_SYSTEME = re.compile(r"^(mailer-daemon|postmaster|no-?reply|noreply|do-?not-?reply)@", re.I)
_RE_STOP = re.compile(r"^\W*(stop\b|d[ée]sinscri|d[ée]sabonn|ne\s+(?:me\s+)?(?:plus\s+)?(?:contact|[ée]cri|relanc)|"
                      r"retirez|supprimez(?:\s+mon)?|unsubscribe|remove\s+me)", re.I)
_RE_FIN_CITATION = re.compile(r"^(>|le .{5,80} a [ée]crit|on .{5,80} wrote|-{2,}\s*(message d'origine|original message)|"
                              r"de\s*:|from\s*:|envoy[ée] de mon)", re.I)


class ErreurBoite(Exception):
    """Boîte mail inutilisable (non configurée, refusée, injoignable) : le message est affiché tel quel."""


def _configuree(cfg):
    return all((cfg.get(c) or "").strip() for c in ("imap_serveur", "imap_utilisateur", "imap_mot_de_passe"))


def _connecter(cfg):
    if not _configuree(cfg):
        raise ErreurBoite("Boîte mail non configurée : renseigne le serveur, l'identifiant et le mot de passe "
                          "(Réglages > Clés et services > Boîte de réception).")
    hote, port = cfg["imap_serveur"].strip(), int(cfg.get("imap_port") or 993)
    try:
        imap = imaplib.IMAP4_SSL(hote, port, timeout=max(int(cfg.get("http_timeout_s") or 10), 10) * 2)
    except (OSError, imaplib.IMAP4.error) as erreur:
        raise ErreurBoite(f"Connexion impossible à {hote}:{port} ({erreur.__class__.__name__}) : vérifie le nom du serveur et le port.") from erreur
    try:
        imap.login(cfg["imap_utilisateur"].strip(), cfg["imap_mot_de_passe"])
    except imaplib.IMAP4.error as erreur:
        raise ErreurBoite("Identifiant ou mot de passe refusé par le serveur. Avec Gmail, il faut un « mot de passe d'application » "
                          "(Compte Google > Sécurité > Validation en deux étapes).") from erreur
    return imap


def _ouvrir_dossier(imap, cfg):
    dossier = (cfg.get("imap_dossier") or "INBOX").strip() or "INBOX"
    etat, donnees = imap.select(dossier, readonly=True)  # lecture seule : rien n'est jamais modifié dans ta boîte
    if etat != "OK":
        raise ErreurBoite(f"Dossier « {dossier} » introuvable dans la boîte mail.")
    return dossier, int(donnees[0] or 0)


def tester_connexion(cfg):
    """Connexion réelle, en lecture seule. Renvoie {'statut': ok|erreur|non_configure, 'message'}."""
    if not _configuree(cfg):
        return {"statut": "non_configure", "message": "Serveur, identifiant et mot de passe à renseigner."}
    try:
        imap = _connecter(cfg)
        try:
            dossier, nombre = _ouvrir_dossier(imap, cfg)
        finally:
            _fermer(imap)
    except ErreurBoite as erreur:
        return {"statut": "erreur", "message": str(erreur)}
    return {"statut": "ok", "message": f"Connecté à {cfg['imap_serveur'].strip()}, dossier « {dossier} » : {nombre} message(s). "
                                      "Lecture seule : rien n'est modifié dans ta boîte."}


def _fermer(imap):
    try:
        imap.logout()
    except (imaplib.IMAP4.error, OSError):
        pass


# --------------------------------------------------------------------------------------
# Lecture d'un message
# --------------------------------------------------------------------------------------


def _ids_message(valeur):
    return {m.lower() for m in re.findall(r"<([^<>\s]+)>", valeur or "")}


def _corps_texte(msg):
    corps = msg.get_body(preferencelist=("plain", "html")) if msg.is_multipart() or msg.get_content_maintype() == "text" else None
    if corps is None:
        return ""
    try:
        texte = corps.get_content()
    except (LookupError, ValueError):
        return ""
    if corps.get_content_subtype() == "html":
        texte = BeautifulSoup(texte, "html.parser").get_text("\n")
    return texte


def extrait_de(texte):
    """Ce que la personne a écrit, sans la citation de ton message ni la signature d'appareil, sur une ligne."""
    gardees = []
    for ligne in (texte or "").splitlines():
        if _RE_FIN_CITATION.match(ligne.strip()):
            break
        gardees.append(ligne.strip())
    return " ".join(" ".join(gardees).split())[:_EXTRAIT_MAX]


def classer(msg, extrait):
    """reponse | stop | automatique, d'après les en-têtes et le début du texte."""
    expediteur = parseaddr(msg.get("From", ""))[1]
    if (_RE_EXPEDITEUR_SYSTEME.match(expediteur) or (msg.get("Auto-Submitted") or "no").lower() != "no"
            or msg.get("X-Autoreply") or msg.get("X-Autorespond") or (msg.get("Precedence") or "").lower() in ("auto_reply", "bulk", "junk")
            or _RE_AUTOMATIQUE_OBJET.search(msg.get("Subject", "") or "")):
        return "automatique"
    return "stop" if _RE_STOP.match(extrait) else "reponse"


def _date_utc(msg, maintenant):
    try:
        return parsedate_to_datetime(msg["Date"]).astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError, IndexError):
        return maintenant


def _date_imap(jour):
    return f"{jour.day:02d}-{_MOIS[jour.month - 1]}-{jour.year}"


# --------------------------------------------------------------------------------------
# Synchronisation
# --------------------------------------------------------------------------------------


def synchroniser_reponses(cfg, maintenant=None):
    """Lit la boîte, enregistre les réponses, met à jour le suivi. Lève ErreurBoite. Renvoie un compte rendu."""
    maintenant = maintenant or datetime.now(timezone.utc)
    with db.connexion() as conn:
        envois = [dict(l) for l in conn.execute(
            """SELECT e.id, e.brevo_id, e.envoye_le, p.id AS prospect_id, p.email
                 FROM envois e JOIN messages m ON m.id = e.message_id JOIN prospects p ON p.id = m.prospect_id
                WHERE e.statut IN ('envoye', 'ouvert', 'repondu') AND p.email IS NOT NULL""")]
    if not envois:
        return {"reponses": 0, "stops": 0, "automatiques": 0, "message": "Aucun envoi à suivre."}
    par_adresse, par_identifiant = {}, {}
    for e in envois:
        par_adresse.setdefault(db.normaliser_email(e["email"]), e)
        for identifiant in _ids_message(e["brevo_id"] if (e["brevo_id"] or "").startswith("<") else f"<{e['brevo_id']}>"):
            par_identifiant[identifiant] = e
    plus_ancien = min(datetime.strptime(e["envoye_le"], _FORMAT).replace(tzinfo=timezone.utc) for e in envois)
    depuis = max(plus_ancien - timedelta(days=1), maintenant - timedelta(days=_JOURS_MAX))
    proprietaires = {db.normaliser_email(cfg.get("expediteur_email")), db.normaliser_email(cfg.get("imap_utilisateur"))}

    imap = _connecter(cfg)
    compte = {"reponses": 0, "stops": 0, "automatiques": 0}
    try:
        _ouvrir_dossier(imap, cfg)
        etat, donnees = imap.search(None, "SINCE", _date_imap(depuis))
        if etat != "OK":
            raise ErreurBoite("La recherche dans la boîte mail a échoué.")
        for numero in donnees[0].split()[-_MESSAGES_MAX:]:
            etat, brut = imap.fetch(numero, "(BODY.PEEK[])")  # PEEK : le message n'est pas marqué comme lu
            if etat != "OK" or not brut or not isinstance(brut[0], tuple):
                continue
            msg = message_from_bytes(brut[0][1], policy=policy.default)
            adresse = db.normaliser_email(parseaddr(msg.get("From", ""))[1])
            if not adresse or adresse in proprietaires:
                continue
            liens = _ids_message(msg.get("In-Reply-To")) | _ids_message(msg.get("References"))
            envoi = next((par_identifiant[i] for i in liens if i in par_identifiant), None) or par_adresse.get(adresse)
            if envoi is None:
                continue
            extrait = extrait_de(_corps_texte(msg))
            type_ = classer(msg, extrait)
            recu = _date_utc(msg, maintenant)
            identifiant = (msg.get("Message-ID") or "").strip() or hashlib.sha1(
                f"{adresse}|{msg.get('Date')}|{msg.get('Subject')}".encode("utf-8", "replace")).hexdigest()
            with db.connexion() as conn:
                if conn.execute("SELECT 1 FROM reponses WHERE identifiant = ?", (identifiant,)).fetchone():
                    continue  # déjà traité lors d'une synchronisation précédente
                conn.execute(
                    "INSERT INTO reponses (prospect_id, recu_le, expediteur, objet, extrait, type, identifiant) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (envoi["prospect_id"], recu.strftime(_FORMAT), adresse, (msg.get("Subject") or "")[:200], extrait or None, type_, identifiant))
                if type_ == "reponse":
                    try:
                        suivi.marquer_repondu(conn, envoi["prospect_id"])
                    except ValueError:
                        pass  # déjà marqué (à la main, ou par une réponse précédente)
                elif type_ == "stop":
                    db.ajouter_liste_noire(conn, envoi["email"], "desinscription")
                    journal.warning("liste noire (désinscription par réponse) : %s", envoi["email"])
            compte[{"reponse": "reponses", "stop": "stops", "automatique": "automatiques"}[type_]] += 1
    finally:
        _fermer(imap)
    with db.connexion() as conn:
        db.set_reglage(conn, "boite_derniere_sync", maintenant.strftime(_FORMAT))
    compte["message"] = (f"{compte['reponses']} réponse(s), {compte['stops']} demande(s) d'arrêt (adresse en liste noire), "
                         f"{compte['automatiques']} réponse(s) automatique(s) lue(s) dans ta boîte mail.")
    return compte


# --------------------------------------------------------------------------------------
# Auto-tests : boîte mail simulée
# --------------------------------------------------------------------------------------


class _FausseBoite:
    """Un faux serveur IMAP : mêmes appels que imaplib, messages fournis par le test."""
    messages = []
    refuse_mot_de_passe = False
    dossier_ouvert = None
    lecture_seule = None
    recherches = []
    lus_sans_peek = False

    def __init__(self, hote, port, timeout=None):
        pass

    def login(self, utilisateur, mot_de_passe):
        if _FausseBoite.refuse_mot_de_passe:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, dossier, readonly=False):
        _FausseBoite.dossier_ouvert, _FausseBoite.lecture_seule = dossier, readonly
        return ("OK", [str(len(_FausseBoite.messages)).encode()]) if dossier == "INBOX" else ("NO", [b"inexistant"])

    def search(self, charset, *criteres):
        _FausseBoite.recherches.append(criteres)
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(_FausseBoite.messages)))]

    def fetch(self, numero, spec):
        if "PEEK" not in spec:
            _FausseBoite.lus_sans_peek = True
        return "OK", [(b"1 (BODY[] {1}", _FausseBoite.messages[int(numero) - 1]), b")"]

    def logout(self):
        pass


def _mail(de, objet, corps, **en_tetes):
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = de, "gill-christ@gdjahoui.com", objet, "Tue, 06 Oct 2026 10:00:00 +0200"
    m["Message-ID"] = en_tetes.pop("message_id", f"<{hashlib.md5((de + objet).encode()).hexdigest()}@exemple>")
    for cle, valeur in en_tetes.items():
        m[cle.replace("_", "-")] = valeur
    m.set_content(corps)
    return m.as_bytes()


def _autotests():
    import config
    from core import campagnes

    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    imap_reel = imaplib.IMAP4_SSL
    imaplib.IMAP4_SSL = _FausseBoite
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ancien = config.DB_PATH
        config.DB_PATH = Path(tmp) / "t.db"
        try:
            db.init_db(importer_env=False)
            with db.connexion() as c:
                cid = campagnes.creer(c, {"nom": "C", "secteur": "s", "pays": "FR", "langue": "fr"})
                ids = {}
                for lettre in "ABCDE":
                    db.inserer_prospect(c, {"campagne_id": cid, "nom": "Cabinet " + lettre, "email": f"{lettre.lower()}@cabinet-{lettre.lower()}.fr",
                                            "ville": "Lyon", "source": "google_places", "source_id": "g:" + lettre})
                    pid = c.execute("SELECT id FROM prospects WHERE nom = ?", ("Cabinet " + lettre,)).fetchone()[0]
                    mid = c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'O', 'C', 1)", (pid,)).lastrowid
                    c.execute("INSERT INTO envois (message_id, statut, brevo_id, envoye_le) VALUES (?, 'envoye', ?, '2026-10-05T10:00:00Z')",
                              (mid, f"<id-{lettre.lower()}@brevo>"))
                    c.execute("UPDATE prospects SET statut = 'envoye' WHERE id = ?", (pid,))
                    ids[lettre] = pid
                db.set_reglage(c, "imap_serveur", "imap.exemple.fr")
                db.set_reglage(c, "imap_utilisateur", "gill-christ@gdjahoui.com")
                db.set_reglage(c, "imap_mot_de_passe", "secret")
                cfg = reglages.valeurs_effectives(c)

            _FausseBoite.messages = [
                _mail("Dr A <a@cabinet-a.fr>", "Re: Cabinet A : une question",
                      "Bonjour Gill,\nOui, ça m'intéresse. Pouvez-vous m'appeler demain ?\n\nLe 05/10/2026 à 11:00, Gill a écrit :\n> Bonjour Maître,", In_Reply_To="<id-a@brevo>"),
                _mail("Secrétariat <accueil@autre-domaine.fr>", "Re: Cabinet B", "Merci de transmettre à Maître B.", In_Reply_To="<id-b@brevo>"),
                _mail("c@cabinet-c.fr", "Re: Cabinet C", "STOP\n\nLe 05/10 Gill a écrit :", ),
                _mail("d@cabinet-d.fr", "Réponse automatique : absent jusqu'au 20", "Je suis absent.", Auto_Submitted="auto-replied"),
                _mail("hasard@inconnu.fr", "Offre de services", "Bonjour, nous proposons…"),
                _mail("Gill <gill-christ@gdjahoui.com>", "Re: copie de mon propre envoi", "Bonjour"),
            ]
            r = synchroniser_reponses(cfg, datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc))
            with db.connexion() as c:
                lignes = {l["expediteur"]: dict(l) for l in c.execute("SELECT * FROM reponses")}
                statuts = {k: c.execute("SELECT statut FROM prospects WHERE id = ?", (v,)).fetchone()[0] for k, v in ids.items()}
                statuts_envoi = {k: c.execute("SELECT e.statut FROM envois e JOIN messages m ON m.id = e.message_id WHERE m.prospect_id = ?", (v,)).fetchone()[0] for k, v in ids.items()}
                noire = {l[0]: l[1] for l in c.execute("SELECT valeur, motif FROM liste_noire")}
            verifier("réponse rattachée par l'adresse de l'expéditeur : prospect « répondu », envoi « répondu »",
                     statuts["A"] == "repondu" and statuts_envoi["A"] == "repondu" and lignes["a@cabinet-a.fr"]["type"] == "reponse")
            verifier("réponse d'un autre expéditeur (secrétariat) rattachée grâce à l'en-tête de réponse (In-Reply-To)",
                     statuts["B"] == "repondu" and lignes["accueil@autre-domaine.fr"]["prospect_id"] == ids["B"])
            verifier("extrait : ce que la personne a écrit, sans la citation de ton message",
                     lignes["a@cabinet-a.fr"]["extrait"] == "Bonjour Gill, Oui, ça m'intéresse. Pouvez-vous m'appeler demain ?")
            verifier("« STOP » : adresse en liste noire (désinscription), jamais comptée comme une réponse",
                     lignes["c@cabinet-c.fr"]["type"] == "stop" and noire.get("c@cabinet-c.fr") == "desinscription" and statuts["C"] == "envoye")
            verifier("réponse automatique d'absence : notée, mais le prospect n'est pas « répondu » et n'est pas blacklisté",
                     lignes["d@cabinet-d.fr"]["type"] == "automatique" and statuts["D"] == "envoye" and "d@cabinet-d.fr" not in noire)
            verifier("expéditeur inconnu et copie de ton propre envoi : ignorés",
                     "hasard@inconnu.fr" not in lignes and "gill-christ@gdjahoui.com" not in lignes and len(lignes) == 4)
            verifier("compte rendu : 2 réponses, 1 arrêt, 1 automatique",
                     (r["reponses"], r["stops"], r["automatiques"]) == (2, 1, 1) and "2 réponse(s)" in r["message"])
            verifier("boîte ouverte en LECTURE SEULE et lue sans marquer « lu » (BODY.PEEK) : rien n'est modifié chez toi",
                     _FausseBoite.lecture_seule is True and _FausseBoite.lus_sans_peek is False)
            verifier("recherche limitée aux messages depuis la veille du premier envoi (format IMAP en anglais)",
                     _FausseBoite.recherches[-1] == ("SINCE", "04-Oct-2026"))
            r2 = synchroniser_reponses(cfg, datetime(2026, 10, 7, 12, 5, tzinfo=timezone.utc))
            with db.connexion() as c:
                nb = c.execute("SELECT COUNT(*) FROM reponses").fetchone()[0]
                derniere = db.get_reglage(c, "boite_derniere_sync")
            verifier("rejouable sans doublon : chaque message n'est traité qu'une fois ; date de synchronisation enregistrée",
                     (r2["reponses"], r2["stops"], r2["automatiques"]) == (0, 0, 0) and nb == 4 and derniere == "2026-10-07T12:05:00Z")

            # Marquage manuel préalable : une réponse en plus ne casse rien
            _FausseBoite.messages.append(_mail("a@cabinet-a.fr", "Re: Re: Cabinet A", "Et merci encore.", In_Reply_To="<id-a@brevo>"))
            verifier("seconde réponse d'un prospect déjà « répondu » : enregistrée sans erreur",
                     synchroniser_reponses(cfg, datetime(2026, 10, 7, 13, 0, tzinfo=timezone.utc))["reponses"] == 1)

            verifier("test de connexion : ok, avec le nombre de messages et la mention « lecture seule »",
                     (t := tester_connexion(cfg))["statut"] == "ok" and "lecture seule" in t["message"].lower())
            verifier("dossier inexistant : message clair", tester_connexion({**cfg, "imap_dossier": "Archives"})["statut"] == "erreur"
                     and "introuvable" in tester_connexion({**cfg, "imap_dossier": "Archives"})["message"])
            _FausseBoite.refuse_mot_de_passe = True
            verifier("mot de passe refusé : message qui parle du mot de passe d'application (Gmail)",
                     "mot de passe d'application" in tester_connexion(cfg)["message"])
            _FausseBoite.refuse_mot_de_passe = False
            verifier("boîte non configurée : le dire, sans connexion", tester_connexion({**cfg, "imap_mot_de_passe": ""})["statut"] == "non_configure")
            try:
                synchroniser_reponses({**cfg, "imap_serveur": ""})
                message = ""
            except ErreurBoite as e:
                message = str(e)
            verifier("synchronisation sans configuration : erreur explicite", "non configurée" in message)
            with db.connexion() as c:
                campagnes.supprimer(c, cid, forcer=True)
                verifier("campagne supprimée : les réponses partent avec leurs prospects, aucune ligne orpheline",
                         c.execute("SELECT COUNT(*) FROM reponses").fetchone()[0] == 0 and c.execute("PRAGMA foreign_key_check").fetchall() == [])
        finally:
            imaplib.IMAP4_SSL = imap_reel
            config.DB_PATH = ancien
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
    print("Usage : python -m core.boite check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
