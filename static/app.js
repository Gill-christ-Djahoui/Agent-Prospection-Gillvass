// JavaScript natif, sans build. Utilitaires communs puis comportements par écran.

// --- Utilitaires -------------------------------------------------------------------

// Notification flottante (en bas à droite). Se ferme seule, ou au clic ; une erreur reste plus longtemps.
function toast(message, type = "info", duree = 6000) {
  const zone = document.getElementById("toasts");
  if (!zone) return;
  const element = document.createElement("div");
  element.className = "toast toast-" + type;
  element.setAttribute("role", type === "erreur" ? "alert" : "status");
  const texte = document.createElement("span");
  texte.textContent = message;  // textContent : jamais d'HTML injecté
  const fermer = document.createElement("button");
  fermer.type = "button";
  fermer.className = "toast-fermer";
  fermer.setAttribute("aria-label", "Fermer");
  fermer.textContent = "×";
  const retirer = () => { element.classList.add("sortie"); window.setTimeout(() => element.remove(), 200); };
  fermer.addEventListener("click", retirer);
  element.append(texte, fermer);
  zone.appendChild(element);
  if (duree) window.setTimeout(retirer, duree);
}

// Affiche une erreur. Une erreur n'est jamais avalée en silence.
function afficherErreur(message) {
  toast(message, "erreur", 12000);
}

// Appel JSON vers le serveur. Si le statut n'est pas 2xx, lève une Error portant le message réel
// du serveur et le corps complet dans `erreur.corps`. Avec { silencieux: true }, la bannière
// n'est pas affichée (l'appelant montre l'erreur près du champ concerné).
async function api(url, options = {}) {
  const { silencieux, ...fetchOptions } = options;
  let reponse;
  try {
    reponse = await fetch(url, { headers: { "Content-Type": "application/json" }, ...fetchOptions });
  } catch (e) {
    const message = "Le serveur local ne répond pas. Vérifie que l'application tourne (run.bat).";
    if (!silencieux) afficherErreur(message);
    throw new Error(message);
  }
  let corps = null;
  try { corps = await reponse.json(); } catch (_) { /* réponse sans JSON */ }
  if (reponse.status === 401) {  // session expirée : retour à la page de connexion
    window.location.href = "/connexion?suite=" + encodeURIComponent(window.location.pathname);
    throw new Error("Session expirée : reconnexion nécessaire.");
  }
  if (!reponse.ok) {
    const message = (corps && (corps.erreur || corps.message)) || `Erreur ${reponse.status}`;
    if (!silencieux) afficherErreur(message);
    const erreur = new Error(message);
    erreur.corps = corps;
    throw erreur;
  }
  return corps;
}

// --- Écran Réglages ----------------------------------------------------------------

const LIBELLES_STATUT = {
  ok: "OK",
  erreur: "Erreur",
  non_configure: "Non configuré",
  attention: "Attention",
};

function afficherResultat(element, statut, message) {
  element.className = "resultat statut-" + statut;
  element.textContent = `${LIBELLES_STATUT[statut] || statut} : ${message}`;
}

function champsDuFormulaire(formulaire) {
  return formulaire.querySelectorAll("[data-cle]");
}

function effacerErreursChamps(portee) {
  portee.querySelectorAll("[data-erreur-pour]").forEach((p) => { p.hidden = true; p.textContent = ""; });
}

function montrerErreursChamps(erreurs) {
  Object.entries(erreurs).forEach(([cle, message]) => {
    const p = document.querySelector(`[data-erreur-pour="${cle}"]`);
    if (p) { p.textContent = message; p.hidden = false; }
  });
}

// Met à jour le masque affiché d'une clé après enregistrement ou effacement.
function majEtatSecrets(etat) {
  Object.entries(etat).forEach(([cle, info]) => {
    const champ = document.querySelector(`[data-cle="${cle}"]`);
    if (!champ) return;
    champ.value = "";
    champ.placeholder = info.definie ? `${info.masque} (enregistrée)` : "Non configurée";
    const effacer = document.querySelector(`[data-effacer="${cle}"]`);
    if (effacer) effacer.hidden = !info.definie;
  });
}

async function enregistrerBloc(formulaire, effacer = []) {
  const bloc = formulaire.dataset.bloc;
  const sortie = formulaire.querySelector(`[data-enregistre-pour="${bloc}"]`);
  const bouton = formulaire.querySelector('button[type="submit"]');
  const valeurs = {};
  champsDuFormulaire(formulaire).forEach((champ) => { valeurs[champ.dataset.cle] = champ.value; });
  effacerErreursChamps(formulaire);
  bouton.disabled = true;
  sortie.textContent = "Enregistrement…";
  sortie.className = "resultat";
  try {
    const reponse = await api("/api/reglages", {
      method: "POST", silencieux: true, body: JSON.stringify({ valeurs, effacer }),
    });
    majEtatSecrets(reponse.etat);
    const fournisseur = document.getElementById("fournisseur");
    if (fournisseur) fournisseur.textContent = reponse.fournisseur;
    afficherResultat(sortie, "ok", reponse.modifiees.length ? "enregistré." : "rien à modifier.");
  } catch (e) {
    if (e.corps && e.corps.erreurs) montrerErreursChamps(e.corps.erreurs);
    afficherResultat(sortie, "erreur", e.corps && e.corps.erreurs
      ? "corrige les champs signalés, rien n'a été enregistré." : e.message);
  } finally {
    bouton.disabled = false;
  }
}

function initialiserReglages() {
  document.querySelectorAll("form[data-bloc]").forEach((formulaire) => {
    formulaire.addEventListener("submit", (evenement) => {
      evenement.preventDefault();
      enregistrerBloc(formulaire);
    });
  });

  document.querySelectorAll("[data-effacer]").forEach((bouton) => {
    bouton.addEventListener("click", () => {
      const cle = bouton.dataset.effacer;
      if (!confirm("Effacer cette clé de la base ? Le service concerné cessera de fonctionner.")) return;
      enregistrerBloc(bouton.closest("form[data-bloc]"), [cle]);
    });
  });

  document.querySelectorAll("[data-tester]").forEach((bouton) => {
    bouton.addEventListener("click", async () => {
      const service = bouton.dataset.tester;
      const sortie = document.querySelector(`[data-resultat-pour="${service}"]`);
      const valeurs = {};
      bouton.dataset.champs.split(",").forEach((cle) => {
        const champ = document.querySelector(`[data-cle="${cle}"]`);
        if (champ) valeurs[cle] = champ.value;
      });
      bouton.disabled = true;
      sortie.className = "resultat";
      sortie.textContent = "Test en cours : appel réel au service…";
      try {
        const resultat = await api(`/api/reglages/tester/${service}`, {
          method: "POST", silencieux: true, body: JSON.stringify({ valeurs }),
        });
        afficherResultat(sortie, resultat.statut, resultat.message);
      } catch (e) {
        afficherResultat(sortie, "erreur", e.message);
      } finally {
        bouton.disabled = false;
      }
    });
  });

  const formListe = document.getElementById("form-liste-noire");
  if (formListe) {
    formListe.addEventListener("submit", async (evenement) => {
      evenement.preventDefault();
      const champ = document.getElementById("valeur-liste-noire");
      const erreur = document.getElementById("erreur-liste-noire");
      erreur.hidden = true;
      try {
        const reponse = await api("/api/liste-noire", {
          method: "POST", silencieux: true, body: JSON.stringify({ valeur: champ.value }),
        });
        dessinerListeNoire(reponse.liste);
        champ.value = "";
        if (!reponse.ajoutee) { erreur.textContent = `${reponse.valeur} était déjà dans la liste.`; erreur.hidden = false; }
      } catch (e) {
        erreur.textContent = e.message;
        erreur.hidden = false;
      }
    });
  }
}

function dessinerListeNoire(liste) {
  const corps = document.getElementById("corps-liste-noire");
  corps.replaceChildren();
  liste.forEach((ligne) => {
    const tr = document.createElement("tr");
    [ligne.valeur, ligne.motif, ligne.ajoute_le].forEach((texte) => {
      const td = document.createElement("td");
      td.textContent = texte;  // textContent : jamais d'HTML injecté depuis une valeur saisie
      tr.appendChild(td);
    });
    corps.appendChild(tr);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  if (document.querySelector("form[data-bloc]")) initialiserReglages();
});

// --- Réglages : santé du domaine, sauvegardes, journal ---
document.addEventListener("DOMContentLoaded", () => {
  const boutonDomaine = document.getElementById("btn-domaine");
  if (!boutonDomaine) return;

  const LIBELLES_CHIP = { ok: "OK", erreur: "À corriger", attention: "À vérifier", info: "Info" };
  boutonDomaine.addEventListener("click", async () => {
    const sortie = document.getElementById("resultat-domaine");
    boutonDomaine.disabled = true;
    sortie.className = "resultat";
    sortie.textContent = "Interrogation du DNS public…";
    try {
      const r = await api("/api/domaine/verifier", { method: "POST", silencieux: true, body: "{}" });
      const liste = document.getElementById("liste-domaine");
      liste.replaceChildren();
      r.controles.forEach((c) => {
        const li = document.createElement("li");
        const chip = document.createElement("span");
        chip.className = "chip chip-domaine-" + c.statut;
        chip.textContent = c.titre + " · " + (LIBELLES_CHIP[c.statut] || c.statut);
        const texte = document.createElement("div");
        const detail = document.createElement("span");
        detail.textContent = c.detail;
        texte.appendChild(detail);
        if (c.conseil) {
          const conseil = document.createElement("span");
          conseil.className = "aide";
          conseil.textContent = " " + c.conseil;
          texte.appendChild(conseil);
        }
        li.append(chip, texte);
        liste.appendChild(li);
      });
      afficherResultat(sortie, r.statut === "ok" ? "ok" : (r.statut === "erreur" ? "erreur" : "attention"),
        r.statut === "ok" ? "domaine " + r.domaine + " bien configuré." : "des points à corriger sur " + r.domaine + ".");
    } catch (e) {
      afficherResultat(sortie, "erreur", e.message);
    } finally {
      boutonDomaine.disabled = false;
    }
  });

  document.getElementById("btn-sauvegarde").addEventListener("click", async (evenement) => {
    const bouton = evenement.currentTarget;
    const sortie = document.getElementById("resultat-sauvegarde");
    bouton.disabled = true;
    try {
      const r = await api("/api/sauvegardes", { method: "POST", silencieux: true, body: "{}" });
      const liste = document.getElementById("liste-sauvegardes");
      liste.replaceChildren();
      r.sauvegardes.forEach((s) => {
        const li = document.createElement("li");
        const nom = document.createElement("span");
        nom.textContent = s.nom;
        const taille = document.createElement("span");
        taille.className = "aide";
        taille.textContent = Math.round(s.taille / 1024) + " Ko";
        li.append(nom, taille);
        liste.appendChild(li);
      });
      afficherResultat(sortie, "ok", "sauvegarde créée : " + r.nom);
    } catch (e) {
      afficherResultat(sortie, "erreur", e.message);
    } finally {
      bouton.disabled = false;
    }
  });

  document.getElementById("btn-journal").addEventListener("click", async () => {
    const zone = document.getElementById("texte-journal");
    try {
      const r = await api("/api/journal?lignes=200", { silencieux: true });
      zone.textContent = r.lignes.length ? r.lignes.join("\n") : "Le journal est vide pour l'instant.";
      zone.hidden = false;
      zone.scrollTop = zone.scrollHeight;
    } catch (e) {
      afficherResultat(document.getElementById("resultat-journal"), "erreur", e.message);
    }
  });
});

// --- Réglages : liste des modèles du fournisseur d'IA (cliquer un modèle le choisit) ---
document.addEventListener("DOMContentLoaded", () => {
  const bouton = document.getElementById("btn-modeles");
  if (!bouton) return;
  const sortie = document.getElementById("resultat-modeles");
  const zone = document.getElementById("liste-modeles");
  bouton.addEventListener("click", async () => {
    bouton.disabled = true;
    sortie.className = "resultat";
    sortie.textContent = "Interrogation du fournisseur…";
    zone.hidden = true;
    zone.replaceChildren();
    const valeurs = {};
    ["ai_endpoint", "ai_key"].forEach((cle) => {
      const champ = document.querySelector('[data-cle="' + cle + '"]');
      if (champ) valeurs[cle] = champ.value;
    });
    try {
      const r = await api("/api/reglages/modeles", { method: "POST", silencieux: true, body: JSON.stringify({ valeurs }) });
      if (r.statut !== "ok") {
        afficherResultat(sortie, r.statut, r.message);
        return;
      }
      afficherResultat(sortie, "ok", r.modeles.length + " modèle(s) : clique sur celui que tu veux utiliser.");
      r.modeles.forEach((id) => {
        const puce = document.createElement("button");
        puce.type = "button";
        puce.className = "secondaire puce-modele";
        puce.textContent = id;
        puce.addEventListener("click", () => {
          document.getElementById("f-ai_model").value = id;
          zone.querySelectorAll(".puce-modele").forEach((p) => p.classList.toggle("choisi", p === puce));
          afficherResultat(sortie, "ok", "« " + id + " » choisi : clique sur « Enregistrer les clés et services », puis teste la connexion.");
        });
        zone.appendChild(puce);
      });
      zone.hidden = false;
    } catch (e) {
      afficherResultat(sortie, "erreur", e.message);
    } finally {
      bouton.disabled = false;
    }
  });
});

// --- Réglages : changement de l'identifiant et du mot de passe d'accès ---
document.addEventListener("DOMContentLoaded", () => {
  const formulaire = document.getElementById("form-acces");
  if (!formulaire) return;
  const sortie = document.getElementById("resultat-acces");
  formulaire.addEventListener("submit", async (evenement) => {
    evenement.preventDefault();
    effacerErreursChamps(formulaire);
    sortie.className = "resultat";
    sortie.textContent = "Enregistrement…";
    try {
      await api("/api/acces", { method: "POST", silencieux: true, body: JSON.stringify({
        identifiant: document.getElementById("ac-identifiant").value,
        actuel: document.getElementById("ac-actuel").value,
        nouveau: document.getElementById("ac-nouveau").value,
        confirmation: document.getElementById("ac-confirmation").value,
      }) });
      ["ac-actuel", "ac-nouveau", "ac-confirmation"].forEach((id) => { document.getElementById(id).value = ""; });
      afficherResultat(sortie, "ok", "accès enregistré. Tu restes connecté ; les autres sessions sont fermées.");
    } catch (e) {
      if (e.corps && e.corps.erreurs) montrerErreursChamps(e.corps.erreurs);
      afficherResultat(sortie, "erreur", e.message);
    }
  });
});

// --- Thème clair / sombre (mémorisé dans le navigateur ; sans mémoire, on suit le système) ---
document.addEventListener("DOMContentLoaded", () => {
  const bouton = document.getElementById("bouton-theme");
  if (!bouton) return;
  const sombre = () => (document.documentElement.dataset.theme || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
  function afficher() {
    bouton.querySelector(".icone-lune").hidden = sombre();
    bouton.querySelector(".icone-soleil").hidden = !sombre();
    document.getElementById("libelle-theme").textContent = sombre() ? "Thème clair" : "Thème sombre";
  }
  bouton.addEventListener("click", () => {
    const suivant = sombre() ? "light" : "dark";
    document.documentElement.dataset.theme = suivant;
    try { localStorage.setItem("theme", suivant); } catch (_) { /* stockage indisponible : valable pour cette page seulement */ }
    afficher();
  });
  afficher();
});


// Adresse web courte et lisible pour l'affichage (« instagram.com/une_fleur »), l'adresse complète reste dans le lien.
function lienCourt(url, maximum = 34) {
  try {
    const u = new URL(/^https?:\/\//i.test(url) ? url : "https://" + url);
    const texte = (u.hostname.replace(/^www\./, "") + u.pathname).replace(/\/+$/, "");
    return texte.length <= maximum ? texte : texte.slice(0, maximum - 1).replace(/[\/.\-_ ]+$/, "") + "…";
  } catch (_) {
    return url.length <= maximum ? url : url.slice(0, maximum - 1) + "…";
  }
}
