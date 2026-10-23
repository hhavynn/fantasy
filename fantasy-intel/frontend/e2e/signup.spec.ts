import { expect, test, type Page } from "@playwright/test";
import { SIGNUP_E2E } from "../playwright.signup.config";
import { applyTheme, collectErrors, expectAccessible, expectAppBarFits, expectNoSidewaysScroll, useTheme } from "./ux";

// Sign in and Sign up (issue #270) against a synthetic Supabase Auth and API (e2e/signup-fake-server.mjs): the two launch
// gates, sign-in that never registers, intentional sign-up, the email-link callback and its failures, enumeration-safe
// answers, safe redirects, and the pages at 1440 and 375px, by keyboard and in both themes. No real email is sent.

const { fake, closed: CLOSED, open: OPEN } = SIGNUP_E2E;
type OtpRequest = { email: string; create_user: boolean; redirect_to: string; pkce: string | null; throttled?: boolean };
type FakeState = { apiOpen: boolean; otp: OtpRequest[]; users: Record<string, { app: boolean; created: boolean }> };

async function control(path: string, body?: unknown) {
  const res = await fetch(`${fake}${path}`, body === undefined ? undefined
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return res.json();
}
const reset = (apiOpen: boolean, otpDelayMs = 0, extra: { throttleSeconds?: number; refusal?: string } = {}) =>
  control("/__test/reset", { apiOpen, otpDelayMs, ...extra });
const fakeState = (): Promise<FakeState> => control("/__test/state");
const emailLink = async (email: string): Promise<string> => (await control(`/__test/link?email=${encodeURIComponent(email)}`)).link;

const emailField = (page: Page) => page.getByLabel("Email", { exact: true });
const submit = (page: Page) => page.getByRole("button", { name: /^Email me a/ });
const sentStatus = (page: Page) => page.getByRole("status").filter({ hasText: "Check your email" });
const banner = (page: Page) => page.getByRole("banner");

/** Fill the address and submit, then wait for the server action's answer (so the fake has seen the request). */
async function requestLink(page: Page, email: string) {
  await emailField(page).fill(email);
  await Promise.all([
    page.waitForResponse((r) => r.request().method() === "POST" && new URL(r.url()).origin === new URL(page.url()).origin),
    submit(page).click(),
  ]);
}
async function expectSignedIn(page: Page) {
  await expect(banner(page).getByText("Account", { exact: true })).toBeVisible();
  await expect(banner(page).getByRole("link", { name: "Sign in" })).toHaveCount(0);
}
async function expectSignedOut(page: Page) {
  await expect(banner(page).getByRole("link", { name: "Sign in" })).toBeVisible();
}

test.describe("sign-ups closed (SIGNUPS_OPEN unset, as in production)", () => {
  test("registration is visibly unavailable and sends nothing; an existing account still signs in", async ({ page }) => {
    await reset(false);
    const errors = collectErrors(page);
    await page.goto(`${CLOSED}/signup`);
    await expect(page.getByRole("heading", { level: 1, name: "Create account" })).toBeVisible();
    await expect(page.getByText("Sign-ups are not open yet", { exact: true })).toBeVisible();
    await expect(emailField(page), "no form while sign-ups are closed").toHaveCount(0);
    await expect(banner(page).getByRole("link", { name: "Sign up" }), "no Sign up in the app bar").toHaveCount(0);
    await expect(page.getByRole("main").getByRole("link", { name: "Open the demo league" })).toBeVisible();

    // Sign in is still there, from the page and from the app bar.
    await page.getByRole("main").getByRole("link", { name: "Sign in" }).click();
    await expect(page).toHaveURL(`${CLOSED}/login`);
    await expect(page.getByText(/Sign-ups.*are not\s+open yet/)).toBeVisible();
    await requestLink(page, "member@example.test");
    await expect(sentStatus(page)).toBeVisible();
    await page.goto(await emailLink("member@example.test"));
    await expect(page).toHaveURL(`${CLOSED}/leagues`);
    await expectSignedIn(page);

    const s = await fakeState();
    expect(s.otp.map((r) => r.create_user), "only a sign-in request, never an account-creating one").toEqual([false]);
    expect(Object.values(s.users).some((u) => u.created)).toBe(false);
    expect(errors).toEqual([]);
  });

  test("an account Supabase has but the API never accepted is signed out with an explanation", async ({ page }) => {
    await reset(false);
    await page.goto(`${CLOSED}/login`);
    await requestLink(page, "authonly@example.test");
    await expect(sentStatus(page)).toBeVisible();
    await page.goto(await emailLink("authonly@example.test"));
    await expect(page).toHaveURL(/\/login\?error=closed/);
    await expect(page.getByRole("main").getByText("Sign-ups are not open yet", { exact: true })).toBeVisible();
    await expect(page.getByText(/you were signed out and nothing was saved/)).toBeVisible();
    await expectSignedOut(page);
    expect((await fakeState()).users["authonly@example.test"].app).toBe(false);
  });
});

test.describe("this app open, the API closed (a mismatch)", () => {
  test("the Create account page asks the API, says not open yet and contacts nobody", async ({ page }) => {
    await reset(false);
    await page.goto(`${OPEN}/signup`);
    await expect(page.getByText("Sign-ups are not open yet", { exact: true })).toBeVisible();
    await expect(emailField(page)).toHaveCount(0);
    expect((await fakeState()).otp).toEqual([]);
  });

  test("a gate closed between the request and the link leaves no usable or promised account", async ({ page }) => {
    await reset(true);
    await page.goto(`${OPEN}/signup`);
    await requestLink(page, "late@example.test");
    await expect(sentStatus(page)).toBeVisible();
    await control("/__test/gate", { open: false });
    await page.goto(await emailLink("late@example.test"));
    // A sign-up link returns to Create account, which re-checks the gates and explains once.
    await expect(page).toHaveURL(/\/signup\?error=closed/);
    await expect(page.getByText(/not accepting new accounts yet/)).toHaveCount(1);
    await expect(emailField(page)).toHaveCount(0);
    await expectSignedOut(page);
    expect((await fakeState()).users["late@example.test"].app, "the API never accepted it").toBe(false);
  });
});

test.describe("sign-ups open on both sides (a controlled test environment)", () => {
  test("a new visitor finds Sign up, registers by email link and lands on the page they asked for", async ({ page }) => {
    await reset(true);
    const errors = collectErrors(page);
    await page.goto(`${OPEN}/leagues`);
    await expect(page.getByText("Sign in or create an account to add leagues")).toBeVisible();
    await banner(page).getByRole("link", { name: "Sign up" }).click();
    await expect(page).toHaveURL(`${OPEN}/signup`);
    await expect(page.getByText(/No password/).first()).toBeVisible();
    await expect(page.getByText(/Open the link in this browser/)).toBeVisible();
    await expect(page.getByRole("main").getByRole("link", { name: "Terms" })).toHaveAttribute("href", "/terms");
    await expect(page.getByRole("main").getByRole("link", { name: "Privacy policy" })).toHaveAttribute("href", "/privacy");

    await page.goto(`${OPEN}/signup?next=${encodeURIComponent("/terms")}`);
    await requestLink(page, "Newbie@Example.test");
    await expect(sentStatus(page)).toBeVisible();
    await expect(sentStatus(page)).toBeFocused();
    const [req] = (await fakeState()).otp;
    expect(req).toMatchObject({ email: "newbie@example.test", create_user: true, pkce: "s256" });
    expect(new URL(req.redirect_to).searchParams.get("next")).toBe("/terms");

    await page.goto(await emailLink("newbie@example.test"));
    await expect(page).toHaveURL(`${OPEN}/terms`);
    await expectSignedIn(page);
    expect((await fakeState()).users["newbie@example.test"], "created, then accepted by the API").toEqual({ app: true, created: true });
    expect(errors).toEqual([]);
  });

  test("Sign in never creates an account, even while sign-ups are open", async ({ page }) => {
    await reset(true);
    await page.goto(`${OPEN}/login`);
    await expect(page.getByRole("main").getByRole("link", { name: "Create an account" })).toHaveAttribute("href", "/signup");
    await requestLink(page, "stranger@example.test");
    await expect(sentStatus(page)).toBeVisible();
    const s = await fakeState();
    expect(s.otp).toMatchObject([{ email: "stranger@example.test", create_user: false }]);
    expect(s.users["stranger@example.test"]).toBeUndefined();
  });

  test("existing and unknown addresses get the same answer on both pages", async ({ browser }) => {
    await reset(true);
    const answers: Record<string, string[]> = { login: [], signup: [] };
    for (const path of ["login", "signup"] as const) {
      for (const email of ["member@example.test", `unknown-${path}@example.test`]) {
        const context = await browser.newContext();
        const page = await context.newPage();
        await page.goto(`${OPEN}/${path}`);
        await requestLink(page, email);
        await expect(sentStatus(page)).toBeVisible();
        answers[path].push(await page.getByRole("main").innerText());
        await context.close();
      }
    }
    expect(answers.login[0]).toBe(answers.login[1]);
    expect(answers.signup[0]).toBe(answers.signup[1]);
    const s = await fakeState();
    expect(s.otp.map((r) => [r.email, r.create_user])).toEqual([
      ["member@example.test", false], ["unknown-login@example.test", false],
      ["member@example.test", true], ["unknown-signup@example.test", true],
    ]);
  });

  test("a signed-in visitor is sent on to a safe destination", async ({ page }) => {
    await reset(true);
    await page.goto(`${OPEN}/login`);
    await requestLink(page, "member@example.test");
    await page.goto(await emailLink("member@example.test"));
    await expectSignedIn(page);
    await page.goto(`${OPEN}/signup?next=${encodeURIComponent("/terms")}`);
    await expect(page).toHaveURL(`${OPEN}/terms`);
    for (const bad of ["//evil.example/x", "https://evil.example", "/\t/evil.example", "/.//evil.example", "/%2E%2E//evil.example"]) {
      await page.goto(`${OPEN}/signup?next=${encodeURIComponent(bad)}`);
      await expect(page).toHaveURL(`${OPEN}/leagues`);
      await page.goto(`${OPEN}/login?next=${encodeURIComponent(bad)}`);
      await expect(page).toHaveURL(`${OPEN}/leagues`);
    }
    // The reviewer's reproduction: a signed-in browser on the callback with a dot-segment next once got
    // `Location: //evil.example`. It must stay on this site.
    for (const raw of ["%2F.%2F%2Fevil.example", "%2F..%2F%2Fevil.example", "%2Fa%2F..%2F%2Fevil.example", "%2F%252e%2F%2Fevil.example",
      "%2F%252E%252E%2F%2Fevil.example"]) {
      const res = await page.request.get(`${OPEN}/auth/callback?next=${raw}`, { maxRedirects: 0 });
      const location = res.headers()["location"] ?? "";
      expect(location.startsWith("/") && !location.startsWith("//"), `${raw} -> ${location}`).toBe(true);
      await page.goto(`${OPEN}/auth/callback?next=${raw}`);
      expect(new URL(page.url()).origin, raw).toBe(OPEN);
      await expect(page).toHaveURL(`${OPEN}/leagues`);
    }
  });

  test("Supabase's per-address throttle answers exactly like an unknown address, with no cookies", async ({ browser }) => {
    // A script that drops the repeat-guard cookie reaches Supabase every time. Supabase throttles only addresses it
    // emails, so the second request for an existing address is refused (over_email_send_rate_limit) while an unknown
    // one never is: the page must not show the difference.
    await reset(true, 0, { throttleSeconds: 60 });
    const answers: Record<string, string> = {};
    for (const path of ["login", "signup"] as const) {
      for (const email of ["member@example.test", `nobody-${path}@example.test`]) {
        for (const attempt of [1, 2]) {
          const context = await browser.newContext();      // no cookies, as a script would send
          const page = await context.newPage();
          await page.goto(`${OPEN}/${path}`);
          await requestLink(page, email);
          await expect(sentStatus(page)).toBeVisible();
          answers[`${path} ${email.startsWith("member") ? "existing" : "unknown"} ${attempt}`] = await page.getByRole("main").innerText();
          await context.close();
        }
      }
    }
    for (const path of ["login", "signup"]) {
      const texts = Object.entries(answers).filter(([k]) => k.startsWith(path)).map(([, v]) => v);
      expect(new Set(texts).size, `${path}: one answer for existing/unknown, first/second`).toBe(1);
    }
    // The member really was throttled on its second requests (the fake's GoTrue refused them).
    const member = (await fakeState()).otp.filter((r) => r.email === "member@example.test");
    expect(member.map((r) => !!r.throttled)).toEqual([false, true, true, true]);
  });
});

test.describe("the email link", () => {
  test("an expired sign-up link returns to Create account with a working form, not to Sign in", async ({ page }) => {
    await reset(true);
    await page.goto(`${OPEN}/signup?next=${encodeURIComponent("/terms")}`);
    await requestLink(page, "newbie@example.test");
    await expect(sentStatus(page)).toBeVisible();
    expect(new URL((await fakeState()).otp[0].redirect_to).searchParams.get("intent")).toBe("signup");
    await control("/__test/expire", { email: "newbie@example.test" });
    await page.goto(await emailLink("newbie@example.test"));
    await expect(page).toHaveURL(`${OPEN}/signup?error=expired&next=%2Fterms`);
    await expect(page.getByRole("alert").filter({ hasText: "That link has expired or was already used" })).toBeVisible();
    // The recovery form is the sign-up form: it asks Supabase to create the account, so a new visitor is not stuck.
    await page.context().clearCookies({ name: "fi_link_recent" });
    await requestLink(page, "newbie@example.test");
    await expect(sentStatus(page)).toBeVisible();
    expect((await fakeState()).otp.map((r) => r.create_user)).toEqual([true, true]);
  });

  test("the API's refusal signs out by status, whatever its wording", async ({ page }) => {
    await reset(false, 0, { refusal: "Account creation is paused." });
    await page.goto(`${CLOSED}/login`);
    await requestLink(page, "authonly@example.test");
    await page.goto(await emailLink("authonly@example.test"));
    await expect(page).toHaveURL(/\/login\?error=closed/);
    await expectSignedOut(page);
  });

  test("expired, reused, replaced, foreign-browser and broken links each say what happened and how to recover", async ({ page, browser }) => {
    await reset(false);
    // Expired.
    await page.goto(`${CLOSED}/login`);
    await requestLink(page, "member@example.test");
    await control("/__test/expire", { email: "member@example.test" });
    await page.goto(await emailLink("member@example.test"));
    await expect(page).toHaveURL(/\/login\?error=expired/);
    await expect(page.getByRole("alert").filter({ hasText: "That link has expired or was already used" })).toBeVisible();
    await expect(emailField(page), "the form to ask again is right there").toBeVisible();

    // Replaced: a newer request in this browser makes the older link unusable.
    await requestLink(page, "member2@example.test");
    await expect(sentStatus(page)).toBeVisible();
    await page.goto(`${CLOSED}/login`);
    await requestLink(page, "member@example.test");
    await expect(sentStatus(page)).toBeVisible();
    await page.goto(await emailLink("member2@example.test"));
    await expect(page).toHaveURL(/\/login\?error=used/);
    await expect(page.getByText("That link was already used or replaced")).toBeVisible();

    // A fresh link works (after the one-minute repeat guard); opened again in another browser it is refused.
    await page.context().clearCookies({ name: "fi_link_recent" });
    await requestLink(page, "member@example.test");
    await expect(sentStatus(page)).toBeVisible();
    const link = await emailLink("member@example.test");
    await page.goto(link);
    await expectSignedIn(page);
    const other = await browser.newContext();
    const otherPage = await other.newPage();
    await otherPage.goto(link);
    await expect(otherPage).toHaveURL(/\/login\?error=expired/);
    // A code carried to a browser that never asked for it.
    await reset(false);
    await page.context().clearCookies();
    await page.goto(`${CLOSED}/login`);
    await requestLink(page, "member@example.test");
    const { code } = await control("/__test/link?email=member%40example.test");
    await otherPage.goto(`${CLOSED}/auth/callback?code=${code}&next=%2Fterms`);
    await expect(otherPage).toHaveURL(/\/login\?error=browser&next=%2Fterms/);
    await expect(otherPage.getByText("Open the link in the browser that asked for it")).toBeVisible();
    // Broken: no code at all.
    await otherPage.goto(`${CLOSED}/auth/callback`);
    await expect(otherPage).toHaveURL(/\/login\?error=invalid/);
    await expect(otherPage.getByText("That link did not work")).toBeVisible();
    await other.close();
  });

  test("next is preserved through the link only when it stays on this site", async ({ page }) => {
    await reset(false);
    const league = "/l/lg_abcdefghijklmnopqrstuvwx/waivers";
    await page.goto(`${CLOSED}/login?next=${encodeURIComponent(league)}`);
    await requestLink(page, "member@example.test");
    const [req] = (await fakeState()).otp;
    expect(new URL(req.redirect_to).searchParams.get("next")).toBe(league);
    await page.goto(await emailLink("member@example.test"));
    expect(new URL(page.url()).pathname).toBe(league);

    // Hostile destinations never leave the site, whether in the page's next or in the callback's (signed out here;
    // the signed-in redirect is covered above).
    await page.context().clearCookies();
    for (const bad of ["https://evil.example", "//evil.example", "/\\evil.example", "/\t/evil.example", "/\n/evil.example"]) {
      await page.goto(`${CLOSED}/auth/callback?code=nope&next=${encodeURIComponent(bad)}`);
      expect(new URL(page.url()).origin, JSON.stringify(bad)).toBe(CLOSED);
      await page.goto(`${CLOSED}/signup?next=${encodeURIComponent(bad)}`);
      expect(new URL(page.url()).origin, JSON.stringify(bad)).toBe(CLOSED);
    }
  });
});

test.describe("form states", () => {
  test("a malformed address, a rate limit and a refused address each get specific, accessible guidance", async ({ page }) => {
    await reset(true);
    for (const base of [CLOSED, OPEN]) {
      for (const path of base === OPEN ? ["login", "signup"] : ["login"]) {
        await page.goto(`${base}/${path}`);
        await requestLink(page, "not-an-email");
        await expect(page.getByRole("alert").filter({ hasText: "Enter a valid email address" })).toBeVisible();
        await expect(emailField(page)).toHaveAttribute("aria-invalid", "true");
        await expect(emailField(page), "the address is kept for correction").toHaveValue("not-an-email");
        await requestLink(page, "bad@invalid.test");
        await expect(page.getByRole("alert").filter({ hasText: "Enter a valid email address" })).toBeVisible();
        await requestLink(page, "ratelimit@example.test");
        await expect(page.getByRole("alert").filter({ hasText: "Too many requests came from this connection" })).toBeVisible();
        await expect(emailField(page)).not.toHaveAttribute("aria-invalid", "true");
      }
    }
    expect((await fakeState()).otp.map((r) => r.email).filter((e) => e === "not-an-email"), "never sent to Supabase").toEqual([]);
  });

  test("a double submit sends one request, and asking again within a minute answers the same for any address", async ({ page }) => {
    await reset(true, 800);
    await page.goto(`${OPEN}/signup`);
    await emailField(page).fill("newbie@example.test");
    await submit(page).click();
    await expect(page.getByRole("button", { name: "Sending…" })).toBeDisabled();
    await page.getByRole("button", { name: "Sending…" }).click({ force: true });     // a second click
    await emailField(page).press("Enter");                                          // and a second Enter
    await expect(sentStatus(page)).toBeVisible();
    expect((await fakeState()).otp).toHaveLength(1);

    const again: string[] = [];
    for (const email of ["member@example.test", "nobody@example.test"]) {
      await page.goto(`${OPEN}/login`);
      await requestLink(page, email);
      await expect(sentStatus(page)).toBeVisible();
      await page.getByRole("button", { name: "ask again or use a different address" }).click();
      await expect(emailField(page)).toBeFocused();
      await expect(emailField(page)).toHaveValue(email);
      await submit(page).click();
      const alert = page.getByRole("alert").filter({ hasText: "less than a minute ago" });
      await expect(alert).toBeVisible();
      again.push(await alert.innerText());
    }
    expect(again[0]).toBe(again[1]);
    expect((await fakeState()).otp.map((r) => r.email), "the repeats never reached Supabase")
      .toEqual(["newbie@example.test", "member@example.test", "nobody@example.test"]);
  });
});

test.describe("layout, keyboard and themes", () => {
  for (const theme of ["light", "dark"] as const) {
    test(`Sign in and Create account fit, pass axe and keep tap targets (${theme})`, async ({ page }, info) => {
      await reset(true);
      await useTheme(page, theme);
      const errors = collectErrors(page);
      const phone = info.project.name.includes("375");
      for (const url of [`${OPEN}/login`, `${OPEN}/signup`, `${CLOSED}/login`, `${CLOSED}/signup`, `${OPEN}/login?error=expired`]) {
        await page.goto(url);
        await applyTheme(page, theme);
        await expect(page.locator("h1")).toHaveCount(1);
        await expectNoSidewaysScroll(page);
        await expectAppBarFits(page);
        await expectAccessible(page, `${url} ${theme}`);
        if (phone) {
          const targets = page.locator(".account-entry a, main button, main a[class]").filter({ visible: true });
          for (const t of await targets.all()) {
            const box = await t.boundingBox();
            expect(box!.height, `${url}: ${await t.innerText()} tap height`).toBeGreaterThanOrEqual(44);
          }
        }
      }
      expect(errors).toEqual([]);
    });
  }

  test("the whole request can be made from the keyboard, and focus follows the result", async ({ page }) => {
    await reset(true);
    await page.goto(`${OPEN}/signup`);
    await emailField(page).focus();
    await page.keyboard.type("keys@example.test");
    await page.keyboard.press("Tab");
    await expect(submit(page)).toBeFocused();
    const ring = await submit(page).evaluate((el) => getComputedStyle(el).outlineStyle);
    expect(ring, "a visible focus ring").not.toBe("none");
    await page.keyboard.press("Enter");
    await expect(sentStatus(page)).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "ask again or use a different address" })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(emailField(page)).toBeFocused();
    expect((await fakeState()).otp).toMatchObject([{ email: "keys@example.test", create_user: true }]);
  });
});

// Sign-out clears this app's account and league data from the browser (lib/local-data.ts): one account signs out and
// another signs in on the same browser, and a session that ends without "Sign out" is cleared on the next page too.
// The stores are seeded directly; each one's own behavior is covered by its suite. (Switching account without a
// sign-out cannot be driven here, since /login sends a signed-in visitor on; lib/local-data.test.ts covers it.)
test.describe("signing out on a shared browser", () => {
  const LG = "lg_0123456789abcdef";
  const MARKER = "fantasy-intel:account";
  const seed = (page: Page) => page.evaluate((lg) => {
    localStorage.setItem(`fi:saved-views:v1:${lg}:rankings`, "{}");
    localStorage.setItem(`fi:trade-scenarios:v1:${lg}:3`, "{}");
    localStorage.setItem(`fi:waiver-claim-plan:v1:${lg}:3`, "{}");
    localStorage.setItem("fantasy-intel:leagues", JSON.stringify([{ id: lg, name: "A private league" }]));
    sessionStorage.setItem(`fantasy-intel:team:${lg}`, "3");
    sessionStorage.setItem(`fi:trade-scenario-editing:v1:${lg}:3`, "s1");
    sessionStorage.setItem("fantasy-intel:tab-memory", JSON.stringify({ Team: { href: `/l/${lg}/matchup`, team: "3" } }));
    localStorage.setItem("fantasy-intel:theme", "dark");
    localStorage.setItem("another-app-on-this-origin", "kept");
  }, LG);
  /** This app's keys (the tab bar may record the page now open, so its memory is checked for the league instead). */
  const left = (page: Page) => page.evaluate((lg) => {
    const keys = (s: Storage) => Object.keys(s).filter((k) => k.startsWith("fi:") || k.startsWith("fantasy-intel:")).sort();
    return {
      local: keys(localStorage),
      session: keys(sessionStorage).filter((k) => k !== "fantasy-intel:tab-memory"),
      tabMemoryNamesLeague: (sessionStorage.getItem("fantasy-intel:tab-memory") ?? "").includes(lg),
      other: localStorage.getItem("another-app-on-this-origin"),
    };
  }, LG);
  const marker = (page: Page) => page.evaluate((k) => localStorage.getItem(k), MARKER);
  async function signIn(page: Page, email: string) {
    await page.context().clearCookies({ name: "fi_link_recent" });
    await page.goto(`${CLOSED}/login`);
    await requestLink(page, email);
    await page.goto(await emailLink(email));
    await expectSignedIn(page);
  }
  /** Only the theme of this app's keys (and the marker of an account now signed in), plus the other app's key. */
  async function expectCleared(page: Page, signedIn: boolean) {
    await expect.poll(() => left(page)).toEqual({
      local: (signedIn ? ["fantasy-intel:theme", MARKER] : ["fantasy-intel:theme"]).sort(), session: [], tabMemoryNamesLeague: false,
      other: "kept",
    });
  }

  test("one account signs out and another signs in: nothing of the first account remains", async ({ page }) => {
    await reset(false);
    await signIn(page, "member@example.test");
    await expect.poll(() => marker(page), "an opaque account marker, never the account id or address").toMatch(/^[A-Za-z0-9_-]{22}$/);
    const first = await marker(page);
    await seed(page);
    await page.reload();
    expect((await left(page)).local, "the same account keeps its data from page to page").toContain(`fi:saved-views:v1:${LG}:rankings`);

    await banner(page).getByText("Account", { exact: true }).click();
    await banner(page).getByRole("menuitem", { name: "Sign out" }).click();
    await expectSignedOut(page);
    await expectCleared(page, false);
    expect(new URL(page.url()).pathname.startsWith("/l/"), "sign-out lands outside any league").toBe(false);
    expect((await page.context().cookies()).map((c) => c.name), "no last-viewed league cookie").not.toContain("fi_league");
    expect(await page.evaluate(() => document.documentElement.dataset.theme), "the theme is a device preference").toBe("dark");

    await signIn(page, "member2@example.test");
    await expect.poll(() => marker(page)).toMatch(/^[A-Za-z0-9_-]{22}$/);
    expect(await marker(page)).not.toBe(first);
    await expectCleared(page, true);
  });

  test("a session that ends without Sign out is cleared on the next page, and the next account starts clean", async ({ page }) => {
    await reset(false);
    await signIn(page, "member@example.test");
    await expect.poll(() => marker(page)).not.toBeNull();
    await seed(page);
    // The session ends without the Sign out button (expired or revoked: the cookies are gone).
    await page.context().clearCookies();
    await page.goto(`${CLOSED}/terms`);
    await expectSignedOut(page);
    await expectCleared(page, false);
    await signIn(page, "member2@example.test");
    await expectCleared(page, true);
  });
});
