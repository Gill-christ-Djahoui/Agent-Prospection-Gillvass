// Écran Appels : la file des prospects à appeler, le script, le résultat de l'appel. Dépend de app.js (api, toast, afficherResultat).

const CHIP_RESULTAT = {
  pas_de_reponse: "chip", a_rappeler: "chip chip-message_pret", interesse: "chip chip-envoye", rdv_obtenu: "chip chip-envoye",
  pas_interesse: "chip chip-ignore", mauvais_numero: "chip chip-ignore",
};
const AVERTISSEMENTS = {
  pas_interesse: "Ce prospect ne sera plus jamais contacté : ni appel, ni email. Son adresse email éventuelle entre en liste noire.",
  interesse: "Si un message pour ce prospect attend dans Messages, ne l'approuve pas : il a déjà répondu au téléphone.",
  rdv_obtenu: "Si un message pour ce prospect attend dans Messages, ne l'approuve pas : il a déjà répondu au téléphone.",
  mauvais_numero: "Le prospect sort de la file. Corrige son numéro dans sa fiche si tu en trouves un autre.",
};

function initialiserAppels() {
  const corps = document.getElementById("appels-corps");
  const compteurs = document.getElementById("appels-compteurs");
  const limite = document.getElementById("appels-limite");
  const fenetre = document.getElementById("fiche-appel");
  const champ = (id) => document.getElementById(id);
  let ligneCourante = null;   // prospect ouvert
  let ids = [];               // ordre actuel de la file (pour passer au suivant)
  let resultat = null;        // résultat choisi dans la fiche

  const filtres = () => new URLSearchParams({
    campagne: champ("f-campagne").value, contact: champ("f-contact").value, etat: champ("f-etat").value }).toString();

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
    compteurs.textContent = `${c.faire} à faire${c.rappels_dus ? ` (dont ${c.rappels_dus} rappel${c.rappels_dus > 1 ? "s" : ""} dû${c.rappels_dus > 1 ? "s" : ""})` : ""}`
      + ` · ${c.plus_tard} plus tard · ${c.interesses} intéressé${c.interesses > 1 ? "s" : ""} · ${c.clos} clôturé${c.clos > 1 ? "s" : ""}`;
    ids = donnees.lignes.map((l) => l.id);
    corps.replaceChildren();
    if (!donnees.lignes.length) {
      const tr = document.createElement("tr");
      tr.className = "ligne-vide";
      const td = document.createElement("td");
      td.colSpan = 6;
      td.className = "vide";
      td.textContent = "Rien à appeler avec ces filtres. Seuls les prospects qui ont un numéro de téléphone apparaissent ici.";
      tr.appendChild(td);
      corps.appendChild(tr);
    }
    donnees.lignes.forEach((l) => {
      const tr = document.createElement("tr");
      tr.className = l.rappel_du ? "rappel-du" : "";
      cellule(tr, String(l.score_affiche)).className = "num score";
      const identite = document.createElement("div");
      const nom = document.createElement("strong");
      nom.textContent = l.nom;
      const meta = document.createElement("div");
      meta.className = "aide";
      meta.textContent = [l.ville, l.campagne_nom].filter(Boolean).join(" · ");
      identite.append(nom, meta);
      cellule(tr, identite);
      if (l.tel) {
        const a = document.createElement("a");
        a.href = "tel:" + l.tel;
        a.textContent = l.telephone;
        cellule(tr, a);
      } else {
        cellule(tr, l.telephone || "—");
      }
      cellule(tr, l.problemes.length ? l.problemes.slice(0, 2).join(" ; ") : "—").className = "problemes";
      const dernier = document.createElement("div");
      if (l.dernier_libelle) {
        const puce = document.createElement("span");
        puce.className = CHIP_RESULTAT[l.dernier_resultat] || "chip";
        puce.textContent = l.dernier_libelle;
        dernier.appendChild(puce);
        const detail = document.createElement("div");
        detail.className = "aide";
        detail.textContent = (l.rappel_du ? "À rappeler aujourd'hui · " : l.rappel_le ? "Rappel le " + l.rappel_le + " · " : "")
          + l.nb_appels + " appel" + (l.nb_appels > 1 ? "s" : "");
        dernier.appendChild(detail);
      } else {
        dernier.textContent = "—";
      }
      cellule(tr, dernier);
      const ouvrir = document.createElement("button");
      ouvrir.type = "button";
      ouvrir.className = "bouton";
      ouvrir.textContent = l.dernier_resultat ? "Ouvrir" : "Appeler";
      ouvrir.addEventListener("click", () => ouvrirFiche(l.id));
      cellule(tr, ouvrir);
      corps.appendChild(tr);
    });
    limite.hidden = donnees.total <= donnees.lignes.length;
    limite.textContent = `Les ${donnees.lignes.length} premiers sur ${donnees.total} sont affichés : affine avec une campagne.`;
  }

  async function charger() {
    try {
      dessiner(await api("/api/appels/file?" + filtres()));
    } catch (_) { /* bannière déjà affichée */ }
  }

  function choisirResultat(code) {
    resultat = code;
    champ("fa-resultats").querySelectorAll("[data-resultat]").forEach((b) => {
      b.classList.toggle("choisi", b.dataset.resultat === code);
      b.setAttribute("aria-pressed", b.dataset.resultat === code ? "true" : "false");
    });
    champ("fa-champs-resultat").hidden = !code;
    champ("fa-bloc-rappel").hidden = code !== "a_rappeler";
    if (code === "a_rappeler" && !champ("fa-rappel").value) champ("fa-rappel").value = fenetre.dataset.demain;
    const avertissement = champ("fa-avertissement");
    avertissement.hidden = !AVERTISSEMENTS[code];
    avertissement.textContent = AVERTISSEMENTS[code] || "";
    champ("fa-valider").disabled = !code;
    erreursChamps(fenetre, {});
  }

  async function ouvrirFiche(id) {
    let f;
    try {
      f = await api("/api/appels/" + id);
    } catch (_) { return; }
    ligneCourante = f;
    champ("fa-nom").textContent = f.nom;
    champ("fa-meta").textContent = [f.ville, f.adresse, f.campagne, f.site ? "Site : " + lienCourt(f.site) : "Aucun site propre connu"].filter(Boolean).join(" · ");
    const tel = champ("fa-tel");
    tel.textContent = f.telephone || "";
    if (f.tel) tel.href = "tel:" + f.tel; else tel.removeAttribute("href");
    const problemes = champ("fa-problemes");
    problemes.replaceChildren();
    f.problemes.forEach((p) => {
      const li = document.createElement("li");
      li.className = "puce";
      li.textContent = p;
      problemes.appendChild(li);
    });
    champ("fa-script").value = f.script;
    champ("fa-etat-script").textContent = "";
    champ("fa-etat-appel").textContent = "";
    champ("fa-note").value = "";
    champ("fa-rappel").value = "";
    champ("fa-generer").textContent = f.script ? "Réécrire avec l'IA" : "Écrire avec l'IA";
    champ("fa-generer").disabled = !fenetre.dataset.ia;
    const historique = champ("fa-historique");
    historique.replaceChildren();
    if (!f.historique.length) {
      const li = document.createElement("li");
      li.className = "aide";
      li.textContent = "Aucun appel pour l'instant.";
      historique.appendChild(li);
    }
    f.historique.forEach((h) => {
      const li = document.createElement("li");
      const puce = document.createElement("span");
      puce.className = CHIP_RESULTAT[h.resultat] || "chip";
      puce.textContent = h.libelle;
      const texte = document.createElement("span");
      texte.textContent = " " + h.appele_le.slice(0, 10).split("-").reverse().join("/")
        + (h.rappel_le ? " · rappel le " + h.rappel_le : "") + (h.note ? " · " + h.note : "");
      li.append(puce, texte);
      historique.appendChild(li);
    });
    choisirResultat(null);
    if (!fenetre.open) fenetre.showModal();
  }

  champ("fa-resultats").querySelectorAll("[data-resultat]").forEach((b) =>
    b.addEventListener("click", () => choisirResultat(b.dataset.resultat)));
  champ("fa-fermer").addEventListener("click", () => fenetre.close());

  champ("fa-generer").addEventListener("click", async () => {
    const bouton = champ("fa-generer");
    const sortie = champ("fa-etat-script");
    bouton.disabled = true;
    sortie.className = "resultat";
    sortie.textContent = "L'IA écrit le script…";
    try {
      const r = await api(`/api/appels/${ligneCourante.id}/script`, { method: "POST", silencieux: true, body: "{}" });
      champ("fa-script").value = r.script;
      bouton.textContent = "Réécrire avec l'IA";
      afficherResultat(sortie, "ok", "script enregistré. Relis-le et corrige-le si besoin.");
    } catch (e) {
      afficherResultat(sortie, "erreur", e.message);
    } finally {
      bouton.disabled = !fenetre.dataset.ia;
    }
  });

  champ("fa-enregistrer-script").addEventListener("click", async () => {
    const sortie = champ("fa-etat-script");
    try {
      await api(`/api/appels/${ligneCourante.id}/script/enregistrer`, {
        method: "POST", silencieux: true, body: JSON.stringify({ script: champ("fa-script").value }) });
      afficherResultat(sortie, "ok", "enregistré.");
    } catch (e) {
      afficherResultat(sortie, "erreur", e.message);
    }
  });

  champ("fa-valider").addEventListener("click", async () => {
    const bouton = champ("fa-valider");
    const sortie = champ("fa-etat-appel");
    erreursChamps(fenetre, {});
    bouton.disabled = true;
    sortie.className = "resultat";
    sortie.textContent = "Enregistrement…";
    const courant = ligneCourante.id;
    try {
      await api(`/api/appels/${courant}/resultat`, { method: "POST", silencieux: true, body: JSON.stringify({
        resultat, note: champ("fa-note").value, rappel_le: resultat === "a_rappeler" ? champ("fa-rappel").value : "" }) });
    } catch (e) {
      if (e.corps && e.corps.erreurs) erreursChamps(fenetre, e.corps.erreurs);
      afficherResultat(sortie, "erreur", e.message);
      bouton.disabled = false;
      return;
    }
    toast("Appel enregistré.", "info", 2500);
    const apres = ids.slice(ids.indexOf(courant) + 1);
    let donnees;
    try { donnees = await api("/api/appels/file?" + filtres()); } catch (_) { fenetre.close(); return; }
    dessiner(donnees);
    const suivant = apres.find((id) => ids.includes(id));  // le prochain de la liste qui est encore à faire
    if (suivant) ouvrirFiche(suivant); else fenetre.close();
  });

  ["f-campagne", "f-contact", "f-etat"].forEach((id) => champ(id).addEventListener("change", charger));
  charger();
}

document.addEventListener("DOMContentLoaded", () => {
  if (document.getElementById("appels-tableau")) initialiserAppels();
});
