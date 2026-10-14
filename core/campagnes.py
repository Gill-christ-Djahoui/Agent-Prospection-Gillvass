"""Campagnes : création, édition, zones, critères, angle de message, duplication, archivage.

Aucune valeur métier ici : requêtes, zones, poids et prompts viennent de la base.
Utilisable seul : python -m core.campagnes        liste les campagnes
                  python -m core.campagnes check  rejoue les tests sur une base jetable (sans réseau)
"""
import json
import logging
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from core import db, seeds
from core.reglages import ErreurValidation
from core.seeds import copier_angle_pour
from core.writer import VARIABLES_PROMPT

journal = logging.getLogger("prospect")

_RE_PAYS = re.compile(r"^[A-Za-z]{2}$")
_RE_LANGUE = re.compile(r"^[a-z]{2}$")
_RE_VARIABLE = re.compile(r"\{(\w*)\}")


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def _texte_requis(valeur, maximum=200):
    texte = str(valeur or "").strip()
    if not texte:
        raise ValueError("Ce champ ne peut pas être vide.")
    if len(texte) > maximum:
        raise ValueError(f"{maximum} caractères au maximum.")
    return texte


def _code_pays(valeur):
    texte = str(valeur or "").strip()
    if not _RE_PAYS.match(texte):
        raise ValueError("Code pays à 2 lettres attendu (exemple : FR).")
    return texte.upper()


def _code_langue(valeur):
    texte = str(valeur or "").strip().lower()
    if not _RE_LANGUE.match(texte):
        raise ValueError("Code langue à 2 lettres attendu (exemple : fr).")
    return texte


def _liste_requetes(valeur):
    """Une recherche par ligne ; une virgule ou un point-virgule sépare aussi deux recherches
    (« avocat, cabinet d'avocats » = deux recherches, pas une)."""
    elements = valeur.splitlines() if isinstance(valeur, str) else list(valeur or [])
    vues, propres = set(), []
    for element in elements:
        for morceau in re.split(r"[,;]", str(element)):
            texte = " ".join(morceau.split())
            if texte and texte.lower() not in vues:
                vues.add(texte.lower())
                propres.append(texte)
    return propres


def _verifier_variables(texte):
    inconnues = sorted(set(_RE_VARIABLE.findall(texte)) - set(VARIABLES_PROMPT))
    if inconnues:
        raise ValueError(
            "Variable(s) inconnue(s) : " + ", ".join("{" + v + "}" for v in inconnues)
            + ". Autorisées : " + ", ".join("{" + v + "}" for v in VARIABLES_PROMPT) + ".")


def _valider(champs, validateurs):
    """Applique un validateur par champ présent. Lève ErreurValidation avec toutes les erreurs."""
    propres, erreurs = {}, {}
    for cle, brut in champs.items():
        if cle not in validateurs:
            erreurs[cle] = "Champ inconnu."
            continue
        try:
            propres[cle] = validateurs[cle](brut)
        except ValueError as erreur:
            erreurs[cle] = str(erreur)
    if erreurs:
        raise ErreurValidation(erreurs)
    return propres


# --------------------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------------------


def lister(conn, modeles=False):
    """Campagnes (ou modèles de départ) avec leurs compteurs. Archivées en dernier."""
    lignes = conn.execute(
        """SELECT c.id, c.nom, c.secteur, c.statut, c.est_modele,
                  (SELECT COUNT(*) FROM zones z WHERE z.campagne_id = c.id) AS nb_zones,
                  (SELECT COUNT(*) FROM prospects p WHERE p.campagne_id = c.id) AS nb_prospects,
                  (SELECT COUNT(*) FROM envois e
                     JOIN messages m ON m.id = e.message_id
                     JOIN prospects p ON p.id = m.prospect_id
                    WHERE p.campagne_id = c.id AND e.statut <> 'echec') AS nb_envois
             FROM campagnes c WHERE c.est_modele = ?
            ORDER BY (c.statut = 'archivee'), CASE WHEN c.est_modele = 1 THEN c.id ELSE -c.id END""",
        (1 if modeles else 0,))
    return [dict(l) for l in lignes]


def criteres_effectifs(conn, campagne_id):
    """Critères applicables à la campagne : sa version personnalisée, sinon le critère global."""
    lignes = conn.execute(
        """SELECT g.code,
                  COALESCE(c.libelle, g.libelle) AS libelle,
                  COALESCE(c.points, g.points) AS points,
                  COALESCE(c.actif, g.actif) AS actif,
                  g.points AS points_global,
                  (c.id IS NOT NULL) AS personnalise
             FROM criteres g
             LEFT JOIN criteres c ON c.code = g.code AND c.campagne_id = ?
            WHERE g.campagne_id IS NULL
            ORDER BY g.id""", (campagne_id,)).fetchall()
    propres = conn.execute(
        """SELECT c.code, c.libelle, c.points, c.actif, NULL AS points_global, 1 AS personnalise
             FROM criteres c
            WHERE c.campagne_id = ?
              AND c.code NOT IN (SELECT code FROM criteres WHERE campagne_id IS NULL)
            ORDER BY c.id""", (campagne_id,)).fetchall()
    return [dict(l) for l in [*lignes, *propres]]


def lister_modeles_message(conn):
    return [dict(l) for l in conn.execute(
        """SELECT m.*, (SELECT COUNT(*) FROM campagnes c WHERE c.modele_message_id = m.id) AS nb_campagnes
             FROM modeles_message m ORDER BY m.id""")]


def lire(conn, campagne_id):
    """Tout ce qu'il faut pour l'écran d'édition d'une campagne. None si elle n'existe pas."""
    ligne = conn.execute("SELECT * FROM campagnes WHERE id = ?", (campagne_id,)).fetchone()
    if ligne is None:
        return None
    campagne = dict(ligne)
    campagne["requetes"] = json.loads(campagne["requetes"])
    zones = [dict(z) for z in conn.execute(
        """SELECT z.*, (SELECT COUNT(*) FROM prospects p WHERE p.zone_id = z.id) AS nb_prospects
             FROM zones z WHERE z.campagne_id = ? ORDER BY z.id""", (campagne_id,))]
    modeles = lister_modeles_message(conn)
    modele = next((m for m in modeles if m["id"] == campagne["modele_message_id"]), None)
    campagne["nb_prospects"] = conn.execute(
        "SELECT COUNT(*) FROM prospects WHERE campagne_id = ?", (campagne_id,)).fetchone()[0]
    return {"campagne": campagne, "zones": zones, "criteres": criteres_effectifs(conn, campagne_id),
            "modele": modele, "modeles": modeles}


# --------------------------------------------------------------------------------------
# Écriture : campagne
# --------------------------------------------------------------------------------------

_VALIDATEURS_CAMPAGNE = {
    "nom": _texte_requis,
    "secteur": _texte_requis,
    "pays": _code_pays,
    "langue": _code_langue,
    "requetes": _liste_requetes,
}


def creer(conn, champs, modele_depart_id=None):
    """Crée une campagne vierge, ou une copie d'un modèle de départ. Renvoie son id."""
    if modele_depart_id:
        source = conn.execute("SELECT est_modele FROM campagnes WHERE id = ?", (modele_depart_id,)).fetchone()
        if source is None:
            raise ErreurValidation({"modele_depart": "Ce modèle n'existe pas."})
        nom = _valider({"nom": champs.get("nom")}, _VALIDATEURS_CAMPAGNE)["nom"]
        return dupliquer(conn, modele_depart_id, nom=nom)
    obligatoires = {k: champs.get(k) for k in ("nom", "secteur", "pays", "langue")}
    propres = _valider(obligatoires, _VALIDATEURS_CAMPAGNE)
    curseur = conn.execute(
        "INSERT INTO campagnes (nom, secteur, pays, langue, requetes, statut, est_modele) "
        "VALUES (?, ?, ?, ?, '[]', 'brouillon', 0)",
        (propres["nom"], propres["secteur"], propres["pays"], propres["langue"]))
    copier_angle_pour(conn, curseur.lastrowid)  # une campagne neuve a toujours un angle : jamais « aucun angle rattaché »
    journal.info("campagne créée : id=%s", curseur.lastrowid)
    return curseur.lastrowid


def modifier(conn, campagne_id, champs):
    """Met à jour les champs fournis (nom, secteur, pays, langue, requetes, modele_message_id)."""
    champs = dict(champs)
    modele_id = champs.pop("modele_message_id", "absent")
    propres = _valider(champs, _VALIDATEURS_CAMPAGNE)
    if "requetes" in propres:
        propres["requetes"] = json.dumps(propres["requetes"], ensure_ascii=False)
    if modele_id != "absent":
        if modele_id in (None, ""):
            propres["modele_message_id"] = None
        else:
            if conn.execute("SELECT 1 FROM modeles_message WHERE id = ?", (modele_id,)).fetchone() is None:
                raise ErreurValidation({"modele_message_id": "Cet angle de message n'existe pas."})
            propres["modele_message_id"] = int(modele_id)
    if not propres:
        return
    colonnes = ", ".join(f"{c} = ?" for c in propres)
    conn.execute(f"UPDATE campagnes SET {colonnes} WHERE id = ?", (*propres.values(), campagne_id))


def archiver(conn, campagne_id, archiver=True):
    """Archive une campagne, ou la remet en brouillon. Les prospects sont conservés."""
    conn.execute("UPDATE campagnes SET statut = ? WHERE id = ?",
                 ("archivee" if archiver else "brouillon", campagne_id))
    journal.info("campagne %s : %s", campagne_id, "archivée" if archiver else "désarchivée")


def dupliquer(conn, source_id, nom=None, est_modele=False):
    """Copie une campagne : requêtes, zones (remises à zéro), critères personnalisés, angle.

    L'angle de message est copié lui aussi : modifier le prompt de la copie n'altère pas l'original.
    """
    source = conn.execute("SELECT * FROM campagnes WHERE id = ?", (source_id,)).fetchone()
    if source is None:
        raise ErreurValidation({"campagne": "Campagne introuvable."})
    nouveau_modele = None
    if source["modele_message_id"]:
        m = conn.execute("SELECT * FROM modeles_message WHERE id = ?", (source["modele_message_id"],)).fetchone()
        nouveau_modele = conn.execute(
            "INSERT INTO modeles_message (nom, objet_template, prompt_systeme, signature) VALUES (?, ?, ?, ?)",
            (f"{m['nom']} (copie)", m["objet_template"], m["prompt_systeme"], m["signature"])).lastrowid
    curseur = conn.execute(
        "INSERT INTO campagnes (nom, secteur, requetes, pays, langue, modele_message_id, statut, est_modele) "
        "VALUES (?, ?, ?, ?, ?, ?, 'brouillon', ?)",
        (nom or f"{source['nom']} (copie)", source["secteur"], source["requetes"], source["pays"],
         source["langue"], nouveau_modele, 1 if est_modele else 0))
    nouvelle_id = curseur.lastrowid
    conn.execute(
        "INSERT INTO zones (campagne_id, ville, pays, rayon_km, lat, lon) "
        "SELECT ?, ville, pays, rayon_km, lat, lon FROM zones WHERE campagne_id = ? ORDER BY id",
        (nouvelle_id, source_id))
    conn.execute(
        "INSERT INTO criteres (campagne_id, code, libelle, points, actif) "
        "SELECT ?, code, libelle, points, actif FROM criteres WHERE campagne_id = ?",
        (nouvelle_id, source_id))
    journal.info("campagne %s dupliquée -> %s", source_id, nouvelle_id)
    return nouvelle_id


class ErreurEnvoisExistants(ErreurValidation):
    """La campagne a déjà envoyé des emails : la supprimer efface aussi cet historique. Confirmation explicite requise."""

    def __init__(self, nb_envois):
        super().__init__({"campagne": f"Cette campagne a envoyé {nb_envois} email(s). Les supprimer efface l'historique d'envoi : "
                                      "le quota du jour, les relances et la protection contre un second contact "
                                      "n'en tiendront plus compte."})
        self.nb_envois = nb_envois


def supprimer(conn, campagne_id, forcer=False):
    """Supprime une campagne (ou un modèle de départ) avec ses zones, prospects, messages et critères.

    - Refusée tant qu'un scan, une analyse ou une rédaction tourne sur cette campagne.
    - Si des emails ont déjà été envoyés, lève ErreurEnvoisExistants tant que `forcer` n'est pas vrai : supprimer
      l'historique d'envoi a des conséquences (quota du jour, relances, anti-doublon), l'utilisateur doit les confirmer.
    - La liste noire n'est jamais touchée : un désinscrit le reste.
    Renvoie le décompte de ce qui a été supprimé.
    """
    ligne = conn.execute("SELECT modele_message_id FROM campagnes WHERE id = ?", (campagne_id,)).fetchone()
    if ligne is None:
        raise ErreurValidation({"campagne": "Campagne introuvable."})
    if conn.execute("SELECT 1 FROM jobs WHERE campagne_id = ? AND statut IN ('en_attente', 'en_cours') LIMIT 1",
                    (campagne_id,)).fetchone():
        raise ErreurValidation({"campagne": "Un scan, une analyse ou une rédaction est en cours sur cette campagne : "
                                            "attends sa fin avant de la supprimer."})
    de_la_campagne = "(SELECT id FROM prospects WHERE campagne_id = ?)"
    envois = conn.execute(
        f"SELECT COUNT(*) FROM envois e JOIN messages m ON m.id = e.message_id WHERE m.prospect_id IN {de_la_campagne}"
        " AND e.statut <> 'echec'", (campagne_id,)).fetchone()[0]
    if envois and not forcer:
        raise ErreurEnvoisExistants(envois)
    compte = {"envois": conn.execute(
        f"DELETE FROM envois WHERE message_id IN (SELECT id FROM messages WHERE prospect_id IN {de_la_campagne})",
        (campagne_id,)).rowcount}
    for journal_du_prospect in ("appels", "contacts_formulaire", "contacts_social", "reponses"):  # les journaux de contact partent avec leurs prospects
        conn.execute(f"DELETE FROM {journal_du_prospect} WHERE prospect_id IN {de_la_campagne}", (campagne_id,))
    compte["messages"] = conn.execute(
        f"DELETE FROM messages WHERE prospect_id IN {de_la_campagne}", (campagne_id,)).rowcount
    compte["prospects"] = conn.execute("DELETE FROM prospects WHERE campagne_id = ?", (campagne_id,)).rowcount
    compte["zones"] = conn.execute("DELETE FROM zones WHERE campagne_id = ?", (campagne_id,)).rowcount
    conn.execute("DELETE FROM criteres WHERE campagne_id = ?", (campagne_id,))
    conn.execute("DELETE FROM jobs WHERE campagne_id = ?", (campagne_id,))
    conn.execute("DELETE FROM campagnes WHERE id = ?", (campagne_id,))
    modele = ligne["modele_message_id"]
    if modele and not conn.execute("SELECT 1 FROM campagnes WHERE modele_message_id = ?", (modele,)).fetchone():
        conn.execute("DELETE FROM modeles_message WHERE id = ?", (modele,))  # l'angle copié pour elle ne sert plus à personne
    journal.info("campagne %s supprimée : %s prospect(s), %s message(s), %s envoi(s), %s zone(s)",
                 campagne_id, compte["prospects"], compte["messages"], compte["envois"], compte["zones"])
    return compte


def supprimer_prospects(conn, ids):
    """Supprime définitivement des prospects, avec leurs messages et leurs journaux de contact (appels, formulaires, messages
    privés, réponses). Ne touche jamais :
    - un prospect déjà contacté par email (envoi réel) ou ayant répondu : son historique sert au quota du jour, aux relances et à
      l'anti-doublon ;
    - un prospect dont la campagne a un scan, une analyse ou une rédaction en cours ;
    - la liste noire.
    Un prospect supprimé n'est plus connu : un nouveau scan peut le retrouver. Pour l'écarter sans l'oublier, il faut l'ignorer.
    Renvoie {'supprimes', 'refuses': {'contactes': n, 'tache_en_cours': n, 'introuvables': n}}."""
    ids = sorted({i for i in ids if isinstance(i, int) and not isinstance(i, bool)})
    refuses = {"contactes": 0, "tache_en_cours": 0, "introuvables": 0}
    a_supprimer = []
    for prospect_id in ids:
        ligne = conn.execute("SELECT campagne_id, statut FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
        if ligne is None:
            refuses["introuvables"] += 1
        elif ligne["statut"] in ("envoye", "repondu") or conn.execute(
                "SELECT 1 FROM envois e JOIN messages m ON m.id = e.message_id WHERE m.prospect_id = ? AND e.statut <> 'echec' LIMIT 1",
                (prospect_id,)).fetchone():
            refuses["contactes"] += 1
        elif conn.execute("SELECT 1 FROM jobs WHERE campagne_id = ? AND statut IN ('en_attente', 'en_cours') LIMIT 1",
                          (ligne["campagne_id"],)).fetchone():
            refuses["tache_en_cours"] += 1
        else:
            a_supprimer.append(prospect_id)
    for prospect_id in a_supprimer:
        conn.execute("DELETE FROM envois WHERE message_id IN (SELECT id FROM messages WHERE prospect_id = ?)", (prospect_id,))  # échecs
        for journal_du_prospect in ("appels", "contacts_formulaire", "contacts_social", "reponses", "messages"):
            conn.execute(f"DELETE FROM {journal_du_prospect} WHERE prospect_id = ?", (prospect_id,))
        conn.execute("DELETE FROM prospects WHERE id = ?", (prospect_id,))
    if a_supprimer:
        journal.info("%d prospect(s) supprimé(s) à la main", len(a_supprimer))
    return {"supprimes": len(a_supprimer), "refuses": refuses}


# --------------------------------------------------------------------------------------
# Écriture : zones
# --------------------------------------------------------------------------------------


def ajouter_zone(conn, campagne_id, ville, pays, rayon_km=None, geocoder=None):
    """Ajoute une zone à la campagne. `geocoder(ville, pays)` fournit les coordonnées (Nominatim).

    Une ville déjà présente est refusée AVANT tout appel réseau. Si la ville est introuvable
    (ou le géocodage en erreur), la zone est quand même créée avec lat/lon NULL : elle est alors
    signalée et non scannable tant que les coordonnées ne sont pas corrigées.
    Renvoie {'zone_id', 'geocodage': {...}}.
    """
    erreurs = {}
    try:
        ville = _texte_requis(ville, 100)
    except ValueError as e:
        erreurs["ville"] = str(e)
    try:
        pays = _code_pays(pays)
    except ValueError as e:
        erreurs["pays"] = str(e)
    rayon = None
    if rayon_km not in (None, ""):
        try:
            rayon = int(rayon_km)
            if not 1 <= rayon <= 200:
                raise ValueError
        except ValueError:
            erreurs["rayon_km"] = "Rayon entier compris entre 1 et 200 km."
    if erreurs:
        raise ErreurValidation(erreurs)
    if conn.execute(
        "SELECT 1 FROM zones WHERE campagne_id = ? AND lower(trim(ville)) = lower(trim(?)) "
        "AND upper(trim(pays)) = upper(trim(?))", (campagne_id, ville, pays)).fetchone():
        raise ErreurValidation({"ville": f"{ville} est déjà dans cette campagne."})

    resultat = geocoder(ville, pays) if geocoder else {"statut": "erreur", "message": "Géocodage non demandé."}
    lat = resultat.get("lat") if resultat["statut"] == "ok" else None
    lon = resultat.get("lon") if resultat["statut"] == "ok" else None
    colonnes, valeurs = ["campagne_id", "ville", "pays", "lat", "lon"], [campagne_id, ville, pays, lat, lon]
    if rayon is not None:
        colonnes.append("rayon_km")
        valeurs.append(rayon)
    curseur = conn.execute(
        f"INSERT INTO zones ({', '.join(colonnes)}) VALUES ({', '.join('?' * len(valeurs))})", valeurs)
    return {"zone_id": curseur.lastrowid, "geocodage": resultat}


def definir_coordonnees(conn, zone_id, lat, lon):
    """Saisie manuelle des coordonnées d'une zone (quand la ville est introuvable)."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ErreurValidation({"coordonnees": "Latitude et longitude numériques attendues."}) from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ErreurValidation({"coordonnees": "Latitude entre -90 et 90, longitude entre -180 et 180."})
    conn.execute("UPDATE zones SET lat = ?, lon = ? WHERE id = ?", (lat, lon, zone_id))


def supprimer_zone(conn, zone_id):
    """Supprime une zone qui n'a encore aucun prospect (sinon l'historique serait cassé)."""
    if conn.execute("SELECT 1 FROM prospects WHERE zone_id = ? LIMIT 1", (zone_id,)).fetchone():
        raise ErreurValidation({"zone": "Cette zone a déjà des prospects : elle ne peut pas être supprimée."})
    conn.execute("DELETE FROM zones WHERE id = ?", (zone_id,))


# --------------------------------------------------------------------------------------
# Écriture : critères et angle de message
# --------------------------------------------------------------------------------------


def definir_critere(conn, campagne_id, code, points, actif):
    """Poids et activation d'un critère pour CETTE campagne (crée sa version personnalisée)."""
    effectif = next((c for c in criteres_effectifs(conn, campagne_id) if c["code"] == code), None)
    if effectif is None:
        raise ErreurValidation({"code": "Critère inconnu."})
    try:
        points = int(points)
        if not 0 <= points <= 100:
            raise ValueError
    except (TypeError, ValueError):
        raise ErreurValidation({"points": "Nombre entier compris entre 0 et 100."}) from None
    actif = 1 if actif in (True, 1, "1", "true") else 0
    existe = conn.execute("SELECT id FROM criteres WHERE campagne_id = ? AND code = ?",
                          (campagne_id, code)).fetchone()
    if existe:
        conn.execute("UPDATE criteres SET points = ?, actif = ? WHERE id = ?", (points, actif, existe["id"]))
    else:
        conn.execute("INSERT INTO criteres (campagne_id, code, libelle, points, actif) VALUES (?, ?, ?, ?, ?)",
                     (campagne_id, code, effectif["libelle"], points, actif))


_VALIDATEURS_MODELE = {
    "nom": _texte_requis,
    "objet_template": lambda v: _texte_requis(v, 300),
    "prompt_systeme": lambda v: _texte_requis(v, 20000),
    "signature": lambda v: str(v or "").strip(),
}


def modifier_modele(conn, modele_id, champs):
    """Met à jour un angle de message. Les variables {…} inconnues sont refusées d'emblée."""
    propres = _valider(champs, _VALIDATEURS_MODELE)
    erreurs = {}
    for cle in ("objet_template", "prompt_systeme", "signature"):
        if cle in propres:
            try:
                _verifier_variables(propres[cle])
            except ValueError as e:
                erreurs[cle] = str(e)
    if erreurs:
        raise ErreurValidation(erreurs)
    if not propres:
        return
    colonnes = ", ".join(f"{c} = ?" for c in propres)
    conn.execute(f"UPDATE modeles_message SET {colonnes} WHERE id = ?", (*propres.values(), modele_id))


def nouvel_angle(conn, campagne_id, depuis_modele_id=None):
    """Crée un nouvel angle (copie de celui indiqué) et le rattache à la campagne. Renvoie son id."""
    if depuis_modele_id is None:
        ligne = conn.execute("SELECT modele_message_id FROM campagnes WHERE id = ?", (campagne_id,)).fetchone()
        depuis_modele_id = ligne["modele_message_id"] if ligne else None
    if depuis_modele_id is None:
        raise ErreurValidation({"modele": "Aucun angle à copier : choisis-en un dans la liste."})
    m = conn.execute("SELECT * FROM modeles_message WHERE id = ?", (depuis_modele_id,)).fetchone()
    if m is None:
        raise ErreurValidation({"modele": "Angle introuvable."})
    nouveau = conn.execute(
        "INSERT INTO modeles_message (nom, objet_template, prompt_systeme, signature) VALUES (?, ?, ?, ?)",
        (f"{m['nom']} (copie)", m["objet_template"], m["prompt_systeme"], m["signature"])).lastrowid
    conn.execute("UPDATE campagnes SET modele_message_id = ? WHERE id = ?", (nouveau, campagne_id))
    return nouveau


# --------------------------------------------------------------------------------------
# Auto-tests (python -m core.campagnes check) : base jetable, aucun appel réseau
# --------------------------------------------------------------------------------------


def _leve(fonction, *args, **kwargs):
    try:
        fonction(*args, **kwargs)
    except ErreurValidation as erreur:
        return erreur.erreurs
    return None


def _autotests():
    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    faux_geocodeur = {"lyon": (45.76, 4.84), "marseille": (43.30, 5.37), "toulouse": (43.60, 1.44),
                      "montpellier": (43.61, 3.88), "nice": (43.70, 7.26)}

    def geocoder(ville, pays):
        coords = faux_geocodeur.get(ville.lower())
        return {"statut": "ok", "lat": coords[0], "lon": coords[1]} if coords else {"statut": "introuvable"}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        chemin = Path(tmp) / "t.db"
        db.init_db(chemin, importer_env=False)
        with db.connexion(chemin) as c:
            modeles = lister(c, modeles=True)
            verifier("3 modèles de départ listés, 0 campagne", len(modeles) == 3 and lister(c) == [])

            # --- Création et validation ---
            cid = creer(c, {"nom": " Cliniques — France Sud ", "secteur": "clinique", "pays": "fr", "langue": "FR"})
            fiche = lire(c, cid)["campagne"]
            verifier("création : valeurs normalisées (pays FR, langue fr, nom rogné)",
                     fiche["pays"] == "FR" and fiche["langue"] == "fr" and fiche["nom"] == "Cliniques — France Sud")
            modele_neuve = lire(c, cid)["modele"]
            catalogue = [m["nom"] for m in lister_modeles_message(c)]
            verifier("catalogue d'angles installé (tous les métiers et tous les problèmes), sans doublon de nom",
                     all(nom in catalogue for nom, *_ in seeds.ANGLES_CATALOGUE) and len(catalogue) == len(set(catalogue))
                     and len(seeds.ANGLES_CATALOGUE) >= 15)
            verifier("une campagne neuve a TOUJOURS un angle (copie de l'angle par défaut, indépendante) : plus de « aucun angle rattaché »",
                     modele_neuve is not None and modele_neuve["nom"].startswith(seeds.ANGLE_PAR_DEFAUT)
                     and "Entre 45 et 70 mots" in modele_neuve["prompt_systeme"] and modele_neuve["nb_campagnes"] == 1)
            c.execute("UPDATE campagnes SET modele_message_id = NULL WHERE id = ?", (cid,))
            c.execute("UPDATE reglages SET valeur = '0' WHERE cle = 'angles_catalogue_version'")
            seeds.appliquer_seeds(c)
            verifier("mise à jour : une campagne qui n'avait aucun angle en reçoit un, sans toucher aux autres campagnes",
                     lire(c, cid)["modele"] is not None
                     and c.execute("SELECT COUNT(*) FROM campagnes WHERE modele_message_id IS NULL AND est_modele = 0").fetchone()[0] == 0)
            verifier("une virgule sépare deux recherches : « avocat, cabinet d'avocats » = 2 recherches",
                     _liste_requetes("avocat, cabinet d'avocats\nArchitecte; avocat") == ["avocat", "cabinet d'avocats", "Architecte"])
            verifier("création invalide refusée avec le détail par champ",
                     set(_leve(creer, c, {"nom": "", "secteur": "x", "pays": "FRA", "langue": "f"}) or {})
                     == {"nom", "pays", "langue"})

            # --- Requêtes ---
            modifier(c, cid, {"requetes": ["clinique esthétique", "Clinique Esthétique", " médecine  esthétique ", ""]})
            verifier("requêtes : doublons (casse) et vides retirés",
                     lire(c, cid)["campagne"]["requetes"] == ["clinique esthétique", "médecine esthétique"])

            # --- Cinq zones ---
            for ville in ["Lyon", "Marseille", "Toulouse", "Montpellier", "Nice"]:
                ajouter_zone(c, cid, ville, "FR", None, geocoder)
            zones = lire(c, cid)["zones"]
            verifier("5 zones créées, toutes géocodées, état a_scanner, rayon par défaut 15",
                     len(zones) == 5 and all(z["lat"] is not None and z["statut"] == "a_scanner"
                                             and z["rayon_km"] == 15 for z in zones))
            verifier("zone en double refusée (casse/espaces), sans appel au géocodeur",
                     _leve(ajouter_zone, c, cid, " lyon ", "fr", None, lambda *a: 1 / 0) is not None)
            r = ajouter_zone(c, cid, "Villeinventée", "FR", 20, geocoder)
            zone_perdue = r["zone_id"]
            ligne = c.execute("SELECT lat, lon, rayon_km FROM zones WHERE id = ?", (zone_perdue,)).fetchone()
            verifier("ville introuvable : zone créée avec lat/lon NULL, signalée introuvable",
                     r["geocodage"]["statut"] == "introuvable" and ligne["lat"] is None and ligne["rayon_km"] == 20)
            definir_coordonnees(c, zone_perdue, 45.0, 5.0)
            verifier("coordonnées saisies à la main", c.execute("SELECT lat FROM zones WHERE id = ?", (zone_perdue,)).fetchone()[0] == 45.0)
            verifier("coordonnées hors limites refusées", _leve(definir_coordonnees, c, zone_perdue, 120, 5) is not None)
            supprimer_zone(c, zone_perdue)
            verifier("zone sans prospect supprimable", len(lire(c, cid)["zones"]) == 5)
            c.execute("INSERT INTO prospects (campagne_id, zone_id, nom, nom_normalise, source) "
                      "VALUES (?, ?, 'P', 'p', 'import_csv')", (cid, zones[0]["id"]))
            verifier("zone avec prospects non supprimable", _leve(supprimer_zone, c, zones[0]["id"]) is not None)

            # --- Critères ---
            avant = {x["code"]: x for x in criteres_effectifs(c, cid)}
            verifier("13 critères hérités des globaux, aucun personnalisé",
                     len(avant) == 13 and not any(x["personnalise"] for x in avant.values()))
            definir_critere(c, cid, "pas_rdv", 0, True)
            definir_critere(c, cid, "lent", 5, False)
            apres = {x["code"]: x for x in criteres_effectifs(c, cid)}
            verifier("poids personnalisé pour la campagne (pas_rdv=0, lent=5 inactif)",
                     apres["pas_rdv"]["points"] == 0 and apres["lent"]["points"] == 5
                     and apres["lent"]["actif"] == 0 and apres["pas_rdv"]["personnalise"] == 1)
            verifier("les critères globaux ne bougent pas",
                     c.execute("SELECT points FROM criteres WHERE campagne_id IS NULL AND code = 'pas_rdv'").fetchone()[0] == 3)
            verifier("poids invalide refusé", _leve(definir_critere, c, cid, "lent", "beaucoup", True) is not None
                     and _leve(definir_critere, c, cid, "inconnu", 1, True) is not None)

            # --- Angle de message ---
            modele_depart_angle = c.execute(
                "SELECT modele_message_id FROM campagnes WHERE est_modele = 1 ORDER BY id").fetchone()[0]
            ang = nouvel_angle(c, cid, modele_depart_angle)
            modifier_modele(c, ang, {"prompt_systeme": "Écris à {nom} à {ville}. Problèmes : {problemes}."})
            verifier("angle copié, rattaché, modifiable",
                     lire(c, cid)["campagne"]["modele_message_id"] == ang)
            erreurs = _leve(modifier_modele, c, ang, {"prompt_systeme": "Bonjour {prenom}, {nom}"})
            verifier("variable inconnue {prenom} refusée avec la liste autorisée",
                     erreurs is not None and "{prenom}" in erreurs["prompt_systeme"] and "{nom}" in erreurs["prompt_systeme"])
            verifier("l'angle du modèle de départ n'est pas altéré",
                     "{prenom}" not in c.execute("SELECT prompt_systeme FROM modeles_message ORDER BY id LIMIT 1").fetchone()[0])

            # --- Duplication ---
            copie = dupliquer(c, cid)
            fc = lire(c, copie)
            verifier("duplication : requêtes, 5 zones remises à zéro, critères personnalisés copiés",
                     fc["campagne"]["requetes"] == ["clinique esthétique", "médecine esthétique"]
                     and len(fc["zones"]) == 5 and all(z["statut"] == "a_scanner" and z["nb_trouves"] == 0 for z in fc["zones"])
                     and {x["code"]: x["points"] for x in fc["criteres"]}["pas_rdv"] == 0
                     and fc["campagne"]["statut"] == "brouillon")
            verifier("duplication : angle copié (indépendant de l'original)",
                     fc["campagne"]["modele_message_id"] not in (None, ang))
            verifier("duplication : aucun prospect copié", fc["campagne"]["nb_prospects"] == 0)

            # --- Archivage ---
            archiver(c, cid)
            verifier("archivage : statut archivee, prospects conservés",
                     lire(c, cid)["campagne"]["statut"] == "archivee" and lire(c, cid)["campagne"]["nb_prospects"] == 1)
            verifier("campagne archivée listée en dernier", lister(c)[-1]["id"] == cid)
            archiver(c, cid, False)

            # --- Depuis un modèle, suppression d'un modèle ---
            mid = c.execute("SELECT id FROM campagnes WHERE est_modele = 1 ORDER BY id").fetchone()[0]
            neuve = creer(c, {"nom": "Dentistes Lyon"}, modele_depart_id=mid)
            verifier("création depuis un modèle : requêtes et angle repris, est_modele = 0",
                     lire(c, neuve)["campagne"]["est_modele"] == 0 and lire(c, neuve)["campagne"]["requetes"] != []
                     and lire(c, neuve)["modele"] is not None)
            angles_avant = c.execute("SELECT COUNT(*) FROM modeles_message").fetchone()[0]
            supprimer(c, mid)
            verifier("un modèle de départ se supprime (et son angle orphelin avec lui)",
                     len(lister(c, modeles=True)) == 2
                     and c.execute("SELECT COUNT(*) FROM modeles_message").fetchone()[0] == angles_avant - 1)
            verifier("compteurs de la liste : 5 zones, 1 prospect, 0 envoi",
                     {x["id"]: (x["nb_zones"], x["nb_prospects"], x["nb_envois"]) for x in lister(c)}[cid] == (5, 1, 0))

            # --- Suppression d'une campagne ---
            verifier("campagne inexistante : refusée", _leve(supprimer, c, 99999) is not None)
            compte = supprimer(c, neuve)
            verifier("campagne vide supprimée avec ses zones, critères et son angle copié",
                     neuve not in {x["id"] for x in lister(c)} and compte["prospects"] == 0
                     and c.execute("SELECT COUNT(*) FROM zones WHERE campagne_id = ?", (neuve,)).fetchone()[0] == 0)

            # Une campagne avec prospects, messages et un envoi réel (+ un envoi en échec, sans conséquence)
            envoyee = creer(c, {"nom": "Avec envoi", "secteur": "s", "pays": "FR", "langue": "fr"})
            ajouter_zone(c, envoyee, "Lyon", "FR", None, geocoder)
            definir_critere(c, envoyee, "lent", 5, True)
            for nom, email in (("Cabinet A", "a@cabinet-a.fr"), ("Cabinet B", "b@cabinet-b.fr")):
                db.inserer_prospect(c, {"campagne_id": envoyee, "nom": nom, "email": email, "ville": "Lyon",
                                        "source": "openstreetmap", "source_id": "osm:" + nom})
            pa, pb = [r[0] for r in c.execute("SELECT id FROM prospects WHERE campagne_id = ? ORDER BY id", (envoyee,))]
            ma = c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'O', 'C', 1)", (pa,)).lastrowid
            mb = c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'O', 'C', 1)", (pb,)).lastrowid
            c.execute("INSERT INTO envois (message_id, statut, brevo_id) VALUES (?, 'envoye', '<x@brevo>')", (ma,))
            c.execute("INSERT INTO envois (message_id, statut, erreur) VALUES (?, 'echec', 'refus')", (mb,))
            db.ajouter_liste_noire(c, "desinscrit@exemple.fr", "desinscription")
            try:
                supprimer(c, envoyee)
                refus = None
            except ErreurEnvoisExistants as e:
                refus = e.nb_envois
            verifier("campagne avec un envoi réel : refusée sans confirmation (l'envoi en échec ne compte pas), rien n'est supprimé",
                     refus == 1 and envoyee in {x["id"] for x in lister(c)}
                     and c.execute("SELECT COUNT(*) FROM prospects WHERE campagne_id = ?", (envoyee,)).fetchone()[0] == 2)
            c.execute("INSERT INTO jobs (type, campagne_id, statut) VALUES ('scan', ?, 'en_cours')", (envoyee,))
            verifier("même confirmée, suppression refusée pendant qu'une tâche tourne sur la campagne",
                     "en cours" in (_leve(supprimer, c, envoyee, True) or {}).get("campagne", ""))
            c.execute("UPDATE jobs SET statut = 'termine' WHERE campagne_id = ?", (envoyee,))
            compte = supprimer(c, envoyee, forcer=True)
            verifier("suppression confirmée : prospects, messages, envois, zones, critères et tâches de la campagne disparaissent",
                     compte == {"envois": 2, "messages": 2, "prospects": 2, "zones": 1}
                     and all(c.execute(f"SELECT COUNT(*) FROM {t} WHERE campagne_id = ?", (envoyee,)).fetchone()[0] == 0
                             for t in ("prospects", "zones", "criteres", "jobs"))
                     and c.execute("SELECT COUNT(*) FROM envois").fetchone()[0] == 0)
            verifier("la liste noire n'est jamais touchée par une suppression",
                     db.est_en_liste_noire(c, "desinscrit@exemple.fr"))

            # --- Suppression de prospects, un par un ou en groupe ---
            lot = creer(c, {"nom": "Lot", "secteur": "s", "pays": "FR", "langue": "fr"})
            for nom in ("P libre", "P echec", "P envoye", "P repondu", "P journal"):
                db.inserer_prospect(c, {"campagne_id": lot, "nom": nom, "email": nom.replace(" ", "") + "@x.fr", "ville": "Lyon",
                                        "source": "openstreetmap", "source_id": "osm:" + nom})
            pid = {r["nom"]: r["id"] for r in c.execute("SELECT id, nom FROM prospects WHERE campagne_id = ?", (lot,))}
            for nom in ("P echec", "P envoye"):
                m = c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'O', 'C', 1)", (pid[nom],)).lastrowid
                c.execute("INSERT INTO envois (message_id, statut, brevo_id, erreur) VALUES (?, ?, 'b', 'e')",
                          (m, "echec" if nom == "P echec" else "envoye"))
            c.execute("UPDATE prospects SET statut = 'repondu' WHERE id = ?", (pid["P repondu"],))
            c.execute("INSERT INTO contacts_social (prospect_id, resultat) VALUES (?, 'envoye')", (pid["P journal"],))
            c.execute("INSERT INTO appels (prospect_id, resultat) VALUES (?, 'pas_de_reponse')", (pid["P journal"],))
            c.execute("INSERT INTO contacts_formulaire (prospect_id, resultat) VALUES (?, 'envoye')", (pid["P journal"],))
            r = supprimer_prospects(c, [pid["P envoye"], pid["P repondu"], 99999, "x", True])
            verifier("suppression de prospects : un prospect déjà contacté par email ou ayant répondu est refusé, inconnu compté à part, rien supprimé",
                     r == {"supprimes": 0, "refuses": {"contactes": 2, "tache_en_cours": 0, "introuvables": 1}}
                     and c.execute("SELECT COUNT(*) FROM prospects WHERE campagne_id = ?", (lot,)).fetchone()[0] == 5)
            c.execute("INSERT INTO jobs (type, campagne_id, statut) VALUES ('scan', ?, 'en_cours')", (lot,))
            verifier("suppression de prospects : refusée pendant qu'une tâche tourne sur leur campagne",
                     supprimer_prospects(c, [pid["P libre"]])["refuses"]["tache_en_cours"] == 1
                     and c.execute("SELECT COUNT(*) FROM prospects WHERE id = ?", (pid["P libre"],)).fetchone()[0] == 1)
            c.execute("UPDATE jobs SET statut = 'termine' WHERE campagne_id = ?", (lot,))
            r = supprimer_prospects(c, [pid["P libre"], pid["P echec"], pid["P journal"], pid["P envoye"]])
            verifier("suppression en groupe : les libres partent avec messages, envoi en échec et journaux ; l'envoyé reste",
                     r == {"supprimes": 3, "refuses": {"contactes": 1, "tache_en_cours": 0, "introuvables": 0}}
                     and {x[0] for x in c.execute("SELECT nom FROM prospects WHERE campagne_id = ?", (lot,))} == {"P envoye", "P repondu"}
                     and c.execute("SELECT COUNT(*) FROM messages WHERE prospect_id IN (?, ?)", (pid["P echec"], pid["P libre"])).fetchone()[0] == 0
                     and all(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0 for t in ("appels", "contacts_social", "contacts_formulaire"))
                     and c.execute("SELECT COUNT(*) FROM envois WHERE statut = 'echec'").fetchone()[0] == 0
                     and c.execute("PRAGMA foreign_key_check").fetchall() == [] and db.est_en_liste_noire(c, "desinscrit@exemple.fr"))
            verifier("aucune ligne orpheline après suppression (clés étrangères cohérentes)",
                     c.execute("PRAGMA foreign_key_check").fetchall() == []
                     and cid in {x["id"] for x in lister(c)})
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
        for titre, modeles in (("Campagnes", False), ("Modèles de départ", True)):
            print(f"== {titre}")
            for c in lister(conn, modeles):
                print(f"  #{c['id']} {c['nom']} [{c['statut']}] zones={c['nb_zones']} "
                      f"prospects={c['nb_prospects']} envois={c['nb_envois']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
