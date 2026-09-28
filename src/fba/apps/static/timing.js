// User Timing entries measure the actual render path without changing calculation policy.
// Navigation starts at zero; include document and module loading in the opening sample.
let pending = {start: 0, sha: null, done: new Set()};

export function measure(stage, sha, start) {
  const end = performance.now();
  performance.measure(`fba:${stage}:${sha}`, {start, end});
  console.debug(`fba timing ${JSON.stringify({stage, state_sha256:sha, elapsed_ms:end-start})}`);
}

export function beginTiming() {
  pending = {start: performance.now(), sha: null, done: new Set()};
}

export function rendered(stage, sha) {
  if (pending.sha != null && pending.sha !== sha) return;
  pending.sha = sha;
  if (pending.done.has(stage)) return;
  pending.done.add(stage);
  const sample = pending;
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (pending !== sample) return;
    measure(stage, sha, sample.start);
  }));
}
