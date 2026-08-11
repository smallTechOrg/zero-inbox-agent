// Lightweight module-level store for live SSE values shared between layout (ActivityDrawer) and page.
type Listener = (val: number) => void
const fetchListeners = new Set<Listener>()
let _fetchedSoFar = 0

export const sseLive = {
  setFetchedSoFar(n: number) {
    _fetchedSoFar = n
    fetchListeners.forEach(l => l(n))
  },
  subscribeFetchedSoFar(l: Listener): () => void {
    fetchListeners.add(l)
    return () => { fetchListeners.delete(l) }
  },
  getFetchedSoFar() { return _fetchedSoFar },
}
