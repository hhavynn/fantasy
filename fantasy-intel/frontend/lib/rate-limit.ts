// A fixed-window request counter keyed by client IP, for the web proxy (multi-league Phase 6). Pure, so it runs under
// the unit-test runner. Counts live in one server instance's memory: a backstop for a single noisy client; the Vercel
// WAF rule (one free rule on Hobby) is the limit across instances and regions.

export type Verdict = { ok: true } | { ok: false; retryAfterSeconds: number };

export class FixedWindowLimiter {
  private readonly hits = new Map<string, { start: number; count: number }>();

  readonly limit: number;
  readonly windowMs: number;
  readonly maxKeys: number;

  constructor(limit: number, windowMs: number, maxKeys = 10_000) {
    this.limit = limit;
    this.windowMs = windowMs;
    this.maxKeys = maxKeys;
  }

  /** Count one request from `key` at `now` (ms). */
  check(key: string, now: number): Verdict {
    let entry = this.hits.get(key);
    if (!entry || now - entry.start >= this.windowMs) {
      this.hits.delete(key);           // re-inserted below, so the map stays ordered oldest window first
      this.makeRoom(now);
      entry = { start: now, count: 0 };
      this.hits.set(key, entry);
    }
    entry.count += 1;
    if (entry.count <= this.limit) return { ok: true };
    return { ok: false, retryAfterSeconds: Math.max(1, Math.ceil((entry.start + this.windowMs - now) / 1000)) };
  }

  get size(): number {
    return this.hits.size;
  }

  /** Keep memory bounded: drop finished windows, then (still full) the oldest ones. */
  private makeRoom(now: number): void {
    if (this.hits.size < this.maxKeys) return;
    for (const [key, entry] of this.hits) {
      if (now - entry.start >= this.windowMs) this.hits.delete(key);
    }
    for (const key of this.hits.keys()) {
      if (this.hits.size < this.maxKeys) break;
      this.hits.delete(key);
    }
  }
}

/** The client's IP as Vercel reports it (it overwrites any client-sent x-forwarded-for); null when unknown. */
export function clientIp(get: (name: string) => string | null): string | null {
  const forwarded = get("x-forwarded-for")?.split(",")[0]?.trim();
  return forwarded || get("x-real-ip")?.trim() || null;
}

/** RATE_LIMIT_PER_MINUTE: a positive whole number turns the limit on; anything else (unset, 0) leaves it off. */
export function limitFromEnv(value: string | undefined): number | null {
  const n = Number((value ?? "").trim());
  return Number.isInteger(n) && n > 0 ? n : null;
}
