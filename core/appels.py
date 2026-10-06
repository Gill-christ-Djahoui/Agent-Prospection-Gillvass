"""Appels téléphoniques : la file des prospects à appeler, un script d'appel par IA, le journal des appels.

Pour ceux qui n'ont pas d'email (la majorité : un cabinet sans site propre n'a aucune adresse à fouiller), le téléphone
est le seul canal. Le script s'appuie sur le VRAI problème constaté ; rien n'est inventé.

- Un appel « pas intéressé » est définitif : le prospect passe en « ignoré » et son email éventuel en liste noire.
- Les autres résultats ne changent que le journal : « pas de réponse » revient dans la file, « à rappeler » revient à la
  date choisie, « intéressé » et « rendez-vous obtenu » sortent de la file « à faire ».

Utilisable seul : python -m core.appels check     tests sans réseau, sur base jetable
"""
import json
import logging
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core import campagnes, db, scorer, writer
from core.reglages import ErreurValidation

journal = logging.getLogger("prospect")

RESULTATS = {
    "pas_de_reponse": "Pas de réponse",
    "a_rappeler": "À rappeler",
    "interesse": "Intéressé",
    "rdv_obtenu": "Rendez-vous obtenu",
    "pas_interesse": "Pas intéressé (ne plus contacter)",
    "mauvais_numero": "Mauvais numéro",
}
ETATS = {"faire": "À faire", "plus_tard": "Plus tard", "interesses": "Intéressés", "clos": "Clôturés"}
_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LONGUEUR_MAX_SCRIPT, _LONGUEUR_MAX_NOTE, _RAPPEL_MAX_JOURS = 2000, 1000, 365


class ErreurAppel(Exception):
    """Action impossible (IA absente, prospect inconnu…) : le message est affiché tel quel."""


# --------------------------------------------------------------------------------------
# Outils
# --------------------------------------------------------------------------------------


def aujourdhui(cfg, maintenant=None):
    """Date du jour (AAAA-MM-JJ) dans le fuseau métier des réglages."""
    zone = ZoneInfo(cfg.get("fuseau") or "Europe/Paris")
    return (maintenant or datetime.now(zone)).astimezone(zone).date().isoformat()


def lien_telephone(numero):
    """Numéro réduit aux chiffres et au « + » initial : sûr dans un lien tel:. None s'il ne reste rien d'utilisable."""
    brut = (numero or "").strip()
    chiffres = re.sub(r"\D", "", brut)
    if len(chiffres) < 6:
        return None
    return ("+" if brut.startswith("+") else "") + chiffres


def etat_de(dernier_resultat, rappel_le, aujourd_hui):
    """Où se range un prospect dans la file, d'après son dernier appel."""
    if dernier_resultat in (None, "pas_de_reponse"):
        return "faire"
    if dernier_resultat == "a_rappeler":
        return "faire" if not rappel_le or rappel_le <= aujourd_hui else "plus_tard"
    if dernier_resultat in ("interesse", "rdv_obtenu"):
        return "interesses"
    return "clos"


def _problemes_concrets(conn, campagne_id, codes, cache):
    """Libellés des vrais problèmes (actifs, avec des points), du plus lourd au plus léger."""
    if campagne_id not in cache:
        cache[campagne_id] = {c["code"]: c for c in campagnes.criteres_effectifs(conn, campagne_id)}
    criteres = cache[campagne_id]
    retenus = sorted((criteres[c] for c in codes if c in criteres and criteres[c]["actif"] and criteres[c]["points"] > 0),
                     key=lambda c: -c["points"])
    return [c["libelle"] for c in retenus]


def _angle_de(prompt):
    """La partie « Angle : … » du prompt d'une campagne (ce qui change d'une niche à l'autre), ou vide."""
    trouve = re.search(r"(?ms)^Angle\s*:.*\Z", prompt or "")
    return trouve.group(0).strip() if trouve else ""


_REQUETE = """SELECT p.id, p.nom, p.ville, p.telephone, p.site, p.email, p.score, p.problemes, p.campagne_id, p.script_appel,
                      c.nom AS campagne_nom, a.resultat AS dernier_resultat, a.appele_le AS dernier_appel, a.rappel_le,
                      (SELECT COUNT(*) FROM appels x WHERE x.prospect_id = p.id) AS nb_appels
                 FROM prospects p JOIN campagnes c ON c.id = p.campagne_id
                 LEFT JOIN appels a ON a.id = (SELECT MAX(id) FROM appels WHERE prospect_id = p.id)
                WHERE p.telephone IS NOT NULL AND TRIM(p.telephone) <> '' AND c.est_modele = 0
                  AND (p.statut <> 'ignore' OR a.id IS NOT NULL){filtres}"""


# --------------------------------------------------------------------------------------
# La file d'appels
# --------------------------------------------------------------------------------------


def file(conn, cfg, campagne_id=None, contact="sans", etat="faire", aujourd_hui=None, limite=500):
    """Prospects à appeler. `contact` : sans (pas d'email) | avec | tous. `etat` : une clé de ETATS, ou « tous ».

    Les rappels dus passent en tête, puis le score le plus haut. Renvoie {'lignes', 'compteurs', 'total'} ; les compteurs
    portent sur la campagne et le type de contact choisis, pas sur l'état.
    """
    aujourd_hui = aujourd_hui or aujourdhui(cfg)
    filtres, valeurs = "", []
    if campagne_id:
        filtres += " AND p.campagne_id = ?"
        valeurs.append(int(campagne_id))
    if contact == "sans":
        filtres += " AND p.email IS NULL"
    elif contact == "avec":
        filtres += " AND p.email IS NOT NULL"
    lignes = [dict(l) for l in conn.execute(_REQUETE.format(filtres=filtres), valeurs)]
    compteurs = {cle: 0 for cle in ETATS} | {"rappels_dus": 0}
    gardees, cache = [], {}
    for l in lignes:
        l["etat"] = etat_de(l["dernier_resultat"], l["rappel_le"], aujourd_hui)
        compteurs[l["etat"]] += 1
        l["rappel_du"] = l["dernier_resultat"] == "a_rappeler" and l["etat"] == "faire"
        compteurs["rappels_dus"] += l["rappel_du"]
        if etat not in ETATS or l["etat"] == etat:
            gardees.append(l)
    gardees.sort(key=lambda l: (not l["rappel_du"], -l["score"], l["id"]))
    for l in gardees[:limite]:
        l["problemes"] = _problemes_concrets(conn, l["campagne_id"], json.loads(l["problemes"]), cache)
        l["tel"] = lien_telephone(l["telephone"])
        l["score_affiche"] = min(l["score"], scorer.PLAFOND_AFFICHAGE)
        l["a_script"] = bool(l.pop("script_appel"))
        l["dernier_libelle"] = RESULTATS.get(l["dernier_resultat"])
        l["a_un_email"] = bool(l.pop("email"))
    return {"lignes": gardees[:limite], "compteurs": compteurs, "total": len(gardees)}


def compteurs_rail(conn, cfg, aujourd_hui=None):
    """Rappels dus aujourd'hui (pastille du rail de navigation)."""
    aujourd_hui = aujourd_hui or aujourdhui(cfg)
    return conn.execute(
        """SELECT COUNT(*) FROM prospects p JOIN appels a ON a.id = (SELECT MAX(id) FROM appels WHERE prospect_id = p.id)
            WHERE a.resultat = 'a_rappeler' AND (a.rappel_le IS NULL OR a.rappel_le <= ?) AND p.statut <> 'ignore'""",
        (aujourd_hui,)).fetchone()[0]


# --------------------------------------------------------------------------------------
# La fiche d'un prospect
# --------------------------------------------------------------------------------------


def fiche(conn, prospect_id):
    p = conn.execute(
        "SELECT p.*, c.nom AS campagne_nom FROM prospects p JOIN campagnes c ON c.id = p.campagne_id WHERE p.id = ?",
        (prospect_id,)).fetchone()
    if p is None:
        raise ErreurAppel("Prospect introuvable.")
    historique = [dict(a, libelle=RESULTATS.get(a["resultat"], a["resultat"])) for a in conn.execute(
        "SELECT id, appele_le, resultat, rappel_le, note FROM appels WHERE prospect_id = ? ORDER BY id DESC", (prospect_id,))]
    return {"id": p["id"], "nom": p["nom"], "ville": p["ville"], "adresse": p["adresse"], "telephone": p["telephone"],
            "tel": lien_telephone(p["telephone"]), "site": p["site"], "email": p["email"], "campagne": p["campagne_nom"],
            "score": min(p["score"], scorer.PLAFOND_AFFICHAGE), "statut": p["statut"],
            "problemes": _problemes_concrets(conn, p["campagne_id"], json.loads(p["problemes"]), {}),
            "script": p["script_appel"] or "", "historique": historique}


# --------------------------------------------------------------------------------------
# Le script d'appel
# --------------------------------------------------------------------------------------


def generer_script(cfg, prospect_id):
    """Écrit (ou réécrit) le script d'appel d'un prospect avec l'IA et l'enregistre. Renvoie le texte."""
    manquants = writer.configuration_manquante(cfg)
    if manquants:
        raise ErreurAppel("L'IA n'est pas prête : il manque " + ", ".join(manquants) + " (Réglages).")
    with db.connexion() as conn:
        p = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
        if p is None:
            raise ErreurAppel("Prospect introuvable.")
        if p["statut"] == "nouveau":
            raise ErreurAppel("Ce prospect n'est pas encore analysé : lance d'abord l'analyse des sites de sa campagne.")
        campagne = conn.execute("SELECT * FROM campagnes WHERE id = ?", (p["campagne_id"],)).fetchone()
        modele = conn.execute("SELECT prompt_systeme FROM modeles_message WHERE id = ?",
                              (campagne["modele_message_id"],)).fetchone() if campagne["modele_message_id"] else None
        problemes = _problemes_concrets(conn, campagne["id"], json.loads(p["problemes"]), {})
    if not problemes:
        raise ErreurAppel("Aucun problème concret détecté pour ce prospect : un script sans problème réel à citer serait inventé.")
    systeme = writer.rendre(cfg["prompt_appel"], {
        "nom": p["nom"], "ville": p["ville"] or "", "secteur": campagne["secteur"], "score": min(p["score"], scorer.PLAFOND_AFFICHAGE),
        "problemes": "\n".join(f"- {x}" for x in problemes), "site": p["site"] or "aucun site web connu",
        "expediteur": cfg["expediteur_nom"].strip(), "angle": _angle_de(modele["prompt_systeme"]) if modele else ""})
    systeme += f"\n\nÉcris le script en langue « {campagne['langue']} »."
    try:
        texte = writer._nettoyer_reponse(writer.appeler_ia(cfg, [
            {"role": "system", "content": systeme}, {"role": "user", "content": "Écris le script de l'appel."}]))
    except writer.ErreurRedaction as erreur:
        raise ErreurAppel(str(erreur)) from erreur
    if len(texte) > _LONGUEUR_MAX_SCRIPT:
        raise ErreurAppel("L'IA a écrit un script trop long. Réessaie.")
    with db.connexion() as conn:
        conn.execute("UPDATE prospects SET script_appel = ? WHERE id = ?", (texte, prospect_id))
    journal.info("script d'appel généré : prospect %s", prospect_id)
    return texte


def enregistrer_script(conn, prospect_id, texte):
    """Enregistre le script relu et corrigé par l'utilisateur (vide = aucun script)."""
    texte = (texte or "").strip()
    if len(texte) > _LONGUEUR_MAX_SCRIPT:
        raise ErreurValidation({"script": f"{_LONGUEUR_MAX_SCRIPT} caractères au maximum."})
    if conn.execute("UPDATE prospects SET script_appel = ? WHERE id = ?", (texte or None, prospect_id)).rowcount == 0:
        raise ErreurAppel("Prospect introuvable.")


# --------------------------------------------------------------------------------------
# Le journal des appels
# --------------------------------------------------------------------------------------


def noter_appel(conn, cfg, prospect_id, resultat, note=None, rappel_le=None, aujourd_hui=None):
    """Enregistre un appel. « Pas intéressé » est définitif : prospect ignoré, email éventuel en liste noire."""
    aujourd_hui = aujourd_hui or aujourdhui(cfg)
    erreurs = {}
    if resultat not in RESULTATS:
        erreurs["resultat"] = "Choisis le résultat de l'appel."
    note = " ".join((note or "").split()) or None
    if note and len(note) > _LONGUEUR_MAX_NOTE:
        erreurs["note"] = f"{_LONGUEUR_MAX_NOTE} caractères au maximum."
    rappel_le = (rappel_le or "").strip() or None
    if resultat == "a_rappeler":
        limite = (datetime.strptime(aujourd_hui, "%Y-%m-%d") + timedelta(days=_RAPPEL_MAX_JOURS)).strftime("%Y-%m-%d")
        if not rappel_le or not _RE_DATE.match(rappel_le):
            erreurs["rappel_le"] = "Choisis la date du rappel."
        elif not aujourd_hui <= rappel_le <= limite:
            erreurs["rappel_le"] = "La date doit être aujourd'hui ou plus tard, dans l'année."
    else:
        rappel_le = None
    if erreurs:
        raise ErreurValidation(erreurs)
    p = conn.execute("SELECT email, statut FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    if p is None:
        raise ErreurAppel("Prospect introuvable.")
    conn.execute("INSERT INTO appels (prospect_id, resultat, rappel_le, note) VALUES (?, ?, ?, ?)",
                 (prospect_id, resultat, rappel_le, note))
    ignore = liste_noire = False
    if resultat == "pas_interesse":
        ignore = bool(db.changer_statut(conn, [prospect_id], "ignore")["modifies"])  # jamais un prospect déjà contacté par email
        if p["email"]:
            liste_noire = bool(db.ajouter_liste_noire(conn, p["email"], "manuel"))
    journal.info("appel enregistré : prospect %s, %s", prospect_id, resultat)
    return {"etat": etat_de(resultat, rappel_le, aujourd_hui), "ignore": ignore, "liste_noire": liste_noire}


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
        except (ErreurValidation, ErreurAppel) as e:
            return e
        return None

    verifier("lien tel: : espaces et ponctuation retirés, « + » initial gardé, rien d'injectable, numéro trop court refusé",
             lien_telephone("04 72 00 00 00") == "0472000000" and lien_telephone("+33 4 72 00 00 00") == "+33472000000"
             and lien_telephone('04"><script>72 00 00 00') == "0472000000" and lien_telephone("12") is None and lien_telephone(None) is None)
    verifier("état d'un prospect : jamais appelé ou pas de réponse = à faire ; rappel futur = plus tard ; intéressé ; clos",
             etat_de(None, None, "2026-10-10") == "faire" and etat_de("pas_de_reponse", None, "2026-10-10") == "faire"
             and etat_de("a_rappeler", "2026-10-11", "2026-10-10") == "plus_tard" and etat_de("a_rappeler", "2026-10-10", "2026-10-10") == "faire"
             and etat_de("rdv_obtenu", None, "x") == "interesses" and etat_de("mauvais_numero", None, "x") == "clos")
    verifier("angle extrait du prompt d'une campagne (la partie propre à la niche)",
             _angle_de("Règles :\n- a\n{signature}\n\nAngle : relie le problème à X.\nDeuxième ligne.") == "Angle : relie le problème à X.\nDeuxième ligne."
             and _angle_de("pas d'angle ici") == "")

    comportement = {"reponse": "Accroche : Bonjour.\nConstat : x.\nQuestion : y ?\nSi ce n'est pas le bon moment : z.", "prompts": []}
    ia_reelle = writer.appeler_ia

    def fausse_ia(cfg, messages):
        comportement["prompts"].append(messages[0]["content"])
        if isinstance(comportement["reponse"], Exception):
            raise comportement["reponse"]
        return comportement["reponse"]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ancien = config.DB_PATH
        config.DB_PATH = Path(tmp) / "t.db"
        writer.appeler_ia = fausse_ia
        try:
            db.init_db(importer_env=False)
            with db.connexion() as c:
                cfg = reglages.valeurs_effectives(c)
                cfg.update(ai_endpoint="https://ia.exemple/v1", ai_model="m", ai_key="k", expediteur_nom="Gill")
                cid = campagnes.creer(c, {"nom": "Dentistes", "secteur": "cabinet dentaire", "pays": "FR", "langue": "fr"})
                angle = c.execute("INSERT INTO modeles_message (nom, objet_template, prompt_systeme) VALUES ('a', '{nom}', ?)",
                                  (seeds.composer_prompt("Angle : les patients cherchent un dentiste depuis leur téléphone."),)).lastrowid
                campagnes.modifier(c, cid, {"modele_message_id": angle})
                modele = c.execute("SELECT id FROM campagnes WHERE est_modele = 1 LIMIT 1").fetchone()[0]

                def ajouter(nom, tel, email, score, problemes, statut="qualifie", campagne=cid):
                    db.inserer_prospect(c, {"campagne_id": campagne, "nom": nom, "ville": "Lyon", "telephone": tel, "email": email,
                                            "source": "google_places", "source_id": "g:" + nom})
                    pid = c.execute("SELECT id FROM prospects WHERE nom = ?", (nom,)).fetchone()[0]
                    c.execute("UPDATE prospects SET score = ?, problemes = ?, statut = ?, site = ? WHERE id = ?",
                              (score, json.dumps(problemes), statut, "https://a.fr" if "sans_site" not in problemes else None, pid))
                    return pid
                a = ajouter("A sans site", "04 72 00 00 01", None, 12, ["sans_site", "site_inconnu"])
                b = ajouter("B rdv", "04 72 00 00 02", None, 5, ["pas_rdv"])
                e = ajouter("C avec email", "04 72 00 00 03", "c@cabinet-c.fr", 8, ["pas_h1"])
                d = ajouter("D sans téléphone", None, None, 9, ["sans_site"])
                i = ajouter("E ignoré", "04 72 00 00 05", None, 9, ["sans_site"], statut="ignore")
                n = ajouter("F nouveau", "04 72 00 00 06", None, 0, [], statut="nouveau")
                m = ajouter("G modèle", "04 72 00 00 07", None, 9, ["sans_site"], campagne=modele)
                f1 = ajouter("H envoyé", "04 72 00 00 08", "h@cabinet-h.fr", 6, ["pas_rdv"], statut="envoye")
                aujourd_hui = "2026-10-10"

                f = file(c, cfg, aujourd_hui=aujourd_hui)
                verifier("file par défaut : sans email, avec téléphone, ni ignoré ni modèle, du score le plus haut au plus bas",
                         [l["nom"] for l in f["lignes"]] == ["A sans site", "B rdv", "F nouveau"])
                verifier("seuls les vrais problèmes sont affichés (« site inconnu » à 0 point est écarté), score plafonné à l'affichage",
                         f["lignes"][0]["problemes"] == ["Aucun site web"] and f["lignes"][0]["score_affiche"] == scorer.PLAFOND_AFFICHAGE
                         and f["lignes"][0]["tel"] == "0472000001")
                verifier("contact « avec » et « tous » : les prospects avec email apparaissent",
                         {l["nom"] for l in file(c, cfg, contact="avec", aujourd_hui=aujourd_hui)["lignes"]} == {"C avec email", "H envoyé"}
                         and "C avec email" in {l["nom"] for l in file(c, cfg, contact="tous", aujourd_hui=aujourd_hui)["lignes"]})

                # --- Le journal ---
                verifier("résultat inconnu, rappel sans date ou dans le passé : refusés avec la raison, rien d'écrit",
                         "resultat" in leve(noter_appel, c, cfg, a, "peut-etre", aujourd_hui=aujourd_hui).erreurs
                         and "rappel_le" in leve(noter_appel, c, cfg, a, "a_rappeler", aujourd_hui=aujourd_hui).erreurs
                         and "rappel_le" in leve(noter_appel, c, cfg, a, "a_rappeler", rappel_le="2026-10-09", aujourd_hui=aujourd_hui).erreurs
                         and c.execute("SELECT COUNT(*) FROM appels").fetchone()[0] == 0)
                noter_appel(c, cfg, a, "pas_de_reponse", "Répondeur", aujourd_hui=aujourd_hui)
                verifier("pas de réponse : le prospect reste à faire", file(c, cfg, aujourd_hui=aujourd_hui)["compteurs"]["faire"] == 3
                         and file(c, cfg, aujourd_hui=aujourd_hui)["lignes"][0]["dernier_libelle"] == "Pas de réponse")
                noter_appel(c, cfg, a, "a_rappeler", "Rappeler jeudi", "2026-10-15", aujourd_hui=aujourd_hui)
                f = file(c, cfg, aujourd_hui=aujourd_hui)
                verifier("rappel futur : sort de « à faire », apparaît dans « plus tard »",
                         "A sans site" not in [l["nom"] for l in f["lignes"]] and f["compteurs"]["plus_tard"] == 1
                         and [l["nom"] for l in file(c, cfg, etat="plus_tard", aujourd_hui=aujourd_hui)["lignes"]] == ["A sans site"])
                noter_appel(c, cfg, b, "a_rappeler", None, "2026-10-10", aujourd_hui=aujourd_hui)
                f = file(c, cfg, aujourd_hui=aujourd_hui)
                verifier("rappel dû aujourd'hui : en tête de file malgré un score plus bas, compté pour la pastille du rail",
                         f["lignes"][0]["nom"] == "B rdv" and f["lignes"][0]["rappel_du"] and f["compteurs"]["rappels_dus"] == 1
                         and compteurs_rail(c, cfg, aujourd_hui) == 1)
                verifier("le jour du rappel passé, le rappel de A est dû à son tour",
                         file(c, cfg, aujourd_hui="2026-10-15")["compteurs"]["rappels_dus"] == 2 and compteurs_rail(c, cfg, "2026-10-15") == 2)
                noter_appel(c, cfg, a, "rdv_obtenu", "RDV mardi 10 h", aujourd_hui=aujourd_hui)
                verifier("rendez-vous obtenu : classé « intéressés », statut du prospect inchangé",
                         [l["nom"] for l in file(c, cfg, etat="interesses", aujourd_hui=aujourd_hui)["lignes"]] == ["A sans site"]
                         and c.execute("SELECT statut FROM prospects WHERE id = ?", (a,)).fetchone()[0] == "qualifie")
                noter_appel(c, cfg, b, "mauvais_numero", aujourd_hui=aujourd_hui)
                verifier("mauvais numéro : clôturé, statut inchangé",
                         [l["nom"] for l in file(c, cfg, etat="clos", aujourd_hui=aujourd_hui)["lignes"]] == ["B rdv"]
                         and c.execute("SELECT statut FROM prospects WHERE id = ?", (b,)).fetchone()[0] == "qualifie")
                r = noter_appel(c, cfg, e, "pas_interesse", "Ne veut plus être appelé", aujourd_hui=aujourd_hui)
                verifier("pas intéressé : prospect ignoré ET email en liste noire (plus aucun contact), toujours visible dans « clôturés »",
                         r == {"etat": "clos", "ignore": True, "liste_noire": True} and db.est_en_liste_noire(c, "c@cabinet-c.fr")
                         and c.execute("SELECT statut FROM prospects WHERE id = ?", (e,)).fetchone()[0] == "ignore"
                         and "C avec email" in {l["nom"] for l in file(c, cfg, contact="tous", etat="clos", aujourd_hui=aujourd_hui)["lignes"]})
                r = noter_appel(c, cfg, f1, "pas_interesse", aujourd_hui=aujourd_hui)
                verifier("pas intéressé sur un prospect déjà contacté par email : jamais modifié (statut « envoyé » conservé)",
                         r["ignore"] is False and c.execute("SELECT statut FROM prospects WHERE id = ?", (f1,)).fetchone()[0] == "envoye")
                fi = fiche(c, a)
                verifier("fiche : historique du plus récent au plus ancien avec libellés, notes et dates de rappel",
                         [h["resultat"] for h in fi["historique"]] == ["rdv_obtenu", "a_rappeler", "pas_de_reponse"]
                         and fi["historique"][1]["rappel_le"] == "2026-10-15" and fi["historique"][2]["note"] == "Répondeur"
                         and fi["historique"][0]["libelle"] == "Rendez-vous obtenu")
                verifier("prospect inconnu : refusé", "introuvable" in str(leve(fiche, c, 99999)) and "introuvable" in str(leve(noter_appel, c, cfg, 99999, "interesse")))

            # --- Le script ---
            texte = generer_script(cfg, a)
            prompt = comportement["prompts"][0]
            with db.connexion() as c:
                enregistre = c.execute("SELECT script_appel FROM prospects WHERE id = ?", (a,)).fetchone()[0]
            verifier("script : le prompt contient le vrai problème, le site, l'angle de la niche et l'expéditeur ; le texte est enregistré",
                     "- Aucun site web" in prompt and "aucun site web connu" in prompt and "Angle : les patients cherchent un dentiste" in prompt
                     and "Gill" in prompt and "{angle}" not in prompt and "{problemes}" not in prompt and enregistre == texte
                     and "en langue « fr »" in prompt)
            with db.connexion() as c:
                enregistrer_script(c, a, "  Mon script corrigé.  ")
                verifier("script corrigé à la main : enregistré rogné ; vide : supprimé",
                         c.execute("SELECT script_appel FROM prospects WHERE id = ?", (a,)).fetchone()[0] == "Mon script corrigé." and fiche(c, a)["script"] == "Mon script corrigé.")
                enregistrer_script(c, a, "")
                verifier("script vidé : plus de script", fiche(c, a)["script"] == "")
                verifier("script trop long : refusé", "script" in leve(enregistrer_script, c, a, "x" * 3000).erreurs)
            verifier("prospect pas encore analysé : message clair, aucun appel d'IA", "analyse" in str(leve(generer_script, cfg, n)) and len(comportement["prompts"]) == 1)
            with db.connexion() as c:
                c.execute("UPDATE prospects SET problemes = '[\"site_inconnu\"]' WHERE id = ?", (b,))
            verifier("aucun problème concret : refusé (un script sans problème réel serait inventé)", "problème concret" in str(leve(generer_script, cfg, b)))
            comportement["reponse"] = writer.ErreurRedaction("Le fournisseur d'IA refuse la requête (HTTP 401)")
            verifier("IA en erreur : message du fournisseur repris", "HTTP 401" in str(leve(generer_script, cfg, a)))
            comportement["reponse"] = "x" * 3000
            verifier("réponse démesurée de l'IA : refusée", "trop long" in str(leve(generer_script, cfg, a)))
            verifier("réglages d'IA manquants : on dit lesquels", "la clé" in str(leve(generer_script, {**cfg, "ai_key": ""}, a)))

            # --- Suppression d'une campagne : le journal d'appels part avec elle ---
            with db.connexion() as c:
                campagnes.supprimer(c, cid, forcer=True)
                verifier("campagne supprimée : prospects et journal d'appels disparaissent, aucune ligne orpheline",
                         c.execute("SELECT COUNT(*) FROM appels").fetchone()[0] == 0 and c.execute("PRAGMA foreign_key_check").fetchall() == [])
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
    print("Usage : python -m core.appels check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
