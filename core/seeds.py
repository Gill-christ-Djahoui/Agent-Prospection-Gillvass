"""Données de départ : réglages, critères globaux, trois modèles de campagne.

C'est le seul fichier où des valeurs métier ont le droit d'apparaître. Tout ce qui est
inséré ici se modifie ensuite depuis l'interface. Les seeds ne sont appliqués qu'une fois
(drapeau `seeds_appliques` dans `reglages`) : ce que l'utilisateur supprime ne revient pas.

Utilisable seul : python -m core.seeds  (initialise la base puis applique les seeds)
"""
import json

# --- Réglages : (clé, valeur, secret) -----------------------------------------------

# Habillage HTML des emails (voir core/courriel.py). Variables : {contenu} (texte approuvé mis en page), {mention}
# (désinscription), {couleur} (réglage email_couleur_accent). Pas d'image ni de script : sobre, rapide, lisible partout.
_MODELE_EMAIL_HTML_V1 = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#ffffff;">
<div style="max-width:620px;margin:0 auto;padding:28px 22px;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.65;color:#1f2933;">
<div style="height:3px;width:44px;background:{couleur};margin:0 0 22px 0;"></div>
{contenu}
<div style="margin-top:26px;padding-top:14px;border-top:1px solid #e3e7ec;font-size:12px;line-height:1.55;color:#7b8794;">{mention}</div>
</div>
</body></html>"""

# Version 2 : une carte blanche sur fond gris, avec une barre de couleur. Jolie, mais Gmail la range dans « Promotions »
# (elle ressemble à une newsletter).
_MODELE_EMAIL_HTML_V2 = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f5f8;">
<div style="padding:28px 12px;">
<div style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #e4e8ee;border-radius:12px;padding:34px 34px 26px 34px;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:16px;line-height:1.7;color:#1f2933;">
<div style="height:4px;width:48px;background:{couleur};border-radius:2px;margin:0 0 26px 0;"></div>
{contenu}
<div style="margin-top:30px;padding-top:16px;border-top:1px solid #edf0f4;font-size:12px;line-height:1.55;color:#8592a3;">{mention}</div>
</div>
</div>
</body></html>"""

# Modèle par défaut actuel : l'allure d'un message écrit à la main (aucun fond, aucun cadre, aucune barre de couleur),
# texte aéré. Un modèle jamais modifié par l'utilisateur et resté identique à une ancienne version par défaut est
# remplacé par celui-ci au lancement (voir appliquer_seeds).
_MODELE_EMAIL_HTML = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#ffffff;">
<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.75;color:#222222;">
{contenu}
<p style="margin:28px 0 0 0;font-size:12px;line-height:1.5;color:#777777;">{mention}</p>
</div>
</body></html>"""
_MODELES_EMAIL_ANCIENS = (_MODELE_EMAIL_HTML_V1, _MODELE_EMAIL_HTML_V2)

# Style « élégant » : une carte soignée sur fond clair (mise en page en tableaux : elle tient dans Gmail, Outlook et les
# téléphones), trait de couleur en tête, texte large et aéré, pied de page discret. Ni image, ni bouton, ni lien ajouté.
_MODELE_EMAIL_ELEGANT = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light"></head>
<body style="margin:0;padding:0;background:#eef1f6;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f6;"><tr><td align="center" style="padding:30px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border:1px solid #dfe5ee;border-radius:14px;">
<tr><td style="height:6px;line-height:6px;font-size:0;background:{couleur};border-radius:14px 14px 0 0;">&nbsp;</td></tr>
<tr><td style="padding:36px 40px 10px 40px;font-family:'Segoe UI',-apple-system,Roboto,Helvetica,Arial,sans-serif;font-size:16px;line-height:1.75;color:#1f2937;">
{contenu}
</td></tr>
<tr><td style="padding:6px 40px 30px 40px;font-family:'Segoe UI',-apple-system,Roboto,Helvetica,Arial,sans-serif;font-size:12px;line-height:1.6;color:#8a94a6;">
<div style="border-top:1px solid #edf0f5;padding-top:16px;">{mention}</div>
</td></tr>
</table>
</td></tr></table>
</body></html>"""

# Consignes de l'assistant de niche (Campagnes > Nouvelle niche). Variables : {niche} {offre} {probleme} {ville} {pays}
# {criteres} {zones_instruction}. L'IA propose, l'utilisateur relit avant toute création ; {nom} reste tel quel (objet d'email).
_PROMPT_NICHE = """Tu aides un développeur web freelance à préparer une campagne de prospection par email pour une nouvelle niche.

Niche visée : {niche}
Ce que le freelance propose : {offre}
Problème qu'il veut cibler : {probleme}
Ville principale : {ville} ({pays})

Critères de diagnostic disponibles (code : description, poids actuel) :
{criteres}

Réponds UNIQUEMENT par un objet JSON, sans texte autour, avec ces clés :
- "secteur" : libellé court du métier, au singulier, repris dans les messages (exemple : "cabinet de kinésithérapie").
- "requetes" : 2 à 4 recherches à saisir dans Google Maps pour trouver ces établissements (exemple : ["kinésithérapeute", "cabinet de kinésithérapie"]), sans la ville.
- "objet" : objet de l'email, 8 mots au plus : {nom} d'abord, puis deux-points, puis une courte question ou observation (exemples de forme : « {nom} : une question rapide », « {nom} : vos clients vous trouvent-ils ? »). Pas de verbe d'action commercial (Améliorez, Découvrez, Boostez), aucun mot de vente, aucune ponctuation d'urgence.
- "angle" : 2 ou 3 phrases, avec « Angle : » écrit UNE seule fois au début. C'est une CONSIGNE adressée à l'IA qui rédigera chaque email (par exemple « Relie le problème constaté à… »), pas un texte de vente : elle explique, en parlant du métier visé, pourquoi le problème ciblé compte pour SES clients. Langage simple, aucune promesse de résultat, aucun chiffre inventé.
- "criteres" : un objet {code: poids entier de 0 à 10} pour chaque critère qui compte vraiment pour cette offre ; les autres gardent leur poids actuel. Plus le poids est élevé, plus ce problème rend l'établissement prioritaire.
- "zones" : {zones_instruction}"""

# Script d'un appel de prospection (écran Appels). Variables : {expediteur} {nom} {ville} {secteur} {site} {problemes}
# {score} {angle} (la partie « Angle : » de la campagne : elle porte ce qui change d'une niche à l'autre).
_PROMPT_APPEL = """Tu prépares ce que {expediteur}, développeur web freelance, dira au téléphone à {nom} (secteur : « {secteur} », {ville}). C'est un premier appel de prospection, à une personne qu'il ne connaît pas.

Ce que tu sais de cet établissement :
- Site : {site}
- Problèmes constatés :
{problemes}

{angle}

Écris le script en 4 lignes, chacune commençant par son titre :
Accroche : « Bonjour, je suis {expediteur}, développeur web. » puis une phrase courte pour dire que tu appelles à propos de leur présence sur internet, et demande poliment à parler à la personne qui s'en occupe.
Constat : une ou deux phrases sur UN problème réel parmi ceux listés, avec ce que ça change concrètement pour leurs clients, sans l'exagérer.
Question : une question ouverte qui propose un échange de dix minutes, sans pression.
Si ce n'est pas le bon moment : une phrase pour proposer de rappeler quand ça l'arrange.

Règles : langage parlé, phrases de 15 mots au plus, aucun jargon technique, aucun mot anglais, aucun mot de vente. Ne dis jamais le prénom de la personne appelée. Ne te donne aucun autre titre que « développeur web ». N'invente aucun chiffre, aucun client, aucune référence. N'affirme rien qui ne soit pas dans les problèmes listés. Aucun lien. Écris uniquement ces 4 lignes."""

# Message à coller dans le formulaire de contact du site d'un prospect (canal « Formulaires »). Même esprit que
# l'email : court, aéré, simple, honnête. Variables : {expediteur} {nom} {ville} {secteur} {site} {problemes} {score}
# {angle} {signature}.
_PROMPT_FORMULAIRE = """Tu es {expediteur}, développeur web freelance. Tu écris en français le message que tu vas coller dans le formulaire de contact du site de {nom}, établissement du secteur « {secteur} » situé à {ville}. La personne qui le lira ne te connaît pas. Écris comme une vraie personne polie, pas comme une entreprise qui prospecte.

Ce que tu sais de cet établissement :
- Site : {site}
- Problèmes constatés :
{problemes}
- Fiche Google : {reputation}

Règles impératives :
- Entre 45 et 70 mots au total.
- Écris 3 ou 4 paragraphes très courts (1 ou 2 phrases chacun), séparés par une ligne vide. Chaque phrase fait 15 mots au maximum.
- Écris à la première personne (« je »), au vouvoiement. Jamais « nous », jamais « notre équipe ».
- Après la salutation, dis en une phrase simple qui tu es et que tu as trouvé leur établissement sur Google (c'est vrai). N'invente aucune histoire (« je passais devant », « on m'a parlé de vous »).
- Si la fiche Google indique une note d'au moins 4,3 sur 5 avec au moins 20 avis, dis-le en une phrase naturelle, comme à l'oral, avec la vraie note et le vrai nombre d'avis (par exemple « Vos 100 avis à 4,9 sur 5 parlent pour vous » ou « 4,9 sur 5 sur 100 avis : vos clients vous apprécient »). Varie la formulation, et n'écris jamais « c'est beau », « votre fiche Google indique » ni « grâce à ». Sinon, ne fais aucun compliment : jamais de flatterie générique.
- Fais ensuite UNE observation sur ce que tu as constaté (un seul des problèmes listés), dite calmement, comme un constat entre voisins. N'écris aucune phrase sur les conséquences du problème (jamais de clients qui partent, abandonnent ou vont chez un concurrent, jamais de concurrence) : dis plutôt, en quelques mots, que c'est un point simple à régler. N'affirme rien d'autre sur leur établissement.
- Langage de tous les jours : aucun terme technique, aucun mot anglais.
- Aucun mot de vente (gratuit, offre, promotion, remise, garantie, urgent, opportunité, améliorer, booster, optimiser, visibilité, solution, performant, digital), aucun point d'exclamation, aucun emoji, aucun lien. Ne cite aucun prix.
- Termine par UNE question facile, à laquelle on répond par oui ou non, qui ne demande aucun engagement. Elle ne contient jamais « discuter », « échanger », « rendez-vous », « appel » ni « oui ou non ».
- Commence par « Bonjour, » (jamais un prénom). Demande poliment, à la fin, de transmettre ce message à la personne qui s'occupe du site.
- N'invente jamais un chiffre, un client ou une référence.
- Écris uniquement le message, sans objet, puis termine par cette signature, telle quelle :
{signature}

{angle}"""

# Message privé à envoyer à la main sur Facebook, Instagram… (canal « Messages privés »). Variables : {expediteur} {nom}
# {ville} {secteur} {site} {problemes} {score} {angle} {signature}.
_PROMPT_SOCIAL = """Tu es {expediteur}, développeur web freelance. Tu écris en français le message privé que tu vas envoyer sur Facebook ou Instagram à {nom}, établissement du secteur « {secteur} » situé à {ville}. La personne qui le lira ne te connaît pas et le lira sur son téléphone. Écris comme une vraie personne polie, pas comme une entreprise qui prospecte.

Ce que tu sais de cet établissement :
- Site : {site}
- Problèmes constatés :
{problemes}
- Fiche Google : {reputation}

Règles impératives :
- Entre 40 et 65 mots au total.
- Écris 3 ou 4 paragraphes très courts (1 ou 2 phrases chacun), séparés par une ligne vide. Chaque phrase fait 15 mots au maximum.
- Écris à la première personne (« je »), au vouvoiement. Jamais « nous », jamais « notre équipe ».
- Après la salutation, dis en une phrase simple qui tu es et que tu as trouvé leur établissement sur Google (c'est vrai). N'invente aucune histoire (« je passais devant », « on m'a parlé de vous »).
- Si la fiche Google indique une note d'au moins 4,3 sur 5 avec au moins 20 avis, dis-le en une phrase naturelle, comme à l'oral, avec la vraie note et le vrai nombre d'avis (par exemple « Vos 100 avis à 4,9 sur 5 parlent pour vous » ou « 4,9 sur 5 sur 100 avis : vos clients vous apprécient »). Varie la formulation, et n'écris jamais « c'est beau », « votre fiche Google indique » ni « grâce à ». Sinon, ne fais aucun compliment : jamais de flatterie générique.
- Fais ensuite UNE observation sur ce que tu as constaté (un seul des problèmes listés), dite calmement, comme un constat entre voisins. N'écris aucune phrase sur les conséquences du problème (jamais de clients qui partent, abandonnent ou vont chez un concurrent, jamais de concurrence) : dis plutôt, en quelques mots, que c'est un point simple à régler. N'affirme rien d'autre sur leur établissement.
- Langage de tous les jours : aucun terme technique, aucun mot anglais.
- Aucun mot de vente (gratuit, offre, promotion, remise, garantie, urgent, opportunité, améliorer, booster, optimiser, visibilité, solution, performant, digital), aucun point d'exclamation, aucun emoji, aucun lien. Ne cite aucun prix.
- Termine par UNE question facile, à laquelle on répond par oui ou non, qui ne demande aucun engagement. Elle ne contient jamais « discuter », « échanger », « rendez-vous », « appel » ni « oui ou non ».
- Commence par « Bonjour, » (jamais un prénom).
- N'invente jamais un chiffre, un client ou une référence.
- Écris uniquement le message, sans objet, puis termine par cette signature, telle quelle :
{signature}

{angle}"""

REGLAGES = [
    # Services
    ("sourcing_provider", "auto", 0),          # auto | google | osm
    ("google_places_key", "", 1),
    ("ai_endpoint", "", 0),
    ("ai_model", "", 0),
    ("ai_key", "", 1),
    ("brevo_key", "", 1),
    # Boîte de réception où arrivent les réponses (lue en lecture seule, jamais modifiée) : détecte qui a répondu
    ("imap_serveur", "", 0),
    ("imap_port", "993", 0),
    ("imap_utilisateur", "", 0),
    ("imap_mot_de_passe", "", 1),
    ("imap_dossier", "INBOX", 0),
    # Canal téléphone : affiché dans le menu seulement si tu peux appeler (oui | non)
    ("canal_appels", "non", 0),
    # Identité d'expédition
    ("expediteur_nom", "", 0),
    ("expediteur_email", "gill-christ@gdjahoui.com", 0),
    ("reponse_email", "", 0),
    ("signature_defaut", "", 0),
    # Ton propre site. Il n'est JAMAIS dans le texte généré par l'IA (pas de lien dans un premier message, pour la
    # délivrabilité) : l'envoi peut l'ajouter sous la signature, selon ce réglage : jamais | relances | toujours.
    ("mon_site", "", 0),
    ("site_dans_signature", "relances", 0),
    ("mention_desinscription",
     "Si vous ne souhaitez pas être recontacté, répondez STOP à ce message.", 0),
    # Limites
    ("plafond_jour", "20", 0),
    # MODE ESSAI : si la liste n'est pas vide, un email ne peut partir QUE vers ces adresses (ou domaines). C'est une
    # restriction, jamais un contournement : l'approbation humaine reste exigée. Vide = aucune restriction.
    ("envoi_adresses_autorisees", json.dumps([]), 0),
    ("pause_envoi_min_s", "30", 0),
    ("pause_envoi_max_s", "60", 0),
    ("pause_ia_s", "2", 0),
    ("timeout_ia_s", "60", 0),
    # Consignes ajoutées au prompt de la campagne pour une relance. Variables : celles du prompt, plus
    # {numero} (n° de la relance), {jours} (jours depuis le dernier envoi), {message_precedent}.
    ("prompt_relance",
     "Il s'agit de la relance n°{numero} d'un email envoyé il y a {jours} jours, resté sans réponse. "
     "Voici le message précédent :\n---\n{message_precedent}\n---\n"
     "Rédige une relance très courte (moins de 60 mots), polie, qui ne répète pas le message précédent mot pour mot, "
     "qui rappelle en une phrase le problème constaté et qui se termine par une question simple. "
     "Ne t'excuse pas de relancer. Les autres règles restent valables (aucun lien, aucun chiffre inventé).", 0),
    ("mots_max_message", "120", 0),
    ("score_min_generation", "5", 0),
    ("delai_relance_jours", "5", 0),
    ("max_relances", "2", 0),
    ("plafond_resultats_scan", "60", 0),
    # Temps et réseau
    ("fuseau", "Europe/Paris", 0),
    ("session_heures", "12", 0),
    # Analyses et décisions : seuils de fiabilité et de réputation
    ("analyses_echantillon_min", "20", 0),     # contacts minimum pour tirer une conclusion globale
    ("analyses_echantillon_groupe", "10", 0),  # contacts minimum dans un groupe (ville, problème, angle…) pour le comparer
    ("analyses_seuil_rebond_pct", "3", 0),     # au-delà, alerte réputation
    ("analyses_seuil_ouverture_pct", "25", 0), # en dessous, alerte objet / spam
    ("analyses_tranches_score", json.dumps([5, 8, 11]), 0),
    ("montee_plafonds", json.dumps([20, 35, 50, 80]), 0),  # plafond conseillé par semaine depuis le 1er envoi
    ("dkim_selecteurs", json.dumps(["brevo1", "brevo2", "mail"]), 0),
    ("sauvegardes_max", "10", 0),
    # Assistant de niche : limites de la couverture automatique d'une ville et consignes envoyées à l'IA
    ("niche_zones_max", "30", 0),          # zones au maximum proposées par l'assistant
    ("niche_distance_max_km", "40", 0),    # une zone géocodée plus loin que ça de la ville principale est un homonyme : écartée
    ("prompt_niche", _PROMPT_NICHE, 0),
    ("prompt_appel", _PROMPT_APPEL, 0),
    ("prompt_formulaire", _PROMPT_FORMULAIRE, 0),
    ("prompt_social", _PROMPT_SOCIAL, 0),
    # Mots qu'un message ne doit jamais contenir (vente, menace) : contrôlés après la rédaction, une correction est demandée à l'IA.
    # Début de mot, sans casse : « concurren » couvre concurrent et concurrence.
    ("mots_interdits_message", json.dumps([
        "visibilit", "concurren", "gratuit", "offre ", "offres", "promotion", "remise", "garantie", "urgen", "opportunit",
        "améliorer", "amélioration", "booster", "optimis", "solution", "performan", "digital", "discuter", "échanger",
        "perdre", "perdez", "abandonn", "rentabilis", "boost", "leader", "incontournable",
    ], ensure_ascii=False), 0),
    # Réseaux sociaux reconnus : un « site » qui en est un est le profil du commerce (canal Messages privés), et les liens
    # vers ces réseaux trouvés sur un site sont retenus comme profil.
    ("domaines_reseaux", json.dumps(["facebook.com", "fb.com", "instagram.com"]), 0),
    ("prompt_analyste",
     "Tu es un analyste de prospection B2B par email, direct et honnête. On te donne des statistiques AGRÉGÉES (aucun nom de "
     "prospect) sur une activité de prospection menée par un développeur web freelance auprès de petites entreprises. "
     "Réponds en français, de façon concise :\n"
     "1. Diagnostic : 4 à 6 lignes sur ce que disent les chiffres.\n"
     "2. Les 3 décisions les plus utiles à prendre maintenant, chacune avec le chiffre précis qui la justifie.\n"
     "3. Ce qu'il ne faut PAS conclure (échantillons trop petits, ouvertures non fiables, corrélation n'est pas causalité).\n"
     "4. Une expérience à tester la semaine prochaine (une seule variable changée).\n"
     "Règles : n'invente aucun chiffre ; si un échantillon est inférieur à quelques dizaines de contacts, dis que c'est insuffisant ; "
     "les réponses comptent plus que les ouvertures ; reste sur le périmètre des données fournies.", 0),
    ("http_timeout_s", "10", 0),
    # Seuils d'analyse
    ("seuil_lenteur_s", "3", 0),
    ("seuil_avis_min", "10", 0),
    ("mots_cles_rdv", json.dumps([
        "rendez-vous", "rdv", "réserver", "réservation", "doctolib", "resalib",
        "calendly", "appointment", "book online", "afspraak", "termin", "cita",
    ], ensure_ascii=False), 0),
    ("titres_generiques", json.dumps([
        "accueil", "home", "bienvenue", "welcome", "index", "untitled", "sans titre",
        "page d'accueil", "just another wordpress site",
    ], ensure_ascii=False), 0),
    # Sourcing OpenStreetMap et recherche d'emails
    ("overpass_urls", json.dumps([
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
    ]), 0),
    # Sources dont l'absence de site web signifie vraiment « pas de site ». Pour les autres (OpenStreetMap,
    # import CSV), un site vide veut dire « inconnu » : aucun point « sans_site », à vérifier à la main.
    # Garde-fou : nombre maximal de requêtes Google Places par mois (le palier gratuit « Enterprise » est de 1 000).
    # 0 = pas de limite. Au-delà, l'application cesse d'appeler Google (et passe sur OpenStreetMap en mode automatique).
    ("google_requetes_mois_max", "900", 0),
    ("sources_site_fiable", json.dumps(["google_places"]), 0),
    # Domaines de plateformes et d'annuaires : un « site » qui en est un n'est pas un site propre. Il n'est ni
    # analysé, ni parcouru pour y chercher un email, ni utilisé pour le dédoublonnage par domaine.
    ("domaines_plateformes", json.dumps([
        "doctolib.fr", "doctolib.com", "facebook.com", "fb.com", "instagram.com", "linkedin.com", "twitter.com",
        "x.com", "pagesjaunes.fr", "annuaire-sante.fr", "resalib.fr", "maiia.com", "keldoc.com", "linktr.ee",
        "g.page", "business.site", "google.com", "yelp.com", "tripadvisor.fr", "tripadvisor.com", "118712.fr",
        "societe.com", "pappers.fr", "tiktok.com", "youtube.com", "infodentiste.fr",
    ]), 0),
    # Import CSV : si le fichier porte un statut de validation des emails, seuls ces statuts (sans casse) gardent
    # l'adresse ; les autres établissements sont importés sans email. Liste vide = tout garder.
    ("import_statuts_email_acceptes", json.dumps(["receiving", "valid", "deliverable", "ok"]), 0),
    # Mise en forme des emails : « html » = le texte approuvé mis en page + sa version texte ; « texte » = texte brut seul.
    ("email_format", "html", 0),
    ("email_couleur_accent", "#1f6feb", 0),
    ("email_style", "elegant", 0),               # elegant | sobre : voir core/courriel.py
    ("modele_email_elegant", _MODELE_EMAIL_ELEGANT, 0),
    ("modele_email_html", _MODELE_EMAIL_HTML, 0),
    ("pause_requetes_s", "2", 0),
    ("timeout_overpass_s", "90", 0),
    # Attente avant de réessayer un serveur OpenStreetMap qui a rejeté la requête (saturé). 0 = pas de seconde passe.
    ("overpass_nouvelle_tentative_s", "45", 0),
    ("pause_pages_s", "1", 0),
    # « mots-clés séparés par | = étiquettes OSM séparées par des virgules ». « nom » ajoute une
    # recherche par nom d'établissement. Sans règle correspondante, la requête est cherchée par nom.
    ("osm_correspondances", json.dumps([
        "dentiste|dentaire|dentist = amenity=dentist, healthcare=dentist",
        "avocat|lawyer|juridique = office=lawyer",
        "esthetique|aesthetic|dermatolog|chirurgie plastique = "
        "healthcare:speciality=plastic_surgery|dermatology, nom",
    ], ensure_ascii=False), 0),
    ("emails_exclus", json.dumps([
        "wixsite", "wix.com", "no-reply", "noreply", "donotreply", "example", "sentry",
        "votreadresse", "votre@", "nom@", "name@", "email@email", "test@", "domain.com",
    ]), 0),
    ("pages_contact", json.dumps([
        "/contact", "/contactez-nous", "/nous-contacter", "/mentions-legales",
    ]), 0),
    # Liens de la page d'accueil suivis pour trouver la page de contact (texte ou adresse du lien contient l'un de ces mots)
    ("mots_cles_contact", json.dumps(["contact", "ecrire", "ecrivez"]), 0),
    ("contact_liens_max", "3", 0),
    # Services de formulaires intégrés dans une page (cadre iframe) : un formulaire de contact à part entière
    ("domaines_formulaires", json.dumps([
        "typeform.com", "docs.google.com/forms", "forms.gle", "jotform.com", "forms.office.com", "hsforms.com", "tally.so",
    ]), 0),
    # Dédoublonnage et adresses
    ("mentions_juridiques", json.dumps([
        "sarl", "sas", "sasu", "eurl", "selarl", "selas", "scp", "sci", "sa", "sel",
        "scm", "ei", "gmbh", "ltd", "bv", "srl",
    ]), 0),
    ("domaines_generiques", json.dumps([
        "gmail.com", "googlemail.com", "hotmail.com", "hotmail.fr", "hotmail.it",
        "outlook.com", "outlook.fr", "live.com", "live.fr", "msn.com", "yahoo.com",
        "yahoo.fr", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "neuf.fr",
        "bbox.fr", "laposte.net", "club-internet.fr", "numericable.fr", "icloud.com",
        "me.com", "aol.com", "gmx.com", "gmx.fr", "gmx.de", "web.de", "t-online.de",
        "libero.it", "protonmail.com", "proton.me",
    ]), 0),
]

# --- Critères globaux (campagne_id NULL) : (code, libellé, points) -------------------

CRITERES_GLOBAUX = [
    ("sans_site", "Aucun site web", 10),
    ("site_casse", "Site inaccessible ou en erreur", 9),
    ("titre_vide", "Page d'accueil sans titre pertinent", 4),
    ("pas_rdv", "Pas de prise de rendez-vous en ligne", 3),
    ("lent", "Site lent à charger", 3),
    ("pas_mobile", "Site non adapté au téléphone", 3),
    ("pas_https", "Site non sécurisé (pas de HTTPS)", 2),
    ("peu_avis", "Peu d'avis Google", 2),
    ("pas_h1", "Aucun titre principal (H1) sur la page", 1),
    ("description_vide", "Pas de description pour les moteurs de recherche", 1),
    # Diagnostics à 0 point : ils s'affichent mais ne comptent pas dans le score tant que tu ne leur donnes pas de poids.
    ("site_plateforme", "Pas de site propre : simple page sur une plateforme (Doctolib, Facebook…)", 6),
    ("site_inconnu", "Site web non renseigné dans la source (à vérifier)", 0),
    ("non_analysable", "Site non analysable (accès refusé, robots.txt, délai dépassé)", 0),
]

# --- Modèles de campagne -------------------------------------------------------------

_PROMPT_COMMUN = """Tu es {expediteur}, développeur web freelance. Tu écris en français le premier message destiné à {nom}, établissement du secteur « {secteur} » situé à {ville}. Tu écris comme une vraie personne polie qui écrit à une autre, pas comme une entreprise qui prospecte : le message doit pouvoir être lu à voix haute sans gêne.

Ce que tu sais de cet établissement :
- Site : {site}
- Problèmes constatés : {problemes}
- Fiche Google : {reputation}
- Score de besoin : {score} sur 10

Règles impératives :
- Entre 45 et 70 mots au total.
- Écris 3 ou 4 paragraphes très courts (1 ou 2 phrases chacun), séparés par une ligne vide : le message doit être aéré. Chaque phrase fait 15 mots au maximum.
- Écris à la première personne (« je »), au vouvoiement. Jamais « nous », jamais « notre équipe ».
- Après la salutation, dis en une phrase simple qui tu es et que tu as trouvé leur établissement sur Google (c'est vrai). N'invente aucune histoire (« je passais devant », « on m'a parlé de vous »).
- Si la fiche Google indique une note d'au moins 4,3 sur 5 avec au moins 20 avis, dis-le en une phrase naturelle, comme à l'oral, avec la vraie note et le vrai nombre d'avis (par exemple « Vos 100 avis à 4,9 sur 5 parlent pour vous » ou « 4,9 sur 5 sur 100 avis : vos clients vous apprécient »). Varie la formulation, et n'écris jamais « c'est beau », « votre fiche Google indique » ni « grâce à ». Sinon, ne fais aucun compliment : jamais de flatterie générique.
- Fais ensuite UNE observation sur ce que tu as constaté (un seul des problèmes listés), dite calmement, comme un constat entre voisins. N'écris aucune phrase sur les conséquences du problème (jamais de clients qui partent, abandonnent ou vont chez un concurrent, jamais de concurrence) : dis plutôt, en quelques mots, que c'est un point simple à régler.
- Langage de tous les jours, comme à voix haute : aucun terme technique (H1, balise, SEO, HTTPS, référencement, viewport…), aucun mot anglais.
- Aucun mot de vente (gratuit, offre, promotion, remise, garantie, urgent, opportunité, améliorer, booster, optimiser, visibilité, solution, performant, digital), aucun point d'exclamation, aucune majuscule d'insistance, aucun emoji.
- Aucun lien, aucune adresse web dans le message.
- Mentionne UN problème précis et réel parmi ceux listés ci-dessus, jamais une généralité.
- Parle de ce que le problème peut changer au conditionnel (« peut », « risque »), jamais comme un fait mesuré : tu ne connais ni son classement sur Google ni ses clients.
- Les avis Google sont ceux de la fiche Google de l'établissement, jamais ceux de son site.
- N'affirme jamais qu'un service existe (prise de rendez-vous en ligne, paiement…) : appuie-toi uniquement sur les problèmes listés.
- Termine par UNE question facile, à laquelle on répond par oui ou non, qui ne demande aucun engagement. Elle ne contient jamais « discuter », « échanger », « rendez-vous », « appel » ni « oui ou non ».
- {regle_salutation}
- N'invente jamais un chiffre, un client ou une référence.
- Écris uniquement le corps du message, sans objet, puis termine par cette signature, telle quelle :
{signature}

{angle}"""

# Règle de salutation : jamais de prénom (il peut être mal orthographié ou mal genré dans les sources).
REGLE_SALUTATION_DEFAUT = ("Salue par « Bonjour Docteur, » si le nom commence par Dr ou Docteur, sinon par « Bonjour, ». "
                           "N'emploie jamais un prénom : il peut être mal orthographié ou mal genré dans nos sources.")
REGLE_SALUTATION_NEUTRE = ("Salue toujours par « Bonjour, ». "
                           "N'emploie jamais un prénom : il peut être mal orthographié ou mal genré dans nos sources.")
REGLE_SALUTATION_AVOCATS = ("Salue par « Bonjour Maître, » si le nom commence par Me ou Maître, sinon par « Bonjour, ». "
                            "N'emploie jamais un prénom : il peut être mal orthographié ou mal genré dans nos sources.")


def composer_prompt(angle, regle_salutation=REGLE_SALUTATION_DEFAUT):
    """Prompt complet d'un angle de message : les règles communes (style, honnêteté) + l'angle propre à la niche."""
    return _PROMPT_COMMUN.replace("{regle_salutation}", regle_salutation).replace("{angle}", angle)

_ANGLE_ESTHETIQUE = (
    "Angle : la communication d'un médecin est encadrée par la déontologie et la "
    "réglementation sur la publicité. Un site sobre, clair et bien tenu permet de se "
    "présenter sans glissement promotionnel. Évoque cet enjeu sans citer de texte de loi "
    "ni de sanction, et n'affirme rien que tu ne puisses constater sur leur site."
)
_ANGLE_DENTAIRE = (
    "Angle : les patients cherchent un dentiste depuis leur téléphone et choisissent "
    "souvent celui chez qui ils peuvent obtenir un rendez-vous tout de suite. Relie le "
    "problème constaté à des demandes de rendez-vous qui peuvent se perdre."
)
_ANGLE_AVOCATS = (
    "Angle : un client potentiel tape un domaine du droit et sa ville dans Google. "
    "Relie le problème constaté à la visibilité locale du cabinet. Ne promets aucun "
    "résultat et ne parle pas de prise de rendez-vous en ligne."
)

# (nom du modèle de message, gabarit d'objet, angle, nom de campagne, secteur, requêtes)
MODELES_CAMPAGNE = [
    (
        "Angle déontologie — médecins", "{nom} : votre présence en ligne", _ANGLE_ESTHETIQUE,
        "Modèle — Cliniques esthétiques", "clinique esthétique",
        ["clinique esthétique", "médecine esthétique", "médecin esthétique"],
    ),
    (
        "Angle prise de rendez-vous — dentistes", "{nom} : vos patients vous trouvent-ils ?",
        _ANGLE_DENTAIRE,
        "Modèle — Cabinets dentaires", "cabinet dentaire",
        ["cabinet dentaire", "chirurgien-dentiste", "dentiste"],
    ),
    (
        "Angle visibilité locale — avocats", "{nom} : être trouvé à {ville}", _ANGLE_AVOCATS,
        "Modèle — Cabinets d'avocats", "cabinet d'avocats",
        ["cabinet d'avocats", "avocat"],
    ),
]


# --- Catalogue d'angles de message ------------------------------------------------------
# Un angle = ce qui change d'un message à l'autre (le problème mis en avant, la façon de le relier aux clients de
# l'établissement). Les règles de style et d'honnêteté, communes, sont ajoutées par composer_prompt. Une campagne
# choisit un angle dans sa fiche (liste déroulante) et peut le modifier ou en créer un à elle.
# (nom, objet, salutation : aucun | docteur | maitre, angle)
ANGLE_PAR_DEFAUT = "Général — être trouvé sur Google"
ANGLES_CATALOGUE = [
    (ANGLE_PAR_DEFAUT, "{nom} : vos clients vous trouvent-ils ?", "aucun",
     "Angle : les gens cherchent ce service sur Google, souvent depuis leur téléphone, puis appellent l'un des premiers "
     "résultats. Relie le problème constaté au fait que des clients peuvent ne pas aller jusqu'à cet établissement."),
    ("Pas de site à soi", "{nom} : une question sur votre présence en ligne", "aucun",
     "Angle : cet établissement n'a pas de site à son nom. Si « Aucun site web » figure dans la liste, c'est le problème à "
     "citer : sans site à soi, les clients ne trouvent pas facilement les horaires, les services et un moyen de contact. "
     "Ne critique jamais un annuaire ou une plateforme."),
    ("Seulement une page sur une plateforme", "{nom} : votre page en ligne", "aucun",
     "Angle : cet établissement n'a qu'une page sur une plateforme (annuaire, réseau social, prise de rendez-vous), pas "
     "de site à lui. Si « Pas de site propre » figure dans la liste, c'est le problème à citer : la page appartient à la "
     "plateforme, qui décide de ce qui s'affiche. Reste respectueux de la plateforme."),
    ("Site illisible sur téléphone", "{nom} : votre site sur téléphone", "aucun",
     "Angle : beaucoup de gens consultent un site depuis leur téléphone. Si « Site non adapté au téléphone » figure dans "
     "la liste, c'est le problème à citer : le visiteur doit zoomer, se perd, et quitte la page. Ne dis jamais que tu as "
     "toi-même testé le site : appuie-toi seulement sur le problème listé."),
    ("Prise de rendez-vous ou de contact", "{nom} : joindre votre établissement", "aucun",
     "Angle : un client qui hésite prend contact avec celui qu'il peut joindre tout de suite. Si « Pas de prise de "
     "rendez-vous en ligne » figure dans la liste, c'est le problème à citer : des demandes se perdent faute de moyen "
     "simple de réserver ou d'écrire. N'affirme pas que le site n'a aucun moyen de contact : dis seulement que la prise "
     "de rendez-vous en ligne n'a pas été trouvée."),
    ("Peu d'avis Google", "{nom} : vos avis en ligne", "aucun",
     "Angle : avant de choisir, beaucoup de gens lisent les avis de la fiche Google. Si « Peu d'avis Google » figure dans "
     "la liste, c'est le problème à citer : avec peu d'avis, l'établissement inspire moins confiance qu'un voisin mieux "
     "noté. Il s'agit de la fiche Google, jamais du site. Ne parle pas de la note, seulement du nombre d'avis."),
    ("Site lent", "{nom} : la vitesse de votre site", "aucun",
     "Angle : un site qui met longtemps à s'afficher fait partir les visiteurs avant qu'ils aient lu quoi que ce soit. Si "
     "« Site lent à charger » figure dans la liste, c'est le problème à citer. Reste concret : le visiteur attend, puis "
     "quitte la page."),
    ("Site signalé « non sécurisé »", "{nom} : le cadenas de votre site", "aucun",
     "Angle : quand un site n'est pas sécurisé, le navigateur affiche un avertissement qui rend les visiteurs méfiants. "
     "Si « Site non sécurisé » figure dans la liste, c'est le problème à citer. Dis-le simplement, sans parler de "
     "technique, et sans faire peur."),
    ("Visibilité sur Google (titre et description)", "{nom} : comment Google vous présente", "aucun",
     "Angle : la façon dont l'établissement apparaît dans les résultats de Google (le titre et la petite description) "
     "décide si on clique ou non. Si « Page d'accueil sans titre pertinent » ou « Pas de description » figure dans la "
     "liste, c'est le problème à citer. Reste simple : on ne comprend pas tout de suite ce que fait l'établissement."),
    ("Site inaccessible", "{nom} : votre site ne s'ouvre pas", "aucun",
     "Angle : le site de cet établissement ne s'ouvre pas correctement. Si « Site inaccessible ou en erreur » figure dans "
     "la liste, c'est le problème à citer. Écris avec bienveillance : la personne ne le sait peut-être pas. Ne donne "
     "aucune cause technique."),
    ("Commerces de proximité", "{nom} : vos horaires en ligne", "aucun",
     "Angle : aujourd'hui, avant de pousser la porte d'un commerce, on regarde sur son téléphone les horaires, "
     "l'adresse et quelques photos. Fais simplement le constat du problème, sans conséquence dramatique. Pour la "
     "question finale, demande si la personne serait d'accord que tu lui montres un petit exemple de ce que ça "
     "pourrait donner pour son commerce."),
    ("Artisans et services à domicile", "{nom} : vos demandes de devis", "aucun",
     "Angle : quelqu'un qui cherche un artisan appelle souvent le premier qui inspire confiance. Relie le problème "
     "constaté à des demandes de devis qui vont chez un concurrent mieux présenté. Ne promets aucun résultat."),
    ("Conseil, comptabilité et services aux entreprises", "{nom} : votre image en ligne", "aucun",
     "Angle : avant de contacter un prestataire, un futur client regarde son site pour juger du sérieux. Relie le "
     "problème constaté à une première impression qui peut faire hésiter. Ton posé, sans exagération."),
    ("Restaurants et hôtels", "{nom} : vos clients vous trouvent-ils ?", "aucun",
     "Angle : on choisit un restaurant ou un hôtel en quelques minutes, sur son téléphone, d'après ce qu'on voit en ligne. "
     "Relie le problème constaté à des réservations qui vont ailleurs. N'invente rien sur la cuisine ou la carte."),
    ("Avocats — visibilité locale", "{nom} : être trouvé à {ville}", "maitre",
     "Angle : un client potentiel tape un domaine du droit et sa ville dans Google. Relie le problème constaté à la "
     "visibilité locale du cabinet. Ne promets aucun résultat et ne parle pas de prise de rendez-vous en ligne."),
    ("Dentistes — rendez-vous immédiats", "{nom} : vos patients vous trouvent-ils ?", "docteur",
     "Angle : les patients cherchent un dentiste depuis leur téléphone et choisissent souvent celui chez qui ils peuvent "
     "obtenir un rendez-vous tout de suite. Relie le problème constaté à des demandes de rendez-vous qui peuvent se perdre."),
    ("Médecins — communication encadrée", "{nom} : votre présence en ligne", "docteur",
     "Angle : la communication d'un médecin est encadrée par la déontologie et la réglementation sur la publicité. Un "
     "site sobre, clair et bien tenu permet de se présenter sans glissement promotionnel. Évoque cet enjeu sans citer de "
     "texte de loi ni de sanction, et n'affirme rien que tu ne puisses constater sur leur site."),
    ("Kinés, infirmiers et professions de santé", "{nom} : vos patients vous trouvent-ils ?", "aucun",
     "Angle : un patient cherche un professionnel de santé près de chez lui et regarde d'abord les informations pratiques "
     "(adresse, horaires, comment prendre rendez-vous). Relie le problème constaté à un patient qui ne trouve pas "
     "l'information et appelle ailleurs. Ton respectueux, aucune promesse."),
]
_VERSION_CATALOGUE_ANGLES = "1"
_SALUTATIONS_CATALOGUE = {"aucun": REGLE_SALUTATION_NEUTRE, "docteur": REGLE_SALUTATION_DEFAUT, "maitre": REGLE_SALUTATION_AVOCATS}


def copier_angle_pour(conn, campagne_id, nom_angle=ANGLE_PAR_DEFAUT):
    """Rattache à la campagne une COPIE de l'angle du catalogue indiqué (la campagne peut alors le modifier sans toucher
    aux autres). Renvoie l'id de la copie, ou None si l'angle n'existe pas."""
    source = conn.execute("SELECT * FROM modeles_message WHERE nom = ? ORDER BY id LIMIT 1", (nom_angle,)).fetchone()
    if source is None:
        return None
    nom_campagne = (conn.execute("SELECT nom FROM campagnes WHERE id = ?", (campagne_id,)).fetchone() or ["campagne"])[0]
    copie = conn.execute(
        "INSERT INTO modeles_message (nom, objet_template, prompt_systeme, signature) VALUES (?, ?, ?, ?)",
        (f"{source['nom']} — {nom_campagne}"[:120], source["objet_template"], source["prompt_systeme"], source["signature"])).lastrowid
    conn.execute("UPDATE campagnes SET modele_message_id = ? WHERE id = ?", (copie, campagne_id))
    return copie


_REGLE_PROBLEME = "- Mentionne UN problème précis et réel parmi ceux listés ci-dessus, jamais une généralité.\n"
_REGLE_CONDITIONNEL = ("- Parle de ce que le problème peut changer au conditionnel (« peut », « risque »), jamais comme un fait "
                       "mesuré : tu ne connais ni son classement sur Google ni ses clients.\n")


def _completer_regles_communes(conn):
    """Ajoute aux angles déjà en base la règle d'honnêteté « conditionnel, jamais un fait mesuré » (idempotent)."""
    for ligne in conn.execute("SELECT id, prompt_systeme FROM modeles_message").fetchall():
        prompt = ligne[1]
        if _REGLE_PROBLEME in prompt and _REGLE_CONDITIONNEL not in prompt:
            conn.execute("UPDATE modeles_message SET prompt_systeme = ? WHERE id = ?",
                         (prompt.replace(_REGLE_PROBLEME, _REGLE_PROBLEME + _REGLE_CONDITIONNEL, 1), ligne[0]))


def _installer_catalogue_angles(conn):
    """Ajoute au premier lancement qui suit une nouvelle version du catalogue les angles qui manquent (par nom), puis donne
    l'angle par défaut aux campagnes qui n'en ont aucun. Un angle que tu supprimes ensuite ne revient pas."""
    ligne = conn.execute("SELECT valeur FROM reglages WHERE cle = 'angles_catalogue_version'").fetchone()
    if ligne and ligne[0] == _VERSION_CATALOGUE_ANGLES:
        return
    for nom, objet, salutation, angle in ANGLES_CATALOGUE:
        if conn.execute("SELECT 1 FROM modeles_message WHERE nom = ?", (nom,)).fetchone() is None:
            conn.execute("INSERT INTO modeles_message (nom, objet_template, prompt_systeme, signature) VALUES (?, ?, ?, '')",
                         (nom, objet, composer_prompt(angle, _SALUTATIONS_CATALOGUE[salutation])))
    for sans_angle in conn.execute("SELECT id FROM campagnes WHERE modele_message_id IS NULL AND est_modele = 0").fetchall():
        copier_angle_pour(conn, sans_angle[0])
    conn.execute("INSERT INTO reglages (cle, valeur, secret) VALUES ('angles_catalogue_version', ?, 0) "
                 "ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur", (_VERSION_CATALOGUE_ANGLES,))


_ANCIENNES_REGLES_QUESTION = ("Termine par une question courte et ouverte", "Termine par UNE question facile")
_MARQUE_A_JOUR = "parlent pour vous"  # présent dans la version actuelle des consignes


def _a_moderniser(prompt):
    return any(r in prompt for r in _ANCIENNES_REGLES_QUESTION) and _MARQUE_A_JOUR not in prompt
_REGLES_SALUTATION = (REGLE_SALUTATION_DEFAUT, REGLE_SALUTATION_NEUTRE, REGLE_SALUTATION_AVOCATS)


def _moderniser_prompts(conn):
    """Remplace par les consignes « écrites par un humain » les prompts de message qui portent encore l'ancienne règle
    (question « courte et ouverte »). L'angle propre à la niche et la règle de salutation sont conservés ; un prompt que tu as
    réécrit à la main, sans cette règle, n'est pas touché. Idempotent."""
    for ligne in conn.execute("SELECT id, prompt_systeme FROM modeles_message").fetchall():
        prompt = ligne[1]
        if not _a_moderniser(prompt) or "Angle :" not in prompt:
            continue
        angle = prompt[prompt.rindex("Angle :"):].strip()
        salutation = next((r for r in _REGLES_SALUTATION if r in prompt), REGLE_SALUTATION_DEFAUT)
        conn.execute("UPDATE modeles_message SET prompt_systeme = ? WHERE id = ?", (composer_prompt(angle, salutation), ligne[0]))
    for cle, neuf in (("prompt_formulaire", _PROMPT_FORMULAIRE), ("prompt_social", _PROMPT_SOCIAL)):
        ligne = conn.execute("SELECT valeur FROM reglages WHERE cle = ?", (cle,)).fetchone()
        if ligne and ligne[0] and _a_moderniser(ligne[0]):
            conn.execute("UPDATE reglages SET valeur = ? WHERE cle = ?", (neuf, cle))


def appliquer_seeds(conn):
    """Insère les données de départ si elles ne l'ont jamais été. Renvoie True si appliqué."""
    # Les réglages manquants (ajoutés par une version plus récente) sont complétés à chaque
    # lancement ; ceux qui existent déjà, y compris modifiés par l'utilisateur, ne sont jamais touchés.
    conn.executemany(
        "INSERT OR IGNORE INTO reglages (cle, valeur, secret) VALUES (?, ?, ?)", REGLAGES
    )
    # Idem pour les critères globaux : ceux qui manquent sont ajoutés, les existants ne sont jamais modifiés.
    conn.executemany(
        "INSERT INTO criteres (campagne_id, code, libelle, points, actif) VALUES (NULL, ?, ?, ?, 1) "
        "ON CONFLICT DO NOTHING", CRITERES_GLOBAUX)
    for ancien in _MODELES_EMAIL_ANCIENS:  # un modèle resté celui d'une ancienne version par défaut est mis à jour
        conn.execute("UPDATE reglages SET valeur = ? WHERE cle = 'modele_email_html' AND valeur = ?",
                     (_MODELE_EMAIL_HTML, ancien))
    _installer_catalogue_angles(conn)
    _completer_regles_communes(conn)
    _moderniser_prompts(conn)
    deja = conn.execute(
        "SELECT valeur FROM reglages WHERE cle = 'seeds_appliques'"
    ).fetchone()
    if deja and deja[0] == "1":
        return False

    for nom_modele, objet, angle, nom_camp, secteur, requetes in MODELES_CAMPAGNE:
        curseur = conn.execute(
            "INSERT INTO modeles_message (nom, objet_template, prompt_systeme, signature) "
            "VALUES (?, ?, ?, '')",
            (nom_modele, objet, composer_prompt(
                angle, REGLE_SALUTATION_AVOCATS if "avocats" in nom_camp else REGLE_SALUTATION_DEFAUT)),
        )
        conn.execute(
            "INSERT INTO campagnes (nom, secteur, requetes, pays, langue, "
            "modele_message_id, statut, est_modele) VALUES (?, ?, ?, 'FR', 'fr', ?, 'brouillon', 1)",
            (nom_camp, secteur, json.dumps(requetes, ensure_ascii=False), curseur.lastrowid),
        )
    conn.execute(
        "INSERT INTO reglages (cle, valeur, secret) VALUES ('seeds_appliques', '1', 0) "
        "ON CONFLICT(cle) DO UPDATE SET valeur = '1'"
    )
    return True


if __name__ == "__main__":
    from core import db

    print(db.init_db())
