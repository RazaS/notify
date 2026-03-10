const root = document.documentElement;
const themeToggle = document.querySelector("[data-theme-toggle]");

function applyTheme(theme) {
    root.dataset.theme = theme;
    localStorage.setItem("notify-theme", theme);
    if (themeToggle) {
        themeToggle.textContent = theme === "dark" ? "Light mode" : "Dark mode";
    }
}

if (themeToggle) {
    applyTheme(root.dataset.theme || "light");
    themeToggle.addEventListener("click", () => {
        const nextTheme = root.dataset.theme === "dark" ? "light" : "dark";
        applyTheme(nextTheme);
    });
}
