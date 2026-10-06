"""Données du tableau de bord : ce qu'il y a à faire maintenant, chiffres clés, entonnoir, activité, mise en route.

Utilisable seul : python -m core.accueil   (affiche le résumé de la vraie base)
"""
import json
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core import analyses, campagnes, db, messages, reglages, sender, suivi

# Entonnoir : (statut, libellé) dans l'ordre du cycle de prospection
ENTONNOIR = [("nouveau", "Nouveaux"), ("qualifie", "Analysés"), ("message_pret", "Message à valider"),
             ("approuve", "Approuvés"), ("envoye", "Envoyés"), ("repondu", "Ont répondu"), ("ignore", "Ignorés")]
TYPES_TACHES = {"scan": "Scan", "analyse": "Analyse des sites", "redaction": "Rédaction", "envoi": "Envoi"}


def _heure_locale(iso, fuseau):
    if not iso:
        return ""
    return (datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            .astimezone(ZoneInfo(fuseau)).strftime("%d/%m %H:%M"))


def mise_en_route(conn, cfg):
    """Étapes pour démarrer, cochées automatiquement : (libellé, fait, lien)."""
    def une(sql):
        return conn.execute(sql).fetchone()[0] > 0

    return [
        ("Renseigner ton identité d'expédition (nom, adresse, signature)",
         bool((cfg.get("expediteur_nom") or "").strip() and (cfg.get("expediteur_email") or "").strip()), "reglages"),
        ("Connecter Brevo (clé API testée)", bool((cfg.get("brevo_key") or "").strip()), "reglages"),
        ("Connecter ton fournisseur d'IA (endpoint, modèle, clé)",
         all((cfg.get(k) or "").strip() for k in ("ai_endpoint", "ai_model", "ai_key")), "reglages"),
        ("Créer ta première campagne", une("SELECT COUNT(*) FROM campagnes WHERE est_modele = 0"), "campagnes"),
        ("Lancer un premier scan", une("SELECT COUNT(*) FROM prospects"), "campagnes"),
        ("Approuver un premier message", une("SELECT COUNT(*) FROM messages WHERE approuve_le IS NOT NULL"), "messages"),
        ("Envoyer un premier email", une("SELECT COUNT(*) FROM envois WHERE statut IN ('envoye', 'ouvert', 'repondu')"), "envois"),
    ]


def donnees(conn, cfg):
    statuts = {l[0]: l[1] for l in conn.execute("SELECT statut, COUNT(*) FROM prospects GROUP BY statut")}
    total_prospects = sum(statuts.values())
    envoyes = conn.execute("SELECT COUNT(*) FROM envois WHERE statut IN ('envoye', 'ouvert', 'repondu')").fetchone()[0]
    ouverts = conn.execute("SELECT COUNT(*) FROM envois WHERE statut IN ('ouvert', 'repondu')").fetchone()[0]
    reponses = conn.execute("SELECT COUNT(*) FROM envois WHERE statut = 'repondu'").fetchone()[0]
    compteurs = messages.compteurs(conn)
    quota = sender.quota(conn, cfg)
    relances = len(suivi.file_de_relance(conn, cfg))
    a_envoyer = len([f for f in sender.en_attente_d_envoi(conn) if not db.est_en_liste_noire(conn, f["email"])])
    maximum = max([statuts.get(s, 0) for s, _ in ENTONNOIR] + [1])

    a_faire = [
        {"cle": "valider", "nombre": compteurs["en_attente"], "titre": "messages à valider",
         "aide": "Relis, corrige, approuve.", "lien": "messages", "action": "Ouvrir les messages"},
        {"cle": "envoyer", "nombre": a_envoyer, "titre": "messages prêts à partir",
         "aide": f"Reste {quota['reste']} envoi(s) aujourd'hui.", "lien": "envois", "action": "Aller aux envois"},
        {"cle": "relancer", "nombre": relances, "titre": "relances à préparer",
         "aide": f"Sans réponse depuis {cfg['delai_relance_jours']} jours.", "lien": "envois", "action": "Voir les relances"},
        {"cle": "analyser", "nombre": statuts.get("nouveau", 0), "titre": "prospects à analyser",
         "aide": "Leur site n'a pas encore été audité.", "lien": "campagnes", "action": "Ouvrir les campagnes"},
    ]
    taches = [dict(l) for l in conn.execute(
        """SELECT j.id, j.type, j.statut, j.progression, j.detail, j.demarre_le, j.fini_le, c.nom AS campagne
             FROM jobs j LEFT JOIN campagnes c ON c.id = j.campagne_id ORDER BY j.id DESC LIMIT 6""")]
    for t in taches:
        t["libelle"] = TYPES_TACHES.get(t["type"], t["type"])
        t["quand"] = _heure_locale(t["fini_le"] or t["demarre_le"], cfg["fuseau"])
    etapes = mise_en_route(conn, cfg)
    return {
        "a_faire": a_faire,
        "chiffres": {"prospects": total_prospects, "envoyes": envoyes, "reponses": reponses,
                     "taux_ouverture": round(100 * ouverts / envoyes) if envoyes else None,
                     "taux_reponse": round(100 * reponses / envoyes) if envoyes else None, "quota": quota},
        "entonnoir": [{"statut": s, "libelle": l, "nombre": statuts.get(s, 0),
                       "largeur": round(100 * statuts.get(s, 0) / maximum)} for s, l in ENTONNOIR],
        "campagnes": [c for c in campagnes.lister(conn) if c["statut"] != "archivee"][:6],
        "taches": taches,
        "tache_en_cours": next((t for t in taches if t["statut"] in ("en_attente", "en_cours")), None),
        "decisions": analyses.donnees_page(conn, cfg)["recommandations"][:3],
        "mise_en_route": [{"libelle": l, "fait": f, "lien": lien} for l, f, lien in etapes],
        "mise_en_route_faite": sum(1 for _, f, _ in etapes if f),
    }


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with db.connexion() as conn:
        d = donnees(conn, reglages.valeurs_effectives(conn))
    print(json.dumps({k: d[k] for k in ("chiffres", "a_faire", "mise_en_route_faite")}, ensure_ascii=False, indent=1))
