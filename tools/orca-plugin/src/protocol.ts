export interface SearchItem {
  ref: string
  revision: number
  label: string
  snippet: string
  status: string
  review_state: string
}

export type LocalizedText = string | Record<string, string | null>

export interface RecordView {
  ref: string
  revision: number
  status: string
  review_state?: string | null
  payload: {
    label?: LocalizedText
    summary_plain?: LocalizedText | null
    description?: LocalizedText | null
    body?: LocalizedText | null
  }
  sources: {
    object: string
    object_name?: string
    page?: number
    selectors?: { type: string; value?: unknown; label?: string }[]
    anchor?: string
    quote?: string
    verdict: string | null
  }[]
}

export interface MemoryStatus {
  base: string
  semantic: string
  queued: number
}

declare module 'orca' {
  interface PluginRequests {
    'amemo.search': { input: { query: string }; output: SearchItem[] }
    'amemo.record': { input: { ref: string; revision: number }; output: RecordView }
    'amemo.status': { output: MemoryStatus }
  }
}
