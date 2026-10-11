/** Only same-origin app paths may be used as a post-login destination (no open redirects). */
export function safeRedirectTarget(candidate: unknown): string | undefined {
  if (typeof candidate !== "string") return undefined;
  if (!candidate.startsWith("/") || candidate.startsWith("//") || candidate.startsWith("/\\")) return undefined;
  if (candidate === "/login" || candidate.startsWith("/login?")) return undefined;
  return candidate;
}
