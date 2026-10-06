// Écran Analyses : demande d'analyse à l'IA (statistiques agrégées uniquement).

document.addEventListener("DOMContentLoaded", () => {
  const bouton = document.getElementById("btn-analyste");
  if (!bouton) return;
  const sortie = document.getElementById("resultat-analyste");
  const zone = document.getElementById("rapport-courant");
  const jours = Number(document.getElementById("panneau-analyste").dataset.jours) || null;

  bouton.addEventListener("click", async () => {
    bouton.disabled = true;
    zone.hidden = true;
    sortie.className = "resultat";
    sortie.textContent = "L'IA lit tes statistiques… (10 à 40 secondes)";
    try {
      const rapport = await api("/api/analyses/ia", {
        method: "POST", silencieux: true,
        body: JSON.stringify({ jours, question: document.getElementById("an-question").value }),
      });
      zone.replaceChildren();
      const date = document.createElement("p");
      date.className = "aide";
      date.textContent = "Analyse du " + rapport.date.slice(0, 10) + (rapport.question ? " · « " + rapport.question + " »" : "")
        + " · Générée par IA : à vérifier avant de décider.";
      const texte = document.createElement("div");
      texte.className = "rapport-texte";
      texte.textContent = rapport.texte;  // textContent : jamais d'HTML injecté depuis la réponse de l'IA
      zone.append(date, texte);
      zone.hidden = false;
      afficherResultat(sortie, "ok", "analyse terminée (elle est conservée dans l'historique).");
    } catch (e) {
      afficherResultat(sortie, "erreur", e.message);
    } finally {
      bouton.disabled = false;
    }
  });
});
