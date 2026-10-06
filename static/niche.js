// Assistant de niche (liste des campagnes) : tu décris la niche, l'IA propose, tu relis, la campagne est créée.
// Dépend de app.js (api, afficherResultat) et de campagnes.js (erreursDans, messageDeValidation).

function initialiserNiche() {
  const panneau = document.getElementById("panneau-niche");
  const formulaire = document.getElementById("form-niche");
  const proposition = document.getElementById("proposition-niche");
  const sortie = document.getElementById("resultat-niche");
  const sortieCreation = document.getElementById("resultat-creation");
  const valeur = (id) => document.getElementById(id).value;
  let etat = null;  // réponses de l'étape 1 (pays, langue, titre de salutation), reprises à la création

  document.getElementById("ouvrir-niche").addEventListener("click", () => {
    document.getElementById("panneau-nouvelle").hidden = true;
    panneau.hidden = false;
    document.getElementById("ni-niche").focus();
  });
  document.getElementById("ouvrir-nouvelle").addEventListener("click", () => { panneau.hidden = true; });
  document.getElementById("annuler-niche").addEventListener("click", () => { panneau.hidden = true; });

  function afficherProposition(p) {
    document.getElementById("pr-nom").value = p.nom;
    document.getElementById("pr-secteur").value = p.secteur;
    document.getElementById("pr-requetes").value = p.requetes.join("\n");
    document.getElementById("pr-objet").value = p.objet;
    document.getElementById("pr-angle").value = p.angle;
    document.getElementById("pr-zones").value = p.zones.map((z) => `${z.nom} | ${z.rayon_km}`).join("\n");
    const corps = document.querySelector("#pr-criteres tbody");
    corps.replaceChildren();
    p.criteres.forEach((k) => {
      const ligne = document.createElement("tr");
      ligne.dataset.code = k.code;
      const libelle = document.createElement("td");
      libelle.textContent = k.libelle;
      const global = document.createElement("td");
      global.className = "num";
      global.textContent = k.points_global;
      const cellule = document.createElement("td");
      cellule.className = "num";
      const champ = document.createElement("input");
      champ.type = "number";
      champ.min = "0";
      champ.max = "100";
      champ.value = k.points;
      champ.setAttribute("aria-label", "Poids pour cette niche : " + k.libelle);
      cellule.appendChild(champ);
      ligne.append(libelle, global, cellule);
      if (k.points !== k.points_global) ligne.querySelector("input").style.fontWeight = "700";
      corps.appendChild(ligne);
    });
    proposition.hidden = false;
    proposition.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  formulaire.addEventListener("submit", async (evenement) => {
    evenement.preventDefault();
    const bouton = formulaire.querySelector('button[type="submit"]');
    erreursDans(formulaire, {});
    bouton.disabled = true;
    sortie.className = "resultat";
    sortie.textContent = "L'IA prépare la proposition (quelques secondes)…";
    const champs = {
      niche: valeur("ni-niche"), ville: valeur("ni-ville"), pays: valeur("ni-pays"), langue: valeur("ni-langue"),
      offre: valeur("ni-offre"), probleme: valeur("ni-probleme"), titre: valeur("ni-titre"),
      agglomeration: document.getElementById("ni-agglo").checked,
    };
    try {
      const p = await api("/api/niche/proposer", { method: "POST", silencieux: true, body: JSON.stringify(champs) });
      etat = { pays: champs.pays.toUpperCase(), langue: champs.langue.toLowerCase(), titre: champs.titre };
      afficherProposition(p);
      sortie.textContent = "";
    } catch (e) {
      if (e.corps && e.corps.erreurs) erreursDans(formulaire, e.corps.erreurs);
      afficherResultat(sortie, "erreur", messageDeValidation(e));
    } finally {
      bouton.disabled = false;
    }
  });

  document.getElementById("creer-niche").addEventListener("click", async (evenement) => {
    const bouton = evenement.currentTarget;
    erreursDans(proposition, {});
    const zones = valeur("pr-zones").split("\n").map((l) => l.trim()).filter(Boolean).map((ligne) => {
      const [nom, rayon] = ligne.split("|").map((s) => s.trim());
      return { nom, rayon_km: rayon ? Number(rayon.replace(",", ".")) : 4 };
    });
    const criteres = [...document.querySelectorAll("#pr-criteres tr[data-code]")]
      .map((ligne) => ({ code: ligne.dataset.code, points: Number(ligne.querySelector("input").value) }));
    const donnees = {
      nom: valeur("pr-nom"), secteur: valeur("pr-secteur"), pays: etat.pays, langue: etat.langue, titre: etat.titre,
      requetes: valeur("pr-requetes").split("\n"), objet: valeur("pr-objet"), angle: valeur("pr-angle"), criteres, zones,
    };
    bouton.disabled = true;
    sortieCreation.className = "resultat";
    sortieCreation.textContent = "Création en cours : chaque zone est localisée (environ une seconde par zone)…";
    try {
      const r = await api("/api/niche/creer", { method: "POST", silencieux: true, body: JSON.stringify(donnees) });
      try { sessionStorage.setItem("niche-creee-" + r.campagne_id, JSON.stringify({ ecartees: r.ecartees })); } catch (_) { /* sans importance */ }
      window.location.href = r.url;
    } catch (e) {
      if (e.corps && e.corps.erreurs) erreursDans(proposition, e.corps.erreurs);
      afficherResultat(sortieCreation, "erreur", messageDeValidation(e, e.message));
      bouton.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  if (document.getElementById("form-niche")) initialiserNiche();
});
