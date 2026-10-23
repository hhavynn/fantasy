// Synthetic Supabase Auth and Fantasy Intel API for e2e/signup.spec.ts (issue #270). No real email, no network, no
// real accounts: an in-memory GoTrue stand-in that speaks the few endpoints the email-link flow uses (POST /otp with
// PKCE, GET /verify, POST /token?grant_type=pkce, the JWKS, /user, /logout) and the API routes the auth pages read
// (/v2/signups, /v2/me with the SIGNUPS_ENABLED rule of fi.ensure_app_user). It mirrors GoTrue's behaviour where the
// UI depends on it: an unknown address with create_user=false is refused with otp_disabled, a link opened twice or
// after expiry comes back with error_code=otp_expired, a reused or foreign code fails the exchange. Test-only
// /__test/* routes reset the state, read what was requested and hand out the "email" link.
//
// Usage: node e2e/signup-fake-server.mjs <port>

import crypto from "node:crypto";
import http from "node:http";

const port = Number(process.argv[2] ?? 8771);
const BASE = `http://127.0.0.1:${port}`;
const { privateKey, publicKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
const JWK = { ...publicKey.export({ format: "jwk" }), kid: "signup-e2e", alg: "RS256", use: "sig" };

// Scripted Supabase answers for request-level problems, by address.
const FORCED = {
  "ratelimit@example.test": [429, { code: "over_email_send_rate_limit", msg: "email rate limit exceeded" }],
  "bad@invalid.test": [400, { code: "email_address_invalid", msg: "Email address \"bad@invalid.test\" is invalid" }],
};

let state;
function reset({ apiOpen = false, otpDelayMs = 0 } = {}) {
  state = {
    apiOpen,
    otpDelayMs,
    // member: a Supabase user the API knows. authonly: a Supabase user the API has never accepted (no fi.app_users row).
    users: new Map([
      ["member@example.test", { id: crypto.randomUUID(), app: true, created: false }],
      ["member2@example.test", { id: crypto.randomUUID(), app: true, created: false }],
      ["authonly@example.test", { id: crypto.randomUUID(), app: false, created: false }],
    ]),
    otp: [],
    flows: [],
  };
}
reset();

const b64u = (v) => Buffer.from(v).toString("base64url");
function sign(payload) {
  const head = b64u(JSON.stringify({ alg: "RS256", typ: "JWT", kid: JWK.kid }));
  const body = b64u(JSON.stringify(payload));
  const sig = crypto.sign("RSA-SHA256", Buffer.from(`${head}.${body}`), privateKey);
  return `${head}.${body}.${b64u(sig)}`;
}
function verify(token) {
  const [head, body, sig] = String(token ?? "").split(".");
  if (!sig) return null;
  const ok = crypto.verify("RSA-SHA256", Buffer.from(`${head}.${body}`), publicKey, Buffer.from(sig, "base64url"));
  if (!ok) return null;
  const claims = JSON.parse(Buffer.from(body, "base64url").toString());
  return claims.exp * 1000 > Date.now() ? claims : null;
}
const userById = (id) => [...state.users.entries()].find(([, u]) => u.id === id);
function userJson(email, u) {
  return { id: u.id, aud: "authenticated", role: "authenticated", email, email_confirmed_at: new Date().toISOString(),
    app_metadata: { provider: "email", providers: ["email"] }, user_metadata: {}, identities: [], created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(), is_anonymous: false };
}

function send(res, status, body, headers = {}) {
  // GoTrue's API version header: supabase-js reads an error's `code` only from a versioned response.
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store", "X-Supabase-Api-Version": "2024-01-01", ...headers });
  res.end(body === undefined ? "" : JSON.stringify(body));
}
const readJson = (req) => new Promise((resolve) => {
  let raw = "";
  req.on("data", (c) => { raw += c; });
  req.on("end", () => { try { resolve(raw ? JSON.parse(raw) : {}); } catch { resolve({}); } });
});
const bearer = (req) => (req.headers.authorization ?? "").replace(/^Bearer /, "");
const latestFlow = (email) => [...state.flows].reverse().find((f) => f.email === email);

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, BASE);
  const path = url.pathname;
  try {
    // ---- test control ----
    if (path === "/__test/reset" && req.method === "POST") { reset(await readJson(req)); return send(res, 200, { ok: true }); }
    if (path === "/__test/gate" && req.method === "POST") { state.apiOpen = (await readJson(req)).open === true; return send(res, 200, { ok: true }); }
    if (path === "/__test/state") {
      return send(res, 200, {
        apiOpen: state.apiOpen,
        otp: state.otp,
        users: Object.fromEntries([...state.users.entries()].map(([e, u]) => [e, { app: u.app, created: u.created }])),
      });
    }
    if (path === "/__test/link") {
      const f = latestFlow(url.searchParams.get("email"));
      if (!f) return send(res, 404, { error: "no link for that address" });
      return send(res, 200, { link: `${BASE}/auth/v1/verify?token=${f.token}&type=magiclink&redirect_to=${encodeURIComponent(f.redirectTo)}`, code: f.code });
    }
    if (path === "/__test/expire" && req.method === "POST") {
      const f = latestFlow((await readJson(req)).email);
      if (f) f.expired = true;
      return send(res, 200, { ok: !!f });
    }

    // ---- Supabase Auth (GoTrue) ----
    if (path === "/auth/v1/.well-known/jwks.json") return send(res, 200, { keys: [JWK] });
    if (path === "/auth/v1/otp" && req.method === "POST") {
      const body = await readJson(req);
      const email = String(body.email ?? "").toLowerCase();
      const redirectTo = url.searchParams.get("redirect_to") ?? "";
      state.otp.push({ email, create_user: body.create_user, redirect_to: redirectTo, pkce: body.code_challenge_method ?? null });
      if (state.otpDelayMs) await new Promise((r) => setTimeout(r, state.otpDelayMs));
      if (FORCED[email]) return send(res, ...FORCED[email]);
      let user = state.users.get(email);
      if (!user && body.create_user !== true) return send(res, 422, { code: "otp_disabled", msg: "Signups not allowed for otp" });
      if (!user) {
        user = { id: crypto.randomUUID(), app: false, created: true };    // GoTrue creates the auth user here
        state.users.set(email, user);
      }
      state.flows.push({ email, token: crypto.randomBytes(16).toString("hex"), code: crypto.randomUUID(), redirectTo,
        challenge: body.code_challenge, method: body.code_challenge_method, verified: false, used: false, expired: false });
      return send(res, 200, {});
    }
    if (path === "/auth/v1/verify") {
      const f = state.flows.find((x) => x.token === url.searchParams.get("token"));
      const to = new URL(f?.redirectTo || url.searchParams.get("redirect_to") || `${BASE}/`);
      if (!f || f.verified || f.expired) {
        to.searchParams.set("error", "access_denied");
        to.searchParams.set("error_code", "otp_expired");
        to.searchParams.set("error_description", "Email link is invalid or has expired");
      } else {
        f.verified = true;
        to.searchParams.set("code", f.code);
      }
      res.writeHead(303, { Location: to.toString() });
      return res.end();
    }
    if (path === "/auth/v1/token" && url.searchParams.get("grant_type") === "pkce" && req.method === "POST") {
      const body = await readJson(req);
      const f = state.flows.find((x) => x.code === body.auth_code);
      if (!f || f.used) return send(res, 404, { code: "flow_state_not_found", msg: "invalid flow state, no valid flow state found" });
      const hashed = f.method === "s256" ? crypto.createHash("sha256").update(String(body.code_verifier)).digest("base64url") : body.code_verifier;
      if (hashed !== f.challenge) return send(res, 400, { code: "bad_code_verifier", msg: "code challenge does not match previously saved code verifier" });
      f.used = true;
      const u = state.users.get(f.email);
      const now = Math.floor(Date.now() / 1000);
      const access = sign({ iss: `${BASE}/auth/v1`, sub: u.id, aud: "authenticated", role: "authenticated", email: f.email,
        exp: now + 3600, iat: now, session_id: crypto.randomUUID(), is_anonymous: false, aal: "aal1" });
      return send(res, 200, { access_token: access, token_type: "bearer", expires_in: 3600, expires_at: now + 3600,
        refresh_token: crypto.randomBytes(12).toString("hex"), user: userJson(f.email, u) });
    }
    if (path === "/auth/v1/user") {
      const claims = verify(bearer(req));
      const hit = claims && userById(claims.sub);
      return hit ? send(res, 200, userJson(hit[0], hit[1])) : send(res, 401, { code: "bad_jwt", msg: "invalid JWT" });
    }
    if (path === "/auth/v1/logout") { res.writeHead(204); return res.end(); }

    // ---- Fantasy Intel API ----
    if (path === "/health") return send(res, 200, { ok: true });
    if (path === "/v2/signups") return send(res, 200, { open: state.apiOpen });
    if (path === "/v2/me") {
      const claims = verify(bearer(req));
      const hit = claims && userById(claims.sub);
      if (!hit) return send(res, 401, { detail: "Sign in to use this endpoint." });
      const u = hit[1];
      if (!u.app && !state.apiOpen) return send(res, 403, { detail: "Sign-ups are not open yet." });
      u.app = true;                                                     // fi.ensure_app_user accepted the account
      return send(res, 200, { user_id: u.id, is_platform_admin: false, created_at: new Date().toISOString(), pins: [] });
    }
    if (path === "/v2/providers") return send(res, 200, { providers: [] });
    if (path === "/v2/leagues") return send(res, 200, { leagues: [] });
    return send(res, 404, { detail: "Not Found" });
  } catch (err) {
    return send(res, 500, { detail: String(err) });
  }
});

server.listen(port, "127.0.0.1", () => console.log(`signup fake auth + API on ${BASE}`));
