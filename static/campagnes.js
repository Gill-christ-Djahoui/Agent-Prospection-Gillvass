// Écrans Campagnes (liste) et fiche d'une campagne. Dépend de app.js (api, afficherErreur, afficherResultat).

function erreursDans(portee, erreurs) {
  portee.querySelectorAll("[data-erreur-pour]").forEach((p) => { p.hidden = true; p.textContent = ""; });
  Object.entries(erreurs || {}).forEach(([cle, message]) => {
    const p = portee.querySelector(`[data-erreur-pour="${cle}"]`);
    if (p) { p.textContent = message; p.hidden = false; }
  });
}

function messageDeValidation(e, defaut) {
  return e.corps && e.corps.erreurs ? "corrige les champs signalés, rien n'a été enregistré." : (defaut || e.message);
}

// Suppression d'une campagne (liste ou fiche). Une première confirmation ; une seconde, plus grave, si des emails
// ont déjà été envoyés (le serveur répond 409 : l'historique d'envoi alimente le quota, les relances et l'anti-doublon).
// Renvoie true si la campagne a bien été supprimée.
async function supprimerCampagne(id, nom, nbProspects) {
  const contenu = nbProspects > 0
    ? ` Ses ${nbProspects} prospect(s), leurs messages et leurs notes seront supprimés aussi.`
    : "";
  if (!confirm(`Supprimer définitivement la campagne « ${nom} » ?${contenu}\n\nCette action est irréversible. La liste noire est conservée.`)) return false;
  try {
    await api(`/api/campagnes/${id}`, { method: "DELETE", silencieux: true, body: "{}" });
    return true;
  } catch (e) {
    if (!(e.corps && e.corps.confirmation_requise)) {
      afficherErreur(e.message);
      return false;
    }
    if (!confirm(`${e.message}\n\nSupprimer quand même, historique d'envoi compris ?`)) return false;
    try {
      await api(`/api/campagnes/${id}`, { method: "DELETE", silencieux: true, body: JSON.stringify({ forcer: true }) });
      return true;
    } catch (e2) {
      afficherErreur(e2.message);
      return false;
    }
  }
}

// --- Liste des campagnes -------------------------------------------------------------

function initialiserListe() {
  document.querySelectorAll("[data-supprimer-campagne]").forEach((bouton) => bouton.addEventListener("click", async () => {
    bouton.disabled = true;
    const faite = await supprimerCampagne(bouton.dataset.supprimerCampagne, bouton.dataset.nom, Number(bouton.dataset.prospects));
    if (!faite) { bouton.disabled = false; return; }
    const tableau = bouton.closest("tbody");
    bouton.closest("tr").remove();
    toast(`Campagne « ${bouton.dataset.nom} » supprimée.`, "info", 4000);
    if (!tableau.children.length) window.location.reload();  // plus aucune ligne : l'écran affiche son état vide
  }));

  const panneau = document.getElementById("panneau-nouvelle");
  const formulaire = document.getElementById("form-nouvelle");
  const depart = document.getElementById("n-depart");
  const sortie = document.getElementById("resultat-nouvelle");

  document.getElementById("ouvrir-nouvelle").addEventListener("click", () => {
    panneau.hidden = false;
    document.getElementById("n-nom").focus();
  });
  document.getElementById("annuler-nouvelle").addEventListener("click", () => { panneau.hidden = true; });
  depart.addEventListener("change", () => {
    document.getElementById("champs-vierge").hidden = depart.value !== "";
  });

  formulaire.addEventListener("submit", async (evenement) => {
    evenement.preventDefault();
    const bouton = formulaire.querySelector('button[type="submit"]');
    erreursDans(formulaire, {});
    bouton.disabled = true;
    sortie.className = "resultat";
    sortie.textContent = "Création…";
    const donnees = {
      nom: document.getElementById("n-nom").value,
      modele_depart: depart.value,
      secteur: document.getElementById("n-secteur").value,
      pays: document.getElementById("n-pays").value,
      langue: document.getElementById("n-langue").value,
    };
    try {
      const reponse = await api("/api/campagnes", { method: "POST", silencieux: true, body: JSON.stringify(donnees) });
      window.location.href = reponse.url;
    } catch (e) {
      if (e.corps && e.corps.erreurs) erreursDans(formulaire, e.corps.erreurs);
      afficherResultat(sortie, "erreur", messageDeValidation(e));
      bouton.disabled = false;
    }
  });
}

// --- Fiche d'une campagne ------------------------------------------------------------

function initialiserFiche() {
  const id = document.getElementById("fiche").dataset.campagneId;
  try {  // arrivée depuis l'assistant de niche : ce qui a été écarté, et la suite
    const garde = sessionStorage.getItem("niche-creee-" + id);
    if (garde) {
      sessionStorage.removeItem("niche-creee-" + id);
      const { ecartees } = JSON.parse(garde);
      toast("Campagne créée en brouillon. Relis-la, puis lance le scan complet.", "info", 9000);
      if (ecartees && ecartees.length) {
        toast(`${ecartees.length} zone(s) écartée(s) : ` + ecartees.map((z) => `${z.nom} (${z.raison})`).join(" ; "), "erreur", 20000);
      }
    }
  } catch (_) { /* stockage indisponible : simplement pas de rappel */ }
  initialiserInfos(id);
  initialiserActionsEntete(id);
  initialiserZones(id);
  initialiserCriteres(id);
  initialiserAngle(id);
  initialiserScan(id);
}

// Scan : lancement, puis suivi de l'avancement RÉEL lu dans la table jobs (interrogée toutes les 1,5 s)
function initialiserScan(id) {
  const panneau = document.getElementById("panneau-job");
  const barre = document.getElementById("job-barre");
  const detail = document.getElementById("job-detail");
  const titre = document.getElementById("job-titre");
  const boutons = () => document.querySelectorAll("#btn-scan-complet, #btn-analyser, #btn-reanalyser, #btn-contacts, [data-scanner-zone]");
  const TYPES = { scan: "Scan", analyse: "Analyse des sites", redaction: "Rédaction", envoi: "Envoi" };
  const ETATS = { en_attente: "en attente…", en_cours: "en cours…", termine: "terminé", erreur: "en erreur" };

  async function suivre(jobId) {
    panneau.hidden = false;
    boutons().forEach((b) => { b.disabled = true; });
    for (;;) {
      let job;
      try {
        job = await api("/api/jobs/" + jobId, { silencieux: true });
      } catch (e) {
        detail.textContent = e.message;
        detail.className = "job-detail statut-erreur";
        return;
      }
      barre.value = job.progression;
      titre.textContent = (TYPES[job.type] || job.type) + " — " + (ETATS[job.statut] || job.statut);
      detail.textContent = job.detail || "";
      detail.className = "job-detail" + (job.statut === "erreur" ? " statut-erreur" : "");
      if (job.statut === "termine" || job.statut === "erreur") {
        window.setTimeout(() => window.location.reload(), 1200);  // le tableau des zones se met à jour
        return;
      }
      await new Promise((r) => window.setTimeout(r, 1500));
    }
  }

  async function lancer(zoneIds) {
    boutons().forEach((b) => { b.disabled = true; });
    try {
      const r = await api("/api/campagnes/" + id + "/scan", { method: "POST", body: JSON.stringify({ zone_ids: zoneIds }) });
      panneau.dataset.jobId = r.job_id;
      barre.value = 0;
      detail.className = "job-detail";
      detail.textContent = "Démarrage…";
      suivre(r.job_id);
    } catch (_) {
      boutons().forEach((b) => { b.disabled = false; });  // l'erreur est déjà affichée en haut de page
    }
  }

  const complet = document.getElementById("btn-scan-complet");
  if (complet) complet.addEventListener("click", () => {
    const zones = document.querySelectorAll("tr[data-zone]").length;
    if (!confirm("Scanner les " + zones + " zone(s) de cette campagne ? Cela peut prendre plusieurs minutes.")) return;
    lancer(null);
  });
  document.querySelectorAll("[data-scanner-zone]").forEach((b) =>
    b.addEventListener("click", () => lancer([Number(b.dataset.scannerZone)])));

  async function lancerAnalyse(tous) {
    boutons().forEach((b) => { b.disabled = true; });
    try {
      const r = await api("/api/campagnes/" + id + "/analyser", { method: "POST", body: JSON.stringify({ tous }) });
      panneau.dataset.jobId = r.job_id;
      barre.value = 0;
      detail.className = "job-detail";
      detail.textContent = "Démarrage…";
      suivre(r.job_id);
    } catch (_) {
      boutons().forEach((b) => { b.disabled = false; });
    }
  }
  const analyser = document.getElementById("btn-analyser");
  if (analyser) analyser.addEventListener("click", () => lancerAnalyse(false));
  const reanalyser = document.getElementById("btn-reanalyser");
  if (reanalyser) reanalyser.addEventListener("click", () => {
    if (!confirm("Re-télécharger et ré-analyser le site de tous les prospects qualifiés de cette campagne ? Cela peut prendre plusieurs minutes.")) return;
    lancerAnalyse(true);
  });

  const contacts = document.getElementById("btn-contacts");
  if (contacts) contacts.addEventListener("click", async () => {
    if (!confirm("Chercher l'email, à défaut le formulaire de contact, des prospects qui ont un site propre mais aucun email connu ? Les sites sont visités poliment (robots.txt respecté) : cela peut prendre plusieurs minutes.")) return;
    boutons().forEach((b) => { b.disabled = true; });
    try {
      const r = await api("/api/campagnes/" + id + "/contacts", { method: "POST", body: "{}" });
      panneau.dataset.jobId = r.job_id;
      barre.value = 0;
      detail.className = "job-detail";
      detail.textContent = "Démarrage…";
      suivre(r.job_id);
    } catch (_) {
      boutons().forEach((b) => { b.disabled = false; });
    }
  });

  // Recalcul des scores : instantané, sans réseau (les problèmes détectés sont déjà enregistrés)
  const recalculer = document.getElementById("btn-recalculer");
  const sortieRecalcul = document.getElementById("resultat-recalcul");
  recalculer.addEventListener("click", async () => {
    recalculer.disabled = true;
    sortieRecalcul.className = "resultat";
    sortieRecalcul.textContent = "Recalcul en cours…";
    try {
      const r = await api("/api/campagnes/" + id + "/recalculer", { method: "POST", silencieux: true, body: "{}" });
      document.getElementById("invite-recalcul").hidden = true;
      recalculer.classList.remove("bouton");
      recalculer.classList.add("secondaire");
      afficherResultat(sortieRecalcul, "ok", r.modifies + " score(s) modifié(s) sur " + r.prospects + " prospect(s) analysé(s).");
    } catch (e) {
      afficherResultat(sortieRecalcul, "erreur", e.message);
    } finally {
      recalculer.disabled = false;
    }
  });

  if (panneau.dataset.actif === "1" && panneau.dataset.jobId) suivre(panneau.dataset.jobId);
}

function initialiserActionsEntete(id) {
  const dupliquer = document.getElementById("btn-dupliquer");
  dupliquer.addEventListener("click", async () => {
    dupliquer.disabled = true;
    try {
      const r = await api(`/api/campagnes/${id}/dupliquer`, { method: "POST", body: "{}" });
      window.location.href = r.url;
    } catch (_) { dupliquer.disabled = false; }
  });

  const modele = document.getElementById("btn-comme-modele");
  if (modele) modele.addEventListener("click", async () => {
    modele.disabled = true;
    try {
      const r = await api(`/api/campagnes/${id}/dupliquer`, { method: "POST", body: JSON.stringify({ comme_modele: true }) });
      window.location.href = r.url;
    } catch (_) { modele.disabled = false; }
  });

  const archiver = document.getElementById("btn-archiver");
  if (archiver) archiver.addEventListener("click", async () => {
    const archive = archiver.dataset.archiver === "true";
    if (archive && !confirm("Archiver cette campagne ? Ses prospects et son historique sont conservés.")) return;
    try {
      await api(`/api/campagnes/${id}/archiver`, { method: "POST", body: JSON.stringify({ archiver: archive }) });
      window.location.reload();
    } catch (_) { /* bannière déjà affichée par api() */ }
  });

  const supprimer = document.getElementById("btn-supprimer-campagne");
  if (supprimer) supprimer.addEventListener("click", async () => {
    supprimer.disabled = true;
    if (await supprimerCampagne(id, supprimer.dataset.nom, Number(supprimer.dataset.prospects))) {
      window.location.href = "/campagnes";
    } else {
      supprimer.disabled = false;
    }
  });
}

// Informations générales + requêtes (ajoutables et supprimables une par une)
function initialiserInfos(id) {
  const bloc = document.getElementById("bloc-infos");
  const liste = document.getElementById("liste-requetes");
  const champ = document.getElementById("i-requete");
  let requetes = JSON.parse(liste.dataset.requetes);

  function dessiner() {
    liste.replaceChildren();
    if (requetes.length === 0) {
      const vide = document.createElement("li");
      vide.className = "vide";
      vide.textContent = "Aucune requête. Ajoutes-en au moins une pour pouvoir lancer un scan.";
      liste.appendChild(vide);
      return;
    }
    requetes.forEach((requete, index) => {
      const li = document.createElement("li");
      li.className = "puce";
      const texte = document.createElement("span");
      texte.textContent = requete;
      const retirer = document.createElement("button");
      retirer.type = "button";
      retirer.className = "retirer";
      retirer.textContent = "×";
      retirer.setAttribute("aria-label", `Retirer la requête ${requete}`);
      retirer.addEventListener("click", () => { requetes.splice(index, 1); dessiner(); });
      li.append(texte, retirer);
      liste.appendChild(li);
    });
  }

  function ajouter() {
    // une virgule ou un point-virgule sépare deux recherches : « avocat, cabinet d'avocats » en fait deux
    champ.value.split(/[,;]/).map((v) => v.trim().replace(/\s+/g, " ")).filter(Boolean).forEach((valeur) => {
      if (!requetes.some((r) => r.toLowerCase() === valeur.toLowerCase())) requetes.push(valeur);
    });
    champ.value = "";
    dessiner();
  }
  document.getElementById("btn-ajout-requete").addEventListener("click", ajouter);
  champ.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); ajouter(); } });
  dessiner();

  const sortie = document.getElementById("resultat-infos");
  document.getElementById("btn-enregistrer-infos").addEventListener("click", async (evenement) => {
    const bouton = evenement.currentTarget;
    ajouter(); // une requête tapée mais pas encore ajoutée ne doit pas être perdue
    const donnees = { requetes };
    bloc.querySelectorAll("[data-champ]").forEach((c) => { donnees[c.dataset.champ] = c.value; });
    erreursDans(bloc, {});
    bouton.disabled = true;
    sortie.className = "resultat";
    sortie.textContent = "Enregistrement…";
    try {
      await api(`/api/campagnes/${id}`, { method: "POST", silencieux: true, body: JSON.stringify(donnees) });
      document.querySelector("h1").textContent = donnees.nom.trim();
      afficherResultat(sortie, "ok", "enregistré.");
    } catch (e) {
      if (e.corps && e.corps.erreurs) erreursDans(bloc, e.corps.erreurs);
      afficherResultat(sortie, "erreur", messageDeValidation(e));
    } finally {
      bouton.disabled = false;
    }
  });
}

// Zones : ajout en série avec progression réelle, géocodage, correction, suppression
function initialiserZones(id) {
  const cleJournal = `journal-zones-${id}`;
  const journal = document.getElementById("journal-zones");
  const sortie = document.getElementById("resultat-zones");

  function ligneJournal(statut, texte) {
    const li = document.createElement("li");
    li.className = "statut-" + statut;
    li.textContent = texte;
    journal.appendChild(li);
  }

  try {
    const garde = sessionStorage.getItem(cleJournal);
    if (garde) {
      JSON.parse(garde).forEach(([statut, texte]) => ligneJournal(statut, texte));
      sessionStorage.removeItem(cleJournal);
    }
  } catch (_) { /* stockage indisponible : le journal n'est simplement pas rejoué */ }

  document.getElementById("btn-ajout-zones").addEventListener("click", async (evenement) => {
    const bouton = evenement.currentTarget;
    const villes = document.getElementById("z-villes").value.split("\n").map((v) => v.trim()).filter(Boolean);
    if (villes.length === 0) {
      afficherResultat(sortie, "attention", "saisis au moins une ville (une par ligne).");
      return;
    }
    const pays = document.getElementById("z-pays").value;
    const rayon = document.getElementById("z-rayon").value;
    bouton.disabled = true;
    journal.replaceChildren();
    const trace = [];
    for (let i = 0; i < villes.length; i++) {
      sortie.className = "resultat";
      sortie.textContent = `Ajout ${i + 1}/${villes.length} : ${villes[i]}…`;
      let entree;
      try {
        const r = await api(`/api/campagnes/${id}/zones`, {
          method: "POST", silencieux: true,
          body: JSON.stringify({ ville: villes[i], pays, rayon_km: rayon }),
        });
        const g = r.geocodage;
        if (g.statut === "ok") entree = ["ok", `${villes[i]} : ajoutée et localisée.`];
        else if (g.statut === "introuvable") entree = ["attention", `${villes[i]} : ville introuvable, ajoutée sans coordonnées (corrige-les dans le tableau).`];
        else entree = ["attention", `${villes[i]} : ajoutée sans coordonnées, géocodage en erreur : ${g.message}`];
      } catch (e) {
        entree = ["erreur", `${villes[i]} : ${e.message}`];
      }
      trace.push(entree);
      ligneJournal(...entree);
    }
    try { sessionStorage.setItem(cleJournal, JSON.stringify(trace)); } catch (_) { /* sans importance */ }
    window.location.reload();
  });

  document.querySelectorAll("[data-geocoder]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    b.textContent = "Géocodage…";
    try {
      const r = await api(`/api/zones/${b.dataset.geocoder}/geocoder`, { method: "POST", body: "{}" });
      if (r.statut === "ok") { window.location.reload(); return; }
      afficherErreur(r.statut === "introuvable"
        ? "Toujours introuvable pour OpenStreetMap : utilise « Saisir les coordonnées »."
        : r.message);
    } catch (_) { /* bannière affichée */ }
    b.disabled = false;
    b.textContent = "Réessayer le géocodage";
  }));

  document.querySelectorAll("[data-coordonnees]").forEach((b) => b.addEventListener("click", async () => {
    const saisie = prompt("Latitude et longitude, séparées par une virgule (exemple : 45.764, 4.835)");
    if (saisie === null) return;
    const [lat, lon] = saisie.split(/[,; ]+/).filter(Boolean);
    try {
      await api(`/api/zones/${b.dataset.coordonnees}/geocoder`, { method: "POST", body: JSON.stringify({ lat, lon }) });
      window.location.reload();
    } catch (_) { /* bannière affichée */ }
  }));

  document.querySelectorAll("[data-supprimer-zone]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Retirer la zone ${b.dataset.ville} de cette campagne ?`)) return;
    try {
      await api(`/api/zones/${b.dataset.supprimerZone}`, { method: "DELETE" });
      window.location.reload();
    } catch (_) { /* bannière affichée */ }
  }));
}

// Critères : enregistrement à chaque modification
function initialiserCriteres(id) {
  document.querySelectorAll("#bloc-criteres tr[data-code]").forEach((ligne) => {
    const points = ligne.querySelector("[data-points]");
    const actif = ligne.querySelector("[data-actif]");
    const etat = ligne.querySelector("[data-etat]");
    async function enregistrer() {
      etat.className = "resultat";
      etat.textContent = "Enregistrement…";
      try {
        await api(`/api/campagnes/${id}/criteres`, {
          method: "POST", silencieux: true,
          body: JSON.stringify({ code: ligne.dataset.code, points: points.value, actif: actif.checked }),
        });
        afficherResultat(etat, "ok", "enregistré");
        // Un poids a changé : on propose le recalcul des scores (action principale tant qu'il n'est pas fait)
        document.getElementById("invite-recalcul").hidden = false;
        const bouton = document.getElementById("btn-recalculer");
        bouton.classList.remove("secondaire");
        bouton.classList.add("bouton");
      } catch (e) {
        afficherResultat(etat, "erreur", e.message);
      }
    }
    points.addEventListener("change", enregistrer);
    actif.addEventListener("change", enregistrer);
  });
}

// Angle du message : rattachement, édition du prompt, copie
function initialiserAngle(id) {
  const bloc = document.getElementById("bloc-angle");
  const modeles = JSON.parse(bloc.dataset.modeles);
  const choix = document.getElementById("a-choix");
  const champs = {
    nom: document.getElementById("a-nom"),
    objet_template: document.getElementById("a-objet"),
    prompt_systeme: document.getElementById("a-prompt"),
    signature: document.getElementById("a-signature"),
  };
  const usage = document.getElementById("a-usage");
  const sortie = document.getElementById("resultat-angle");

  function afficher() {
    const modele = modeles.find((m) => String(m.id) === choix.value);
    if (!modele) return;
    Object.entries(champs).forEach(([cle, champ]) => { champ.value = modele[cle] || ""; });
    usage.textContent = modele.nb_campagnes > 1
      ? `Cet angle est utilisé par ${modele.nb_campagnes} campagnes : le modifier les modifie toutes. Utilise « Copier en un nouvel angle » pour un prompt propre à cette campagne.`
      : "Cet angle n'est utilisé que par cette campagne.";
  }
  afficher();

  choix.addEventListener("change", async () => {
    try {
      await api(`/api/campagnes/${id}`, { method: "POST", body: JSON.stringify({ modele_message_id: choix.value || null }) });
      window.location.reload();
    } catch (_) { /* bannière affichée */ }
  });

  document.getElementById("btn-enregistrer-angle").addEventListener("click", async (evenement) => {
    const bouton = evenement.currentTarget;
    const donnees = {};
    Object.entries(champs).forEach(([cle, champ]) => { donnees[cle] = champ.value; });
    erreursDans(bloc, {});
    bouton.disabled = true;
    sortie.className = "resultat";
    sortie.textContent = "Enregistrement…";
    try {
      await api(`/api/modeles/${choix.value}`, { method: "POST", silencieux: true, body: JSON.stringify(donnees) });
      const modele = modeles.find((m) => String(m.id) === choix.value);
      Object.assign(modele, donnees);
      afficherResultat(sortie, "ok", "angle enregistré. Les prochains messages le suivront.");
    } catch (e) {
      if (e.corps && e.corps.erreurs) erreursDans(bloc, e.corps.erreurs);
      afficherResultat(sortie, "erreur", messageDeValidation(e));
    } finally {
      bouton.disabled = false;
    }
  });

  document.getElementById("btn-copier-angle").addEventListener("click", async () => {
    try {
      await api(`/api/campagnes/${id}/angle`, { method: "POST", body: "{}" });
      window.location.reload();
    } catch (_) { /* bannière affichée */ }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  if (document.getElementById("form-nouvelle")) initialiserListe();
  if (document.getElementById("fiche")) initialiserFiche();
});
