# État du projet : point de reprise

*(Fichier de reprise écrit pour continuer après un compactage de contexte. Aucun secret ici.)*

## Application
Outil local de prospection (Flask + SQLite) dans `C:\Users\gillc\Projects\agent-prospection`. Lancement : `run.bat`
(ouvre http://localhost:5000, connexion identifiant + mot de passe ; identifiant `gill`, mot de passe donné dans la
conversation, modifiable dans Réglages). Vérification globale : `.\.venv\Scripts\python verifier.py` (dernier résultat :
TOUT EST BON). Spécification d'origine : `SPEC.md`. Documentation : `README.md`.

Les 10 étapes de la spec sont faites, plus : import CSV, accueil/tableau de bord, analyses + décisions recommandées +
analyste IA, santé du domaine (SPF/DKIM/DMARC), sauvegardes, journal, notes par prospect, « Mon site », plafond mensuel
Google Places, plateformes (Doctolib…) traitées comme « pas de site propre », mode essai (voir plus bas), connexion.

## Réglages réels de l'utilisateur (base `data/prospect.db`, lue en lecture seule)
Brevo : clé OK, test OK. Groq : clé OK, mais modèle actuel `allam-2-7b` (petit modèle arabe : à remplacer par un grand
modèle via « Voir les modèles disponibles »). Nom d'expéditeur « Gill », expéditeur gill-christ@gdjahoui.com. Pas de clé
Google (OpenStreetMap seul). Base vide : 0 campagne, 0 prospect. Sa base est encore en schéma v1 : son application tourne
sur l'ANCIENNE version, elle doit la redémarrer (fermer la fenêtre noire, relancer `run.bat`) pour charger tout le nouveau code.

## Test en cours demandé par l'utilisateur
1. Scan Bordeaux + dentiste ; 2. puis Bordeaux + médecin esthétique, pour comparer le nombre de résultats et savoir si
OpenStreetMap suffit ou s'il faut la clé Google Places. Aucun envoi vers un vrai dentiste : tout vers sa Gmail de test
(il la saisit lui-même sur un prospect dans Messages). Il veut valider 6 points un par un :
1. envoyer sans approuver → erreur claire : **VALIDÉ** (bac à sable : message non approuvé refusé « Ce message n'est pas
   approuvé… », route Envois → HTTP 400, texte modifié après approbation → approbation retirée, falsification SQL refusée par
   la base, aucun appel Brevo) ;
2. relancer le même scan → aucun doublon, compteur d'ignorés visible (colonne « Dont déjà en base ») : à faire avec lui ;
3. problèmes détectés sur 3 prospects, vérifiés par lui sur leurs sites : à faire avec lui ;
4. le message cite un problème précis et réel : à faire avec lui ;
5. quota à 1, deux messages approuvés → le second bloqué : il faut deux adresses DISTINCTES (ex. `moi+t1@gmail.com`,
   `moi+t2@gmail.com`), sinon le second est refusé pour « même adresse déjà contactée » et non pour le quota ;
6. mention de désinscription dans le mail reçu (+ « Afficher l'original » dans Gmail : SPF/DKIM/DMARC = PASS).

## Mode essai (ajouté pour sa contrainte « aucun envoi à un vrai dentiste »)
Réglage `envoi_adresses_autorisees` (Réglages > Limites) : si non vide, un email ne peut partir QUE vers ces adresses
exactes (jamais « gmail.com » seul). Restriction uniquement, l'approbation humaine reste exigée. Bandeau sur l'écran Envois,
badge dans le menu, avertissement dans Messages. Testé (sender : 31/31).

## Overpass / « médecine esthétique » : traité (3 octobre 2026)
Mesures à Bordeaux (15 km), service Overpass lent (35-55 s par requête) et par moments indisponible (504 même pour un point) :
- dentistes : 56 établissements, 14 % avec site, 41 % avec téléphone, 4 emails ;
- esthétique : `plastic_surgery` = 0, `dermatology` = 3 (médecins individuels), `shop=beauty` = 184 instituts (hors cible) ;
  la recherche par NOM a échoué 7 fois sur 7 (délai dépassé / 504), même service revenu ; cliniques + cabinets par étiquette
  non mesurés (504). taginfo (monde entier) : `aesthetic` 21, `aesthetics` 18, `cosmetic` 55 contre 1 867 `plastic_surgery`.
- Conclusion : OpenStreetMap NE SUFFIT PAS pour l'esthétique médicale ; Google Places (ou CSV) est nécessaire.
Correctif fait dans `core/sourcing.py` : une clause d'égalité par valeur (plus de regex sur les valeurs), volets
« étiquettes » (obligatoire) et « nom » (au mieux : 1 serveur, 1 essai) posés séparément ; si seul le nom échoue, les résultats
par étiquettes sont gardés et le résumé du scan affiche « ATTENTION, résultats incomplets ». sourcing 41/41, verifier OK.

## Règles de travail à respecter
- Tests de serveur sur un AUTRE port que 5000 (ex. 5055) : l'utilisateur fait tourner sa vraie application sur 5000.
- Ne jamais arrêter les processus Python en bloc ; filtrer par ligne de commande (`create_app`, `fake_ia`).
- Ne rien envoyer par Brevo avec sa vraie clé. Ne jamais toucher à sa vraie base en écriture sans qu'il le demande.
- Les scripts d'essai vont dans le dossier scratchpad de la session, pas dans le projet.

## Prochaines actions
1. Régler les requêtes Overpass « esthétique » (voir plus haut), relancer `verifier.py`.
2. L'utilisateur redémarre l'application, choisit un vrai modèle Groq, renseigne le mode essai (ses adresses de test), fait le
   scan Bordeaux + dentiste ; on valide les points 2 à 6 avec lui un par un.
3. Scan Bordeaux + médecin esthétique et comparaison chiffrée avec les dentistes → décision OSM / Google Places.

## Mise à jour du 4 octobre 2026 (après les 6 points)
- Les 6 points de test sont validés (point 1 route + code d'envoi, 2 relance de scan, 3 vérification de sites, 4 message précis, 5 quota à 1, 6 mention + livraison). Envoi réel fait depuis une COPIE de la base (jamais la vraie), uniquement vers les 2 adresses du mode essai. Premier mail arrivé en onglet Promotions de Gmail (domaine neuf, en-tête List-Unsubscribe : normal).
- Corrections issues des tests : lien profond 404 jugé sur la page d'accueil, « lent » confirmé par 2 mesures, raisons des fiches ignorées dans le résumé du scan, seconde passe Overpass, règle de salutation sans prénom dans le prompt par défaut (la base de l'utilisateur garde l'ancien prompt : il doit coller la règle dans ses angles).
- Nouveautés : import CSV de listes de leads (en-têtes après des lignes d'avertissement, regroupement des contacts par établissement, statut de validation des emails, réglage import_statuts_email_acceptes) ; mise en forme HTML des emails (core/courriel.py, réglages email_format / email_couleur_accent / modele_email_html, aperçu dans l'écran Messages ; le HTML ne change aucun mot, testé).
- Sourcing (mesures à Bordeaux, 15 km) : OSM = 56 dentistes (14 % avec site), 0 médecin esthétique exploitable ; registre officiel (API recherche-entreprises.api.gouv.fr, gratuite, sans clé) = 793 cabinets dentaires actifs, 1 523 spécialistes, mais SANS site ni email. Google Places (palier gratuit ~1 000 requêtes/mois pour les champs site/téléphone, carte bancaire exigée) reste la voie pour obtenir site + téléphone + avis. Aucun code « Sirene » n'a été ajouté.
- Fichier de leads de l'utilisateur (Téléchargements, « 100.000 LEADS - Rh et administration.csv ») : 9 932 lignes, 4 743 établissements après regroupement, 4 025 avec site + email validé, surtout cabinets RH / intérim / mairies ; 6 dentistes, 0 esthétique. Essai à blanc fait dans une base jetable ; RIEN importé dans la vraie base.
