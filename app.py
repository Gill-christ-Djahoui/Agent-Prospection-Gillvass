"""Point d'entrée Flask : routes et démarrage. Lancement : python app.py"""
import json
import os
import socket
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, jsonify, redirect, render_template, request, session,
                   url_for)

import config
from core import accueil as accueil_mod
from core import analyses as analyses_mod
from core import analyste, domaine, sauvegarde
from core import analyzer, auth, importer, scorer
from core import messages as messages_mod
from core import sender as sender_mod
from core import suivi
from core import niche
from core import boite
from core import appels as appels_mod
from core import formulaires as formulaires_mod
from core import social as social_mod
from core import campagnes as campagnes_mod
from core import db, jobs, sender, sourcing, writer
from core import reglages as reglages_mod

HOTES_AUTORISES = {"localhost", "127.0.0.1"}
LIMITE_PROSPECTS = 5000  # lignes envoyées au tableau des prospects

# Rail de navigation : (nom de la route, libellé affiché)
NAVIGATION = [
    ("accueil", "Accueil", "accueil"),
    ("campagnes", "Campagnes", "campagnes"),
    ("prospects", "Prospects", "prospects"),
    ("formulaires", "Formulaires", "formulaires"),
    ("messages_prives", "Messages privés", "social"),
    ("appels", "Appels", "appels"),
    ("messages", "Messages", "messages"),
    ("envois", "Envois", "envois"),
    ("analyses", "Analyses", "analyses"),
    ("reglages", "Réglages", "reglages"),
]
TITRES_COMPTEURS = {"messages": "Messages en attente de validation", "envois": "Messages approuvés prêts à partir",
                    "appels": "Rappels à faire aujourd'hui"}


def _lire_liste_noire(conn):
    return [dict(l) for l in conn.execute(
        "SELECT valeur, motif, ajoute_le FROM liste_noire ORDER BY id DESC")]


def _etat_secrets(etat):
    """Pour la réponse JSON : uniquement l'état masqué des clés (jamais leur valeur)."""
    return {cle: info for cle, info in etat.items() if info["secret"]}


def create_app():
    config.configurer_logging()
    db.init_db()  # crée la base au premier lancement, sans effet ensuite
    with db.connexion() as conn:
        jobs.marquer_interrompus(conn)  # tâches restées « en cours » d'un lancement précédent

    app = Flask(__name__)
    with db.connexion() as conn:
        app.config["ACCES_INITIAL"] = auth.initialiser_si_absent(conn)  # affiché une seule fois dans la console
        app.secret_key = auth.cle_de_session(conn)
        heures = int(reglages_mod.valeurs_effectives(conn)["session_heures"])
    app.config.update(SESSION_COOKIE_NAME="prospection_session", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Strict", PERMANENT_SESSION_LIFETIME=timedelta(hours=heures))

    @app.before_request
    def refuser_hotes_etrangers():
        # Protège contre le « DNS rebinding » : seul localhost peut parler à l'application.
        if request.host.rsplit(":", 1)[0] not in HOTES_AUTORISES:
            abort(400)

    @app.before_request
    def exiger_la_connexion():
        """Tout l'application (pages ET API) est derrière l'identifiant et le mot de passe."""
        if request.endpoint in ("connexion", "static"):
            return None  # (une adresse inconnue passe aussi par la connexion : aucune page ne s'affiche sans accès)
        with db.connexion() as conn:
            valide = session.get("auth") is True and session.get("empreinte") == auth.empreinte_session(conn)
        if valide:
            return None
        if request.path.startswith("/api/"):
            return jsonify(erreur="Session expirée : reconnecte-toi."), 401
        suite = request.full_path.rstrip("?") if request.path != "/" else ""
        return redirect(url_for("connexion", suite=suite) if suite else url_for("connexion"))

    @app.context_processor
    def injecter_navigation():
        # La fiche d'une campagne reste rattachée à l'entrée « Campagnes » du rail.
        section = "campagnes" if request.endpoint == "campagne" else request.endpoint
        with db.connexion() as conn:
            compteurs = messages_mod.compteurs(conn)
            cfg_rail = reglages_mod.valeurs_effectives(conn)
            quota_du_jour = sender_mod.quota(conn, cfg_rail)
            rappels = appels_mod.compteurs_rail(conn, cfg_rail)
        navigation = [n for n in NAVIGATION if n[0] != "appels" or cfg_rail.get("canal_appels") == "oui"]  # pas de numéro : pas d'Appels
        return {"navigation": navigation, "section_active": section, "quota_rail": quota_du_jour,
                "identifiant_connecte": session.get("identifiant", ""),
                "plafond_score": scorer.PLAFOND_AFFICHAGE,
                "mode_essai": bool(json.loads(cfg_rail.get("envoi_adresses_autorisees") or "[]")),
                "rail": {"messages": compteurs["en_attente"], "envois": compteurs["approuves"], "appels": rappels},
                "rail_titres": TITRES_COMPTEURS}

    @app.errorhandler(messages_mod.ErreurMessage)
    def erreur_de_message(erreur):
        return jsonify(erreur=str(erreur)), 400

    @app.errorhandler(niche.ErreurNiche)
    def erreur_de_niche(erreur):
        return jsonify(erreur=str(erreur)), 400

    @app.errorhandler(appels_mod.ErreurAppel)
    def erreur_d_appel(erreur):
        return jsonify(erreur=str(erreur)), 400

    @app.errorhandler(formulaires_mod.ErreurFormulaire)
    def erreur_de_formulaire(erreur):
        return jsonify(erreur=str(erreur)), 400

    @app.errorhandler(campagnes_mod.ErreurEnvoisExistants)
    def erreur_envois_existants(erreur):
        return jsonify(erreur=" ".join(erreur.erreurs.values()), erreurs=erreur.erreurs,
                       confirmation_requise=True, envois=erreur.nb_envois), 409

    @app.errorhandler(reglages_mod.ErreurValidation)
    def erreur_de_validation(erreur):
        return jsonify(erreur=" ".join(erreur.erreurs.values()),
                       erreurs=erreur.erreurs), 400

    @app.get("/connexion")
    @app.post("/connexion")
    def connexion():
        suite = request.values.get("suite", "")
        suite = suite if suite.startswith("/") and not suite.startswith("//") and "\\" not in suite else ""
        erreur = None
        if request.method == "POST":
            attente = auth.secondes_de_blocage()
            if attente:
                erreur = f"Trop d'essais échoués. Réessaie dans {max(1, round(attente / 60))} min."
                return render_template("connexion.html", erreur=erreur, suite=suite), 429
            with db.connexion() as conn:
                ok = auth.verifier_acces(conn, request.form.get("identifiant"), request.form.get("mot_de_passe"))
                if ok:
                    auth.effacer_echecs()
                    session.clear()
                    session.update(auth=True, empreinte=auth.empreinte_session(conn), identifiant=auth.identifiant(conn))
                    session.permanent = True
                    return redirect(suite or url_for("accueil"))
            auth.noter_echec()
            erreur = "Identifiant ou mot de passe incorrect."
            return render_template("connexion.html", erreur=erreur, suite=suite), 401
        return render_template("connexion.html", erreur=None, suite=suite)

    @app.post("/deconnexion")
    def deconnexion():
        session.clear()
        return redirect(url_for("connexion"))

    @app.post("/api/acces")
    def api_acces():
        """Change l'identifiant et/ou le mot de passe (le mot de passe actuel est exigé)."""
        d = request.get_json(silent=True) or {}
        attente = auth.secondes_de_blocage()
        if attente:
            return jsonify(erreur=f"Trop d'essais échoués. Réessaie dans {max(1, round(attente / 60))} min."), 429
        with db.connexion() as conn:
            if not auth.verifier_acces(conn, auth.identifiant(conn), d.get("actuel")):
                auth.noter_echec()
                return jsonify(erreur="Le mot de passe actuel est incorrect.", erreurs={"actuel": "Mot de passe incorrect."}), 400
            nouveau = d.get("nouveau") or ""
            if nouveau != (d.get("confirmation") or ""):
                return jsonify(erreur="La confirmation ne correspond pas.", erreurs={"confirmation": "Différent du nouveau mot de passe."}), 400
            try:
                auth.definir_acces(conn, d.get("identifiant") or auth.identifiant(conn), nouveau or d.get("actuel"))
            except auth.ErreurAcces as erreur:
                return jsonify(erreur=str(erreur)), 400
            session.update(empreinte=auth.empreinte_session(conn), identifiant=auth.identifiant(conn))
        auth.effacer_echecs()
        return jsonify(ok=True)

    @app.get("/")
    def accueil():
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            donnees = accueil_mod.donnees(conn, cfg)
        return render_template("accueil.html", **donnees, delai_relance=cfg["delai_relance_jours"])

    # --- Campagnes ----------------------------------------------------------------------

    @app.get("/campagnes")
    def campagnes():
        with db.connexion() as conn:
            return render_template("campagnes.html",
                                   campagnes=campagnes_mod.lister(conn),
                                   modeles=campagnes_mod.lister(conn, modeles=True))

    @app.get("/campagnes/<int:campagne_id>")
    def campagne(campagne_id):
        with db.connexion() as conn:
            fiche = campagnes_mod.lire(conn, campagne_id)
            dernier_scan = jobs.dernier(conn, campagne_id)
        if fiche is None:
            abort(404)
        scan_actif = bool(dernier_scan and dernier_scan["statut"] in jobs.STATUTS_ACTIFS)
        return render_template("campagne.html", **fiche, variables=writer.VARIABLES_PROMPT,
                               job=dernier_scan, scan_actif=scan_actif)

    def _exiger_campagne(conn, campagne_id):
        if conn.execute("SELECT 1 FROM campagnes WHERE id = ?", (campagne_id,)).fetchone() is None:
            abort(404)

    @app.post("/api/campagnes")
    def api_campagne_creer():
        donnees = request.get_json(silent=True) or {}
        with db.connexion() as conn:
            nouvelle = campagnes_mod.creer(conn, donnees, donnees.get("modele_depart") or None)
        return jsonify(id=nouvelle, url=url_for("campagne", campagne_id=nouvelle))

    @app.post("/api/niche/proposer")
    def api_niche_proposer():
        """Assistant de niche, étape 1 : l'IA propose requêtes, angle, poids des critères et zones. Rien n'est écrit."""
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            criteres = niche.criteres_globaux(conn)
        return jsonify(niche.proposer(cfg, request.get_json(silent=True) or {}, criteres))

    @app.post("/api/niche/creer")
    def api_niche_creer():
        """Assistant de niche, étape 2 : crée la campagne (brouillon) à partir de la proposition relue."""
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
        resultat = niche.creer(cfg, request.get_json(silent=True) or {})
        return jsonify(**resultat, url=url_for("campagne", campagne_id=resultat["campagne_id"]))

    @app.post("/api/campagnes/<int:campagne_id>")
    def api_campagne_modifier(campagne_id):
        with db.connexion() as conn:
            _exiger_campagne(conn, campagne_id)
            campagnes_mod.modifier(conn, campagne_id, request.get_json(silent=True) or {})
        return jsonify(ok=True)

    @app.post("/api/campagnes/<int:campagne_id>/dupliquer")
    def api_campagne_dupliquer(campagne_id):
        comme_modele = bool((request.get_json(silent=True) or {}).get("comme_modele"))
        with db.connexion() as conn:
            _exiger_campagne(conn, campagne_id)
            nouvelle = campagnes_mod.dupliquer(conn, campagne_id, est_modele=comme_modele)
        return jsonify(id=nouvelle, url=url_for("campagne", campagne_id=nouvelle))

    @app.post("/api/campagnes/<int:campagne_id>/archiver")
    def api_campagne_archiver(campagne_id):
        archiver = (request.get_json(silent=True) or {}).get("archiver", True)
        with db.connexion() as conn:
            _exiger_campagne(conn, campagne_id)
            campagnes_mod.archiver(conn, campagne_id, bool(archiver))
        return jsonify(ok=True)

    @app.delete("/api/campagnes/<int:campagne_id>")
    def api_campagne_supprimer(campagne_id):
        """Supprime une campagne ou un modèle. Avec des envois déjà faits : 409 tant que `forcer` n'est pas confirmé."""
        forcer = bool((request.get_json(silent=True) or {}).get("forcer"))
        with db.connexion() as conn:
            _exiger_campagne(conn, campagne_id)
            supprime = campagnes_mod.supprimer(conn, campagne_id, forcer)
        return jsonify(ok=True, url=url_for("campagnes"), supprime=supprime)

    @app.post("/api/campagnes/<int:campagne_id>/zones")
    def api_zone_ajouter(campagne_id):
        donnees = request.get_json(silent=True) or {}
        with db.connexion() as conn:
            _exiger_campagne(conn, campagne_id)
            cfg = reglages_mod.valeurs_effectives(conn)
            resultat = campagnes_mod.ajouter_zone(
                conn, campagne_id, donnees.get("ville"), donnees.get("pays"), donnees.get("rayon_km"),
                geocoder=lambda ville, pays: sourcing.geocoder(cfg, ville, pays))
        return jsonify(resultat)

    @app.post("/api/zones/<int:zone_id>/geocoder")
    def api_zone_geocoder(zone_id):
        """Relance le géocodage d'une zone, ou enregistre des coordonnées saisies à la main."""
        donnees = request.get_json(silent=True) or {}
        with db.connexion() as conn:
            zone = conn.execute("SELECT ville, pays FROM zones WHERE id = ?", (zone_id,)).fetchone()
            if zone is None:
                abort(404)
            if donnees.get("lat") not in (None, "") or donnees.get("lon") not in (None, ""):
                campagnes_mod.definir_coordonnees(conn, zone_id, donnees.get("lat"), donnees.get("lon"))
                return jsonify(statut="ok", message="Coordonnées enregistrées.")
            cfg = reglages_mod.valeurs_effectives(conn)
            resultat = sourcing.geocoder(cfg, zone["ville"], zone["pays"])
            if resultat["statut"] == "ok":
                campagnes_mod.definir_coordonnees(conn, zone_id, resultat["lat"], resultat["lon"])
        return jsonify(resultat)

    @app.delete("/api/zones/<int:zone_id>")
    def api_zone_supprimer(zone_id):
        with db.connexion() as conn:
            campagnes_mod.supprimer_zone(conn, zone_id)
        return jsonify(ok=True)

    @app.post("/api/campagnes/<int:campagne_id>/scan")
    def api_scan_lancer(campagne_id):
        """Lance le scan de toutes les zones (ou de celles indiquées) en tâche de fond."""
        zone_ids = (request.get_json(silent=True) or {}).get("zone_ids") or None
        with db.connexion() as conn:
            fiche = campagnes_mod.lire(conn, campagne_id)
            if fiche is None:
                abort(404)
            if fiche["campagne"]["statut"] == "archivee":
                return jsonify(erreur="Cette campagne est archivée : désarchive-la avant de scanner."), 400
            if not fiche["campagne"]["requetes"]:
                return jsonify(erreur="Cette campagne n'a aucune requête de recherche : ajoutes-en une."), 400
            cibles = [z for z in fiche["zones"] if not zone_ids or z["id"] in zone_ids]
            if not cibles:
                return jsonify(erreur="Aucune zone à scanner."), 400
            if all(z["lat"] is None for z in cibles):
                return jsonify(erreur="Les zones n'ont pas de coordonnées : corrige-les d'abord."), 400
            code, explication = sourcing.fournisseur_effectif(reglages_mod.valeurs_effectives(conn))
            if code is None:
                return jsonify(erreur=explication), 400
            if jobs.actif(conn, campagne_id):
                return jsonify(erreur="Une tâche (scan ou analyse) est déjà en cours pour cette campagne."), 409
        job_id = jobs.soumettre("scan", campagne_id, sourcing.scanner, campagne_id, zone_ids)
        return jsonify(job_id=job_id)

    @app.post("/api/campagnes/<int:campagne_id>/analyser")
    def api_analyser(campagne_id):
        """Ré-analyse les sites des prospects (nouveaux seulement, ou tous les qualifiés avec tous=true)."""
        tous = bool((request.get_json(silent=True) or {}).get("tous"))
        with db.connexion() as conn:
            _exiger_campagne(conn, campagne_id)
            if jobs.actif(conn, campagne_id):
                return jsonify(erreur="Une tâche (scan ou analyse) est déjà en cours pour cette campagne."), 409
        job_id = jobs.soumettre("analyse", campagne_id, analyzer.analyser_campagne, campagne_id, not tous)
        return jsonify(job_id=job_id)

    @app.post("/api/campagnes/<int:campagne_id>/recalculer")
    def api_recalculer(campagne_id):
        """Recalcule les scores avec les poids actuels, sans retélécharger aucun site."""
        with db.connexion() as conn:
            _exiger_campagne(conn, campagne_id)
            return jsonify(scorer.recalculer(conn, campagne_id))

    @app.post("/api/prospects/import")
    def api_import_csv():
        fichier = request.files.get("fichier")
        if fichier is None or not fichier.filename:
            return jsonify(erreur="Choisis un fichier CSV."), 400
        try:
            campagne_id = int(request.form.get("campagne_id", ""))
        except ValueError:
            return jsonify(erreur="Choisis la campagne qui recevra les prospects."), 400
        octets = fichier.read(importer.TAILLE_MAX_OCTETS + 1)
        try:
            with db.connexion() as conn:
                rapport = importer.importer_csv(conn, octets, campagne_id)
        except ValueError as erreur:
            return jsonify(erreur=str(erreur)), 400
        if rapport["importes"]:  # le circuit normal : analyse et score des nouveaux prospects
            with db.connexion() as conn:
                libre = jobs.actif(conn, campagne_id) is None
            if libre:
                rapport["job_id"] = jobs.soumettre("analyse", campagne_id, analyzer.analyser_campagne, campagne_id, True)
            else:
                rapport["avertissements"].append(
                    "Une tâche est en cours sur cette campagne : lance « Analyser les sites » ensuite.")
        rapport.pop("nouveaux_ids", None)
        return jsonify(rapport)

    @app.post("/api/messages/generer")
    def api_generer():
        """Génère les messages de prospects sélectionnés, en tâche de fond. forcer=true ignore le seuil de score."""
        donnees = request.get_json(silent=True) or {}
        ids = [i for i in (donnees.get("prospect_ids") or []) if isinstance(i, int)][:500]
        if not ids:
            return jsonify(erreur="Sélectionne au moins un prospect."), 400
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            manquants = writer.configuration_manquante(cfg)
            if manquants:
                return jsonify(erreur="Fournisseur d'IA non configuré : il manque " + ", ".join(manquants)
                               + ". Renseigne-le dans les Réglages."), 400
            marques = ",".join("?" * len(ids))
            campagnes_ids = {l[0] for l in conn.execute(
                f"SELECT DISTINCT campagne_id FROM prospects WHERE id IN ({marques})", ids)}
            if len(campagnes_ids) == 1 and jobs.actif(conn, next(iter(campagnes_ids))):
                return jsonify(erreur="Une tâche est déjà en cours pour cette campagne."), 409
        job_id = jobs.soumettre("redaction", next(iter(campagnes_ids)) if len(campagnes_ids) == 1 else None,
                                writer.rediger_lot, ids, bool(donnees.get("forcer")))
        return jsonify(job_id=job_id)

    @app.post("/api/prospects/statut")
    def api_prospects_statut():
        donnees = request.get_json(silent=True) or {}
        ids = [i for i in (donnees.get("prospect_ids") or []) if isinstance(i, int)]
        try:
            with db.connexion() as conn:
                return jsonify(db.changer_statut(conn, ids, donnees.get("statut")))
        except ValueError as erreur:
            return jsonify(erreur=str(erreur)), 400

    @app.get("/api/prospects/<int:prospect_id>/messages")
    def api_prospect_messages(prospect_id):
        with db.connexion() as conn:
            lignes = [dict(l) for l in conn.execute(
                "SELECT id, version, objet, corps, genere_le, edite_par_humain, approuve_le "
                "FROM messages WHERE prospect_id = ? ORDER BY version", (prospect_id,))]
        return jsonify(lignes)

    @app.get("/api/jobs/<int:job_id>")
    def api_job(job_id):
        with db.connexion() as conn:
            job = jobs.lire(conn, job_id)
        if job is None:
            abort(404)
        return jsonify(job)

    @app.post("/api/campagnes/<int:campagne_id>/criteres")
    def api_critere_definir(campagne_id):
        donnees = request.get_json(silent=True) or {}
        with db.connexion() as conn:
            _exiger_campagne(conn, campagne_id)
            campagnes_mod.definir_critere(conn, campagne_id, donnees.get("code"),
                                          donnees.get("points"), donnees.get("actif"))
        return jsonify(ok=True)

    @app.post("/api/modeles/<int:modele_id>")
    def api_modele_modifier(modele_id):
        with db.connexion() as conn:
            if conn.execute("SELECT 1 FROM modeles_message WHERE id = ?", (modele_id,)).fetchone() is None:
                abort(404)
            campagnes_mod.modifier_modele(conn, modele_id, request.get_json(silent=True) or {})
        return jsonify(ok=True)

    @app.post("/api/campagnes/<int:campagne_id>/angle")
    def api_angle_nouveau(campagne_id):
        with db.connexion() as conn:
            _exiger_campagne(conn, campagne_id)
            campagnes_mod.nouvel_angle(conn, campagne_id)
        return jsonify(ok=True)

    @app.get("/prospects")
    def prospects():
        with db.connexion() as conn:
            lignes = [dict(l) for l in conn.execute(
                """SELECT p.id, p.nom, p.ville, p.telephone, p.site, p.site_domaine, p.email, p.formulaire_url, p.profil_social_url, p.problemes, p.notes,
                          p.score, p.statut, p.source, p.campagne_id, c.nom AS campagne_nom,
                          est_generique(p.email) AS generique,
                          (SELECT MAX(e.envoye_le) FROM envois e JOIN messages m ON m.id = e.message_id
                            WHERE m.prospect_id = p.id AND e.statut <> 'echec') AS date_envoi
                     FROM prospects p JOIN campagnes c ON c.id = p.campagne_id
                    ORDER BY p.score DESC, p.id DESC LIMIT ?""", (LIMITE_PROSPECTS,))]
            total = conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
            job_redaction = jobs.dernier_du_type(conn, "redaction")
            seuil = reglages_mod.valeurs_effectives(conn)["score_min_generation"]
            filtres_campagnes = [dict(l) for l in conn.execute(
                "SELECT id, nom FROM campagnes WHERE id IN (SELECT campagne_id FROM prospects) ORDER BY nom")]
            libelles = {(l["campagne_id"], l["code"]): l["libelle"] for l in conn.execute(
                "SELECT campagne_id, code, libelle FROM criteres")}
            campagnes_import = [dict(l) for l in conn.execute(
                "SELECT id, nom FROM campagnes WHERE est_modele = 0 AND statut <> 'archivee' ORDER BY nom")]
        for l in lignes:
            l["problemes"] = json.loads(l["problemes"])
            l["problemes_libelles"] = [libelles.get((l["campagne_id"], code)) or libelles.get((None, code)) or code
                                       for code in l["problemes"]]
        villes = sorted({l["ville"] for l in lignes if l["ville"]}, key=str.lower)
        return render_template("prospects.html", prospects=lignes, total=total, campagnes=filtres_campagnes,
                               villes=villes, limite=LIMITE_PROSPECTS, plafond=scorer.PLAFOND_AFFICHAGE,
                               campagnes_import=campagnes_import, job=job_redaction,
                               job_actif=bool(job_redaction and job_redaction["statut"] in jobs.STATUTS_ACTIFS),
                               seuil=int(seuil), generables=writer.STATUTS_GENERABLES)

    # --- Formulaires de contact : liste, message prêt à coller, résultat (rien n'est envoyé par l'application) ---

    @app.get("/formulaires")
    def formulaires():
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            liste = [dict(l) for l in conn.execute(
                "SELECT id, nom FROM campagnes WHERE est_modele = 0 AND statut <> 'archivee' ORDER BY nom")]
        return render_template("formulaires.html", campagnes=liste, etats=formulaires_mod.ETATS,
                               resultats=formulaires_mod.RESULTATS, ia_prete=not writer.configuration_manquante(cfg))

    @app.get("/api/formulaires/file")
    def api_formulaires_file():
        with db.connexion() as conn:
            return jsonify(formulaires_mod.file(conn, request.args.get("campagne") or None, request.args.get("etat", "faire")))

    @app.get("/api/formulaires/<int:prospect_id>")
    def api_formulaire_fiche(prospect_id):
        with db.connexion() as conn:
            return jsonify(formulaires_mod.fiche(conn, prospect_id))

    @app.post("/api/formulaires/<int:prospect_id>/message")
    def api_formulaire_message(prospect_id):
        """Écrit (ou réécrit) le message à coller dans le formulaire, avec l'IA, et l'enregistre."""
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
        return jsonify(message=formulaires_mod.generer_message(cfg, prospect_id))

    @app.post("/api/formulaires/<int:prospect_id>/message/enregistrer")
    def api_formulaire_message_enregistrer(prospect_id):
        with db.connexion() as conn:
            formulaires_mod.enregistrer_message(conn, prospect_id, (request.get_json(silent=True) or {}).get("message"))
        return jsonify(ok=True)

    @app.post("/api/formulaires/<int:prospect_id>/resultat")
    def api_formulaire_resultat(prospect_id):
        d = request.get_json(silent=True) or {}
        with db.connexion() as conn:
            return jsonify(formulaires_mod.noter(conn, prospect_id, d.get("resultat"), d.get("note")))

    # --- Messages privés (Facebook, Instagram…) : liste, lien du profil, message prêt à coller, résultat ---

    @app.get("/messages-prives")
    def messages_prives():
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            liste = [dict(l) for l in conn.execute(
                "SELECT id, nom FROM campagnes WHERE est_modele = 0 AND statut <> 'archivee' ORDER BY nom")]
        return render_template("social.html", campagnes=liste, etats=social_mod.ETATS,
                               resultats=social_mod.RESULTATS, ia_prete=not writer.configuration_manquante(cfg))

    @app.get("/api/social/file")
    def api_social_file():
        with db.connexion() as conn:
            return jsonify(social_mod.file(conn, request.args.get("campagne") or None, request.args.get("etat", "faire")))

    @app.get("/api/social/<int:prospect_id>")
    def api_social_fiche(prospect_id):
        with db.connexion() as conn:
            return jsonify(social_mod.fiche(conn, prospect_id))

    @app.post("/api/social/<int:prospect_id>/profil")
    def api_social_profil(prospect_id):
        with db.connexion() as conn:
            return jsonify(social_mod.enregistrer_profil(conn, prospect_id, (request.get_json(silent=True) or {}).get("profil")))

    @app.post("/api/social/<int:prospect_id>/message")
    def api_social_message(prospect_id):
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
        return jsonify(message=social_mod.generer_message(cfg, prospect_id))

    @app.post("/api/social/<int:prospect_id>/message/enregistrer")
    def api_social_message_enregistrer(prospect_id):
        with db.connexion() as conn:
            social_mod.enregistrer_message(conn, prospect_id, (request.get_json(silent=True) or {}).get("message"))
        return jsonify(ok=True)

    @app.post("/api/social/<int:prospect_id>/resultat")
    def api_social_resultat(prospect_id):
        d = request.get_json(silent=True) or {}
        with db.connexion() as conn:
            return jsonify(social_mod.noter(conn, prospect_id, d.get("resultat"), d.get("note")))

    @app.post("/api/campagnes/<int:campagne_id>/contacts")
    def api_contacts_chercher(campagne_id):
        """Cherche l'email, à défaut le formulaire de contact, des prospects de la campagne jamais explorés (tâche de fond)."""
        with db.connexion() as conn:
            _exiger_campagne(conn, campagne_id)
            if jobs.actif(conn, campagne_id):
                return jsonify(erreur="Une tâche (scan ou analyse) est déjà en cours pour cette campagne."), 409
        return jsonify(job_id=jobs.soumettre("analyse", campagne_id, sourcing.chercher_contacts, campagne_id))

    # --- Appels téléphoniques ---

    @app.get("/appels")
    def appels():
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            liste = [dict(l) for l in conn.execute(
                "SELECT id, nom FROM campagnes WHERE est_modele = 0 AND statut <> 'archivee' ORDER BY nom")]
        demain = (datetime.strptime(appels_mod.aujourdhui(cfg), "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        return render_template("appels.html", campagnes=liste, etats=appels_mod.ETATS, resultats=appels_mod.RESULTATS,
                               ia_prete=not writer.configuration_manquante(cfg), demain=demain,
                               aujourdhui=appels_mod.aujourdhui(cfg))

    @app.get("/api/appels/file")
    def api_appels_file():
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            return jsonify(appels_mod.file(
                conn, cfg, request.args.get("campagne") or None, request.args.get("contact", "sans"),
                request.args.get("etat", "faire")))

    @app.get("/api/appels/<int:prospect_id>")
    def api_appel_fiche(prospect_id):
        with db.connexion() as conn:
            return jsonify(appels_mod.fiche(conn, prospect_id))

    @app.post("/api/appels/<int:prospect_id>/script")
    def api_appel_script(prospect_id):
        """Écrit (ou réécrit) le script d'appel avec l'IA et l'enregistre."""
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
        return jsonify(script=appels_mod.generer_script(cfg, prospect_id))

    @app.post("/api/appels/<int:prospect_id>/script/enregistrer")
    def api_appel_script_enregistrer(prospect_id):
        with db.connexion() as conn:
            appels_mod.enregistrer_script(conn, prospect_id, (request.get_json(silent=True) or {}).get("script"))
        return jsonify(ok=True)

    @app.post("/api/appels/<int:prospect_id>/resultat")
    def api_appel_resultat(prospect_id):
        d = request.get_json(silent=True) or {}
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            return jsonify(appels_mod.noter_appel(conn, cfg, prospect_id, d.get("resultat"), d.get("note"), d.get("rappel_le")))

    @app.get("/messages")
    def messages():
        with db.connexion() as conn:
            compteurs = messages_mod.compteurs(conn)
        return render_template("messages.html", en_attente=compteurs["en_attente"], approuves=compteurs["approuves"])

    @app.get("/api/messages/file")
    def api_messages_file():
        with db.connexion() as conn:
            return jsonify(compteurs=messages_mod.compteurs(conn), elements=messages_mod.file_de_validation(conn))

    @app.get("/api/prospects/<int:prospect_id>/revue")
    def api_prospect_revue(prospect_id):
        with db.connexion() as conn:
            return jsonify(messages_mod.revue(conn, prospect_id, reglages_mod.valeurs_effectives(conn)))

    def _texte_de_la_requete():
        donnees = request.get_json(silent=True) or {}
        return donnees.get("objet"), donnees.get("corps")

    @app.post("/api/messages/<int:message_id>/approuver")
    def api_message_approuver(message_id):
        """Le seul chemin vers le statut « approuvé » : un clic humain, sur le texte affiché à l'écran."""
        objet, corps = _texte_de_la_requete()
        with db.connexion() as conn:
            messages_mod.approuver(conn, message_id, objet, corps)
            return jsonify(compteurs=messages_mod.compteurs(conn))

    @app.post("/api/messages/apercu")
    def api_message_apercu():
        """Aperçu fidèle de l'email tel qu'il sera reçu (mêmes fonctions que l'envoi), sans rien envoyer ni enregistrer."""
        donnees = request.get_json(silent=True) or {}
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
        return jsonify(sender_mod.apercu(cfg, str(donnees.get("corps") or ""), bool(donnees.get("relance"))))

    @app.post("/api/messages/<int:message_id>/desapprouver")
    def api_message_desapprouver(message_id):
        with db.connexion() as conn:
            messages_mod.desapprouver(conn, message_id)
            return jsonify(compteurs=messages_mod.compteurs(conn))

    @app.post("/api/messages/<int:message_id>/enregistrer")
    def api_message_enregistrer(message_id):
        objet, corps = _texte_de_la_requete()
        with db.connexion() as conn:
            modifie = messages_mod.enregistrer_edition(conn, message_id, objet, corps)
            return jsonify(modifie=modifie, compteurs=messages_mod.compteurs(conn))

    @app.post("/api/prospects/<int:prospect_id>/email")
    def api_prospect_email(prospect_id):
        with db.connexion() as conn:
            adresse = messages_mod.definir_email(conn, prospect_id, (request.get_json(silent=True) or {}).get("email"))
            return jsonify(email=adresse, compteurs=messages_mod.compteurs(conn))

    @app.get("/envois")
    def envois():
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            quota = sender_mod.quota(conn, cfg)
            file = sender_mod.en_attente_d_envoi(conn)
            bloques = sum(1 for f in file if db.est_en_liste_noire(conn, f["email"]))
            hors_essai = sum(1 for f in file if not sender_mod.destinataire_autorise(cfg, f["email"]))
            partiront = sum(1 for f in file if sender_mod.destinataire_autorise(cfg, f["email"])
                            and not db.est_en_liste_noire(conn, f["email"]))
            autorisees = json.loads(cfg.get("envoi_adresses_autorisees") or "[]")
            historique = [dict(l) for l in conn.execute(
                """SELECT e.id, e.envoye_le, e.statut, e.erreur, e.brevo_id, p.id AS prospect_id, p.nom, p.email, p.statut AS statut_prospect,
                          c.id AS campagne_id, c.nom AS campagne_nom,
                          r.type AS reponse_type, r.extrait AS reponse_extrait, r.recu_le AS reponse_le
                     FROM envois e JOIN messages m ON m.id = e.message_id JOIN prospects p ON p.id = m.prospect_id
                     JOIN campagnes c ON c.id = p.campagne_id
                     LEFT JOIN reponses r ON r.id = (SELECT MAX(id) FROM reponses WHERE prospect_id = p.id)
                    ORDER BY e.id DESC LIMIT 1000""")]
            campagnes_liste = [dict(l) for l in conn.execute(
                "SELECT id, nom FROM campagnes WHERE id IN (SELECT campagne_id FROM prospects WHERE id IN "
                "(SELECT prospect_id FROM messages WHERE id IN (SELECT message_id FROM envois))) ORDER BY nom")]
            job = jobs.dernier_du_type(conn, "envoi")
            relances = suivi.file_de_relance(conn, cfg)
            derniere_sync = db.get_reglage(conn, "suivi_derniere_sync")
        fuseau = ZoneInfo(cfg["fuseau"])
        for r in relances:
            r["date_locale"] = datetime.strptime(r["dernier_envoi"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).astimezone(fuseau).strftime("%d/%m/%Y")
        if derniere_sync:
            derniere_sync = datetime.strptime(derniere_sync, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).astimezone(fuseau).strftime("%d/%m/%Y %H:%M")
        for l in historique:
            local = datetime.strptime(l["envoye_le"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(fuseau)
            l["date_locale"], l["jour_local"] = local.strftime("%d/%m/%Y %H:%M"), local.strftime("%Y-%m-%d")
            l["incertain"] = (l["erreur"] or "").startswith(sender_mod.INCERTAIN) and l["statut"] == "echec"
        a_envoyer = min(partiront, quota["reste"])
        pause_moyenne = (int(cfg["pause_envoi_min_s"]) + int(cfg["pause_envoi_max_s"])) / 2
        return render_template(
            "envois.html", quota=quota, nb_approuves=len(file), nb_bloques=bloques, a_envoyer=a_envoyer,
            mode_essai=autorisees, nb_hors_essai=hors_essai,
            minutes=round(a_envoyer * pause_moyenne / 60), manquants=sender_mod.configuration_manquante(cfg),
            pause_min=cfg["pause_envoi_min_s"], pause_max=cfg["pause_envoi_max_s"],
            historique=historique, campagnes=campagnes_liste, job=job, relances=relances,
            derniere_sync=derniere_sync, delai_relance=cfg["delai_relance_jours"],
            suivi_possible=bool(cfg.get("brevo_key")) and bool(historique),
            job_actif=bool(job and job["statut"] in jobs.STATUTS_ACTIFS))

    @app.post("/api/envois/envoyer")
    def api_envois_envoyer():
        """Envoie les messages approuvés. Aucun paramètre : rien ne peut forcer ni contourner l'approbation."""
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            manquants = sender_mod.configuration_manquante(cfg)
            if manquants:
                return jsonify(erreur="Réglages incomplets : il manque " + ", ".join(manquants) + "."), 400
            if sender_mod.quota(conn, cfg)["reste"] <= 0:
                return jsonify(erreur=f"Plafond du jour atteint ({cfg['plafond_jour']} envois). Reviens demain."), 400
            file = sender_mod.en_attente_d_envoi(conn)
            if not file:
                return jsonify(erreur="Aucun message approuvé à envoyer : approuve des messages dans l'écran Messages."), 400
            if not any(sender_mod.destinataire_autorise(cfg, f["email"]) for f in file):
                return jsonify(erreur="Mode essai actif : aucun des messages approuvés n'est destiné à une adresse autorisée. "
                                      "Rien n'est envoyé (Réglages > Limites)."), 400
            actif = conn.execute("SELECT 1 FROM jobs WHERE type = 'envoi' AND statut IN ('en_attente', 'en_cours')").fetchone()
            if actif:
                return jsonify(erreur="Un envoi est déjà en cours."), 409
        return jsonify(job_id=jobs.soumettre("envoi", None, sender_mod.envoyer_lot))

    @app.post("/api/envois/synchroniser")
    def api_envois_synchroniser():
        """Interroge Brevo : ouvertures, rebonds définitifs et désinscriptions (ces deux-là vont en liste noire)."""
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
        try:
            compte = suivi.synchroniser_brevo(cfg)
        except suivi.ErreurSuivi as erreur:
            return jsonify(erreur=str(erreur)), 400
        compte.update(reponses=0, stops=0, automatiques=0)
        if boite._configuree(cfg):  # les réponses sont lues dans ta boîte mail (lecture seule)
            try:
                lues = boite.synchroniser_reponses(cfg)
                compte.update(reponses=lues["reponses"], stops=lues["stops"], automatiques=lues["automatiques"])
                compte["message"] += " " + lues["message"]
            except boite.ErreurBoite as erreur:
                compte["message"] += " Boîte mail : " + str(erreur)
        else:
            compte["message"] += " Pour voir les réponses ici, renseigne ta boîte mail dans Réglages (Clés et services)."
        return jsonify(compte)

    @app.post("/api/prospects/<int:prospect_id>/repondu")
    def api_prospect_repondu(prospect_id):
        try:
            with db.connexion() as conn:
                suivi.marquer_repondu(conn, prospect_id)
        except ValueError as erreur:
            return jsonify(erreur=str(erreur)), 400
        return jsonify(ok=True)

    @app.post("/api/relances/preparer")
    def api_relances_preparer():
        """Génère les relances (message à valider ensuite dans l'écran Messages). Sans liste : toutes celles dues."""
        voulus = (request.get_json(silent=True) or {}).get("prospect_ids")
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            manquants = writer.configuration_manquante(cfg)
            if manquants:
                return jsonify(erreur="Fournisseur d'IA non configuré : il manque " + ", ".join(manquants) + "."), 400
            dues = [r["id"] for r in suivi.file_de_relance(conn, cfg)]
        ids = [i for i in dues if not voulus or i in voulus]
        if not ids:
            return jsonify(erreur="Aucune relance à préparer pour le moment."), 400
        return jsonify(job_id=jobs.soumettre("redaction", None, writer.rediger_lot, ids, False, True))

    @app.post("/api/prospects/export")
    def api_prospects_export():
        """CSV des prospects indiqués (la sélection ou les lignes filtrées à l'écran)."""
        ids = [i for i in ((request.get_json(silent=True) or {}).get("prospect_ids") or []) if isinstance(i, int)][:20000]
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            contenu = importer.exporter_csv(conn, ids, cfg["fuseau"], scorer.PLAFOND_AFFICHAGE)
        nom = f"prospects-{datetime.now(ZoneInfo(cfg['fuseau'])).strftime('%Y-%m-%d')}.csv"
        return Response(contenu, mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{nom}"'})

    # --- Analyses et décisions ---

    @app.get("/analyses")
    def analyses():
        jours = request.args.get("jours", type=int)
        jours = jours if jours in (7, 30, 90) else None
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
            donnees = analyses_mod.donnees_page(conn, cfg, jours)
            rapports = analyste.historique(conn)
            ia_prete = all((cfg.get(k) or "").strip() for k in ("ai_endpoint", "ai_model", "ai_key"))
        return render_template("analyses.html", **donnees, rapports=rapports, ia_prete=ia_prete)

    @app.post("/api/analyses/ia")
    def api_analyses_ia():
        """Demande à l'IA de lire les statistiques agrégées (aucune donnée personnelle) et de conseiller."""
        d = request.get_json(silent=True) or {}
        jours = d.get("jours") if d.get("jours") in (7, 30, 90) else None
        try:
            with db.connexion() as conn:
                rapport = analyste.analyser(conn, reglages_mod.valeurs_effectives(conn), jours, d.get("question"))
        except analyste.ErreurAnalyste as erreur:
            return jsonify(erreur=str(erreur)), 400
        return jsonify(rapport)

    @app.post("/api/domaine/verifier")
    def api_domaine_verifier():
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn)
        try:
            resultat = domaine.verifier_domaine(cfg)
        except domaine.ErreurDomaine as erreur:
            return jsonify(erreur=str(erreur)), 400
        with db.connexion() as conn:
            domaine.memoriser(conn, resultat)
        return jsonify(resultat)

    # --- Sauvegardes, journal, notes ---

    @app.get("/api/sauvegardes")
    def api_sauvegardes():
        return jsonify(sauvegarde.lister())

    @app.post("/api/sauvegardes")
    def api_sauvegarder():
        with db.connexion() as conn:
            maximum = int(reglages_mod.valeurs_effectives(conn)["sauvegardes_max"])
        nom = sauvegarde.sauvegarder(maximum=maximum)
        return jsonify(nom=nom, sauvegardes=sauvegarde.lister())

    @app.get("/api/journal")
    def api_journal():
        """Les dernières lignes du journal d'activité (appels externes, envois, erreurs)."""
        n = min(request.args.get("lignes", 200, type=int), 1000)
        if not config.LOG_PATH.exists():
            return jsonify(lignes=[])
        with open(config.LOG_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 300_000))
            contenu = f.read().decode("utf-8", errors="replace")
        return jsonify(lignes=contenu.splitlines()[-n:])

    @app.post("/api/prospects/<int:prospect_id>/notes")
    def api_prospect_notes(prospect_id):
        notes = ((request.get_json(silent=True) or {}).get("notes") or "").strip()[:5000]
        with db.connexion() as conn:
            if conn.execute("SELECT 1 FROM prospects WHERE id = ?", (prospect_id,)).fetchone() is None:
                abort(404)
            conn.execute("UPDATE prospects SET notes = ? WHERE id = ?", (notes or None, prospect_id))
        return jsonify(ok=True)

    @app.post("/api/envois/arreter")
    def api_envois_arreter():
        sender_mod.demander_arret()
        return jsonify(ok=True)

    @app.post("/api/envois/<int:envoi_id>/resoudre")
    def api_envoi_resoudre(envoi_id):
        envoye = bool((request.get_json(silent=True) or {}).get("envoye"))
        try:
            with db.connexion() as conn:
                sender_mod.resoudre_incertain(conn, envoi_id, envoye)
        except ValueError as erreur:
            return jsonify(erreur=str(erreur)), 400
        return jsonify(ok=True)

    # --- Réglages -----------------------------------------------------------------------

    @app.get("/reglages")
    def reglages():
        with db.connexion() as conn:
            etat = reglages_mod.lire_pour_ui(conn)
            cfg = reglages_mod.valeurs_effectives(conn)
            liste_noire = _lire_liste_noire(conn)
            google_usage = sourcing.requetes_google_du_mois(conn)
            verif_domaine = domaine.derniere_verification(conn)
        sauvegardes = sauvegarde.lister()
        return render_template(
            "reglages.html", etat=etat, liste_noire=liste_noire,
            fournisseur=sourcing.fournisseur_effectif(cfg)[1],
            google_usage=google_usage, verif_domaine=verif_domaine, sauvegardes=sauvegardes,
            endpoints_ia=config.ENDPOINTS_IA_SUGGERES,
        )

    @app.post("/api/reglages")
    def api_reglages_enregistrer():
        donnees = request.get_json(silent=True) or {}
        try:
            with db.connexion() as conn:
                modifiees = reglages_mod.sauvegarder(
                    conn, donnees.get("valeurs") or {}, donnees.get("effacer") or [])
                etat = reglages_mod.lire_pour_ui(conn)
                cfg = reglages_mod.valeurs_effectives(conn)
        except reglages_mod.ErreurValidation as erreur:
            return jsonify(erreur="Certains champs sont invalides.", erreurs=erreur.erreurs), 400
        except RuntimeError as erreur:  # garde .gitignore
            return jsonify(erreur=str(erreur)), 500
        return jsonify(modifiees=modifiees, etat=_etat_secrets(etat),
                       fournisseur=sourcing.fournisseur_effectif(cfg)[1])

    @app.post("/api/reglages/tester/<service>")
    def api_reglages_tester(service):
        testeurs = {"google": sourcing.tester_connexion, "ia": writer.tester_connexion,
                    "brevo": sender.tester_connexion, "boite": boite.tester_connexion}
        if service not in testeurs:
            return jsonify(erreur=f"Service inconnu : {service}"), 404
        donnees = request.get_json(silent=True) or {}
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn, donnees.get("valeurs") or {})
        return jsonify(testeurs[service](cfg))

    @app.post("/api/reglages/modeles")
    def api_reglages_modeles():
        """Liste les modèles du fournisseur d'IA (avec l'endpoint et la clé du formulaire, sinon ceux enregistrés)."""
        donnees = request.get_json(silent=True) or {}
        with db.connexion() as conn:
            cfg = reglages_mod.valeurs_effectives(conn, donnees.get("valeurs") or {})
        return jsonify(writer.lister_modeles(cfg))

    @app.get("/api/liste-noire")
    def api_liste_noire():
        with db.connexion() as conn:
            return jsonify(_lire_liste_noire(conn))

    @app.post("/api/liste-noire")
    def api_liste_noire_ajouter():
        brut = (request.get_json(silent=True) or {}).get("valeur", "")
        valeur = reglages_mod.valeur_liste_noire_valide(brut)
        if valeur is None:
            return jsonify(erreur="Saisis une adresse email (nom@domaine.fr) ou un domaine (domaine.fr)."), 400
        with db.connexion() as conn:
            ajoutee = db.ajouter_liste_noire(conn, valeur, "manuel")
            liste = _lire_liste_noire(conn)
        return jsonify(ajoutee=ajoutee, valeur=valeur, liste=liste)

    @app.errorhandler(404)
    def introuvable(_erreur):
        return render_template("404.html"), 404

    return app


def _lancer():
    adresse = f"http://localhost:{config.PORT}"
    with socket.socket() as sonde:
        if sonde.connect_ex((config.HOST, config.PORT)) == 0:
            print(f"\nL'application tourne déjà (le port {config.PORT} est occupé) : j'ouvre {adresse}.")
            webbrowser.open(adresse)
            return
    application = create_app()
    try:
        with db.connexion() as conn:
            maximum = int(reglages_mod.valeurs_effectives(conn)["sauvegardes_max"])
        sauvegarde.sauvegarde_auto(maximum)  # une copie par jour au plus, dès que la base contient du travail
    except Exception as erreur:  # une sauvegarde ratée ne doit jamais empêcher de démarrer
        print(f"  (sauvegarde automatique impossible : {erreur})")
    acces = application.config["ACCES_INITIAL"]
    print("\n" + "=" * 62)
    print(f"  Prospection est prête :  {adresse}")
    print("  (laisse cette fenêtre ouverte ; ferme-la pour arrêter l'application)")
    if acces:
        print("\n  PREMIÈRE CONNEXION : note ces identifiants, ils ne seront plus affichés.")
        print(f"    Identifiant  : {acces[0]}\n    Mot de passe : {acces[1]}")
        print("  Tu pourras les changer dans Réglages.")
    print("=" * 62 + "\n")
    if os.environ.get("PROSPECTION_SANS_NAVIGATEUR") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(adresse)).start()
    application.run(host=config.HOST, port=config.PORT, debug=False)


if __name__ == "__main__":
    _lancer()
