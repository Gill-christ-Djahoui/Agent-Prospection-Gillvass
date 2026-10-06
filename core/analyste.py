"""Analyste IA : demande à ton fournisseur d'IA de lire tes statistiques et de te conseiller.

Ce que l'IA reçoit : UNIQUEMENT des chiffres agrégés (taux par campagne, ville, problème, tranche de score, heure…),
les noms de tes campagnes, et le début du prompt de chaque angle. JAMAIS un nom de prospect, une adresse email,
un téléphone ou un site. Les conseils sont une aide à la décision, à vérifier : l'IA peut se tromper.

Utilisable seul : python -m core.analyste check   (tests, IA simulée)
"""
import json
import logging
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from core import analyses, campagnes, db, reglages, writer

journal = logging.getLogger("prospect")

HISTORIQUE_MAX = 8
_LIGNES_MAX = 10


class ErreurAnalyste(Exception):
    """Analyse impossible : le message est affiché tel quel."""


def _allege(groupes):
    """Garde l'essentiel d'un tableau (les plus gros groupes), sans libellé superflu."""
    return [{k: g[k] for k in ("cle", "contactes", "reponses", "taux_reponse", "taux_ouverture") if k in g}
            | ({"libelle": g["libelle"]} if "libelle" in g else {}) for g in groupes[:_LIGNES_MAX]]


def contexte(conn, cfg, jours=None, maintenant=None):
    """Les données agrégées envoyées à l'IA (aucune donnée personnelle sur un prospect)."""
    d = analyses.donnees_page(conn, cfg, jours, maintenant)
    angles = {}
    for l in conn.execute(
            "SELECT DISTINCT m.nom, m.prompt_systeme FROM modeles_message m JOIN campagnes c ON c.modele_message_id = m.id "
            "WHERE c.est_modele = 0"):
        angles[l["nom"]] = l["prompt_systeme"][:700]
    return {
        "periode": f"{jours} derniers jours" if jours else "tout l'historique",
        "indicateurs": d["kpis"],
        "par_campagne": _allege(d["tableaux"]["campagnes"]),
        "par_angle": _allege(d["tableaux"]["angles"]),
        "par_probleme_detecte": _allege(d["tableaux"]["problemes"]),
        "par_ville": _allege(d["tableaux"]["villes"]),
        "par_tranche_de_score": _allege(d["tableaux"]["tranches"]),
        "par_source_de_donnees": _allege(d["tableaux"]["sources"]),
        "par_heure_d_envoi": _allege([g for g in d["tableaux"]["heures"] if g["contactes"]]),
        "par_jour_d_envoi": _allege([g for g in d["tableaux"]["jours"] if g["contactes"]]),
        "decisions_deja_detectees_par_les_regles": [{"niveau": r["niveau"], "titre": r["titre"], "preuve": r["preuve"]}
                                                    for r in d["recommandations"]],
        "reglages_utiles": {k: cfg[k] for k in ("plafond_jour", "delai_relance_jours", "max_relances", "score_min_generation")},
        "debut_des_prompts_par_angle": angles,
    }


def _historique(conn):
    brut = db.get_reglage(conn, "analyste_rapports")
    return json.loads(brut) if brut else []


def historique(conn):
    return _historique(conn)


def analyser(conn, cfg, jours=None, question=None, maintenant=None):
    """Interroge l'IA sur tes statistiques. Renvoie le rapport {'date', 'jours', 'question', 'texte'} (aussi mémorisé)."""
    manquants = [nom for nom, cle in (("l'endpoint", "ai_endpoint"), ("le modèle", "ai_model"), ("la clé", "ai_key"))
                 if not (cfg.get(cle) or "").strip()]
    if manquants:
        raise ErreurAnalyste("Fournisseur d'IA non configuré : il manque " + ", ".join(manquants) + " (Réglages).")
    donnees = contexte(conn, cfg, jours, maintenant)
    if donnees["indicateurs"]["contactes"] == 0:
        raise ErreurAnalyste("Aucun email envoyé pour l'instant : il n'y a encore rien à analyser.")
    question = (question or "").strip()[:600]
    demande = "Statistiques (JSON) :\n" + json.dumps(donnees, ensure_ascii=False, indent=1)
    demande += ("\n\nQuestion de l'utilisateur : " + question) if question else "\n\nFais l'analyse complète demandée."
    try:
        texte = writer.appeler_ia(cfg, [{"role": "system", "content": cfg["prompt_analyste"]},
                                        {"role": "user", "content": demande}])
    except writer.ErreurRedaction as erreur:
        raise ErreurAnalyste(str(erreur)) from erreur
    rapport = {"date": (maintenant or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"), "jours": jours,
               "question": question, "texte": texte.strip()}
    db.set_reglage(conn, "analyste_rapports", json.dumps(([rapport] + _historique(conn))[:HISTORIQUE_MAX], ensure_ascii=False))
    journal.info("analyse IA demandée (%s, %d caractères)", donnees["periode"], len(rapport["texte"]))
    return rapport


def _autotests():
    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    appels = []
    ia_reelle = writer.appeler_ia

    def fausse_ia(cfg, messages):
        appels.append(messages)
        return "  Diagnostic : ça démarre.  \n1. Continue."
    writer.appeler_ia = fausse_ia
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            chemin = Path(tmp) / "t.db"
            db.init_db(chemin, importer_env=False)
            with db.connexion(chemin) as c:
                cfg = reglages.valeurs_effectives(c)
                try:
                    analyser(c, cfg)
                    message = ""
                except ErreurAnalyste as e:
                    message = str(e)
                verifier("IA non configurée : message clair, aucun appel", "non configuré" in message and not appels)
                for k, v in (("ai_endpoint", "https://ia.test/v1/chat"), ("ai_model", "m"), ("ai_key", "k")):
                    db.set_reglage(c, k, v)
                cfg = reglages.valeurs_effectives(c)
                try:
                    analyser(c, cfg)
                    message = ""
                except ErreurAnalyste as e:
                    message = str(e)
                verifier("aucun email envoyé : refus explicite (rien à analyser), aucun appel à l'IA", "rien à analyser" in message and not appels)

                cid = campagnes.creer(c, {"nom": "Dentistes Lyon", "secteur": "s", "pays": "FR", "langue": "fr"})
                for i in range(3):
                    db.inserer_prospect(c, {"campagne_id": cid, "nom": f"Dr Secret{i}", "email": f"secret{i}@cabinet.fr", "telephone": "0612345678",
                                            "site": f"https://secret{i}.fr", "ville": "Lyon", "source": "openstreetmap", "source_id": f"o:{i}"})
                    pid = c.execute("SELECT id FROM prospects WHERE source_id = ?", (f"o:{i}",)).fetchone()[0]
                    c.execute("UPDATE prospects SET statut = 'envoye', problemes = '[\"pas_rdv\"]', score = 8 WHERE id = ?", (pid,))
                    mid = c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'o', 'c', 1)", (pid,)).lastrowid
                    c.execute("INSERT INTO envois (message_id, statut) VALUES (?, ?)", (mid, "repondu" if i == 0 else "envoye"))
                r = analyser(c, cfg, 30, "Quel angle changer ?")
                demande = appels[-1][1]["content"]
                verifier("rapport renvoyé (texte nettoyé), mémorisé, avec la question et la période",
                         r["texte"].startswith("Diagnostic") and r["question"] == "Quel angle changer ?" and r["jours"] == 30
                         and historique(c)[0]["texte"] == r["texte"])
                verifier("CONFIDENTIALITÉ : ni nom de prospect, ni email, ni téléphone, ni site n'est envoyé à l'IA",
                         not re.search(r"@|Secret|0612345678|secret\d\.fr", appels[-1][0]["content"] + demande))
                verifier("l'IA reçoit les chiffres agrégés, le nom de la campagne, la question et le prompt de l'analyste",
                         '"contactes": 3' in demande and "Dentistes Lyon" in demande and "Quel angle changer ?" in demande
                         and appels[-1][0]["content"] == cfg["prompt_analyste"])
                for _ in range(10):
                    analyser(c, cfg)
                verifier(f"historique limité aux {HISTORIQUE_MAX} derniers rapports, le plus récent d'abord", len(historique(c)) == HISTORIQUE_MAX)

                def en_panne(cfg_, messages):
                    raise writer.ErreurRedaction("Le fournisseur d'IA refuse la requête (HTTP 401) : Invalid API Key")
                writer.appeler_ia = en_panne
                try:
                    analyser(c, cfg)
                    message = ""
                except ErreurAnalyste as e:
                    message = str(e)
                verifier("l'IA échoue : le message réel du fournisseur est affiché, l'historique n'est pas altéré",
                         "Invalid API Key" in message and len(historique(c)) == HISTORIQUE_MAX)
    finally:
        writer.appeler_ia = ia_reelle
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
    print("Usage : python -m core.analyste check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
