// Tableau des prospects : filtres et tri côté navigateur, sans rechargement de page.

document.addEventListener("DOMContentLoaded", () => {
  const tableau = document.getElementById("tableau-prospects");
  if (!tableau) return;
  const corps = tableau.querySelector("tbody");
  const lignes = [...corps.querySelectorAll("tr[data-statut]")];
  const vide = document.getElementById("vide-prospects");
  const texteVide = document.getElementById("texte-vide");
  const compteur = document.getElementById("nb-affiches");

  const filtres = {
    campagne: document.getElementById("f-campagne"),
    ville: document.getElementById("f-ville"),
    statut: document.getElementById("f-statut"),
    site: document.getElementById("f-site"),
    email: document.getElementById("f-email"),
    generique: document.getElementById("f-generique"),
    texte: document.getElementById("f-texte"),
  };

  function appliquer() {
    const texte = filtres.texte.value.trim().toLowerCase();
    let visibles = 0;
    lignes.forEach((ligne) => {
      const d = ligne.dataset;
      const ok = (!filtres.campagne.value || d.campagne === filtres.campagne.value)
        && (!filtres.ville.value || d.ville === filtres.ville.value)
        && (!filtres.statut.value || d.statut === filtres.statut.value)
        && (!filtres.site.value || d.site === filtres.site.value)
        && (!filtres.email.value || (filtres.email.value === "ecrit" ? d.contact !== "aucun" : d.contact === filtres.email.value))
        && (!filtres.generique.value || d.generique === filtres.generique.value)
        && (!texte || d.recherche.includes(texte));
      ligne.hidden = !ok;
      if (ok) visibles++;
    });
    compteur.textContent = visibles;
    if (lignes.length > 0) {
      vide.hidden = visibles > 0;
      texteVide.textContent = "Aucun prospect ne correspond à ces filtres.";
    }
  }
  Object.values(filtres).forEach((f) => f.addEventListener("input", appliquer));

  // Une campagne peut être présélectionnée par l'adresse : /prospects?campagne=3
  const parametre = new URLSearchParams(window.location.search).get("campagne");
  if (parametre) filtres.campagne.value = parametre;
  appliquer();

  // --- Détail du score : quels problèmes, combien de points (poids de la campagne) ---
  const fenetre = document.getElementById("detail-analyse");
  async function ouvrirDetail(prospectId) {
    let revue;
    try { revue = await api("/api/prospects/" + prospectId + "/revue"); } catch (_) { return; }
    const p = revue.prospect;
    document.getElementById("da-titre").textContent = p.nom;
    document.getElementById("da-meta").textContent = [p.ville, p.site ? lienCourt(p.site) : "aucun site renseigné"].filter(Boolean).join(" · ");
    const lignesDetail = document.getElementById("da-corps");
    lignesDetail.replaceChildren();
    revue.problemes.forEach((pb) => {
      const tr = document.createElement("tr");
      const nom = document.createElement("td");
      nom.textContent = pb.libelle;
      const code = document.createElement("span");
      code.className = "code";
      code.textContent = " " + pb.code;
      nom.appendChild(code);
      const pts = document.createElement("td");
      pts.className = "num";
      pts.textContent = pb.points > 0 ? "+" + pb.points : "0";
      tr.append(nom, pts);
      lignesDetail.appendChild(tr);
    });
    if (!revue.problemes.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 2;
      td.className = "vide";
      td.textContent = "Aucun problème détecté sur ce site.";
      tr.appendChild(td);
      lignesDetail.appendChild(tr);
    }
    document.getElementById("da-notes").value = p.notes || "";
    document.getElementById("da-enregistrer-note").dataset.prospect = p.id;
    document.getElementById("da-total").textContent = p.score;
    const plafond = Number(document.body.dataset.plafondScore);  // fourni par le serveur (une seule source)
    document.getElementById("da-aide").textContent = "Score affiché : " + Math.min(p.score, plafond)
      + " sur " + plafond + " (le classement utilise le score brut). Les critères à 0 point ou désactivés ne comptent pas ; "
      + "leur poids se règle sur la fiche de la campagne.";
    fenetre.showModal();
  }
  document.getElementById("da-enregistrer-note").addEventListener("click", async (evenement) => {
    const bouton = evenement.currentTarget;
    try {
      await api("/api/prospects/" + bouton.dataset.prospect + "/notes", { method: "POST", body: JSON.stringify({ notes: document.getElementById("da-notes").value }) });
      toast("Note enregistrée.", "info", 2500);
    } catch (_) { /* erreur déjà affichée */ }
  });
  corps.addEventListener("click", (e) => {
    const bouton = e.target.closest("button[data-detail]");
    if (bouton) ouvrirDetail(Number(bouton.dataset.detail));
  });

  // --- Sélection, génération de messages, ignorer / remettre en qualifié ---------------
  const seuil = Number(tableau.dataset.seuil);
  const barre = document.getElementById("barre-actions");
  const toutCocher = document.getElementById("tout-cocher");
  const sortieLot = document.getElementById("resultat-lot");
  const coches = () => [...corps.querySelectorAll("tr[data-statut]")].filter((l) => l.querySelector(".coche").checked);

  function majSelection() {
    const n = coches().length;
    document.getElementById("nb-selection").textContent = n;
    barre.hidden = n === 0;
  }
  corps.addEventListener("change", (e) => { if (e.target.classList.contains("coche")) majSelection(); });
  toutCocher.addEventListener("change", () => {
    lignes.forEach((l) => { l.querySelector(".coche").checked = toutCocher.checked && !l.hidden; });
    majSelection();
  });

  // Rédaction : suivi de l'avancement réel (table jobs), comme pour le scan
  const panneauRedaction = document.getElementById("panneau-redaction");
  async function suivreRedaction(jobId) {
    panneauRedaction.hidden = false;
    for (;;) {
      let job;
      try {
        job = await api("/api/jobs/" + jobId, { silencieux: true });
      } catch (e) {
        document.getElementById("redaction-detail").textContent = e.message;
        return;
      }
      document.getElementById("redaction-barre").value = job.progression;
      const etats = { en_attente: "en attente…", en_cours: "en cours…", termine: "terminée", erreur: "en erreur" };
      document.getElementById("redaction-titre").textContent = "Rédaction — " + (etats[job.statut] || job.statut);
      const detail = document.getElementById("redaction-detail");
      detail.textContent = job.detail || "";
      detail.className = "job-detail" + (job.statut === "erreur" ? " statut-erreur" : "");
      if (job.statut === "termine" || job.statut === "erreur") {
        window.setTimeout(() => window.location.reload(), 1500);
        return;
      }
      await new Promise((r) => window.setTimeout(r, 1500));
    }
  }
  if (panneauRedaction.dataset.actif === "1" && panneauRedaction.dataset.jobId) suivreRedaction(panneauRedaction.dataset.jobId);

  async function lancerGeneration(ids, forcer) {
    try {
      const r = await api("/api/messages/generer", { method: "POST", body: JSON.stringify({ prospect_ids: ids, forcer }) });
      panneauRedaction.dataset.jobId = r.job_id;
      document.getElementById("redaction-detail").textContent = "Démarrage…";
      suivreRedaction(r.job_id);
    } catch (_) { /* l'erreur est affichée en haut de page (clé manquante, tâche déjà en cours…) */ }
  }

  document.getElementById("btn-generer-lot").addEventListener("click", () => {
    const selection = coches();
    const ids = selection.map((l) => Number(l.dataset.id));
    const sous = selection.filter((l) => Number(l.dataset.score) < seuil).length;
    let forcer = false;
    if (sous > 0) {
      forcer = confirm(sous + " prospect(s) sur " + ids.length + " ont un score inférieur au seuil (" + seuil + ").\n\n"
        + "OK : générer aussi leur message (consomme des crédits IA).\nAnnuler : générer seulement pour les autres.");
    }
    lancerGeneration(ids, forcer);
  });
  corps.addEventListener("click", async (e) => {
    const bouton = e.target.closest("button");
    if (!bouton) return;
    if (bouton.dataset.generer) {
      lancerGeneration([Number(bouton.dataset.generer)], true);  // un prospect précis : génération forcée
    } else if (bouton.dataset.supprimer) {
      await supprimer([Number(bouton.dataset.supprimer)]);
    } else if (bouton.dataset.ignorer || bouton.dataset.qualifier) {
      await changerStatut([Number(bouton.dataset.ignorer || bouton.dataset.qualifier)], bouton.dataset.ignorer ? "ignore" : "qualifie");
    }
  });
  document.getElementById("btn-ignorer-lot").addEventListener("click", () =>
    changerStatut(coches().map((l) => Number(l.dataset.id)), "ignore"));

  document.getElementById("btn-supprimer-lot").addEventListener("click", () =>
    supprimer(coches().map((l) => Number(l.dataset.id))));

  // Suppression définitive, confirmée. Le serveur garde les prospects déjà contactés par email (leur historique sert aux quotas et aux relances).
  async function supprimer(ids) {
    if (!ids.length) return;
    const quoi = ids.length === 1 ? "ce prospect" : ids.length + " prospects";
    if (!confirm("Supprimer définitivement " + quoi + " ?\n\nSes messages et son journal de contact disparaissent aussi. "
      + "Un nouveau scan pourra le retrouver : pour l'écarter sans l'oublier, utilise « Ignorer ».\n"
      + "Les prospects déjà contactés par email sont conservés.")) return;
    try {
      const r = await api("/api/prospects/supprimer", { method: "POST", body: JSON.stringify({ prospect_ids: ids }) });
      const refuses = r.refuses.contactes + r.refuses.tache_en_cours + r.refuses.introuvables;
      if (refuses) {
        const raisons = [];
        if (r.refuses.contactes) raisons.push(r.refuses.contactes + " déjà contacté(s) par email (conservés)");
        if (r.refuses.tache_en_cours) raisons.push(r.refuses.tache_en_cours + " dans une campagne où une tâche est en cours");
        if (r.refuses.introuvables) raisons.push(r.refuses.introuvables + " introuvable(s)");
        afficherErreur(r.supprimes + " supprimé(s). Non supprimés : " + raisons.join(", ") + ".");
      }
      if (r.supprimes) {
        if (!refuses) window.location.reload();
        else setTimeout(() => window.location.reload(), 3500);
      }
    } catch (_) { /* bannière affichée */ }
  }

  async function changerStatut(ids, statut) {
    try {
      const r = await api("/api/prospects/statut", { method: "POST", body: JSON.stringify({ prospect_ids: ids, statut }) });
      if (r.refuses) afficherErreur(r.refuses + " prospect(s) non modifié(s) : déjà envoyés ou dans un statut incompatible.");
      if (r.modifies) window.location.reload();
    } catch (_) { /* bannière affichée */ }
  }

  // Export CSV : la sélection s'il y en a une, sinon toutes les lignes affichées (filtres appliqués), dans l'ordre à l'écran
  document.getElementById("btn-exporter").addEventListener("click", async () => {
    const choisies = coches();
    const lignesExport = choisies.length ? choisies : [...corps.querySelectorAll("tr[data-statut]")].filter((l) => !l.hidden);
    if (!lignesExport.length) { afficherErreur("Aucune ligne à exporter avec ces filtres."); return; }
    try {
      const reponse = await fetch("/api/prospects/export", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prospect_ids: lignesExport.map((l) => Number(l.dataset.id)) }),
      });
      if (!reponse.ok) throw new Error("Erreur " + reponse.status);
      const blob = await reponse.blob();
      const lien = document.createElement("a");
      lien.href = URL.createObjectURL(blob);
      lien.download = (reponse.headers.get("Content-Disposition") || "").split('filename="')[1]?.replace('"', "") || "prospects.csv";
      document.body.appendChild(lien);
      lien.click();
      lien.remove();
      URL.revokeObjectURL(lien.href);
    } catch (e) {
      afficherErreur("L'export a échoué : " + e.message);
    }
  });

  // Import CSV : envoi du fichier, puis compte rendu complet (rien n'est masqué)
  const panneauImport = document.getElementById("panneau-import");
  document.getElementById("ouvrir-import").addEventListener("click", () => { panneauImport.hidden = false; });
  document.getElementById("annuler-import").addEventListener("click", () => { panneauImport.hidden = true; });
  document.getElementById("form-import").addEventListener("submit", async (evenement) => {
    evenement.preventDefault();
    const rapport = document.getElementById("rapport-import");
    const bouton = evenement.currentTarget.querySelector('button[type="submit"]');
    const fichier = document.getElementById("import-fichier").files[0];
    const campagne = document.getElementById("import-campagne").value;
    rapport.replaceChildren();
    const ligne = (texte, classe) => {
      const p = document.createElement("p");
      p.className = classe || "aide";
      p.textContent = texte;
      rapport.appendChild(p);
    };
    if (!campagne) { ligne("Choisis la campagne qui recevra les prospects.", "erreur-champ"); return; }
    if (!fichier) { ligne("Choisis un fichier CSV.", "erreur-champ"); return; }
    const donnees = new FormData();
    donnees.append("fichier", fichier);
    donnees.append("campagne_id", campagne);
    bouton.disabled = true;
    ligne("Import en cours…");
    try {
      const reponse = await fetch("/api/prospects/import", { method: "POST", body: donnees });
      const corps = await reponse.json();
      rapport.replaceChildren();
      if (!reponse.ok) { ligne(corps.erreur || "Erreur " + reponse.status, "erreur-champ"); return; }
      ligne(corps.importes + " prospect(s) importé(s), " + corps.ignores + " ignoré(s) (déjà en base"
        + (corps.autres_campagnes ? ", dont " + corps.autres_campagnes + " dans une autre campagne" : "") + "), "
        + corps.invalides.length + " ligne(s) invalide(s).", "resultat statut-ok");
      corps.invalides.forEach(([n, raison]) => ligne("Ligne " + n + " : " + raison, "erreur-champ"));
      corps.avertissements.forEach((a) => ligne(a, "resultat statut-attention"));
      if (corps.colonnes_ignorees.length) ligne("Colonnes ignorées : " + corps.colonnes_ignorees.join(", "));
      if (corps.job_id) ligne("L'analyse des sites est lancée : suis-la sur la fiche de la campagne.");
      if (corps.importes) {
        const recharger = document.createElement("button");
        recharger.type = "button";
        recharger.className = "bouton";
        recharger.textContent = "Afficher les prospects importés";
        recharger.addEventListener("click", () => { window.location.href = "/prospects?campagne=" + campagne; });
        rapport.appendChild(recharger);
      }
    } catch (e) {
      rapport.replaceChildren();
      ligne("Le serveur local ne répond pas : " + e.message, "erreur-champ");
    } finally {
      bouton.disabled = false;
    }
  });

  // Tri par clic sur un en-tête (le score brut sert au classement, pas le score affiché)
  let tri = { cle: "score", sens: -1 };
  tableau.querySelectorAll("th[data-tri]").forEach((th) => {
    th.addEventListener("click", () => {
      const cle = th.dataset.tri;
      tri = { cle, sens: tri.cle === cle ? -tri.sens : (th.dataset.numerique ? -1 : 1) };
      const valeur = (ligne) => {
        const v = ligne.dataset[cle] || "";
        return th.dataset.numerique ? Number(v) : v;
      };
      lignes.sort((a, b) => {
        const va = valeur(a), vb = valeur(b);
        return (va < vb ? -1 : va > vb ? 1 : 0) * tri.sens;
      });
      lignes.forEach((l) => corps.insertBefore(l, vide));
      tableau.querySelectorAll("th[aria-sort]").forEach((autre) => autre.removeAttribute("aria-sort"));
      th.setAttribute("aria-sort", tri.sens > 0 ? "ascending" : "descending");
    });
  });
});
