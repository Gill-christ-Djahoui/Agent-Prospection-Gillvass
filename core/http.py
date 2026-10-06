"""Point d'appel unique pour tout le réseau sortant.

Deux garanties, sans exception :
- le timeout est OBLIGATOIRE (argument sans valeur par défaut) : un appel sans timeout ne tourne pas ;
- chaque appel est journalisé avec son horodatage (méthode, hôte, chemin, statut, durée),
  jamais les en-têtes, les corps ni les paramètres d'URL, donc jamais une clé.

Aucun autre module n'appelle `requests` directement.
"""
import logging
import time
from urllib.parse import urlsplit

import requests

journal = logging.getLogger("prospect")


def requete(methode, url, *, timeout, **kwargs):
    """Appelle `requests.request` avec timeout obligatoire et journalisation."""
    morceaux = urlsplit(url)
    cible = f"{morceaux.netloc}{morceaux.path}"
    debut = time.monotonic()
    try:
        reponse = requests.request(methode, url, timeout=timeout, **kwargs)
    except requests.RequestException as erreur:
        journal.warning("HTTP %s %s -> ERREUR %s (%d ms)", methode, cible,
                        type(erreur).__name__, (time.monotonic() - debut) * 1000)
        raise
    journal.info("HTTP %s %s -> %s (%d ms)", methode, cible, reponse.status_code,
                 (time.monotonic() - debut) * 1000)
    return reponse


def message_erreur_reseau(erreur, timeout):
    """Message lisible pour une exception réseau, affiché tel quel dans l'interface."""
    if isinstance(erreur, requests.Timeout):
        return f"Délai dépassé : aucune réponse en {timeout} s."
    if isinstance(erreur, requests.exceptions.SSLError):
        return f"Erreur de certificat SSL : {erreur}"
    if isinstance(erreur, requests.ConnectionError):
        return ("Connexion impossible (réseau coupé, adresse erronée ou service injoignable) : "
                f"{str(erreur)[:160]}")
    if isinstance(erreur, requests.exceptions.InvalidURL | requests.exceptions.MissingSchema):
        return f"Adresse invalide : {erreur}"
    return f"Erreur réseau : {erreur}"


def resume_erreur(reponse):
    """Extrait le message d'erreur d'une réponse HTTP (JSON si possible, sinon texte brut tronqué)."""
    try:
        corps = reponse.json()
    except ValueError:
        return (reponse.text or "").strip()[:300] or "(réponse vide)"
    if isinstance(corps, dict):
        erreur = corps.get("error", corps.get("message"))
        if isinstance(erreur, dict):
            erreur = erreur.get("message") or erreur
        if erreur:
            return str(erreur)[:300]
    return str(corps)[:300]
