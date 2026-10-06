"""Sauvegardes de la base (copie cohérente de data/prospect.db, même pendant que l'application tourne).

Une sauvegarde automatique est faite au lancement si la dernière date de plus de 24 h. Les plus anciennes sont
supprimées au-delà de `sauvegardes_max`. ⚠ Une sauvegarde contient tes clés API en clair, comme la base : même
précautions (ne la mets pas dans un cloud public, ne la partage pas).

Restaurer : ferme l'application, remplace data/prospect.db par la sauvegarde choisie, relance.

Utilisable seul : python -m core.sauvegarde          crée une sauvegarde et liste les existantes
                  python -m core.sauvegarde check    tests sur base jetable
"""
import logging
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from core import db

journal = logging.getLogger("prospect")

DELAI_AUTO_S = 24 * 3600
_PREFIXE = "prospect-"


def dossier():
    return config.DB_PATH.parent / "sauvegardes"  # à côté de la base utilisée (jamais dans le dossier d'une autre)


def lister(dossier_sauvegardes=None):
    """Sauvegardes existantes, la plus récente d'abord : [{'nom', 'taille', 'date'}]."""
    d = Path(dossier_sauvegardes) if dossier_sauvegardes else dossier()
    if not d.exists():
        return []
    fichiers = sorted(d.glob(_PREFIXE + "*.db"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"nom": f.name, "taille": f.stat().st_size,
             "date": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")} for f in fichiers]


def sauvegarder(source=None, destination=None, maximum=10):
    """Copie la base avec l'API de sauvegarde de SQLite (cohérente même en cours d'écriture). Renvoie le nom du fichier."""
    source = Path(source) if source else config.DB_PATH
    destination = Path(destination) if destination else dossier()
    destination.mkdir(parents=True, exist_ok=True)
    nom = _PREFIXE + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".db"
    cible = destination / nom
    origine = sqlite3.connect(source)
    copie = sqlite3.connect(cible)
    try:
        origine.backup(copie)
    finally:
        copie.close()
        origine.close()
    anciennes = lister(destination)[maximum:] if maximum else []
    for ancienne in anciennes:
        (destination / ancienne["nom"]).unlink(missing_ok=True)
    journal.info("sauvegarde créée : %s (%d ancienne(s) supprimée(s))", nom, len(anciennes))
    return nom


def sauvegarde_auto_necessaire(dossier_sauvegardes=None, maintenant=None):
    """Vrai si aucune sauvegarde n'existe ou si la dernière a plus de 24 h."""
    existantes = lister(dossier_sauvegardes)
    if not existantes:
        return True
    derniere = datetime.strptime(existantes[0]["date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return ((maintenant or datetime.now(timezone.utc)) - derniere).total_seconds() > DELAI_AUTO_S


def sauvegarde_auto(maximum=10):
    """Au lancement : sauvegarde si besoin et si la base contient autre chose que les données de départ."""
    if not config.DB_PATH.exists() or not sauvegarde_auto_necessaire():
        return None
    with db.connexion() as conn:
        if conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 0 and \
                conn.execute("SELECT COUNT(*) FROM campagnes WHERE est_modele = 0").fetchone()[0] == 0:
            return None  # rien d'autre que les modèles de départ : inutile
    return sauvegarder(maximum=maximum)


def _autotests():
    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        base = Path(tmp) / "t.db"
        cible = Path(tmp) / "sauv"
        db.init_db(base, importer_env=False)
        with db.connexion(base) as c:
            db.set_reglage(c, "mon_site", "https://exemple.fr")
        nom = sauvegarder(base, cible, maximum=3)
        copie = sqlite3.connect(cible / nom)
        valeur = copie.execute("SELECT valeur FROM reglages WHERE cle = 'mon_site'").fetchone()[0]
        tables = {l[0] for l in copie.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        copie.close()
        verifier("sauvegarde : copie complète et lisible de la base (contenu et toutes les tables retrouvés)",
                 valeur == "https://exemple.fr" and tables == db.TABLES_ATTENDUES)
        verifier("sauvegarde : le fichier est listé", [s["nom"] for s in lister(cible)] == [nom])
        for i in range(4):
            time.sleep(1.05)
            sauvegarder(base, cible, maximum=3)
        verifier("rotation : seules les 3 sauvegardes les plus récentes sont conservées", len(lister(cible)) == 3 and nom not in [s["nom"] for s in lister(cible)])
        verifier("sauvegarde automatique : inutile juste après une sauvegarde, nécessaire s'il n'y en a aucune ou si elle a plus de 24 h",
                 not sauvegarde_auto_necessaire(cible) and sauvegarde_auto_necessaire(Path(tmp) / "vide")
                 and sauvegarde_auto_necessaire(cible, datetime.now(timezone.utc).replace(year=datetime.now().year + 1)))
        # la base source n'a pas été modifiée par la sauvegarde
        with db.connexion(base) as c:
            verifier("la sauvegarde ne modifie pas la base d'origine", db.get_reglage(c, "mon_site") == "https://exemple.fr")
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
    print("Sauvegarde créée :", sauvegarder())
    for s in lister():
        print(f"  {s['nom']}  {s['taille'] // 1024} Ko")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
