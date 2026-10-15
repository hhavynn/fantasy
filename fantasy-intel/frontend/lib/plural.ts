/** `one` when `n` is exactly 1, otherwise `many` (default: `one` + "s"). Replaces "game(s)" in copy. */
export function plural(n: number, one: string, many = `${one}s`): string {
  return n === 1 ? one : many;
}
