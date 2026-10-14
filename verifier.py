"""Vérifie toute l'application d'une commande : python verifier.py

1. rejoue les auto-tests de chaque module (bases jetables, réseau simulé : aucun appel réel, aucun email) ;
2. vérifie la protection par identifiant et mot de passe (pages, API, session, essais répétés) ;
3. ouvre chaque écran de l'application, connecté, sur une base jetable ;
4. vérifie la syntaxe des fichiers JavaScript (si Node.js est installé).

Ta vraie base (data/prospect.db) n'est jamais touchée.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile

RACINE = pathlib.Path(__file__).resolve().parent
MODULES = ["db", "auth", "campagnes", "sourcing", "analyzer", "importer", "writer", "messages", "sender", "suivi",
           "sauvegarde", "domaine", "analyses", "analyste", "courriel", "niche", "appels", "formulaires", "social", "boite"]
ECRANS = ["/", "/campagnes", "/campagnes/1", "/prospects", "/formulaires", "/messages-prives", "/appels", "/messages", "/envois", "/analyses", "/reglages"]
IDENTIFIANT_TEST, MOT_DE_PASSE_TEST = "verif", "mot-de-passe-de-verification"


def auto_tests():
    echecs = 0
    for module in MODULES:
        sortie = subprocess.run([sys.executable, "-m", f"core.{module}", "check"], cwd=RACINE,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        resume = next((l for l in reversed(sortie.stdout.splitlines()) if l.startswith("Résultat")), "aucun résultat")
        ok = sortie.returncode == 0
        echecs += not ok
        print(f"  {'OK ' if ok else 'KO '} core.{module:10} {resume}")
        if not ok:
            for l in sortie.stdout.splitlines():
                if l.startswith("KO"):
                    print("       " + l)
            print(sortie.stderr[-600:])
    return echecs


def application_jetable():
    import config

    dossier = pathlib.Path(tempfile.mkdtemp())
    config.DB_PATH = dossier / "verif.db"
    sys.path.insert(0, str(RACINE))
    import app
    from core import auth, db

    application = app.create_app()
    with db.connexion() as conn:
        auth.definir_acces(conn, IDENTIFIANT_TEST, MOT_DE_PASSE_TEST)
    return application, dossier


def acces_et_ecrans():
    from core import auth

    application, dossier = application_jetable()
    echecs = 0

    def verifier(libelle, condition, detail=""):
        nonlocal echecs
        echecs += not condition
        print(f"  {'OK ' if condition else 'KO '} {libelle}{(' : ' + str(detail)) if not condition else ''}")

    anonyme = application.test_client()
    verifier("page protégée sans connexion : redirigée vers /connexion",
             (r := anonyme.get("/prospects")).status_code == 302 and "/connexion" in r.headers["Location"], r.status_code)
    verifier("API sans connexion : 401, aucune donnée", (r := anonyme.get("/api/messages/file")).status_code == 401, r.status_code)
    verifier("API d'envoi sans connexion : 401 (rien ne peut partir sans être connecté)",
             (r := anonyme.post("/api/envois/envoyer", json={})).status_code == 401, r.status_code)
    verifier("adresse inconnue sans connexion : redirigée aussi (rien ne s'affiche)", anonyme.get("/introuvable").status_code == 302)
    verifier("page de connexion et feuille de style accessibles sans connexion",
             anonyme.get("/connexion").status_code == 200 and anonyme.get("/static/app.css").status_code == 200)
    verifier("la page de connexion ne révèle aucun chiffre ni donnée", b"prospect" not in anonyme.get("/connexion").data.lower().replace(b"prospection", b""))
    r = anonyme.post("/connexion", data={"identifiant": IDENTIFIANT_TEST, "mot_de_passe": "faux"})
    verifier("mauvais mot de passe : 401 et message générique", r.status_code == 401 and "incorrect" in r.get_data(as_text=True), r.status_code)
    verifier("toujours rien d'accessible après un échec", anonyme.get("/campagnes").status_code == 302)
    auth.effacer_echecs()

    client = application.test_client()
    r = client.post("/connexion", data={"identifiant": IDENTIFIANT_TEST, "mot_de_passe": MOT_DE_PASSE_TEST, "suite": "/messages"})
    verifier("bonne connexion : redirigée vers la page demandée", r.status_code == 302 and r.headers["Location"].endswith("/messages"), r.status_code)
    cookie = r.headers.get("Set-Cookie", "")
    verifier("cookie de session : HttpOnly et SameSite=Strict", "HttpOnly" in cookie and "SameSite=Strict" in cookie, cookie[:120])
    r = client.post("/connexion", data={"identifiant": IDENTIFIANT_TEST, "mot_de_passe": MOT_DE_PASSE_TEST, "suite": "//evil.example"})
    verifier("redirection après connexion : jamais vers un autre site", r.headers["Location"].endswith("/") and "evil" not in r.headers["Location"])

    print("  --  écrans (connecté)")
    for url in ECRANS:
        reponse = client.get(url, follow_redirects=True)
        verifier(f"{url:16} s'affiche", reponse.status_code == 200, reponse.status_code)
    verifier("adresse inconnue (connecté) : page 404", client.get("/introuvable").status_code == 404)
    verifier("l'accueil affiche la mise en route sur une base neuve", "Mise en route" in client.get("/").get_data(as_text=True))

    # --- suppression d'une campagne : API et écran ---
    from core import db

    vide = client.post("/api/campagnes", json={"nom": "À supprimer", "secteur": "s", "pays": "FR", "langue": "fr"}).get_json()["id"]
    verifier("chaque campagne a sa corbeille dans la liste", f'data-supprimer-campagne="{vide}"' in client.get("/campagnes").get_data(as_text=True))
    verifier("campagne vide : supprimée (200), puis introuvable (404)",
             client.delete(f"/api/campagnes/{vide}", json={}).status_code == 200 and client.get(f"/campagnes/{vide}").status_code == 404)
    verifier("campagne inexistante : 404", client.delete("/api/campagnes/99999", json={}).status_code == 404)
    envoyee = client.post("/api/campagnes", json={"nom": "Avec envoi", "secteur": "s", "pays": "FR", "langue": "fr"}).get_json()["id"]
    with db.connexion() as conn:
        db.inserer_prospect(conn, {"campagne_id": envoyee, "nom": "Cabinet", "email": "c@cabinet.fr", "ville": "Lyon",
                                   "source": "openstreetmap", "source_id": "osm:1"})
        mid = conn.execute("INSERT INTO messages (prospect_id, objet, corps, version) "
                           "SELECT id, 'O', 'C', 1 FROM prospects WHERE campagne_id = ?", (envoyee,)).lastrowid
        conn.execute("INSERT INTO envois (message_id, statut, brevo_id) VALUES (?, 'envoye', '<a@brevo>')", (mid,))
    r = client.delete(f"/api/campagnes/{envoyee}", json={})
    verifier("campagne avec un email déjà envoyé : 409 et confirmation demandée, rien n'est supprimé",
             r.status_code == 409 and r.get_json().get("confirmation_requise") and r.get_json().get("envois") == 1
             and client.get(f"/campagnes/{envoyee}").status_code == 200, r.status_code)
    verifier("même campagne, confirmée : supprimée avec son historique d'envoi",
             client.delete(f"/api/campagnes/{envoyee}", json={"forcer": True}).status_code == 200
             and client.get(f"/campagnes/{envoyee}").status_code == 404)
    # --- assistant de niche : écran et API ---
    page = client.get("/campagnes").get_data(as_text=True)
    verifier("l'écran Campagnes propose l'assistant de niche (formulaire, relecture, script)",
             'id="form-niche"' in page and 'id="proposition-niche"' in page and "niche.js" in page)
    r = client.post("/api/niche/proposer", json={"niche": "", "offre": "", "ville": ""})
    verifier("assistant : champs obligatoires signalés un par un (400)", r.status_code == 400 and set(r.get_json()["erreurs"]) == {"niche", "offre", "ville"}, r.status_code)
    r = client.post("/api/niche/proposer", json={"niche": "kiné", "offre": "site", "ville": "Lyon"})
    verifier("assistant sans IA configurée : message clair qui dit ce qui manque, aucun appel", r.status_code == 400 and "Réglages" in r.get_json()["erreur"], r.status_code)
    verifier("assistant sans connexion : refusé (401)",
             application.test_client().post("/api/niche/proposer", json={}).status_code == 401
             and application.test_client().post("/api/niche/creer", json={}).status_code == 401)
    # --- appels téléphoniques : écran et API ---
    verifier("l'écran Appels est caché du menu par défaut (pas de numéro : pas d'appels)", 'href="/appels"' not in client.get("/").get_data(as_text=True))
    with db.connexion() as conn:
        db.set_reglage(conn, "canal_appels", "oui")
    verifier("réglage « canal téléphone : oui » : l'écran Appels apparaît dans le menu", 'href="/appels"' in client.get("/").get_data(as_text=True))
    with db.connexion() as conn:
        db.set_reglage(conn, "canal_appels", "non")
    page = client.get("/appels").get_data(as_text=True)
    verifier("l'écran Appels propose les filtres, la fiche d'appel et ses six résultats",
             'id="f-contact"' in page and 'id="fiche-appel"' in page and page.count('data-resultat="') == 6 and "appels.js" in page)
    appel_c = client.post("/api/campagnes", json={"nom": "Appels", "secteur": "s", "pays": "FR", "langue": "fr"}).get_json()["id"]
    with db.connexion() as conn:
        db.inserer_prospect(conn, {"campagne_id": appel_c, "nom": "Cabinet Appel", "telephone": "04 72 00 00 01", "ville": "Lyon",
                                   "source": "google_places", "source_id": "g:appel"})
        pid = conn.execute("SELECT id FROM prospects WHERE campagne_id = ?", (appel_c,)).fetchone()[0]
        conn.execute("UPDATE prospects SET statut = 'qualifie', problemes = '[\"sans_site\"]', score = 10 WHERE id = ?", (pid,))
    file_appels = client.get("/api/appels/file").get_json()
    verifier("file d'appels : le prospect sans email avec téléphone y est, avec son vrai problème et un lien tel:",
             [l["nom"] for l in file_appels["lignes"]] == ["Cabinet Appel"] and file_appels["lignes"][0]["problemes"] == ["Aucun site web"]
             and file_appels["lignes"][0]["tel"] == "0472000001")
    r = client.post(f"/api/appels/{pid}/resultat", json={"resultat": "a_rappeler"})
    verifier("rappel sans date : refusé (400) avec le champ en cause", r.status_code == 400 and "rappel_le" in r.get_json()["erreurs"], r.status_code)
    r = client.post(f"/api/appels/{pid}/resultat", json={"resultat": "pas_de_reponse", "note": "Répondeur"})
    verifier("appel enregistré (200), puis visible dans la fiche",
             r.status_code == 200 and client.get(f"/api/appels/{pid}").get_json()["historique"][0]["note"] == "Répondeur", r.status_code)
    r = client.post(f"/api/appels/{pid}/script", json={})
    verifier("script d'appel sans IA configurée : message clair (400), rien d'inventé", r.status_code == 400 and "Réglages" in r.get_json()["erreur"], r.status_code)
    verifier("fiche d'un prospect inconnu : 400 avec message", client.get("/api/appels/99999").status_code == 400)
    verifier("appels sans connexion : refusés (401)",
             application.test_client().get("/api/appels/file").status_code == 401
             and application.test_client().post(f"/api/appels/{pid}/resultat", json={"resultat": "interesse"}).status_code == 401)
    # --- formulaires de contact : écran, API, recherche des contacts d'une campagne ---
    verifier("l'écran Formulaires est dans le rail de navigation", 'href="/formulaires"' in client.get("/").get_data(as_text=True))
    page = client.get("/formulaires").get_data(as_text=True)
    verifier("l'écran Formulaires propose le lien du formulaire, la copie du message et ses quatre résultats",
             'id="ff-ouvrir"' in page and 'id="ff-copier"' in page and page.count('data-resultat="') == 4 and "formulaires.js" in page
             and 'rel="noopener noreferrer"' in page)
    with db.connexion() as conn:
        conn.execute("UPDATE prospects SET formulaire_url = 'https://cabinet.fr/contact', email = NULL WHERE id = ?", (pid,))
    file_formulaires = client.get("/api/formulaires/file").get_json()
    verifier("file de formulaires : le prospect sans email avec un formulaire y est, avec son vrai problème et l'adresse du formulaire",
             [l["nom"] for l in file_formulaires["lignes"]] == ["Cabinet Appel"]
             and file_formulaires["lignes"][0]["formulaire_url"] == "https://cabinet.fr/contact"
             and file_formulaires["lignes"][0]["problemes"] == ["Aucun site web"])
    r = client.post(f"/api/formulaires/{pid}/resultat", json={"resultat": "envoye", "note": "Page Contact"})
    verifier("envoi noté (200), visible dans la fiche", r.status_code == 200
             and client.get(f"/api/formulaires/{pid}").get_json()["historique"][0]["note"] == "Page Contact", r.status_code)
    r = client.post(f"/api/formulaires/{pid}/message", json={})
    verifier("message de formulaire sans IA configurée : message clair (400)", r.status_code == 400 and "Réglages" in r.get_json()["erreur"], r.status_code)
    r = client.post(f"/api/formulaires/{pid}/resultat", json={"resultat": "peut-etre"})
    verifier("résultat inconnu : refusé (400)", r.status_code == 400, r.status_code)
    r = client.post(f"/api/campagnes/{appel_c}/contacts", json={})
    verifier("chercher les contacts d'une campagne : tâche lancée (200)", r.status_code == 200 and "job_id" in r.get_json(), r.status_code)
    verifier("campagne inconnue : 404", client.post("/api/campagnes/99999/contacts", json={}).status_code == 404)
    verifier("formulaires sans connexion : refusés (401)",
             application.test_client().get("/api/formulaires/file").status_code == 401
             and application.test_client().post(f"/api/campagnes/{appel_c}/contacts", json={}).status_code == 401)
    page_prospects = client.get("/prospects").get_data(as_text=True)
    verifier("écran Prospects : filtre « Site web » (avec / sans site propre) et marque data-site sur chaque ligne",
             'id="f-site"' in page_prospects and 'data-site="' in page_prospects and 'value="sans"' in page_prospects)
    # --- messages privés (Facebook, Instagram…) : écran, API ---
    verifier("l'écran Messages privés est dans le rail de navigation", 'href="/messages-prives"' in client.get("/").get_data(as_text=True))
    page = client.get("/messages-prives").get_data(as_text=True)
    verifier("l'écran Messages privés propose l'adresse du profil, la copie du message et ses quatre résultats",
             'id="fs-profil"' in page and 'id="fs-copier"' in page and page.count('data-resultat="') == 4 and "social.js" in page)
    with db.connexion() as conn:
        conn.execute("UPDATE prospects SET formulaire_url = NULL, profil_social_url = 'https://www.facebook.com/cabinet', email = NULL WHERE id = ?", (pid,))
    file_social = client.get("/api/social/file?etat=tous").get_json()
    verifier("file de messages privés : le prospect sans email avec un profil y est, avec ses liens (profil, recherche) et son vrai problème",
             [l["nom"] for l in file_social["lignes"]] == ["Cabinet Appel"]
             and [x["type"] for x in file_social["lignes"][0]["liens"]] == ["profil", "recherche"]
             and file_social["lignes"][0]["problemes"] == ["Aucun site web"])
    r = client.post(f"/api/social/{pid}/profil", json={"profil": "instagram.com/cabinet"})
    verifier("adresse de profil collée : enregistrée complète (200) ; invalide : 400",
             r.status_code == 200 and r.get_json()["profil_social_url"] == "https://instagram.com/cabinet"
             and client.post(f"/api/social/{pid}/profil", json={"profil": "n'importe quoi"}).status_code == 400)
    r = client.post(f"/api/social/{pid}/resultat", json={"resultat": "envoye", "note": "Instagram"})
    verifier("message privé noté (200), visible dans la fiche", r.status_code == 200
             and client.get(f"/api/social/{pid}").get_json()["historique"][0]["note"] == "Instagram", r.status_code)
    r = client.post(f"/api/social/{pid}/message", json={})
    verifier("message privé sans IA configurée : message clair (400)", r.status_code == 400 and "Réglages" in r.get_json()["erreur"], r.status_code)
    r = client.post("/api/social/99999/ignorer", json={})
    verifier("écarter un prospect inconnu depuis les messages privés : 400 avec message ; le bouton existe dans l'écran",
             r.status_code == 400 and 'id="fs-ignorer"' in page, r.status_code)
    verifier("messages privés sans connexion : refusés (401)",
             application.test_client().get("/api/social/file").status_code == 401
             and application.test_client().post(f"/api/social/{pid}/resultat", json={"resultat": "envoye"}).status_code == 401)
    import time as _temps
    from core import jobs as _jobs
    for _ in range(100):  # laisse finir la tâche avant de supprimer la campagne
        with db.connexion() as conn:
            if _jobs.actif(conn, appel_c) is None:
                break
        _temps.sleep(0.1)
    client.delete(f"/api/campagnes/{appel_c}", json={})
    anonyme_suppr = application.test_client().delete("/api/campagnes/1", json={})
    verifier("sans connexion : la suppression est refusée (401)", anonyme_suppr.status_code == 401, anonyme_suppr.status_code)

    # --- changement de mot de passe : ferme les autres sessions, garde la courante ---
    autre = application.test_client()
    autre.post("/connexion", data={"identifiant": IDENTIFIANT_TEST, "mot_de_passe": MOT_DE_PASSE_TEST})
    r = client.post("/api/acces", json={"actuel": "faux", "nouveau": "nouveau-mot-de-passe", "confirmation": "nouveau-mot-de-passe"})
    verifier("changer l'accès avec un mauvais mot de passe actuel : refusé", r.status_code == 400, r.status_code)
    auth.effacer_echecs()
    r = client.post("/api/acces", json={"actuel": MOT_DE_PASSE_TEST, "nouveau": "nouveau-mot-de-passe", "confirmation": "autre-chose"})
    verifier("confirmation différente : refusé", r.status_code == 400, r.status_code)
    r = client.post("/api/acces", json={"actuel": MOT_DE_PASSE_TEST, "nouveau": "court", "confirmation": "court"})
    verifier("mot de passe trop court : refusé", r.status_code == 400, r.status_code)
    r = client.post("/api/acces", json={"actuel": MOT_DE_PASSE_TEST, "nouveau": "nouveau-mot-de-passe", "confirmation": "nouveau-mot-de-passe"})
    verifier("changement valide : accepté", r.status_code == 200, r.status_code)
    verifier("après changement : la session courante reste connectée", client.get("/campagnes").status_code == 200)
    verifier("après changement : les autres sessions sont fermées", autre.get("/campagnes").status_code == 302)
    anonyme2 = application.test_client()
    verifier("l'ancien mot de passe ne marche plus",
             anonyme2.post("/connexion", data={"identifiant": IDENTIFIANT_TEST, "mot_de_passe": MOT_DE_PASSE_TEST}).status_code == 401)
    auth.effacer_echecs()

    # --- déconnexion ---
    client.post("/deconnexion")
    verifier("déconnexion : plus rien d'accessible", client.get("/campagnes").status_code == 302)

    # --- essais répétés ---
    pirate = application.test_client()
    codes = [pirate.post("/connexion", data={"identifiant": IDENTIFIANT_TEST, "mot_de_passe": f"essai{i}"}).status_code for i in range(7)]
    verifier("essais répétés : bloqué (429) après 5 échecs", codes[:5] == [401] * 5 and 429 in codes[5:], codes)
    verifier("pendant le blocage, même le BON mot de passe est refusé",
             pirate.post("/connexion", data={"identifiant": IDENTIFIANT_TEST, "mot_de_passe": "nouveau-mot-de-passe"}).status_code == 429)
    auth.effacer_echecs()
    shutil.rmtree(dossier, ignore_errors=True)
    return echecs


def javascript():
    node = shutil.which("node")
    if not node:
        print("  --  Node.js absent : syntaxe JavaScript non vérifiée (sans importance pour l'utilisation)")
        return 0
    echecs = 0
    for fichier in sorted((RACINE / "static").glob("*.js")):
        sortie = subprocess.run([node, "--check", str(fichier)], capture_output=True, text=True)
        ok = sortie.returncode == 0
        echecs += not ok
        print(f"  {'OK ' if ok else 'KO '} static/{fichier.name}")
        if not ok:
            print(sortie.stderr[:500])
    return echecs


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("1/3  Auto-tests des modules")
    total = auto_tests()
    print("2/3  Accès protégé et écrans")
    total += acces_et_ecrans()
    print("3/3  JavaScript")
    total += javascript()
    print("\n" + ("TOUT EST BON." if total == 0 else f"{total} PROBLÈME(S) : voir les lignes KO ci-dessus."))
    sys.exit(0 if total == 0 else 1)
