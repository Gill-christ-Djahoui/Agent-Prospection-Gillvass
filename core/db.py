"""Schéma SQLite, migrations, accès et auto-vérification.

Utilisable seul :
    python -m core.db init     crée / met à jour la base, applique les seeds
    python -m core.db check    inspecte la base réelle puis rejoue les tests sur une base jetable

Règles portées par ce module :
- une connexion par appel (les threads de jobs ne partagent rien) ;
- les fonctions SQL personnalisées sont enregistrées dans get_conn(), donc sur CHAQUE connexion ;
- emails normalisés (minuscules, sans espaces) avant stockage et avant comparaison ;
- une insertion en doublon n'est jamais une exception : ON CONFLICT DO NOTHING + compteur.
"""
import json
import logging
import re
import sqlite3
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import config

# Correspondance variable du .env -> clé de la table reglages (import unique au premier lancement)
ENV_VERS_REGLAGES = {
    "GOOGLE_PLACES_API_KEY": "google_places_key",
    "AI_ENDPOINT": "ai_endpoint",
    "AI_MODEL": "ai_model",
    "AI_API_KEY": "ai_key",
    "BREVO_API_KEY": "brevo_key",
    "SENDER_NAME": "expediteur_nom",
    "SENDER_EMAIL": "expediteur_email",
    "REPLY_TO_EMAIL": "reponse_email",
}

TABLES_ATTENDUES = {
    "reglages", "modeles_message", "campagnes", "zones", "criteres",
    "prospects", "messages", "envois", "liste_noire", "jobs", "appels", "contacts_formulaire",
}

# --------------------------------------------------------------------------------------
# Schéma (migration 1)
# --------------------------------------------------------------------------------------

_NOW = "(strftime('%Y-%m-%dT%H:%M:%SZ','now'))"  # dates stockées en UTC

_MIGRATION_1 = f"""
BEGIN;

CREATE TABLE reglages (
    cle     TEXT PRIMARY KEY,
    valeur  TEXT,
    secret  INTEGER NOT NULL DEFAULT 0 CHECK (secret IN (0, 1)),
    maj_le  TEXT NOT NULL DEFAULT {_NOW}
);

CREATE TABLE modeles_message (
    id              INTEGER PRIMARY KEY,
    nom             TEXT NOT NULL,
    objet_template  TEXT NOT NULL,
    prompt_systeme  TEXT NOT NULL,
    signature       TEXT NOT NULL DEFAULT '',
    cree_le         TEXT NOT NULL DEFAULT {_NOW}
);

CREATE TABLE campagnes (
    id                 INTEGER PRIMARY KEY,
    nom                TEXT NOT NULL,
    secteur            TEXT NOT NULL,
    requetes           TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(requetes)),
    pays               TEXT NOT NULL,
    langue             TEXT NOT NULL,
    modele_message_id  INTEGER REFERENCES modeles_message(id),
    statut             TEXT NOT NULL DEFAULT 'brouillon'
                       CHECK (statut IN ('brouillon', 'active', 'archivee')),
    est_modele         INTEGER NOT NULL DEFAULT 0 CHECK (est_modele IN (0, 1)),
    cree_le            TEXT NOT NULL DEFAULT {_NOW}
);

CREATE TABLE zones (
    id           INTEGER PRIMARY KEY,
    campagne_id  INTEGER NOT NULL REFERENCES campagnes(id),
    ville        TEXT NOT NULL,
    pays         TEXT NOT NULL,
    rayon_km     INTEGER NOT NULL DEFAULT 15 CHECK (rayon_km > 0),
    lat          REAL CHECK (lat IS NULL OR lat BETWEEN -90 AND 90),
    lon          REAL CHECK (lon IS NULL OR lon BETWEEN -180 AND 180),
    statut       TEXT NOT NULL DEFAULT 'a_scanner'
                 CHECK (statut IN ('a_scanner', 'en_cours', 'termine')),
    dernier_scan TEXT,
    nb_trouves   INTEGER NOT NULL DEFAULT 0,
    nb_ignores   INTEGER NOT NULL DEFAULT 0
);
-- lat/lon NULL = géocodage échoué ("ville introuvable") : la zone n'est pas scannable.
-- Une même ville ne peut figurer qu'une fois par campagne (casse et espaces ignorés).
CREATE UNIQUE INDEX ux_zones_campagne_ville
    ON zones (campagne_id, lower(trim(ville)), upper(trim(pays)));

CREATE TABLE criteres (
    id           INTEGER PRIMARY KEY,
    campagne_id  INTEGER REFERENCES campagnes(id),
    code         TEXT NOT NULL,
    libelle      TEXT NOT NULL,
    points       INTEGER NOT NULL CHECK (points >= 0),
    actif        INTEGER NOT NULL DEFAULT 1 CHECK (actif IN (0, 1))
);
-- campagne_id NULL = critère global ; COALESCE car SQLite traite les NULL comme distincts.
CREATE UNIQUE INDEX ux_criteres_campagne_code ON criteres (COALESCE(campagne_id, 0), code);

CREATE TABLE prospects (
    id             INTEGER PRIMARY KEY,
    campagne_id    INTEGER NOT NULL REFERENCES campagnes(id),
    zone_id        INTEGER REFERENCES zones(id),
    nom            TEXT NOT NULL,
    nom_normalise  TEXT NOT NULL,
    adresse        TEXT,
    ville          TEXT,
    telephone      TEXT,
    site           TEXT,
    site_domaine   TEXT,
    email          TEXT CHECK (email IS NULL OR (email <> '' AND email = lower(trim(email)))),
    source         TEXT NOT NULL CHECK (source IN ('google_places', 'openstreetmap', 'import_csv')),
    source_id      TEXT,
    note_moyenne   REAL,
    nb_avis        INTEGER,
    problemes      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(problemes)),
    score          INTEGER NOT NULL DEFAULT 0,   -- score BRUT : affiché plafonné, trié sur le brut
    statut         TEXT NOT NULL DEFAULT 'nouveau'
                   CHECK (statut IN ('nouveau', 'qualifie', 'ignore', 'message_pret',
                                     'approuve', 'envoye', 'repondu')),
    cree_le        TEXT NOT NULL DEFAULT {_NOW}
);
-- Dédoublonnage en 3 niveaux, appliqué par la base elle-même.
CREATE UNIQUE INDEX ux_prospects_source_id ON prospects (source_id) WHERE source_id IS NOT NULL;
CREATE UNIQUE INDEX ux_prospects_nom_ville
    ON prospects (nom_normalise, lower(trim(COALESCE(ville, ''))));
CREATE UNIQUE INDEX ux_prospects_domaine ON prospects (site_domaine) WHERE site_domaine IS NOT NULL;
CREATE INDEX ix_prospects_campagne_statut ON prospects (campagne_id, statut);
CREATE INDEX ix_prospects_score ON prospects (score DESC);

CREATE TABLE messages (
    id                  INTEGER PRIMARY KEY,
    prospect_id         INTEGER NOT NULL REFERENCES prospects(id),
    objet               TEXT NOT NULL,
    corps               TEXT NOT NULL,
    genere_le           TEXT NOT NULL DEFAULT {_NOW},
    edite_par_humain    INTEGER NOT NULL DEFAULT 0 CHECK (edite_par_humain IN (0, 1)),
    version             INTEGER NOT NULL DEFAULT 1,
    approuve_le         TEXT,
    approuve_par_humain INTEGER NOT NULL DEFAULT 0 CHECK (approuve_par_humain IN (0, 1)),
    UNIQUE (prospect_id, version),
    CHECK ((approuve_le IS NULL AND approuve_par_humain = 0)
        OR (approuve_le IS NOT NULL AND approuve_par_humain = 1))
);

CREATE TABLE envois (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER NOT NULL REFERENCES messages(id),
    envoye_le   TEXT NOT NULL DEFAULT {_NOW},
    brevo_id    TEXT,
    statut      TEXT NOT NULL CHECK (statut IN ('envoye', 'ouvert', 'repondu', 'echec')),
    erreur      TEXT
);
CREATE INDEX ix_envois_message ON envois (message_id);
CREATE INDEX ix_envois_date ON envois (envoye_le);

CREATE TABLE liste_noire (
    id        INTEGER PRIMARY KEY,
    valeur    TEXT NOT NULL UNIQUE CHECK (valeur <> '' AND valeur = lower(trim(valeur))),
    motif     TEXT NOT NULL CHECK (motif IN ('desinscription', 'manuel', 'rebond')),
    ajoute_le TEXT NOT NULL DEFAULT {_NOW}
);

CREATE TABLE jobs (
    id           INTEGER PRIMARY KEY,
    type         TEXT NOT NULL CHECK (type IN ('scan', 'analyse', 'redaction', 'envoi')),
    campagne_id  INTEGER REFERENCES campagnes(id),
    statut       TEXT NOT NULL DEFAULT 'en_attente'
                 CHECK (statut IN ('en_attente', 'en_cours', 'termine', 'erreur')),
    progression  INTEGER NOT NULL DEFAULT 0 CHECK (progression BETWEEN 0 AND 100),
    detail       TEXT,
    demarre_le   TEXT,
    fini_le      TEXT
);

-- ---------------------------------------------------------------------------
-- Garde-fous d'approbation. Ils s'ajoutent à la garde explicite de sender.py,
-- ils ne la remplacent pas.
-- ---------------------------------------------------------------------------

-- Un message ne naît jamais approuvé : l'approbation est un acte posé après coup.
CREATE TRIGGER trg_messages_naissance_non_approuvee
BEFORE INSERT ON messages
WHEN NEW.approuve_le IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'Un message ne peut pas etre cree deja approuve');
END;

-- Une régénération (nouveau message) annule l'approbation en cours.
CREATE TRIGGER trg_messages_regeneration_annule_approbation
AFTER INSERT ON messages
BEGIN
    UPDATE messages SET approuve_le = NULL, approuve_par_humain = 0
     WHERE prospect_id = NEW.prospect_id AND id <> NEW.id AND approuve_le IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM envois e
                        WHERE e.message_id = messages.id AND e.statut <> 'echec');
    UPDATE prospects SET statut = 'message_pret'
     WHERE id = NEW.prospect_id AND statut = 'approuve';
END;

-- Modifier le texte d'un message approuvé annule aussi son approbation.
CREATE TRIGGER trg_messages_edition_annule_approbation
AFTER UPDATE OF objet, corps ON messages
WHEN OLD.approuve_le IS NOT NULL AND NEW.approuve_le IS OLD.approuve_le
BEGIN
    UPDATE messages SET approuve_le = NULL, approuve_par_humain = 0 WHERE id = NEW.id;
    UPDATE prospects SET statut = 'message_pret'
     WHERE id = NEW.prospect_id AND statut = 'approuve';
END;

-- Un prospect ne naît pas approuvé.
CREATE TRIGGER trg_prospects_naissance_non_approuvee
BEFORE INSERT ON prospects
WHEN NEW.statut = 'approuve'
BEGIN
    SELECT RAISE(ABORT, 'Un prospect ne peut pas etre cree deja approuve');
END;

-- Passage à 'approuve' : il faut un email ET un message approuvé par un humain, non encore envoyé.
CREATE TRIGGER trg_prospects_garde_approbation
BEFORE UPDATE OF statut ON prospects
WHEN NEW.statut = 'approuve' AND OLD.statut <> 'approuve'
BEGIN
    SELECT RAISE(ABORT, 'Approbation refusee : prospect sans email')
     WHERE NEW.email IS NULL;
    SELECT RAISE(ABORT, 'Approbation refusee : aucun message approuve par un humain')
     WHERE NOT EXISTS (
        SELECT 1 FROM messages m
         WHERE m.prospect_id = NEW.id AND m.approuve_le IS NOT NULL AND m.approuve_par_humain = 1
           AND NOT EXISTS (SELECT 1 FROM envois e
                            WHERE e.message_id = m.id AND e.statut <> 'echec'));
END;

PRAGMA user_version = 1;
COMMIT;
"""

_MIGRATION_2 = """
BEGIN;
ALTER TABLE prospects ADD COLUMN notes TEXT;
PRAGMA user_version = 2;
COMMIT;
"""

_MIGRATION_3 = """
BEGIN;
-- Appels téléphoniques : le script préparé pour un prospect, et le journal de ses appels.
ALTER TABLE prospects ADD COLUMN script_appel TEXT;
CREATE TABLE appels (
    id          INTEGER PRIMARY KEY,
    prospect_id INTEGER NOT NULL REFERENCES prospects(id),
    appele_le   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    resultat    TEXT NOT NULL CHECK (resultat IN ('pas_de_reponse', 'a_rappeler', 'interesse', 'rdv_obtenu',
                                                  'pas_interesse', 'mauvais_numero')),
    rappel_le   TEXT CHECK (rappel_le IS NULL OR rappel_le GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    note        TEXT
);
CREATE INDEX ix_appels_prospect ON appels (prospect_id, id);
PRAGMA user_version = 3;
COMMIT;
"""

_MIGRATION_4 = """
BEGIN;
-- Canal « formulaire de contact » : la page du formulaire repérée sur le site du prospect, le message préparé,
-- et le journal de ce qui a été envoyé à la main. `contact_cherche_le` évite de refaire l'exploration d'un site.
ALTER TABLE prospects ADD COLUMN formulaire_url TEXT;
ALTER TABLE prospects ADD COLUMN contact_cherche_le TEXT;
ALTER TABLE prospects ADD COLUMN message_formulaire TEXT;
CREATE TABLE contacts_formulaire (
    id          INTEGER PRIMARY KEY,
    prospect_id INTEGER NOT NULL REFERENCES prospects(id),
    fait_le     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    resultat    TEXT NOT NULL CHECK (resultat IN ('envoye', 'reponse', 'pas_interesse', 'inutilisable')),
    note        TEXT
);
CREATE INDEX ix_contacts_formulaire_prospect ON contacts_formulaire (prospect_id, id);
PRAGMA user_version = 4;
COMMIT;
"""

MIGRATIONS = [(1, _MIGRATION_1), (2, _MIGRATION_2), (3, _MIGRATION_3), (4, _MIGRATION_4)]

# --------------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------------


def normaliser_email(valeur):
    """Minuscules, sans espaces, sans 'mailto:' ni paramètres. None si vide."""
    if valeur is None:
        return None
    texte = str(valeur).strip().lower()
    if texte.startswith("mailto:"):
        texte = texte[len("mailto:"):]
    texte = texte.split("?")[0].strip()
    return texte or None


def normaliser_valeur_liste_noire(valeur):
    """Email complet ou domaine, en minuscules ; un '@' initial (« @domaine.fr ») est retiré."""
    texte = normaliser_email(valeur)
    if texte is None:
        return None
    return texte.lstrip("@") or None


def normaliser_nom(nom, mentions_juridiques=()):
    """Sans accents, casse, ponctuation ni mentions juridiques (liste fournie par les réglages)."""
    if not nom:
        return ""
    texte = unicodedata.normalize("NFKD", str(nom))
    texte = "".join(c for c in texte if not unicodedata.combining(c)).lower()
    texte = re.sub(r"[.'’`]", "", texte)  # « S.A.R.L. » -> « sarl », « l'esthétique » -> « lesthetique »
    mots = re.sub(r"[\W_]+", " ", texte).split()
    mentions = {m.strip().lower() for m in mentions_juridiques}
    utiles = [m for m in mots if m not in mentions]
    return " ".join(utiles or mots)


def extraire_domaine(url):
    """Domaine d'une URL, sans 'www.'. None si absent ou illisible."""
    if not url:
        return None
    brut = str(url).strip()
    if "//" not in brut:
        brut = "//" + brut
    hote = urlparse(brut).hostname
    if not hote:
        return None
    hote = hote.lower()
    return hote[4:] if hote.startswith("www.") else hote


def est_plateforme(domaine, plateformes):
    """Ce domaine (ou l'un de ses sous-domaines) est-il une plateforme / un annuaire de la liste ?"""
    if not domaine:
        return False
    return any(domaine == p or domaine.endswith("." + p) for p in plateformes)


def masquer_secret(valeur):
    """N'affiche que les quatre derniers caractères d'une clé."""
    if not valeur:
        return ""
    return "****" + valeur[-4:] if len(valeur) > 4 else "****"


# --------------------------------------------------------------------------------------
# Connexion
# --------------------------------------------------------------------------------------


def _domaines_generiques(conn):
    existe = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reglages'"
    ).fetchone()
    if not existe:
        return frozenset()
    ligne = conn.execute("SELECT valeur FROM reglages WHERE cle = 'domaines_generiques'").fetchone()
    if not ligne or not ligne["valeur"]:
        return frozenset()
    return frozenset(d.strip().lower() for d in json.loads(ligne["valeur"]))


def _enregistrer_fonctions(conn):
    """Les fonctions SQL sont propres à la connexion : on les pose ici, à chaque ouverture."""
    domaines = _domaines_generiques(conn)

    def est_generique(email):
        adresse = normaliser_email(email)
        if not adresse or "@" not in adresse:  # couvre aussi email NULL
            return 0
        return int(adresse.rsplit("@", 1)[1] in domaines)

    conn.create_function("est_generique", 1, est_generique, deterministic=True)


def get_conn(db_path=None):
    chemin = Path(db_path) if db_path else config.DB_PATH
    chemin.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(chemin, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Explicite : un trigger ne se redéclenche pas lui-même. Le trigger « régénération »
    # (INSERT sur messages) met à jour prospects, ce qui déclenche bien le trigger de garde
    # de prospects, mais celui-ci ne s'oppose qu'au passage VERS 'approuve'.
    conn.execute("PRAGMA recursive_triggers = OFF")
    _enregistrer_fonctions(conn)
    return conn


@contextmanager
def connexion(db_path=None):
    """Connexion avec commit en sortie normale, rollback sur exception, fermeture dans tous les cas."""
    conn = get_conn(db_path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------------------
# Réglages
# --------------------------------------------------------------------------------------


def get_reglage(conn, cle, defaut=None):
    ligne = conn.execute("SELECT valeur FROM reglages WHERE cle = ?", (cle,)).fetchone()
    return ligne["valeur"] if ligne and ligne["valeur"] is not None else defaut


def set_reglage(conn, cle, valeur):
    """Crée ou met à jour un réglage. Le drapeau `secret` d'une clé existante est conservé."""
    conn.execute(
        "INSERT INTO reglages (cle, valeur) VALUES (?, ?) "
        "ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur, "
        "maj_le = strftime('%Y-%m-%dT%H:%M:%SZ','now')",
        (cle, valeur),
    )


def _get_json(conn, cle):
    brut = get_reglage(conn, cle)
    return json.loads(brut) if brut else []


reglage_json = _get_json  # nom public : une liste de réglages lue en base (vide si absente)


def verifier_gitignore():
    """Refuse d'écrire des clés si .gitignore ne protège pas .env, data/ et *.db."""
    chemin = config.BASE_DIR / ".gitignore"
    lignes = set()
    if chemin.exists():
        lignes = {l.strip() for l in chemin.read_text(encoding="utf-8").splitlines()}
    manquants = [m for m in (".env", "data/", "*.db") if m not in lignes]
    if manquants:
        raise RuntimeError(
            f".gitignore ne protège pas : {', '.join(manquants)}. "
            "Corrige-le avant de stocker une clé dans la base."
        )


def importer_env_une_fois(conn):
    """Copie le .env dans `reglages` au tout premier lancement, puis la base fait foi.

    Renvoie le nombre de clés importées. Les valeurs ne sont jamais écrites dans le journal.
    """
    if get_reglage(conn, "env_importe") == "1":
        return 0
    importees = 0
    if config.ENV_PATH.exists():
        verifier_gitignore()
        try:
            from dotenv import dotenv_values
        except ImportError as erreur:
            raise RuntimeError(
                "Un fichier .env existe mais python-dotenv n'est pas installé. "
                "Lance : pip install -r requirements.txt"
            ) from erreur
        valeurs = dotenv_values(config.ENV_PATH)
        for variable, cle in ENV_VERS_REGLAGES.items():
            valeur = (valeurs.get(variable) or "").strip()
            if valeur:
                set_reglage(conn, cle, valeur)
                importees += 1
    set_reglage(conn, "env_importe", "1")
    return importees


# --------------------------------------------------------------------------------------
# Liste noire et prospects
# --------------------------------------------------------------------------------------


def ajouter_liste_noire(conn, valeur, motif):
    """Ajoute un email ou un domaine. Renvoie False s'il y était déjà (casse ignorée)."""
    normalisee = normaliser_valeur_liste_noire(valeur)
    if normalisee is None:
        raise ValueError("Valeur de liste noire vide")
    curseur = conn.execute(
        "INSERT INTO liste_noire (valeur, motif) VALUES (?, ?) ON CONFLICT(valeur) DO NOTHING",
        (normalisee, motif),
    )
    return curseur.rowcount == 1


def est_en_liste_noire(conn, email):
    """Vrai si l'adresse, ou son domaine, figure en liste noire."""
    adresse = normaliser_email(email)
    if adresse is None:
        return False
    domaine = adresse.rsplit("@", 1)[1] if "@" in adresse else adresse
    ligne = conn.execute(
        "SELECT 1 FROM liste_noire WHERE valeur IN (?, ?)", (adresse, domaine)
    ).fetchone()
    return ligne is not None


_CHAMPS_PROSPECT = (
    "campagne_id", "zone_id", "nom", "adresse", "ville", "telephone", "site",
    "email", "source", "source_id", "note_moyenne", "nb_avis",
)


def inserer_prospect(conn, prospect):
    """Insère un prospect. Renvoie True s'il est nouveau, False si c'est un doublon.

    Un doublon (source_id, nom normalisé + ville, ou domaine déjà connu) n'est jamais une
    exception : la ligne est ignorée et `zones.nb_ignores` s'incrémente.
    """
    valeurs = {champ: prospect.get(champ) for champ in _CHAMPS_PROSPECT}
    valeurs["email"] = normaliser_email(valeurs["email"])
    valeurs["nom_normalise"] = normaliser_nom(
        valeurs["nom"], _get_json(conn, "mentions_juridiques")
    )
    domaine = extraire_domaine(valeurs["site"])
    # Un site hébergé sur une plateforme (doctolib.fr…) ne sert pas au dédoublonnage : deux cabinets peuvent partager ce domaine.
    valeurs["site_domaine"] = None if est_plateforme(domaine, _get_json(conn, "domaines_plateformes")) else domaine

    curseur = conn.execute(
        """INSERT INTO prospects
               (campagne_id, zone_id, nom, nom_normalise, adresse, ville, telephone, site,
                site_domaine, email, source, source_id, note_moyenne, nb_avis)
           VALUES (:campagne_id, :zone_id, :nom, :nom_normalise, :adresse, :ville, :telephone,
                   :site, :site_domaine, :email, :source, :source_id, :note_moyenne, :nb_avis)
           ON CONFLICT DO NOTHING""",
        valeurs,
    )
    if curseur.rowcount == 1:
        return True
    if valeurs["zone_id"] is not None:
        conn.execute("UPDATE zones SET nb_ignores = nb_ignores + 1 WHERE id = ?",
                     (valeurs["zone_id"],))
    return False


def trouver_doublon(conn, prospect):
    """Prospect déjà en base qui ferait doublon (même règles que les index uniques), ou None.

    Sert à éviter un travail coûteux (recherche d'email) sur un doublon, et à signaler les
    prospects déjà présents dans une AUTRE campagne.
    """
    nom_normalise = normaliser_nom(prospect.get("nom"), _get_json(conn, "mentions_juridiques"))
    domaine = extraire_domaine(prospect.get("site"))
    if est_plateforme(domaine, _get_json(conn, "domaines_plateformes")):
        domaine = None
    ville = (prospect.get("ville") or "").strip().lower()
    requetes = []
    if prospect.get("source_id"):
        requetes.append(("source_id", "SELECT id, campagne_id FROM prospects WHERE source_id = ?", (prospect["source_id"],)))
    requetes.append(("nom_ville", "SELECT id, campagne_id FROM prospects WHERE nom_normalise = ? "
                     "AND lower(trim(COALESCE(ville, ''))) = ?", (nom_normalise, ville)))
    if domaine:
        requetes.append(("domaine", "SELECT id, campagne_id FROM prospects WHERE site_domaine = ?", (domaine,)))
    for motif, sql, params in requetes:
        ligne = conn.execute(sql, params).fetchone()
        if ligne:
            return {**dict(ligne), "motif": motif}  # motif : source_id | nom_ville | domaine
    return None


# Changements de statut décidés à la main dans le tableau : {statut visé: statuts de départ autorisés}
TRANSITIONS_MANUELLES = {
    "ignore": ("nouveau", "qualifie", "message_pret", "approuve"),
    "qualifie": ("ignore",),
}


def changer_statut(conn, ids, cible):
    """Ignore des prospects, ou les remet en « qualifié ». Renvoie {'modifies', 'refuses'}.

    Un prospect déjà envoyé ou répondu n'est jamais touché.
    """
    if cible not in TRANSITIONS_MANUELLES:
        raise ValueError(f"Statut non modifiable à la main : {cible}")
    departs = TRANSITIONS_MANUELLES[cible]
    modifies = 0
    for prospect_id in ids:
        curseur = conn.execute(
            f"UPDATE prospects SET statut = ? WHERE id = ? AND statut IN ({','.join('?' * len(departs))})",
            (cible, prospect_id, *departs))
        modifies += curseur.rowcount
    return {"modifies": modifies, "refuses": len(ids) - modifies}


def compter_ignore(conn, zone_id):
    """Incrémente le compteur « déjà en base » d'une zone."""
    if zone_id is not None:
        conn.execute("UPDATE zones SET nb_ignores = nb_ignores + 1 WHERE id = ?", (zone_id,))


# --------------------------------------------------------------------------------------
# Initialisation
# --------------------------------------------------------------------------------------


def init_db(db_path=None, importer_env=True):
    """Applique les migrations, les seeds (une fois) et l'import .env (une fois)."""
    from core import seeds

    resume = {}
    with connexion(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for numero, script in MIGRATIONS:
            if numero > version:
                conn.executescript(script)
        resume["version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        resume["seeds_appliques_maintenant"] = seeds.appliquer_seeds(conn)
        resume["cles_importees_du_env"] = importer_env_une_fois(conn) if importer_env else 0
    return resume


# --------------------------------------------------------------------------------------
# Vérification (python -m core.db check)
# --------------------------------------------------------------------------------------


def _comptes(chemin):
    with connexion(chemin) as conn:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def _refuse(chemin, sql, params=()):
    """Exécute une écriture qui DOIT être refusée. Renvoie le message de refus, ou None."""
    try:
        with connexion(chemin) as conn:
            conn.execute(sql, params)
    except sqlite3.IntegrityError as erreur:
        return str(erreur)
    return None


def _autotests():
    """Rejoue les garde-fous sur une base jetable. Renvoie la liste (réussi, libellé)."""
    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        chemin = Path(tmp) / "test.db"
        init_db(chemin, importer_env=False)

        # --- Seeds et idempotence ---------------------------------------------------
        avant = _comptes(chemin)
        init_db(chemin, importer_env=False)
        verifier("init rejoué : aucun doublon de seeds", avant == _comptes(chemin))
        with connexion(chemin) as c:
            verifier("3 campagnes modèles en base",
                     c.execute("SELECT COUNT(*) FROM campagnes WHERE est_modele = 1").fetchone()[0] == 3)
            verifier("13 critères globaux (10 de la spec + « site_plateforme » + 2 diagnostics à 0 point), aucun 'site_daté'",
                     c.execute("SELECT COUNT(*) FROM criteres WHERE campagne_id IS NULL").fetchone()[0] == 13
                     and c.execute("SELECT COUNT(*) FROM criteres WHERE code LIKE 'site_dat%'").fetchone()[0] == 0)
            verifier("foreign_keys actif", c.execute("PRAGMA foreign_keys").fetchone()[0] == 1)
            verifier("recursive_triggers posé explicitement (OFF)",
                     c.execute("PRAGMA recursive_triggers").fetchone()[0] == 0)
            c.execute("INSERT INTO campagnes (nom, secteur, pays, langue) VALUES ('T', 's', 'FR', 'fr')")
            cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO zones (campagne_id, ville, pays) VALUES (?, 'Lyon', 'FR')", (cid,))
            zid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        # --- Zones : unicité par campagne --------------------------------------------
        verifier("zone en double refusée (casse et espaces ignorés)",
                 _refuse(chemin, "INSERT INTO zones (campagne_id, ville, pays) VALUES (?, ' lyon ', 'fr')", (cid,)))

        # --- Dédoublonnage sans exception --------------------------------------------
        base = {"campagne_id": cid, "zone_id": zid, "source": "google_places", "ville": "Lyon"}
        with connexion(chemin) as c:
            r = [
                inserer_prospect(c, {**base, "nom": "Cabinet A", "source_id": "g1",
                                     "site": "https://www.cabinet-a.fr", "email": " Contact@Cabinet-A.FR "}),
                inserer_prospect(c, {**base, "nom": "Autre nom", "source_id": "g1"}),
                inserer_prospect(c, {**base, "nom": "CABINET A S.A.R.L.", "source_id": "g2", "ville": "lyon"}),
                inserer_prospect(c, {**base, "nom": "Nom différent", "source_id": "g3",
                                     "site": "http://cabinet-a.fr/contact"}),
                inserer_prospect(c, {**base, "nom": "Cabinet B", "source_id": "g4"}),
            ]
            verifier("dédoublonnage : 3 doublons ignorés sans exception, la boucle continue",
                     r == [True, False, False, False, True])
            verifier("zones.nb_ignores = 3",
                     c.execute("SELECT nb_ignores FROM zones WHERE id = ?", (zid,)).fetchone()[0] == 3)
            verifier("email normalisé à l'insertion",
                     c.execute("SELECT email FROM prospects WHERE source_id = 'g1'").fetchone()[0]
                     == "contact@cabinet-a.fr")
            plateformes = [
                inserer_prospect(c, {**base, "nom": "Cabinet Doc 1", "source_id": "g10", "site": "https://www.doctolib.fr/dentiste/lyon/a"}),
                inserer_prospect(c, {**base, "nom": "Cabinet Doc 2", "source_id": "g11", "site": "https://www.doctolib.fr/dentiste/lyon/b"}),
                inserer_prospect(c, {**base, "nom": "Cabinet Doc 3", "source_id": "g12", "site": "https://fr-fr.facebook.com/doc3"})]
            verifier("plateformes (Doctolib, Facebook) : plusieurs cabinets peuvent partager ce domaine, aucun faux doublon",
                     plateformes == [True, True, True]
                     and c.execute("SELECT COUNT(*) FROM prospects WHERE source_id IN ('g10', 'g11', 'g12') AND site_domaine IS NULL").fetchone()[0] == 3)
            pid = c.execute("SELECT id FROM prospects WHERE source_id = 'g1'").fetchone()[0]
            pid_b = c.execute("SELECT id FROM prospects WHERE source_id = 'g4'").fetchone()[0]

        verifier("email non normalisé refusé directement par la base",
                 _refuse(chemin, "INSERT INTO prospects (campagne_id, nom, nom_normalise, source, email) "
                                 "VALUES (?, 'X', 'x', 'import_csv', 'Majuscule@X.fr')", (cid,)))
        verifier("statut inconnu refusé",
                 _refuse(chemin, "UPDATE prospects SET statut = 'bidule' WHERE id = ?", (pid,)))
        verifier("clé étrangère inexistante refusée",
                 _refuse(chemin, "INSERT INTO prospects (campagne_id, nom, nom_normalise, source) "
                                 "VALUES (999999, 'X', 'x2', 'import_csv')"))

        # --- Liste noire : casse et domaine -------------------------------------------
        with connexion(chemin) as c:
            verifier("liste noire : ajout", ajouter_liste_noire(c, "contact@cabinet-a.fr", "manuel"))
            verifier("liste noire : même valeur en majuscules = doublon",
                     not ajouter_liste_noire(c, "  CONTACT@Cabinet-A.fr ", "manuel"))
            verifier("liste noire : Contact@Cabinet.FR bloqué malgré la casse",
                     est_en_liste_noire(c, " Contact@Cabinet-A.FR "))
            ajouter_liste_noire(c, "@Bloque.COM", "desinscription")
            verifier("liste noire : un domaine bloque toutes ses adresses",
                     est_en_liste_noire(c, "x@BLOQUE.com"))
            verifier("liste noire : adresse saine non bloquée", not est_en_liste_noire(c, "x@ok.fr"))
        verifier("liste noire : valeur en majuscules refusée directement par la base",
                 _refuse(chemin, "INSERT INTO liste_noire (valeur, motif) VALUES ('ABC@X.fr', 'manuel')"))

        # --- est_generique sur chaque connexion ---------------------------------------
        c1, c2 = get_conn(chemin), get_conn(chemin)
        sql = "SELECT est_generique('Jean@Gmail.com'), est_generique(NULL), est_generique('a@cabinet.fr')"
        verifier("est_generique disponible sur la 1re connexion", tuple(c1.execute(sql).fetchone()) == (1, 0, 0))
        verifier("est_generique disponible sur la 2e connexion (email NULL géré)",
                 tuple(c2.execute(sql).fetchone()) == (1, 0, 0))
        c1.close()
        c2.close()

        # --- Approbation et triggers (dont le cas récursif) ---------------------------
        with connexion(chemin) as c:
            c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'o', 'v1', 1)", (pid,))
            m1 = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        verifier("approbation refusée sans message approuvé",
                 _refuse(chemin, "UPDATE prospects SET statut = 'approuve' WHERE id = ?", (pid,)))
        verifier("message créé déjà approuvé : refusé",
                 _refuse(chemin, "INSERT INTO messages (prospect_id, objet, corps, version, approuve_le, "
                                 "approuve_par_humain) VALUES (?, 'o', 'x', 9, '2026-01-01T00:00:00Z', 1)", (pid,)))
        with connexion(chemin) as c:
            c.execute("UPDATE messages SET approuve_le = strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
                      "approuve_par_humain = 1 WHERE id = ?", (m1,))
            c.execute("UPDATE prospects SET statut = 'approuve' WHERE id = ?", (pid,))
            verifier("approbation acceptée avec email + message approuvé",
                     c.execute("SELECT statut FROM prospects WHERE id = ?", (pid,)).fetchone()[0] == "approuve")
        # Cas récursif : INSERT sur messages -> UPDATE prospects -> trigger de garde de prospects
        erreur_regen = None
        try:
            with connexion(chemin) as c:
                c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'o', 'v2', 2)", (pid,))
        except sqlite3.Error as e:
            erreur_regen = str(e)
        with connexion(chemin) as c:
            statut = c.execute("SELECT statut FROM prospects WHERE id = ?", (pid,)).fetchone()[0]
            m1_appr = c.execute("SELECT approuve_le FROM messages WHERE id = ?", (m1,)).fetchone()[0]
        verifier("régénération sur prospect approuvé : repasse à message_pret sans erreur (récursif)",
                 erreur_regen is None and statut == "message_pret")
        verifier("régénération : l'ancienne approbation est annulée", m1_appr is None)
        with connexion(chemin) as c:
            m2 = c.execute("SELECT id FROM messages WHERE prospect_id = ? AND version = 2", (pid,)).fetchone()[0]
            c.execute("UPDATE messages SET approuve_le = strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
                      "approuve_par_humain = 1 WHERE id = ?", (m2,))
            c.execute("UPDATE prospects SET statut = 'approuve' WHERE id = ?", (pid,))
            c.execute("UPDATE messages SET corps = 'v2 corrigé après approbation' WHERE id = ?", (m2,))
        with connexion(chemin) as c:
            ligne = c.execute("SELECT m.approuve_le, p.statut FROM messages m JOIN prospects p "
                              "ON p.id = m.prospect_id WHERE m.id = ?", (m2,)).fetchone()
        verifier("texte modifié après approbation : approbation annulée, statut message_pret",
                 ligne["approuve_le"] is None and ligne["statut"] == "message_pret")

        # --- Prospect sans email : générable mais jamais approuvable ------------------
        with connexion(chemin) as c:
            c.execute("INSERT INTO messages (prospect_id, objet, corps, version) VALUES (?, 'o', 'c', 1)", (pid_b,))
            mb = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("UPDATE messages SET approuve_le = strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
                      "approuve_par_humain = 1 WHERE id = ?", (mb,))
        verifier("prospect sans email : approbation refusée même avec un message approuvé",
                 _refuse(chemin, "UPDATE prospects SET statut = 'approuve' WHERE id = ?", (pid_b,)))
        verifier("prospect créé déjà approuvé : refusé",
                 _refuse(chemin, "INSERT INTO prospects (campagne_id, nom, nom_normalise, source, statut) "
                                 "VALUES (?, 'Z', 'z', 'import_csv', 'approuve')", (cid,)))

    return resultats


def _cmd_init():
    config.configurer_logging()
    resume = init_db()
    journal = logging.getLogger("prospect")
    journal.info("init base : %s", resume)
    print(f"Base               : {config.DB_PATH}")
    print(f"Version du schéma  : {resume['version']}")
    print("Seeds              : "
          + ("appliqués à l'instant" if resume["seeds_appliques_maintenant"] else "déjà appliqués (inchangés)"))
    print(f"Clés lues du .env  : {resume['cles_importees_du_env']}")
    print("Terminé. Lance maintenant : python -m core.db check")
    return 0


def _cmd_check():
    if not config.DB_PATH.exists():
        print(f"ERREUR : base absente ({config.DB_PATH}). Lance d'abord : python -m core.db init")
        return 1
    echecs = 0
    print(f"=== Base réelle : {config.DB_PATH}")
    with connexion() as c:
        tables = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        print(f"Version du schéma : {c.execute('PRAGMA user_version').fetchone()[0]}")
        print(f"Tables ({len(tables)}) : {', '.join(sorted(tables))}")
        if tables != TABLES_ATTENDUES:
            print(f"KO  tables attendues différentes : manque {sorted(TABLES_ATTENDUES - tables)}, "
                  f"en trop {sorted(tables - TABLES_ATTENDUES)}")
            echecs += 1
        else:
            print("OK  les 10 tables attendues sont présentes")
        print("Modèles de campagne :")
        for l in c.execute("SELECT nom, secteur, requetes FROM campagnes WHERE est_modele = 1 ORDER BY id"):
            print(f"  - {l['nom']}  [{l['secteur']}]  requêtes : {l['requetes']}")
        print(f"Critères globaux : {c.execute('SELECT COUNT(*) FROM criteres WHERE campagne_id IS NULL').fetchone()[0]}")
        print("Clés (masquées) :")
        for l in c.execute("SELECT cle, valeur FROM reglages WHERE secret = 1 ORDER BY cle"):
            print(f"  - {l['cle']} = {masquer_secret(l['valeur']) or '(vide : non configuré)'}")
        print(f"Autres réglages  : {c.execute('SELECT COUNT(*) FROM reglages WHERE secret = 0').fetchone()[0]}")
    try:
        verifier_gitignore()
        print("OK  .gitignore protège .env, data/ et *.db")
    except RuntimeError as erreur:
        print(f"KO  {erreur}")
        echecs += 1

    print("\n=== Auto-tests sur une base jetable (la vraie base n'est pas touchée)")
    resultats = _autotests()
    for reussi, libelle in resultats:
        print(f"{'OK ' if reussi else 'KO '} {libelle}")
    echecs += sum(1 for reussi, _ in resultats if not reussi)
    print(f"\nRésultat : {sum(1 for r, _ in resultats if r)}/{len(resultats)} tests réussis"
          + ("" if echecs == 0 else f"  --  {echecs} ÉCHEC(S)"))
    return 0 if echecs == 0 else 1


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    commande = argv[1] if len(argv) > 1 else ""
    if commande == "init":
        return _cmd_init()
    if commande == "check":
        return _cmd_check()
    print("Usage : python -m core.db init | check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
