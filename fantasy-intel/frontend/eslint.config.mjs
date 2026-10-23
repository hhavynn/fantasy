import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import { UNSCOPED_PREFIXES } from "./lib/unscoped-prefixes.mjs";

// League-scoped links and forms (multi-league site). On a league page (`/l/<lg>/...`) a bare in-app href or form action
// such as `/players?id=1` leaves the league prefix, and proxy.ts then routes it by the last-viewed-league cookie, which
// another tab may have changed, so the click can land in a different league than the page. App code links through
// `@/components/LeagueLink` (a drop-in `next/link` that scopes string hrefs to the page's league) or `TeamLink`, and builds
// form actions and pushed URLs with `withLeague` (server, from `currentLeague()`) or `useLeagueHref()` (client).
const LEAGUE_LINK_MESSAGE = "Use `import Link from \"@/components/LeagueLink\"` (or TeamLink): a bare next/link href leaves "
  + "the page's league (/l/<lg>/...) and goes through the last-viewed-league cookie redirect. See lib/league-scope.ts.";
const LEAGUE_URL_MESSAGE = "A literal in-app URL here leaves the page's league (/l/<lg>/...). Build it with "
  + "withLeague(path, (await currentLeague())?.id) on the server or useLeagueHref() in a client component.";
const APP_FILES = ["app/**/*.{ts,tsx,js,jsx}", "components/**/*.{ts,tsx,js,jsx}", "lib/**/*.tsx"];
// A string, or a template literal's first chunk, that is an in-app absolute path ("/x", not "//host" or "?q").
const IN_APP = "/^\\/(?!\\/)/";
const inAppUrl = (parent) => [
  `${parent} > Literal[value=${IN_APP}]`,
  `${parent} > JSXExpressionContainer > Literal[value=${IN_APP}]`,
  `${parent} > JSXExpressionContainer > TemplateLiteral > TemplateElement:first-child[value.raw=${IN_APP}]`,
];
const ROUTER_CALL = "CallExpression[callee.object.name='router'][callee.property.name=/^(push|replace)$/]";
// In a client component, `.push("/x")` / `.replace("/x")` on any other object (`nav.push`, `useRouter().replace`, ...)
// shaped like a navigation: the URL alone or followed by an options object. So `items.push("/a", "/b")`,
// `text.replace("/", "-")` (a second argument that is not an object), server files and any call whose first argument
// does not start with a single `/` are not flagged.
const CLIENT_NAV_CALL = "Program:has(> ExpressionStatement[directive='use client']) "
  + "CallExpression[callee.type='MemberExpression'][callee.property.name=/^(push|replace)$/]:not([callee.object.name='router'])"
  + ":matches([arguments.length=1], [arguments.1.type='ObjectExpression'])";
// `redirect("/players")` / `permanentRedirect(...)` to an in-app league page, including the league home with a query or
// hash (`/?team=1`). Unscoped targets are legitimate (lib/unscoped-prefixes.mjs, the list the proxy passes through: the
// connections page, sign-in, account pages, terms), as are already-scoped `/l/...` targets and the bare home page `/`
// (sign-out).
const UNSCOPED = UNSCOPED_PREFIXES.map((p) => p.slice(1)).join("|");
const LEAGUE_PAGE = `/^\\/(?!$|\\/|(?:l|${UNSCOPED})(?:[\\/.?#]|$))/`;
const REDIRECT_CALL = "CallExpression[callee.name=/^(redirect|permanentRedirect)$/]";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    files: APP_FILES,
    ignores: [
      // The wrappers themselves render next/link.
      "components/LeagueLink.tsx",
      "components/TeamLink.tsx",
      // Links to other leagues and to the classic site (`/?league=clear`): deliberately not the page's league.
      "components/LeagueSwitcher.tsx",
      // The connections page's cross-league action center: every href is already built for its own league
      // (lib/action-center.ts), so a "page's league" wrapper would mislead a reader.
      "app/leagues/ActionCenter.tsx",
    ],
    rules: {
      // Type-only imports (`import type { LinkProps } from "next/link"`) render nothing and stay allowed.
      "no-restricted-imports": ["error", { paths: [{ name: "next/link", message: LEAGUE_LINK_MESSAGE, allowTypeImports: true }] }],
    },
  },
  {
    files: APP_FILES,
    rules: {
      "no-restricted-syntax": ["error",
        ...[
          // <form action="/page">, <button formAction={"/page"}>, action={`/page...`}
          ...inAppUrl("JSXAttribute[name.name=/^(action|formAction)$/]"),
          // a raw <a href="/page"> (use LeagueLink, which also passes unscoped pages such as /terms through unchanged)
          ...inAppUrl("JSXOpeningElement[name.name='a'] > JSXAttribute[name.name='href']"),
          // router.push("/page") and router.replace(`/page...`)
          `${ROUTER_CALL} > Literal.arguments:first-child[value=${IN_APP}]`,
          `${ROUTER_CALL} > TemplateLiteral.arguments:first-child > TemplateElement:first-child[value.raw=${IN_APP}]`,
          // in a client component, the same on any object: `nav.push("/page")`, `useRouter().replace(`/page...`)`
          `${CLIENT_NAV_CALL} > Literal.arguments:first-child[value=${IN_APP}]`,
          `${CLIENT_NAV_CALL} > TemplateLiteral.arguments:first-child > TemplateElement:first-child[value.raw=${IN_APP}]`,
          // redirect("/page") in a server component or action (unscoped and /l/... targets stay allowed)
          `${REDIRECT_CALL} > Literal.arguments:first-child[value=${LEAGUE_PAGE}]`,
          `${REDIRECT_CALL} > TemplateLiteral.arguments:first-child > TemplateElement:first-child[value.raw=${LEAGUE_PAGE}]`,
        ].map((selector) => ({ selector, message: LEAGUE_URL_MESSAGE })),
      ],
    },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
