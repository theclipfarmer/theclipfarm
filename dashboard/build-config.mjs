// Netlify build step: writes the public Supabase config the dashboard needs.
// The anon key is designed to be public; Row Level Security protects the data.
import { writeFileSync } from "node:fs";

const url = process.env.SUPABASE_URL || "";
const anon = process.env.SUPABASE_ANON_KEY || "";
if (!url || !anon) {
  console.warn("⚠️  SUPABASE_URL / SUPABASE_ANON_KEY not set in Netlify env — the dashboard will show a setup notice.");
}
writeFileSync(
  new URL("./public/config.js", import.meta.url),
  `window.CLIPFARM_CONFIG = ${JSON.stringify({ supabaseUrl: url, supabaseAnonKey: anon })};\n`
);
console.log("wrote dashboard/public/config.js");
