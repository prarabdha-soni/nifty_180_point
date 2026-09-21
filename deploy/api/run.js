// Vercel serverless function: start the "evening update" GitHub workflow from
// the paper page, and report its status.
//   GET  /api/run  -> { latest: {status, conclusion, created_at, html_url} | null, can_run, reason }
//   POST /api/run  -> dispatches the workflow (refused while a run is in progress
//                     or within MIN_GAP_MIN of the previous one)
// Needs env GH_DISPATCH_TOKEN: a fine-grained GitHub token scoped to this one
// repository with Actions: read & write only. That is all it can do.
const REPO = "prarabdha-soni/nifty_180_point";
const WORKFLOW = "evening.yml";
const MIN_GAP_MIN = 15;

async function gh(path, init = {}) {
  const token = process.env.GH_DISPATCH_TOKEN;
  if (!token) throw new Error("GH_DISPATCH_TOKEN is not configured on Vercel");
  const r = await fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "nifty180-run-button",
      ...(init.headers || {}),
    },
  });
  if (r.status === 204) return null;
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`GitHub ${r.status}: ${body.message || "error"}`);
  return body;
}

async function latestRun() {
  const d = await gh(`/actions/workflows/${WORKFLOW}/runs?per_page=1`);
  const run = d && d.workflow_runs && d.workflow_runs[0];
  if (!run) return null;
  return { id: run.id, status: run.status, conclusion: run.conclusion,
           created_at: run.created_at, updated_at: run.updated_at, html_url: run.html_url };
}

function gate(latest) {
  if (!latest) return { can_run: true, reason: "" };
  if (latest.status !== "completed") return { can_run: false, reason: `a run is already ${latest.status.replace("_", " ")}` };
  const ageMin = (Date.now() - Date.parse(latest.created_at)) / 60000;
  if (ageMin < MIN_GAP_MIN) return { can_run: false, reason: `last run started ${Math.floor(ageMin)} min ago; wait ${Math.ceil(MIN_GAP_MIN - ageMin)} min` };
  return { can_run: true, reason: "" };
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  try {
    const latest = await latestRun();
    const g = gate(latest);
    if (req.method === "GET") return res.status(200).json({ latest, ...g });
    if (req.method !== "POST") return res.status(405).json({ error: "POST or GET" });
    if (!g.can_run) return res.status(429).json({ error: g.reason, latest });
    await gh(`/actions/workflows/${WORKFLOW}/dispatches`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ref: "main" }),
    });
    return res.status(202).json({ ok: true, message: "workflow queued; the page refreshes when it finishes" });
  } catch (e) {
    return res.status(500).json({ error: String(e.message || e) });
  }
}
