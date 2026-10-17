const TOKEN_LENGTH = 48;
const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
/** Largest multiple of the alphabet size that fits in a byte; bytes above it are discarded so no character is favoured. */
const UNBIASED_LIMIT = 256 - (256 % ALPHABET.length);

export const WEBHOOK_TOKEN_LENGTH = TOKEN_LENGTH;
export const WEBHOOK_TOKEN_PATTERN = /^[A-Za-z0-9]{48}$/;
export const WEBHOOK_TOKEN_FIELD = "webhook_token";

/** A 48-character alphanumeric token from the browser's CSPRNG (the backend requires at least 32). */
export function generateWebhookToken(): string {
  let token = "";
  while (token.length < TOKEN_LENGTH) {
    const bytes = crypto.getRandomValues(new Uint8Array(TOKEN_LENGTH));
    for (const byte of bytes) {
      if (byte < UNBIASED_LIMIT && token.length < TOKEN_LENGTH) token += ALPHABET[byte % ALPHABET.length];
    }
  }
  return token;
}

/**
 * The public base of the API. The console talks to the API on its own origin (Vite proxies `/v1` in development,
 * the reverse proxy does in production), so the origin is the API base; there is no separate setting.
 */
export function apiBaseUrl(): string {
  return window.location.origin;
}

/** Where Asaas must POST: `POST /v1/webhooks/asaas/{provider_account_id}`. */
export function asaasWebhookUrl(providerAccountId: string): string {
  return `${apiBaseUrl()}/v1/webhooks/asaas/${providerAccountId}`;
}
