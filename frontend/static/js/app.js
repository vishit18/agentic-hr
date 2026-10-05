// Small glue for HTMX. No framework.

// Send the CSRF token with every HTMX request (token comes from <meta name="csrf-token">).
document.addEventListener("htmx:configRequest", (event) => {
  const meta = document.querySelector('meta[name="csrf-token"]');
  if (meta) {
    event.detail.headers["X-CSRF-Token"] = meta.content;
  }
});

// Show a readable message if an HTMX request fails instead of failing silently.
document.addEventListener("htmx:responseError", (event) => {
  const status = event.detail.xhr ? event.detail.xhr.status : "unknown";
  console.error("Request failed with status", status);
  alert("Something went wrong (" + status + "). Please try again, or use 'Flag a problem'.");
});