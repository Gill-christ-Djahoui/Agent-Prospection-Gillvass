# Cahier des charges — Agent de prospection

Oct 2, 2026 · @Gill Christ djahoui

Document de spécification à transmettre à Claude Code pour la construction de l'application.

## Ce que fait l'outil

Une application web qui tourne en local sur l'ordinateur de l'utilisateur et qui transforme une recherche terrain en emails de prospection prêts à partir, sous contrôle humain.

L'utilisateur est un développeur web freelance qui vend des sites et du référencement local à des TPE européennes. Son travail de prospection aujourd'hui est manuel : chercher des entreprises sur Google Maps, ouvrir leur site, juger s'il est mauvais, trouver un email, écrire un message. L'outil fait les quatre premières étapes à sa place et lui laisse la décision d'envoyer.

Le cycle complet :

```
Campagne (niche + zones + critères)
        ↓
   Sourcing          → trouve les établissements dans chaque zone
        ↓
   Analyse           → ouvre chaque site, détecte les faiblesses
        ↓
   Score             → note le besoin selon des règles configurables
        ↓
   Rédaction IA      → génère un email personnalisé par prospect
        ↓
   VALIDATION HUMAINE → l'utilisateur lit, corrige, approuve
        ↓
   Envoi             → Brevo expédie depuis son adresse pro
        ↓
   Suivi             → ouvertures, réponses, relances
```

Deux exigences non négociables traversent tout le document.

**Rien ne part sans approbation.** Le statut `APPROUVE` est posé à la main par l'utilisateur, prospect par prospect. Aucun chemin de code ne doit permettre un envoi sans ce statut, y compris en mode test.

**L'outil n'est jamais figé sur une cible.** Niche, pays, villes, critères de qualification, angle du message : tout se change depuis l'interface, sans toucher au code. C'est l'objet de la section suivante.

## Le concept central : la campagne

La campagne est l'unité de travail de l'application. C'est elle qui rend l'outil réutilisable au lieu d'être un script à usage unique.

Une campagne regroupe quatre choses que l'utilisateur définit lui-même depuis l'interface :

| Composant | Ce que c'est | Exemple |
| --- | --- | --- |
| Requêtes de recherche | Les termes envoyés au moteur de sourcing | `clinique esthétique`, `médecine esthétique`, `médecin esthétique` |
| Zones | Les villes ou territoires à couvrir, autant qu'on veut | Lyon, Bordeaux, Toulouse, Nantes, Bruxelles |
| Critères de qualification | Les règles qui calculent le score de besoin, avec leur poids | Pas de site = 10 pts · Site lent = 3 pts · Pas de HTTPS = 2 pts |
| Angle du message | Le prompt qui guide l'IA pour cette cible précise | L'argument déontologie-publicité, propre aux médecins |

L'utilisateur crée autant de campagnes qu'il veut et les fait tourner en parallèle. Changer de cible ne demande aucune modification du code : il crée une campagne, il la lance.

### Pourquoi les zones sont une liste, pas un champ

C'est le point qui distingue un outil professionnel d'un script de démonstration. Une campagne porte N zones. Un lancement de scan parcourt les N zones à la suite, en file d'attente, et consolide tout dans la même table de prospects.

Concrètement, l'utilisateur lance « Cliniques esthétiques — France Sud » une fois et l'outil balaie Lyon, Marseille, Toulouse, Montpellier et Nice dans la foulée. Il ne relance pas cinq fois.

Chaque zone porte son propre état : jamais scannée, en cours, terminée le JJ/MM, nombre de prospects trouvés. L'utilisateur voit d'un coup d'œil ce qui est couvert et ce qui reste. Il peut ajouter une zone à une campagne existante et ne scanner que celle-là, sans repasser sur les anciennes.

### Dupliquer plutôt que recréer

Une campagne se duplique en un clic. L'utilisateur part de « Cliniques esthétiques — France » pour créer « Cabinets dentaires — France » : il garde les zones et les critères, il change les requêtes et l'angle du message. Trente secondes au lieu d'une reconfiguration complète.

### Une bibliothèque de départ, pas un carcan

L'application est livrée avec trois modèles de campagne préremplis — cliniques esthétiques, cabinets dentaires, cabinets d'avocats — stockés en base, pas en dur dans le code. L'utilisateur les modifie, les supprime, en ajoute. Ce sont des exemples de départ, rien d'autre.

## Architecture technique

Une application Flask qui tourne en local, base SQLite, interface HTML servie par Flask. Pas de build frontend, pas de Node, pas de Docker, pas de VPS. L'utilisateur lance un script et ouvre son navigateur sur `localhost:5000`.

Ce choix est délibéré : l'outil doit démarrer en une commande sur une machine de travail et rester réparable par une personne seule.

### Stack

| Couche | Choix | Pourquoi |
| --- | --- | --- |
| Serveur | Flask | Léger, un seul fichier d'entrée, pas de configuration |
| Base | SQLite | Un fichier, zéro serveur, sauvegarde par copier-coller |
| Interface | HTML + CSS + JavaScript natif | Pas de build, le fichier s'ouvre et se modifie directement |
| Tâches longues | `threading` + table `jobs` en base | Le scan ne bloque pas l'interface, l'avancement est lisible |
| Sourcing | Google Places API, OpenStreetMap en secours | API officielle, ne casse pas quand le HTML change |
| Analyse de sites | `requests` + `BeautifulSoup` | Suffisant pour les signaux qu'on cherche |
| IA | Adaptateur compatible OpenAI | Une seule classe sert xAI, Groq et OpenAI |
| Envoi | API Brevo | 300 emails/jour gratuits, gestion de la délivrabilité |

### Arborescence

```
prospect/
├── app.py                  # Flask : routes et démarrage
├── config.py               # Chargement .env et constantes
├── requirements.txt
├── .env.example            # Modèle, sans aucune clé réelle
├── README.md
├── run.sh / run.bat        # Lancement en une commande
├── data/
│   └── prospect.db         # SQLite, créé au premier lancement
├── core/
│   ├── db.py               # Schéma, migrations, accès
│   ├── sourcing.py         # Google Places + OpenStreetMap
│   ├── analyzer.py         # Audit technique des sites
│   ├── scorer.py           # Application des critères configurables
│   ├── writer.py           # Génération des messages par IA
│   ├── sender.py           # Envoi Brevo et journalisation
│   ├── jobs.py             # File d'attente et suivi des tâches
│   └── seeds.py            # Campagnes et critères de démarrage
├── templates/
│   ├── base.html
│   ├── campagnes.html
│   ├── prospects.html
│   ├── messages.html
│   ├── envois.html
│   └── reglages.html
└── static/
    ├── app.css
    └── app.js
```

### Règles de conception imposées

Aucune clé API n'apparaît dans le code ni dans la base en clair dans un dépôt partagé. Les clés vivent dans `.env`, et `.env` est dans `.gitignore`. Le fichier `.env.example` ne contient que des noms de variables vides.

Aucune valeur métier n'est écrite en dur : niches, villes, critères, poids, modèles de messages, quotas — tout vient de la base et se modifie dans l'interface.

Chaque module `core/` est utilisable seul, en ligne de commande, sans passer par Flask. Cela permet de tester une brique sans lancer toute l'application.

Toute opération longue écrit son avancement dans la table `jobs`. L'interface interroge cette table et affiche une barre de progression réelle, jamais une animation décorative.

## Modèle de données

Neuf tables. Le schéma porte à lui seul la promesse de reconfiguration : aucune niche, aucune ville, aucun critère n'existe ailleurs qu'en base.

### campagnes

| Colonne | Type | Rôle |
| --- | --- | --- |
| id | INTEGER PK |  |
| nom | TEXT | « Cliniques esthétiques — France Sud » |
| secteur | TEXT | Libellé de la niche, réutilisé dans les prompts |
| requetes | TEXT (JSON) | Liste des termes de recherche |
| pays | TEXT | Code pays pour biaiser le sourcing |
| langue | TEXT | Langue de rédaction des messages |
| modele\_message\_id | INTEGER FK | Angle de message rattaché |
| statut | TEXT | `brouillon` · `active` · `archivee` |
| cree\_le | DATETIME |  |

### zones

| Colonne | Type | Rôle |
| --- | --- | --- |
| id | INTEGER PK |  |
| campagne\_id | INTEGER FK |  |
| ville | TEXT | « Lyon » |
| pays | TEXT | « FR » |
| rayon\_km | INTEGER | Rayon de recherche, défaut 15 |
| statut | TEXT | `a_scanner` · `en_cours` · `termine` |
| dernier\_scan | DATETIME |  |
| nb\_trouves | INTEGER | Prospects remontés au dernier scan |

Une campagne porte autant de lignes `zones` que nécessaire. C'est la table qui permet le multi-territoire.

### criteres

| Colonne | Type | Rôle |
| --- | --- | --- |
| id | INTEGER PK |  |
| campagne\_id | INTEGER FK | NULL = critère global par défaut |
| code | TEXT | `sans_site`, `pas_https`, `lent`, `pas_mobile`, `titre_vide`, `pas_rdv`, `peu_avis` |
| libelle | TEXT | Texte affiché et injecté dans le prompt |
| points | INTEGER | Poids dans le score |
| actif | BOOLEAN | Désactivable sans suppression |

L'utilisateur ajuste les poids depuis l'interface. Un critère mis à 0 ou désactivé disparaît du calcul sans casser l'historique.

### prospects

| Colonne | Type | Rôle |
| --- | --- | --- |
| id | INTEGER PK |  |
| campagne\_id | INTEGER FK |  |
| zone\_id | INTEGER FK |  |
| nom | TEXT | Nom de l'établissement |
| adresse | TEXT |  |
| ville | TEXT |  |
| telephone | TEXT |  |
| site | TEXT | URL, NULL si absent |
| email | TEXT | NULL si introuvable |
| source | TEXT | `google_places` · `openstreetmap` · `import_csv` |
| source\_id | TEXT | Identifiant d'origine, sert au dédoublonnage |
| note\_moyenne | REAL |  |
| nb\_avis | INTEGER |  |
| problemes | TEXT (JSON) | Codes des critères déclenchés |
| score | INTEGER | Somme des points |
| statut | TEXT | `nouveau` · `qualifie` · `ignore` · `message_pret` · `approuve` · `envoye` · `repondu` |
| cree\_le | DATETIME |  |

Contrainte d'unicité sur `source_id`, et à défaut sur la paire nom + ville. Un même cabinet trouvé par deux requêtes différentes ne crée qu'une ligne.

### modeles\_message

| Colonne | Type | Rôle |
| --- | --- | --- |
| id | INTEGER PK |  |
| nom | TEXT | « Angle déontologie — médecins » |
| objet\_template | TEXT | Gabarit de l'objet |
| prompt\_systeme | TEXT | Instructions complètes données à l'IA |
| signature | TEXT | Bloc de signature |
| cree\_le | DATETIME |  |

### messages

| Colonne | Type | Rôle |
| --- | --- | --- |
| id | INTEGER PK |  |
| prospect\_id | INTEGER FK |  |
| objet | TEXT |  |
| corps | TEXT | Version générée puis éventuellement corrigée |
| genere\_le | DATETIME |  |
| edite\_par\_humain | BOOLEAN | Trace les corrections manuelles |
| version | INTEGER | Permet de régénérer sans écraser |

### envois

| Colonne | Type | Rôle |
| --- | --- | --- |
| id | INTEGER PK |  |
| message\_id | INTEGER FK |  |
| envoye\_le | DATETIME |  |
| brevo\_id | TEXT | Identifiant retourné par Brevo |
| statut | TEXT | `envoye` · `ouvert` · `repondu` · `echec` |
| erreur | TEXT | Message d'erreur si échec |

### liste\_noire

| Colonne | Type | Rôle |
| --- | --- | --- |
| id | INTEGER PK |  |
| valeur | TEXT | Email ou domaine |
| motif | TEXT | `desinscription` · `manuel` · `rebond` |
| ajoute\_le | DATETIME |  |

Consultée avant toute génération et avant tout envoi. Une adresse présente ici ne reçoit plus jamais rien, toutes campagnes confondues.

### jobs

| Colonne | Type | Rôle |
| --- | --- | --- |
| id | INTEGER PK |  |
| type | TEXT | `scan` · `analyse` · `redaction` · `envoi` |
| campagne\_id | INTEGER FK |  |
| statut | TEXT | `en_attente` · `en_cours` · `termine` · `erreur` |
| progression | INTEGER | 0 à 100 |
| detail | TEXT | « Lyon 3/5 — 42 trouvés » |
| demarre\_le | DATETIME |  |
| fini\_le | DATETIME |  |

## L'interface, écran par écran

Cinq écrans, accessibles par un rail de navigation permanent à gauche. L'écran qui compte est le tableau des prospects : c'est là que l'utilisateur passe ses quinze minutes quotidiennes.

### Campagnes

La liste des campagnes, avec pour chacune : nom, secteur, nombre de zones, nombre de prospects, nombre d'envois, statut.

Ouvrir une campagne donne accès à son édition complète :

- Les requêtes de recherche, ajoutables et supprimables une par une
- Les zones, en tableau, avec leur état de scan et un bouton pour scanner uniquement celle-là
- Les critères et leurs poids, modifiables en place
- L'angle de message rattaché
- Les boutons **Lancer le scan complet**, **Dupliquer**, **Archiver**

Un bouton **Nouvelle campagne** ouvre un formulaire vide, ou propose de partir d'un des trois modèles livrés.

### Prospects

Le tableau central. Colonnes : établissement, ville, site, email, problèmes détectés, score, statut, date d'envoi.

Filtres en haut : par campagne, par ville, par score minimum, par statut, par présence d'email.

Tri par défaut sur le score décroissant — les plus gros besoins en premier.

Actions possibles sur une ligne : ignorer, remettre en qualifié, ouvrir le site dans un onglet, voir le détail de l'analyse, générer le message.

Actions de masse sur une sélection : générer les messages, ignorer, exporter.

Un bouton **Exporter en CSV** exporte la sélection courante avec tous les filtres appliqués.

### Messages

La file de validation. Deux colonnes : la liste des prospects dont le message est prêt, et le message lui-même en lecture et en édition.

Pour chaque message, l'utilisateur voit le nom du cabinet, les problèmes détectés, l'objet et le corps. Il peut corriger le texte directement dans le champ. Trois boutons : **Approuver**, **Régénérer**, **Ignorer**.

Approuver passe le prospect en `approuve` et le rend éligible à l'envoi. C'est le seul chemin.

Un compteur permanent en haut : nombre de messages en attente de validation, nombre déjà approuvés non encore envoyés.

### Envois

Le tableau de bord de l'expédition. En haut, le quota du jour : envoyés aujourd'hui sur plafond autorisé.

Un bouton **Envoyer les messages approuvés** déclenche l'expédition, dans la limite du quota restant. L'avancement s'affiche en direct.

En dessous, l'historique des envois : destinataire, date, statut, ouverture, réponse. Filtrable par campagne et par période.

### Réglages

Trois blocs.

**Clés et services.** Clé Google Places, fournisseur d'IA avec son endpoint, son modèle et sa clé, clé Brevo. Chaque champ a un bouton **Tester la connexion** qui fait un appel réel et affiche le résultat. Les clés déjà saisies s'affichent masquées.

**Identité d'expédition.** Nom affiché, adresse email, signature par défaut, adresse de réponse.

**Limites.** Plafond d'envois par jour, délai entre deux envois, score minimum pour générer un message automatiquement.

Un bloc **Liste noire** permet d'ajouter une adresse ou un domaine à la main et de consulter ce qui est bloqué.

## Sourcing des prospects

Deux fournisseurs interchangeables derrière une interface commune. Le module expose une seule fonction : donne-moi une requête et une zone, je te rends une liste d'établissements normalisés.

### Google Places API — fournisseur principal

Endpoint `places:searchText` de l'API Places. Appel authentifié par clé, champs demandés explicitement pour maîtriser le coût.

Champs récupérés : nom, adresse formatée, site web, téléphone national, note moyenne, nombre d'avis, identifiant de lieu.

La pagination est gérée jusqu'à épuisement des résultats ou plafond configuré. Le biais géographique se fait par coordonnées de ville plus rayon, pas par texte libre, pour éviter les débordements sur les villes homonymes.

Pourquoi ce choix plutôt que du scraping : l'API ne casse pas quand Google change son HTML, ne se fait pas bloquer, et le quota gratuit mensuel couvre largement l'usage d'un freelance. C'est la fondation stable d'un outil qu'on utilise tous les jours.

### OpenStreetMap Overpass — secours

Aucune clé, aucun compte, aucun coût. Requête Overpass sur les tags de commerce et de santé dans un rayon autour des coordonnées de la ville.

Les données sont moins complètes — moins de sites web, pas de note ni d'avis — mais l'outil reste utilisable immédiatement, avant même que l'utilisateur ait ouvert un compte Google Cloud.

Le choix du fournisseur se fait dans les réglages. Si la clé Google est absente, l'application bascule automatiquement sur OpenStreetMap et le signale dans l'interface.

### Récupération des emails

Google Maps ne donne jamais l'email. Le module va le chercher lui-même, dans cet ordre :

1. Page d'accueil du site, recherche des liens `mailto:` et des adresses en texte brut
2. Pages `/contact`, `/contactez-nous`, `/nous-contacter`, `/mentions-legales`
3. Si rien n'est trouvé, le champ reste vide

Les adresses génériques de plateforme — `wixsite`, `no-reply`, `example` — sont écartées. Un prospect sans email reste en base avec son téléphone : l'utilisateur le traitera autrement.

L'expérience terrain indique qu'environ la moitié des établissements d'une liste exposent un email exploitable. C'est le taux à anticiper, pas un défaut de l'outil.

### Dédoublonnage

Trois niveaux, appliqués à l'insertion :

- Identifiant de source identique → ignoré
- Nom normalisé plus ville identiques → ignoré
- Domaine du site identique à un prospect existant → ignoré

La normalisation du nom retire les accents, la casse, la ponctuation et les mentions juridiques courantes. Un cabinet trouvé par trois requêtes différentes de la même campagne ne produit qu'une ligne.

Le dédoublonnage vaut aussi entre campagnes : si un cabinet a déjà été contacté dans une autre campagne, l'outil le signale au lieu de créer un doublon silencieux.

## Analyse des sites et scoring

Le principe est inversé par rapport à l'intuition : plus le site d'un prospect est mauvais, plus son score est élevé. Le score mesure le besoin, pas la qualité.

### Ce que l'analyseur vérifie

Pour chaque prospect disposant d'un site, le module charge la page d'accueil avec un délai d'attente de 10 secondes et relève :

| Code | Vérification | Signal détecté |
| --- | --- | --- |
| `sans_site` | Aucune URL en base | Le besoin maximal |
| `site_casse` | Code HTTP ≥ 400 ou connexion impossible | Site mort |
| `pas_https` | URL en `http://` ou certificat invalide | Problème de confiance |
| `lent` | Temps de réponse > 3 secondes | Pénalité Google et abandon visiteur |
| `pas_mobile` | Absence de balise viewport | Site illisible sur téléphone |
| `titre_vide` | Balise title absente, vide ou générique | Référencement inexistant |
| `description_vide` | Pas de meta description | Même cause |
| `pas_h1` | Aucun H1 dans la page | Structure SEO absente |
| `pas_rdv` | Aucun mot-clé de réservation ni formulaire | Pas de conversion |
| `peu_avis` | Moins de 10 avis Google | Preuve sociale faible |
| `site_daté` | Technologies obsolètes détectées | Refonte justifiée |

Chaque détection écrit son code dans le champ `problemes` du prospect, en JSON.

### Calcul du score

Le score est la somme des points des critères déclenchés, plafonnée à 10. Les points viennent de la table `criteres`, filtrée sur la campagne, avec repli sur les critères globaux.

Poids de départ livrés avec l'application, tous modifiables :

| Critère | Points |
| --- | --- |
| Aucun site | 10 |
| Site cassé | 9 |
| Titre vide | 4 |
| Pas de prise de rendez-vous | 3 |
| Lent | 3 |
| Pas mobile | 3 |
| Pas de HTTPS | 2 |
| Peu d'avis | 2 |
| Pas de H1 | 1 |
| Description vide | 1 |

Un prospect sans site obtient 10 d'office : c'est la cible la plus facile à convaincre.

### Pourquoi les poids sont en base

Un cabinet dentaire et un cabinet d'avocats n'ont pas les mêmes points faibles. Le dentiste perd des patients sans prise de rendez-vous en ligne ; l'avocat, non. L'utilisateur ajuste les poids par campagne et l'outil s'adapte à la cible sans qu'une ligne de code change.

### Seuil de génération

Un réglage global fixe le score minimum à partir duquel un message peut être généré. Par défaut 5. En dessous, le prospect reste en base mais ne consomme pas de crédit IA.

L'utilisateur garde la possibilité de forcer la génération sur un prospect précis, quel que soit son score.

## Génération des messages par IA

### Un adaptateur, plusieurs fournisseurs

xAI, Groq et OpenAI exposent tous le même format d'API. Le module `writer.py` implémente une seule classe qui prend un endpoint, un modèle et une clé, et fonctionne avec les trois.

| Fournisseur | Endpoint | Note |
| --- | --- | --- |
| xAI | `https://api.x.ai/v1/chat/completions` | Payant à l'usage |
| Groq | `https://api.groq.com/openai/v1/chat/completions` | Palier gratuit généreux |
| OpenAI | `https://api.openai.com/v1/chat/completions` | Payant à l'usage |

Le fournisseur se change dans les réglages, sans redémarrage. Si l'appel échoue, le message n'est pas créé et l'erreur est affichée telle quelle — jamais de message silencieusement vide.

### Le prompt vient de la campagne

C'est l'autre pilier de la réutilisabilité. Le prompt système n'est pas dans le code : il est stocké dans `modeles_message.prompt_systeme` et l'utilisateur le réécrit depuis l'interface.

Variables injectées automatiquement dans le prompt avant l'appel :

| Variable | Contenu |
| --- | --- |
| `{nom}` | Nom de l'établissement |
| `{ville}` | Ville |
| `{secteur}` | Secteur de la campagne |
| `{problemes}` | Libellés des critères déclenchés, en clair |
| `{score}` | Score de besoin |
| `{site}` | URL du site, ou mention d'absence |
| `{expediteur}` | Nom de l'expéditeur |
| `{signature}` | Bloc de signature |

### Contraintes imposées à la génération

Le prompt livré par défaut impose à l'IA :

- Moins de 120 mots
- Pas de lien dans le premier message, pour la délivrabilité
- Mentionner un problème précis et réel du site, jamais une généralité
- Finir par une question ouverte, pas par une offre
- Ton direct, phrases courtes, pas de formule administrative
- Interdiction d'inventer un chiffre, un client ou une référence

Ce prompt est un point de départ. L'utilisateur l'adapte à chaque campagne.

### Génération par lot

L'utilisateur sélectionne des prospects et lance la génération. Une tâche de fond traite la file, un prospect à la fois, avec une pause configurable entre les appels.

La progression s'affiche en direct. Un échec sur un prospect n'interrompt pas le lot : il est marqué en erreur et la file continue.

Une régénération crée une nouvelle version du message sans écraser la précédente. L'utilisateur peut comparer et choisir.

## Validation et envoi

### Le verrou

Un message ne part que si son prospect porte le statut `approuve`, et ce statut ne s'obtient que par un clic humain sur le bouton Approuver de l'écran Messages.

Ce point doit être implémenté comme une garde explicite dans `sender.py`, vérifiée juste avant l'appel à Brevo, et pas seulement comme un filtre de requête. Si le statut n'est pas `approuve`, la fonction lève une erreur et n'envoie rien.

Aucune option de configuration, aucun mode test, aucun paramètre d'URL ne doit pouvoir contourner cette garde.

### Avant chaque envoi

Trois vérifications enchaînées, dans cet ordre :

1. L'email n'est pas dans la liste noire
2. Ce prospect n'a pas déjà reçu un email de cette campagne
3. Le quota quotidien n'est pas atteint

Un échec sur l'une des trois arrête l'envoi pour ce prospect et consigne la raison, sans interrompre le lot.

### Expédition

Appel à l'API transactionnelle de Brevo, un email à la fois, avec une pause configurable entre deux envois — 30 à 60 secondes par défaut. Un envoi en rafale est le meilleur moyen de finir en spam.

Chaque envoi écrit une ligne dans `envois` avec l'identifiant Brevo retourné, qui sert ensuite au suivi.

### Montée en volume

Le plafond par défaut est volontairement bas : 20 emails par jour. Une adresse neuve qui expédie 200 messages le premier jour détruit sa réputation en quelques heures et finit en spam durablement.

La progression recommandée, inscrite dans le README et affichée en aide dans les réglages :

| Période | Plafond quotidien |
| --- | --- |
| Semaine 1 | 20 |
| Semaine 2 | 35 |
| Semaine 3 | 50 |
| Semaine 4 et au-delà | 80 à 100 |

### Suivi

L'écran Envois interroge Brevo pour récupérer les ouvertures et les met à jour en base.

Les réponses arrivent dans la boîte mail normale de l'utilisateur. Il marque le prospect comme `repondu` à la main dans l'interface — l'automatisation de cette détection est hors périmètre de cette version.

### Relances

Un prospect sans réponse après un délai configurable — 5 jours par défaut — remonte dans une file de relance sur l'écran Envois.

La relance suit exactement le même chemin que le premier message : génération, validation humaine, envoi. Aucune relance automatique sans approbation.

Deux relances maximum par prospect, puis abandon définitif.

## Garde-fous

Ces règles ne sont pas des options. Elles protègent la réputation du domaine de l'utilisateur, qui met des mois à se reconstruire une fois abîmée.

### Sécurité des clés

Les clés vivent dans `.env`, jamais dans le code, jamais dans un dépôt. Le `.gitignore` couvre `.env` et `data/`. Le fichier `.env.example` ne contient que des noms de variables vides.

Dans l'interface, une clé déjà enregistrée s'affiche masquée, seuls les quatre derniers caractères sont visibles.

### Volume et rythme

Plafond quotidien appliqué en dur dans le code d'envoi, pas seulement dans l'interface. Pause obligatoire entre deux envois. Compteur quotidien remis à zéro à minuit, stocké en base pour survivre à un redémarrage.

### Liste noire

Toute demande de désinscription entre en liste noire immédiatement. Tout rebond définitif aussi. La liste est consultée avant génération et avant envoi, et elle vaut pour toutes les campagnes.

Chaque email porte une mention de désinscription en bas, avec une adresse de réponse claire.

### Cadre légal européen

Les cibles sont des professionnels contactés sur leur adresse professionnelle au sujet de leur activité : c'est le cas d'usage prévu par le RGPD pour la prospection B2B, à condition de respecter trois choses.

1. L'objet du message doit concerner l'activité professionnelle du destinataire
2. Un moyen simple de s'opposer doit figurer dans chaque message
3. L'identité de l'expéditeur doit être clairement indiquée

L'application impose les trois. Elle n'envoie jamais à une adresse personnelle détectée comme telle, et le README rappelle la règle.

### Journalisation

Toute action qui touche l'extérieur — appel API, envoi, échec — est consignée avec son horodatage. En cas de problème de délivrabilité, l'utilisateur doit pouvoir reconstituer exactement ce qui est parti, quand, et vers qui.

## Enrichir l'outil dans le temps

L'application doit encore servir dans six mois, quand la cible aura changé deux fois. Sept mécanismes rendent cela possible sans jamais rouvrir le code.

### Ajouter une niche

Créer une campagne, saisir les requêtes et le secteur, rattacher ou écrire un angle de message. Aucune liste de niches n'existe en dur : la campagne est la niche.

### Ajouter une zone

Ajouter une ligne dans les zones de la campagne, puis scanner cette zone seule. Les prospects déjà collectés ne bougent pas. L'utilisateur peut couvrir une ville de plus chaque semaine sans tout relancer.

### Ajuster les critères

Modifier les poids dans l'écran de la campagne. Un recalcul des scores est proposé après modification, appliqué à tous les prospects de la campagne.

### Changer l'angle du message

Réécrire le prompt système du modèle rattaché. Les messages déjà envoyés gardent leur texte ; les prochains suivent le nouveau prompt.

### Importer des prospects de l'extérieur

Un import CSV accepte un fichier avec les colonnes nom, ville, site, email, téléphone. Les lignes importées entrent dans le circuit normal — analyse, score, génération, validation — avec `import_csv` comme source.

Cela couvre les listes obtenues autrement : annuaires professionnels, salons, recommandations, recherche manuelle.

### Changer de fournisseur d'IA

Trois champs dans les réglages : endpoint, modèle, clé. Passer de Groq à xAI, ou tester un nouveau modèle, prend trente secondes et ne demande aucun redéploiement.

### Ajouter un critère d'analyse

C'est le seul point qui demande une intervention dans le code, et il doit être conçu pour rester trivial. Le module `analyzer.py` expose une liste de fonctions de vérification, chacune suivant la même signature : elle reçoit la page et rend un code si le problème est détecté.

Ajouter un critère = écrire une fonction de dix lignes, l'ajouter à la liste, insérer une ligne dans `criteres`. Claude Code doit documenter ce point précisément dans le README, avec un exemple complet.

### Ce qui reste hors périmètre pour cette version

À noter pour ne pas élargir le chantier : détection automatique des réponses, intégration calendrier, séquences multi-canal incluant LinkedIn ou Instagram, hébergement distant, multi-utilisateur.

Ces points sont des évolutions possibles. Les écarter maintenant est ce qui permet de livrer une V1 qui fonctionne.

## Consignes de construction pour Claude Code

### Ordre de construction

Construire dans cet ordre et vérifier chaque étape avant de passer à la suivante. Une étape qui ne tourne pas ne se contourne pas.

1. **Base et schéma** — `db.py`, les neuf tables, les migrations, les données de départ. Vérification : la base se crée au premier lancement et contient les trois campagnes modèles.
2. **Flask et navigation** — `app.py`, les cinq gabarits, le rail de navigation. Vérification : les cinq écrans s'affichent, même vides.
3. **Réglages** — saisie et test des clés. Vérification : les trois boutons de test renvoient un résultat réel.
4. **Campagnes** — création, édition, zones, critères, duplication. Vérification : on crée une campagne avec cinq zones et on les voit.
5. **Sourcing** — Google Places puis OpenStreetMap, dédoublonnage, recherche d'emails. Vérification : un scan sur une ville réelle remplit la table prospects sans doublon.
6. **Analyse et score** — audit des sites, calcul avec les poids en base. Vérification : les scores sont cohérents, un prospect sans site obtient 10.
7. **Rédaction** — adaptateur IA, prompts par campagne, génération par lot. Vérification : un message généré mentionne un problème réel du site visé.
8. **Validation** — écran Messages, édition, approbation. Vérification : le statut passe bien à `approuve` et nulle part ailleurs.
9. **Envoi** — Brevo, quotas, liste noire, journalisation. Vérification : un envoi test vers une adresse contrôlée par l'utilisateur part et se consigne.
10. **Export, suivi, relances** — CSV, ouvertures, file de relance.

### Critères d'acceptation

L'application est considérée comme livrée quand les huit points suivants sont vrais.

- [ ] Un seul `python app.py` lance tout, sans Docker ni service externe
- [ ] Une campagne avec cinq zones se scanne en une action et remplit la table
- [ ] Changer un poids de critère et recalculer modifie les scores sans redémarrage
- [ ] Changer le prompt d'une campagne change les messages suivants
- [ ] Aucun envoi n'est possible sans le statut `approuve`, vérifié dans le code d'envoi
- [ ] Le plafond quotidien bloque réellement au-delà du nombre fixé
- [ ] Aucune clé n'apparaît dans le code, les gabarits ou un fichier versionné
- [ ] Le README permet à une personne qui découvre le projet de le lancer en dix minutes

### Ce qu'il ne faut pas faire

Ne pas scraper Google Maps avec un navigateur piloté. Le sourcing passe par l'API officielle ou par OpenStreetMap. Un scraper casse à la première modification du HTML et bloque un outil utilisé tous les jours.

Ne pas écrire de valeur métier en dur : aucune ville, aucune niche, aucun poids, aucun prompt dans le code. Si une valeur de ce type apparaît dans un fichier `.py`, elle doit aller en base.

Ne pas ajouter de dépendance lourde : pas de React, pas de framework CSS, pas d'ORM, pas de broker de tâches. La stack tient dans ce qui est listé.

Ne pas créer de chemin d'envoi parallèle ni de mode automatique, même désactivé par défaut. La garde d'approbation doit être le seul chemin possible.

Ne pas masquer les erreurs. Un appel API qui échoue affiche son message réel dans l'interface. Un message vide, un score à zéro silencieux ou un envoi qui ne part pas sans explication coûtent plus cher qu'une erreur visible.

### Qualité d'interface attendue

L'outil sera ouvert tous les matins. Le tableau des prospects doit rester lisible avec 500 lignes, trier sans rechargement, et rendre le score immédiatement repérable à l'œil.

Les états vides disent quoi faire, pas qu'il n'y a rien. Les boutons disent ce qui se passe quand on les presse. Les tâches longues montrent une progression réelle issue de la table `jobs`.

Une seule couleur d'accent, utilisée pour les actions. Les scores élevés se repèrent par leur valeur, pas par une décoration.
