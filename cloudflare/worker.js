// Cloudflare Worker: the hourly trigger for every bot in this repo.
//
// GitHub's own `schedule` drops runs under load, so each bot's real clock is
// a cron trigger here that calls GitHub's workflow_dispatch API. Each workflow
// keeps a `schedule` of its own as a fallback, on a different minute.
//
// Deploy: Cloudflare dashboard -> Workers & Pages -> pbandai-trigger ->
// Edit code, replace everything with this file, Deploy.
// Settings -> Trigger events needs one cron per minute listed in JOBS. After
// adding a trigger, Deploy again: on 2026-09-03 a trigger added in Settings
// showed a "Next" time but never fired until the Worker was redeployed.
// Settings -> Variables and Secrets needs GITHUB_PAT (type: Secret), a
// fine-grained token for this one repo with Actions: Read and write.

const REPO = "chunkeat00/pbandai-restock-bot";

// Minute the trigger fires on -> workflow file to dispatch. Keyed on the minute
// alone rather than the whole cron string, so a trigger re-typed with
// different spacing still matches instead of silently doing nothing.
const JOBS = {
  "23": "check.yml",           // P-Bandai
  "38": "kgb-check.yml",       // Kelab Gasing Beyblade
  "46": "toymana-check.yml",   // Toymana
};

export default {
  async scheduled(event, env, ctx) {
    const minute = String(event.cron || "").trim().split(/\s+/)[0];
    const wf = JOBS[minute];
    if (!wf) {
      console.log(`no workflow for cron ${JSON.stringify(event.cron)}`);
      return;
    }

    const res = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${wf}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_PAT}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "pbandai-restock-trigger",   // GitHub rejects requests without one
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main" }),
      }
    );
    // Success is 204 No Content, so there is nothing to log on the happy path.
    if (!res.ok) console.log(`${wf} dispatch failed: ${res.status} ${await res.text()}`);
  },
};
