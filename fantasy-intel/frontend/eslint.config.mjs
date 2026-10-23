import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

// League-scoped links and forms (multi-league site). On a league page (`/l/<lg>/...`) a bare in-app href or form action
// such as `/players?id=1` leaves the league prefix, and proxy.ts then routes it by the last-viewed-league cookie, which
// another tab may have changed, so the click can land in a different league than the page. App code links through
// `@/components/LeagueLink` (a drop-in `next/link` that scopes string hrefs to the page's league) or `TeamLink`, and builds
// form actions and pushed URLs with `withLeague` (server, from `currentLeague()`) or `useLeagueHref()` (client).
const LEAGUE_LINK_MESSAGE = "Use `import Link from \"@/components/LeagueLink\"` (or TeamLink): a bare next/link href leaves "
  + "the page's league (/l/<lg>/...) and goes through the last-viewed-league cookie redirect. See lib/league-scope.ts.";
const LEAGUE_URL_MESSAGE = "A literal in-app URL here leaves the page's league (/l/<lg>/...). Build it with "
  + "withLeague(path, (await currentLeague())?.id) on the server or useLeagueHref() in a client component.";
const APP_FILES = ["app/**/*.{ts,tsx,js,jsx}", "components/**/*.{ts,tsx,js,jsx}"];
// TODO(#264): the waiver pages are being converted in that open PR; drop this entry once it merges and they use
// LeagueLink and scoped form actions. Both rules below skip it until then.
const PENDING_CONVERSION = ["app/waivers/**"];

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
      ...PENDING_CONVERSION,
    ],
    rules: {
      "no-restricted-imports": ["error", { paths: [{ name: "next/link", message: LEAGUE_LINK_MESSAGE }] }],
    },
  },
  {
    files: APP_FILES,
    ignores: PENDING_CONVERSION,
    rules: {
      "no-restricted-syntax": ["error",
        // <form action="/page">, action={"/page"} and action={`/page...`}
        { selector: "JSXAttribute[name.name='action'] > Literal[value=/^\\/(?!\\/)/]", message: LEAGUE_URL_MESSAGE },
        { selector: "JSXAttribute[name.name='action'] > JSXExpressionContainer > Literal[value=/^\\/(?!\\/)/]", message: LEAGUE_URL_MESSAGE },
        { selector: "JSXAttribute[name.name='action'] > JSXExpressionContainer > TemplateLiteral > TemplateElement:first-child[value.raw=/^\\/(?!\\/)/]", message: LEAGUE_URL_MESSAGE },
        // router.push("/page") and router.replace(`/page...`)
        { selector: "CallExpression[callee.object.name='router'][callee.property.name=/^(push|replace)$/] > Literal.arguments:first-child[value=/^\\/(?!\\/)/]", message: LEAGUE_URL_MESSAGE },
        { selector: "CallExpression[callee.object.name='router'][callee.property.name=/^(push|replace)$/] > TemplateLiteral.arguments:first-child > TemplateElement:first-child[value.raw=/^\\/(?!\\/)/]", message: LEAGUE_URL_MESSAGE },
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
