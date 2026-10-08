// Cloudflare Worker: the clock for every bot in this repo.
//
// Each bot's workflow only runs when this Worker calls GitHub's
// workflow_dispatch API. (The workflows used to carry a GitHub `schedule` as a
// fallback; in three days of hourly runs it never once ran in an hour this
// Worker had missed, it only produced duplicate runs, so it was removed.)
//
// Settings -> Trigger events needs exactly ONE cron trigger:
//
//     2-59/5 * * * *        (fires at :02 :07 :12 ... :57)
//
// Each tick dispatches whichever bots are due. They take turns, so no two ever
// push their state to main at the same moment:
//
//     P-Bandai   :17 :47            every 30 min
//     KGB        :07 :37            every 30 min
//     Toymana    :12 :27 :42 :57    every 15 min — restocks most often of the
//                                   three (31 times in its first 3 days)
//
// To change one bot's frequency, change its `every` to 5, 10, 15, 20, 30 or 60
// (it must divide 60). `offset` must be one of the trigger's minutes — 2, 7,
// 12, 17 ... (2 more than a multiple of 5) — or that bot never fires.
//
// Deploy: Workers & Pages -> pbandai-trigger -> Edit code, replace everything
// with this file, Deploy. After changing triggers, Deploy again, then give it
// an hour before deciding it is broken. Cloudflare says trigger changes take
// up to 15 minutes; twice they took far longer. On 2026-09-03 a new trigger
// never fired until the Worker was redeployed; on 2026-10-08 this one was set
// around 10:05 UTC, redeployed around 10:20, and first fired at 11:27.
// Settings -> Variables and Secrets needs GITHUB_PAT (type: Secret), a
// fine-grained token for this one repo with Actions: Read and write.

const REPO = "chunkeat00/pbandai-restock-bot";

const BOTS = [
  { workflow: "check.yml",         every: 30, offset: 17 },   // P-Bandai
  { workflow: "kgb-check.yml",     every: 30, offset: 7  },   // Kelab Gasing Beyblade
  { workflow: "toymana-check.yml", every: 15, offset: 12 },   // Toymana
];

export default {
  async scheduled(controller, env, ctx) {
    // scheduledTime is the *planned* fire time, so this is the exact minute
    // even when the invocation itself starts a few seconds late.
    const minute = new Date(controller.scheduledTime).getUTCMinutes();

    for (const b of BOTS) {
      if (b.offset % 5 !== 2) console.log(`${b.workflow}: offset ${b.offset} is never a tick — it will not run`);
    }

    const due = BOTS.filter(b => (minute - b.offset + 60) % b.every === 0);
    if (!due.length) {
      console.log(`:${String(minute).padStart(2, "0")} — nothing due`);
      return;
    }
    await Promise.all(due.map(b => dispatch(b.workflow, env)));
  },
};

async function dispatch(workflow, env) {
  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`,
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
  if (!res.ok) console.log(`${workflow} dispatch failed: ${res.status} ${await res.text()}`);
}
