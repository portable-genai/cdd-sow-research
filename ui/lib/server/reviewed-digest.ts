export function requireReviewedDigest(
  actualDigest: string,
  expectedDigest: string | undefined,
  production: boolean,
  expectedSettingsDigest: string | undefined,
): void {
  if (production && (!expectedDigest || !expectedSettingsDigest)) {
    throw new Error("production requires expected manifest and settings digests");
  }
  if (expectedDigest && actualDigest !== expectedDigest) {
    throw new Error("installation manifest digest does not match reviewed deployment");
  }
}
