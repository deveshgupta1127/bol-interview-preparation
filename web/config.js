// Where the API is and how to sign in. Locally the API serves this page itself and there is no sign-in.
// On Azure the deploy workflow replaces this file, for example:
//   window.BOL_CONFIG = {
//     apiBase: "https://bol-api.<env>.centralindia.azurecontainerapps.io",
//     auth: { clientId: "<SPA app id>", authority: "https://bolusers.ciamlogin.com/",
//             scope: "api://<API app id>/practice.readwrite" },
//   };
window.BOL_CONFIG = { apiBase: "", auth: null };
