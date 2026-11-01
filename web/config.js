// Where the API is. Locally the API serves this page itself, so apiBase stays empty.
// On Azure the deploy workflow replaces this file, for example:
//   window.BOL_CONFIG = { apiBase: "https://bol-api.<env>.uaenorth.azurecontainerapps.io" };
window.BOL_CONFIG = { apiBase: "" };
