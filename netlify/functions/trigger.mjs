// POST /.netlify/functions/trigger   body: { "action": "run" | "post" }
// Lets the dashboard start the GitHub Actions workflow on demand.
// Only callers signed in to the dashboard AND listed in dashboard_users may use it.

export default async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const { SUPABASE_URL, SUPABASE_ANON_KEY, GITHUB_TOKEN, GITHUB_REPO } = process.env;
  const GITHUB_BRANCH = process.env.GITHUB_BRANCH || "main";
  if (!SUPABASE_URL || !SUPABASE_ANON_KEY || !GITHUB_TOKEN || !GITHUB_REPO) {
    return json({ error: "Netlify env vars missing: SUPABASE_URL, SUPABASE_ANON_KEY, GITHUB_TOKEN, GITHUB_REPO" }, 500);
  }

  // 1) Verify the caller is the dashboard owner (via the is_owner() RPC, using *their* token).
  const auth = req.headers.get("authorization") || "";
  if (!auth.startsWith("Bearer ")) return json({ error: "Not signed in" }, 401);
  const ownerRes = await fetch(`${SUPABASE_URL}/rest/v1/rpc/is_owner`, {
    method: "POST",
    headers: { apikey: SUPABASE_ANON_KEY, Authorization: auth, "Content-Type": "application/json" },
    body: "{}",
  });
  if (!ownerRes.ok || (await ownerRes.json()) !== true) return json({ error: "Not allowed" }, 403);

  // 2) Dispatch the workflow.
  let body = {};
  try { body = await req.json(); } catch {}
  const forcePublish = body.action === "post";
  const gh = await fetch(
    `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/farm.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gta6-clip-farm",
      },
      body: JSON.stringify({ ref: GITHUB_BRANCH, inputs: { force_publish: forcePublish } }),
    }
  );
  if (gh.status !== 204) return json({ error: `GitHub said ${gh.status}: ${await gh.text()}` }, 502);
  return json({ ok: true, started: forcePublish ? "post one clip" : "farm run" });
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
}
