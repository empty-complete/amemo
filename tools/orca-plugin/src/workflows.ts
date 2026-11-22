export const workflows = [
  { value: 'remember', label: 'Remember', argument: 'What to remember (optional)' },
  { value: 'verify', label: 'Verify', argument: 'Number of items (default 20)' },
  { value: 'ingest', label: 'Ingest', argument: 'Document path or obj:UUID, with optional pages' }
]

export function preparePrompt(action: string, argument: string): string {
  const value = argument.trim()
  switch (action) {
    case 'remember':
      return [
        'Use the amemo-remember skill if installed and write through amemo MCP.',
        value ? `Findings to remember: ${value}` : 'Record one to three durable findings from this conversation.',
        'Check for duplicates and give each record a source or basis. Report the atm: references and statuses.'
      ].join('\n')
    case 'verify':
      if (value && !/^[1-9]\d{0,3}$/.test(value)) throw new Error('Enter a whole number from 1 to 9999.')
      return [
        'Use the amemo-verify skill if installed. Read the semantic review queue through amemo MCP.',
        `Review up to ${value || '20'} semantic queue items against their own source fragments. Leave uncertain items for human review.`,
        'Report how many were confirmed, refuted and left for review.'
      ].join('\n')
    case 'ingest':
      return [
        'Use amemo-atomize and amemo-remember if installed. Read the document scope, check amemo for prior coverage and duplicates, and cite the exact source location and quote for each claim.',
        value ? `Document and scope: ${value}` : 'Find an open document task with amemo_tasks, then claim it with amemo_task_claim.',
        'Write the records through amemo MCP. Report their statuses, what went to review and what remains uncovered.'
      ].join('\n')
    default:
      throw new Error('Choose an amemo workflow.')
  }
}
