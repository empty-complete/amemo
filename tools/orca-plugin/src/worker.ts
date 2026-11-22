import type { OrcaWorkerApi } from 'orca/worker'
import type { RecordView, SearchItem } from './protocol'

const BASE = 'http://127.0.0.1:8767/api/v1/'

async function get<T>(path: string, signal: AbortSignal): Promise<T> {
  let response: Response
  try {
    response = await fetch(BASE + path, {
      headers: { Accept: 'application/json' },
      redirect: 'error',
      signal: AbortSignal.any([signal, AbortSignal.timeout(8000)])
    })
  } catch {
    throw new Error('Could not reach amemo at 127.0.0.1:8767. Check that the local service is running.')
  }
  if (response.status === 401 || response.status === 403) {
    throw new Error('The amemo service requires credentials. This panel uses the local no-auth endpoint.')
  }
  if (!response.ok) throw new Error(`amemo returned HTTP ${response.status}.`)
  return await response.json() as T
}

export default function activate(orca: OrcaWorkerApi): void {
  orca.panels.handle('amemo.search', async ({ query }, { signal }) => {
    const text = query.trim()
    if (!text || text.length > 500) throw new Error('Enter a query of 1 to 500 characters.')
    const params = new URLSearchParams({ q: text, limit: '15' })
    const { items } = await get<{ items: SearchItem[] }>(`search?${params}`, signal)
    return items
  })

  orca.panels.handle('amemo.record', async ({ ref, revision }, { signal }) => {
    if (!/^(atm|ent):[\da-f-]{36}$/i.test(ref) || !Number.isSafeInteger(revision) || revision < 1) {
      throw new Error('Invalid amemo record reference or revision.')
    }
    return get<RecordView>(`records/${encodeURIComponent(ref)}?revision=${revision}`, signal)
  })

  orca.panels.handle('amemo.status', async (_, { signal }) => {
    const [session, stats] = await Promise.all([
      get<{ base: string; base_name?: string; semantic: string }>('session', signal),
      get<{ queue: Record<string, { queued: number }> }>('review/stats', signal)
    ])
    return {
      base: session.base_name || session.base,
      semantic: session.semantic,
      queued: Object.values(stats.queue).reduce((count, item) => count + item.queued, 0)
    }
  })
}
