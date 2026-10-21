declare namespace Cloudflare {
  interface Env {
    DB?: D1Database;
    OPENAI_API_KEY?: string;
    OPENAI_TECHNICAL_MODEL?: string;
    BUCKET?: R2Bucket;
  }
}
