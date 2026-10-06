# Agent de prospection

Application web **locale** (Flask + SQLite) qui transforme une recherche terrain en emails de prospection prêts à
partir, **sous contrôle humain** : elle trouve des établissements, audite leur site, note le besoin, rédige un
message par IA, et **n'envoie rien tant que tu n'as pas approuvé** chaque message. Spécification : `SPEC.md`.

```
Campagne → Sourcing → Analyse → Score → Rédaction IA → VALIDATION HUMAINE → Envoi → Suivi → Relances
```

## Lancer en dix minutes

Prérequis : Python 3.12 ou plus (testé avec 3.14).

1. **Windows** : double-clique sur `run.bat` (ou tape `.\run.bat` dans PowerShell, depuis ce dossier).
   **Mac / Linux** : `sh run.sh`.
   Au premier passage, le script crée un environnement `.venv` et installe les dépendances (`requirements.txt`).
   **Laisse la fenêtre noire ouverte** tant que tu utilises l'application ; ferme-la pour l'arrêter.
2. Le navigateur s'ouvre tout seul sur **http://localhost:5000** (l'adresse est aussi affichée dans la fenêtre).
   La base `data/prospect.db` est créée toute seule, avec trois modèles de campagne (cliniques esthétiques,
   cabinets dentaires, cabinets d'avocats).
   **Connexion** : identifiant et mot de passe demandés. Au tout premier lancement sans accès existant, un mot de
   passe aléatoire est affiché une seule fois dans la fenêtre. Mot de passe perdu :
   `.\.venv\Scripts\python -m core.auth reinitialiser`. Changement dans **Réglages > Accès à l'application**.
3. Va dans **Réglages** et renseigne :
   - ton **nom d'expéditeur** et ton **adresse d'expédition** (validée chez Brevo, SPF/DKIM en place) ;
   - la **clé Brevo** ; l'**endpoint, le modèle et la clé de ton fournisseur d'IA** (Groq, xAI, OpenAI ou tout
     endpoint compatible OpenAI) ; la clé Google Places est facultative.
   - Clique sur **Tester la connexion** à côté de chaque service : le test fait un vrai appel (sans envoyer d'email).
     Un service non configuré affiche « Non configuré », jamais un faux succès.
4. **Campagnes** → « Nouvelle campagne » (pars d'un modèle ou d'une page blanche) → ajoute tes villes →
   **Lancer le scan complet**.
5. Le scan enchaîne tout seul l'analyse des sites et le score. Dans **Prospects**, coche les prospects qui
   t'intéressent → **Générer les messages**.
6. **Messages** : relis, corrige, **Approuve**. **Envois** : « Envoyer les messages approuvés ».

Sans clé Google, le sourcing utilise OpenStreetMap (le réglage « Automatique » bascule tout seul et le dit).

## Outils intégrés

- **Mon site** (Réglages > Identité) : jamais dans le texte écrit par l'IA (pas de lien dans un premier email), mais
  ajoutable sous la signature à l'envoi : jamais, seulement dans les relances (défaut), ou toujours.
- **Santé du domaine d'envoi** (Réglages) : vérifie SPF, DKIM et DMARC de ton adresse d'expédition, avec le conseil
  exact à appliquer. Sans eux, tes emails finissent en spam quel que soit leur contenu. Le DNS est lu par le résolveur
  de ta machine (nslookup), puis par le DNS public sur HTTPS si besoin : un réseau qui bloque ce dernier ne gêne pas.
- **Sauvegardes** : une copie de la base par jour au lancement, plus un bouton « Sauvegarder maintenant ».
- **Journal d'activité** : chaque appel externe, envoi, refus et erreur, avec son horodatage.
- **Notes** par prospect (fenêtre « Détail du score » du tableau) pour tes appels.
- **Plafond mensuel Google Places** : l'application n'appelle plus Google au-delà, pour ne jamais sortir du gratuit.
- **Raccourcis** dans Messages : Ctrl+Entrée approuve, Alt+↓ / Alt+↑ change de prospect.

## Les écrans

| Écran | Rôle |
| --- | --- |
| **Accueil** | Tableau de bord : ce qu'il y a à faire maintenant (messages à valider, prêts à partir, relances), chiffres clés, entonnoir, activité récente, et une « mise en route » qui se coche toute seule. |
| **Campagnes** | **Nouvelle niche (assistant)** : tu décris la niche, ton offre, la ville et le problème visé ; l'IA propose les recherches, l'objet, l'angle du message, le poids des critères et les zones (quartiers et communes voisines, contrôlées : un homonyme lointain est écarté) ; tu relis tout, puis la campagne est créée en brouillon. Les règles communes (message court, aéré, sans jargon ni mot de vente, jamais de prénom) sont ajoutées à chaque angle. Une campagne = requêtes de recherche, zones (autant que voulu), critères et poids, angle du message (prompt). Dupliquer, archiver, supprimer (corbeille sur chaque ligne ; une campagne qui a déjà envoyé des emails demande une seconde confirmation, car son historique alimente le quota, les relances et l'anti-doublon ; la liste noire n'est jamais touchée), scanner une zone ou toutes. Un recalcul des scores est proposé après modification d'un poids. |
| **Prospects** | Tableau central, 500+ lignes lisibles, filtres sans rechargement (campagne, ville, statut, email, adresse générique), tri par clic, import et export CSV, génération de messages en masse. Les prospects sans email gardent leur téléphone : tu les appelles. |
| **Formulaires** | Pour les prospects **sans email dont le site a un formulaire de contact** (repéré automatiquement pendant l'exploration des sites : scan, analyse, import CSV, ou « Chercher les emails et formulaires manquants » sur la fiche d'une campagne, quelle que soit la niche). L'application n'envoie rien : elle te donne le lien du formulaire et un **message prêt à coller** (écrit par l'IA d'après le vrai problème et l'angle de la campagne, modifiable, bouton Copier). Tu le colles toi-même, depuis n'importe quel pays, puis tu notes : envoyé, a répondu, pas intéressé (définitif : prospect ignoré), formulaire inutilisable. Les formulaires qui exigent un captcha ou s'affichent par JavaScript ne sont pas repérables. |
| **Appels** | La file des prospects à appeler (par défaut ceux sans email, du plus gros score au plus petit ; les rappels dus passent en tête). Pour chacun : numéro cliquable, vrai problème constaté, **script de 4 lignes écrit par l'IA** (modifiable), résultat de l'appel (pas de réponse, à rappeler le…, intéressé, rendez-vous obtenu, pas intéressé, mauvais numéro), note et historique. « Pas intéressé » est définitif : prospect ignoré et email éventuel en liste noire. Après chaque appel, la fiche suivante s'ouvre. |
| **Messages** | File de validation : corriger, **Approuver**, régénérer (nouvelle version, les anciennes restent), ignorer. Saisie manuelle d'un email manquant. |
| **Envois** | Quota du jour, envoi avec pauses, arrêt d'urgence, suivi Brevo (ouvertures, rebonds), réponses marquées à la main, file de relance. |
| **Analyses** | Ce que disent tes résultats : taux de réponse par campagne, angle de message, problème détecté, ville, tranche de score, heure et jour d'envoi. Des **décisions recommandées** calculées par des règles prudentes (une comparaison n'est proposée que si l'écart est statistiquement crédible, rien n'est conclu avant 20 contacts), et un **analyste IA** qui lit tes statistiques agrégées (jamais un nom, un email ou un site) et te conseille. |
| **Réglages** | Clés, identité d'expédition, limites, seuils, liste noire. Tout ce qui est « métier » se règle ici. |

## Règles non négociables (vérifiées par des tests)

- **Rien ne part sans approbation.** `core/sender.py` relit l'approbation dans la base juste avant d'appeler Brevo ;
  la fonction d'envoi n'accepte aucun paramètre de contournement, il n'existe aucun mode test ni envoi automatique.
  Seul `core/messages.py` pose une approbation, sur un clic humain, pour un message précis. Modifier le texte ou
  régénérer annule l'approbation.
- **Tout est derrière une connexion** (identifiant + mot de passe) : pages et API. Le mot de passe n'est jamais
  stocké, seulement son empreinte (scrypt salée) ; cookie de session `HttpOnly` + `SameSite=Strict` ; blocage
  temporaire après 5 échecs en 10 minutes ; changer le mot de passe ferme toutes les autres sessions.
- **Aucune valeur métier en dur** : villes, niches, poids, seuils, mots-clés, prompts, plafonds, pauses, fuseau,
  domaines génériques… tout est en base et se modifie dans l'interface. Seul `core/seeds.py` contient des valeurs de départ.
- **Aucune clé dans un fichier versionné** : `.env` et `data/` sont dans `.gitignore`, `.env.example` ne contient que
  des noms de variables vides, et l'application refuse d'écrire une clé si le `.gitignore` ne les protège plus.
- **Un `timeout` explicite sur chaque appel réseau** : tout passe par `core/http.py`, qui journalise aussi chaque
  appel (jamais les clés) dans `data/logs/prospect.log`.
- **Erreurs visibles** : un appel qui échoue affiche son message réel ; jamais de message vide, de score à zéro
  silencieux ou d'envoi qui ne part pas sans explication.

## ⚠ Sécurité : `data/prospect.db` contient tes clés API en clair

Les clés (Google Places, fournisseur d'IA, Brevo) sont stockées dans la table `reglages`. C'est acceptable pour un
outil local mono-utilisateur, à condition de :

- **ne jamais partager ce fichier** (email, messagerie, support) ;
- **ne jamais le sauvegarder sur un cloud public** (Drive, Dropbox, OneDrive partagé…) : fais plutôt une copie sur
  un disque local ou chiffré ;
- ne jamais le versionner (`.gitignore` exclut `.env`, `data/` et `*.db`).

Si le fichier a fuité, révoque les clés chez chaque fournisseur. Le serveur n'écoute que sur `127.0.0.1` et refuse
toute requête dont l'adresse n'est pas `localhost`.

`.env` est facultatif : s'il existe au tout premier lancement, ses valeurs sont copiées **une seule fois** dans la
base, qui fait ensuite foi (voir `.env.example`).

## Envoyer sans abîmer ton domaine

- Plafond par défaut volontairement bas : **20 emails par jour**. Montée recommandée : semaine 1 : 20 · semaine 2 : 35 ·
  semaine 3 : 50 · semaine 4 et au-delà : 80 à 100.
- **Allure de l'email** : par défaut un message « écrit à la main » (aucun fond, cadre ni barre de couleur, texte aéré), car
  un email dessiné ressemble à une newsletter et Gmail le range dans « Promotions ». Réglages > Format : « texte brut
  seulement » est encore plus discret, mais Brevo ne peut alors plus compter les ouvertures.
- Pause aléatoire de 30 à 60 s entre deux emails (réglable). Le plafond du jour est appliqué dans le code d'envoi
  lui-même et remis à zéro à minuit (fuseau réglable, `Europe/Paris` par défaut).
- Vérifications avant chaque envoi : liste noire, pas de double envoi, plafond. Chaque email porte ton identité, une
  adresse de réponse, un en-tête `List-Unsubscribe` et la mention « répondez STOP… » (modifiable, jamais supprimable).
- Un rebond définitif ou une désinscription entre **immédiatement** en liste noire, pour toutes les campagnes.
- Si un envoi est interrompu en plein appel réseau, il est marqué **incertain** et n'est jamais rejoué tout seul :
  vérifie dans Brevo, puis tranche dans l'écran Envois.
- Relances : sans réponse après 5 jours (réglable), deux relances au maximum, même chemin que le premier message
  (génération, validation humaine, envoi).

## Cadre légal (prospection B2B, RGPD)

Contacter un professionnel sur son adresse professionnelle, au sujet de son activité, est permis à trois
conditions, que l'application impose : l'objet du message concerne son activité, un moyen simple de s'opposer figure
dans chaque message, et ton identité d'expéditeur est clairement indiquée. Ne cible jamais l'adresse personnelle
d'un particulier. Les adresses chez un fournisseur grand public (gmail, orange…) sont **signalées** « adresse
générique » mais pas bloquées : c'est à toi de juger s'il s'agit d'une adresse professionnelle.

## Données et limites à connaître

- **OpenStreetMap** est moins complet que Google : peu de sites web, aucune note ni avis. Un site vide y signifie
  « inconnu », pas « inexistant » : le critère « aucun site » ne se déclenche donc que pour les sources listées dans
  le réglage « Sources où pas de site est fiable » (Google par défaut).
- La recherche d'emails visite la page d'accueil et les pages de contact, respecte `robots.txt`, et ne retient que
  les adresses du domaine du site (ou d'un fournisseur grand public).
- Hors périmètre de cette version : détection automatique des réponses, intégration calendrier, canaux LinkedIn ou
  Instagram, hébergement distant, multi-utilisateur.

## Importer des prospects

Prospects → « Importer un CSV » : colonnes `nom` (obligatoire), `ville`, `site`, `email`, `telephone`, `adresse`
(séparateur virgule, point-virgule ou tabulation ; UTF-8 ou Excel). Les lignes entrent dans le circuit normal
(analyse, score, rédaction, validation). Le compte rendu liste les doublons, lignes invalides et avertissements.

## Ajouter un critère d'analyse (le seul point qui demande du code)

Un critère = une fonction de quelques lignes + une ligne en base. Exemple complet : détecter un site sans lien vers
une page Facebook (`pas_facebook`).

**1. Écrire la fonction** dans `core/analyzer.py`. Elle reçoit `page` et renvoie un code si le problème est détecté,
sinon `None`. `page` contient notamment `page["soup"]` (la page, via BeautifulSoup, ou `None` si elle n'a pas pu
être lue), `page["charge"]` (la page a-t-elle été lue ?), `page["prospect"]` (la fiche) et `page["cfg"]` (les réglages).

```python
def verifier_pas_facebook(page):
    if not page["charge"]:
        return None                      # pas de page lue : on ne dit rien
    if page["soup"].find("a", href=lambda h: h and "facebook.com" in h) is None:
        return "pas_facebook"
```

**2. L'ajouter à la liste** `VERIFICATIONS`, en bas du fichier : `verifier_pas_facebook,`.

**3. Insérer la ligne** dans la table `criteres` (libellé affiché et injecté dans le prompt, poids de départ) :

```sql
INSERT INTO criteres (campagne_id, code, libelle, points, actif)
VALUES (NULL, 'pas_facebook', 'Aucun lien vers une page Facebook', 2, 1);
```

Le critère apparaît alors sur la fiche de chaque campagne avec son poids modifiable. Pour l'appliquer aux prospects
existants : « Réanalyser tous les sites ». Vérification : `python -m core.analyzer URL`.

## Vérifier que tout fonctionne

```
.\.venv\Scripts\python verifier.py
```

Rejoue les auto-tests de chaque module (bases jetables, réseau et Brevo **simulés** : aucun appel réel, aucun email),
ouvre chaque écran et contrôle la syntaxe JavaScript. Ta vraie base n'est jamais touchée. Chaque module `core/` se
teste aussi seul : `python -m core.sender check`, `python -m core.writer check`, etc.

## Structure

```
app.py            routes Flask et démarrage
config.py         chemins uniquement
verifier.py       vérification globale
core/
  auth.py         identifiant, mot de passe, session               accueil.py   données du tableau de bord
  analyses.py     statistiques et décisions recommandées           analyste.py  analyste IA (données agrégées seulement)
  domaine.py      SPF / DKIM / DMARC                               sauvegarde.py copies de la base
  niche.py        assistant de niche (IA propose, tu relis)        courriel.py   mise en page des emails
  appels.py       file d'appels, script d'appel, journal       formulaires.py  canal « formulaire de contact »
  db.py           schéma, migrations, garde-fous de la base      jobs.py      tâches de fond (table jobs)
  seeds.py        données de départ                              sourcing.py  OpenStreetMap, géocodage, emails
  campagnes.py    campagnes, zones, critères, angles             analyzer.py  audit des sites
  scorer.py       calcul du score                                writer.py    rédaction par IA
  messages.py     validation humaine (SEUL point d'approbation)  sender.py    envoi Brevo (garde d'approbation)
  suivi.py        ouvertures, rebonds, réponses, relances        importer.py  import / export CSV
  reglages.py     réglages et validation                         http.py      réseau (timeout et journal obligatoires)
templates/ static/   interface HTML + CSS + JavaScript natif (sans build)
```
