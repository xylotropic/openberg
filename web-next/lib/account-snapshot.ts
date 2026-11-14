// REST and socket messages can arrive out of order; the server stamps display snapshots.
export function latestAccount(current: any, incoming: any) {
  if (!current || !incoming || current.account_id !== incoming.account_id) return incoming;
  const before = Number(current.view_at ?? current.last_sync ?? 0);
  const after = Number(incoming.view_at ?? incoming.last_sync ?? 0);
  return Number.isFinite(before) && Number.isFinite(after) && before > after ? current : incoming;
}
