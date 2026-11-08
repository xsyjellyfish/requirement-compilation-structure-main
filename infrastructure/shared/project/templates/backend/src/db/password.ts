import { randomBytes, scryptSync, timingSafeEqual } from "node:crypto";

// Compiler-owned credential encoding. The seed contract in seed-data.ts stores
// credentials in this exact format (see infrastructure/stages/database/initial_data.py),
// so a credential endpoint must import these functions instead of hashing on its own.
// Format: scrypt$<salt hex>$<dklen 64 digest hex>, the parameters of node:crypto defaults.

const SCHEME = "scrypt";
const KEY_LENGTH = 64;

export function hashPassword(password: string): string {
  const salt = randomBytes(16).toString("hex");
  const digest = scryptSync(password, Buffer.from(salt, "hex"), KEY_LENGTH).toString("hex");
  return `${SCHEME}$${salt}$${digest}`;
}

export function verifyPassword(password: string, stored: string): boolean {
  const [scheme, salt, digest] = String(stored ?? "").split("$");
  if (scheme !== SCHEME || !salt || !digest) return false;
  const expected = scryptSync(password, Buffer.from(salt, "hex"), KEY_LENGTH);
  const actual = Buffer.from(digest, "hex");
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}
