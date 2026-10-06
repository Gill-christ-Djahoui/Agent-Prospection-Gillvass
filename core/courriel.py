"""Mise en forme des emails : le texte approuvé, présenté proprement (HTML) en plus de sa version texte.

Règle de fond : le HTML n'est QUE la mise en page du texte que tu as approuvé. Il n'ajoute aucun mot, aucun lien,
aucune image : le corps approuvé, l'adresse de ton site si le réglage le demande, puis la mention de désinscription.
Tout est échappé, le texte approuvé ne peut donc pas injecter de balises. Le modèle (réglage `modele_email_html`)
ne décide que de l'habillage : polices, marges, couleur.

Utilisable seul : python -m core.courriel check     tests
"""
import html
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urlsplit

_COULEUR_DEFAUT = "#1f6feb"
_RE_COULEUR = re.compile(r"^#[0-9a-fA-F]{6}$")
_RE_HOTE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def _remplacer(modele, valeurs):
    """Remplacement simple des {variables} connues : les accolades libres d'un modèle ne cassent rien."""
    for cle, valeur in valeurs.items():
        modele = modele.replace("{" + cle + "}", valeur)
    return modele


def _adresse_web(site):
    """Adresse http(s) sûre pour un lien, ou None."""
    site = (site or "").strip()
    if not site:
        return None
    adresse = site if "//" in site else "https://" + site
    if not re.match(r"^https?://[^\s\"'<>]+$", adresse):
        return None
    try:
        hote = (urlsplit(adresse).hostname or "").lower()
    except ValueError:
        return None
    return adresse if _RE_HOTE.match(hote) else None  # un vrai nom de domaine, pas « javascript:… »


def _est_signature(paragraphe):
    """Un dernier paragraphe court, sans phrase (« Gill » / « Développeur web »), est une signature."""
    lignes = [l.strip() for l in paragraphe.split("\n") if l.strip()]
    return 0 < len(lignes) <= 4 and all(len(l) <= 60 and l[-1] not in ".?!:;" for l in lignes)


def rendre_html(cfg, corps, site, mention):
    """HTML complet de l'email. `corps`, `site` et `mention` sont EXACTEMENT les textes de la version texte.

    L'allure est celle d'un message écrit à la main : paragraphes aérés, signature en simples lignes, ton site en lien
    ordinaire dessous. Aucune décoration (un email « dessiné » est classé dans Promotions par Gmail). Cela ne touche à
    aucun mot.
    """
    couleur = cfg.get("email_couleur_accent") or _COULEUR_DEFAUT
    if not _RE_COULEUR.match(couleur):
        couleur = _COULEUR_DEFAUT  # n'intervient que dans un modèle personnalisé qui utilise {couleur}
    paragraphes = [b for b in re.split(r"\n\s*\n", corps.strip()) if b.strip()]
    signature = paragraphes.pop() if len(paragraphes) >= 2 and _est_signature(paragraphes[-1]) else None
    blocs = ['<p style="margin:0 0 18px 0;">' + "<br>".join(html.escape(l.rstrip()) for l in p.split("\n")) + "</p>"
             for p in paragraphes]
    lien = ""
    if site:
        adresse = _adresse_web(site)
        affiche = html.escape(site.strip())
        lien = f'<a href="{html.escape(adresse, quote=True)}">{affiche}</a>' if adresse else affiche
    if signature:
        lignes = [html.escape(l.strip()) for l in signature.split("\n") if l.strip()]
        if lien:
            lignes.append(lien)
        blocs.append('<p style="margin:24px 0 0 0;">' + "<br>".join(lignes) + "</p>")
    elif lien:
        blocs.append(f'<p style="margin:0 0 18px 0;">{lien}</p>')
    mention_html = "<br>".join(html.escape(l.strip()) for l in mention.strip().split("\n"))
    return _remplacer(cfg["modele_email_html"],
                      {"contenu": "\n".join(blocs), "mention": mention_html, "couleur": couleur})


def texte_du_html(contenu_html):
    """Texte visible d'un HTML (pour vérifier que la mise en forme n'ajoute ni ne retire aucun mot)."""
    class Lecteur(HTMLParser):
        def __init__(self):
            super().__init__()
            self.morceaux, self.ignorer = [], False

        def handle_starttag(self, balise, attrs):
            if balise in ("style", "script", "head", "title"):
                self.ignorer = True
            elif balise in ("br", "p", "div"):
                self.morceaux.append(" ")

        def handle_endtag(self, balise):
            if balise in ("style", "script", "head", "title"):
                self.ignorer = False
            elif balise in ("p", "div"):
                self.morceaux.append(" ")

        def handle_data(self, donnees):
            if not self.ignorer:
                self.morceaux.append(donnees)

    lecteur = Lecteur()
    lecteur.feed(contenu_html)
    return " ".join("".join(lecteur.morceaux).split())


# --------------------------------------------------------------------------------------
# Auto-tests
# --------------------------------------------------------------------------------------


def _autotests():
    from core import db, seeds
    courriel_texte = texte_du_html

    resultats = []

    def verifier(libelle, condition):
        resultats.append((bool(condition), libelle))

    reglages = dict((cle, valeur) for cle, valeur, _ in seeds.REGLAGES)
    cfg = {"modele_email_html": reglages["modele_email_html"], "email_couleur_accent": "#112233"}
    corps = "Bonjour,\n\nVotre site ne s'affiche pas bien <b>sur mobile</b> & c'est dommage.\nAvez-vous constaté des pertes ?\n\nGill\nDéveloppeur web"
    mention = "Si vous ne souhaitez pas être recontacté, répondez STOP à ce message."
    page = rendre_html(cfg, corps, "https://gdjahoui.com", mention)

    texte_envoye = " ".join((corps.rstrip() + "\nhttps://gdjahoui.com\n\n" + mention).split())
    verifier("le texte visible du HTML est EXACTEMENT celui de la version texte (aucun mot ajouté ni retiré)",
             texte_du_html(page) == texte_envoye)
    verifier("le texte approuvé est échappé : une balise écrite dans le message n'est jamais interprétée",
             "&lt;b&gt;sur mobile&lt;/b&gt;" in page and "<b>sur mobile</b>" not in page and "&amp;" in page)
    verifier("paragraphes aérés conservés (2 paragraphes de texte), signature reconnue : simples lignes, lien dessous",
             page.count('<p style="margin:0 0 18px 0;">') == 2
             and '<p style="margin:24px 0 0 0;">Gill<br>Développeur web<br><a href="https://gdjahoui.com">https://gdjahoui.com</a></p>' in page)
    verifier("aucune décoration : ni fond, ni cadre, ni barre de couleur, ni gras, ni arrondi (rien qui ressemble à une newsletter)",
             all(m not in page for m in ("background:#f3", "border", "font-weight", "box-shadow", "max-width")) and "#112233" not in page)
    sans_signature = rendre_html(cfg, "Bonjour,\n\nVotre site est lent. Pouvons-nous en parler ?", "https://gdjahoui.com", mention)
    verifier("un dernier paragraphe qui est une phrase n'est PAS pris pour une signature (le site reste un paragraphe)",
             sans_signature.count('<p style="margin:0 0 18px 0;">') == 3 and "margin:24px 0 0 0" not in sans_signature
             and courriel_texte(sans_signature) == " ".join(("Bonjour,\n\nVotre site est lent. Pouvons-nous en parler ?\nhttps://gdjahoui.com\n\n" + mention).split()))
    verifier("le site devient un lien http(s) ordinaire ; la mention est en pied de page",
             'href="https://gdjahoui.com"' in page and page.index("répondez STOP") > page.index("gdjahoui.com"))
    sans_site = rendre_html(cfg, corps, "", mention)
    verifier("sans site : aucun lien dans l'email", "<a " not in sans_site)
    dangereux = rendre_html(cfg, corps, "javascript:alert(1)", mention)
    verifier("adresse de site dangereuse : jamais transformée en lien", "<a " not in dangereux and 'href="javascript' not in dangereux)
    cfg_perso = {"modele_email_html": '<div style="color:{couleur}">{contenu}{mention}</div>',
                 "email_couleur_accent": "red;background:url(x)"}
    mauvaise_couleur = rendre_html(cfg_perso, corps, "https://gdjahoui.com", mention)
    verifier("modèle personnalisé avec {couleur} : couleur invalide remplacée par la couleur par défaut, rien n'est injecté dans le style",
             "url(x)" not in mauvaise_couleur and f"color:{_COULEUR_DEFAUT}" in mauvaise_couleur
             and "color:#112233" in rendre_html({**cfg_perso, "email_couleur_accent": "#112233"}, corps, "", mention))
    verifier("le HTML ne contient ni image, ni script, ni pixel de suivi",
             all(b not in page for b in ("<img", "<script", "<iframe", "onload=")))
    # Migration : un modèle resté identique à une ancienne version par défaut est mis à jour, un modèle personnalisé jamais
    import tempfile
    from pathlib import Path

    import config

    ancien_chemin = config.DB_PATH
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        config.DB_PATH = Path(tmp) / "t.db"
        try:
            db.init_db(importer_env=False)
            with db.connexion() as c:
                verifier("réglages de départ : le modèle par défaut actuel est installé",
                         db.get_reglage(c, "modele_email_html") == seeds._MODELE_EMAIL_HTML)
                db.set_reglage(c, "modele_email_html", seeds._MODELE_EMAIL_HTML_V1)
                seeds.appliquer_seeds(c)
                verifier("un modèle resté celui d'une ancienne version par défaut est mis à jour au lancement",
                         db.get_reglage(c, "modele_email_html") == seeds._MODELE_EMAIL_HTML)
                db.set_reglage(c, "modele_email_html", seeds._MODELE_EMAIL_HTML_V2)
                seeds.appliquer_seeds(c)
                verifier("l'ancienne « carte » colorée (version 2, rangée dans Promotions) est remplacée par l'allure sobre",
                         db.get_reglage(c, "modele_email_html") == seeds._MODELE_EMAIL_HTML
                         and "border-radius" not in seeds._MODELE_EMAIL_HTML)
                perso = seeds._MODELE_EMAIL_HTML_V1 + "<!-- ma version -->"
                db.set_reglage(c, "modele_email_html", perso)
                seeds.appliquer_seeds(c)
                verifier("un modèle personnalisé n'est JAMAIS écrasé", db.get_reglage(c, "modele_email_html") == perso)
        finally:
            config.DB_PATH = ancien_chemin
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
    print("Usage : python -m core.courriel check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
