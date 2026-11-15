// CMI Gateway API-Client (S4-1/S4-3).
//
// Alle /api/v1/*-Aufrufe laufen mit `Authorization: Bearer <token>`.
// 401 -> Token neu abfragen (Event an die UI).

const TOKEN_KEY = "cmi-gateway.read-token";

/** Gepeiltes Read-Token aus localStorage (leer, wenn noch nicht gesetzt). */
export function getStoredToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

/** Read-Token im localStorage ablegen (leere Zeichenkette = entfernen). */
export function storeToken(token) {
  try {
    if (token) {
      localStorage.setItem(TOKEN_KEY, token);
    } else {
      localStorage.removeItem(TOKEN_KEY);
    }
  } catch {
    /* localStorage nicht verfuegbar (Private Mode o. a.) */
  }
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail || "";
  }
}

/**
 * API-Aufruf mit Bearer-Header.
 *
 * @param {string} path Pfad, z. B. "/api/v1/state"
 * @param {string} token Read-Token
 * @returns {Promise<any>} geparstes JSON
 * @throws {ApiError} bei HTTP-Fehlern (401/400/500 ...)
 */
export async function apiGet(path, token) {
  const res = await fetch(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body);
    } catch {
      /* kein JSON-Koerper */
    }
    throw new ApiError(res.status, detail);
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return res.json();
  }
  return res.text();
}
