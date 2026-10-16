"use server";

import { cookies } from "next/headers";
import { PROJECTION_COOKIE, parseProjectionSource } from "./projection-source";

/** Remember the projection source for a year; pages re-render with it on the next request. */
export async function setProjectionSource(value: string): Promise<void> {
  (await cookies()).set(PROJECTION_COOKIE, parseProjectionSource(value), {
    path: "/", maxAge: 60 * 60 * 24 * 365, sameSite: "lax",
  });
}
