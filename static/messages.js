// Écran Messages : file de validation, correction, approbation. Dépend de app.js (api, afficherResultat).
// Rien n'est approuvé sans un clic sur « Approuver », sur le texte exact affiché à l'écran.

document.addEventListener("DOMContentLoaded", () => {
  if (!document.getElementById("file-messages")) return;

  const PLAFOND_SCORE = Number(document.body.dataset.plafondScore);  // fourni par le serveur (une seule source)
  const $ = (id) => document.getElementById(id);
  const etat = { filtre: "a_valider", elements: [], courant: null, revue: null, versionId: null, modifie: false };

  const dateLisible = (iso) => (iso ? iso.replace("T", " ").replace("Z", " UTC").slice(0, 20) : "");
  const erreur = (message) => { $("m-erreur").textContent = message; $("m-erreur").hidden = !message; };

  // --- Liste -------------------------------------------------------------------------

  function majCompteurs(c) {
    $("cpt-attente").textContent = c.en_attente;
    $("cpt-approuves").textContent = c.approuves;
    $("pluriel-approuves").textContent = c.approuves > 1 ? "s" : "";
    $("pluriel-envoyes").textContent = c.approuves > 1 ? "s" : "";
    document.querySelector('[data-filtre="a_valider"]').textContent = "À valider (" + c.en_attente + ")";
    document.querySelector('[data-filtre="approuves"]').textContent = "Approuvés (" + c.approuves + ")";
  }

  function dessinerListe() {
    const liste = $("file-messages");
    liste.replaceChildren();
    const voulu = etat.filtre === "a_valider" ? "message_pret" : "approuve";
    const elements = etat.elements.filter((e) => e.statut === voulu);
    const vide = $("file-vide");
    vide.hidden = elements.length > 0;
    vide.textContent = etat.filtre === "a_valider"
      ? "Aucun message à valider. Sélectionne des prospects dans l'écran Prospects et lance la génération."
      : "Aucun message approuvé en attente d'envoi.";
    elements.forEach((e) => {
      const li = document.createElement("li");
      const bouton = document.createElement("button");
      bouton.type = "button";
      bouton.className = "element-file" + (e.id === etat.courant ? " actif" : "");
      const haut = document.createElement("span");
      haut.className = "ef-nom";
      haut.textContent = e.nom;
      const bas = document.createElement("span");
      bas.className = "ef-meta";
      const morceaux = [e.ville || "—", "score " + Math.min(e.score, PLAFOND_SCORE)];
      if (!e.a_email) morceaux.push("sans email");
      else if (e.generique) morceaux.push("adresse générique");
      if (e.relance_numero > 0) morceaux.unshift("relance n°" + e.relance_numero);
      else if (e.nb_versions > 1) morceaux.push(e.nb_versions + " versions");
      bas.textContent = morceaux.join(" · ");
      bouton.append(haut, bas);
      bouton.addEventListener("click", () => ouvrir(e.id));
      li.appendChild(bouton);
      liste.appendChild(li);
    });
  }

  async function chargerFile() {
    const r = await api("/api/messages/file");
    etat.elements = r.elements;
    majCompteurs(r.compteurs);
    dessinerListe();
  }

  document.querySelectorAll(".onglet").forEach((onglet) => onglet.addEventListener("click", () => {
    etat.filtre = onglet.dataset.filtre;
    document.querySelectorAll(".onglet").forEach((o) => {
      o.classList.toggle("actif", o === onglet);
      o.setAttribute("aria-selected", o === onglet ? "true" : "false");
    });
    dessinerListe();
  }));

  // --- Message -----------------------------------------------------------------------

  function videMessage(texte) {
    etat.courant = null;
    $("message-contenu").hidden = true;
    $("message-vide").hidden = false;
    if (texte) $("message-vide").textContent = texte;
  }

  async function ouvrir(prospectId, versionId) {
    erreur("");
    $("m-resultat").textContent = "";
    let revue;
    try {
      revue = await api("/api/prospects/" + prospectId + "/revue");
    } catch (_) { return; }
    etat.courant = prospectId;
    etat.revue = revue;
    window.location.hash = String(prospectId);
    dessinerListe();
    $("message-vide").hidden = true;
    $("message-contenu").hidden = false;

    const p = revue.prospect;
    $("m-nom").textContent = p.nom;
    const meta = [revue.relance_numero > 0 ? "Relance n°" + revue.relance_numero : null, p.ville, p.adresse, p.telephone ? "Tél. " + p.telephone : null, "score " + Math.min(p.score, PLAFOND_SCORE)];
    $("m-meta").textContent = meta.filter(Boolean).join(" · ");
    if (p.site) {
      const lien = document.createElement("a");
      lien.href = p.site; lien.target = "_blank"; lien.rel = "noopener noreferrer";
      lien.textContent = lienCourt(p.site); lien.title = p.site;
      $("m-meta").append(" · ", lien);
    }

    const puces = $("m-problemes");
    puces.replaceChildren();
    revue.problemes.forEach((pb) => {
      const li = document.createElement("li");
      li.className = "puce" + (pb.points > 0 ? "" : " puce-neutre");
      li.textContent = pb.libelle + (pb.points > 0 ? " (+" + pb.points + ")" : "");
      puces.appendChild(li);
    });
    if (revue.problemes.length === 0) {
      const li = document.createElement("li");
      li.className = "vide";
      li.textContent = "Aucun problème détecté.";
      puces.appendChild(li);
    }

    dessinerEmail(p);
    $("m-mention").textContent = (revue.site_signature ? revue.site_signature + " — " : "")
      + (revue.mention_desinscription || "(aucune mention définie dans les Réglages)");

    const select = $("m-versions");
    select.replaceChildren();
    revue.versions.forEach((v) => {
      const option = document.createElement("option");
      option.value = v.id;
      option.textContent = "Version " + v.version + (v.approuve_le ? " — approuvée" : "") + (v.envoye ? " — envoyée" : "");
      select.appendChild(option);
    });
    $("m-versions-zone").hidden = revue.versions.length < 2;
    const choisie = (versionId && revue.versions.find((v) => v.id === versionId)) || revue.versions[revue.versions.length - 1];
    select.value = choisie.id;
    afficherVersion(choisie.id);
  }

  function dessinerEmail(p) {
    const zone = $("m-email-zone");
    zone.replaceChildren();
    const ligne = document.createElement("div");
    ligne.className = "ligne-email";
    if (p.email) {
      const texte = document.createElement("span");
      texte.textContent = "Destinataire : " + p.email;
      ligne.appendChild(texte);
      if (p.generique) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "adresse générique";
        badge.title = "Adresse chez un fournisseur grand public (gmail, orange…) : à toi de décider si c'est une adresse professionnelle.";
        ligne.appendChild(badge);
      }
    } else {
      const avert = document.createElement("span");
      avert.className = "resultat statut-attention";
      avert.textContent = "Pas d'email pour ce prospect : le message ne peut pas être approuvé.";
      ligne.appendChild(avert);
    }
    const champ = document.createElement("input");
    champ.type = "text";
    champ.id = "m-email";
    champ.placeholder = p.email ? "Changer l'adresse…" : "Saisir l'email trouvé à la main";
    champ.autocomplete = "off";
    const bouton = document.createElement("button");
    bouton.type = "button";
    bouton.className = "secondaire";
    bouton.textContent = "Enregistrer l'email";
    bouton.addEventListener("click", async () => {
      erreur("");
      try {
        await api("/api/prospects/" + p.id + "/email", { method: "POST", silencieux: true, body: JSON.stringify({ email: champ.value }) });
        await chargerFile();
        await ouvrir(p.id, etat.versionId);
      } catch (e) { erreur(e.message); }
    });
    zone.append(ligne, champ, bouton);
  }

  function mots(texte) { return texte.trim() ? texte.trim().split(/\s+/).length : 0; }

  function majMots() {
    const max = etat.revue ? etat.revue.mots_max : 0;
    const n = mots($("m-corps").value);
    $("m-mots").textContent = n + " mots" + (max ? " (maximum conseillé : " + max + ")" : "");
    $("m-mots").className = "code" + (max && n > max ? " depasse" : "");
  }

  function majBoutons() {
    const v = etat.revue.versions.find((x) => x.id === etat.versionId);
    const peut = !v.refus && (!v.approuve_le || etat.modifie);
    $("btn-approuver").disabled = !peut;
    $("btn-enregistrer").disabled = v.envoye || !etat.modifie;
    $("btn-desapprouver").hidden = !(v.approuve_le && !v.envoye);
    $("btn-regenerer").disabled = false;
    $("btn-ignorer").disabled = false;
    const avertissementEssai = etat.revue.hors_essai
      ? " ⚠ Mode essai actif : ce destinataire n'est pas dans la liste autorisée, ce message ne partira pas." : "";
    $("m-aide-approbation").textContent = (v.refus
      ? v.refus
      : (v.approuve_le && !etat.modifie ? "Ce message est approuvé et attend l'envoi. Le modifier annulera son approbation." : "")) + avertissementEssai;
  }

  // Aperçu : le serveur utilise les mêmes fonctions que l'envoi ; l'iframe est isolée (sandbox), sans script.
  let minuterieApercu = null;
  async function majApercu() {
    if (!$("m-apercu-zone").open || !etat.revue) return;
    try {
      const r = await api("/api/messages/apercu", { method: "POST", silencieux: true,
        body: JSON.stringify({ corps: $("m-corps").value, relance: etat.revue.relance_numero > 0 }) });
      if (r.format === "html") {
        $("m-apercu").srcdoc = r.html;
        $("m-apercu-info").textContent = "Mise en forme HTML, avec la version texte jointe. Réglable dans Réglages > Identité d'expédition.";
      } else {
        const pre = document.createElement("pre");
        pre.textContent = r.texte;
        $("m-apercu").srcdoc = "<body style=\"margin:0;padding:18px;font:14px/1.6 sans-serif;white-space:pre-wrap\">"
          + pre.innerHTML + "</body>";
        $("m-apercu-info").textContent = "Texte brut (le format HTML est désactivé dans les Réglages).";
      }
    } catch (_) { /* l'aperçu est un confort : l'erreur éventuelle est déjà affichée par api() */ }
  }
  $("m-apercu-zone").addEventListener("toggle", majApercu);

  function afficherVersion(versionId) {
    const v = etat.revue.versions.find((x) => x.id === Number(versionId));
    etat.versionId = v.id;
    etat.modifie = false;
    $("m-objet").value = v.objet;
    $("m-corps").value = v.corps;
    $("m-objet").readOnly = $("m-corps").readOnly = v.envoye;
    const infos = ["Version " + v.version + ", générée le " + dateLisible(v.genere_le)];
    if (v.edite_par_humain) infos.push("corrigée à la main");
    if (v.approuve_le) infos.push("approuvée le " + dateLisible(v.approuve_le));
    if (v.envoye) infos.push("déjà envoyée");
    $("m-version-info").textContent = infos.join(" · ");
    majMots();
    majBoutons();
    majApercu();
  }

  $("m-versions").addEventListener("change", (e) => afficherVersion(e.target.value));
  ["m-objet", "m-corps"].forEach((id) => $(id).addEventListener("input", () => {
    etat.modifie = true;
    majMots();
    majBoutons();
    window.clearTimeout(minuterieApercu);
    minuterieApercu = window.setTimeout(majApercu, 350);
  }));

  // --- Actions -----------------------------------------------------------------------

  const boutons = () => ["btn-approuver", "btn-enregistrer", "btn-regenerer", "btn-ignorer", "btn-desapprouver"].map($);

  async function action(fonction) {
    erreur("");
    $("m-resultat").textContent = "";
    boutons().forEach((b) => { b.disabled = true; });
    try {
      await fonction();
    } catch (e) {
      erreur(e.message);
      if (etat.revue) majBoutons();
    }
  }

  function texte() { return { objet: $("m-objet").value, corps: $("m-corps").value }; }

  function suivant(apres) {
    const voulu = etat.filtre === "a_valider" ? "message_pret" : "approuve";
    const restants = etat.elements.filter((e) => e.statut === voulu);
    const index = restants.findIndex((e) => e.id === apres);
    return restants[index + 1] || restants[index - 1] || null;
  }

  $("btn-approuver").addEventListener("click", () => action(async () => {
    const prospectId = etat.courant;
    await api("/api/messages/" + etat.versionId + "/approuver", { method: "POST", silencieux: true, body: JSON.stringify(texte()) });
    const prochain = suivant(prospectId);
    await chargerFile();
    if (prochain && etat.filtre === "a_valider") await ouvrir(prochain.id);
    else videMessage("Approuvé. Choisis un autre prospect dans la liste.");
  }));

  $("btn-enregistrer").addEventListener("click", () => action(async () => {
    await api("/api/messages/" + etat.versionId + "/enregistrer", { method: "POST", silencieux: true, body: JSON.stringify(texte()) });
    await chargerFile();
    await ouvrir(etat.courant, etat.versionId);
    afficherResultat($("m-resultat"), "ok", "corrections enregistrées.");
  }));

  $("btn-desapprouver").addEventListener("click", () => action(async () => {
    await api("/api/messages/" + etat.versionId + "/desapprouver", { method: "POST", silencieux: true, body: "{}" });
    await chargerFile();
    await ouvrir(etat.courant, etat.versionId);
    afficherResultat($("m-resultat"), "ok", "approbation retirée.");
  }));

  $("btn-ignorer").addEventListener("click", () => action(async () => {
    if (!confirm("Ignorer ce prospect ? Il sort de la file (tu pourras le remettre en qualifié depuis l'écran Prospects).")) {
      majBoutons();
      return;
    }
    const prospectId = etat.courant;
    const prochain = suivant(prospectId);
    await api("/api/prospects/statut", { method: "POST", silencieux: true, body: JSON.stringify({ prospect_ids: [prospectId], statut: "ignore" }) });
    await chargerFile();
    if (prochain) await ouvrir(prochain.id);
    else videMessage("Prospect ignoré.");
  }));

  $("btn-regenerer").addEventListener("click", () => action(async () => {
    if (!confirm("Générer une nouvelle version du message ? L'approbation en cours sera annulée ; les versions précédentes restent consultables.")) {
      majBoutons();
      return;
    }
    const prospectId = etat.courant;
    if (etat.modifie) {  // tes corrections ne sont jamais perdues : elles restent dans la version actuelle
      await api("/api/messages/" + etat.versionId + "/enregistrer", { method: "POST", silencieux: true, body: JSON.stringify(texte()) });
    }
    const lancement = await api("/api/messages/generer", { method: "POST", silencieux: true,
      body: JSON.stringify({ prospect_ids: [prospectId], forcer: true }) });
    $("m-resultat").className = "resultat";
    $("m-resultat").textContent = "Génération en cours…";
    for (;;) {
      const job = await api("/api/jobs/" + lancement.job_id, { silencieux: true });
      if (job.statut === "termine" || job.statut === "erreur") {
        await chargerFile();
        await ouvrir(prospectId);
        if (job.statut === "erreur") erreur(job.detail);
        else afficherResultat($("m-resultat"), "ok", "nouvelle version générée. " + job.detail);
        return;
      }
      await new Promise((r) => window.setTimeout(r, 1200));
    }
  }));

  // --- Raccourcis : Ctrl+Entrée approuve ; Alt+↓ / Alt+↑ prospect suivant / précédent --------
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.key === "Enter" && !$("btn-approuver").disabled && !$("message-contenu").hidden) {
      e.preventDefault();
      $("btn-approuver").click();
    } else if (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      const voulu = etat.filtre === "a_valider" ? "message_pret" : "approuve";
      const liste = etat.elements.filter((x) => x.statut === voulu);
      const index = liste.findIndex((x) => x.id === etat.courant);
      const cible = liste[Math.min(liste.length - 1, Math.max(0, index + (e.key === "ArrowDown" ? 1 : -1)))];
      if (cible) ouvrir(cible.id);
    }
  });

  // --- Démarrage ---------------------------------------------------------------------

  chargerFile().then(() => {
    const depuisAdresse = Number(window.location.hash.slice(1));
    const cible = etat.elements.find((e) => e.id === depuisAdresse);
    if (cible) {
      if (cible.statut === "approuve") document.querySelector('[data-filtre="approuves"]').click();
      ouvrir(cible.id);
    }
  }).catch(() => { /* api() a déjà affiché l'erreur */ });
});
