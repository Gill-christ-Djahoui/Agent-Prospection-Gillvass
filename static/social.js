// Écran Messages privés : la file des commerces à contacter sur Facebook/Instagram, le message à envoyer, le résultat.
// Dépend de app.js (api, toast, afficherResultat).

const CHIP_SOCIAL = {
  envoye: "chip chip-envoye", reponse: "chip chip-repondu", pas_interesse: "chip chip-ignore", introuvable: "chip chip-ignore",
};
const AVERTISSEMENT_SOCIAL = {
  pas_interesse: "Ce prospect ne sera plus jamais contacté, par aucun canal. Il passe en « ignoré ».",
  introuvable: "Le prospect sort de la file. Si tu trouves son profil plus tard, il reste consultable dans « Clôturés ».",
};

function initialiserSocial() {
  const corps = document.getElementById("social-corps");
  const compteurs = document.getElementById("social-compteurs");
  const limite = document.getElementById("social-limite");
  const fenetre = document.getElementById("fiche-social");
  const champ = (id) => document.getElementById(id);
  let courant = null;
  let ids = [];
  let resultat = null;

  const filtres = () => new URLSearchParams({
    campagne: champ("f-campagne").value, etat: champ("f-etat").value,
    whatsapp: champ("f-whatsapp").value, profil: champ("f-profil").value }).toString();

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

  function lienExterne(lien, principal) {
    const a = document.createElement("a");
    a.href = lien.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = lien.libelle + " ↗";
    if (principal) a.className = "bouton";
    return a;
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
      td.colSpan = 6;
      td.className = "vide";
      td.textContent = "Rien ici pour l'instant. Les commerces sans email arrivent ici après un scan et l'analyse de la campagne.";
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
      const liens = document.createElement("div");
      l.liens.forEach((lien, k) => {
        if (k) liens.append(" · ");
        liens.appendChild(lienExterne(lien, false));
      });
      cellule(tr, liens);
      cellule(tr, l.problemes.length ? l.problemes.slice(0, 2).join(" ; ") : "—").className = "problemes";
      const dernier = document.createElement("div");
      if (l.dernier_libelle) {
        const puce = document.createElement("span");
        puce.className = CHIP_SOCIAL[l.dernier_resultat] || "chip";
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
    try { dessiner(await api("/api/social/file?" + filtres())); } catch (_) { /* bannière déjà affichée */ }
  }

  function choisirResultat(code) {
    resultat = code;
    champ("fs-resultats").querySelectorAll("[data-resultat]").forEach((b) => {
      b.classList.toggle("choisi", b.dataset.resultat === code);
      b.setAttribute("aria-pressed", b.dataset.resultat === code ? "true" : "false");
    });
    champ("fs-champ-note").hidden = !code;
    const avertissement = champ("fs-avertissement");
    avertissement.hidden = !AVERTISSEMENT_SOCIAL[code];
    avertissement.textContent = AVERTISSEMENT_SOCIAL[code] || "";
    champ("fs-valider").disabled = !code;
    erreursChamps(fenetre, {});
  }

  function dessinerLiens(liens) {
    const zone = champ("fs-liens");
    zone.replaceChildren();
    liens.forEach((lien, k) => {
      const a = lienExterne(lien, k === 0);
      if (lien.type === "whatsapp") {  // la conversation s'ouvre avec le message tel qu'il est écrit à cet instant
        a.addEventListener("click", () => {
          const texte = champ("fs-message").value.trim();
          a.href = lien.url + (texte ? "?text=" + encodeURIComponent(texte) : "");
        });
      }
      zone.appendChild(a);
      zone.append(" ");
    });
  }

  async function ouvrirFiche(id) {
    let f;
    try { f = await api("/api/social/" + id); } catch (_) { return; }
    courant = f;
    champ("fs-nom").textContent = f.nom;
    champ("fs-meta").textContent = [f.ville, f.campagne, f.site ? "Site : " + lienCourt(f.site) : null].filter(Boolean).join(" · ");
    dessinerLiens(f.liens);
    champ("fs-profil").value = f.profil_social_url || "";
    champ("fs-etat-profil").textContent = "";
    const problemes = champ("fs-problemes");
    problemes.replaceChildren();
    f.problemes.forEach((p) => {
      const li = document.createElement("li");
      li.className = "puce";
      li.textContent = p;
      problemes.appendChild(li);
    });
    champ("fs-message").value = f.message;
    champ("fs-etat-message").textContent = "";
    champ("fs-etat-resultat").textContent = "";
    champ("fs-note").value = "";
    champ("fs-generer").textContent = f.message ? "Réécrire avec l'IA" : "Écrire avec l'IA";
    champ("fs-generer").disabled = !fenetre.dataset.ia;
    const historique = champ("fs-historique");
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
      puce.className = CHIP_SOCIAL[h.resultat] || "chip";
      puce.textContent = h.libelle;
      const texte = document.createElement("span");
      texte.textContent = " " + h.fait_le.slice(0, 10).split("-").reverse().join("/") + (h.note ? " · " + h.note : "");
      li.append(puce, texte);
      historique.appendChild(li);
    });
    choisirResultat(null);
    if (!fenetre.open) fenetre.showModal();
  }

  champ("fs-resultats").querySelectorAll("[data-resultat]").forEach((b) =>
    b.addEventListener("click", () => choisirResultat(b.dataset.resultat)));
  champ("fs-fermer").addEventListener("click", () => { fenetre.close(); charger(); });

  champ("fs-ignorer").addEventListener("click", async () => {
    const id = courant.id;
    try {
      await api(`/api/social/${id}/ignorer`, { method: "POST", silencieux: true, body: "{}" });
    } catch (e) {
      afficherResultat(champ("fs-etat-resultat"), "erreur", e.message);
      return;
    }
    toast("Écarté : il ne sera plus proposé.", "info", 2500);
    const apres = ids.slice(ids.indexOf(id) + 1);
    let donnees;
    try { donnees = await api("/api/social/file?" + filtres()); } catch (_) { fenetre.close(); return; }
    dessiner(donnees);
    const suivant = apres.find((x) => ids.includes(x));
    if (suivant) ouvrirFiche(suivant); else fenetre.close();
  });

  champ("fs-enregistrer-profil").addEventListener("click", async () => {
    const sortie = champ("fs-etat-profil");
    erreursChamps(fenetre, {});
    try {
      const r = await api(`/api/social/${courant.id}/profil`, {
        method: "POST", silencieux: true, body: JSON.stringify({ profil: champ("fs-profil").value }) });
      champ("fs-profil").value = r.profil_social_url || "";
      courant = await api("/api/social/" + courant.id);
      dessinerLiens(courant.liens);
      afficherResultat(sortie, "ok", r.profil_social_url ? "adresse enregistrée." : "adresse effacée.");
    } catch (e) {
      if (e.corps && e.corps.erreurs) erreursChamps(fenetre, e.corps.erreurs);
      afficherResultat(sortie, "erreur", e.message);
    }
  });

  champ("fs-copier").addEventListener("click", async () => {
    const sortie = champ("fs-etat-message");
    const texte = champ("fs-message").value;
    if (!texte.trim()) { afficherResultat(sortie, "attention", "il n'y a pas encore de message."); return; }
    try {
      await navigator.clipboard.writeText(texte);
    } catch (_) {  // presse-papiers refusé par le navigateur : on sélectionne le texte, Ctrl+C fait le reste
      champ("fs-message").select();
      afficherResultat(sortie, "attention", "texte sélectionné : fais Ctrl+C.");
      return;
    }
    afficherResultat(sortie, "ok", "copié. Colle-le dans la conversation (sur WhatsApp, le bouton l'insère déjà pour toi).");
  });

  champ("fs-generer").addEventListener("click", async () => {
    const bouton = champ("fs-generer");
    const sortie = champ("fs-etat-message");
    bouton.disabled = true;
    sortie.className = "resultat";
    sortie.textContent = "L'IA écrit le message…";
    try {
      const r = await api(`/api/social/${courant.id}/message`, { method: "POST", silencieux: true, body: "{}" });
      champ("fs-message").value = r.message;
      bouton.textContent = "Réécrire avec l'IA";
      afficherResultat(sortie, "ok", "message enregistré. Relis-le et corrige-le si besoin.");
    } catch (e) {
      afficherResultat(sortie, "erreur", e.message);
    } finally {
      bouton.disabled = !fenetre.dataset.ia;
    }
  });

  champ("fs-enregistrer").addEventListener("click", async () => {
    const sortie = champ("fs-etat-message");
    try {
      await api(`/api/social/${courant.id}/message/enregistrer`, {
        method: "POST", silencieux: true, body: JSON.stringify({ message: champ("fs-message").value }) });
      afficherResultat(sortie, "ok", "enregistré.");
    } catch (e) {
      afficherResultat(sortie, "erreur", e.message);
    }
  });

  champ("fs-valider").addEventListener("click", async () => {
    const bouton = champ("fs-valider");
    const sortie = champ("fs-etat-resultat");
    erreursChamps(fenetre, {});
    bouton.disabled = true;
    sortie.className = "resultat";
    sortie.textContent = "Enregistrement…";
    const id = courant.id;
    try {
      await api(`/api/social/${id}/resultat`, { method: "POST", silencieux: true,
        body: JSON.stringify({ resultat, note: champ("fs-note").value }) });
    } catch (e) {
      if (e.corps && e.corps.erreurs) erreursChamps(fenetre, e.corps.erreurs);
      afficherResultat(sortie, "erreur", e.message);
      bouton.disabled = false;
      return;
    }
    toast("Enregistré.", "info", 2500);
    const apres = ids.slice(ids.indexOf(id) + 1);
    let donnees;
    try { donnees = await api("/api/social/file?" + filtres()); } catch (_) { fenetre.close(); return; }
    dessiner(donnees);
    const suivant = apres.find((x) => ids.includes(x));
    if (suivant) ouvrirFiche(suivant); else fenetre.close();
  });

  ["f-campagne", "f-etat", "f-whatsapp", "f-profil"].forEach((id) => champ(id).addEventListener("change", charger));
  charger();
}

document.addEventListener("DOMContentLoaded", () => {
  if (document.getElementById("social-tableau")) initialiserSocial();
});
