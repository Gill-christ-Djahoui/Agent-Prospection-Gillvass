// Écran Envois : envoi, suivi réel (table jobs), arrêt d'urgence, suivi Brevo, relances, historique filtrable.

document.addEventListener("DOMContentLoaded", () => {
  const bouton = document.getElementById("btn-envoyer");
  if (!bouton) return;
  const arreter = document.getElementById("btn-arreter");
  const panneau = document.getElementById("panneau-job");
  const barre = document.getElementById("job-barre");
  const detail = document.getElementById("job-detail");
  const titre = document.getElementById("job-titre");
  const sortie = document.getElementById("resultat-envoi");
  const ETATS = { en_attente: "en attente…", en_cours: "en cours…", termine: "terminé", erreur: "en erreur" };

  // Suit une tâche de la table jobs jusqu'à sa fin ; `fin` est appelé avec la tâche terminée.
  async function suivreJob(jobId, ecran, fin) {
    for (;;) {
      let job;
      try {
        job = await api("/api/jobs/" + jobId, { silencieux: true });
      } catch (e) {
        ecran.detail.textContent = e.message;
        return;
      }
      ecran.barre.value = job.progression;
      if (ecran.titre) ecran.titre.textContent = ecran.libelle + " — " + (ETATS[job.statut] || job.statut);
      ecran.detail.textContent = job.detail || "";
      ecran.detail.className = "job-detail" + (job.statut === "erreur" ? " statut-erreur" : "");
      if (job.statut === "termine" || job.statut === "erreur") { fin(job); return; }
      await new Promise((r) => window.setTimeout(r, 1500));
    }
  }

  function suivreEnvoi(jobId) {
    panneau.hidden = false;
    arreter.hidden = false;
    bouton.disabled = true;
    suivreJob(jobId, { barre, detail, titre, libelle: "Dernier envoi" }, () => window.setTimeout(() => window.location.reload(), 1500));
  }

  bouton.addEventListener("click", async () => {
    const n = bouton.dataset.aEnvoyer;
    if (!confirm("Envoyer " + n + " email(s) maintenant ?\n\nChaque message part à son destinataire réel, avec une pause entre deux envois (environ "
      + bouton.dataset.pause + " min au total). Tu peux arrêter à tout moment.")) return;
    bouton.disabled = true;
    try {
      const r = await api("/api/envois/envoyer", { method: "POST", body: "{}" });
      panneau.dataset.jobId = r.job_id;
      barre.value = 0;
      detail.textContent = "Démarrage…";
      suivreEnvoi(r.job_id);
    } catch (_) {
      bouton.disabled = false;  // l'erreur est affichée en haut de page
    }
  });

  arreter.addEventListener("click", async () => {
    arreter.disabled = true;
    try {
      await api("/api/envois/arreter", { method: "POST", body: "{}" });
      afficherResultat(sortie, "attention", "arrêt demandé : l'envoi s'arrête avant le prochain email.");
    } catch (_) { arreter.disabled = false; }
  });

  if (panneau.dataset.actif === "1" && panneau.dataset.jobId) suivreEnvoi(panneau.dataset.jobId);

  // --- Envois incertains : à résoudre après vérification dans Brevo ---
  document.querySelectorAll("[data-resoudre]").forEach((b) => b.addEventListener("click", async () => {
    const envoye = b.dataset.envoye === "1";
    if (!confirm(envoye ? "Confirmer que cet email est bien parti (vérifié dans Brevo) ?"
      : "Confirmer que cet email n'est PAS parti (vérifié dans Brevo) ? Il pourra être renvoyé.")) return;
    try {
      await api("/api/envois/" + b.dataset.resoudre + "/resoudre", { method: "POST", body: JSON.stringify({ envoye }) });
      window.location.reload();
    } catch (_) { /* bannière affichée */ }
  }));

  // --- Suivi Brevo : ouvertures, rebonds, désinscriptions ---
  const boutonSuivi = document.getElementById("btn-suivi");
  const sortieSuivi = document.getElementById("resultat-suivi");
  async function synchroniser(silencieux) {
    boutonSuivi.disabled = true;
    sortieSuivi.className = "resultat";
    sortieSuivi.textContent = "Interrogation de Brevo…";
    try {
      const r = await api("/api/envois/synchroniser", { method: "POST", silencieux: true, body: "{}" });
      afficherResultat(sortieSuivi, "ok", r.message);
      if (r.ouvertures || r.rebonds || r.desinscriptions) window.setTimeout(() => window.location.reload(), 1500);
    } catch (e) {
      afficherResultat(sortieSuivi, "erreur", e.message);
    } finally {
      boutonSuivi.disabled = false;
    }
  }
  boutonSuivi.addEventListener("click", () => synchroniser(false));
  if (document.getElementById("panneau-suivi").dataset.auto === "1") synchroniser(true);  // à l'ouverture de l'écran

  // --- Réponses reçues (marquées à la main) ---
  document.querySelectorAll("[data-repondu]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Marquer ce prospect comme ayant répondu ? Il ne sera plus relancé.")) return;
    try {
      await api("/api/prospects/" + b.dataset.repondu + "/repondu", { method: "POST", body: "{}" });
      window.location.reload();
    } catch (_) { /* bannière affichée */ }
  }));

  // --- Relances : génération, puis validation dans l'écran Messages ---
  const zoneRelance = document.getElementById("panneau-relance-job");
  const sortieRelance = document.getElementById("resultat-relance");
  async function preparerRelances(ids) {
    document.querySelectorAll("[data-relancer], #btn-relancer-tout").forEach((b) => { b.disabled = true; });
    try {
      const r = await api("/api/relances/preparer", { method: "POST", body: JSON.stringify({ prospect_ids: ids }) });
      zoneRelance.hidden = false;
      const ecran = { barre: document.getElementById("relance-barre"), detail: document.getElementById("relance-detail"), libelle: "Relances" };
      suivreJob(r.job_id, ecran, (job) => {
        afficherResultat(sortieRelance, job.statut === "termine" ? "ok" : "erreur",
          job.statut === "termine" ? "relances prêtes : à relire dans l'écran Messages." : "voir le détail ci-dessus.");
      });
    } catch (_) {
      document.querySelectorAll("[data-relancer], #btn-relancer-tout").forEach((b) => { b.disabled = false; });
    }
  }
  document.querySelectorAll("[data-relancer]").forEach((b) => b.addEventListener("click", () => preparerRelances([Number(b.dataset.relancer)])));
  const toutes = document.getElementById("btn-relancer-tout");
  if (toutes) toutes.addEventListener("click", () => preparerRelances(null));

  // --- Filtres de l'historique ---
  const lignes = [...document.querySelectorAll("#tableau-envois tbody tr[data-campagne]")];
  const vide = document.getElementById("vide-envois");
  const filtreCampagne = document.getElementById("fe-campagne");
  const filtrePeriode = document.getElementById("fe-periode");
  function appliquer() {
    const jours = filtrePeriode.value === "" ? null : Number(filtrePeriode.value);
    const limite = jours === null ? null : new Date(Date.now() - jours * 86400000).toLocaleDateString("sv-SE");
    let visibles = 0;
    lignes.forEach((l) => {
      const ok = (!filtreCampagne.value || l.dataset.campagne === filtreCampagne.value)
        && (limite === null || l.dataset.jour >= limite);
      l.hidden = !ok;
      if (ok) visibles++;
    });
    if (lignes.length) {
      vide.hidden = visibles > 0;
      document.getElementById("texte-vide-envois").textContent = "Aucun envoi ne correspond à ces filtres.";
    }
  }
  filtreCampagne.addEventListener("change", appliquer);
  filtrePeriode.addEventListener("change", appliquer);
});
