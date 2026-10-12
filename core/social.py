"""Canal « message privé » (Facebook, Instagram…) : pour les commerces qui n'ont ni email ni (souvent) site.

Rien n'est envoyé par l'application : elle te donne la liste, le lien du profil (ou, à défaut, la fiche Google Maps et une
recherche toute prête) et un message à coller ; tu l'envoies toi-même depuis ton compte, puis tu notes le résultat. Ça marche
depuis n'importe quel pays.

Le lien du profil vient, dans l'ordre : du « site » donné par Google quand c'est une page Facebook/Instagram, des liens
vers ces réseaux trouvés sur le site du commerce (voir core.sourcing.trouver_contact), ou de toi (tu colles le lien trouvé).
Les réseaux reconnus sont un réglage (`domaines_reseaux`). La rédaction est celle des formulaires (core.formulaires), avec
son propre prompt (`prompt_social`) ; le message suit l'angle de la campagne, comme un email.

- Un « pas intéressé » est définitif : le prospect passe en « ignoré » (aucun contact, par aucun canal).
- Les autres résultats ne changent que le journal.

Utilisable seul : python -m core.social check     tests sans réseau, sur base jetable
"""
import json
import logging
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from core import appels, campagnes, db, formulaires, scorer, writer
from core.formulaires import ErreurFormulaire as ErreurSocial
from core.reglages import ErreurValidation

journal = logging.getLogger("prospect")

RESULTATS = {
    "envoye": "Message envoyé",
    "reponse": "A répondu",
    "pas_interesse": "Pas intéressé (ne plus contacter)",
    "introuvable": "Profil introuvable ou messages fermés",
}
ETATS = formulaires.ETATS
_LONGUEUR_MAX_NOTE, _LONGUEUR_MAX_URL = 1000, 500

etat_de = formulaires.etat_de


def lien_recherche(nom, ville, reseaux):
    """Recherche Google toute prête pour trouver le profil d'un commerce sur les réseaux reconnus."""
    sites = " OR ".join(f"site:{r}" for r in reseaux if r)
    return "https://www.google.com/search?q=" + quote_plus(f'"{nom}" {ville or ""} {sites}'.strip())


def numero_whatsapp(telephone, indicatif, prefixes_mobiles):
    """Numéro au format international sans « + » (« 33612345678 ») si c'est un mobile joignable sur WhatsApp, sinon None.
    Accepte « 06 12 34 56 78 » et « +33 6 12 34 56 78 ». Un fixe n'a pas WhatsApp ; l'indicatif et les débuts de mobiles
    sont des réglages (autres pays, autres opérateurs)."""
    chiffres = re.sub(r"\D", "", telephone or "")
    indicatif = re.sub(r"\D", "", indicatif or "")
    if not chiffres or not indicatif:
        return None
    if (telephone or "").lstrip().startswith(("+", "00")) and chiffres.lstrip("0").startswith(indicatif):
        national = "0" + chiffres.lstrip("0")[len(indicatif):]
    else:
        national = chiffres
    if len(national) < 9 or not any(national.startswith(pre) for pre in prefixes_mobiles if pre):
        return None
    return indicatif + national[1:]


def _whatsapp_du_prospect(conn, telephone):
    return numero_whatsapp(telephone, db.get_reglage(conn, "whatsapp_indicatif", ""), db.reglage_json(conn, "whatsapp_prefixes_mobiles"))


def _liens(ligne, reseaux, whatsapp=None):
    """Les façons de joindre ce commerce, la meilleure d'abord : profil connu, WhatsApp (mobile), fiche Google Maps, recherche."""
    liens = []
    if ligne["profil_social_url"]:
        liens.append({"type": "profil", "libelle": "Ouvrir son profil", "url": ligne["profil_social_url"]})
    if whatsapp:
        liens.append({"type": "whatsapp", "libelle": "Écrire sur WhatsApp", "url": "https://wa.me/" + whatsapp})
    if ligne["maps_url"]:
        liens.append({"type": "maps", "libelle": "Fiche Google Maps", "url": ligne["maps_url"]})
    liens.append({"type": "recherche", "libelle": "Chercher son profil", "url": lien_recherche(ligne["nom"], ligne["ville"], reseaux)})
    return liens


_REQUETE = """SELECT p.id, p.nom, p.ville, p.site, p.profil_social_url, p.maps_url, p.formulaire_url, p.telephone, p.score, p.problemes,
                      p.campagne_id, p.message_social, c.nom AS campagne_nom, s.resultat AS dernier_resultat, s.fait_le AS dernier_contact,
                      (SELECT COUNT(*) FROM contacts_social x WHERE x.prospect_id = p.id) AS nb_contacts
                 FROM prospects p JOIN campagnes c ON c.id = p.campagne_id
                 LEFT JOIN contacts_social s ON s.id = (SELECT MAX(id) FROM contacts_social WHERE prospect_id = p.id)
                WHERE p.email IS NULL AND c.est_modele = 0
                  AND (p.profil_social_url IS NOT NULL OR p.formulaire_url IS NULL)
                  AND (p.statut <> 'ignore' OR s.id IS NOT NULL){filtre}"""


def file(conn, campagne_id=None, etat="faire", limite=500):
    """Prospects à contacter en message privé : sans email, avec un profil connu ou sans autre moyen écrit (formulaire). Du
    score le plus haut au plus bas. Renvoie {'lignes', 'compteurs', 'total'} ; les compteurs portent sur la campagne choisie."""
    filtre, valeurs = (" AND p.campagne_id = ?", [int(campagne_id)]) if campagne_id else ("", [])
    reseaux = db.reglage_json(conn, "domaines_reseaux")
    lignes = [dict(l) for l in conn.execute(_REQUETE.format(filtre=filtre), valeurs)]
    compteurs = {cle: 0 for cle in ETATS}
    gardees, cache = [], {}
    for l in lignes:
        l["etat"] = etat_de(l["dernier_resultat"])
        compteurs[l["etat"]] += 1
        if etat not in ETATS or l["etat"] == etat:
            gardees.append(l)
    # Les profils connus d'abord à score égal : ce sont les messages qu'on peut envoyer tout de suite.
    gardees.sort(key=lambda l: (-l["score"], l["profil_social_url"] is None, l["id"]))
    for l in gardees[:limite]:
        l["problemes"] = appels._problemes_concrets(conn, l["campagne_id"], json.loads(l["problemes"]), cache)
        l["score_affiche"] = min(l["score"], scorer.PLAFOND_AFFICHAGE)
        l["a_message"] = bool(l.pop("message_social"))
        l["dernier_libelle"] = RESULTATS.get(l["dernier_resultat"])
        l["whatsapp"] = _whatsapp_du_prospect(conn, l["telephone"])
        l["liens"] = _liens(l, reseaux, l["whatsapp"])
    return {"lignes": gardees[:limite], "compteurs": compteurs, "total": len(gardees)}


def fiche(conn, prospect_id):
    p = conn.execute(
        "SELECT p.*, c.nom AS campagne_nom FROM prospects p JOIN campagnes c ON c.id = p.campagne_id WHERE p.id = ?",
        (prospect_id,)).fetchone()
    if p is None:
        raise ErreurSocial("Prospect introuvable.")
    historique = [dict(h, libelle=RESULTATS.get(h["resultat"], h["resultat"])) for h in conn.execute(
        "SELECT id, fait_le, resultat, note FROM contacts_social WHERE prospect_id = ? ORDER BY id DESC", (prospect_id,))]
    return {"id": p["id"], "nom": p["nom"], "ville": p["ville"], "site": p["site"], "profil_social_url": p["profil_social_url"],
            "campagne": p["campagne_nom"], "score": min(p["score"], scorer.PLAFOND_AFFICHAGE),
            "whatsapp": _whatsapp_du_prospect(conn, p["telephone"]),
            "liens": _liens(p, db.reglage_json(conn, "domaines_reseaux"), _whatsapp_du_prospect(conn, p["telephone"])),
            "problemes": appels._problemes_concrets(conn, p["campagne_id"], json.loads(p["problemes"]), {}),
            "message": p["message_social"] or "", "historique": historique}


def generer_message(cfg, prospect_id):
    """Écrit (ou réécrit) le message privé d'un prospect, avec l'IA, et l'enregistre."""
    return formulaires.generer_message(cfg, prospect_id, canal="social")


def enregistrer_message(conn, prospect_id, texte):
    """Enregistre le message relu et corrigé par l'utilisateur (vide = aucun message)."""
    return formulaires.enregistrer_message(conn, prospect_id, texte, canal="social")


def enregistrer_profil(conn, prospect_id, url):
    """Enregistre le lien du profil que l'utilisateur a trouvé (vide = l'oublier)."""
    url = (url or "").strip()
    if url:
        morceaux = urlsplit(url if "//" in url else "https://" + url)
        if morceaux.scheme not in ("http", "https") or "." not in morceaux.netloc or len(url) > _LONGUEUR_MAX_URL:
            raise ErreurValidation({"profil": "Colle l'adresse complète du profil (par exemple https://www.facebook.com/nom-du-commerce)."})
        url = morceaux.geturl()
    if conn.execute("UPDATE prospects SET profil_social_url = ? WHERE id = ?", (url or None, prospect_id)).rowcount == 0:
        raise ErreurSocial("Prospect introuvable.")
    return {"profil_social_url": url or None}


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
        raise ErreurSocial("Prospect introuvable.")
    conn.execute("INSERT INTO contacts_social (prospect_id, resultat, note) VALUES (?, ?, ?)", (prospect_id, resultat, note))
    ignore = resultat == "pas_interesse" and bool(db.changer_statut(conn, [prospect_id], "ignore")["modifies"])
    journal.info("message privé noté : prospect %s, %s", prospect_id, resultat)
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
        except (ErreurValidation, ErreurSocial) as e:
            return e
        return None

    mobiles = ["06", "07"]
    verifier("WhatsApp : mobile national ou international converti, fixe, numéro trop court ou sans indicatif refusés",
             numero_whatsapp("06 12 34 56 78", "33", mobiles) == "33612345678"
             and numero_whatsapp("+33 7 12 34 56 78", "33", mobiles) == "33712345678"
             and numero_whatsapp("0033 6 12 34 56 78", "33", mobiles) == "33612345678"
             and numero_whatsapp("02 47 12 34 56", "33", mobiles) is None and numero_whatsapp("06 12", "33", mobiles) is None
             and numero_whatsapp("06 12 34 56 78", "", mobiles) is None and numero_whatsapp(None, "33", mobiles) is None
             and numero_whatsapp("06 12 34 56 78", "33", []) is None)
    verifier("lien de recherche : le nom entre guillemets, la ville et chaque réseau reconnu",
             lien_recherche("Chez Léa", "Tours", ["facebook.com", "instagram.com"]).startswith("https://www.google.com/search?q=")
             and "site%3Afacebook.com+OR+site%3Ainstagram.com" in lien_recherche("Chez Léa", "Tours", ["facebook.com", "instagram.com"])
             and "%22Chez+L%C3%A9a%22+Tours" in lien_recherche("Chez Léa", "Tours", ["facebook.com", "instagram.com"]))

    reponse = "Bonjour,\n\nJe suis Gill, développeur web.\n\nVotre boutique n'a pas de site internet.\n\nPuis-je vous montrer un exemple ?\n\nGill,\nDéveloppeur web"
    comportement = {"prompts": []}
    ia_reelle = writer.appeler_ia

    def fausse_ia(cfg, messages):
        comportement["prompts"].append(messages[0]["content"])
        return reponse

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
                cid = campagnes.creer(c, {"nom": "Commerces", "secteur": "boulangerie", "pays": "FR", "langue": "fr"})
                angle = c.execute("INSERT INTO modeles_message (nom, objet_template, prompt_systeme) VALUES ('a', '{nom}', ?)",
                                  (seeds.composer_prompt("Angle : un client cherche les horaires d'une boulangerie sur son téléphone."),)).lastrowid
                campagnes.modifier(c, cid, {"modele_message_id": angle})

                def ajouter(nom, profil, maps, form, email, score, problemes, statut="qualifie"):
                    db.inserer_prospect(c, {"campagne_id": cid, "nom": nom, "ville": "Tours", "email": email, "maps_url": maps,
                                            "source": "google_places", "source_id": "g:" + nom})
                    pid = c.execute("SELECT id FROM prospects WHERE nom = ?", (nom,)).fetchone()[0]
                    c.execute("UPDATE prospects SET score = ?, problemes = ?, statut = ?, profil_social_url = ?, formulaire_url = ? WHERE id = ?",
                              (score, json.dumps(problemes), statut, profil, form, pid))
                    return pid
                a = ajouter("A profil", "https://www.facebook.com/a", "https://maps.google.com/a", None, None, 9, ["sans_site"])
                b = ajouter("B sans rien", None, "https://maps.google.com/b", None, None, 9, ["sans_site"])
                e = ajouter("E email", "https://www.facebook.com/e", None, None, "e@e.fr", 9, ["sans_site"])
                f = ajouter("F formulaire seul", None, None, "https://f.fr/contact", None, 9, ["sans_site"])
                g = ajouter("G formulaire et profil", "https://www.instagram.com/g", None, "https://g.fr/contact", None, 5, ["sans_site"])
                i = ajouter("I ignoré", "https://www.facebook.com/i", None, None, None, 9, ["sans_site"], statut="ignore")
                c.execute("UPDATE prospects SET telephone = '06 12 34 56 78' WHERE id = ?", (b,))  # mobile : WhatsApp
                c.execute("UPDATE prospects SET telephone = '02 47 00 00 00' WHERE id = ?", (a,))  # fixe : pas de WhatsApp

                q = file(c)
                verifier("file : sans email, profil connu ou aucun autre moyen écrit ; un formulaire seul va dans Formulaires ; ignorés exclus ; "
                         "profil connu d'abord à score égal",
                         [l["nom"] for l in q["lignes"]] == ["A profil", "B sans rien", "G formulaire et profil"] and q["compteurs"]["faire"] == 3)
                verifier("liens : profil, fiche Maps et recherche pour A (fixe : pas de WhatsApp) ; WhatsApp, Maps et recherche pour B (mobile) ; recherche seule si rien d'autre",
                         [x["type"] for x in q["lignes"][0]["liens"]] == ["profil", "maps", "recherche"]
                         and [x["type"] for x in q["lignes"][1]["liens"]] == ["whatsapp", "maps", "recherche"]
                         and q["lignes"][1]["liens"][0]["url"] == "https://wa.me/33612345678" and q["lignes"][0]["whatsapp"] is None
                         and [x["type"] for x in q["lignes"][2]["liens"]] == ["profil", "recherche"])
                verifier("vrai problème en clair, pas encore de message", q["lignes"][0]["problemes"] == ["Aucun site web"] and not q["lignes"][0]["a_message"])

                r = enregistrer_profil(c, b, "instagram.com/boulangerie-b")
                verifier("lien collé : adresse complète enregistrée, la ligne passe en tête avec son profil",
                         r["profil_social_url"] == "https://instagram.com/boulangerie-b"
                         and [x["type"] for x in file(c)["lignes"][0]["liens"]][0] == "profil")
                verifier("lien invalide ou prospect inconnu : refusé, rien d'écrit",
                         "profil" in leve(enregistrer_profil, c, b, "pas une adresse").erreurs and "introuvable" in str(leve(enregistrer_profil, c, 9999, "https://x.fr/y"))
                         and fiche(c, b)["profil_social_url"] == "https://instagram.com/boulangerie-b")
                enregistrer_profil(c, b, "")
                verifier("lien vidé : oublié", fiche(c, b)["profil_social_url"] is None)

                verifier("résultat inconnu refusé, prospect inconnu refusé, rien d'écrit",
                         "resultat" in leve(noter, c, a, "peut-etre").erreurs and "introuvable" in str(leve(noter, c, 9999, "envoye"))
                         and c.execute("SELECT COUNT(*) FROM contacts_social").fetchone()[0] == 0)
                noter(c, a, "envoye", "Message Facebook le 13/10")
                verifier("message envoyé : sort de « à faire », apparaît dans « envoyés »",
                         [l["nom"] for l in file(c)["lignes"]] == ["B sans rien", "G formulaire et profil"]
                         and [l["nom"] for l in file(c, etat="envoyes")["lignes"]] == ["A profil"])
                noter(c, a, "reponse", "Intéressé")
                noter(c, b, "introuvable", "Aucun profil")
                verifier("réponse : « réponses » ; profil introuvable : « clôturés » ; statuts inchangés",
                         [l["nom"] for l in file(c, etat="reponses")["lignes"]] == ["A profil"]
                         and [l["nom"] for l in file(c, etat="clos")["lignes"]] == ["B sans rien"]
                         and c.execute("SELECT statut FROM prospects WHERE id = ?", (b,)).fetchone()[0] == "qualifie")
                r = noter(c, g, "pas_interesse")
                verifier("pas intéressé : prospect ignoré (aucun contact), toujours visible dans « clôturés »",
                         r == {"etat": "clos", "ignore": True} and "G formulaire et profil" in [l["nom"] for l in file(c, etat="clos")["lignes"]]
                         and c.execute("SELECT statut FROM prospects WHERE id = ?", (g,)).fetchone()[0] == "ignore")
                fi = fiche(c, a)
                verifier("fiche : historique du plus récent au plus ancien, avec libellés",
                         [h["resultat"] for h in fi["historique"]] == ["reponse", "envoye"] and fi["historique"][0]["libelle"] == "A répondu")

            # --- Le message ---
            texte = generer_message(cfg, a)
            prompt = comportement["prompts"][0]
            with db.connexion() as c:
                enregistre = fiche(c, a)["message"]
                colonne_formulaire = c.execute("SELECT message_formulaire FROM prospects WHERE id = ?", (a,)).fetchone()[0]
            verifier("message : prompt « message privé » avec le vrai problème, l'angle de la niche et la signature ; enregistré dans sa propre colonne",
                     "message privé" in prompt and "- Aucun site web" in prompt and "Angle : un client cherche les horaires" in prompt
                     and "{angle}" not in prompt and "{signature}" not in prompt and enregistre == texte and colonne_formulaire is None)
            verifier("message : une seule signature, à la fin", texte.count("Développeur web") == 1 and texte.endswith("Développeur web"))
            with db.connexion() as c:
                enregistrer_message(c, a, "  Mon message corrigé.  ")
                verifier("message corrigé à la main : enregistré rogné ; vide : supprimé ; trop long : refusé",
                         fiche(c, a)["message"] == "Mon message corrigé." and enregistrer_message(c, a, "") is None
                         and fiche(c, a)["message"] == "" and "message" in leve(enregistrer_message, c, a, "x" * 4000).erreurs)

                # --- Suppression d'une campagne : le journal part avec elle ---
                campagnes.supprimer(c, cid, forcer=True)
                verifier("campagne supprimée : prospects et journal de messages privés disparaissent, aucune ligne orpheline",
                         c.execute("SELECT COUNT(*) FROM contacts_social").fetchone()[0] == 0 and c.execute("PRAGMA foreign_key_check").fetchall() == [])
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
    print("Usage : python -m core.social check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
