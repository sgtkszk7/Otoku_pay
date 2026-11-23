// Maximise the yearly reward across favourite stores. Each capped payment method
// shares one allowance across all stores. Reverse edges allow a previous choice
// to move when another store benefits more from the same allowance.
function allocateRewards(entries) {
  const graph = [], tracks = [];
  function node() { graph.push([]); return graph.length - 1; }
  function edge(from, to, capacity, cost) {
    const forward = {to, left:capacity, cost, back:graph[to].length};
    graph[to].push({to:from, left:0, cost:-cost, back:graph[from].length});
    graph[from].push(forward);
    return forward;
  }
  const source = node(), sink = node(), caps = new Map();
  for (const entry of entries) {
    const store = node();
    edge(source, store, entry.yen, 0);
    for (const row of entry.rows) {
      const add = (target, rate) => {
        const route = edge(store, target, entry.yen, -rate);
        tracks.push({entry, row, route, rate});
      };
      if (row.cap) {
        const monthly = row.cap.period === "month";
        const key = monthly ? row.m.id + ":" + entry.period.key : row.m.id;
        if (!caps.has(key)) {
          const capNode = node();
          caps.set(key, capNode);
          edge(capNode, sink, row.cap.yen * (monthly ? entry.period.capMonths : 1), 0);
        }
        add(caps.get(key), row.rate);
        add(sink, row.afterRate);
      } else {
        add(sink, row.rate);
      }
    }
  }
  while (true) {
    const dist = graph.map(()=>Infinity), previous = [];
    dist[source] = 0;
    for (let i = 1; i < graph.length; i++) {
      let changed = false;
      for (let from = 0; from < graph.length; from++) {
        if (from === sink || !Number.isFinite(dist[from])) continue;
        for (const e of graph[from]) {
          const cost = dist[from] + e.cost;
          if (e.to !== source && e.left > 0 && cost < dist[e.to] - 1e-9) {
            dist[e.to] = cost; previous[e.to] = {from, e}; changed = true;
          }
        }
      }
      if (!changed) break;
    }
    if (!previous[sink]) break;
    let yen = Infinity;
    for (let i=sink; i!==source; i=previous[i].from) yen = Math.min(yen, previous[i].e.left);
    for (let i=sink; i!==source; i=previous[i].from) {
      const e = previous[i].e;
      e.left -= yen; graph[e.to][e.back].left += yen;
    }
  }
  const amounts = new Map(), used = new Set();
  let total = 0;
  for (const {entry, row, route, rate} of tracks) {
    const allocated = graph[route.to][route.back].left;
    if (!allocated) continue;
    const reward = allocated * rate / 100;
    amounts.set(entry.s.id, (amounts.get(entry.s.id) || 0) + reward);
    total += reward; used.add(row.m.id);
    if (row.charger) used.add(row.charger.id);
  }
  return {total, amounts, used};
}
