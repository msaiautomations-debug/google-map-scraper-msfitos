(function () {
  const refreshIcons = () => {
    if (window.lucide) {
      window.lucide.createIcons();
    }
  };

  const form = document.querySelector("[data-scrape-form]");
  const submitButton = document.querySelector("[data-submit-button]");
  const runState = document.querySelector("[data-run-state] span");
  const fullCityToggle = document.querySelector("#scrape_full_city");
  const areaField = document.querySelector("[data-area-field]");
  const areaInput = areaField ? areaField.querySelector("input") : null;

  const syncAreaState = () => {
    if (!fullCityToggle || !areaField || !areaInput) {
      return;
    }

    const fullCity = fullCityToggle.checked;
    areaInput.disabled = fullCity;
    areaField.style.opacity = fullCity ? "0.48" : "1";
  };

  if (fullCityToggle) {
    fullCityToggle.addEventListener("change", syncAreaState);
    syncAreaState();
  }

  if (form && submitButton) {
    form.addEventListener("submit", () => {
      submitButton.disabled = true;
      submitButton.querySelector("span").textContent = "Scraping...";
      if (runState) {
        runState.textContent = "Running";
      }
    });
  }

  refreshIcons();
})();
