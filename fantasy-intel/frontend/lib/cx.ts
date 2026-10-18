/** Join class names, skipping falsy values: cx("a", cond && "b", undefined) → "a" or "a b". */
export function cx(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}
