"""File d'attente et suivi des tâches longues (table `jobs`).

Un seul fil de travail traite les tâches l'une après l'autre : elles ne se marchent pas dessus
(services publics partagés, un seul scan à la fois). Chaque tâche écrit son avancement RÉEL
dans `jobs` ; l'interface interroge cette table.

Utilisable seul : python -m core.jobs   (liste les 20 dernières tâches)
"""
import logging
import queue
import sys
import threading

from core import db

journal = logging.getLogger("prospect")

_file = queue.Queue()
_verrou = threading.Lock()
_fil = None

STATUTS_ACTIFS = ("en_attente", "en_cours")
_MAINTENANT = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


def soumettre(type_job, campagne_id, fonction, *args):
    """Crée la tâche (en_attente) et la met en file. Renvoie son id.

    `fonction(job_id, *args)` renvoie {'statut': 'termine'|'erreur', 'detail': str}. Une exception
    non prévue marque la tâche en erreur avec son message, jamais en silence.
    """
    with db.connexion() as conn:
        job_id = conn.execute(
            "INSERT INTO jobs (type, campagne_id, statut, progression, detail) "
            "VALUES (?, ?, 'en_attente', 0, 'En attente de démarrage')", (type_job, campagne_id)).lastrowid
    _file.put((job_id, fonction, args))
    _demarrer_fil()
    return job_id


def _demarrer_fil():
    global _fil
    with _verrou:
        if _fil is None or not _fil.is_alive():
            _fil = threading.Thread(target=_boucle, name="jobs", daemon=True)
            _fil.start()


def _boucle():
    while True:
        job_id, fonction, args = _file.get()
        try:
            _executer(job_id, fonction, args)
        finally:
            _file.task_done()


def _executer(job_id, fonction, args):
    with db.connexion() as conn:
        conn.execute(f"UPDATE jobs SET statut = 'en_cours', demarre_le = {_MAINTENANT} WHERE id = ?", (job_id,))
    journal.info("job %s démarré", job_id)
    try:
        resultat = fonction(job_id, *args)
        statut, detail = resultat["statut"], resultat["detail"]
    except Exception as erreur:  # une tâche qui plante doit le dire, pas rester « en cours »
        journal.exception("job %s en erreur", job_id)
        statut, detail = "erreur", f"Erreur inattendue : {type(erreur).__name__} : {erreur}"
    with db.connexion() as conn:
        progression = "100" if statut == "termine" else "progression"
        conn.execute(
            f"UPDATE jobs SET statut = ?, detail = ?, progression = {progression}, "
            f"fini_le = {_MAINTENANT} WHERE id = ?", (statut, detail, job_id))
    journal.info("job %s terminé : %s", job_id, statut)


def maj(job_id, progression=None, detail=None):
    """Écrit l'avancement d'une tâche (appelé par la tâche elle-même)."""
    with db.connexion() as conn:
        if progression is not None:
            conn.execute("UPDATE jobs SET progression = ? WHERE id = ?",
                         (max(0, min(100, int(progression))), job_id))
        if detail is not None:
            conn.execute("UPDATE jobs SET detail = ? WHERE id = ?", (detail, job_id))


def lire(conn, job_id):
    ligne = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(ligne) if ligne else None


def dernier(conn, campagne_id, type_job=None):
    """Dernière tâche de la campagne (de ce type, ou de n'importe quel type)."""
    ligne = conn.execute(
        "SELECT * FROM jobs WHERE campagne_id = ? AND (? IS NULL OR type = ?) ORDER BY id DESC LIMIT 1",
        (campagne_id, type_job, type_job)).fetchone()
    return dict(ligne) if ligne else None


def dernier_du_type(conn, type_job):
    """Dernière tâche d'un type, toutes campagnes confondues."""
    ligne = conn.execute("SELECT * FROM jobs WHERE type = ? ORDER BY id DESC LIMIT 1", (type_job,)).fetchone()
    return dict(ligne) if ligne else None


def actif(conn, campagne_id, type_job=None):
    """Tâche en attente ou en cours pour la campagne (de ce type, ou de n'importe quel type), ou None."""
    ligne = conn.execute(
        "SELECT * FROM jobs WHERE campagne_id = ? AND (? IS NULL OR type = ?) "
        "AND statut IN ('en_attente', 'en_cours') ORDER BY id DESC LIMIT 1",
        (campagne_id, type_job, type_job)).fetchone()
    return dict(ligne) if ligne else None


def marquer_interrompus(conn):
    """Au démarrage : une tâche restée en cours n'a plus de fil pour la traiter, on le dit."""
    curseur = conn.execute(
        f"UPDATE jobs SET statut = 'erreur', fini_le = {_MAINTENANT}, "
        "detail = 'Interrompue : l''application a été arrêtée pendant ce traitement. Relance-la.' "
        "WHERE statut IN ('en_attente', 'en_cours')")
    if curseur.rowcount:
        journal.warning("%d tâche(s) interrompue(s) au redémarrage", curseur.rowcount)
    return curseur.rowcount


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with db.connexion() as conn:
        for j in conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 20"):
            print(f"#{j['id']} {j['type']} [{j['statut']}] {j['progression']}% — {j['detail']}")
