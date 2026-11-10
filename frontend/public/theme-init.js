// Applied before first paint so dark mode does not flash. External file: the CSP forbids inline scripts.
try {
  if (localStorage.getItem("ar-theme") === "dark") document.documentElement.classList.add("dark");
} catch (e) {
  /* storage unavailable: light theme */
}
