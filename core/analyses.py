"""Analyses de performance et recommandations de décision.

Rien ici n'est de l'IA : ce sont des statistiques calculées sur TES envois, avec des règles de décision prudentes.
Une recommandation n'apparaît que si l'échantillon est assez grand, et une comparaison entre deux groupes ne
est retenue que si elle est statistiquement crédible (test z sur deux proportions) : mieux vaut « pas assez de
données » qu'une conclusion fausse. Toutes les valeurs de seuil sont dans Réglages.

Les taux se calculent par PROSPECT contacté (pas par email : une relance ne doit pas gonfler les chiffres).
Les ouvertures sont indicatives (les messageries qui protègent la vie privée en simulent) ; les RÉPONSES sont le vrai signal.

Utilisable seul : python -m core.analyses          résumé de la vraie base
                  python -m core.analyses check    tests sur base jetable
"""
import json
import math
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core import campagnes, db, messages, reglages, sender, suivi

_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_DIAGNOSTICS = ("site_inconnu", "non_analysable")


def _pct(x, n):
    return round(100 * x / n, 1) if n else None


def z_deux_proportions(x1, n1, x2, n2):
    """Statistique z de la différence entre deux proportions (0 si indéterminée). |z| ≥ 1,64 ≈ crédible à 90 %."""
    if not n1 or not n2:
        return 0.0
    p = (x1 + x2) / (n1 + n2)
    variance = p * (1 - p) * (1 / n1 + 1 / n2)
    return 0.0 if variance <= 0 else (x1 / n1 - x2 / n2) / math.sqrt(variance)


# --------------------------------------------------------------------------------------
# Collecte
# --------------------------------------------------------------------------------------


def collecter(conn, cfg, jours=None, maintenant=None):
    """Un enregistrement par prospect contacté (au moins un email réellement envoyé)."""
    maintenant = maintenant or datetime.now(timezone.utc)
    fuseau = ZoneInfo(cfg["fuseau"])
    limite = (maintenant - timedelta(days=jours)).strftime(_FORMAT) if jours else None
    lignes = conn.execute(
        """SELECT p.id, p.campagne_id, p.ville, p.score, p.source, p.problemes, p.statut,
                  c.nom AS campagne_nom, c.modele_message_id, mm.nom AS angle_nom,
                  MIN(e.envoye_le) AS premier_envoi, COUNT(e.id) AS nb_emails,
                  MAX(e.statut IN ('ouvert', 'repondu')) AS ouvert, MAX(e.statut = 'repondu') AS repondu_envoi
             FROM prospects p
             JOIN messages m ON m.prospect_id = p.id
             JOIN envois e ON e.message_id = m.id AND e.statut IN ('envoye', 'ouvert', 'repondu')
             JOIN campagnes c ON c.id = p.campagne_id
             LEFT JOIN modeles_message mm ON mm.id = c.modele_message_id
            GROUP BY p.id""").fetchall()
    enregistrements = []
    for l in lignes:
        if limite and l["premier_envoi"] < limite:
            continue
        local = datetime.strptime(l["premier_envoi"], _FORMAT).replace(tzinfo=timezone.utc).astimezone(fuseau)
        enregistrements.append({
            "id": l["id"], "campagne": l["campagne_nom"], "campagne_id": l["campagne_id"],
            "angle": l["angle_nom"] or "(sans angle)", "ville": (l["ville"] or "—").strip() or "—",
            "score": l["score"], "source": l["source"], "problemes": json.loads(l["problemes"]),
            "ouvert": bool(l["ouvert"]) or l["statut"] == "repondu",
            "repondu": bool(l["repondu_envoi"]) or l["statut"] == "repondu",
            "emails": l["nb_emails"], "heure": local.hour, "jour": JOURS[local.weekday()],
        })
    return enregistrements


def tranche_score(score, bornes):
    """Libellé de la tranche d'un score brut, d'après les bornes des réglages (ex. [5, 8, 11])."""
    bornes = sorted(bornes)
    if not bornes or score < bornes[0]:
        return f"moins de {bornes[0]}" if bornes else "tous"
    for i, b in enumerate(bornes):
        suivante = bornes[i + 1] if i + 1 < len(bornes) else None
        if suivante is None:
            return f"{b} et plus"
        if score < suivante:
            return f"{b} à {suivante - 1}"
    return "tous"


def agreger(enregistrements, cle, ordre=None):
    """Regroupe : [{'cle', 'contactes', 'ouverts', 'reponses', 'taux_ouverture', 'taux_reponse'}], par nombre de contacts."""
    groupes = {}
    for e in enregistrements:
        for k in (cle(e) if not isinstance(cle(e), str) else [cle(e)]):
            g = groupes.setdefault(k, {"cle": k, "contactes": 0, "ouverts": 0, "reponses": 0})
            g["contactes"] += 1
            g["ouverts"] += e["ouvert"]
            g["reponses"] += e["repondu"]
    lignes = list(groupes.values())
    for g in lignes:
        g["taux_ouverture"] = _pct(g["ouverts"], g["contactes"])
        g["taux_reponse"] = _pct(g["reponses"], g["contactes"])
    lignes.sort(key=(lambda g: ordre.index(g["cle"]) if ordre and g["cle"] in ordre else 0) if ordre else (lambda g: (-g["contactes"], str(g["cle"]))))
    return lignes


def indicateurs(conn, cfg, enregistrements):
    contactes = len(enregistrements)
    ouverts = sum(e["ouvert"] for e in enregistrements)
    reponses = sum(e["repondu"] for e in enregistrements)
    emails = sum(e["emails"] for e in enregistrements)
    rebonds = conn.execute("SELECT COUNT(*) FROM liste_noire WHERE motif = 'rebond'").fetchone()[0]
    desinscrits = conn.execute("SELECT COUNT(*) FROM liste_noire WHERE motif = 'desinscription'").fetchone()[0]
    return {"contactes": contactes, "emails": emails, "relances": emails - contactes, "ouverts": ouverts, "reponses": reponses,
            "taux_ouverture": _pct(ouverts, contactes), "taux_reponse": _pct(reponses, contactes),
            "rebonds": rebonds, "taux_rebond": _pct(rebonds, contactes), "desinscrits": desinscrits,
            "taux_desinscription": _pct(desinscrits, contactes)}


def tableaux(conn, cfg, enregistrements):
    libelles = {l["code"]: l["libelle"] for l in conn.execute("SELECT code, libelle FROM criteres WHERE campagne_id IS NULL")}
    bornes = json.loads(cfg.get("analyses_tranches_score") or "[]")
    ordre_tranches = []
    for e in sorted(enregistrements, key=lambda e: e["score"]):
        t = tranche_score(e["score"], bornes)
        if t not in ordre_tranches:
            ordre_tranches.append(t)
    return {
        "campagnes": agreger(enregistrements, lambda e: e["campagne"]),
        "angles": agreger(enregistrements, lambda e: e["angle"]),
        "problemes": [{**g, "libelle": libelles.get(g["cle"], g["cle"])} for g in agreger(
            enregistrements, lambda e: [p for p in e["problemes"] if p not in _DIAGNOSTICS] or ["(aucun problème)"])],
        "villes": agreger(enregistrements, lambda e: e["ville"]),
        "tranches": agreger(enregistrements, lambda e: tranche_score(e["score"], bornes), ordre_tranches),
        "sources": agreger(enregistrements, lambda e: e["source"]),
        "heures": agreger(enregistrements, lambda e: f"{e['heure']:02d} h", [f"{h:02d} h" for h in range(24)]),
        "jours": agreger(enregistrements, lambda e: e["jour"], JOURS),
    }


# --------------------------------------------------------------------------------------
# Décisions recommandées
# --------------------------------------------------------------------------------------

_ORDRE_NIVEAUX = {"attention": 0, "action": 1, "info": 2}


def _recommandation(niveau, cle, titre, detail, preuve="", lien=None, action=None):
    return {"niveau": niveau, "cle": cle, "titre": titre, "detail": detail, "preuve": preuve, "lien": lien, "action": action}


def recommandations(conn, cfg, enregistrements, kpis, tab, maintenant=None):
    maintenant = maintenant or datetime.now(timezone.utc)
    mini = int(cfg["analyses_echantillon_min"])
    mini_groupe = int(cfg["analyses_echantillon_groupe"])
    seuil_rebond = float(cfg["analyses_seuil_rebond_pct"])
    seuil_ouverture = float(cfg["analyses_seuil_ouverture_pct"])
    z_min = 1.64
    r = []
    n, taux_rep = kpis["contactes"], kpis["taux_reponse"]

    # --- Santé du domaine d'envoi ---
    from core import domaine
    verif = domaine.derniere_verification(conn)
    if verif is None:
        r.append(_recommandation("action", "domaine", "Vérifie la santé de ton domaine d'envoi",
                                 "SPF, DKIM et DMARC décident si tes emails arrivent en boîte de réception ou en spam. "
                                 "Le contrôle est gratuit et ne prend que quelques secondes.", lien="/reglages", action="Vérifier mon domaine"))
    elif verif["statut"] in ("erreur", "attention"):
        problemes = [c for c in verif["controles"] if c["statut"] in ("erreur", "attention")]
        r.append(_recommandation("attention", "domaine", "Ton domaine d'envoi a un point à corriger",
                                 " ".join(f"{c['titre']} : {c['conseil'] or c['detail']}" for c in problemes), lien="/reglages", action="Voir le détail"))

    # --- Volume insuffisant ---
    if n < mini:
        r.append(_recommandation("info", "echantillon", f"Pas encore assez de données ({n}/{mini} prospects contactés)",
                                 "Les statistiques ci-dessous sont indicatives. Les recommandations fines (quel angle, quelle ville, quel problème) "
                                 f"n'apparaîtront qu'à partir de {mini} contacts.", preuve=f"{n} contact(s) sur {mini} nécessaires"))

    # --- Réputation / délivrabilité ---
    if n >= 10 and kpis["taux_rebond"] is not None and kpis["taux_rebond"] > seuil_rebond:
        r.append(_recommandation("attention", "rebonds", f"Taux de rebond élevé : {kpis['taux_rebond']} %",
                                 "Trop d'adresses invalides abîment la réputation de ton domaine. Ralentis, et vérifie tes sources d'emails "
                                 "(adresses trouvées sur les sites, listes importées).", preuve=f"{kpis['rebonds']} rebond(s) pour {n} contact(s), seuil {seuil_rebond} %",
                                 lien="/envois", action="Voir les envois"))
    if kpis["desinscrits"]:
        r.append(_recommandation("info", "desinscriptions", f"{kpis['desinscrits']} désinscription(s) ou plainte(s)",
                                 "Elles sont déjà en liste noire. Si leur nombre grimpe, relis ton angle : un message trop insistant ou hors sujet fait réagir.",
                                 preuve=f"{kpis['taux_desinscription']} % des contacts"))
    if n >= mini and kpis["taux_ouverture"] is not None and kpis["taux_ouverture"] < seuil_ouverture:
        r.append(_recommandation("attention", "ouvertures", f"Peu d'ouvertures : {kpis['taux_ouverture']} %",
                                 "Soit l'objet n'attire pas, soit tes emails arrivent en spam. Vérifie ton domaine et teste un autre gabarit d'objet "
                                 "dans l'angle de la campagne. (Les ouvertures sont indicatives.)",
                                 preuve=f"{kpis['ouverts']} ouverture(s) sur {n} contact(s), seuil {seuil_ouverture} %", lien="/campagnes", action="Ouvrir les campagnes"))

    # --- Résultats : réponses ---
    if n >= mini and kpis["reponses"] == 0:
        r.append(_recommandation("action", "zero_reponse", "Aucune réponse pour l'instant",
                                 "Passé ce volume, zéro réponse indique en général un problème d'angle, de cible ou de délivrabilité. "
                                 "Relis 5 de tes messages envoyés comme le ferait le destinataire, et change UNE chose à la fois.",
                                 preuve=f"0 réponse sur {n} contacts", lien="/campagnes", action="Retravailler l'angle"))
    for g in tab["campagnes"]:
        if g["contactes"] >= mini and g["reponses"] == 0 and kpis["reponses"] > 0:
            r.append(_recommandation("action", "campagne_muette", f"« {g['cle']} » n'a obtenu aucune réponse",
                                     "Les autres campagnes répondent, pas celle-ci : change sa cible, ses requêtes ou son angle.",
                                     preuve=f"0 réponse sur {g['contactes']} contacts", lien="/campagnes", action="Ouvrir les campagnes"))

    # --- Qu'est-ce qui marche ? (comparaisons crédibles seulement) ---
    if n >= mini and taux_rep is not None:
        def meilleurs(groupes, etiquette, lien, action, formater=lambda g: g["cle"]):
            for g in groupes:
                if g["contactes"] < mini_groupe or g["reponses"] < 3:
                    continue
                reste_n, reste_x = n - g["contactes"], kpis["reponses"] - g["reponses"]
                z = z_deux_proportions(g["reponses"], g["contactes"], reste_x, reste_n)
                if z >= z_min and g["taux_reponse"] >= 1.5 * (_pct(reste_x, reste_n) or 0):
                    r.append(_recommandation("action", f"gagnant_{etiquette}_{g['cle']}", f"{etiquette.capitalize()} qui répond le mieux : {formater(g)}",
                                             f"Priorise-le : il répond nettement mieux que le reste ({g['taux_reponse']} % contre {_pct(reste_x, reste_n)} %).",
                                             preuve=f"{g['reponses']} réponse(s) sur {g['contactes']} contacts, écart crédible (z = {z:.1f})", lien=lien, action=action))
        meilleurs([g for g in tab["problemes"] if g["cle"] != "(aucun problème)"], "problème", "/prospects", "Voir les prospects", lambda g: g["libelle"])
        meilleurs(tab["villes"], "ville", "/prospects", "Voir les prospects")
        meilleurs(tab["angles"], "angle", "/campagnes", "Ouvrir les campagnes")
        meilleurs(tab["heures"], "heure d'envoi", "/envois", "Voir les envois")
        # Le score prédit-il les réponses ?
        tr = [g for g in tab["tranches"] if g["contactes"] >= mini_groupe]
        if len(tr) >= 2 and tr[-1]["taux_reponse"] is not None and tr[0]["taux_reponse"] is not None \
                and tr[-1]["taux_reponse"] <= tr[0]["taux_reponse"] and kpis["reponses"] >= 5:
            r.append(_recommandation("attention", "score", "Ton score ne prédit pas (encore) les réponses",
                                     "Les prospects au score le plus haut ne répondent pas mieux que ceux au score le plus bas. Les poids des critères "
                                     "ne reflètent peut-être pas ce qui compte pour cette cible : ajuste-les sur la fiche de campagne.",
                                     preuve=f"tranche « {tr[-1]['cle']} » : {tr[-1]['taux_reponse']} % ; tranche « {tr[0]['cle']} » : {tr[0]['taux_reponse']} %",
                                     lien="/campagnes", action="Ajuster les poids"))

    # --- Qualité des messages générés ---
    ligne = conn.execute("SELECT COUNT(*), COALESCE(SUM(edite_par_humain), 0) FROM messages WHERE approuve_le IS NOT NULL").fetchone()
    if ligne[0] >= 10 and ligne[1] / ligne[0] > 0.5:
        r.append(_recommandation("action", "edition", f"Tu corriges {round(100 * ligne[1] / ligne[0])} % des messages avant de les approuver",
                                 "Si tu refais souvent les mêmes corrections, mets-les dans le prompt de l'angle : tu gagneras du temps à chaque validation.",
                                 preuve=f"{ligne[1]} message(s) corrigé(s) sur {ligne[0]} approuvés", lien="/campagnes", action="Améliorer l'angle"))

    # --- Montée en volume ---
    premier = conn.execute("SELECT MIN(envoye_le) FROM envois WHERE statut <> 'echec'").fetchone()[0]
    if premier:
        semaine = (maintenant - datetime.strptime(premier, _FORMAT).replace(tzinfo=timezone.utc)).days // 7 + 1
        paliers = json.loads(cfg.get("montee_plafonds") or "[]")
        if paliers:
            conseille = paliers[min(semaine, len(paliers)) - 1]
            plafond = int(cfg["plafond_jour"])
            sain = (kpis["taux_rebond"] or 0) <= seuil_rebond
            if plafond < conseille and sain:
                r.append(_recommandation("action", "montee", f"Tu peux monter ton plafond à {conseille} par jour",
                                         f"Semaine {semaine} depuis ton premier envoi : la montée en volume progressive recommandée t'autorise {conseille} emails par jour, "
                                         f"et tes indicateurs de réputation sont sains (plafond actuel : {plafond}).",
                                         preuve=f"montée recommandée : {' → '.join(str(p) for p in paliers)}", lien="/reglages", action="Changer le plafond"))
            elif plafond > conseille:
                r.append(_recommandation("attention", "montee_trop_vite", f"Ton plafond ({plafond}) dépasse la montée recommandée ({conseille})",
                                         "Une adresse récente qui envoie trop vite détruit sa réputation en quelques jours.",
                                         preuve=f"semaine {semaine} : {conseille} par jour conseillés", lien="/reglages", action="Changer le plafond"))

    # --- Travail en attente ---
    compteurs = messages.compteurs(conn)
    if compteurs["en_attente"] >= 10:
        r.append(_recommandation("action", "a_valider", f"{compteurs['en_attente']} messages attendent ta validation",
                                 "Plus ils attendent, plus l'information sur les prospects vieillit.", lien="/messages", action="Valider"))
    relances = len(suivi.file_de_relance(conn, cfg))
    if relances:
        r.append(_recommandation("action", "relances", f"{relances} relance(s) à préparer",
                                 "Une relance bien placée fait souvent autant de réponses que le premier message.", lien="/envois", action="Préparer les relances"))
    appels = conn.execute("SELECT COUNT(*) FROM prospects WHERE email IS NULL AND telephone IS NOT NULL AND statut IN ('qualifie', 'nouveau') AND score >= ?",
                          (int(cfg["score_min_generation"]),)).fetchone()[0]
    if appels:
        r.append(_recommandation("action", "appels", f"{appels} prospect(s) à fort besoin sans email, mais avec un téléphone",
                                 "Ce sont souvent les meilleurs leads : un appel de deux minutes vaut mieux qu'un email jamais envoyé.",
                                 lien="/prospects?email=sans", action="Voir la liste d'appels"))
    inconnus = conn.execute("SELECT COUNT(*), SUM(problemes LIKE '%site_inconnu%') FROM prospects WHERE statut <> 'nouveau'").fetchone()
    if inconnus[0] >= 20 and (inconnus[1] or 0) / inconnus[0] > 0.5:
        r.append(_recommandation("info", "sources", f"{round(100 * inconnus[1] / inconnus[0])} % de tes prospects n'ont pas de site connu",
                                 "Ta source de données ne renseigne pas leur site : tu ne peux pas savoir s'ils en ont un. Une clé Google Places, ou des listes "
                                 "importées avec les sites, rendrait le score beaucoup plus fiable.", lien="/reglages", action="Voir les réglages"))

    r.sort(key=lambda x: _ORDRE_NIVEAUX[x["niveau"]])
    return r


def donnees_page(conn, cfg, jours=None, maintenant=None):
    enr = collecter(conn, cfg, jours, maintenant)
    kpis = indicateurs(conn, cfg, enr)
    tab = tableaux(conn, cfg, enr)
    return {"jours": jours, "kpis": kpis, "tableaux": tab,
            "recommandations": recommandations(conn, cfg, enr, kpis, tab, maintenant),
            "echantillon_min": int(cfg["analyses_echantillon_min"])}


# --------------------------------------------------------------------------------------
# Auto-tests
# --------------------------------------------------------------------------------------


def _autotests():
    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    verifier("test z : groupes identiques -> 0 ; écart net sur grands effectifs -> |z| > 1,64 ; effectifs nuls -> 0",
             z_deux_proportions(10, 100, 10, 100) == 0 and z_deux_proportions(30, 100, 10, 100) > 1.64 and z_deux_proportions(0, 0, 5, 10) == 0)
    verifier("test z : petit échantillon (3/5 contre 1/5) : écart NON crédible, on ne conclut pas", z_deux_proportions(3, 5, 1, 5) < 1.64)
    verifier("tranches de score : bornes [5, 8, 11] -> « moins de 5 », « 5 à 7 », « 8 à 10 », « 11 et plus »",
             [tranche_score(s, [5, 8, 11]) for s in (0, 4, 5, 7, 8, 10, 11, 30)]
             == ["moins de 5", "moins de 5", "5 à 7", "5 à 7", "8 à 10", "8 à 10", "11 et plus", "11 et plus"])

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        chemin = Path(tmp) / "t.db"
        db.init_db(chemin, importer_env=False)
        maintenant = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        with db.connexion(chemin) as c:
            cfg = reglages.valeurs_effectives(c)
            cid = campagnes.creer(c, {"nom": "Dentistes", "secteur": "s", "pays": "FR", "langue": "fr"})
            cid2 = campagnes.creer(c, {"nom": "Avocats", "secteur": "s", "pays": "FR", "langue": "fr"})
            vide = donnees_page(c, cfg, None, maintenant)
            verifier("base sans envoi : aucun chiffre inventé (taux absents), recommandation « vérifie ton domaine » et « pas assez de données »",
                     vide["kpis"]["contactes"] == 0 and vide["kpis"]["taux_reponse"] is None
                     and {"domaine", "echantillon"} <= {x["cle"] for x in vide["recommandations"]})
            # 60 prospects contactés : « pas_rdv » répond bien (12/20), le reste mal (3/40) ; campagne Avocats muette
            idx = 0

            def contacter(campagne, ville, problemes, score, repondu, ouvert, heure=10):
                nonlocal idx
                idx += 1
                db.inserer_prospect(c, {"campagne_id": campagne, "nom": f"P{idx}", "email": f"p{idx}@x.fr", "ville": ville, "source": "openstreetmap", "source_id": f"o:{idx}"})
                pid = c.execute("SELECT id FROM prospects WHERE source_id = ?", (f"o:{idx}",)).fetchone()[0]
                c.execute("UPDATE prospects SET problemes = ?, score = ?, statut = ? WHERE id = ?",
                          (json.dumps(problemes), score, "repondu" if repondu else "envoye", pid))
                mid = c.execute("INSERT INTO messages (prospect_id, objet, corps, version, edite_par_humain) "
                                "VALUES (?, 'o', 'c', 1, 1)", (pid,)).lastrowid
                c.execute("UPDATE messages SET approuve_le = '2026-10-01T00:00:00Z', approuve_par_humain = 1 WHERE id = ?", (mid,))
                c.execute("INSERT INTO envois (message_id, statut, envoye_le) VALUES (?, ?, ?)",
                          (mid, "repondu" if repondu else ("ouvert" if ouvert else "envoye"), f"2026-10-0{1 + idx % 7}T{heure:02d}:00:00Z"))
            for k in range(20):
                contacter(cid, "Lyon", ["pas_rdv"], 8, k < 12, k < 17)
            for k in range(20):
                contacter(cid, "Lyon", ["lent"], 3, k < 1, k < 5)
            for k in range(20):
                contacter(cid2, "Nice", ["pas_h1"], 2, k < 2, k < 4)
            for k in range(5):
                db.ajouter_liste_noire(c, f"b{k}@x.fr", "rebond")
            c.execute("UPDATE campagnes SET modele_message_id = (SELECT MIN(id) FROM modeles_message) WHERE id = ?", (cid,))
            d = donnees_page(c, cfg, None, maintenant)
            cles = {x["cle"] for x in d["recommandations"]}
            k = d["kpis"]
            verifier("indicateurs : 60 contactés, 15 réponses (25 %), taux par PROSPECT, 5 rebonds comptés",
                     k["contactes"] == 60 and k["reponses"] == 15 and k["taux_reponse"] == 25.0 and k["rebonds"] == 5)
            verifier("décision : le problème « pas_rdv » (12/20 contre 3/40) est recommandé, avec les chiffres et un écart crédible",
                     "gagnant_problème_pas_rdv" in cles
                     and "12 réponse(s) sur 20" in next(x for x in d["recommandations"] if x["cle"] == "gagnant_problème_pas_rdv")["preuve"])
            verifier("décision : la ville Lyon (13/40) n'est PAS présentée comme gagnante seule sans écart crédible avec Nice",
                     all(not x["cle"].startswith("gagnant_ville_Nice") for x in d["recommandations"]))
            verifier("décision : taux de rebond 8,3 % > seuil 3 % -> alerte « attention », classée en premier",
                     d["recommandations"][0]["niveau"] == "attention" and "rebonds" in cles)
            verifier("décision : la campagne « Avocats » (2/20) n'est pas déclarée muette (elle a eu 2 réponses)", "campagne_muette" not in cles)
            tr = {g["cle"]: g for g in d["tableaux"]["tranches"]}
            verifier("tableaux : tranches de score ordonnées, taux de réponse par tranche (score 8 -> 60 %)",
                     [g["cle"] for g in d["tableaux"]["tranches"]] == ["moins de 5", "8 à 10"] and tr["8 à 10"]["taux_reponse"] == 60.0)
            verifier("tableaux : par heure d'envoi (fuseau Paris, 10 h UTC = 12 h), par jour, par campagne et par angle",
                     d["tableaux"]["heures"][0]["cle"] == "12 h" and len(d["tableaux"]["jours"]) >= 5
                     and {g["cle"] for g in d["tableaux"]["campagnes"]} == {"Dentistes", "Avocats"} and len(d["tableaux"]["angles"]) == 2)
            verifier("échantillon suffisant (60 ≥ 20) : plus de message « pas assez de données »", "echantillon" not in cles)

            # Période : seulement les envois récents
            recent = donnees_page(c, cfg, 7, maintenant)
            verifier("filtre de période : 7 jours retient uniquement les contacts dont le 1er envoi est dans la fenêtre",
                     recent["kpis"]["contactes"] <= 60)

            # Aucune réponse du tout après assez de contacts
            c.execute("UPDATE prospects SET statut = 'envoye'")
            c.execute("UPDATE envois SET statut = 'envoye'")
            muet = donnees_page(c, cfg, None, maintenant)
            verifier("60 contacts, zéro réponse : décision « change UNE chose à la fois » avec la preuve chiffrée",
                     "zero_reponse" in {x["cle"] for x in muet["recommandations"]})
            verifier("zéro réponse : aucune comparaison de groupes n'est tentée (pas de faux gagnant)",
                     not any(x["cle"].startswith("gagnant_") for x in muet["recommandations"]))

            # Montée en volume
            c.execute("UPDATE envois SET envoye_le = '2026-10-01T09:00:00Z'")
            db.set_reglage(c, "plafond_jour", "20")
            db.set_reglage(c, "analyses_seuil_rebond_pct", "50")
            m = donnees_page(c, reglages.valeurs_effectives(c), None, maintenant)
            verifier("montée : 3e semaine -> 50 par jour autorisés, plafond 20, indicateurs sains -> conseil de monter",
                     "montee" in {x["cle"] for x in m["recommandations"]})
            db.set_reglage(c, "plafond_jour", "100")
            m = donnees_page(c, reglages.valeurs_effectives(c), None, maintenant)
            verifier("montée : plafond au-dessus de la montée recommandée -> alerte « trop vite »",
                     "montee_trop_vite" in {x["cle"] for x in m["recommandations"]})
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
        d = donnees_page(conn, reglages.valeurs_effectives(conn))
    print(json.dumps({"kpis": d["kpis"], "recommandations": [(r["niveau"], r["titre"]) for r in d["recommandations"]]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
