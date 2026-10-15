import { cookies } from "next/headers";
import { PROJECTION_COOKIE, parseProjectionSource, type ProjectionSource } from "./projection-source";

/** The visitor's projection source (cookie), ESPN unless they switched. Server components only. */
export async function getProjectionSource(): Promise<ProjectionSource> {
  return parseProjectionSource((await cookies()).get(PROJECTION_COOKIE)?.value);
}
