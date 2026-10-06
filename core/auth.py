"""Accès à l'application : identifiant + mot de passe, session, protection contre les essais répétés.

Le mot de passe n'est JAMAIS stocké : seule son empreinte (hachage scrypt salé) est en base, dans la table
`reglages`. Au tout premier lancement, un mot de passe aléatoire est généré et affiché UNE fois dans la console.
Un mot de passe oublié se remplace avec : python -m core.auth reinitialiser

Utilisable seul : python -m core.auth reinitialiser [identifiant]   génère et affiche un nouveau mot de passe
                  python -m core.auth check                          tests sur base jetable
"""
import hashlib
import logging
import re
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from core import db

journal = logging.getLogger("prospect")

IDENTIFIANT_PAR_DEFAUT = "admin"
LONGUEUR_MINI_MOT_DE_PASSE = 10
ESSAIS_MAX = 5            # échecs tolérés...
FENETRE_S = 600           # ...dans cette fenêtre, sinon blocage jusqu'à la fin de la fenêtre
_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"  # sans caractères ambigus (0/O, 1/l/I)
_RE_IDENTIFIANT = re.compile(r"^[A-Za-z0-9._@-]{3,40}$")
_HACHAGE_BIDON = generate_password_hash("sans-importance")  # comparé quand l'identifiant est faux : même durée de calcul

_echecs = []
_verrou = threading.Lock()


class ErreurAcces(Exception):
    """Refus lisible (mot de passe trop court, identifiant invalide…)."""


def generer_mot_de_passe():
    """Mot de passe aléatoire lisible : quatre groupes de quatre caractères (environ 90 bits d'entropie)."""
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(4))


def _marquer_secret(conn, cle):
    conn.execute("UPDATE reglages SET secret = 1 WHERE cle = ?", (cle,))


def definir_acces(conn, identifiant, mot_de_passe):
    """Enregistre l'identifiant et l'empreinte du mot de passe."""
    identifiant = (identifiant or "").strip()
    if not _RE_IDENTIFIANT.match(identifiant):
        raise ErreurAcces("Identifiant : 3 à 40 caractères (lettres, chiffres, . _ - @).")
    if len(mot_de_passe or "") < LONGUEUR_MINI_MOT_DE_PASSE:
        raise ErreurAcces(f"Mot de passe : {LONGUEUR_MINI_MOT_DE_PASSE} caractères au minimum.")
    db.set_reglage(conn, "auth_identifiant", identifiant)
    db.set_reglage(conn, "auth_hash", generate_password_hash(mot_de_passe))
    _marquer_secret(conn, "auth_hash")
    journal.info("accès modifié : identifiant « %s »", identifiant)


def initialiser_si_absent(conn):
    """Au premier lancement : crée l'accès avec un mot de passe aléatoire. Renvoie (identifiant, mot de passe) ou None."""
    if db.get_reglage(conn, "auth_hash"):
        return None
    mot_de_passe = generer_mot_de_passe()
    definir_acces(conn, IDENTIFIANT_PAR_DEFAUT, mot_de_passe)
    return IDENTIFIANT_PAR_DEFAUT, mot_de_passe


def identifiant(conn):
    return db.get_reglage(conn, "auth_identifiant", "")


def verifier_acces(conn, identifiant_saisi, mot_de_passe):
    """Vrai si l'identifiant et le mot de passe sont les bons. Le temps de calcul ne révèle pas lequel est faux."""
    empreinte = db.get_reglage(conn, "auth_hash", "")
    bon_identifiant = secrets.compare_digest((identifiant_saisi or "").strip().encode(), identifiant(conn).encode())
    bon_mot_de_passe = check_password_hash(empreinte or _HACHAGE_BIDON, mot_de_passe or "")
    return bool(empreinte) and bon_identifiant and bon_mot_de_passe


def empreinte_session(conn):
    """Identifie l'accès courant : elle change dès que l'identifiant ou le mot de passe change, ce qui ferme
    toutes les sessions ouvertes."""
    brut = (identifiant(conn) + "|" + db.get_reglage(conn, "auth_hash", "")).encode()
    return hashlib.sha256(brut).hexdigest()[:24]


def cle_de_session(conn):
    """Clé de signature des cookies de session : aléatoire, créée une fois, gardée en base."""
    cle = db.get_reglage(conn, "cle_session")
    if not cle:
        cle = secrets.token_hex(32)
        db.set_reglage(conn, "cle_session", cle)
        _marquer_secret(conn, "cle_session")
    return cle


# --------------------------------------------------------------------------------------
# Limitation des essais
# --------------------------------------------------------------------------------------


def secondes_de_blocage(maintenant=None):
    """0 si on peut essayer ; sinon le nombre de secondes à attendre."""
    maintenant = maintenant if maintenant is not None else time.time()
    with _verrou:
        _echecs[:] = [t for t in _echecs if maintenant - t < FENETRE_S]
        if len(_echecs) >= ESSAIS_MAX:
            return int(FENETRE_S - (maintenant - _echecs[0])) + 1
    return 0


def noter_echec(maintenant=None):
    with _verrou:
        _echecs.append(maintenant if maintenant is not None else time.time())


def effacer_echecs():
    with _verrou:
        _echecs.clear()


# --------------------------------------------------------------------------------------
# Auto-tests et ligne de commande
# --------------------------------------------------------------------------------------


def _autotests():
    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        chemin = Path(tmp) / "t.db"
        db.init_db(chemin, importer_env=False)
        with db.connexion(chemin) as c:
            premier = initialiser_si_absent(c)
            verifier("premier lancement : accès créé avec un mot de passe aléatoire de 16 caractères + 3 tirets",
                     premier and premier[0] == "admin" and len(premier[1]) == 19 and premier[1].count("-") == 3)
            verifier("second lancement : rien n'est recréé ni écrasé", initialiser_si_absent(c) is None)
            stocke = db.get_reglage(c, "auth_hash")
            verifier("le mot de passe n'est stocké nulle part en clair : seulement une empreinte scrypt salée",
                     premier[1] not in stocke and stocke.startswith(("scrypt:", "pbkdf2:"))
                     and c.execute("SELECT secret FROM reglages WHERE cle = 'auth_hash'").fetchone()[0] == 1)
            verifier("bon identifiant + bon mot de passe : accepté", verifier_acces(c, "admin", premier[1]))
            verifier("mauvais mot de passe, mauvais identifiant, champs vides : refusés",
                     not verifier_acces(c, "admin", "faux") and not verifier_acces(c, "autre", premier[1])
                     and not verifier_acces(c, "", "") and not verifier_acces(c, None, None))
            e1 = empreinte_session(c)
            definir_acces(c, "gill", "un-mot-de-passe-long")
            verifier("changer l'accès : nouvel identifiant valide, l'ancien mot de passe ne marche plus, les sessions ouvertes tombent",
                     verifier_acces(c, "gill", "un-mot-de-passe-long") and not verifier_acces(c, "admin", premier[1])
                     and empreinte_session(c) != e1)
            for faux_id, faux_mdp, attendu in (("a", "un-mot-de-passe-long", "Identifiant"), ("gill", "court", "Mot de passe"),
                                               ("gill; DROP", "un-mot-de-passe-long", "Identifiant")):
                try:
                    definir_acces(c, faux_id, faux_mdp)
                    message = ""
                except ErreurAcces as e:
                    message = str(e)
                verifier(f"accès invalide refusé avec un message clair ({attendu})", attendu in message)
            cle = cle_de_session(c)
            verifier("clé de session : aléatoire (64 caractères), stable d'un appel à l'autre, marquée secrète",
                     len(cle) == 64 and cle_de_session(c) == cle
                     and c.execute("SELECT secret FROM reglages WHERE cle = 'cle_session'").fetchone()[0] == 1)

    effacer_echecs()
    t0 = 1000.0
    for i in range(ESSAIS_MAX - 1):
        noter_echec(t0 + i)
    verifier(f"{ESSAIS_MAX - 1} échecs : pas encore bloqué", secondes_de_blocage(t0 + 10) == 0)
    noter_echec(t0 + 10)
    attente = secondes_de_blocage(t0 + 20)
    verifier(f"{ESSAIS_MAX} échecs en {FENETRE_S // 60} min : bloqué, avec le temps d'attente restant", 0 < attente <= FENETRE_S)
    verifier("après la fenêtre de blocage : de nouveau autorisé", secondes_de_blocage(t0 + FENETRE_S + 50) == 0)
    effacer_echecs()
    mots = {generer_mot_de_passe() for _ in range(200)}
    verifier("200 mots de passe générés : tous différents, jamais de caractère ambigu (0 O 1 l I)",
             len(mots) == 200 and not any(c in m for m in mots for c in "0O1lI"))
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
    if len(argv) > 1 and argv[1] == "reinitialiser":
        nouveau = generer_mot_de_passe()
        with db.connexion() as conn:
            ident = argv[2] if len(argv) > 2 else (identifiant(conn) or IDENTIFIANT_PAR_DEFAUT)
            definir_acces(conn, ident, nouveau)
        print(f"Nouvel accès enregistré.\n  Identifiant  : {ident}\n  Mot de passe : {nouveau}\n"
              "Note-le maintenant : il n'est affiché qu'une fois. Tu pourras le changer dans Réglages.")
        return 0
    print("Usage : python -m core.auth reinitialiser [identifiant] | check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
