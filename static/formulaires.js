// Écran Formulaires : la file des prospects joignables par formulaire, le message à coller, le résultat.
// Dépend de app.js (api, toast, afficherResultat).

const CHIP_FORMULAIRE = {
  envoye: "chip chip-envoye", reponse: "chip chip-repondu", pas_interesse: "chip chip-ignore", inutilisable: "chip chip-ignore",
};
const AVERTISSEMENT_FORMULAIRE = {
  pas_interesse: "Ce prospect ne sera plus jamais contacté, par aucun canal. Il passe en « ignoré ».",
  inutilisable: "Le prospect sort de la file. Tu peux toujours lui écrire autrement (email, s'il en a un).",
};

function initialiserFormulaires() {
  const corps = document.getElementById("formulaires-corps");
  const compteurs = document.getElementById("formulaires-compteurs");
  const limite = document.getElementById("formulaires-limite");
  const fenetre = document.getElementById("fiche-formulaire");
  const champ = (id) => document.getElementById(id);
  let courant = null;
  let ids = [];
  let resultat = null;

  const filtres = () => new URLSearchParams({ campagne: champ("f-campagne").value, etat: champ("f-etat").value }).toString();

  function erreursChamps(portee, erreurs) {
    portee.querySelectorAll("[data-erreur-pour]").forEach((p) => { p.hidden = true; p.textContent = ""; });
    Object.entries(erreurs || {}).forEach(([cle, message]) => {
      const p = portee.querySelector(`[data-erreur-pour="${cle}"]`);
      if (p) { p.textContent = message; p.hidden = false; }
    });
  }

  function cellule(ligne, contenu) {
    const td = document.createElement("td");
    if (contenu instanceof Node) td.appendChild(contenu); else td.textContent = contenu;
    ligne.appendChild(td);
    return td;
  }

  function dessiner(donnees) {
    const c = donnees.compteurs;
    compteurs.textContent = `${c.faire} à faire · ${c.envoyes} envoyé${c.envoyes > 1 ? "s" : ""} · ${c.reponses} réponse${c.reponses > 1 ? "s" : ""} · ${c.clos} clôturé${c.clos > 1 ? "s" : ""}`;
    ids = donnees.lignes.map((l) => l.id);
    corps.replaceChildren();
    if (!donnees.lignes.length) {
      const tr = document.createElement("tr");
      tr.className = "ligne-vide";
      const td = document.createElement("td");
      td.colSpan = 5;
      td.className = "vide";
      td.textContent = "Rien ici pour l'instant. Les formulaires sont repérés pendant l'exploration des sites : lance un scan, ou « Chercher les emails et formulaires manquants » sur la fiche d'une campagne.";
      tr.appendChild(td);
      corps.appendChild(tr);
    }
    donnees.lignes.forEach((l) => {
      const tr = document.createElement("tr");
      cellule(tr, String(l.score_affiche)).className = "num score";
      const identite = document.createElement("div");
      const nom = document.createElement("strong");
      nom.textContent = l.nom;
      const meta = document.createElement("div");
      meta.className = "aide";
      meta.textContent = [l.ville, l.campagne_nom].filter(Boolean).join(" · ");
      identite.append(nom, meta);
      cellule(tr, identite);
      cellule(tr, l.problemes.length ? l.problemes.slice(0, 2).join(" ; ") : "—").className = "problemes";
      const dernier = document.createElement("div");
      if (l.dernier_libelle) {
        const puce = document.createElement("span");
        puce.className = CHIP_FORMULAIRE[l.dernier_resultat] || "chip";
        puce.textContent = l.dernier_libelle;
        dernier.appendChild(puce);
      } else {
        dernier.textContent = l.a_message ? "Message prêt" : "—";
      }
      cellule(tr, dernier);
      const ouvrir = document.createElement("button");
      ouvrir.type = "button";
      ouvrir.className = "bouton";
      ouvrir.textContent = l.dernier_resultat ? "Ouvrir" : "Préparer";
      ouvrir.addEventListener("click", () => ouvrirFiche(l.id));
      cellule(tr, ouvrir);
      corps.appendChild(tr);
    });
    limite.hidden = donnees.total <= donnees.lignes.length;
    limite.textContent = `Les ${donnees.lignes.length} premiers sur ${donnees.total} sont affichés : affine avec une campagne.`;
  }

  async function charger() {
    try { dessiner(await api("/api/formulaires/file?" + filtres())); } catch (_) { /* bannière déjà affichée */ }
  }

  function choisirResultat(code) {
    resultat = code;
    champ("ff-resultats").querySelectorAll("[data-resultat]").forEach((b) => {
      b.classList.toggle("choisi", b.dataset.resultat === code);
      b.setAttribute("aria-pressed", b.dataset.resultat === code ? "true" : "false");
    });
    champ("ff-champ-note").hidden = !code;
    const avertissement = champ("ff-avertissement");
    avertissement.hidden = !AVERTISSEMENT_FORMULAIRE[code];
    avertissement.textContent = AVERTISSEMENT_FORMULAIRE[code] || "";
    champ("ff-valider").disabled = !code;
    erreursChamps(fenetre, {});
  }

  async function ouvrirFiche(id) {
    let f;
    try { f = await api("/api/formulaires/" + id); } catch (_) { return; }
    courant = f;
    champ("ff-nom").textContent = f.nom;
    champ("ff-meta").textContent = [f.ville, f.campagne, f.site ? "Site : " + lienCourt(f.site) : null].filter(Boolean).join(" · ");
    champ("ff-ouvrir").href = f.formulaire_url;
    const problemes = champ("ff-problemes");
    problemes.replaceChildren();
    f.problemes.forEach((p) => {
      const li = document.createElement("li");
      li.className = "puce";
      li.textContent = p;
      problemes.appendChild(li);
    });
    champ("ff-message").value = f.message;
    champ("ff-etat-message").textContent = "";
    champ("ff-etat-resultat").textContent = "";
    champ("ff-note").value = "";
    champ("ff-generer").textContent = f.message ? "Réécrire avec l'IA" : "Écrire avec l'IA";
    champ("ff-generer").disabled = !fenetre.dataset.ia;
    const historique = champ("ff-historique");
    historique.replaceChildren();
    if (!f.historique.length) {
      const li = document.createElement("li");
      li.className = "aide";
      li.textContent = "Rien pour l'instant.";
      historique.appendChild(li);
    }
    f.historique.forEach((h) => {
      const li = document.createElement("li");
      const puce = document.createElement("span");
      puce.className = CHIP_FORMULAIRE[h.resultat] || "chip";
      puce.textContent = h.libelle;
      const texte = document.createElement("span");
      texte.textContent = " " + h.fait_le.slice(0, 10).split("-").reverse().join("/") + (h.note ? " · " + h.note : "");
      li.append(puce, texte);
      historique.appendChild(li);
    });
    choisirResultat(null);
    if (!fenetre.open) fenetre.showModal();
  }

  champ("ff-resultats").querySelectorAll("[data-resultat]").forEach((b) =>
    b.addEventListener("click", () => choisirResultat(b.dataset.resultat)));
  champ("ff-fermer").addEventListener("click", () => fenetre.close());

  champ("ff-copier").addEventListener("click", async () => {
    const sortie = champ("ff-etat-message");
    const texte = champ("ff-message").value;
    if (!texte.trim()) { afficherResultat(sortie, "attention", "il n'y a pas encore de message."); return; }
    try {
      await navigator.clipboard.writeText(texte);
    } catch (_) {  // presse-papiers refusé par le navigateur : on sélectionne le texte, Ctrl+C fait le reste
      champ("ff-message").select();
      afficherResultat(sortie, "attention", "texte sélectionné : fais Ctrl+C.");
      return;
    }
    afficherResultat(sortie, "ok", "copié. Colle-le dans le formulaire.");
  });

  champ("ff-generer").addEventListener("click", async () => {
    const bouton = champ("ff-generer");
    const sortie = champ("ff-etat-message");
    bouton.disabled = true;
    sortie.className = "resultat";
    sortie.textContent = "L'IA écrit le message…";
    try {
      const r = await api(`/api/formulaires/${courant.id}/message`, { method: "POST", silencieux: true, body: "{}" });
      champ("ff-message").value = r.message;
      bouton.textContent = "Réécrire avec l'IA";
      afficherResultat(sortie, "ok", "message enregistré. Relis-le et corrige-le si besoin.");
    } catch (e) {
      afficherResultat(sortie, "erreur", e.message);
    } finally {
      bouton.disabled = !fenetre.dataset.ia;
    }
  });

  champ("ff-enregistrer").addEventListener("click", async () => {
    const sortie = champ("ff-etat-message");
    try {
      await api(`/api/formulaires/${courant.id}/message/enregistrer`, {
        method: "POST", silencieux: true, body: JSON.stringify({ message: champ("ff-message").value }) });
      afficherResultat(sortie, "ok", "enregistré.");
    } catch (e) {
      afficherResultat(sortie, "erreur", e.message);
    }
  });

  champ("ff-valider").addEventListener("click", async () => {
    const bouton = champ("ff-valider");
    const sortie = champ("ff-etat-resultat");
    erreursChamps(fenetre, {});
    bouton.disabled = true;
    sortie.className = "resultat";
    sortie.textContent = "Enregistrement…";
    const id = courant.id;
    try {
      await api(`/api/formulaires/${id}/resultat`, { method: "POST", silencieux: true,
        body: JSON.stringify({ resultat, note: champ("ff-note").value }) });
    } catch (e) {
      if (e.corps && e.corps.erreurs) erreursChamps(fenetre, e.corps.erreurs);
      afficherResultat(sortie, "erreur", e.message);
      bouton.disabled = false;
      return;
    }
    toast("Enregistré.", "info", 2500);
    const apres = ids.slice(ids.indexOf(id) + 1);
    let donnees;
    try { donnees = await api("/api/formulaires/file?" + filtres()); } catch (_) { fenetre.close(); return; }
    dessiner(donnees);
    const suivant = apres.find((x) => ids.includes(x));
    if (suivant) ouvrirFiche(suivant); else fenetre.close();
  });

  ["f-campagne", "f-etat"].forEach((id) => champ(id).addEventListener("change", charger));
  charger();
}

document.addEventListener("DOMContentLoaded", () => {
  if (document.getElementById("formulaires-tableau")) initialiserFormulaires();
});
