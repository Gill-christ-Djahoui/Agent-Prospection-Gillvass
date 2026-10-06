"""Validation humaine des messages : file de relecture, corrections, approbation.

C'est le SEUL endroit du code qui pose une approbation (`approuve_par_humain = 1`) et qui fait passer
un prospect à « approuvé ». L'approbation porte sur un message précis (message_id) : c'est ce
message-là, et aucun autre, que l'envoi lira. Toute correction du texte ou toute régénération annule
l'approbation (garde-fous dans la base, voir core.db).

Utilisable seul : python -m core.messages check   (tests sur base jetable, sans réseau)
"""
import json
import logging
import re
import sys
import tempfile
from pathlib import Path

from core import db

journal = logging.getLogger("prospect")

_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAINTENANT = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


class ErreurMessage(Exception):
    """Refus d'une action de validation : le message est affiché tel quel à l'utilisateur."""


# --------------------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------------------


def compteurs(conn):
    """Messages en attente de validation / approuvés et pas encore envoyés."""
    ligne = conn.execute(
        "SELECT SUM(statut = 'message_pret') AS en_attente, SUM(statut = 'approuve') AS approuves "
        "FROM prospects").fetchone()
    return {"en_attente": ligne["en_attente"] or 0, "approuves": ligne["approuves"] or 0}


def file_de_validation(conn):
    """Prospects dont le message est prêt (à valider d'abord, puis approuvés), les plus gros besoins en premier."""
    return [dict(l) for l in conn.execute(
        """SELECT p.id, p.nom, p.ville, p.score, p.statut, p.email IS NOT NULL AS a_email,
                  est_generique(p.email) AS generique,
                  (SELECT COUNT(*) FROM envois e JOIN messages m ON m.id = e.message_id
                    WHERE m.prospect_id = p.id AND e.statut IN ('envoye', 'ouvert', 'repondu')) AS relance_numero,
                  (SELECT COUNT(*) FROM messages m WHERE m.prospect_id = p.id) AS nb_versions
             FROM prospects p WHERE p.statut IN ('message_pret', 'approuve')
            ORDER BY (p.statut = 'approuve'), p.score DESC, p.id""")]


def _envoye(conn, message_id):
    return conn.execute("SELECT 1 FROM envois WHERE message_id = ? AND statut <> 'echec'",
                        (message_id,)).fetchone() is not None


def raison_de_refus(conn, prospect, message):
    """Pourquoi ce message ne peut pas être approuvé (None s'il le peut)."""
    if prospect["statut"] not in ("message_pret", "approuve"):
        return f"Le prospect est « {prospect['statut']} » : il n'y a plus de message à valider."
    if not prospect["email"]:
        return "Ce prospect n'a pas d'email : saisis-en un, ou contacte-le par téléphone."
    if db.est_en_liste_noire(conn, prospect["email"]):
        return "Cette adresse (ou son domaine) est en liste noire : aucun message ne peut lui être envoyé."
    if _envoye(conn, message["id"]):
        return "Ce message a déjà été envoyé."
    return None


def revue(conn, prospect_id, cfg):
    """Tout ce qu'il faut pour relire un prospect : fiche, problèmes en clair, versions du message."""
    from core import campagnes

    p = conn.execute("SELECT *, est_generique(email) AS generique FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    if p is None:
        raise ErreurMessage("Prospect introuvable.")
    p = dict(p)
    criteres = {c["code"]: c for c in campagnes.criteres_effectifs(conn, p["campagne_id"])}
    problemes = [{"code": c, "libelle": criteres[c]["libelle"] if c in criteres else c,
                  "points": criteres[c]["points"] if c in criteres and criteres[c]["actif"] else 0}
                 for c in json.loads(p["problemes"])]
    versions = []
    for m in conn.execute("SELECT * FROM messages WHERE prospect_id = ? ORDER BY version", (prospect_id,)):
        m = dict(m)
        m["envoye"] = _envoye(conn, m["id"])
        m["refus"] = raison_de_refus(conn, p, m)
        versions.append(m)
    relance_numero = conn.execute(
        "SELECT COUNT(*) FROM envois e JOIN messages m ON m.id = e.message_id "
        "WHERE m.prospect_id = ? AND e.statut IN ('envoye', 'ouvert', 'repondu')", (prospect_id,)).fetchone()[0]
    from core import sender
    autorisees = json.loads(cfg.get("envoi_adresses_autorisees") or "[]")
    return {"mode_essai_actif": bool(autorisees), "hors_essai": not sender.destinataire_autorise(cfg, p["email"]),
            "relance_numero": relance_numero, "prospect": {k: p[k] for k in ("id", "nom", "ville", "adresse", "telephone", "site", "email",
                                           "generique", "statut", "score", "source", "campagne_id", "notes")},
            "problemes": problemes, "versions": versions,
            "mention_desinscription": cfg.get("mention_desinscription", ""),
            "site_signature": __import__("core.sender", fromlist=["site_a_afficher"]).site_a_afficher(cfg, relance_numero > 0),
            "mots_max": int(cfg.get("mots_max_message") or 0)}


# --------------------------------------------------------------------------------------
# Écriture
# --------------------------------------------------------------------------------------


def _texte_valide(objet, corps):
    objet = " ".join(str(objet or "").split())
    corps = str(corps or "").replace("\r\n", "\n").strip()
    if not objet:
        raise ErreurMessage("L'objet ne peut pas être vide.")
    if not corps:
        raise ErreurMessage("Le message ne peut pas être vide.")
    if len(objet) > 300:
        raise ErreurMessage("L'objet est trop long (300 caractères maximum).")
    return objet, corps


def _charger(conn, message_id):
    m = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if m is None:
        raise ErreurMessage("Message introuvable.")
    p = conn.execute("SELECT * FROM prospects WHERE id = ?", (m["prospect_id"],)).fetchone()
    return dict(m), dict(p)


def enregistrer_edition(conn, message_id, objet, corps):
    """Enregistre les corrections de l'utilisateur. Si le message était approuvé, l'approbation est annulée
    (par la base) : ce qui partira sera toujours le texte qu'il a relu et approuvé."""
    m, p = _charger(conn, message_id)
    if _envoye(conn, message_id):
        raise ErreurMessage("Ce message a déjà été envoyé : il ne peut plus être modifié.")
    objet, corps = _texte_valide(objet, corps)
    if (objet, corps) != (m["objet"], m["corps"]):
        conn.execute("UPDATE messages SET objet = ?, corps = ?, edite_par_humain = 1 WHERE id = ?",
                     (objet, corps, message_id))
        journal.info("message %s corrigé à la main", message_id)
        return True
    return False


def approuver(conn, message_id, objet, corps):
    """Approbation humaine d'UN message, avec le texte exact affiché à l'écran.

    Les corrections en cours sont enregistrées d'abord, puis le message est approuvé ; toute autre
    version non envoyée du même prospect perd son approbation (un seul message approuvé à la fois).
    """
    m, p = _charger(conn, message_id)
    raison = raison_de_refus(conn, p, m)
    if raison:
        raise ErreurMessage(raison)
    objet, corps = _texte_valide(objet, corps)
    enregistrer_edition(conn, message_id, objet, corps)
    conn.execute(
        "UPDATE messages SET approuve_le = NULL, approuve_par_humain = 0 "
        "WHERE prospect_id = ? AND id <> ? AND approuve_le IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM envois e WHERE e.message_id = messages.id AND e.statut <> 'echec')",
        (p["id"], message_id))
    conn.execute(f"UPDATE messages SET approuve_le = {_MAINTENANT}, approuve_par_humain = 1 WHERE id = ?",
                 (message_id,))
    conn.execute("UPDATE prospects SET statut = 'approuve' WHERE id = ?", (p["id"],))
    journal.info("message %s approuvé (prospect %s)", message_id, p["id"])


def desapprouver(conn, message_id):
    """Retire l'approbation : le message redevient « à valider »."""
    m, p = _charger(conn, message_id)
    if _envoye(conn, message_id):
        raise ErreurMessage("Ce message a déjà été envoyé.")
    conn.execute("UPDATE messages SET approuve_le = NULL, approuve_par_humain = 0 WHERE id = ?", (message_id,))
    conn.execute("UPDATE prospects SET statut = 'message_pret' WHERE id = ? AND statut = 'approuve'", (p["id"],))
    journal.info("approbation du message %s retirée", message_id)


def definir_email(conn, prospect_id, email):
    """Saisie manuelle de l'email d'un prospect (trouvé à la main). Annule l'approbation en cours si l'adresse change."""
    p = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    if p is None:
        raise ErreurMessage("Prospect introuvable.")
    if p["statut"] in ("envoye", "repondu"):
        raise ErreurMessage("Ce prospect a déjà été contacté.")
    adresse = db.normaliser_email(email)
    if adresse is not None and not _RE_EMAIL.match(adresse):
        raise ErreurMessage("Adresse email invalide (exemple : nom@domaine.fr).")
    if adresse == p["email"]:
        return adresse
    if p["statut"] == "approuve":  # l'approbation portait sur une autre destination : à refaire
        conn.execute("UPDATE messages SET approuve_le = NULL, approuve_par_humain = 0 WHERE prospect_id = ?", (prospect_id,))
        conn.execute("UPDATE prospects SET statut = 'message_pret' WHERE id = ?", (prospect_id,))
    conn.execute("UPDATE prospects SET email = ? WHERE id = ?", (adresse, prospect_id))
    journal.info("email du prospect %s modifié à la main", prospect_id)
    return adresse


# --------------------------------------------------------------------------------------
# Auto-tests
# --------------------------------------------------------------------------------------


def _refus(fonction, *args):
    try:
        fonction(*args)
    except ErreurMessage as erreur:
        return str(erreur)
    return None


def _autotests():
    from core import campagnes

    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        chemin = Path(tmp) / "t.db"
        db.init_db(chemin, importer_env=False)
        with db.connexion(chemin) as c:
            cid = campagnes.creer(c, {"nom": "C", "secteur": "s", "pays": "FR", "langue": "fr"})
            ids, msgs = {}, {}
            for nom, email, score in (("A", "a@a.fr", 9), ("B", None, 7), ("C", "c@bloque.fr", 6), ("D", "d@gmail.com", 4)):
                db.inserer_prospect(c, {"campagne_id": cid, "nom": nom, "email": email, "ville": "Lyon",
                                        "source": "openstreetmap", "source_id": "osm:" + nom})
                ids[nom] = c.execute("SELECT id FROM prospects WHERE nom = ?", (nom,)).fetchone()[0]
                c.execute("UPDATE prospects SET problemes = '[\"lent\"]', score = ?, statut = 'qualifie' WHERE id = ?", (score, ids[nom]))
                msgs[nom] = c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'Objet', 'Corps v1', 1)",
                                      (ids[nom],)).lastrowid
                c.execute("UPDATE prospects SET statut = 'message_pret' WHERE id = ?", (ids[nom],))
            db.ajouter_liste_noire(c, "bloque.fr", "manuel")

            file = file_de_validation(c)
            verifier("file : plus gros score d'abord ; compteurs 4 en attente, 0 approuvé",
                     [f["nom"] for f in file] == ["A", "B", "C", "D"] and compteurs(c) == {"en_attente": 4, "approuves": 0})
            verifier("file : sans email et adresse générique signalés",
                     file[1]["a_email"] == 0 and file[3]["generique"] == 1)

            # --- Approbation ---
            approuver(c, msgs["A"], "Objet corrigé", "Corps corrigé à la main")
            m = dict(c.execute("SELECT * FROM messages WHERE id = ?", (msgs["A"],)).fetchone())
            statut = c.execute("SELECT statut FROM prospects WHERE id = ?", (ids["A"],)).fetchone()[0]
            verifier("approuver : corrections enregistrées (marquées humaines), message approuvé, prospect « approuve »",
                     m["objet"] == "Objet corrigé" and m["corps"] == "Corps corrigé à la main" and m["edite_par_humain"] == 1
                     and m["approuve_par_humain"] == 1 and m["approuve_le"] and statut == "approuve")
            verifier("compteurs après approbation : 3 en attente, 1 approuvé", compteurs(c) == {"en_attente": 3, "approuves": 1})
            verifier("approuver sans email : refusé avec explication (et le prospect reste « message_pret »)",
                     "pas d'email" in (_refus(approuver, c, msgs["B"], "O", "C") or "")
                     and c.execute("SELECT statut FROM prospects WHERE id = ?", (ids["B"],)).fetchone()[0] == "message_pret")
            verifier("approuver une adresse en liste noire : refusé", "liste noire" in (_refus(approuver, c, msgs["C"], "O", "C") or ""))
            verifier("approuver un texte vide : refusé", _refus(approuver, c, msgs["D"], "O", "   ") is not None
                     and _refus(approuver, c, msgs["D"], "  ", "C") is not None)
            verifier("l'approbation d'un message vide ou refusé n'a rien posé en base",
                     c.execute("SELECT COUNT(*) FROM messages WHERE approuve_par_humain = 1").fetchone()[0] == 1)

            # --- Correction d'un message approuvé : l'approbation tombe ---
            enregistrer_edition(c, msgs["A"], "Objet corrigé", "Corps retouché encore")
            m = dict(c.execute("SELECT approuve_le FROM messages WHERE id = ?", (msgs["A"],)).fetchone())
            statut = c.execute("SELECT statut FROM prospects WHERE id = ?", (ids["A"],)).fetchone()[0]
            verifier("corriger un message approuvé : approbation annulée, prospect de nouveau « à valider »",
                     m["approuve_le"] is None and statut == "message_pret")
            enregistrer_edition(c, msgs["A"], "Objet corrigé", "Corps retouché encore")  # idem : aucune modification
            approuver(c, msgs["A"], "Objet corrigé", "Corps retouché encore")

            # --- Versions : un seul message approuvé à la fois ---
            v2 = c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'O2', 'Corps v2', 2)",
                           (ids["A"],)).lastrowid
            statut = c.execute("SELECT statut FROM prospects WHERE id = ?", (ids["A"],)).fetchone()[0]
            verifier("régénération : l'approbation de la v1 tombe, prospect « message_pret »",
                     c.execute("SELECT approuve_le FROM messages WHERE id = ?", (msgs["A"],)).fetchone()[0] is None
                     and statut == "message_pret")
            approuver(c, msgs["A"], "Objet corrigé", "Corps retouché encore")  # on choisit la v1
            approuver(c, v2, "O2", "Corps v2")                                  # puis la v2
            approuves = [r[0] for r in c.execute("SELECT id FROM messages WHERE prospect_id = ? AND approuve_le IS NOT NULL", (ids["A"],))]
            verifier("deux versions : approuver l'une retire l'approbation de l'autre (un seul message lu par l'envoi)",
                     approuves == [v2])

            # --- Retirer l'approbation ---
            desapprouver(c, v2)
            verifier("retirer l'approbation : message et prospect redeviennent « à valider »",
                     c.execute("SELECT approuve_le FROM messages WHERE id = ?", (v2,)).fetchone()[0] is None
                     and c.execute("SELECT statut FROM prospects WHERE id = ?", (ids["A"],)).fetchone()[0] == "message_pret")

            # --- Déjà envoyé ---
            approuver(c, v2, "O2", "Corps v2")
            c.execute("INSERT INTO envois (message_id, statut) VALUES (?, 'envoye')", (v2,))
            verifier("message déjà envoyé : ni corrigé, ni ré-approuvé, ni dé-approuvé",
                     all(_refus(f, c, v2, "O", "C") for f in (approuver, enregistrer_edition))
                     and _refus(desapprouver, c, v2) is not None)
            c.execute("UPDATE envois SET statut = 'echec' WHERE message_id = ?", (v2,))
            verifier("envoi en échec : le message reste modifiable et approuvable (nouvel essai possible)",
                     _refus(enregistrer_edition, c, v2, "O2", "Corps v2") is None)

            # --- Email saisi à la main ---
            verifier("email invalide refusé", "invalide" in (_refus(definir_email, c, ids["B"], "pas un email") or ""))
            verifier("email saisi : normalisé (minuscules), puis le prospect devient approuvable",
                     definir_email(c, ids["B"], "  Dr.B@Cabinet-B.FR ") == "dr.b@cabinet-b.fr"
                     and _refus(approuver, c, msgs["B"], "O", "Corps B") is None)
            definir_email(c, ids["B"], "autre@cabinet-b.fr")
            verifier("email changé sur un prospect approuvé : approbation annulée (à refaire pour la nouvelle adresse)",
                     c.execute("SELECT statut FROM prospects WHERE id = ?", (ids["B"],)).fetchone()[0] == "message_pret"
                     and c.execute("SELECT approuve_le FROM messages WHERE id = ?", (msgs["B"],)).fetchone()[0] is None)

            # --- Revue ---
            r = revue(c, ids["A"], {"mention_desinscription": "STOP", "mots_max_message": "120"})
            verifier("revue : problèmes en clair avec poids, versions numérotées, mention de désinscription fournie à l'écran",
                     r["problemes"][0]["libelle"] == "Site lent à charger" and r["problemes"][0]["points"] == 3
                     and [v["version"] for v in r["versions"]] == [1, 2] and r["mention_desinscription"] == "STOP")
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
        print(compteurs(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
