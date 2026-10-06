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
_PROMPT_FORMULAIRE = """Tu es {expediteur}, développeur web freelance. Tu écris en français le message que tu vas coller dans le formulaire de contact du site de {nom}, établissement du secteur « {secteur} » situé à {ville}. La personne qui le lira ne te connaît pas.

Ce que tu sais de cet établissement :
- Site : {site}
- Problèmes constatés :
{problemes}

Règles impératives :
- Entre 45 et 75 mots au total.
- Écris 3 ou 4 paragraphes très courts (1 ou 2 phrases chacun), séparés par une ligne vide. Chaque phrase fait 15 mots au maximum.
- Langage de tous les jours : aucun terme technique, aucun mot anglais. Dis ce que ça change concrètement pour ses clients.
- Aucun mot de vente (gratuit, offre, promotion, remise, garantie, urgent, opportunité), aucun point d'exclamation, aucun emoji, aucun lien.
- Mentionne UN problème précis et réel parmi ceux listés, jamais une généralité. N'affirme rien d'autre sur leur site.
- Commence par « Bonjour, » (jamais un prénom), dis en une phrase qui tu es, puis le constat.
- Termine par une question courte et ouverte, puis demande poliment de transmettre ce message à la personne qui s'occupe du site.
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

_PROMPT_COMMUN = """Tu es {expediteur}, développeur web freelance. Tu écris en français le premier email de prospection destiné à {nom}, établissement du secteur « {secteur} » situé à {ville}.

Ce que tu sais de cet établissement :
- Site : {site}
- Problèmes constatés : {problemes}
- Score de besoin : {score} sur 10

Règles impératives :
- Entre 45 et 75 mots au total.
- Écris 3 ou 4 paragraphes très courts (1 ou 2 phrases chacun), séparés par une ligne vide : le message doit être aéré. Chaque phrase fait 15 mots au maximum.
- Langage de tous les jours, comme à voix haute : aucun terme technique (H1, balise, SEO, HTTPS, référencement, viewport…), aucun mot anglais. Dis plutôt ce que ça change concrètement pour ses clients (par exemple « les gens vous trouvent plus difficilement sur Google »).
- Aucun mot de vente (gratuit, offre, promotion, remise, garantie, urgent, opportunité), aucun point d'exclamation, aucune majuscule d'insistance, aucun emoji.
- Aucun lien, aucune adresse web dans le message.
- Mentionne UN problème précis et réel parmi ceux listés ci-dessus, jamais une généralité.
- Les avis Google sont ceux de la fiche Google de l'établissement, jamais ceux de son site.
- N'affirme jamais qu'un service existe (prise de rendez-vous en ligne, paiement…) : appuie-toi uniquement sur les problèmes listés.
- Termine par une question courte et ouverte, pas par une offre.
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
