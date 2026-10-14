"""Canal « formulaire de contact » : pour les prospects qui n'ont pas d'email mais dont le site a un formulaire.

Rien n'est envoyé par l'application : elle te donne la liste, la page du formulaire et un message prêt à coller ; tu le
colles toi-même dans le formulaire, puis tu notes le résultat. Ça marche depuis n'importe quel pays.

Le formulaire est repéré pendant l'exploration des sites (scan, analyse, import CSV, ou « chercher les contacts » d'une
campagne) : voir core.sourcing.trouver_contact. Aucune valeur propre à une niche n'est ici : le message suit l'angle de
la campagne, comme un email.

- Un « pas intéressé » est définitif : le prospect passe en « ignoré » (aucun contact, par aucun canal).
- Les autres résultats ne changent que le journal.

Utilisable seul : python -m core.formulaires check     tests sans réseau, sur base jetable
"""
import json
import logging
import sys
import tempfile
from pathlib import Path

from core import appels, campagnes, db, scorer, writer
from core.reglages import ErreurValidation

journal = logging.getLogger("prospect")

RESULTATS = {
    "envoye": "Message envoyé",
    "reponse": "A répondu",
    "pas_interesse": "Pas intéressé (ne plus contacter)",
    "inutilisable": "Formulaire inutilisable (captcha, erreur…)",
}
ETATS = {"faire": "À faire", "envoyes": "Envoyés", "reponses": "Réponses", "clos": "Clôturés"}
_LONGUEUR_MAX_MESSAGE, _LONGUEUR_MAX_NOTE = 3000, 1000


class ErreurFormulaire(Exception):
    """Action impossible (IA absente, prospect inconnu…) : le message est affiché tel quel."""


def etat_de(dernier_resultat):
    """Où se range un prospect dans la file, d'après son dernier résultat."""
    return {None: "faire", "envoye": "envoyes", "reponse": "reponses"}.get(dernier_resultat, "clos")


_REQUETE = """SELECT p.id, p.nom, p.ville, p.site, p.formulaire_url, p.score, p.problemes, p.campagne_id, p.message_formulaire,
                      c.nom AS campagne_nom, f.resultat AS dernier_resultat, f.fait_le AS dernier_contact,
                      (SELECT COUNT(*) FROM contacts_formulaire x WHERE x.prospect_id = p.id) AS nb_contacts
                 FROM prospects p JOIN campagnes c ON c.id = p.campagne_id
                 LEFT JOIN contacts_formulaire f ON f.id = (SELECT MAX(id) FROM contacts_formulaire WHERE prospect_id = p.id)
                WHERE p.formulaire_url IS NOT NULL AND p.email IS NULL AND c.est_modele = 0
                  AND (p.statut <> 'ignore' OR f.id IS NOT NULL){filtre}"""


def file(conn, campagne_id=None, etat="faire", limite=500):
    """Prospects à contacter par formulaire, du score le plus haut au plus bas. Renvoie {'lignes', 'compteurs', 'total'} ;
    les compteurs portent sur la campagne choisie, pas sur l'état."""
    filtre, valeurs = (" AND p.campagne_id = ?", [int(campagne_id)]) if campagne_id else ("", [])
    seuil = int(db.get_reglage(conn, "score_min_generation", "0") or 0)  # le même seuil que pour les emails (Réglages)
    lignes = [dict(l) for l in conn.execute(_REQUETE.format(filtre=filtre), valeurs)
              if l["score"] >= seuil or l["dernier_resultat"] is not None]  # un site déjà contacté reste visible
    compteurs = {cle: 0 for cle in ETATS}
    gardees, cache = [], {}
    for l in lignes:
        l["etat"] = etat_de(l["dernier_resultat"])
        compteurs[l["etat"]] += 1
        if etat not in ETATS or l["etat"] == etat:
            gardees.append(l)
    gardees.sort(key=lambda l: (-l["score"], l["id"]))
    for l in gardees[:limite]:
        l["problemes"] = appels._problemes_concrets(conn, l["campagne_id"], json.loads(l["problemes"]), cache)
        l["score_affiche"] = min(l["score"], scorer.PLAFOND_AFFICHAGE)
        l["a_message"] = bool(l.pop("message_formulaire"))
        l["dernier_libelle"] = RESULTATS.get(l["dernier_resultat"])
    return {"lignes": gardees[:limite], "compteurs": compteurs, "total": len(gardees)}


def fiche(conn, prospect_id):
    p = conn.execute(
        "SELECT p.*, c.nom AS campagne_nom FROM prospects p JOIN campagnes c ON c.id = p.campagne_id WHERE p.id = ?",
        (prospect_id,)).fetchone()
    if p is None:
        raise ErreurFormulaire("Prospect introuvable.")
    historique = [dict(h, libelle=RESULTATS.get(h["resultat"], h["resultat"])) for h in conn.execute(
        "SELECT id, fait_le, resultat, note FROM contacts_formulaire WHERE prospect_id = ? ORDER BY id DESC", (prospect_id,))]
    return {"id": p["id"], "nom": p["nom"], "ville": p["ville"], "site": p["site"], "formulaire_url": p["formulaire_url"],
            "campagne": p["campagne_nom"], "score": min(p["score"], scorer.PLAFOND_AFFICHAGE),
            "problemes": appels._problemes_concrets(conn, p["campagne_id"], json.loads(p["problemes"]), {}),
            "message": p["message_formulaire"] or "", "historique": historique}


# Canaux écrits à la main qui partagent cette rédaction : (réglage du prompt, colonne du message, consigne à l'IA).
CANAUX_MESSAGE = {
    "formulaire": ("prompt_formulaire", "message_formulaire", "Écris le message pour le formulaire de contact."),
    "social": ("prompt_social", "message_social", "Écris le message privé."),
}


def generer_message(cfg, prospect_id, canal="formulaire"):
    """Écrit (ou réécrit) le message à coller dans le formulaire d'un prospect (ou à envoyer en message privé), avec l'IA,
    et l'enregistre."""
    cle_prompt, colonne, consigne = CANAUX_MESSAGE[canal]
    manquants = writer.configuration_manquante(cfg)
    if manquants:
        raise ErreurFormulaire("L'IA n'est pas prête : il manque " + ", ".join(manquants) + " (Réglages).")
    with db.connexion() as conn:
        p = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
        if p is None:
            raise ErreurFormulaire("Prospect introuvable.")
        if p["statut"] == "nouveau":
            raise ErreurFormulaire("Ce prospect n'est pas encore analysé : lance d'abord l'analyse des sites de sa campagne.")
        campagne = conn.execute("SELECT * FROM campagnes WHERE id = ?", (p["campagne_id"],)).fetchone()
        modele = conn.execute("SELECT * FROM modeles_message WHERE id = ?",
                              (campagne["modele_message_id"],)).fetchone() if campagne["modele_message_id"] else None
        problemes = appels._problemes_concrets(conn, campagne["id"], json.loads(p["problemes"]), {})
    if not problemes:
        raise ErreurFormulaire("Aucun problème concret détecté pour ce prospect : un message sans problème réel à citer serait inventé.")
    signature = ((modele["signature"] if modele else "") or "").strip() or (cfg.get("signature_defaut") or "").strip() \
        or cfg["expediteur_nom"].strip()
    if canal == "social":  # un message privé se signe de son prénom, comme un vrai message
        signature = cfg["expediteur_nom"].strip()
    systeme = writer.rendre(cfg[cle_prompt], {
        "nom": p["nom"], "ville": p["ville"] or "", "secteur": campagne["secteur"], "score": min(p["score"], scorer.PLAFOND_AFFICHAGE),
        "problemes": "\n".join(f"- {x}" for x in problemes), "site": p["site"] or "aucun site web connu", "reputation": writer.reputation(p),
        "expediteur": cfg["expediteur_nom"].strip(), "signature": signature,
        "angle": appels._angle_de(modele["prompt_systeme"]) if modele else ""})
    systeme += f"\n\nÉcris le message en langue « {campagne['langue']} »."
    conversation = [{"role": "system", "content": systeme}, {"role": "user", "content": consigne}]
    try:
        texte = writer._nettoyer_reponse(writer.appeler_ia(cfg, conversation))
        defauts = writer.verifier_message(texte, signature, cfg)
        if defauts:  # une seule correction automatique, puis l'échec est visible
            conversation += [{"role": "assistant", "content": texte},
                             {"role": "user", "content": "Ce message ne respecte pas les règles : " + " ; ".join(defauts)
                              + ". Réécris-le en les respectant strictement."}]
            texte = writer._nettoyer_reponse(writer.appeler_ia(cfg, conversation))
            defauts = writer.verifier_message(texte, signature, cfg)
            if defauts:
                raise writer.ErreurRedaction("message refusé, " + " ; ".join(defauts) + " (après une correction)")
    except writer.ErreurRedaction as erreur:
        raise ErreurFormulaire(str(erreur)) from erreur
    texte = writer.poser_signature(texte, signature)
    if len(texte) > _LONGUEUR_MAX_MESSAGE:
        raise ErreurFormulaire("L'IA a écrit un message trop long. Réessaie.")
    with db.connexion() as conn:
        conn.execute(f"UPDATE prospects SET {colonne} = ? WHERE id = ?", (texte, prospect_id))
    journal.info("message (%s) généré : prospect %s", canal, prospect_id)
    return texte


def enregistrer_message(conn, prospect_id, texte, canal="formulaire"):
    """Enregistre le message relu et corrigé par l'utilisateur (vide = aucun message)."""
    colonne = CANAUX_MESSAGE[canal][1]
    texte = (texte or "").strip()
    if len(texte) > _LONGUEUR_MAX_MESSAGE:
        raise ErreurValidation({"message": f"{_LONGUEUR_MAX_MESSAGE} caractères au maximum."})
    if conn.execute(f"UPDATE prospects SET {colonne} = ? WHERE id = ?", (texte or None, prospect_id)).rowcount == 0:
        raise ErreurFormulaire("Prospect introuvable.")


def noter(conn, prospect_id, resultat, note=None):
    """Enregistre ce qui s'est passé. « Pas intéressé » est définitif : prospect ignoré (jamais un prospect déjà contacté
    par email : db.changer_statut ne touche ni « envoyé » ni « répondu »)."""
    erreurs = {}
    if resultat not in RESULTATS:
        erreurs["resultat"] = "Choisis le résultat."
    note = " ".join((note or "").split()) or None
    if note and len(note) > _LONGUEUR_MAX_NOTE:
        erreurs["note"] = f"{_LONGUEUR_MAX_NOTE} caractères au maximum."
    if erreurs:
        raise ErreurValidation(erreurs)
    if conn.execute("SELECT 1 FROM prospects WHERE id = ?", (prospect_id,)).fetchone() is None:
        raise ErreurFormulaire("Prospect introuvable.")
    conn.execute("INSERT INTO contacts_formulaire (prospect_id, resultat, note) VALUES (?, ?, ?)", (prospect_id, resultat, note))
    ignore = resultat == "pas_interesse" and bool(db.changer_statut(conn, [prospect_id], "ignore")["modifies"])
    journal.info("contact par formulaire noté : prospect %s, %s", prospect_id, resultat)
    return {"etat": etat_de(resultat), "ignore": ignore}


# --------------------------------------------------------------------------------------
# Auto-tests
# --------------------------------------------------------------------------------------


def _autotests():
    import config
    from core import reglages, seeds

    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    def leve(fonction, *args, **kw):
        try:
            fonction(*args, **kw)
        except (ErreurValidation, ErreurFormulaire) as e:
            return e
        return None

    verifier("état d'un prospect : jamais contacté = à faire ; envoyé ; réponse ; pas intéressé et inutilisable = clôturés",
             etat_de(None) == "faire" and etat_de("envoye") == "envoyes" and etat_de("reponse") == "reponses"
             and etat_de("pas_interesse") == "clos" and etat_de("inutilisable") == "clos")

    reponse = "Bonjour,\n\nJe suis Gill, développeur web.\n\nVotre site n'a pas de page claire.\n\nPouvons-nous en parler ?\n\nGill,\nDéveloppeur web"
    comportement = {"reponses": [reponse], "prompts": []}
    ia_reelle = writer.appeler_ia

    def fausse_ia(cfg, messages):
        comportement["prompts"].append(messages[0]["content"])
        suivante = comportement["reponses"][min(len(comportement["prompts"]) - 1, len(comportement["reponses"]) - 1)]
        if isinstance(suivante, Exception):
            raise suivante
        return suivante

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ancien = config.DB_PATH
        config.DB_PATH = Path(tmp) / "t.db"
        writer.appeler_ia = fausse_ia
        try:
            db.init_db(importer_env=False)
            with db.connexion() as c:
                cfg = reglages.valeurs_effectives(c)
                cfg.update(ai_endpoint="https://ia.exemple/v1", ai_model="m", ai_key="k", expediteur_nom="Gill",
                           signature_defaut="Gill,\nDéveloppeur web", mots_max_message="120")
                cid = campagnes.creer(c, {"nom": "Avocats", "secteur": "cabinet d'avocats", "pays": "FR", "langue": "fr"})
                angle = c.execute("INSERT INTO modeles_message (nom, objet_template, prompt_systeme) VALUES ('a', '{nom}', ?)",
                                  (seeds.composer_prompt("Angle : un client cherche un avocat sur Google."),)).lastrowid
                campagnes.modifier(c, cid, {"modele_message_id": angle})

                def ajouter(nom, form, email, score, problemes, statut="qualifie"):
                    db.inserer_prospect(c, {"campagne_id": cid, "nom": nom, "ville": "Lyon", "email": email,
                                            "source": "google_places", "source_id": "g:" + nom})
                    pid = c.execute("SELECT id FROM prospects WHERE nom = ?", (nom,)).fetchone()[0]
                    c.execute("UPDATE prospects SET score = ?, problemes = ?, statut = ?, site = ?, formulaire_url = ? WHERE id = ?",
                              (score, json.dumps(problemes), statut, "https://" + nom[:1].lower() + ".fr", form, pid))
                    return pid
                a = ajouter("A", "https://a.fr/contact", None, 9, ["pas_rdv", "site_inconnu"])
                b = ajouter("B", "https://b.fr/contact", None, 6, ["pas_h1"])
                ajouter("Bas", "https://bas.fr/contact", None, 2, ["pas_h1"])
                e = ajouter("E avec email", "https://e.fr/contact", "e@e.fr", 8, ["pas_h1"])
                s = ajouter("S sans formulaire", None, None, 8, ["pas_h1"])
                i = ajouter("I ignoré", "https://i.fr/contact", None, 8, ["pas_h1"], statut="ignore")
                n = ajouter("N nouveau", "https://n.fr/contact", None, 5, [], statut="nouveau")

                f = file(c)
                verifier("file : seulement les prospects avec un formulaire ET sans email, ni ignorés, du score le plus haut au plus bas",
                         [l["nom"] for l in f["lignes"]] == ["A", "B", "N nouveau"] and f["compteurs"]["faire"] == 3)
                verifier("file : vrai problème en clair (« site inconnu » à 0 point écarté), adresse du formulaire fournie",
                         f["lignes"][0]["problemes"] == ["Pas de prise de rendez-vous en ligne"]
                         and f["lignes"][0]["formulaire_url"] == "https://a.fr/contact" and not f["lignes"][0]["a_message"])

                verifier("résultat inconnu refusé, prospect inconnu refusé, rien d'écrit",
                         "resultat" in leve(noter, c, a, "peut-etre").erreurs and "introuvable" in str(leve(noter, c, 9999, "envoye"))
                         and c.execute("SELECT COUNT(*) FROM contacts_formulaire").fetchone()[0] == 0)
                noter(c, a, "envoye", "Envoyé le 6/10")
                f = file(c)
                verifier("message envoyé : sort de « à faire », apparaît dans « envoyés »",
                         [l["nom"] for l in f["lignes"]] == ["B", "N nouveau"] and f["compteurs"]["envoyes"] == 1
                         and [l["nom"] for l in file(c, etat="envoyes")["lignes"]] == ["A"])
                noter(c, a, "reponse", "Réponse par email")
                verifier("réponse reçue : rangée dans « réponses », statut inchangé",
                         [l["nom"] for l in file(c, etat="reponses")["lignes"]] == ["A"]
                         and c.execute("SELECT statut FROM prospects WHERE id = ?", (a,)).fetchone()[0] == "qualifie")
                noter(c, b, "inutilisable", "Captcha")
                verifier("formulaire inutilisable : clôturé, statut inchangé",
                         [l["nom"] for l in file(c, etat="clos")["lignes"]] == ["B"]
                         and c.execute("SELECT statut FROM prospects WHERE id = ?", (b,)).fetchone()[0] == "qualifie")
                r = noter(c, n, "pas_interesse")
                verifier("pas intéressé : prospect ignoré (aucun contact), toujours visible dans « clôturés »",
                         r == {"etat": "clos", "ignore": True} and "N nouveau" in [l["nom"] for l in file(c, etat="clos")["lignes"]]
                         and c.execute("SELECT statut FROM prospects WHERE id = ?", (n,)).fetchone()[0] == "ignore")
                fi = fiche(c, a)
                verifier("fiche : historique du plus récent au plus ancien, avec libellés et notes",
                         [h["resultat"] for h in fi["historique"]] == ["reponse", "envoye"] and fi["historique"][1]["note"] == "Envoyé le 6/10"
                         and fi["historique"][0]["libelle"] == "A répondu")

            # --- Le message ---
            texte = generer_message(cfg, a)
            prompt = comportement["prompts"][0]
            with db.connexion() as c:
                enregistre = fiche(c, a)["message"]
            verifier("message : le prompt contient le vrai problème, l'angle de la niche, l'expéditeur et la signature ; le texte est enregistré",
                     "- Pas de prise de rendez-vous en ligne" in prompt and "Angle : un client cherche un avocat sur Google." in prompt
                     and "Gill" in prompt and "{angle}" not in prompt and "{signature}" not in prompt and enregistre == texte
                     and "formulaire de contact" in prompt)
            verifier("message : une seule signature, à la fin (jamais recopiée en double)", texte.count("Développeur web") == 1 and texte.endswith("Développeur web"))
            comportement["prompts"].clear()
            comportement["reponses"] = ["Voir https://monsite.fr pour plus de détails sur mon travail et mes références.", reponse]
            generer_message(cfg, a)
            verifier("message avec un lien : corrigé automatiquement une fois (2 appels), le second est retenu",
                     len(comportement["prompts"]) == 2)
            comportement["prompts"].clear()
            comportement["reponses"] = ["Voir https://monsite.fr pour plus de détails sur mon travail."]
            verifier("message qui garde son lien après correction : refusé, rien d'enregistré en plus", "lien" in str(leve(generer_message, cfg, a)))
            comportement["prompts"].clear()
            comportement["reponses"] = [writer.ErreurRedaction("Le fournisseur d'IA refuse la requête (HTTP 401)")]
            verifier("IA en erreur : message du fournisseur repris", "HTTP 401" in str(leve(generer_message, cfg, a)))
            comportement["reponses"] = [reponse]
            with db.connexion() as c:
                c.execute("UPDATE prospects SET statut = 'nouveau' WHERE id = ?", (n,))
            comportement["prompts"].clear()
            verifier("prospect pas encore analysé : refusé sans appel d'IA", "analysé" in str(leve(generer_message, cfg, n)) and not comportement["prompts"])
            verifier("réglages d'IA manquants : on dit lesquels", "la clé" in str(leve(generer_message, {**cfg, "ai_key": ""}, a)))
            with db.connexion() as c:
                c.execute("UPDATE prospects SET problemes = '[\"site_inconnu\"]' WHERE id = ?", (b,))
            verifier("aucun problème concret : refusé", "problème concret" in str(leve(generer_message, cfg, b)))
            with db.connexion() as c:
                enregistrer_message(c, a, "  Mon message corrigé.  ")
                verifier("message corrigé à la main : enregistré rogné ; vide : supprimé ; trop long : refusé",
                         fiche(c, a)["message"] == "Mon message corrigé." and enregistrer_message(c, a, "") is None
                         and fiche(c, a)["message"] == "" and "message" in leve(enregistrer_message, c, a, "x" * 4000).erreurs)

                # --- Suppression d'une campagne : le journal part avec elle ---
                campagnes.supprimer(c, cid, forcer=True)
                verifier("campagne supprimée : prospects et journal de formulaires disparaissent, aucune ligne orpheline",
                         c.execute("SELECT COUNT(*) FROM contacts_formulaire").fetchone()[0] == 0 and c.execute("PRAGMA foreign_key_check").fetchall() == [])
        finally:
            writer.appeler_ia = ia_reelle
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
    print("Usage : python -m core.formulaires check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
