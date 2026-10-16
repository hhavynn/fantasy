// Shared by the server actions and the client forms. Plain data only: nothing here is a secret.
import type { ConnectErrors } from "@/lib/leagues";

export type ActionState = {
  status: "idle" | "ok" | "error";
  message: string | null;
  fieldErrors?: ConnectErrors;
};

export const IDLE: ActionState = { status: "idle", message: null };
