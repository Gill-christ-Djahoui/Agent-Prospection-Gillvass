"""Assistant de niche : quelques réponses -> une campagne prête (requêtes, angle de message, poids des critères, zones).

L'IA ne fait que PROPOSER. Rien n'est écrit en base avant que tu aies relu la proposition et cliqué sur « Créer ».
Chaque zone est localisée puis contrôlée : une commune trouvée à plus de `niche_distance_max_km` de la ville principale
est un homonyme d'une autre région (« Saint-Priest » en Ardèche) et n'est pas ajoutée.

Utilisable seul : python -m core.niche check     tests sans réseau, sur base jetable
"""
import json
import logging
import math
import re
import sys
import tempfile
from pathlib import Path

from core import campagnes, db, reglages, seeds, sourcing, writer
from core.reglages import ErreurValidation

journal = logging.getLogger("prospect")

SALUTATIONS = {"aucun": seeds.REGLE_SALUTATION_NEUTRE, "docteur": seeds.REGLE_SALUTATION_DEFAUT,
               "maitre": seeds.REGLE_SALUTATION_AVOCATS}
_RAYON_VILLE_KM, _RAYON_QUARTIER_KM, _RAYON_COMMUNE_KM = 3, 2, 4
_OBJET_SECOURS = "{nom} : une question"


class ErreurNiche(Exception):
    """L'assistant ne peut pas continuer : le message est affiché tel quel."""


# --------------------------------------------------------------------------------------
# Outils
# --------------------------------------------------------------------------------------


def distance_km(lat1, lon1, lat2, lon2):
    """Distance à vol d'oiseau entre deux points (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _texte(valeur, maximum=200):
    return " ".join(str(valeur or "").split())[:maximum]


def _extraire_json(texte):
    """L'objet JSON d'une réponse d'IA, même entouré de texte ou d'une clôture ```json."""
    debut, fin = texte.find("{"), texte.rfind("}")
    if debut < 0 or fin <= debut:
        raise ErreurNiche("L'IA n'a pas renvoyé de proposition lisible. Réessaie ; si ça se répète, change de modèle dans Réglages.")
    try:
        donnees = json.loads(texte[debut:fin + 1])
    except ValueError:
        raise ErreurNiche("La proposition de l'IA est mal formée. Réessaie ; si ça se répète, change de modèle dans Réglages.") from None
    if not isinstance(donnees, dict):
        raise ErreurNiche("La proposition de l'IA est inattendue. Réessaie.")
    return donnees


def _sans_variables_inconnues(texte):
    """Retire les {variables} que le prompt d'un angle n'accepte pas (une IA en invente parfois : {prenom})."""
    return re.sub(r"\{(\w*)\}", lambda m: m.group(0) if m.group(1) in writer.VARIABLES_PROMPT else "", texte)


def criteres_globaux(conn):
    return [dict(l) for l in conn.execute(
        "SELECT code, libelle, points FROM criteres WHERE campagne_id IS NULL ORDER BY id")]


def _champs_requis(champs):
    obligatoires = {"niche": "Dis quelle niche tu vises.", "offre": "Dis ce que tu proposes.", "ville": "Indique la ville principale."}
    erreurs = {cle: message for cle, message in obligatoires.items() if not _texte(champs.get(cle))}
    pays = _texte(champs.get("pays") or "FR")
    langue = _texte(champs.get("langue") or "fr")
    if not re.fullmatch(r"[A-Za-z]{2}", pays):
        erreurs["pays"] = "Code pays à 2 lettres (FR, BE, CH…)."
    if not re.fullmatch(r"[A-Za-z]{2}", langue):
        erreurs["langue"] = "Code langue à 2 lettres (fr, en…)."
    if erreurs:
        raise ErreurValidation(erreurs)
    titre = champs.get("titre") if champs.get("titre") in SALUTATIONS else "aucun"
    return {"niche": _texte(champs["niche"], 120), "offre": _texte(champs["offre"], 400),
            "probleme": _texte(champs.get("probleme"), 400) or "non précisé : à toi de choisir le plus pertinent pour cette offre",
            "ville": _texte(champs["ville"], 100), "pays": pays.upper(), "langue": langue.lower(),
            "agglomeration": bool(champs.get("agglomeration")), "titre": titre, "nom": _texte(champs.get("nom"), 200)}


# --------------------------------------------------------------------------------------
# Étape 1 : la proposition de l'IA (rien n'est écrit en base)
# --------------------------------------------------------------------------------------


def proposer(cfg, champs, criteres):
    """Proposition complète de campagne à partir des réponses de l'utilisateur. Lève ErreurValidation / ErreurNiche."""
    c = _champs_requis(champs)
    manquants = writer.configuration_manquante(cfg)
    if manquants:
        raise ErreurNiche("L'IA n'est pas prête : il manque " + ", ".join(manquants) + " (Réglages).")
    maximum = int(cfg.get("niche_zones_max") or 30)
    instruction = (f"liste de 12 à {maximum} zones à couvrir autour de la ville principale : d'abord ses quartiers ou arrondissements "
                   '(type "quartier"), puis les communes voisines de son agglomération (type "commune"), sous la forme '
                   '[{"nom": "...", "type": "quartier"}] ; uniquement des lieux réels à moins de 30 km de la ville principale ; '
                   "écris le nom tel qu'on le trouve sur une carte, avec la ville quand le nom est ambigu"
                   if c["agglomeration"] else "[] (une liste vide : seule la ville principale est couverte)")
    prompt = writer.rendre(cfg["prompt_niche"], {
        "niche": c["niche"], "offre": c["offre"], "probleme": c["probleme"], "ville": c["ville"], "pays": c["pays"],
        "criteres": "\n".join(f"- {k['code']} : {k['libelle']} (poids actuel {k['points']})" for k in criteres),
        "zones_instruction": instruction})
    try:
        reponse = writer.appeler_ia(cfg, [{"role": "user", "content": prompt}])
    except writer.ErreurRedaction as erreur:
        raise ErreurNiche(str(erreur)) from erreur
    donnees = _extraire_json(reponse)

    secteur = _texte(donnees.get("secteur"), 100) or c["niche"]
    requetes = campagnes._liste_requetes([_texte(r, 80) for r in (donnees.get("requetes") or []) if isinstance(r, str)])[:6] or [c["niche"]]
    angle = re.sub(r"(?i)\bangle\s*:\s*", "", str(donnees.get("angle") or "")).strip()  # l'IA répète parfois « Angle : »
    if len(angle) < 40:
        raise ErreurNiche("L'IA n'a pas écrit l'angle du message. Réessaie.")
    angle = "Angle : " + angle
    objet = _texte(donnees.get("objet"), 120) or _OBJET_SECOURS
    if _sans_variables_inconnues(objet) != objet:
        objet = _OBJET_SECOURS
    angle = _sans_variables_inconnues(angle)

    proposes = donnees.get("criteres") if isinstance(donnees.get("criteres"), dict) else {}
    lignes = []
    for k in criteres:
        valeur = proposes.get(k["code"])
        points = valeur if isinstance(valeur, int) and not isinstance(valeur, bool) and 0 <= valeur <= 10 else k["points"]
        lignes.append({"code": k["code"], "libelle": k["libelle"], "points_global": k["points"], "points": points})

    zones = [{"nom": c["ville"], "rayon_km": _RAYON_VILLE_KM}]
    if c["agglomeration"]:
        vues = {c["ville"].lower()}
        for z in donnees.get("zones") or []:
            nom = _texte(z.get("nom") if isinstance(z, dict) else z, 100)
            if not nom or nom.lower() in vues:
                continue
            vues.add(nom.lower())
            quartier = isinstance(z, dict) and z.get("type") == "quartier"
            zones.append({"nom": nom, "rayon_km": _RAYON_QUARTIER_KM if quartier else _RAYON_COMMUNE_KM})
    return {"nom": c["nom"] or f"{secteur[:1].upper()}{secteur[1:]} — {c['ville']}", "secteur": secteur, "pays": c["pays"],
            "langue": c["langue"], "requetes": requetes, "objet": objet, "angle": angle, "titre": c["titre"],
            "criteres": lignes, "zones": zones[:maximum + 1]}


# --------------------------------------------------------------------------------------
# Étape 2 : création (après relecture par l'utilisateur)
# --------------------------------------------------------------------------------------


def _localiser(cfg, zones, pays, geocoder):
    """Zones localisées et contrôlées : [(nom, rayon, lat, lon)], [(nom, raison)] pour celles qui sont écartées."""
    maximum_km = int(cfg.get("niche_distance_max_km") or 40)
    centre_nom = zones[0]["nom"]
    g = geocoder(centre_nom, pays)
    if g["statut"] == "erreur":
        raise ErreurNiche(f"Localisation impossible pour « {centre_nom} » : {g.get('message', 'service indisponible')}")
    if g["statut"] != "ok":
        raise ErreurNiche(f"Ville principale « {centre_nom} » introuvable : vérifie l'orthographe.")
    centre = (g["lat"], g["lon"])
    retenues, ecartees = [(centre_nom, zones[0]["rayon_km"], *centre)], []
    for z in zones[1:]:
        for essai in (z["nom"], f"{z['nom']}, {centre_nom}"):  # un homonyme lointain : on précise la ville principale
            g = geocoder(essai, pays)
            if g["statut"] == "erreur":
                ecartees.append((z["nom"], "localisation en erreur : " + g.get("message", "")))
                break
            if g["statut"] == "ok" and distance_km(*centre, g["lat"], g["lon"]) <= maximum_km:
                retenues.append((essai, z["rayon_km"], g["lat"], g["lon"]))  # le nom qui a marché : sans ambiguïté (« 3e arrondissement, Lyon »)
                break
        else:
            ecartees.append((z["nom"], f"introuvable à moins de {maximum_km} km de {centre_nom}"))
    return retenues, ecartees


def creer(cfg, proposition, geocoder=None):
    """Crée la campagne (brouillon) à partir de la proposition relue. Renvoie {'campagne_id', 'zones', 'ecartees'}."""
    geocoder = geocoder or (lambda ville, pays: sourcing.geocoder(cfg, ville, pays))
    erreurs = {}
    nom, secteur = _texte(proposition.get("nom"), 200), _texte(proposition.get("secteur"), 200)
    pays, langue = _texte(proposition.get("pays")), _texte(proposition.get("langue"))
    requetes = campagnes._liste_requetes(proposition.get("requetes") or [])
    objet, angle = _texte(proposition.get("objet"), 200), (proposition.get("angle") or "").strip()
    for cle, valeur, message in (("nom", nom, "Donne un nom à la campagne."), ("secteur", secteur, "Indique le secteur."),
                                 ("requetes", requetes, "Garde au moins une recherche."), ("objet", objet, "L'objet est vide."),
                                 ("angle", angle, "L'angle du message est vide.")):
        if not valeur:
            erreurs[cle] = message
    zones = []
    for z in proposition.get("zones") or []:
        z_nom = _texte(z.get("nom") if isinstance(z, dict) else z, 100)
        try:
            rayon = int(float((z.get("rayon_km") if isinstance(z, dict) else None) or _RAYON_COMMUNE_KM))
        except (TypeError, ValueError):
            rayon = _RAYON_COMMUNE_KM
        if z_nom and 0 < rayon <= 100 and z_nom.lower() not in {x["nom"].lower() for x in zones}:
            zones.append({"nom": z_nom, "rayon_km": rayon})
    if not zones:
        erreurs["zones"] = "Garde au moins la ville principale."
    maximum = int(cfg.get("niche_zones_max") or 30)
    if len(zones) > maximum + 1:
        erreurs["zones"] = f"{len(zones)} zones : {maximum} au maximum (Réglages)."
    titre = proposition.get("titre") if proposition.get("titre") in SALUTATIONS else "aucun"
    try:
        campagnes._verifier_variables(objet + " " + angle)
    except ValueError as e:
        erreurs["angle"] = str(e)
    if erreurs:
        raise ErreurValidation(erreurs)

    retenues, ecartees = _localiser(cfg, zones, pays or "FR", geocoder)  # le réseau d'abord : la base n'est pas touchée avant

    with db.connexion() as conn:
        modele = conn.execute(
            "INSERT INTO modeles_message (nom, objet_template, prompt_systeme, signature) VALUES (?, ?, ?, '')",
            (f"Angle {secteur}"[:120], objet, seeds.composer_prompt(angle, SALUTATIONS[titre]))).lastrowid
        campagne_id = campagnes.creer(conn, {"nom": nom, "secteur": secteur, "pays": pays or "FR", "langue": langue or "fr"})
        copie_par_defaut = conn.execute("SELECT modele_message_id FROM campagnes WHERE id = ?", (campagne_id,)).fetchone()[0]
        campagnes.modifier(conn, campagne_id, {"requetes": requetes, "modele_message_id": modele})
        if copie_par_defaut:  # l'angle par défaut donné à toute campagne neuve est remplacé par celui qui vient d'être relu
            conn.execute("DELETE FROM modeles_message WHERE id = ?", (copie_par_defaut,))
        for z_nom, rayon, lat, lon in retenues:
            campagnes.ajouter_zone(conn, campagne_id, z_nom, pays or "FR", rayon,
                                   lambda ville, p, lat=lat, lon=lon: {"statut": "ok", "lat": lat, "lon": lon})
        globaux = {k["code"]: k["points"] for k in criteres_globaux(conn)}
        for ligne in proposition.get("criteres") or []:
            code, points = ligne.get("code"), ligne.get("points")
            try:
                points = int(points)
            except (TypeError, ValueError):
                continue
            if code in globaux and 0 <= points <= 100 and points != globaux[code]:
                campagnes.definir_critere(conn, campagne_id, code, points, True)
    journal.info("campagne %s créée par l'assistant de niche : %s zone(s), %s écartée(s)", campagne_id, len(retenues), len(ecartees))
    return {"campagne_id": campagne_id, "zones": len(retenues), "ecartees": [{"nom": n, "raison": r} for n, r in ecartees]}


# --------------------------------------------------------------------------------------
# Auto-tests
# --------------------------------------------------------------------------------------


def _autotests():
    import config

    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    def leve(fonction, *args):
        try:
            fonction(*args)
        except (ErreurValidation, ErreurNiche) as e:
            return e
        return None

    verifier("distance : Lyon-Villeurbanne ≈ 4 km, Lyon-Privas ≈ 100 km",
             3 < distance_km(45.7578, 4.8320, 45.7661, 4.8796) < 5 and 90 < distance_km(45.7578, 4.8320, 44.7177, 4.5469) < 130)

    reponse_ia = ('Voici la proposition :\n```json\n' + json.dumps({
        "secteur": "cabinet de kinésithérapie", "requetes": ["kinésithérapeute", "Kinésithérapeute", "cabinet de kinésithérapie"],
        "objet": "{nom} : vos patients vous trouvent-ils ?", "angle": "Angle : les patients cherchent un kiné près de chez eux et choisissent celui qu'ils trouvent vite.",
        "criteres": {"sans_site": 9, "pas_rdv": 5, "inconnu": 4, "lent": 99, "peu_avis": "beaucoup", "pas_h1": True},
        "zones": [{"nom": "Lyon 3e Arrondissement", "type": "quartier"}, {"nom": "Villeurbanne", "type": "commune"},
                  {"nom": "Saint-Priest", "type": "commune"}, {"nom": "Villeurbanne", "type": "commune"}, {"nom": "Atlantide", "type": "commune"}]},
        ensure_ascii=False) + "\n```\nBonne chance !")
    comportement = {"reponse": reponse_ia, "appels": []}
    ia_reelle = writer.appeler_ia

    def fausse_ia(cfg, messages):
        comportement["appels"].append(messages[0]["content"])
        if isinstance(comportement["reponse"], Exception):
            raise comportement["reponse"]
        return comportement["reponse"]

    # Un géocodeur simulé : Saint-Priest tombe d'abord dans le département voisin (homonyme), puis au bon endroit avec la précision.
    points = {"Lyon": (45.7578, 4.8320), "Lyon 3e Arrondissement": (45.7556, 4.8623), "Villeurbanne": (45.7661, 4.8796),
              "Saint-Priest": (44.7177, 4.5469), "Saint-Priest, Lyon": (45.6965, 4.9442)}
    demandes = []

    def geocodeur(ville, pays):
        demandes.append(ville)
        if ville == "Panne":
            return {"statut": "erreur", "message": "réseau coupé"}
        return {"statut": "ok", "lat": points[ville][0], "lon": points[ville][1]} if ville in points else {"statut": "introuvable"}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ancien = config.DB_PATH
        config.DB_PATH = Path(tmp) / "t.db"
        writer.appeler_ia = fausse_ia
        try:
            db.init_db(importer_env=False)
            with db.connexion() as c:
                cfg = reglages.valeurs_effectives(c)
                globaux = criteres_globaux(c)
            cfg.update(ai_endpoint="https://ia.exemple/v1", ai_model="m", ai_key="k", expediteur_nom="Gill")
            champs = {"niche": "kinésithérapeute", "offre": "site vitrine avec prise de rendez-vous", "probleme": "pas de site",
                      "ville": "Lyon", "agglomeration": True, "titre": "docteur"}

            # --- Proposition ---
            p = proposer(cfg, champs, globaux)
            prompt_envoye = comportement["appels"][0]
            verifier("le prompt envoyé à l'IA contient la niche, l'offre, la ville et la liste des critères avec leur poids",
                     "kinésithérapeute" in prompt_envoye and "site vitrine" in prompt_envoye and "Lyon (FR)" in prompt_envoye
                     and "- sans_site : Aucun site web (poids actuel 10)" in prompt_envoye and "{nom}" in prompt_envoye
                     and "{criteres}" not in prompt_envoye and "{zones_instruction}" not in prompt_envoye)
            verifier("proposition lue malgré le texte et la clôture autour du JSON ; requêtes dédoublonnées ; nom de campagne par défaut",
                     p["secteur"] == "cabinet de kinésithérapie" and p["requetes"] == ["kinésithérapeute", "cabinet de kinésithérapie"]
                     and p["nom"] == "Cabinet de kinésithérapie — Lyon" and p["objet"] == "{nom} : vos patients vous trouvent-ils ?"
                     and p["angle"].startswith("Angle :"))
            poids = {k["code"]: (k["points"], k["points_global"]) for k in p["criteres"]}
            verifier("poids des critères : valeurs valides reprises ; code inconnu, poids hors 0-10, texte ou booléen ignorés (poids global gardé)",
                     poids["sans_site"] == (9, 10) and poids["pas_rdv"] == (5, 3) and poids["lent"] == (3, 3)
                     and poids["peu_avis"] == (2, 2) and poids["pas_h1"] == (1, 1) and len(p["criteres"]) == len(globaux))
            verifier("zones : ville principale d'abord (3 km), doublon retiré, quartier 2 km, communes 4 km",
                     [(z["nom"], z["rayon_km"]) for z in p["zones"]] == [("Lyon", 3), ("Lyon 3e Arrondissement", 2), ("Villeurbanne", 4),
                                                                         ("Saint-Priest", 4), ("Atlantide", 4)])
            sans_agglo = proposer(cfg, {**champs, "agglomeration": False}, globaux)
            verifier("sans « couvrir l'agglomération » : la seule ville principale", [z["nom"] for z in sans_agglo["zones"]] == ["Lyon"])

            comportement["reponse"] = 'Désolé, je ne peux pas.'
            verifier("réponse sans JSON : erreur claire", "lisible" in str(leve(proposer, cfg, champs, globaux)))
            comportement["reponse"] = '{"secteur": "x", "angle": "trop court"}'
            verifier("angle absent ou trop court : erreur claire", "angle" in str(leve(proposer, cfg, champs, globaux)))
            comportement["reponse"] = json.dumps({"secteur": "x", "requetes": [], "objet": "{prenom}, bonjour",
                                                  "angle": "Angle : " + "une phrase assez longue pour passer le contrôle {ville}. " * 2})
            q = proposer(cfg, {**champs, "agglomeration": False}, globaux)
            verifier("variable inconnue dans l'objet : objet de secours ; aucune requête proposée : la niche elle-même",
                     q["objet"] == "{nom} : une question" and q["requetes"] == ["kinésithérapeute"])
            comportement["reponse"] = writer.ErreurRedaction("Le fournisseur d'IA refuse la requête (HTTP 401)")
            verifier("IA en erreur : message du fournisseur repris", "HTTP 401" in str(leve(proposer, cfg, champs, globaux)))
            comportement["reponse"] = reponse_ia
            verifier("réglages d'IA manquants : on dit lesquels, sans appel", "la clé" in str(leve(proposer, {**cfg, "ai_key": ""}, champs, globaux)))
            erreurs = leve(proposer, cfg, {"niche": "", "offre": " ", "ville": "", "pays": "FRA"}, globaux).erreurs
            verifier("champs obligatoires : une erreur par champ", set(erreurs) == {"niche", "offre", "ville", "pays"})

            # --- Création ---
            resultat = creer(cfg, {**p, "nom": "Kinés — Lyon"}, geocodeur)
            with db.connexion() as c:
                fiche = campagnes.lire(c, resultat["campagne_id"])
                criteres = {x["code"]: (x["points"], x["personnalise"]) for x in fiche["criteres"]}
                prompt_campagne = fiche["modele"]["prompt_systeme"]
                nb_campagnes = c.execute("SELECT COUNT(*) FROM campagnes").fetchone()[0]
                global_sans_site = c.execute("SELECT points FROM criteres WHERE campagne_id IS NULL AND code = 'sans_site'").fetchone()[0]
            verifier("campagne créée en brouillon avec ses requêtes, son secteur, son pays",
                     fiche["campagne"]["statut"] == "brouillon" and fiche["campagne"]["secteur"] == "cabinet de kinésithérapie"
                     and fiche["campagne"]["requetes"] == ["kinésithérapeute", "cabinet de kinésithérapie"] and fiche["campagne"]["pays"] == "FR")
            verifier("angle : règles communes (court, aéré, sans jargon) + angle de la niche + règle de salutation « Docteur », aucune variable orpheline",
                     "Entre 45 et 70 mots" in prompt_campagne and "Angle : les patients cherchent un kiné" in prompt_campagne
                     and "Bonjour Docteur" in prompt_campagne and "{angle}" not in prompt_campagne and "{regle_salutation}" not in prompt_campagne
                     and fiche["modele"]["objet_template"] == "{nom} : vos patients vous trouvent-ils ?")
            noms_zones = [z["ville"] for z in fiche["zones"]]
            verifier("zones : celles qui sont localisées dans la métropole sont ajoutées, avec leurs coordonnées",
                     noms_zones == ["Lyon", "Lyon 3e Arrondissement", "Villeurbanne", "Saint-Priest, Lyon"] and all(z["lat"] is not None for z in fiche["zones"]))
            verifier("homonyme lointain : la ville principale est ajoutée à la requête, le bon point retenu (à ~12 km, pas en Ardèche) et la zone nommée sans ambiguïté",
                     "Saint-Priest, Lyon" in demandes and 5 < distance_km(45.7578, 4.8320, *[(z["lat"], z["lon"]) for z in fiche["zones"] if z["ville"] == "Saint-Priest, Lyon"][0]) < 20)
            comportement["reponse"] = json.dumps({"secteur": "x", "requetes": ["x"], "objet": "{nom} : une question",
                                                  "angle": "Angle : Relie le problème constaté à la perte de clients. Angle : Reste simple et concret, sans promesse."})
            verifier("angle : « Angle : » répété par l'IA n'est gardé qu'une fois",
                     proposer(cfg, {**champs, "agglomeration": False}, globaux)["angle"].count("Angle :") == 1)
            comportement["reponse"] = reponse_ia
            verifier("zone introuvable : écartée avec sa raison, sans bloquer la création",
                     [e["nom"] for e in resultat["ecartees"]] == ["Atlantide"] and "introuvable" in resultat["ecartees"][0]["raison"])
            verifier("critères : seuls ceux qui diffèrent du poids global sont personnalisés ; les globaux ne bougent pas",
                     criteres["sans_site"] == (9, 1) and criteres["pas_rdv"] == (5, 1) and criteres["lent"] == (3, 0)
                     and global_sans_site == 10)

            # Échecs : rien n'est écrit en base
            with db.connexion() as c:
                angles_avant = c.execute("SELECT COUNT(*) FROM modeles_message").fetchone()[0]
            erreur_ville = leve(creer, cfg, {**p, "zones": [{"nom": "Atlantide", "rayon_km": 3}]}, geocodeur)
            erreur_panne = leve(creer, cfg, {**p, "zones": [{"nom": "Panne", "rayon_km": 3}]}, geocodeur)
            with db.connexion() as c:
                inchange = (c.execute("SELECT COUNT(*) FROM campagnes").fetchone()[0] == nb_campagnes
                            and c.execute("SELECT COUNT(*) FROM modeles_message").fetchone()[0] == angles_avant)
            verifier("ville principale introuvable ou service en panne : erreur claire, aucune campagne ni angle créés",
                     "introuvable" in str(erreur_ville) and "réseau coupé" in str(erreur_panne) and inchange)
            erreurs = leve(creer, cfg, {"nom": "", "secteur": "", "requetes": [], "objet": "", "angle": "", "zones": []}, geocodeur).erreurs
            verifier("proposition vidée par l'utilisateur : toutes les erreurs signalées, rien d'écrit",
                     set(erreurs) == {"nom", "secteur", "requetes", "objet", "angle", "zones"})
            verifier("variable inconnue dans l'angle relu : refusée avec la liste autorisée",
                     "{prenom}" in leve(creer, cfg, {**p, "angle": "Angle : bonjour {prenom}, ceci est un angle assez long."}, geocodeur).erreurs["angle"])
            trop = [{"nom": f"Zone {i}", "rayon_km": 2} for i in range(40)]
            verifier("trop de zones : refusé avec la limite des Réglages", "30 au maximum" in leve(creer, cfg, {**p, "zones": trop}, geocodeur).erreurs["zones"])
            maitre = creer(cfg, {**p, "nom": "Avocats", "titre": "maitre", "zones": [{"nom": "Lyon", "rayon_km": 3}]}, geocodeur)
            neutre = creer(cfg, {**p, "nom": "Autres", "titre": "aucun", "zones": [{"nom": "Lyon", "rayon_km": 3}]}, geocodeur)
            with db.connexion() as c:
                prompts = [campagnes.lire(c, r["campagne_id"])["modele"]["prompt_systeme"] for r in (maitre, neutre)]
            verifier("titre de salutation : « Maître » pour les avocats, « Bonjour, » seul sinon (jamais de prénom)",
                     "Bonjour Maître" in prompts[0] and "Bonjour Docteur" not in prompts[1] and "Salue toujours par « Bonjour, »" in prompts[1])
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
    print("Usage : python -m core.niche check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
