"""Import de prospects depuis un fichier CSV (annuaires, salons, exports, recherche manuelle).

Les lignes importées entrent dans le circuit normal (analyse, score, rédaction, validation) avec
`import_csv` comme source. Colonnes reconnues : nom, ville, site, email, telephone (+ adresse en option).
Les en-têtes sont reconnus sans tenir compte de la casse, des accents ni de quelques variantes.

Utilisable seul : python -m core.importer FICHIER.csv ID_CAMPAGNE
                  python -m core.importer check
"""
import csv
import io
import json
import logging
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

from core import db

journal = logging.getLogger("prospect")

TAILLE_MAX_OCTETS = 5_000_000
LIGNES_MAX = 5000
_EN_TETES = {
    "nom": {"nom", "name", "etablissement", "raison sociale", "entreprise", "societe", "name_for_emails"},
    "ville": {"ville", "city", "commune"},
    "site": {"site", "site web", "website", "url", "web", "site internet"},
    "email": {"email", "e-mail", "mail", "courriel", "adresse email"},
    "telephone": {"telephone", "tel", "phone", "portable", "mobile", "numero de telephone"},
    "adresse": {"adresse", "address", "rue"},
    # Statut de validation de l'adresse email, quand l'export en fournit un (voir le réglage import_statuts_email_acceptes)
    "statut_email": {"email.emails_validator.status", "statut email", "statut de l'email", "email status", "email_status"},
}
_AVERTISSEMENTS_MAX = 20  # au-delà, un compte global : un fichier de 10 000 lignes ne doit pas noyer l'écran
_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RE_HOTE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def _cle(texte):
    decompose = unicodedata.normalize("NFKD", (texte or "").strip().lower())
    return "".join(c for c in decompose if not unicodedata.combining(c)).strip()


def _decoder(octets):
    for encodage in ("utf-8-sig", "cp1252"):
        try:
            return octets.decode(encodage)
        except UnicodeDecodeError:
            continue
    return octets.decode("latin-1")


def _normaliser_site(brut):
    site = (brut or "").strip()
    if not site:
        return None, None
    avec_schema = site if "//" in site else "https://" + site
    hote = (db.extraire_domaine(avec_schema) or "")
    if not _RE_HOTE.match(hote):
        return None, f"site invalide « {site[:60]} »"
    return avec_schema, None


def _trouver_entete(enregistrements):
    """Indice de la ligne d'en-têtes : les exports de leads commencent souvent par quelques lignes d'avertissement."""
    for i, enregistrement in enumerate(enregistrements[:30]):
        if any(_cle(cellule) in _EN_TETES["nom"] for cellule in enregistrement):
            return i
    return None


def importer_csv(conn, octets, campagne_id):
    """Importe un fichier CSV dans une campagne. Renvoie un compte rendu détaillé, sans rien masquer.

    {'importes', 'ignores', 'autres_campagnes', 'invalides': [(ligne, raison)], 'avertissements': [...],
     'nouveaux_ids', 'colonnes_ignorees', 'emails_ecartes': {statut: nombre}}
    Lève ValueError (message affiché tel quel) si le fichier est inutilisable.

    Les exports de leads comptent souvent une ligne par CONTACT : plusieurs lignes pour un même établissement
    (même site, ou même nom) sont regroupées en une seule fiche, qui garde l'adresse email utilisable. Si le fichier
    porte un statut de validation des emails, seuls les statuts du réglage `import_statuts_email_acceptes` sont gardés :
    les autres établissements sont importés SANS email (jamais écartés, ils restent joignables autrement).
    """
    if conn.execute("SELECT 1 FROM campagnes WHERE id = ?", (campagne_id,)).fetchone() is None:
        raise ValueError("Campagne introuvable.")
    if not octets:
        raise ValueError("Le fichier est vide.")
    if len(octets) > TAILLE_MAX_OCTETS:
        raise ValueError(f"Fichier trop volumineux (maximum {TAILLE_MAX_OCTETS // 1_000_000} Mo).")
    texte = _decoder(octets)
    echantillon = texte[:4096]
    try:
        dialecte = csv.Sniffer().sniff(echantillon, delimiters=";,\t|")
    except csv.Error:
        dialecte = csv.excel  # virgule par défaut
    lecteur = csv.reader(io.StringIO(texte), dialecte)
    enregistrements, numeros = [], []  # numeros : ligne du fichier où finit chaque enregistrement (celle qu'affiche un tableur)
    for enregistrement in lecteur:
        enregistrements.append(enregistrement)
        numeros.append(lecteur.line_num)
    if not enregistrements:
        raise ValueError("Le fichier est vide.")
    i_entete = _trouver_entete(enregistrements)
    if i_entete is None:
        lus = next((e for e in enregistrements if any(c.strip() for c in e)), [])
        raise ValueError("Colonne « nom » introuvable. En-têtes lus : " + ", ".join(e.strip() for e in lus[:10])
                         + ". Colonnes attendues : nom, ville, site, email, telephone.")
    entetes = enregistrements[i_entete]

    colonnes = {}
    for index, entete in enumerate(entetes):
        for champ, variantes in _EN_TETES.items():
            if _cle(entete) in variantes and champ not in colonnes:
                colonnes[champ] = index
    ignorees = [e.strip() for i, e in enumerate(entetes) if i not in colonnes.values() and e.strip()]
    acceptes = {str(s).strip().lower() for s in json.loads(db.get_reglage(conn, "import_statuts_email_acceptes", "[]"))}
    filtrer_emails = "statut_email" in colonnes and bool(acceptes)

    rapport = {"importes": 0, "ignores": 0, "autres_campagnes": 0, "invalides": [], "avertissements": [],
               "nouveaux_ids": [], "colonnes_ignorees": ignorees, "emails_ecartes": {}}
    avertissements, nb_avertissements = rapport["avertissements"], 0

    def avertir(message):
        nonlocal nb_avertissements
        nb_avertissements += 1
        if nb_avertissements <= _AVERTISSEMENTS_MAX:
            avertissements.append(message)

    # --- Lecture : une entrée par ligne utilisable ---
    lues = []
    for indice in range(i_entete + 1, len(enregistrements)):
        ligne, numero = enregistrements[indice], numeros[indice]
        if not any(c.strip() for c in ligne):
            continue

        def champ(nom):
            i = colonnes.get(nom)
            return ligne[i].strip() if i is not None and i < len(ligne) else ""

        nom = champ("nom")
        if not nom:
            rapport["invalides"].append((numero, "nom vide"))
            continue
        site, erreur_site = _normaliser_site(champ("site"))
        if erreur_site:
            avertir(f"Ligne {numero} ({nom}) : {erreur_site}, importé sans site.")
        email = db.normaliser_email(champ("email"))
        if email and not _RE_EMAIL.match(email):
            avertir(f"Ligne {numero} ({nom}) : email invalide « {email[:60]} », importé sans email.")
            email = None
        if email and filtrer_emails and champ("statut_email").lower() not in acceptes:
            statut = champ("statut_email") or "(vide)"
            rapport["emails_ecartes"][statut] = rapport["emails_ecartes"].get(statut, 0) + 1
            email = None
        telephone = champ("telephone")
        if telephone[:1] in ("#", "="):  # « #ERROR! » : erreur de formule d'un tableur, pas un numéro
            telephone = ""
        lues.append({"numero": numero, "nom": nom, "site": site, "email": email, "telephone": telephone or None,
                     "ville": champ("ville") or None, "adresse": champ("adresse") or None})

    # --- Regroupement : plusieurs contacts d'un même établissement = une seule fiche ---
    mentions, plateformes = db.reglage_json(conn, "mentions_juridiques"), db.reglage_json(conn, "domaines_plateformes")
    groupes = {}
    for l in lues:
        domaine = db.extraire_domaine(l["site"])
        if db.est_plateforme(domaine, plateformes):
            domaine = None
        cle = ("site", domaine) if domaine else ("nom", db.normaliser_nom(l["nom"], mentions), (l["ville"] or "").lower())
        groupes.setdefault(cle, []).append(l)
    regroupes = 0
    prospects = []
    for membres in groupes.values():
        choisi = dict(next((m for m in membres if m["email"]), membres[0]))
        for autre in membres:  # une information manquante est reprise d'une autre ligne du même établissement
            for cle_champ in ("telephone", "adresse", "ville", "site"):
                choisi[cle_champ] = choisi[cle_champ] or autre[cle_champ]
        regroupes += len(membres) - 1
        prospects.append(choisi)
    prospects.sort(key=lambda p: p["numero"])
    rapport["ignores"] += regroupes

    # --- Insertion ---
    for rang, l in enumerate(prospects, start=1):
        if rang > LIGNES_MAX:
            avertir(f"Import arrêté après {LIGNES_MAX} établissements : découpe le fichier.")
            break
        prospect = {"campagne_id": campagne_id, "zone_id": None, "nom": l["nom"], "adresse": l["adresse"],
                    "ville": l["ville"], "telephone": l["telephone"], "site": l["site"],
                    "email": l["email"], "source": "import_csv", "source_id": None, "note_moyenne": None, "nb_avis": None}
        doublon = db.trouver_doublon(conn, prospect)
        if doublon is None and db.inserer_prospect(conn, prospect):
            rapport["importes"] += 1
            # id du prospect inséré : recherché par nom normalisé + ville (clé unique)
            ligne_id = conn.execute("SELECT MAX(id) FROM prospects WHERE campagne_id = ?", (campagne_id,)).fetchone()
            rapport["nouveaux_ids"].append(ligne_id[0])
        else:
            rapport["ignores"] += 1
            if doublon and doublon["campagne_id"] != campagne_id:
                rapport["autres_campagnes"] += 1

    if nb_avertissements > _AVERTISSEMENTS_MAX:
        avertissements.append(f"… et {nb_avertissements - _AVERTISSEMENTS_MAX} autre(s) avertissement(s) du même type.")
    if i_entete:
        avertissements.append(f"Les en-têtes ont été trouvés à la ligne {numeros[i_entete]} : les lignes précédentes sont ignorées.")
    if regroupes:
        avertissements.append(f"{regroupes} ligne(s) regroupée(s) : plusieurs contacts d'un même établissement n'ont donné "
                              "qu'une fiche, avec l'adresse email utilisable.")
    if rapport["emails_ecartes"]:
        detail = ", ".join(f"{statut} : {n}" for statut, n in sorted(rapport["emails_ecartes"].items(), key=lambda x: -x[1]))
        avertissements.append(f"{sum(rapport['emails_ecartes'].values())} adresse(s) email non retenue(s) selon leur statut de "
                              f"validation ({detail}) : ces établissements sont importés sans email (Réglages > statuts acceptés).")
    journal.info("import CSV campagne %s : %d importés, %d ignorés, %d invalides",
                 campagne_id, rapport["importes"], rapport["ignores"], len(rapport["invalides"]))
    return rapport


# --------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------

COLONNES_EXPORT = ["Établissement", "Ville", "Adresse", "Téléphone", "Site", "Email", "Adresse générique",
                   "Problèmes détectés", "Score brut", "Score affiché", "Statut", "Campagne", "Source", "Date d'envoi"]
_LOT_SQL = 500


def _cellule(valeur):
    """Texte sûr pour un tableur : une cellule qui commence par = + - @ serait exécutée comme une formule."""
    texte = "" if valeur is None else str(valeur)
    return "'" + texte if texte[:1] in ("=", "+", "-", "@", "\t", "\r") else texte


def exporter_csv(conn, ids, fuseau, plafond_affichage):
    """CSV (UTF-8 avec BOM, séparateur « ; » : s'ouvre directement dans Excel) des prospects `ids`, dans cet ordre."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    libelles = {(l["campagne_id"], l["code"]): l["libelle"]
                for l in conn.execute("SELECT campagne_id, code, libelle FROM criteres")}
    lignes = {}
    for i in range(0, len(ids), _LOT_SQL):
        lot = ids[i:i + _LOT_SQL]
        for l in conn.execute(
                f"""SELECT p.*, c.nom AS campagne_nom, est_generique(p.email) AS generique,
                           (SELECT MAX(e.envoye_le) FROM envois e JOIN messages m ON m.id = e.message_id
                             WHERE m.prospect_id = p.id AND e.statut <> 'echec') AS date_envoi
                      FROM prospects p JOIN campagnes c ON c.id = p.campagne_id
                     WHERE p.id IN ({','.join('?' * len(lot))})""", lot):
            lignes[l["id"]] = l
    sortie = io.StringIO()
    ecrivain = csv.writer(sortie, delimiter=";", lineterminator="\r\n")
    ecrivain.writerow(COLONNES_EXPORT)
    zone = ZoneInfo(fuseau)
    for prospect_id in ids:
        p = lignes.get(prospect_id)
        if p is None:
            continue
        codes = json.loads(p["problemes"])
        problemes = " | ".join(libelles.get((p["campagne_id"], c)) or libelles.get((None, c)) or c for c in codes)
        envoi = ""
        if p["date_envoi"]:
            envoi = datetime.strptime(p["date_envoi"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(zone).strftime("%d/%m/%Y")
        ecrivain.writerow([_cellule(v) for v in (
            p["nom"], p["ville"], p["adresse"], p["telephone"], p["site"], p["email"], "oui" if p["generique"] else "non",
            problemes if p["statut"] != "nouveau" else "pas encore analysé", p["score"],
            min(p["score"], plafond_affichage) if p["statut"] != "nouveau" else "", p["statut"], p["campagne_nom"],
            p["source"], envoi)])
    return b"\xef\xbb\xbf" + sortie.getvalue().encode("utf-8")


# --------------------------------------------------------------------------------------
# Auto-tests et ligne de commande
# --------------------------------------------------------------------------------------


def _autotests():
    from core import campagnes

    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    contenu = ("Nom;Ville;Site Web;E-mail;Téléphone;Notes\n"
               "Cabinet Dupont;Lyon;www.dupont-dentiste.fr;Contact@Dupont-Dentiste.FR;04 78 00 00 00;vu au salon\n"
               "Cabinet Dupont;Lyon;;;;\n"                       # doublon nom + ville
               ";Lyon;;;;\n"                                       # nom vide
               "Cabinet Durand;Lyon;pas un site;pas-un-email;;\n"  # site et email invalides : importé quand même
               "Clinique Été;Paris;https://clinique-ete.fr;;;\n"
               "Autre nom;Lyon;https://dupont-dentiste.fr/contact;;;\n").encode("cp1252")  # même domaine, encodage Windows
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        chemin = Path(tmp) / "t.db"
        db.init_db(chemin, importer_env=False)
        with db.connexion(chemin) as c:
            cid = campagnes.creer(c, {"nom": "C", "secteur": "s", "pays": "FR", "langue": "fr"})
            r = importer_csv(c, contenu, cid)
            lignes = {l["nom"]: dict(l) for l in c.execute("SELECT * FROM prospects")}
            verifier("import : 3 importés (Dupont, Durand, Clinique Été), 2 ignorés (doublon nom, doublon domaine), 1 invalide",
                     r["importes"] == 3 and r["ignores"] == 2 and len(r["invalides"]) == 1 and r["invalides"][0][0] == 4)
            verifier("import : séparateur « ; » et encodage cp1252 (accents) reconnus",
                     "Clinique Été" in lignes and lignes["Cabinet Dupont"]["telephone"] == "04 78 00 00 00")
            verifier("import : email normalisé, site complété en https, source import_csv",
                     lignes["Cabinet Dupont"]["email"] == "contact@dupont-dentiste.fr"
                     and lignes["Cabinet Dupont"]["site"] == "https://www.dupont-dentiste.fr"
                     and lignes["Cabinet Dupont"]["source"] == "import_csv" and lignes["Cabinet Dupont"]["statut"] == "nouveau")
            verifier("import : site et email invalides signalés (avertissements), ligne importée quand même",
                     len([a for a in r["avertissements"] if a.startswith("Ligne ")]) == 2
                     and any("regroupée" in a for a in r["avertissements"]) and lignes["Cabinet Durand"]["site"] is None
                     and lignes["Cabinet Durand"]["email"] is None)
            verifier("import : colonne inconnue signalée, ids des nouveaux prospects renvoyés",
                     r["colonnes_ignorees"] == ["Notes"] and len(r["nouveaux_ids"]) == 3)
            r2 = importer_csv(c, contenu, cid)
            verifier("réimport du même fichier : rien de nouveau, tout ignoré", r2["importes"] == 0 and r2["ignores"] == 5)
            c2 = campagnes.creer(c, {"nom": "C2", "secteur": "s", "pays": "FR", "langue": "fr"})
            r3 = importer_csv(c, b"nom,ville\nCabinet Dupont,Lyon\n", c2)
            verifier("même prospect dans une autre campagne : ignoré et signalé", r3["ignores"] == 1 and r3["autres_campagnes"] == 1)
            # --- Format « liste de leads » : lignes d'avertissement avant les en-têtes, un contact par ligne, statut d'email ---
            c3 = campagnes.creer(c, {"nom": "Leads", "secteur": "rh", "pays": "FR", "langue": "fr"})
            leads = ('"IMPORTANT\nRegarde cette vidéo : https://exemple.fr/v",,,,\r\n'
                     ',,,,\r\n'
                     'name_for_emails,site,phone,email,email.emails_validator.status\r\n'
                     'Cabinet RH Alpha,https://alpha-rh.fr/?utm_source=google,#ERROR!,mauvais@alpha-rh.fr,INVALID\r\n'
                     'Cabinet RH Alpha,https://alpha-rh.fr/?utm_source=google,#ERROR!,contact@alpha-rh.fr,RECEIVING\r\n'
                     'Cabinet RH Alpha,https://alpha-rh.fr/?utm_source=google,05 56 00 00 00,jean@alpha-rh.fr,RECEIVING\r\n'
                     'Beta Recrutement,https://beta-recrutement.fr,,boite@beta-recrutement.fr,BLACKLISTED\r\n'
                     'Gamma Conseil,https://gamma-conseil.fr,,info@gamma-conseil.fr,UNKNOWN\r\n'
                     'Delta Intérim,https://delta-interim.fr,,accueil@delta-interim.fr,receiving\r\n').encode("utf-8")
            r4 = importer_csv(c, leads, c3)
            fiches = {l["nom"]: dict(l) for l in c.execute("SELECT * FROM prospects WHERE campagne_id = ?", (c3,))}
            verifier("leads : en-têtes trouvés après les lignes d'avertissement, 4 établissements pour 6 lignes de contacts",
                     r4["importes"] == 4 and r4["ignores"] == 2 and any("ligne 4" in a for a in r4["avertissements"]))
            verifier("leads : plusieurs contacts d'un même établissement -> une fiche, avec l'email validé (le 1er était INVALID)",
                     fiches["Cabinet RH Alpha"]["email"] == "contact@alpha-rh.fr")
            verifier("leads : téléphone repris d'une autre ligne du même établissement, « #ERROR! » jamais gardé",
                     fiches["Cabinet RH Alpha"]["telephone"] == "05 56 00 00 00" and fiches["Delta Intérim"]["telephone"] is None)
            verifier("leads : emails BLACKLISTED et UNKNOWN non retenus (fiche importée SANS email) ; « receiving » sans casse accepté",
                     fiches["Beta Recrutement"]["email"] is None and fiches["Gamma Conseil"]["email"] is None
                     and fiches["Delta Intérim"]["email"] == "accueil@delta-interim.fr")
            verifier("leads : le compte rendu dit combien d'adresses ont été écartées et pourquoi",
                     r4["emails_ecartes"] == {"INVALID": 1, "BLACKLISTED": 1, "UNKNOWN": 1}
                     and any("non retenue" in a for a in r4["avertissements"]))
            db.set_reglage(c, "import_statuts_email_acceptes", "[]")
            c4 = campagnes.creer(c, {"nom": "Leads2", "secteur": "rh", "pays": "FR", "langue": "fr"})
            r5 = importer_csv(c, leads.replace(b"Alpha", b"Omega").replace(b"alpha", b"omega"), c4)
            verifier("leads : liste de statuts vide = toutes les adresses gardées, aucun écart", r5["emails_ecartes"] == {})
            # --- Export ---
            c.execute("UPDATE prospects SET statut = 'qualifie', problemes = '[\"lent\"]', score = 12 WHERE nom = 'Cabinet Dupont'")
            c.execute("INSERT INTO prospects (campagne_id, nom, nom_normalise, source, email) VALUES (?, '=CMD()', 'cmd', 'import_csv', 'x@y.fr')", (cid,))
            ids_export = [r[0] for r in c.execute("SELECT id FROM prospects ORDER BY id DESC")]
            octets_csv = exporter_csv(c, ids_export, "Europe/Paris", 10)
            texte_csv = octets_csv.decode("utf-8-sig")
            lignes_csv = list(csv.reader(io.StringIO(texte_csv), delimiter=";"))
            verifier("export : BOM UTF-8, séparateur « ; », en-têtes en français, une ligne par prospect dans l'ordre demandé",
                     octets_csv.startswith(b"\xef\xbb\xbf") and lignes_csv[0][0] == "Établissement"
                     and len(lignes_csv) == 1 + len(ids_export) and lignes_csv[1][0].endswith("CMD()"))
            verifier("export : cellule commençant par « = » neutralisée (pas d'exécution de formule dans Excel)",
                     lignes_csv[1][0] == "'=CMD()")
            dupont = next(l for l in lignes_csv if l[0] == "Cabinet Dupont")
            verifier("export : problèmes en clair, score brut 12 et score affiché plafonné à 10, adresse non générique",
                     dupont[7] == "Site lent à charger" and dupont[8] == "12" and dupont[9] == "10" and dupont[6] == "non")
            verifier("export : liste vide = seulement les en-têtes", len(list(csv.reader(io.StringIO(exporter_csv(c, [], "Europe/Paris", 10).decode("utf-8-sig")), delimiter=";"))) == 1)
            for mauvais, attendu in ((b"", "vide"), (b"ville,site\nLyon,x.fr\n", "nom"), (b"x" * (TAILLE_MAX_OCTETS + 1), "volumineux")):
                try:
                    importer_csv(c, mauvais, cid)
                    message = ""
                except ValueError as e:
                    message = str(e)
                verifier(f"fichier inutilisable refusé avec un message clair ({attendu})", attendu in message)
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
    if len(argv) > 2:
        with db.connexion() as conn:
            rapport = importer_csv(conn, Path(argv[1]).read_bytes(), int(argv[2]))
        print({k: v for k, v in rapport.items() if k != "nouveaux_ids"})
        return 0
    print("Usage : python -m core.importer FICHIER.csv ID_CAMPAGNE | check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
