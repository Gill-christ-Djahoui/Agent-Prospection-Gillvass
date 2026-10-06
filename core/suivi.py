"""Suivi après envoi : ouvertures, rebonds, désinscriptions (via Brevo), réponses, file de relance.

- Les ouvertures passent `envois.statut` de « envoye » à « ouvert ».
- Un rebond définitif ou une désinscription entre IMMÉDIATEMENT en liste noire (toutes campagnes).
- Les réponses arrivent dans ta boîte mail : tu marques le prospect « repondu » à la main.
- Un prospect sans réponse après `delai_relance_jours` remonte dans la file de relance ; la relance suit
  le même chemin que le premier message (génération, validation humaine, envoi). Deux relances maximum.

Utilisable seul : python -m core.suivi check
"""
import json
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from core import db, http, reglages

journal = logging.getLogger("prospect")

URL_EVENEMENTS = "https://api.brevo.com/v3/smtp/statistics/events"
_LIMITE_PAGE = 2500
_PAGES_MAX = 20
# Événements Brevo suivis : (nom de l'événement Brevo) -> ce qu'on en fait
EVENEMENTS = {"opened": "ouverture", "hardBounces": "rebond", "invalid": "rebond",
              "unsubscribed": "desinscription", "spam": "desinscription"}
_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ErreurSuivi(Exception):
    """Échec de la synchronisation avec Brevo : message affiché tel quel."""


def _id_brevo(valeur):
    return (valeur or "").strip().strip("<>").lower()


def _evenements(cfg, evenement, debut, fin):
    timeout = int(cfg["http_timeout_s"])
    resultats, decalage = [], 0
    for _ in range(_PAGES_MAX):
        try:
            reponse = http.requete(
                "GET", URL_EVENEMENTS, timeout=timeout,
                headers={"api-key": cfg["brevo_key"].strip(), "accept": "application/json"},
                params={"event": evenement, "startDate": debut, "endDate": fin, "limit": _LIMITE_PAGE,
                        "offset": decalage, "sort": "desc"})
        except requests.RequestException as erreur:
            raise ErreurSuivi(http.message_erreur_reseau(erreur, timeout)) from erreur
        if not reponse.ok:
            raise ErreurSuivi(f"Brevo refuse la requête de suivi (HTTP {reponse.status_code}) : {http.resume_erreur(reponse)}")
        try:
            page = reponse.json().get("events", [])
        except ValueError:
            raise ErreurSuivi("Réponse de suivi illisible.") from None
        resultats += page
        if len(page) < _LIMITE_PAGE:
            return resultats
        decalage += _LIMITE_PAGE
    return resultats


def synchroniser_brevo(cfg, maintenant=None):
    """Récupère ouvertures, rebonds et désinscriptions depuis Brevo. Renvoie un compte rendu.

    Lève ErreurSuivi si Brevo est injoignable ou refuse (clé absente comprise).
    """
    if not (cfg.get("brevo_key") or "").strip():
        raise ErreurSuivi("Aucune clé Brevo : le suivi est impossible.")
    maintenant = maintenant or datetime.now(timezone.utc)
    with db.connexion() as conn:
        envois = [dict(l) for l in conn.execute(
            "SELECT id, brevo_id, statut, envoye_le FROM envois WHERE brevo_id IS NOT NULL AND statut IN ('envoye', 'ouvert')")]
    if not envois:
        return {"ouvertures": 0, "rebonds": 0, "desinscriptions": 0, "message": "Aucun envoi à suivre."}
    plus_ancien = min(datetime.strptime(e["envoye_le"], _FORMAT).replace(tzinfo=timezone.utc) for e in envois)
    debut = max(plus_ancien, maintenant - timedelta(days=89)).strftime("%Y-%m-%d")  # Brevo limite l'historique à 90 jours
    fin = maintenant.strftime("%Y-%m-%d")  # Brevo refuse une date de fin postérieure à aujourd'hui (HTTP 400)
    par_id = {_id_brevo(e["brevo_id"]): e for e in envois}

    compte = {"ouvertures": 0, "rebonds": 0, "desinscriptions": 0}
    for evenement, action in EVENEMENTS.items():
        for ev in _evenements(cfg, evenement, debut, fin):
            envoi = par_id.get(_id_brevo(ev.get("messageId")))
            with db.connexion() as conn:
                if action == "ouverture":
                    if envoi and envoi["statut"] == "envoye":
                        conn.execute("UPDATE envois SET statut = 'ouvert' WHERE id = ? AND statut = 'envoye'", (envoi["id"],))
                        envoi["statut"] = "ouvert"
                        compte["ouvertures"] += 1
                    continue
                adresse = db.normaliser_email(ev.get("email"))
                if not adresse:
                    continue
                motif = "rebond" if action == "rebond" else "desinscription"
                if db.ajouter_liste_noire(conn, adresse, motif):
                    compte["rebonds" if action == "rebond" else "desinscriptions"] += 1
                    journal.warning("liste noire (%s) : %s", motif, adresse)
                if action == "rebond" and envoi:
                    conn.execute("UPDATE envois SET erreur = 'Rebond définitif : adresse mise en liste noire' WHERE id = ?",
                                 (envoi["id"],))
    with db.connexion() as conn:
        db.set_reglage(conn, "suivi_derniere_sync", maintenant.strftime(_FORMAT))
    compte["message"] = (f"{compte['ouvertures']} ouverture(s), {compte['rebonds']} rebond(s) et "
                         f"{compte['desinscriptions']} désinscription(s) ajouté(s) en liste noire.")
    return compte


def marquer_repondu(conn, prospect_id):
    """L'utilisateur a reçu une réponse dans sa boîte mail : le prospect devient « repondu » (plus de relance)."""
    p = conn.execute("SELECT statut FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    if p is None:
        raise ValueError("Prospect introuvable.")
    if p["statut"] != "envoye":
        raise ValueError("Seul un prospect auquel un message a été envoyé peut être marqué « répondu ».")
    conn.execute(
        "UPDATE envois SET statut = 'repondu' WHERE id = (SELECT e.id FROM envois e JOIN messages m ON m.id = e.message_id "
        "WHERE m.prospect_id = ? AND e.statut IN ('envoye', 'ouvert') ORDER BY e.id DESC LIMIT 1)", (prospect_id,))
    conn.execute("UPDATE prospects SET statut = 'repondu' WHERE id = ?", (prospect_id,))
    journal.info("prospect %s marqué comme ayant répondu", prospect_id)


# --------------------------------------------------------------------------------------
# File de relance
# --------------------------------------------------------------------------------------


def file_de_relance(conn, cfg, maintenant=None):
    """Prospects sans réponse depuis `delai_relance_jours`, avec au plus `max_relances` relances déjà faites.

    `numero` = numéro de la relance à préparer (1 = première relance).
    """
    limite = ((maintenant or datetime.now(timezone.utc)) - timedelta(days=int(cfg["delai_relance_jours"]))).strftime(_FORMAT)
    lignes = conn.execute(
        """SELECT p.id, p.nom, p.email, p.campagne_id, c.nom AS campagne_nom,
                  MAX(e.envoye_le) AS dernier_envoi, COUNT(e.id) AS nb_envoyes
             FROM prospects p JOIN messages m ON m.prospect_id = p.id
             JOIN envois e ON e.message_id = m.id AND e.statut IN ('envoye', 'ouvert')
             JOIN campagnes c ON c.id = p.campagne_id
            WHERE p.statut = 'envoye'
            GROUP BY p.id HAVING MAX(e.envoye_le) <= ? AND COUNT(e.id) <= ?
            ORDER BY MAX(e.envoye_le)""", (limite, int(cfg["max_relances"]))).fetchall()
    file = []
    for l in lignes:
        if db.est_en_liste_noire(conn, l["email"]):
            continue
        file.append({**dict(l), "numero": l["nb_envoyes"]})
    return file


def relance_possible(conn, cfg, prospect_id, maintenant=None):
    """La ligne de la file de relance de ce prospect, ou None s'il n'y est pas."""
    return next((r for r in file_de_relance(conn, cfg, maintenant) if r["id"] == prospect_id), None)


# --------------------------------------------------------------------------------------
# Auto-tests
# --------------------------------------------------------------------------------------


class _Reponse:
    def __init__(self, evenements=None, statut=200, brut=None):
        self.status_code, self.ok = statut, statut < 400
        self._json = brut if brut is not None else {"events": evenements or []}
        self.text = json.dumps(self._json)

    def json(self):
        return self._json


def _autotests():
    import config
    from core import campagnes

    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    http_reel, chemin_reel = http.requete, config.DB_PATH
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        config.DB_PATH = Path(tmp) / "t.db"
        try:
            db.init_db(importer_env=False)
            maintenant = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
            with db.connexion() as c:
                cid = campagnes.creer(c, {"nom": "C", "secteur": "s", "pays": "FR", "langue": "fr"})
                db.set_reglage(c, "brevo_key", "k")
                ids, envois = {}, {}
                specs = [("A", "a@a.fr", "2026-10-01T09:00:00Z", 1), ("B", "b@b.fr", "2026-10-01T09:00:00Z", 1),
                         ("C", "c@c.fr", "2026-10-18T09:00:00Z", 1), ("D", "d@d.fr", "2026-10-01T09:00:00Z", 3),
                         ("E", "e@e.fr", "2026-10-01T09:00:00Z", 2), ("F", "f@f.fr", "2026-10-01T09:00:00Z", 1)]
                for nom, email, date, nb in specs:
                    db.inserer_prospect(c, {"campagne_id": cid, "nom": nom, "email": email, "ville": nom, "source": "openstreetmap", "source_id": "o:" + nom})
                    pid = ids[nom] = c.execute("SELECT id FROM prospects WHERE nom = ?", (nom,)).fetchone()[0]
                    c.execute("UPDATE prospects SET statut = 'qualifie', problemes = '[\"lent\"]', score = 5 WHERE id = ?", (pid,))
                    for k in range(nb):
                        mid = c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'O', 'C', ?)", (pid, k + 1)).lastrowid
                        envois[(nom, k)] = c.execute(
                            "INSERT INTO envois (message_id, statut, brevo_id, envoye_le) VALUES (?, 'envoye', ?, ?)",
                            (mid, f"<{nom.lower()}{k}@brevo>", date)).lastrowid
                    c.execute("UPDATE prospects SET statut = 'envoye' WHERE id = ?", (pid,))
                db.ajouter_liste_noire(c, "f@f.fr", "manuel")
                cfg = reglages.valeurs_effectives(c)

            # --- File de relance ---
            with db.connexion() as c:
                file = {r["nom"]: r for r in file_de_relance(c, cfg, maintenant)}
            verifier("relance : A, B (1 message) et E (2 messages), envoyés il y a 19 jours, remontent",
                     set(file) == {"A", "B", "E"})
            verifier("relance : numéro de relance = nombre de messages déjà envoyés (E a déjà reçu 2 messages -> relance n°2)",
                     file["A"]["numero"] == 1 and file["E"]["numero"] == 2)
            verifier("relance : D (3 messages = 1 + 2 relances) : abandon définitif, absent de la file",
                     "D" not in file and "C" not in file and "F" not in file)
            with db.connexion() as c:
                verifier("relance_possible : cohérent avec la file", relance_possible(c, cfg, ids["A"], maintenant) is not None
                         and relance_possible(c, cfg, ids["D"], maintenant) is None)

            # --- Synchronisation Brevo ---
            requetes = []

            def faux(methode, url, *, timeout, **kw):
                requetes.append(kw["params"])
                if kw["params"]["endDate"] > maintenant.strftime("%Y-%m-%d"):  # la règle réelle de Brevo
                    return _Reponse(statut=400, brut={"message": "End date should not be greater than current date"})
                ev = kw["params"]["event"]
                donnees = {
                    "opened": [{"messageId": "<a0@brevo>", "email": "a@a.fr"}, {"messageId": "<inconnu@brevo>", "email": "x@x.fr"}],
                    "hardBounces": [{"messageId": "<b0@brevo>", "email": "B@B.fr"}],
                    "unsubscribed": [{"messageId": "<c0@brevo>", "email": "c@c.fr"}],
                }.get(ev, [])
                return _Reponse(donnees)
            http.requete = faux
            r = synchroniser_brevo(cfg, maintenant)
            with db.connexion() as c:
                statuts = {n: c.execute("SELECT statut, erreur FROM envois WHERE id = ?", (envois[(n, 0)],)).fetchone() for n in "ABC"}
                noire = {l["valeur"]: l["motif"] for l in c.execute("SELECT valeur, motif FROM liste_noire")}
            verifier("suivi : ouverture reconnue par l'identifiant Brevo (avec ou sans chevrons) ; envoi inconnu ignoré",
                     statuts["A"]["statut"] == "ouvert" and r["ouvertures"] == 1)
            verifier("suivi : rebond définitif -> adresse (normalisée) en liste noire « rebond », envoi annoté, statut inchangé",
                     noire.get("b@b.fr") == "rebond" and "Rebond" in statuts["B"]["erreur"] and statuts["B"]["statut"] == "envoye")
            verifier("suivi : désinscription -> liste noire « desinscription » immédiatement", noire.get("c@c.fr") == "desinscription")
            verifier("suivi : 5 types d'événements interrogés, fenêtre depuis le plus ancien envoi (jamais plus de 90 jours)",
                     {q["event"] for q in requetes} == set(EVENEMENTS) and all(q["startDate"] == "2026-10-01" for q in requetes))
            verifier("suivi : date de fin = aujourd'hui, jamais demain (Brevo répond 400 sinon)",
                     all(q["endDate"] == "2026-10-20" for q in requetes))
            with db.connexion() as c:
                verifier("suivi : date de dernière synchronisation enregistrée", db.get_reglage(c, "suivi_derniere_sync") == "2026-10-20T12:00:00Z")
                cfg2 = reglages.valeurs_effectives(c)
                file2 = {r["nom"] for r in file_de_relance(c, cfg2, maintenant)}
            verifier("suivi : un prospect désinscrit/rebondi sort aussitôt de la file de relance", "B" not in file2 and "A" in file2)
            r2 = synchroniser_brevo(cfg, maintenant)
            verifier("suivi : rejouable sans doublon (idempotent)", r2["ouvertures"] == 0 and r2["rebonds"] == 0)

            http.requete = lambda *a, **k: _Reponse(statut=401, brut={"message": "Key not found"})
            try:
                synchroniser_brevo(cfg, maintenant)
                message = ""
            except ErreurSuivi as e:
                message = str(e)
            verifier("Brevo refuse : erreur explicite avec le message réel", "Key not found" in message)
            try:
                synchroniser_brevo({**cfg, "brevo_key": ""}, maintenant)
                message = ""
            except ErreurSuivi as e:
                message = str(e)
            verifier("sans clé Brevo : erreur explicite, aucun appel", "Aucune clé" in message)

            # --- Réponse ---
            with db.connexion() as c:
                marquer_repondu(c, ids["A"])
                a = c.execute("SELECT statut FROM prospects WHERE id = ?", (ids["A"],)).fetchone()[0]
                e = c.execute("SELECT statut FROM envois WHERE id = ?", (envois[("A", 0)],)).fetchone()[0]
                verifier("marquer « répondu » : prospect et dernier envoi mis à jour, plus de relance",
                         a == "repondu" and e == "repondu" and relance_possible(c, cfg, ids["A"], maintenant) is None)
                try:
                    marquer_repondu(c, ids["A"])
                    refus = False
                except ValueError:
                    refus = True
                verifier("marquer « répondu » deux fois ou sans envoi : refusé", refus)
        finally:
            http.requete, config.DB_PATH = http_reel, chemin_reel
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
        for r in file_de_relance(conn, reglages.valeurs_effectives(conn)):
            print(f"relance n°{r['numero']} : {r['nom']} (dernier envoi {r['dernier_envoi']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
