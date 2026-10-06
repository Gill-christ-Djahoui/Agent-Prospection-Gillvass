"""Calcul du score de besoin à partir des critères en base.

Le score est la somme des points des critères détectés ET actifs, avec les poids de la campagne
(sinon les poids globaux). On stocke le score BRUT : l'affichage le plafonne à 10, le tri se fait
sur le brut (un prospect sans site ET sans avis passe devant un prospect sans site seul).

Utilisable seul : python -m core.scorer ID   recalcule les scores de la campagne ID
"""
import json
import logging
import sys

from core import campagnes, db

journal = logging.getLogger("prospect")

PLAFOND_AFFICHAGE = 10  # le score affiché est min(score brut, 10) ; réglage d'affichage, pas de calcul


def poids_effectifs(conn, campagne_id):
    """{code: points} des critères actifs de la campagne (poids de campagne, sinon globaux)."""
    return {c["code"]: c["points"] for c in campagnes.criteres_effectifs(conn, campagne_id) if c["actif"]}


def calculer_score(conn, campagne_id, codes, poids=None):
    poids = poids if poids is not None else poids_effectifs(conn, campagne_id)
    return sum(poids.get(code, 0) for code in codes)


def recalculer(conn, campagne_id):
    """Recalcule le score de tous les prospects analysés de la campagne, sans retélécharger les sites.

    Renvoie {'prospects': nombre examiné, 'modifies': nombre dont le score a changé}.
    """
    poids = poids_effectifs(conn, campagne_id)
    examines = modifies = 0
    for ligne in conn.execute(
            "SELECT id, problemes, score FROM prospects WHERE campagne_id = ? AND statut <> 'nouveau'",
            (campagne_id,)).fetchall():
        nouveau = calculer_score(conn, campagne_id, json.loads(ligne["problemes"]), poids)
        examines += 1
        if nouveau != ligne["score"]:
            conn.execute("UPDATE prospects SET score = ? WHERE id = ?", (nouveau, ligne["id"]))
            modifies += 1
    journal.info("scores recalculés campagne %s : %d examinés, %d modifiés", campagne_id, examines, modifies)
    return {"prospects": examines, "modifies": modifies}


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print("Usage : python -m core.scorer ID_CAMPAGNE")
        sys.exit(2)
    with db.connexion() as conn:
        print(recalculer(conn, int(sys.argv[1])))
